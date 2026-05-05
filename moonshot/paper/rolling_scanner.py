"""RawSurgeScanner — R24 raw-surge scan for Moonshot paper trading.

与 duo-live ``rolling_scanner`` / ``r24_raw_surge_preload`` 同漏斗：

- 候选：24hr ticker 涨幅 >= ``min_pct_chg``（全市场最多 500 名，见 ``scan_rolling_top_gainers``）
- 探测：按涨幅降序截 ``max_sr_probe`` 后再算卖量比（防 REST 风暴；与 live 一致）
- 卖量：上一整点已收盘 1h K 相对昨日日均小时主动卖额；``raw_max`` / ``raw_min_sell_surge`` 门
- 截断：``candidate_rank_mode`` 排序后取 ``top_n``，再 ``select_signals``（与 preload 同 ``candidate_rank_score``）
- 策略：``should_enter`` 用当前整点 UTC 作 ``dt``；与历史回测 hourly 桶标签可差 1h，属实盘优先。
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from moonshot.paper.live_feed import LiveFeed
from moonshot.paper.paper_account import PaperAccount
from moonshot.paper.paper_store import MoonshotPosition, PaperStore
from moonshot.r24_raw_surge_config import RawSurgeR24Config
from moonshot.r24_raw_surge_preload import candidate_rank_score
from moonshot.r24_raw_surge_strategy import RawSurgeRollingStrategy

logger = logging.getLogger(__name__)

# 与 duo-live ``live/rolling_scanner.ROLLING_24H_GAINERS_CAP`` 一致
_ROLLING_24H_GAINERS_CAP = 500


class _RawSurgeFeedAdapter:
    """给 RawSurgeRollingStrategy 用的同步适配层（面向多 symbol）。"""

    def __init__(self):
        self._listing: dict[str, datetime | None] = {}
        self._sell: dict[tuple[str, str], tuple[float | None, float | None]] = {}

    def set_listing_date(self, symbol: str, dt: datetime | None) -> None:
        self._listing[symbol] = dt

    def set_sell_surge_detail(self, symbol: str, hour_dt: datetime, sr: float | None, yavg: float | None) -> None:
        key = (symbol, hour_dt.strftime("%Y-%m-%d %H:00"))
        self._sell[key] = (sr, yavg)

    def load_listing_date(self, symbol: str) -> datetime | None:
        return self._listing.get(symbol)

    def load_sell_surge_detail(self, symbol: str, dt: datetime):
        key = (symbol, dt.strftime("%Y-%m-%d %H:00"))
        return self._sell.get(key, (None, None))


class RawSurgeScanner:
    """R24 raw-surge scanner."""

    def __init__(self, feed: LiveFeed, store: PaperStore, account: PaperAccount, config: RawSurgeR24Config):
        self._feed = feed
        self._store = store
        self._account = account
        self._config = config
        self._strategy = RawSurgeRollingStrategy(config)

    def _symbol_in_cooldown(self, symbol: str, now: datetime) -> bool:
        """True if we closed this symbol recently (within signal_cooldown_hours)."""
        exit_str = self._store.get_latest_exit_time_iso(symbol)
        if not exit_str:
            return False
        try:
            exit_dt = datetime.fromisoformat(exit_str.replace("Z", "+00:00"))
            if exit_dt.tzinfo is None:
                exit_dt = exit_dt.replace(tzinfo=UTC)
            return (now - exit_dt).total_seconds() < self._config.signal_cooldown_hours * 3600
        except Exception:
            return False

    async def scan(self):
        now = datetime.now(UTC)
        self._store.log_event("SCAN", "SYSTEM", "Starting R24 raw-surge scan")

        gainers = await self._feed.scan_rolling_top_gainers(
            min_pct_chg=self._config.min_pct_chg,
            top_n=500,  # fetch all pct-qualifying symbols; top_n cap applied after sell surge filter
            window_hours=self._config.rolling_window_hours,
            kline_prefilter_pct_ratio=getattr(
                self._config, "rolling_kline_prefilter_pct_ratio", 0.6,
            ),
            kline_prefilter_union_top=getattr(
                self._config, "rolling_kline_prefilter_union_top", 500,
            ),
        )

        if not gainers:
            symbols = await self._feed.get_usdt_symbols()
            self._store.log_event(
                "SCAN", "SYSTEM",
                f"No targets found (scanned {len(symbols)} symbols, min_chg={self._config.min_pct_chg}%)",
            )
            self._store.set_state("last_scan", json.dumps({"scan_time": now.isoformat(), "gainers": []}))
            return

        gainer_str = ", ".join(f"{s}(+{p:.1f}%)" for s, p in gainers)
        self._store.log_event("SCAN", "SYSTEM", f"Found {len(gainers)} raw candidate(s): {gainer_str}")

        open_positions = [p.symbol for p in self._store.get_open_positions()]
        initial_open_syms = set(open_positions)
        skipped = []
        accepted = []

        # Full scan records for all pct-qualifying gainers
        all_records: dict[str, dict] = {
            symbol: {"symbol": symbol, "pct_chg": round(float(pct_chg), 4), "filter_result": "通过", "sell_surge_ratio": None, "yesterday_avg_hour_sell_volume": None}
            for symbol, pct_chg in gainers
        }

        max_probe = int(getattr(self._config, "max_sr_probe", 50) or 50)
        max_probe = max(1, min(max_probe, _ROLLING_24H_GAINERS_CAP, len(gainers)))
        probe_gainers = gainers[:max_probe]
        probe_set = {s for s, _ in probe_gainers}
        rank_mode = (getattr(self._config, "candidate_rank_mode", "sr") or "sr").strip().lower()
        for symbol, _pct in gainers:
            if symbol not in probe_set:
                all_records[symbol]["filter_result"] = "探测集外"
        self._store.log_event(
            "SCAN", "SYSTEM",
            f"max_sr_probe={max_probe} / cap{_ROLLING_24H_GAINERS_CAP}, rank={rank_mode}（与 live / preload 对齐）",
        )

        # preload 桶键与 select_signals 的 dt 对齐「当前整点 UTC」（扫描落在该小时）
        hour_floor = now.replace(minute=0, second=0, microsecond=0)
        hour_key = hour_floor.strftime("%Y-%m-%d %H:00")
        prev_hour = hour_floor - timedelta(hours=1)
        preloaded: dict[str, list[tuple[str, float, float, float]]] = {hour_key: []}
        adapter = _RawSurgeFeedAdapter()

        def _fmt(pct: float, sr: float | None) -> str:
            sr_str = f"卖量×{sr:.1f}" if sr is not None else "卖量×—"
            return f"+{pct:.1f}% {sr_str}"

        # 仅对探测集算 sr；通过后收集，再按 candidate_rank_mode 截 top_n 再喂 select（对齐 duo-live / r24_preload）
        sr_passed: list[tuple[str, float, float, float | None]] = []

        for symbol, pct_chg in probe_gainers:
            if symbol in open_positions:
                self._store.log_event("SCAN", symbol, f"❌ {symbol} +{pct_chg:.1f}% — 已持仓")
                skipped.append((symbol, f"+{pct_chg:.1f}% 已持仓"))
                all_records[symbol]["filter_result"] = "已持仓"
                continue

            if self._symbol_in_cooldown(symbol, now):
                self._store.log_event("SCAN", symbol, f"❌ {symbol} +{pct_chg:.1f}% — 冷却期")
                skipped.append((symbol, f"+{pct_chg:.1f}% 冷却期"))
                all_records[symbol]["filter_result"] = "冷却期"
                continue

            sr, yavg = await self._feed.sell_surge_ratio_at_hour(symbol, prev_hour)
            all_records[symbol]["sell_surge_ratio"] = round(float(sr), 4) if sr is not None else None
            all_records[symbol]["yesterday_avg_hour_sell_volume"] = round(float(yavg), 4) if yavg is not None else None
            sr_str = f"{sr:.1f}" if sr is not None else "—"

            # Check raw_max_sell_surge if configured
            max_sr = getattr(self._config, "raw_max_sell_surge", None)
            if sr is not None and max_sr is not None and sr > max_sr:
                self._store.log_event(
                    "SCAN", symbol,
                    f"❌ {symbol} +{pct_chg:.1f}% 卖量×{sr_str}（> {max_sr}× 过高）",
                )
                skipped.append((symbol, f"+{pct_chg:.1f}% 卖量过高"))
                all_records[symbol]["filter_result"] = "卖量过高"
                continue

            if sr is None or sr <= self._config.raw_min_sell_surge:
                self._store.log_event(
                    "SCAN", symbol,
                    f"❌ {symbol} +{pct_chg:.1f}% 卖量×{sr_str}（< {self._config.raw_min_sell_surge}×）",
                )
                skipped.append((symbol, f"+{pct_chg:.1f}% 卖量×{sr_str}"))
                all_records[symbol]["filter_result"] = "卖量不足"
                continue

            min_yavg = getattr(self._config, "raw_min_yavg_sell_volume", None)
            if min_yavg is not None and float(min_yavg) > 0:
                yv = float(yavg) if yavg is not None else -1.0
                if yavg is None or yv < float(min_yavg):
                    self._store.log_event(
                        "SCAN", symbol,
                        f"❌ {symbol} +{pct_chg:.1f}% 昨均卖额{yavg if yavg is not None else '—'} < {min_yavg}",
                    )
                    skipped.append((symbol, f"+{pct_chg:.1f}% 昨均卖额过低"))
                    all_records[symbol]["filter_result"] = "昨均卖额过低"
                    continue

            sr_passed.append((symbol, float(pct_chg), float(sr), yavg))

        sr_passed.sort(
            key=lambda x: candidate_rank_score(x[1], x[2], rank_mode, x[3] if x[3] is not None else None),
            reverse=True,
        )
        topn = int(self._config.top_n) if (self._config.top_n or 0) > 0 else 0
        finalists = sr_passed[:topn] if topn else []
        finalist_syms = {s for s, _, _, _ in finalists}
        for sym, pct, sr, yavg in sr_passed:
            if sym not in finalist_syms:
                all_records[sym]["filter_result"] = "未进TopN"

        for sym, pct, sr, yavg in finalists:
            listing = await self._feed.load_listing_date(sym)
            adapter.set_listing_date(sym, listing)
            adapter.set_sell_surge_detail(sym, now, sr, yavg)
            preloaded[hour_key].append((sym, float(pct), float(sr), float(yavg or 0.0)))

        # Let strategy do final filtering and details
        _ = self._strategy.select_signals(adapter, now, preloaded)
        # Merge strategy details into all_records
        for detail in getattr(self._strategy, "last_signal_details", []):
            sym = detail["symbol"]
            if sym in all_records:
                all_records[sym].update({k: v for k, v in detail.items() if k not in ("symbol",)})

        # 与 live 一致：以 candidate_rank_mode 为优先序（在 select 之后仍按同一分数，便于同分 tie-break）
        def _yavg_for_rank(d: dict) -> float | None:
            y = d.get("yesterday_avg_hour_sell_volume")
            if y is None:
                return None
            try:
                return float(y)
            except (TypeError, ValueError):
                return None

        signal_details = sorted(
            getattr(self._strategy, "last_signal_details", []),
            key=lambda d: candidate_rank_score(
                float(d.get("pct_chg") or 0.0),
                float(d.get("sell_surge_ratio") or 0.0),
                rank_mode,
                _yavg_for_rank(d),
            ),
            reverse=True,
        )
        # Track all active symbols: existing positions + symbols accepted in this scan
        active_symbols = set(open_positions)
        for detail in signal_details:
            symbol = detail['symbol']
            pct_chg = detail['pct_chg']
            sr = detail.get('sell_surge_ratio')

            if symbol in active_symbols:
                kind = "已持仓" if symbol in initial_open_syms else "本轮已受理"
                msg = f"{_fmt(pct_chg, sr)} — {kind}"
                self._store.log_event("SCAN", symbol, f"❌ {symbol} {msg}")
                skipped.append((symbol, msg))
                if symbol in all_records:
                    all_records[symbol]["filter_result"] = kind
                continue
            if self._symbol_in_cooldown(symbol, now):
                msg = f"{_fmt(pct_chg, sr)} — 冷却期"
                self._store.log_event("SCAN", symbol, f"❌ {symbol} {msg}")
                skipped.append((symbol, msg))
                if symbol in all_records:
                    all_records[symbol]["filter_result"] = "冷却期"
                continue
            if detail.get('filter_result') != '通过':
                self._store.log_event(
                    "SCAN", symbol,
                    f"❌ {symbol} {_fmt(pct_chg, sr)} — {detail.get('filter_result')}",
                )
                skipped.append((symbol, _fmt(pct_chg, sr)))
                continue

            if len(active_symbols) >= self._config.max_positions:
                all_records[symbol]['filter_result'] = 'max_positions截断'
                skipped.append((symbol, _fmt(pct_chg, sr)))
                continue

            if len([s for s, _ in accepted]) >= self._config.top_n:
                all_records[symbol]['filter_result'] = 'top_n截断'
                skipped.append((symbol, _fmt(pct_chg, sr)))
                continue

            ok, reason, _ = self._strategy.should_enter(symbol, pct_chg, adapter, now, list(active_symbols))
            if ok:
                await self._open_position(
                    symbol,
                    pct_chg,
                    reason,
                    sell_surge_ratio=sr,
                    yesterday_avg_hour_sell_quote=detail.get("yesterday_avg_hour_sell_volume"),
                )
                accepted.append((symbol, _fmt(pct_chg, sr)))
                active_symbols.add(symbol)  # Track newly opened position
                all_records[symbol]["filter_result"] = "建仓"
            else:
                self._store.log_event("SCAN", symbol, f"❌ {symbol} {_fmt(pct_chg, sr)} — {reason}")
                skipped.append((symbol, _fmt(pct_chg, sr)))
                all_records[symbol]["filter_result"] = reason

        ok_str = " ".join(f"✅ {s}({r})" for s, r in accepted) if accepted else ""
        skip_str = " ".join(f"❌ {s}({r})" for s, r in skipped) if skipped else ""
        self._store.log_event(
            "SCAN", "SYSTEM",
            f"扫描完成 {len(accepted)}✅ {len(skipped)}❌"
            + (f" | {ok_str}" if ok_str else "")
            + (f" | {skip_str}" if skip_str else ""),
        )

        # Save full scan records (all pct-qualifying gainers)
        scan_time = now.isoformat()
        try:
            self._store.save_scan_signals(scan_time, list(all_records.values()))
        except Exception as e:
            self._store.log_event("SCAN", "SYSTEM", f"save_scan_signals failed: {e}")

        scan_snapshot = {
            "scan_time": scan_time,
            "gainers": [
                {"symbol": s, "pct_chg": round(p, 2), "status": "accepted", "detail": next((r for sym, r in accepted if sym == s), "")}
                if any(sym == s for sym, _ in accepted)
                else {"symbol": s, "pct_chg": round(p, 2), "status": "filtered", "detail": next((r for sym, r in skipped if sym == s), "")}
                for s, p in gainers
            ],
        }
        self._store.set_state("last_scan", json.dumps(scan_snapshot, ensure_ascii=False))

    async def _open_position(
        self,
        symbol: str,
        surge_pct: float,
        reason: str,
        *,
        sell_surge_ratio: float | None,
        yesterday_avg_hour_sell_quote: float | None,
    ):
        current_price = await self._feed.get_current_price(symbol)
        if not current_price:
            return

        free = float(self._account.capital)
        locked = sum(p.invest_amount for p in self._store.get_open_positions())
        total_equity = free + locked
        invest = self._strategy.compute_order_margin(free, total_equity)
        if invest <= 0:
            return
        # Apply entry slippage for short entry (worse fill = lower sell price).
        slip_bps = float(getattr(self._config, "entry_slippage_bps", 0.0) or 0.0)
        fill_price = current_price * (1 - slip_bps / 10_000.0)
        tp_price = fill_price * (1 - self._config.tp_initial)
        sl_price = fill_price * (1 + self._config.sl_threshold)

        pos = MoonshotPosition(
            symbol=symbol,
            entry_price=float(fill_price),
            entry_time=datetime.now(UTC).isoformat(),
            invest_amount=float(invest),
            position_size=float(invest / fill_price) if fill_price > 0 else 0.0,
            leverage=self._config.leverage,
            surge_pct=surge_pct,
            entry_reason=reason,
            tp_price=tp_price,
            sl_price=sl_price,
            target_pct=self._config.tp_initial * 100,
            stop_loss_pct=self._config.sl_threshold * 100,
            capital_before=free,
            highest_price=float(fill_price),
            tp_initial_price=tp_price,
            signal_price=current_price,
            sell_surge_ratio=sell_surge_ratio,
            yesterday_avg_hour_sell_volume=yesterday_avg_hour_sell_quote,
        )
        if not self._account.open_position(pos):
            self._store.log_event(
                "SCAN",
                symbol,
                f"❌ {symbol} +{surge_pct:.1f}% — 资金不足，未开仓",
            )
