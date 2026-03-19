"""PaperRunner — Orchestrator for Moonshot paper trading system.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from moonshot.client import BinanceFuturesClient
from moonshot.strategy import MoonshotConfig
from moonshot.paper.live_feed import LiveFeed
from moonshot.paper.paper_account import PaperAccount
from moonshot.paper.paper_store import PaperStore
from moonshot.paper.daily_scanner import DailyScanner
from moonshot.paper.position_monitor import PositionMonitor
from moonshot.paper.supertrend_monitor import SupertrendMonitor
from moonshot.paper.ws_feed import PriceFeedWS

logger = logging.getLogger(__name__)

class PaperRunner:
    def __init__(self, config: MoonshotConfig, api_key: str = "", api_secret: str = ""):
        self.config = config
        self.store = PaperStore()
        self.client = BinanceFuturesClient(api_key=api_key, secret_key=api_secret)
        self.ws_feed = PriceFeedWS()
        self.feed = LiveFeed(self.client, ws_feed=self.ws_feed)
        self.account = PaperAccount(self.store)
        
        self.scanner = DailyScanner(self.feed, self.store, self.account, config)
        self.monitor = PositionMonitor(self.feed, self.store, self.account, config)
        self.st_monitor = SupertrendMonitor(self.feed, self.store, self.scanner, config)
        
        self._running = False
        self._tasks = []

    async def start(self):
        if self._running: return
        self._running = True
        logger.info("PaperRunner: Starting...")
        await self.client.__aenter__()

        self._tasks.append(asyncio.create_task(self.ws_feed.start(), name="PriceFeedWS"))
        self._tasks.append(asyncio.create_task(self._monitor_loop(), name="PositionMonitor"))
        self._tasks.append(asyncio.create_task(self._scanner_loop(), name="DailyScanner"))
        self._tasks.append(asyncio.create_task(self._equity_loop(), name="EquityLoop"))
        if self.config.enable_supertrend_gate:
            self._tasks.append(asyncio.create_task(self._supertrend_loop(), name="SupertrendMonitor"))

    async def stop(self):
        logger.info("PaperRunner: Stopping...")
        self._running = False
        for task in self._tasks: task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.__aexit__(None, None, None)

    async def _monitor_loop(self):
        """Check positions every 3s (WS gives real-time prices)."""
        while self._running:
            try:
                await self.monitor.check_all()
                await asyncio.sleep(3)
            except asyncio.CancelledError: break
            except Exception as e:
                logger.error("MonitorLoop ERROR: %s", e)
                await asyncio.sleep(10)

    async def _supertrend_loop(self):
        while self._running:
            try:
                await self.st_monitor.check_all()
                await asyncio.sleep(60)
            except asyncio.CancelledError: break
            except Exception as e:
                logger.error("SupertrendLoop ERROR: %s", e)
                await asyncio.sleep(60)

    async def _scanner_loop(self):
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                target = now.replace(hour=0, minute=5, second=0, microsecond=0)
                if now >= target: target += timedelta(days=1)
                
                wait_secs = (target - now).total_seconds()
                logger.info("ScannerLoop: Waiting %.1f hours until scan", wait_secs / 3600)
                await asyncio.sleep(wait_secs)
                
                if not self._running: break
                await self.scanner.scan()
            except asyncio.CancelledError: break
            except Exception as e:
                logger.error("ScannerLoop ERROR: %s", e)
                await asyncio.sleep(60)

    async def _equity_loop(self):
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                cash = float(self.account.capital)
                positions = self.store.get_open_positions()
                pos_value = 0.0
                for p in positions:
                    price = await self.feed.get_current_price(p.symbol)
                    if price:
                        pnl = p.invest_amount * (p.entry_price - price) / p.entry_price * p.leverage
                        pos_value += (p.invest_amount + pnl)
                
                self.store.append_equity_snapshot(now.isoformat(), cash + pos_value, cash)
                await asyncio.sleep(300)
            except asyncio.CancelledError: break
            except Exception as e:
                logger.error("EquityLoop ERROR: %s", e)
                await asyncio.sleep(300)
