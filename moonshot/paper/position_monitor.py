"""PositionMonitor — Real-time exit checks for open paper-trade positions.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Union

from moonshot.strategy import MoonshotStrategy, MoonshotConfig
from moonshot.rolling_strategy import RollingStrategy, RollingConfig
from moonshot.paper.live_feed import LiveFeed
from moonshot.paper.paper_account import PaperAccount
from moonshot.paper.paper_store import PaperStore, MoonshotPosition

logger = logging.getLogger(__name__)

class PositionMonitor:
    """Works with MoonshotStrategy or RollingStrategy (both share check_exit interface)."""

    def __init__(
        self,
        feed: LiveFeed,
        store: PaperStore,
        account: PaperAccount,
        strategy: Union[MoonshotStrategy, RollingStrategy],
    ):
        self._feed = feed
        self._store = store
        self._account = account
        self._strategy = strategy
        self._config = strategy.config

    async def check_all(self):
        positions = self._store.get_open_positions()
        if not positions: return
        for pos in positions:
            try:
                await self.check_single(pos)
            except Exception as e:
                logger.error("Error monitoring %s: %s", pos.symbol, e)

    async def check_single(self, pos: MoonshotPosition):
        symbol = pos.symbol
        current_price = await self._feed.get_current_price(symbol)
        if not current_price: return

        entry_dt = datetime.fromisoformat(pos.entry_time)
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        now_dt = datetime.now(timezone.utc)
        hold_hours = (now_dt - entry_dt).total_seconds() / 3600

        pos.current_price = current_price
        if not pos.entry_price or pos.entry_price <= 0:
            return
        actual_pct = (pos.entry_price - current_price) / pos.entry_price
        pos.profit_pct = actual_pct * pos.leverage * 100
        pos.unrealized_pnl = pos.invest_amount * pos.profit_pct / 100

        # Track lowest price for trailing take profit (short: lower = more profit)
        if pos.lowest_price is None or current_price < pos.lowest_price:
            pos.lowest_price = current_price

        if hold_hours >= self._config.max_hold_days * 24:
            await self._account.close_position(pos, current_price, now_dt.isoformat(), "timeout", feed=self._feed)
            return

        result = self._strategy.check_exit(
            pos, candle_high=current_price, candle_low=current_price,
            current_price=current_price, current_time=now_dt, hold_hours=hold_hours
        )

        if result is None and isinstance(self._strategy, MoonshotStrategy):
            thr = self._strategy.resolve_tp_threshold(pos, hold_hours)
            if pos.entry_price and pos.entry_price > 0:
                pos.tp_price = pos.entry_price * (1 - thr)
                pos.target_pct = thr * 100

        if result is None and self._config.enable_dynamic_ratio_sl:
            current_ratio = await self._feed.load_top_trader_ratio(symbol)
            result = self._strategy.check_dynamic_ratio_sl(pos, current_ratio, now_dt, current_price)

        if result is None:
            self._store.save_position(pos)
            return

        exit_result, exit_price = result

        if exit_result == "add_position":
            self._account.add_position(symbol, exit_price, now_dt.isoformat())
            return

        await self._account.close_position(pos, exit_price, now_dt.isoformat(), exit_result, feed=self._feed)
