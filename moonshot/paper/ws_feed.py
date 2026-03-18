"""WebSocket Price Feed — real-time mark prices via Binance Futures WebSocket.

Subscribes to !markPrice@arr@1s for all symbols.
Maintains a price cache with timestamps.
Falls back to REST API if WebSocket price is stale (>30s).
Tries multiple WS endpoints for connectivity.
"""

import asyncio
import json
import logging
import time
import traceback
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

# Try multiple endpoints (some may work better in certain regions)
WS_URLS = [
    "wss://fstream.binance.com/ws/!markPrice@arr@1s",
    "wss://fstream.binancefuture.com/ws/!markPrice@arr@1s",  # alternative domain
]
STALE_THRESHOLD = 30  # seconds — fall back to REST if older


class PriceFeedWS:
    """Real-time mark price cache via Binance Futures WebSocket."""

    def __init__(self):
        self._prices: dict[str, float] = {}   # symbol -> price
        self._timestamps: dict[str, float] = {}  # symbol -> epoch
        self._ws = None
        self._running = False
        self._connected = False
        self._reconnect_delay = 1.0
        self._url_index = 0

    # ── Public API ────────────────────────────────────────────────

    def get_price(self, symbol: str) -> Optional[float]:
        """Get cached price if fresh (< STALE_THRESHOLD seconds old)."""
        ts = self._timestamps.get(symbol)
        if ts is None:
            return None
        if time.time() - ts > STALE_THRESHOLD:
            return None  # stale → caller should use REST
        return self._prices.get(symbol)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def symbol_count(self) -> int:
        return len(self._prices)

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self):
        """Start the WebSocket connection loop (runs forever with auto-reconnect)."""
        self._running = True
        logger.info("PriceFeedWS: Starting...")
        while self._running:
            url = WS_URLS[self._url_index % len(WS_URLS)]
            try:
                await self._connect(url)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                err_type = type(e).__name__
                logger.warning(
                    "PriceFeedWS: %s: %s (url=%s), retry in %.0fs...",
                    err_type, e or "connection failed", url, self._reconnect_delay
                )
                # Rotate to next URL after failures
                if self._reconnect_delay >= 8:
                    self._url_index += 1
                    logger.info("PriceFeedWS: Switching to URL #%d", self._url_index % len(WS_URLS))
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
        self._connected = False
        logger.info("PriceFeedWS: Stopped")

    # ── Internal ──────────────────────────────────────────────────

    async def _connect(self, url: str):
        """Connect and consume the mark price stream."""
        logger.info("PriceFeedWS: Connecting to %s ...", url.split("/ws/")[0])
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
            additional_headers={"User-Agent": "Mozilla/5.0"},
        ) as ws:
            self._ws = ws
            self._connected = True
            self._reconnect_delay = 1.0
            logger.info("PriceFeedWS: ✅ Connected! Receiving mark prices...")

            async for raw in ws:
                if not self._running:
                    break
                try:
                    data = json.loads(raw)
                    now = time.time()
                    count = 0
                    for item in data:
                        symbol = item.get("s")
                        price_str = item.get("p")  # mark price
                        if symbol and price_str:
                            self._prices[symbol] = float(price_str)
                            self._timestamps[symbol] = now
                            count += 1
                    # Log first successful batch
                    if count > 0 and len(self._prices) == count:
                        logger.info("PriceFeedWS: First batch — %d symbols", count)
                except Exception as e:
                    logger.debug("PriceFeedWS: Parse error: %s", e)
