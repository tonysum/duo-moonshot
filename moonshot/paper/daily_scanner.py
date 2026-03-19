"""DailyScanner — Core signal scanner for Moonshot paper trading.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from moonshot.strategy import MoonshotStrategy, MoonshotConfig
from moonshot.models import Candle
from moonshot.paper.live_feed import LiveFeed
from moonshot.paper.paper_account import PaperAccount
from moonshot.paper.paper_store import PaperStore, MoonshotPosition, PendingSignal, PendingSupertrendSignal

logger = logging.getLogger(__name__)

class LiveFeedAdapter:
    """Synchronous adapter for LiveFeed."""

    def __init__(self, symbol: str, feed: LiveFeed):
        self.symbol = symbol
        self.feed = feed
        self._daily_data: dict[str, dict] = {} 
        self._30d_avg_price: float = 0.0

    async def prefetch(self, signal_date: datetime):
        self._30d_avg_price = await self.feed.load_30d_avg_price(self.symbol) or 0.0
        for i in range(3):
            dt = signal_date + timedelta(days=i)
            dstr = dt.strftime("%Y-%m-%d")
            o = await self.feed.load_1d_open(self.symbol, dt)
            c = await self.feed.load_1d_close(self.symbol, dt)
            v = await self.feed.load_24h_volume(self.symbol, dt)
            r = await self.feed.load_top_trader_ratio(self.symbol, dt)
            self._daily_data[dstr] = {"open": o or 0.0, "close": c or 0.0, "volume": v, "ratio": r or 0.0}

    def _get_day(self, dt: datetime) -> dict:
        return self._daily_data.get(dt.strftime("%Y-%m-%d"), {})

    def load_30d_avg_price(self, symbol: str, dt: Optional[datetime] = None) -> float:
        return self._30d_avg_price

    def load_1d_open(self, symbol: str, dt: datetime, *args) -> float:
        return self._get_day(dt).get("open", 0.0)

    def load_1d_close(self, symbol: str, dt: datetime, *args) -> float:
        return self._get_day(dt).get("close", 0.0)

    def load_24h_volume(self, symbol: str, dt: datetime, *args) -> float:
        return self._get_day(dt).get("volume", -1.0)

    def load_top_trader_ratio(self, symbol: str, dt: datetime, *args) -> float:
        return self._get_day(dt).get("ratio", 0.0)

class DailyScanner:
    def __init__(self, feed: LiveFeed, store: PaperStore, account: PaperAccount, config: MoonshotConfig):
        self._feed = feed
        self._store = store
        self._account = account
        self._config = config
        self._strategy = MoonshotStrategy(config)

    async def scan(self):
        now = datetime.now(timezone.utc)
        self._store.log_event("SCAN", "SYSTEM", "Starting daily scan")
        await self._process_pending_signals(now)

        gainers = await self._feed.scan_daily_top_gainers(min_pct_chg=self._config.min_pct_chg, top_n=self._config.top_n)

        if not gainers:
            symbols = await self._feed.get_usdt_symbols()
            self._store.log_event("SCAN", "SYSTEM",
                f"No targets found (scanned {len(symbols)} symbols, min_chg={self._config.min_pct_chg}%)")
            return

        # Log all gainers found
        gainer_str = ", ".join(f"{s}(+{p:.1f}%)" for s, p in gainers)
        self._store.log_event("SCAN", "SYSTEM", f"Found {len(gainers)} gainer(s): {gainer_str}")

        open_positions = [p.symbol for p in self._store.get_open_positions()]
        yesterday = now - timedelta(days=1)
        skipped = []
        accepted = []

        for symbol, pct_chg in gainers:
            if symbol in open_positions:
                skipped.append((symbol, "already_in_position"))
                self._store.log_event("SCAN", symbol, f"SKIP: already in position (+{pct_chg:.1f}%)")
                continue

            adapter = LiveFeedAdapter(symbol, self._feed)
            await adapter.prefetch(yesterday)

            ok, reason, delay_days = self._strategy.should_enter(symbol, pct_chg, adapter, yesterday, open_positions)

            if ok:
                if delay_days > 0:
                    self._store.save_pending_signal(PendingSignal(symbol=symbol, pct_chg=pct_chg, signal_date_str=yesterday.strftime("%Y-%m-%d"), delay_reason=reason))
                    self._store.log_event("SCAN", symbol, f"DELAY entry (+{pct_chg:.1f}%): {reason}")
                    accepted.append((symbol, f"delayed({reason})"))
                else:
                    if self._config.enable_supertrend_gate:
                        self._store.save_pending_st_signal(PendingSupertrendSignal(symbol=symbol, pct_chg=pct_chg, entry_reason=reason))
                        self._store.log_event("SCAN", symbol, f"ST PENDING (+{pct_chg:.1f}%): waiting for bearish flip")
                        accepted.append((symbol, "st_pending"))
                    else:
                        await self._open_position(symbol, pct_chg, reason, adapter)
                        accepted.append((symbol, "opened"))
            else:
                skipped.append((symbol, reason))
                self._store.log_event("SCAN", symbol, f"SKIP (+{pct_chg:.1f}%): {reason}")

        # Summary log
        summary_parts = []
        if accepted:
            summary_parts.append(f"accepted: {', '.join(f'{s}({r})' for s, r in accepted)}")
        if skipped:
            summary_parts.append(f"filtered: {', '.join(f'{s}({r})' for s, r in skipped)}")
        self._store.log_event("SCAN", "SYSTEM",
            f"Scan complete — {len(accepted)} accepted, {len(skipped)} filtered. {'; '.join(summary_parts)}")

    async def _process_pending_signals(self, now: datetime):
        pending = self._store.get_pending_signals()
        for sig in pending:
            surge_date = datetime.strptime(sig.signal_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if now < surge_date + timedelta(days=2): continue
            
            adapter = LiveFeedAdapter(sig.symbol, self._feed)
            await adapter.prefetch(surge_date)
            open_positions = [p.symbol for p in self._store.get_open_positions()]
            accepted, reason, _ = self._strategy.should_enter(sig.symbol, sig.pct_chg, adapter, surge_date, open_positions)

            if accepted:
                if self._config.enable_supertrend_gate:
                    self._store.save_pending_st_signal(PendingSupertrendSignal(symbol=sig.symbol, pct_chg=sig.pct_chg, entry_reason=reason))
                else:
                    await self._open_position(sig.symbol, sig.pct_chg, reason or "Delayed Entry", adapter)
            self._store.remove_pending_signal(sig.symbol)

    async def _open_position(self, symbol: str, surge_pct: float, reason: str, adapter: LiveFeedAdapter):
        current_price = await self._feed.get_current_price(symbol)
        if not current_price: return

        capital = float(self._account.capital)
        invest = self._strategy.invest_amount(capital)
        tp_price = current_price * (1 - self._config.tp_initial)
        sl_price = current_price * (1 + self._config.sl_threshold)
        entry_ratio = await self._feed.load_top_trader_ratio(symbol)

        pos = MoonshotPosition(
            symbol=symbol, entry_price=current_price, entry_time=datetime.now(timezone.utc).isoformat(),
            invest_amount=float(invest), position_size=float(invest / current_price),
            leverage=self._config.leverage, surge_pct=surge_pct, entry_reason=reason,
            tp_price=tp_price, sl_price=sl_price, target_pct=self._config.tp_initial * 100,
            stop_loss_pct=self._config.sl_threshold * 100, capital_before=capital, entry_account_ratio=entry_ratio
        )
        self._account.open_position(pos)
