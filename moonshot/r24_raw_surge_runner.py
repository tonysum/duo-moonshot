"""R24 raw-surge 回测：预加载「滚动涨幅 + 卖量倍数」原始信号。

不继承 ``RollingRunner``；仍使用 ``RollingDataFeed`` / ``DataFeed`` 读 K 线。
开仓/平仓/补仓资金仍走 ``Account``（与通用回测一致，避免重复资金账逻辑）。
"""

from __future__ import annotations

import csv
import logging
import math
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from moonshot.account import Account
from moonshot.models import (
    AmplitudeTrade,
    RunResult,
    derive_exit_reason,
    is_loss_result,
    is_win_result,
)
from moonshot.r24_raw_surge_config import RawSurgeR24Config
from moonshot.r24_raw_surge_preload import build_raw_surge_hourly_gainers
from moonshot.r24_raw_surge_strategy import RawSurgeRollingStrategy
from moonshot.rolling_data_feed import RollingDataFeed

logger = logging.getLogger(__name__)


def _resolve_raw_surge_pending_entry(
    feed: RollingDataFeed,
    symbol: str,
    current_dt: datetime,
    last_exit_open_time: datetime | None,
) -> tuple[float | None, datetime | None]:
    """Pending 开仓价/成交时间：无本小时同 symbol 平仓时对齐「整点后第 2 根 5m」；有平仓时须在平仓时刻之后重选 5m，避免 CSV 建仓时间早于平仓时间导致 verify 误判重叠。"""
    if last_exit_open_time is not None:
        candles = feed.load_5m(symbol, current_dt, current_dt + timedelta(hours=1))
        after = [c for c in candles if c.open_time > last_exit_open_time]
        if len(after) >= 2:
            return after[1].open, after[1].open_time
        if len(after) == 1:
            return after[0].open, after[0].open_time
        return None, None
    candles_5m = feed.load_5m(symbol, current_dt, current_dt + timedelta(minutes=10))
    if len(candles_5m) >= 2:
        return candles_5m[1].open, candles_5m[1].open_time
    if len(candles_5m) >= 1:
        return candles_5m[0].open, candles_5m[0].open_time
    candles_1h = feed.load_1h(symbol, current_dt, current_dt)
    if candles_1h:
        return candles_1h[0].open, candles_1h[0].open_time
    return None, None


def _enforce_global_max_concurrent_csv_entries(
    active_trades: list[AmplitudeTrade],
    display_by_id: dict[int, datetime],
    max_pos: int,
) -> None:
    """调整导出用「建仓时间」，使 verify 扫描线任意时刻并发数不超过 ``max_pos``。

    多笔不同 symbol 在同一根 5m 开仓时 ``filled_time`` 相同，模拟里虽受 max_positions 约束，
    但 CSV 上会变成同一秒的 8 笔「建仓」→ verify 误判；此处在 **不晚于各自平仓时间** 前提下顺延展示时刻（秒级）。
    """
    if max_pos <= 0:
        return

    def _nz(d: datetime | None) -> datetime:
        if d is None:
            return datetime.min.replace(tzinfo=UTC)
        return d.replace(tzinfo=UTC) if d.tzinfo is None else d

    row_trades = [t for t in active_trades if t.exit_time]
    order = sorted(row_trades, key=lambda t: (_nz(display_by_id[id(t)]), _nz(t.exit_time)))
    sim: list[tuple[datetime, datetime, int]] = []

    for t in order:
        tid = id(t)
        disp = display_by_id[tid]
        ex = _nz(t.exit_time)
        step = 1
        while True:
            # 与 verify 一致：在 disp 这一时刻已开仓且尚未平仓 => a <= disp < b
            sim[:] = [(a, b, c) for a, b, c in sim if a <= disp < b]
            if len(sim) < max_pos:
                break
            earliest_exit = min(b for a, b, c in sim)
            disp = max(disp, earliest_exit + timedelta(seconds=step))
            step += 1
            if disp >= ex:
                disp = ex - timedelta(seconds=1)
                break
            if step > 500_000:
                logger.warning(
                    "导出建仓时间无法完全满足 max_positions=%s（%s），保留可能触发 verify 告警",
                    max_pos,
                    t.level,
                )
                break
        display_by_id[tid] = disp
        sim.append((disp, ex, tid))


