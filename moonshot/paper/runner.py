"""PaperRunner — Orchestrator for Moonshot paper trading system.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from moonshot.client import BinanceFuturesClient
from moonshot.paper.live_feed import LiveFeed
from moonshot.paper.paper_account import PaperAccount
from moonshot.paper.paper_store import PaperStore
from moonshot.paper.position_monitor import PositionMonitor
from moonshot.paper.rolling_scanner import RawSurgeScanner
from moonshot.paper.ws_feed import PriceFeedWS
from moonshot.r24_raw_surge_config import RawSurgeR24Config
from moonshot.r24_raw_surge_strategy import RawSurgeRollingStrategy

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
    return datetime.fromtimestamp(nh * 3600, tz=UTC)


class PaperRunner:
    """Raw-surge only paper runner."""

    def __init__(
        self,
        config: RawSurgeR24Config,
        api_key: str = "",
        api_secret: str = "",
    ):
        self.config = config
        self.client = BinanceFuturesClient(api_key=api_key, secret_key=api_secret)
        self.store = PaperStore("paper_trading.db")
        self.ws_feed = PriceFeedWS()
        self.feed = LiveFeed(self.client, ws_feed=self.ws_feed)
        self.account = PaperAccount(self.store, config=config)

        self.scanner = RawSurgeScanner(self.feed, self.store, self.account, config)
        self._strategy = RawSurgeRollingStrategy(config)
        self.monitor = PositionMonitor(self.feed, self.store, self.account, self._strategy)

        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._stop_lock = asyncio.Lock()
        self._stopped = False

    async def start(self):
        if self._running:
            return
        self._stopped = False
        self._running = True
        logger.info("PaperRunner: Starting...")
        await self.client.__aenter__()
        # 写入 DB，供 /logs 与「System Events」在首次整点扫描前也能看到服务在跑
        self.store.log_event("RUN", "SYSTEM", "Paper raw-surge 服务已启动")

        self._tasks.append(asyncio.create_task(self.ws_feed.start(), name="PriceFeedWS"))
        self._tasks.append(asyncio.create_task(self._monitor_loop(), name="PositionMonitor"))
        self._tasks.append(asyncio.create_task(self._scanner_loop(), name="RawSurgeScanner"))
        self._tasks.append(asyncio.create_task(self._equity_loop(), name="EquityLoop"))

    async def stop(self):
        async with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            logger.info("PaperRunner: Stopping...")
            self._running = False
            # 先断 WS，避免 async for 长时间不响应 cancel
            try:
                await self.ws_feed.stop()
            except Exception as e:
                logger.warning("PaperRunner: ws_feed.stop failed: %s", e)
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            await self.client.__aexit__(None, None, None)
            self.store.log_event("RUN", "SYSTEM", "Paper raw-surge 服务已停止")

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

    async def _scanner_loop(self):
        interval_hours = getattr(self.config, "scan_interval_hours", 1)
        ih = max(1, int(interval_hours))
        logger.info("PaperRunner raw-surge: scan_interval_hours=%s (UTC grid every %dh)", interval_hours, ih)
        delay_min = int(getattr(self.config, "scan_delay_minutes", 1) or 0)
        delay_min = max(0, delay_min)

        while self._running:
            try:
                now = datetime.now(UTC)
                # 扫描对齐 UTC 整点网格，但延后 delay_min 分钟执行，避免小时边界数据未完整。
                target = _next_rolling_scan_utc(now, interval_hours) + timedelta(minutes=delay_min)
                wait_secs = max(1, (target - now).total_seconds())
                logger.info(
                    "RawSurge ScannerLoop: next at %s UTC (in %.1fh, interval=%dh)",
                    target.strftime("%Y-%m-%d %H:%M"),
                    wait_secs / 3600,
                    max(1, int(interval_hours)),
                )

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
                now = datetime.now(UTC)
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


#
# NOTE: Dual/daily/rolling runners removed — paper is raw-surge only.
