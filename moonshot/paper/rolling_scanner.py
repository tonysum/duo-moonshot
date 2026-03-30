"""RollingScanner — R24 hourly scan for Moonshot paper trading.

Uses 24h rolling top gainers (Binance ticker) and RollingStrategy gates.
Runs every scan_interval_hours at :00.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from moonshot.paper.daily_scanner import LiveFeedAdapter
from moonshot.paper.live_feed import LiveFeed
from moonshot.paper.paper_account import PaperAccount
from moonshot.paper.paper_store import MoonshotPosition, PaperStore
from moonshot.rolling_strategy import RollingConfig, RollingStrategy

logger = logging.getLogger(__name__)


class RollingScanner:
    """R24 scanner: hourly 24h-rolling top gainers."""

    def __init__(self, feed: LiveFeed, store: PaperStore, account: PaperAccount, config: RollingConfig):
        self._feed = feed
        self._store = store
        self._account = account
        self._config = config
        self._strategy = RollingStrategy(config)

    def _symbol_in_cooldown(self, symbol: str, now: datetime) -> bool:
        """True if we closed this symbol recently (within signal_cooldown_hours)."""
        trades = self._store.get_trades(limit=200)
        for t in trades:
            if t.get("symbol") != symbol:
                continue
            exit_str = t.get("exit_time")
            if not exit_str:
                continue
            try:
                exit_dt = datetime.fromisoformat(exit_str.replace("Z", "+00:00"))
                if exit_dt.tzinfo is None:
                    exit_dt = exit_dt.replace(tzinfo=UTC)
                if (now - exit_dt).total_seconds() < self._config.signal_cooldown_hours * 3600:
                    return True
            except Exception:
                pass
        return False

    async def scan(self):
        now = datetime.now(UTC)
        self._store.log_event("SCAN", "SYSTEM", "Starting R24 rolling scan")

        gainers = await self._feed.scan_rolling_top_gainers(
            min_pct_chg=self._config.min_pct_chg,
            top_n=self._config.top_n,
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
        self._store.log_event("SCAN", "SYSTEM", f"Found {len(gainers)} gainer(s): {gainer_str}")

        open_positions = [p.symbol for p in self._store.get_open_positions()]
        # prefetch 昨日及之后几日，供 RollingStrategy 内主力检查（profit_dt = signal_dt - 1d）读日K
        prefetch_day = now - timedelta(days=1)
        skipped = []
        accepted = []

        for symbol, pct_chg in gainers:
            if symbol in open_positions:
                skipped.append((symbol, "already_in_position"))
                self._store.log_event("SCAN", symbol, f"SKIP: already in position (+{pct_chg:.1f}%)")
                continue

            if self._symbol_in_cooldown(symbol, now):
                skipped.append((symbol, "cooldown"))
                self._store.log_event("SCAN", symbol, f"SKIP: in cooldown (+{pct_chg:.1f}%)")
                continue

            adapter = LiveFeedAdapter(symbol, self._feed)
            await adapter.prefetch(prefetch_day)

            ok, reason, _ = self._strategy.should_enter(
                symbol, pct_chg, adapter, now, open_positions
            )

            if ok:
                await self._open_position(symbol, pct_chg, reason, adapter)
                accepted.append((symbol, "opened"))
            else:
                skipped.append((symbol, reason))
                self._store.log_event("SCAN", symbol, f"SKIP (+{pct_chg:.1f}%): {reason}")

        summary_parts = []
        if accepted:
            summary_parts.append(f"accepted: {', '.join(f'{s}({r})' for s, r in accepted)}")
        if skipped:
            summary_parts.append(f"filtered: {', '.join(f'{s}({r})' for s, r in skipped)}")
        self._store.log_event(
            "SCAN", "SYSTEM",
            f"R24 scan complete — {len(accepted)} accepted, {len(skipped)} filtered. {'; '.join(summary_parts)}",
        )

        scan_snapshot = {
            "scan_time": now.isoformat(),
            "gainers": [
                {"symbol": s, "pct_chg": round(p, 2), "status": "accepted", "detail": next((r for sym, r in accepted if sym == s), "")}
                if any(sym == s for sym, _ in accepted)
                else {"symbol": s, "pct_chg": round(p, 2), "status": "filtered", "detail": next((r for sym, r in skipped if sym == s), "")}
                for s, p in gainers
            ],
        }
        self._store.set_state("last_scan", json.dumps(scan_snapshot, ensure_ascii=False))

    async def _open_position(self, symbol: str, surge_pct: float, reason: str, adapter: LiveFeedAdapter):
        current_price = await self._feed.get_current_price(symbol)
        if not current_price:
            return

        free = float(self._account.capital)
        locked = sum(p.invest_amount for p in self._store.get_open_positions())
        total_equity = free + locked
        invest = self._strategy.compute_order_margin(free, total_equity)
        if invest <= 0:
            return
        tp_price = current_price * (1 - self._config.tp_initial)
        sl_price = current_price * (1 + self._config.sl_threshold)
        entry_ratio = await self._feed.load_top_trader_ratio(symbol)

        pos = MoonshotPosition(
            symbol=symbol,
            entry_price=current_price,
            entry_time=datetime.now(UTC).isoformat(),
            invest_amount=float(invest),
            position_size=float(invest / current_price),
            leverage=self._config.leverage,
            surge_pct=surge_pct,
            entry_reason=reason,
            tp_price=tp_price,
            sl_price=sl_price,
            target_pct=self._config.tp_initial * 100,
            stop_loss_pct=self._config.sl_threshold * 100,
            capital_before=free,
            entry_account_ratio=entry_ratio,
            highest_price=current_price,
            tp_initial_price=tp_price,
            signal_price=current_price,
        )
        self._account.open_position(pos)