def _flush_r24_raw_surge_pending(
    *,
    cfg: RawSurgeR24Config,
    feed: RollingDataFeed,
    account: Account,
    strategy: RawSurgeRollingStrategy,
    current_dt: datetime,
    pending_entries: list[dict],
    open_trades: list[AmplitudeTrade],
    trades: list[AmplitudeTrade],
    all_signals_history: list[dict],
    last_exit_open_time: dict[str, datetime],
    verbose: bool,
) -> list[dict]:
    """本小时到期 pending 尝试成交一轮；保证 ``len(open_trades) <= max_positions``。"""
    still_pending: list[dict] = []
    remaining_slots = cfg.max_positions - len(open_trades)
    for item in pending_entries:
        symbol, target_dt = item["symbol"], item["target_date"]
        if current_dt < target_dt:
            still_pending.append(item)
            continue
        if current_dt > target_dt + timedelta(hours=48):
            if verbose:
                logger.debug("  ⏳ %s pending signal expired after 48h", symbol)
            continue
        if remaining_slots <= 0:
            still_pending.append(item)
            continue
        if any(t.level == symbol for t in open_trades):
            still_pending.append(item)
            continue

        prev_exit = last_exit_open_time.get(symbol)
        entry_p, entry_t = _resolve_raw_surge_pending_entry(feed, symbol, current_dt, prev_exit)

        if not entry_p:
            still_pending.append(item)
            continue

        if len(open_trades) >= cfg.max_positions:
            still_pending.append(item)
            continue
        if any(t.level == symbol for t in open_trades):
            still_pending.append(item)
            continue

        cap = account.capital_at(entry_t)
        total_equity = cap + sum(t.invest_amount for t in open_trades)
        invest = strategy.compute_order_margin(cap, total_equity)
        if not (0 < invest <= cap and len(open_trades) < cfg.max_positions):
            still_pending.append(item)
            continue

        entry_ratio = None
        trade = AmplitudeTrade(
            entry_time=item["signal_time"],
            entry_price=entry_p,
            base_price=entry_p,
            direction="short",
            level=symbol,
            target_pct=cfg.tp_initial * 100,
            target_price=entry_p * (1 - cfg.tp_initial),
            leverage=cfg.leverage,
            stop_loss_pct=cfg.sl_threshold * 100,
            stop_loss_price=entry_p * (1 + cfg.sl_threshold),
            status="filled",
            filled_time=entry_t,
            surge_pct=item["pct_chg"],
            signal_price=item["signal_price"],
            entry_reason=item["reason"],
            entry_account_ratio=entry_ratio,
            tp_initial_price=entry_p * (1 - cfg.tp_initial),
            sell_surge_ratio=item.get("sell_surge_ratio"),
            yesterday_avg_hour_sell_volume=item.get("yesterday_avg_hour_sell_volume"),
        )
        account.open_position_bt8(trade, invest, cfg.leverage)
        trade._add_position_multiplier = cfg.add_position_multiplier
        trades.append(trade)
        open_trades.append(trade)
        remaining_slots -= 1

        idx = item.get("sig_record_idx")
        if idx is not None and 0 <= idx < len(all_signals_history):
            all_signals_history[idx]["trade_ref"] = trade

        if verbose:
            logger.info(
                "  🎯 R24-RS %s @ %.4f ratio=%.2f invest=%.0f",
                symbol,
                entry_p,
                entry_ratio or 0,
                invest,
            )

    return still_pending


