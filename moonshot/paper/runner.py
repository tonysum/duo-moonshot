"""PaperRunner — Orchestrator for Moonshot paper trading system.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Union

from moonshot.client import BinanceFuturesClient
from moonshot.strategy import MoonshotConfig, MoonshotStrategy
from moonshot.rolling_strategy import RollingConfig, RollingStrategy
from moonshot.paper.live_feed import LiveFeed
from moonshot.paper.paper_account import PaperAccount
from moonshot.paper.paper_store import PaperStore
from moonshot.paper.daily_scanner import DailyScanner
from moonshot.paper.rolling_scanner import RollingScanner
from moonshot.paper.position_monitor import PositionMonitor
from moonshot.paper.supertrend_monitor import SupertrendMonitor
from moonshot.paper.ws_feed import PriceFeedWS

logger = logging.getLogger(__name__)


def _next_rolling_scan_utc(now: datetime, interval_hours: int) -> datetime:
    """下一档 R24 扫描时刻（UTC 整点对齐）。

    用「自 epoch 起的小时数」对齐，避免 `replace(hour=…)` 在间隔不能整除 24 时的边角问题。
    注意：`interval_hours >= 24`（常见误配为 24）时等价于「每 UTC 自然日 0 点」一次，
    若期望每小时扫描请保持 `scan_interval_hours=1`。
    """
    n = max(1, int(interval_hours))
    th = int(now.timestamp()) // 3600
    nh = ((th // n) + 1) * n
    return datetime.fromtimestamp(nh * 3600, tz=timezone.utc)


class PaperRunner:
    """Supports MoonshotConfig (daily) or RollingConfig (R24 hourly)."""

    def __init__(
        self,
        config: Union[MoonshotConfig, RollingConfig],
        api_key: str = "",
        api_secret: str = "",
    ):
        self.config = config
        self.client = BinanceFuturesClient(api_key=api_key, secret_key=api_secret)
        db_path = "paper_trading_rolling.db" if isinstance(config, RollingConfig) else "paper_trading.db"
        self.store = PaperStore(db_path)
        self.ws_feed = PriceFeedWS()
        self.feed = LiveFeed(self.client, ws_feed=self.ws_feed)
        self.account = PaperAccount(self.store)

        is_rolling = isinstance(config, RollingConfig)
        if is_rolling:
            self.scanner = RollingScanner(self.feed, self.store, self.account, config)
            self._strategy = RollingStrategy(config)
        else:
            self.scanner = DailyScanner(self.feed, self.store, self.account, config)
            self._strategy = MoonshotStrategy(config)

        self.monitor = PositionMonitor(self.feed, self.store, self.account, self._strategy)
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
        is_rolling = isinstance(self.config, RollingConfig)
        interval_hours = getattr(self.config, "scan_interval_hours", 1) if is_rolling else 24

        # R24: initial scan 60s after start
        if is_rolling:
            ih = max(1, int(interval_hours))
            logger.info("PaperRunner R24: scan_interval_hours=%s (UTC grid every %dh)", interval_hours, ih)
            await asyncio.sleep(60)
            if self._running:
                await self.scanner.scan()

        while self._running:
            try:
                now = datetime.now(timezone.utc)
                if is_rolling:
                    target = _next_rolling_scan_utc(now, interval_hours)
                    wait_secs = max(60, (target - now).total_seconds())
                    logger.info(
                        "R24 ScannerLoop: next at %s UTC (in %.1fh, interval=%dh)",
                        target.strftime("%Y-%m-%d %H:%M"),
                        wait_secs / 3600,
                        max(1, int(interval_hours)),
                    )
                else:
                    # Daily: run at 00:05 UTC
                    target = now.replace(hour=0, minute=5, second=0, microsecond=0)
                    if now >= target:
                        target += timedelta(days=1)
                    wait_secs = (target - now).total_seconds()
                    logger.info("ScannerLoop: Waiting %.1f hours until daily scan", wait_secs / 3600)

                await asyncio.sleep(wait_secs)

                if not self._running:
                    break
                await self.scanner.scan()
            except asyncio.CancelledError:
                break
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


class DualPaperRunner:
    """Runs daily and rolling strategies in parallel, each with independent store/account."""

    def __init__(
        self,
        daily_config: MoonshotConfig,
        rolling_config: RollingConfig,
        api_key: str = "",
        api_secret: str = "",
    ):
        self.daily_config = daily_config
        self.rolling_config = rolling_config
        self.client = BinanceFuturesClient(api_key=api_key, secret_key=api_secret)
        self.ws_feed = PriceFeedWS()
        self.feed = LiveFeed(self.client, ws_feed=self.ws_feed)

        # Daily lane
        self.daily_store = PaperStore("paper_trading_daily.db")
        self.daily_account = PaperAccount(self.daily_store, 10000.0)
        self.daily_scanner = DailyScanner(self.feed, self.daily_store, self.daily_account, daily_config)
        self.daily_monitor = PositionMonitor(
            self.feed, self.daily_store, self.daily_account,
            MoonshotStrategy(daily_config),
        )
        self.daily_st_monitor = SupertrendMonitor(
            self.feed, self.daily_store, self.daily_scanner, daily_config,
        )

        # Rolling lane
        self.rolling_store = PaperStore("paper_trading_rolling.db")
        self.rolling_account = PaperAccount(self.rolling_store, 10000.0)
        self.rolling_scanner = RollingScanner(
            self.feed, self.rolling_store, self.rolling_account, rolling_config,
        )
        self.rolling_monitor = PositionMonitor(
            self.feed, self.rolling_store, self.rolling_account,
            RollingStrategy(rolling_config),
        )

        # Compatibility: expose first store/account for single-runner API paths
        self.store = self.daily_store
        self.account = self.daily_account
        self.scanner = self.daily_scanner

        self._running = False
        self._tasks = []

    async def start(self):
        if self._running:
            return
        self._running = True
        ri = max(1, int(self.rolling_config.scan_interval_hours))
        logger.info(
            "DualPaperRunner: Starting daily + rolling (R24 interval=%dh, UTC grid)",
            ri,
        )
        await self.client.__aenter__()

        self._tasks.append(asyncio.create_task(self.ws_feed.start(), name="PriceFeedWS"))
        self._tasks.append(asyncio.create_task(self._dual_monitor_loop(), name="PositionMonitor"))
        self._tasks.append(asyncio.create_task(self._daily_scanner_loop(), name="DailyScanner"))
        self._tasks.append(asyncio.create_task(self._rolling_scanner_loop(), name="RollingScanner"))
        self._tasks.append(asyncio.create_task(self._dual_equity_loop(), name="EquityLoop"))
        if self.daily_config.enable_supertrend_gate:
            self._tasks.append(asyncio.create_task(self._supertrend_loop(), name="SupertrendMonitor"))

    async def stop(self):
        logger.info("DualPaperRunner: Stopping...")
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.__aexit__(None, None, None)

    async def _dual_monitor_loop(self):
        while self._running:
            try:
                await self.daily_monitor.check_all()
                await self.rolling_monitor.check_all()
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("MonitorLoop ERROR: %s", e)
                await asyncio.sleep(10)

    async def _supertrend_loop(self):
        while self._running:
            try:
                await self.daily_st_monitor.check_all()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("SupertrendLoop ERROR: %s", e)
                await asyncio.sleep(60)

    async def _daily_scanner_loop(self):
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                target = now.replace(hour=0, minute=5, second=0, microsecond=0)
                if now >= target:
                    target += timedelta(days=1)
                wait_secs = (target - now).total_seconds()
                logger.info("DailyScannerLoop: waiting %.1fh until scan", wait_secs / 3600)
                await asyncio.sleep(wait_secs)
                if not self._running:
                    break
                await self.daily_scanner.scan()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("DailyScannerLoop ERROR: %s", e)
                await asyncio.sleep(60)

    async def _rolling_scanner_loop(self):
        interval_hours = getattr(self.rolling_config, "scan_interval_hours", 1)
        await asyncio.sleep(60)
        if self._running:
            await self.rolling_scanner.scan()

        while self._running:
            try:
                now = datetime.now(timezone.utc)
                target = _next_rolling_scan_utc(now, interval_hours)
                wait_secs = max(60, (target - now).total_seconds())
                logger.info(
                    "RollingScannerLoop: next at %s UTC (in %.1fh, interval=%dh)",
                    target.strftime("%Y-%m-%d %H:%M"),
                    wait_secs / 3600,
                    max(1, int(interval_hours)),
                )
                await asyncio.sleep(wait_secs)
                if not self._running:
                    break
                await self.rolling_scanner.scan()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("RollingScannerLoop ERROR: %s", e)
                await asyncio.sleep(60)

    async def _dual_equity_loop(self):
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                for store, account in [
                    (self.daily_store, self.daily_account),
                    (self.rolling_store, self.rolling_account),
                ]:
                    cash = float(account.capital)
                    positions = store.get_open_positions()
                    pos_value = 0.0
                    for p in positions:
                        price = await self.feed.get_current_price(p.symbol)
                        if price:
                            pnl = p.invest_amount * (p.entry_price - price) / p.entry_price * p.leverage
                            pos_value += (p.invest_amount + pnl)
                    store.append_equity_snapshot(now.isoformat(), cash + pos_value, cash)
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("EquityLoop ERROR: %s", e)
                await asyncio.sleep(300)
