"""SupertrendMonitor — Checks pending signals against Supertrend.
"""

import logging
from moonshot.strategy import MoonshotConfig
from moonshot.paper.live_feed import LiveFeed
from moonshot.paper.paper_store import PaperStore, PendingSupertrendSignal
from moonshot.paper.daily_scanner import DailyScanner, LiveFeedAdapter

logger = logging.getLogger(__name__)

class SupertrendMonitor:
    def __init__(self, feed: LiveFeed, store: PaperStore, scanner: DailyScanner, config: MoonshotConfig):
        self._feed = feed
        self._store = store
        self._scanner = scanner
        self._config = config

    async def check_all(self):
        signals = self._store.get_pending_st_signals()
        if not signals: return
        for sig in signals:
            try:
                await self.check_single(sig)
            except Exception as e:
                logger.error("Error monitoring ST for %s: %s", sig.symbol, e)

    async def check_single(self, sig: PendingSupertrendSignal):
        symbol = sig.symbol
        timeframe = getattr(self._config, 'st_timeframe', '1h')
        
        positions = self._store.get_open_positions()
        if len(positions) >= self._config.max_positions: return
        if any(p.symbol == symbol for p in positions):
            self._store.remove_pending_st_signal(symbol)
            return

        trend = await self._feed.load_supertrend(
            symbol=symbol, period=self._config.st_period,
            multiplier=self._config.st_multiplier, timeframe=timeframe
        )

        if trend == "bearish":
            self._store.log_event("SCAN", f"ST ({timeframe}) bearish. Executing.", symbol)
            adapter = LiveFeedAdapter(symbol, self._feed)
            await self._scanner._open_position(
                symbol=symbol, surge_pct=sig.pct_chg,
                reason=sig.entry_reason + f" (ST_{timeframe})", adapter=adapter
            )
            self._store.remove_pending_st_signal(symbol)