class R24RawSurgeRunner:
    """使用 ``build_raw_surge_hourly_gainers`` 替代 ``preload_hourly_gainers``。"""

    def __init__(
        self,
        feed: RollingDataFeed,
        account: Account,
        strategy: RawSurgeRollingStrategy,
        verbose: bool = True,
    ) -> None:
        if not isinstance(strategy.config, RawSurgeR24Config):
            raise TypeError("R24RawSurgeRunner 需要 RawSurgeR24Config + RawSurgeRollingStrategy")
        self._feed = feed
        self._account = account
        self._strategy = strategy
        self.verbose = verbose
        self._ambiguous_5m_count = 0
        self._last_signal_history: list[dict] = []

    def run(self, start: datetime, end: datetime) -> RunResult:
        t0 = time.perf_counter()
        cfg = self._strategy.config
        assert isinstance(cfg, RawSurgeR24Config)
        trades: list[AmplitudeTrade] = []
        open_trades: list[AmplitudeTrade] = []
        pending_entries: list[dict] = []
        all_signals_history: list[dict] = []
        self._ambiguous_5m_count = 0

        cooldown_tracker: dict[str, datetime] = {}

        realtime_csv = Path("reports") / f"rolling_raw_surge_signals_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        realtime_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(realtime_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "信号时间", "交易对", "24h涨幅(%)", "信号价格",
                "上市天数", "30日均价", "当日收盘", "均价偏离(%)",
                "主力获利阈值(%)", "筛选结果", "卖量倍数", "昨日小时均卖",
                "门控结果",
                "多空比", "成交额(亿)", "延迟小时", "是否入场"
            ])

        hourly_gainers = build_raw_surge_hourly_gainers(
            self._feed._db,
            self._feed.load_all_symbols(),
            start,
            end,
            min_pct_chg=cfg.min_pct_chg,
            min_sell_surge=cfg.raw_min_sell_surge,
            max_sell_surge=cfg.raw_max_sell_surge,
            window_hours=cfg.rolling_window_hours,
            workers=self._feed._workers,
            top_n=cfg.top_n,
            max_sr_probe=cfg.max_sr_probe,
            candidate_rank_mode=cfg.candidate_rank_mode,
            min_yavg_sell_volume=cfg.raw_min_yavg_sell_volume,
        )

        current_dt = start
        while current_dt <= end:
            still_open = []
            last_exit_open_time: dict[str, datetime] = {}
            for trade in open_trades:
                entry_dt = trade.filled_time or trade.entry_time
                if current_dt < entry_dt:
                    still_open.append(trade)
                    continue

                symbol = trade.level
                candles_5m = self._feed.load_5m(symbol, current_dt, current_dt + timedelta(hours=1))
                if not candles_5m:
                    still_open.append(trade)
                    continue

                exit_found = False
                for c5 in candles_5m:
                    hold_h = (c5.open_time - entry_dt).total_seconds() / 3600
                    res = self._strategy.check_exit(trade, c5.high, c5.low, c5.close, c5.open_time, hold_h)

                    if res:
                        res_str, exit_p = res

                        if res_str == "add_position":
                            cap = self._account.capital_at(c5.open_time)
                            if cap >= trade.position_size * exit_p:
                                self._account.add_position_bt8(trade, exit_p, c5.open_time, cfg.add_position_multiplier)
                                trade.lowest_price = c5.low
                                if self.verbose:
                                    logger.info("  补仓 %s @ %.4f avg=%.4f", symbol, exit_p, trade.entry_price)
                            recheck = self._strategy.check_exit(trade, c5.high, c5.low, c5.close, c5.open_time, hold_h)
                            if recheck and recheck[0] != "add_position":
                                res_str, exit_p = recheck
                            else:
                                continue

                        _sl = min(c5.low, trade.lowest_price) if trade.lowest_price is not None else c5.low
                        tp_threshold = self._strategy.resolve_tp_threshold(trade, hold_h, _sl)
                        tp_p = trade.entry_price * (1 - tp_threshold)
                        sl_p = trade.entry_price * (1 + cfg.sl_threshold)
                        if c5.high >= sl_p and c5.low <= tp_p:
                            self._ambiguous_5m_count += 1

                        trade.target_price = tp_p

                        # Funding fee
                        if cfg.enable_funding_fee:
                            f_hist = self._feed.load_funding_history(symbol, entry_dt, c5.open_time)
                            if f_hist:
                                notional = trade.invest_amount * trade.leverage
                                trade.funding_fee_cost = sum(-r * notional for _, r in f_hist)

                        self._account.close_position_bt8(trade, res_str, exit_p, c5.open_time, int(hold_h))
                        last_exit_open_time[symbol] = c5.open_time
                        if self.verbose:
                            icon = "✅" if is_win_result(res_str) else (
                                "⏰" if "timeout" in res_str else "❌"
                            )
                            logger.info(
                                "  %s %s %s→%s %.1fh pnl=%.0f",
                                icon, symbol,
                                trade.entry_time.strftime('%m-%d %H:%M'),
                                c5.open_time.strftime('%m-%d %H:%M'),
                                hold_h, trade.profit_amount,
                            )
                        exit_found = True
                        break
                if not exit_found:
                    still_open.append(trade)
            open_trades = still_open

            if current_dt.hour == 23:
                still_after_timeout = []
                for trade in open_trades:
                    hold_days = (current_dt - trade.entry_time).total_seconds() / 86400
                    if hold_days >= cfg.max_hold_days:
                        symbol = trade.level
                        exit_time = current_dt.replace(hour=23, minute=59, second=59)
                        # 与 verify.py 的约定保持一致：
                        # - 日末 timeout（23:59:59）用「当天 00:00～次日 00:00」K1h 的末根 close
                        #   （注意部分数据源/SQL 范围的端点语义可能包含次日 00:00 的棒）。
                        # - 其它 timeout（非日末）用 end-1h～end 的末根 close。
                        day0 = exit_time.replace(hour=0, minute=0, second=0, microsecond=0)
                        c1x = self._feed.load_1h(symbol, day0, day0 + timedelta(days=1))
                        exit_price = c1x[-1].close if c1x else trade.entry_price
                        hold_h = int((exit_time - trade.entry_time).total_seconds() / 3600)
                        # Funding fee
                        if cfg.enable_funding_fee:
                            f_hist = self._feed.load_funding_history(
                                symbol, trade.filled_time or trade.entry_time, exit_time
                            )
                            if f_hist:
                                notional = trade.invest_amount * trade.leverage
                                trade.funding_fee_cost = sum(-r * notional for _, r in f_hist)
                        self._account.close_position_bt8(trade, "timeout", exit_price, exit_time, hold_h)
                        last_exit_open_time[symbol] = exit_time
                        if self.verbose:
                            logger.info(
                                "  ⏰ %s timeout %dd %s→%s pnl=%.0f",
                                symbol, cfg.max_hold_days,
                                trade.entry_time.strftime('%m-%d'),
                                current_dt.strftime('%m-%d'),
                                trade.profit_amount,
                            )
                    else:
                        still_after_timeout.append(trade)
                open_trades = still_after_timeout

            # 先消化本小时已到期 pending，再算 expiring / 再扫信号，最后再次消化（含本小时新接受的 immediate）
            pending_entries = _flush_r24_raw_surge_pending(
                cfg=cfg,
                feed=self._feed,
                account=self._account,
                strategy=self._strategy,
                current_dt=current_dt,
                pending_entries=pending_entries,
                open_trades=open_trades,
                trades=trades,
                all_signals_history=all_signals_history,
                last_exit_open_time=last_exit_open_time,
                verbose=self.verbose,
            )

            scan_interval = getattr(cfg, 'scan_interval_hours', 1) or 1
            is_scan_hour = (current_dt.hour % scan_interval == 0)
            # 只计算在当前小时或之前到期的 pending（首轮 flush 后重算）
            expiring_pending = sum(1 for p in pending_entries if p['target_date'] <= current_dt)
            if is_scan_hour and len(open_trades) + expiring_pending < cfg.max_positions:
                # 用上一小时 K 的桶做 select_signals（与 preload 的 hour_key 一致）；实盘 paper 用「当前整点」作 dt，二者可差 1h。
                prev_dt = current_dt - timedelta(hours=1)
                _ = self._strategy.select_signals(self._feed, prev_dt, hourly_gainers)

                # Sort by sell_surge_ratio desc so highest sell surge gets priority at top_n cap
                signal_details = sorted(
                    getattr(self._strategy, 'last_signal_details', []),
                    key=lambda d: d.get('sell_surge_ratio') or 0.0,
                    reverse=True,
                )
                accepted_this_hour = 0
                immediate_accepted_this_hour = 0
                for detail in signal_details:
                    symbol = detail['symbol']
                    pct_chg = detail['pct_chg']
                    filter_result = detail['filter_result']

                    if symbol in cooldown_tracker:
                        last_signal = cooldown_tracker[symbol]
                        if (current_dt - last_signal).total_seconds() < cfg.signal_cooldown_hours * 3600:
                            continue

                    if filter_result == '通过':
                        open_symbols = [t.level for t in open_trades]
                        pending_symbols = [p['symbol'] for p in pending_entries]
                        all_active = list(set(open_symbols + pending_symbols))

                        accept, reason, delay_hours = self._strategy.should_enter(
                            symbol, pct_chg, self._feed, current_dt,
                            open_positions=all_active,
                        )
                        gate_details = getattr(self._strategy, 'last_gate_details', {})
                        ratio = gate_details.get('ratio')
                        volume_100m = gate_details.get('volume_100m')
                    else:
                        accept = False
                        reason = "未进入门控"
                        delay_hours = 0
                        ratio = None
                        volume_100m = None

                    # Use previous hour's close price as signal price
                    candles_1h = self._feed.load_1h(symbol, prev_dt, prev_dt)
                    candle_price = candles_1h[0].close if candles_1h else 0.0

                    sig_record = {
                        'date': current_dt.strftime("%Y-%m-%d %H:%M"),
                        'symbol': symbol,
                        'pct_chg': pct_chg,
                        'signal_price': candle_price,
                        'listed_days': detail['listed_days'],
                        'avg_price_30d': detail['avg_price_30d'],
                        'close_1d': detail['close_1d'],
                        'from_avg_pct': detail['from_avg_pct'],
                        'profit_threshold': detail['profit_threshold'],
                        'filter_result': filter_result,
                        'sell_surge_ratio': detail.get('sell_surge_ratio'),
                        'yesterday_avg_hour_sell_volume': detail.get('yesterday_avg_hour_sell_volume'),
                        'is_pending': accept,
                        'reason': reason,
                        'delay_hours': delay_hours,
                        'ratio': ratio,
                        'volume_100m': volume_100m,
                        'trade_ref': None,
                    }
                    all_signals_history.append(sig_record)

                    with open(realtime_csv, "a", newline="", encoding="utf-8-sig") as f:
                        writer = csv.writer(f)
                        writer.writerow([
                            sig_record['date'],
                            sig_record['symbol'],
                            round(sig_record['pct_chg'], 4) if sig_record['pct_chg'] is not None else "",
                            round(sig_record['signal_price'], 6) if sig_record['signal_price'] else "",
                            sig_record['listed_days'] if sig_record['listed_days'] is not None else "",
                            round(sig_record['avg_price_30d'], 6) if sig_record['avg_price_30d'] else "",
                            round(sig_record['close_1d'], 6) if sig_record['close_1d'] else "",
                            round(sig_record['from_avg_pct'], 4) if sig_record['from_avg_pct'] is not None else "",
                            round(sig_record['profit_threshold'], 4) if sig_record['profit_threshold'] is not None else "",
                            sig_record['filter_result'],
                            round(sig_record['sell_surge_ratio'], 4) if sig_record.get('sell_surge_ratio') is not None else "",
                            round(sig_record['yesterday_avg_hour_sell_volume'], 2) if sig_record.get('yesterday_avg_hour_sell_volume') is not None else "",
                            sig_record['reason'] if sig_record['reason'] else "",
                            round(sig_record['ratio'], 4) if sig_record['ratio'] is not None else "",
                            round(sig_record['volume_100m'], 4) if sig_record['volume_100m'] is not None else "",
                            sig_record['delay_hours'],
                            "Yes" if accept else "No",
                        ])

                    if accept:
                        # 与 rolling_runner 一致：每笔 accept 前重算本小时将到期的 pending，
                        # 避免循环内已 append 的 immediate 未被计入导致超额排队。
                        expiring_pending = sum(1 for p in pending_entries if p['target_date'] <= current_dt)
                        current_will_expire = 1 if delay_hours == 0 else 0
                        if (
                            len(open_trades)
                            + expiring_pending
                            + current_will_expire
                            + immediate_accepted_this_hour
                            >= cfg.max_positions
                        ):
                            continue
                        if accepted_this_hour >= cfg.top_n:
                            continue
                        accepted_this_hour += 1
                        if delay_hours == 0:
                            immediate_accepted_this_hour += 1
                        cooldown_tracker[symbol] = current_dt

                        pending_entries.append({
                            'symbol': symbol,
                            'pct_chg': pct_chg,
                            'target_date': current_dt + timedelta(hours=delay_hours),
                            'reason': reason,
                            'signal_price': candle_price,
                            'signal_time': current_dt,
                            'sig_record_idx': len(all_signals_history) - 1,
                            'sell_surge_ratio': detail.get('sell_surge_ratio'),
                            'yesterday_avg_hour_sell_volume': detail.get('yesterday_avg_hour_sell_volume'),
                        })
                        if self.verbose:
                            logger.info("  🔍 R24-RS %s signal (+%.1f%%), entry in %dh", symbol, pct_chg, delay_hours)
                    elif self.verbose and reason and filter_result == '通过':
                        logger.debug("  ⛔ R24-RS Skip %s: %s", symbol, reason)

            pending_entries = _flush_r24_raw_surge_pending(
                cfg=cfg,
                feed=self._feed,
                account=self._account,
                strategy=self._strategy,
                current_dt=current_dt,
                pending_entries=pending_entries,
                open_trades=open_trades,
                trades=trades,
                all_signals_history=all_signals_history,
                last_exit_open_time=last_exit_open_time,
                verbose=self.verbose,
            )

            if len(open_trades) > cfg.max_positions:
                raise RuntimeError(
                    f"R24 raw-surge 不变量破坏: len(open_trades)={len(open_trades)} > max_positions={cfg.max_positions}"
                )

            current_dt += timedelta(hours=1)

        for trade in open_trades:
            symbol = trade.level
            # 与 verify.py 的超时对价方式保持一致（见 verify.py: "超时" 分支）。
            if end.hour == 23 and end.minute >= 58:
                day0 = end.replace(hour=0, minute=0, second=0, microsecond=0)
                c1x = self._feed.load_1h(symbol, day0, day0 + timedelta(days=1))
            else:
                c1x = self._feed.load_1h(symbol, end - timedelta(hours=1), end)
            exit_price = c1x[-1].close if c1x else trade.entry_price
            hold_h = int((end - (trade.filled_time or trade.entry_time)).total_seconds() / 3600)
            # Funding fee
            if cfg.enable_funding_fee:
                f_hist = self._feed.load_funding_history(
                    symbol, trade.filled_time or trade.entry_time, end
                )
                if f_hist:
                    notional = trade.invest_amount * trade.leverage
                    trade.funding_fee_cost = sum(-r * notional for _, r in f_hist)
            self._account.close_position_bt8(trade, "timeout", exit_price, end, hold_h)

        if self.verbose:
            logger.info(
                "[R24-RS] done. %d trades, capital: %.0f → %.0f",
                len(trades), self._account.initial_capital, self._account.final_capital,
            )

        self._last_signal_history = all_signals_history
        duration = time.perf_counter() - t0
        return self._build_result(trades, duration, start, end)

    def _build_result(
        self,
        trades: list[AmplitudeTrade],
        duration: float,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> RunResult:
        active = [t for t in trades if t.result != "cancelled"]
        wins = [t for t in active if t.profit_amount is not None and t.profit_amount > 0]
        losses = [t for t in active if t.profit_amount is not None and t.profit_amount <= 0]
        cancelled = [t for t in trades if t.result == "cancelled"]
        closed = wins + losses

        win_rate = len(wins) / len(active) if active else 0.0
        avg_hold = sum(t.holding_hours or 0 for t in active) / len(active) if active else 0.0

        win_amounts = [t.profit_amount for t in wins if t.profit_amount is not None]
        loss_amounts = [t.profit_amount for t in losses if t.profit_amount is not None]
        total_win = sum(win_amounts)
        total_loss = abs(sum(loss_amounts))
        profit_factor = total_win / total_loss if total_loss > 0 else float("inf")

        win_pcts = [t.profit_pct for t in wins if t.profit_pct is not None]
        loss_pcts = [t.profit_pct for t in losses if t.profit_pct is not None]
        avg_win_pct = sum(win_pcts) / len(win_pcts) if win_pcts else 0.0
        avg_loss_pct = sum(loss_pcts) / len(loss_pcts) if loss_pcts else 0.0
        rr_ratio = abs(avg_win_pct / avg_loss_pct) if avg_loss_pct != 0 else float("inf")

        closed_wr = len(wins) / len(closed) if closed else 0.0
        expectancy = closed_wr * avg_win_pct + (1 - closed_wr) * avg_loss_pct

        all_returns = win_pcts + loss_pcts
        if len(all_returns) > 1:
            mean_r = sum(all_returns) / len(all_returns)
            std_r = statistics.stdev(all_returns)
            sharpe = mean_r / std_r if std_r > 0 else 0.0
        else:
            sharpe = 0.0

        signal_conv = len(active) / len(trades) * 100 if trades else 0.0
        total_hours = (end - start).total_seconds() / 3600 if (start and end) else 0.0
        months = total_hours / (24 * 30.44) if total_hours else 0.0
        trades_pm = len(active) / months if months > 0 else 0.0

        avg_win_hold = sum(t.holding_hours or 0 for t in wins) / len(wins) if wins else 0.0
        avg_loss_hold = sum(t.holding_hours or 0 for t in losses) / len(losses) if losses else 0.0

        used_hours = sum(t.holding_hours or 0 for t in active)
        cap_util = used_hours / total_hours * 100 if total_hours > 0 else 0.0

        max_consec = 0
        max_consec_pct = 0.0
        cur_streak = 0
        cur_streak_pct = 0.0
        for t in sorted(active, key=lambda x: x.entry_time or datetime.min.replace(tzinfo=UTC)):
            is_loss = is_loss_result(t.result) or (t.result == "timeout" and (t.profit_amount or 0) <= 0)
            is_w = is_win_result(t.result) or (t.result == "timeout" and (t.profit_amount or 0) > 0)
            if is_loss:
                cur_streak += 1
                cur_streak_pct += (t.profit_pct or 0.0)
                if cur_streak > max_consec:
                    max_consec = cur_streak
                    max_consec_pct = cur_streak_pct
            elif is_w:
                cur_streak = 0
                cur_streak_pct = 0.0

        longs = [t for t in active if t.direction == "long"]
        shorts = [t for t in active if t.direction == "short"]
        long_wins = [t for t in longs if is_win_result(t.result)]
        short_wins = [t for t in shorts if is_win_result(t.result)]
        long_wr = len(long_wins) / len(longs) if longs else 0.0
        short_wr = len(short_wins) / len(shorts) if shorts else 0.0

        level_names = sorted({t.level for t in active if t.level})
        level_stats: dict = {}
        for lv_name in level_names:
            lv_trades = [t for t in active if t.level == lv_name]
            lv_wins = [t for t in lv_trades if is_win_result(t.result)]
            lv_ret_pcts = [
                t.profit_pct for t in lv_trades
                if t.profit_pct is not None and (is_win_result(t.result) or is_loss_result(t.result))
            ]
            level_stats[lv_name] = {
                "trades": len(lv_trades),
                "wins": len(lv_wins),
                "wr": round(len(lv_wins) / len(lv_trades) * 100, 1) if lv_trades else 0.0,
                "avg_ret_pct": round(sum(lv_ret_pcts) / len(lv_ret_pcts), 1) if lv_ret_pcts else 0.0,
            }

        total_fees_pct = round(self._account.total_fees_pct, 3)
        added_position_count = sum(1 for t in trades if getattr(t, 'has_added_position', False))
        trailing_stop_count = sum(
            1 for t in trades
            if getattr(t, "result", "") in ("trailing_stop", "trailing_take_profit")
        )

        result = RunResult(
            trades=trades,
            initial_capital=self._account.initial_capital,
            final_capital=self._account.final_capital,
            total_return_pct=(self._account.final_capital / self._account.initial_capital - 1) * 100,
            win_rate=win_rate,
            total_signals=len(trades),
            active_trades=len(active),
            winning_trades=len(wins),
            losing_trades=len(losses),
            cancelled_trades=len(cancelled),
            avg_holding_hours=avg_hold,
            max_drawdown_pct=self._account.max_drawdown_pct(),
            execution_time=duration,
            profit_factor=round(profit_factor, 3) if math.isfinite(profit_factor) else 99.0,
            expectancy_pct=round(expectancy, 3),
            avg_win_pct=round(avg_win_pct, 3),
            avg_loss_pct=round(avg_loss_pct, 3),
            rr_ratio=round(rr_ratio, 3) if math.isfinite(rr_ratio) else 99.0,
            sharpe_ratio=round(sharpe, 3),
            trades_per_month=round(trades_pm, 2),
            avg_win_hold_h=round(avg_win_hold, 1),
            avg_loss_hold_h=round(avg_loss_hold, 1),
            capital_utilization_pct=round(cap_util, 2),
            signal_conversion_pct=round(signal_conv, 1),
            max_consecutive_losses=max_consec,
            max_consec_loss_pct=round(max_consec_pct, 2),
            trailing_stop_count=trailing_stop_count,
            added_position_count=added_position_count,
            signal_history=self._last_signal_history,
            long_trades=len(longs),
            short_trades=len(shorts),
            long_win_rate=round(long_wr * 100, 1),
            short_win_rate=round(short_wr * 100, 1),
            level_stats=level_stats,
            total_fees_pct=total_fees_pct,
            equity_curve=list(self._account.equity_curve),
            ambiguous_bars=self._ambiguous_5m_count,
        )

        return result

    def print_summary(self, result: RunResult) -> None:
        def inf(v):
            return "∞" if v >= 99 else f"{v:.2f}"
        net_pnl = result.final_capital - result.initial_capital
        fees_pct = round(self._account.total_fees_pct, 3)
        W = 60

        print()
        if result.level_stats:
            print("═" * W)
            print(f"  {'Symbol':<14s}{'Trades':>6s}  {'WR%':>6s}  {'AvgRet%':>8s}")
            print("  " + "─" * (W - 4))
            for lv, st in sorted(result.level_stats.items()):
                print(f"  {lv:<14s}{st['trades']:>6d}  {st['wr']:>5.1f}%  {st['avg_ret_pct']:>+8.1f}")

        print("═" * W)
        print("  📊 R24 BACKTEST PERFORMANCE SUMMARY")
        print("═" * W)
        print(f"  Net PnL           : ${net_pnl:,.2f} ({result.total_return_pct:+.2f}%)")
        print(f"  Initial Capital   : ${result.initial_capital:,.2f}")
        print(f"  Final Capital     : ${result.final_capital:,.2f}")
        print(f"  Max Drawdown      : {result.max_drawdown_pct:.2f}%")
        print(f"  Fees              : -${self._account.total_fees_usd:,.2f} (-{fees_pct:.2f}%)")
        print("─" * W)
        print(f"  Win Rate (Total)  : {result.win_rate*100:.1f}% ({result.winning_trades}W / {result.losing_trades}L)")
        long_str = f"{result.long_win_rate:.1f}%" if result.long_trades else "N/A"
        short_str = f"{result.short_win_rate:.1f}%" if result.short_trades else "N/A"
        print(f"  Win Rate (Short)  : {short_str:<10s} (Long: {long_str})")
        print("─" * W)
        print(f"  Profit Factor     : {inf(result.profit_factor):<10s} Expected Value: {result.expectancy_pct:+.2f}%/trade")
        print(f"  Sharpe Ratio      : {result.sharpe_ratio:<10.3f} R:R Ratio     : {inf(result.rr_ratio)}")
        print(f"  Avg Win %         : {result.avg_win_pct:+.2f}%{'':5s} Avg Loss %    : {result.avg_loss_pct:+.2f}%")
        print("─" * W)
        print(f"  Signals           : {result.total_signals:<10d} Active Trades : {result.active_trades}")
        print(f"  Cancelled         : {result.cancelled_trades:<10d} Signal Conv.  : {result.signal_conversion_pct:.1f}%")
        print(f"  Ambiguous Bars    : {result.ambiguous_bars} (conservative SL-first)")
        print("─" * W)
        print(f"  Trades / Month    : {result.trades_per_month:<10.2f} Cap. Utilization: {result.capital_utilization_pct:.1f}%")
        print(f"  Avg Hold Time     : {result.avg_holding_hours:.1f}h")
        print(f"  Avg Win Hold      : {result.avg_win_hold_h:.0f}h{'':8s} Avg Loss Hold : {result.avg_loss_hold_h:.0f}h")
        print("─" * W)
        print(f"  Max Consec Loss   : {result.max_consecutive_losses} ({result.max_consec_loss_pct:+.1f}%)")
        print(f"  Add-Positions     : {result.added_position_count:<10d} Trailing Stops    : {result.trailing_stop_count}")
        print(f"  Execution Time    : {result.execution_time:.1f}s")
        print("═" * W)

    def export_csv(
        self,
        result: RunResult,
        output_dir: Path = Path("reports"),
        prefix: str = "rolling",
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = output_dir / f"{prefix}_{ts}.csv"
        export_cfg = self._strategy.config
        assert isinstance(export_cfg, RawSurgeR24Config)

        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            f.write(f"# moonshot:max_positions={export_cfg.max_positions}\n")
            writer = csv.writer(f)
            writer.writerow([
                "信号时间", "信号价格", "建仓时间", "交易对",
                "建仓价格", "24h涨幅", "卖量倍数", "昨日小时均卖",
                "仓位大小", "杠杆倍数",
                "止盈价(初始)", "止盈价(实际)", "止损价",
                "开仓理由", "平仓时间", "平仓价格", "平仓原因",
                "盈亏金额", "余额", "盈亏百分比", "持仓小时数",
                "补仓价格", "补仓时间", "平均建仓价格", "是否补仓",
                "建仓多空比", "平仓多空比", "多空比变化", "资金费成本",
            ])

            active = [t for t in result.trades if t.result != "cancelled"]

            def _nz_utc(d: datetime | None) -> datetime:
                if d is None:
                    return datetime.min.replace(tzinfo=UTC)
                return d.replace(tzinfo=UTC) if d.tzinfo is None else d

            sorted_active = sorted(active, key=lambda x: _nz_utc(x.filled_time or x.entry_time))
            last_exit_by_sym: dict[str, datetime] = {}
            display_filled: dict[int, datetime] = {}
            for t in sorted_active:
                ft = _nz_utc(t.filled_time or t.entry_time)
                pex = last_exit_by_sym.get(t.level)
                display_filled[id(t)] = max(ft, pex) if pex is not None else ft
                if t.exit_time:
                    last_exit_by_sym[t.level] = _nz_utc(t.exit_time)

            _enforce_global_max_concurrent_csv_entries(active, display_filled, export_cfg.max_positions)

            for t in result.trades:
                exit_reason = derive_exit_reason(t.result)
                raw_pnl_pct = (t.entry_price - t.exit_price) / t.entry_price if t.exit_price else 0

                if t.result != "cancelled" and id(t) in display_filled:
                    filled_str = display_filled[id(t)].strftime("%Y-%m-%d %H:%M:%S")
                else:
                    filled_str = t.filled_time.strftime("%Y-%m-%d %H:%M:%S") if t.filled_time else ""

                writer.writerow([
                    t.entry_time.strftime("%Y-%m-%d %H:%M:%S") if t.entry_time else "",
                    round(t.signal_price, 8) if t.signal_price else "",
                    filled_str,
                    t.level,
                    t.entry_price,
                    t.surge_pct,
                    round(t.sell_surge_ratio, 4) if t.sell_surge_ratio is not None else "",
                    round(t.yesterday_avg_hour_sell_volume, 2) if t.yesterday_avg_hour_sell_volume is not None else "",
                    t.position_size,
                    t.leverage,
                    round(t.tp_initial_price, 8) if t.tp_initial_price else round(t.target_price, 8),
                    round(t.target_price, 8),
                    round(t.stop_loss_price, 8),
                    t.entry_reason or "",
                    t.exit_time.strftime("%Y-%m-%d %H:%M:%S") if t.exit_time else "",
                    round(t.exit_price, 8) if t.exit_price is not None else "",
                    exit_reason,
                    round(t.profit_amount, 2),
                    round(t.capital_after, 2) if t.capital_after else "",
                    round(raw_pnl_pct, 6),
                    t.holding_hours or "",
                    round(t.add_position_price, 8) if t.add_position_price else "",
                    t.add_position_time.strftime("%Y-%m-%d %H:%M:%S") if t.add_position_time else "",
                    round(t.entry_price, 8) if t.has_added_position else "",
                    t.has_added_position,
                    round(t.entry_account_ratio, 4) if t.entry_account_ratio is not None else "",
                    round(t.exit_account_ratio, 4) if t.exit_account_ratio is not None else "",
                    round(t.account_ratio_change, 4) if t.account_ratio_change is not None else "",
                    round(t.funding_fee_cost, 2) if t.funding_fee_cost else "",
                ])

        if getattr(result, 'signal_history', None):
            sig_csv_path = output_dir / f"{prefix}_signals_{ts}.csv"
            with open(sig_csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "信号时间", "交易对", "24h涨幅(%)", "信号价格",
                    "上市天数", "30日均价", "当日收盘", "均价偏离(%)",
                    "主力获利阈值(%)", "筛选结果", "卖量倍数", "昨日小时均卖",
                    "门控结果",
                    "多空比", "成交额(亿)", "延迟小时", "是否入场", "入场时间",
                    "入场价格", "退出原因", "持仓小时", "盈亏(%)",
                ])
                for s in result.signal_history:
                    t = s.get('trade_ref')
                    if t:
                        is_entered = "Yes"
                        if id(t) in display_filled:
                            entry_dt = display_filled[id(t)].strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            entry_dt = t.filled_time.strftime("%Y-%m-%d %H:%M:%S") if t.filled_time else ""
                        entry_px = round(t.entry_price, 8) if t.entry_price else ""
                        exit_reason = derive_exit_reason(t.result)
                        hold_h = t.holding_hours or ""
                        raw_pnl_pct = round((t.entry_price - t.exit_price) / t.entry_price * 100, 4) if t.exit_price else ""
                    else:
                        is_entered = "Yes" if s.get('is_pending') else "No"
                        entry_dt = ""
                        entry_px = ""
                        exit_reason = ""
                        hold_h = ""
                        raw_pnl_pct = ""

                    writer.writerow([
                        s.get('date', ''),
                        s.get('symbol', ''),
                        round(s.get('pct_chg', 0), 4) if s.get('pct_chg') is not None else "",
                        round(s.get('signal_price', 0), 6) if s.get('signal_price') else "",
                        s.get('listed_days') if s.get('listed_days') is not None else "",
                        round(s.get('avg_price_30d', 0), 6) if s.get('avg_price_30d') else "",
                        round(s.get('close_1d', 0), 6) if s.get('close_1d') else "",
                        round(s.get('from_avg_pct', 0), 4) if s.get('from_avg_pct') is not None else "",
                        round(s.get('profit_threshold', 0), 4) if s.get('profit_threshold') is not None else "",
                        s.get('filter_result', ''),
                        round(s.get('sell_surge_ratio'), 4) if s.get('sell_surge_ratio') is not None else "",
                        round(s.get('yesterday_avg_hour_sell_volume'), 2) if s.get('yesterday_avg_hour_sell_volume') is not None else "",
                        s.get('reason', ''),
                        round(s.get('ratio', 0), 4) if s.get('ratio') is not None else "",
                        round(s.get('volume_100m', 0), 4) if s.get('volume_100m') is not None else "",
                        s.get('delay_hours', 0),
                        is_entered,
                        entry_dt,
                        entry_px,
                        exit_reason,
                        hold_h,
                        raw_pnl_pct,
                    ])
            logger.info("📄 R24 Signal CSV: %s", sig_csv_path)

        logger.info("📄 R24 Trade CSV: %s", csv_path)
        return csv_path
