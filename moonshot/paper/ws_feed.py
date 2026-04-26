"""WebSocket Price Feed — real-time mark prices via Binance Futures WebSocket.

Subscribes to !markPrice@arr@1s for all symbols.
Maintains a price cache with timestamps.
Falls back to REST API if WebSocket price is stale (>30s).
Tries multiple WS endpoints for connectivity.
Supports proxy via HTTPS_PROXY / HTTP_PROXY env vars.
"""

import asyncio
import json
import logging
import os
import time

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# Try multiple endpoints (some may work better in certain regions)
WS_URLS = [
    "wss://fstream.binance.com/ws/!markPrice@arr@1s",
    "wss://fstream.binancefuture.com/ws/!markPrice@arr@1s",  # alternative domain
]
STALE_THRESHOLD = 30  # seconds — fall back to REST if older


def _detect_proxy() -> str | None:
    """Detect proxy from environment variables."""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        proxy = os.environ.get(var)
        if proxy:
            # websockets needs socks5:// or http:// prefix
            logger.info("PriceFeedWS: Using proxy from %s = %s", var, proxy)
            return proxy
    return None


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
        self._proxy = _detect_proxy()

    # ── Public API ────────────────────────────────────────────────

    def get_price(self, symbol: str) -> float | None:
        """Get cached price if fresh (< STALE_THRESHOLD seconds old)."""
        ts = self._timestamps.get(symbol)
        if ts is None:
            if len(self._prices) == 0:
                logger.debug("PriceFeedWS: Cache miss for %s (no prices cached)", symbol)
            else:
                logger.debug("PriceFeedWS: Cache miss for %s (not in cache, have %d symbols)", symbol, len(self._prices))
            return None
        age = time.time() - ts
        if age > STALE_THRESHOLD:
            logger.debug("PriceFeedWS: Stale price for %s (%.1fs old)", symbol, age)
            return None
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
        logger.info("PriceFeedWS: Starting...%s", f" (proxy: {self._proxy})" if self._proxy else "")
        while self._running:
            url = WS_URLS[self._url_index % len(WS_URLS)]
            try:
                await self._connect(url)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                err_type = type(e).__name__
                # keepalive 超时等断线属常态，用 INFO 避免与真正异常抢日志级别
                if isinstance(e, ConnectionClosed):
                    logger.info(
                        "PriceFeedWS: %s (url=%s), reconnect in %.0fs...",
                        err_type, url.split("/ws/")[0], self._reconnect_delay
                    )
                else:
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
        # 10s ping_timeout 易在弱网/休眠后触发 "keepalive ping timeout" 导致 1011 断线
        connect_kwargs = dict(
            ping_interval=30,
            ping_timeout=30,
            close_timeout=5,
            open_timeout=15,
            additional_headers={"User-Agent": "Mozilla/5.0"},
        )
        # websockets >= 12.0 supports proxy via python-socks
        if self._proxy:
            try:
                from python_socks.async_.asyncio import Proxy
                proxy = Proxy.from_url(self._proxy)
                sock = await proxy.connect(dest_host=url.split("/")[2], dest_port=443)
                connect_kwargs["sock"] = sock
                logger.info("PriceFeedWS: Proxy tunnel established")
            except ImportError:
                logger.warning("PriceFeedWS: python-socks not installed, proxy won't work for WS. "
                             "Install with: uv add python-socks[asyncio]")
            except Exception as e:
                logger.warning("PriceFeedWS: Proxy connection failed: %s", e)

        try:
            async with websockets.connect(url, **connect_kwargs) as ws:
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

                        # Debug: log raw data structure on first message
                        if len(self._prices) == 0:
                            logger.debug("PriceFeedWS: First raw message type=%s, sample=%s", type(data).__name__, str(raw)[:200])

                        # Handle array format (markPrice@arr returns list)
                        items = data if isinstance(data, list) else [data]

                        for item in items:
                            symbol = item.get("s")
                            price_str = item.get("p")  # mark price
                            if symbol and price_str:
                                self._prices[symbol] = float(price_str)
                                self._timestamps[symbol] = now
                                count += 1

                        # Log first successful batch only
                        if count > 0 and len(self._prices) == count:
                            logger.info("PriceFeedWS: Cache ready — %d symbols", len(self._prices))
                    except Exception as e:
                        logger.warning("PriceFeedWS: Parse error: %s (raw=%s...)", e, str(raw)[:100])
        except ConnectionClosed as e:
            # 含 keepalive 超时、对端 1011 等；外层会打一条重连 WARNING，这里不再用 ERROR
            if self._running:
                logger.debug("PriceFeedWS: ConnectionClosed: %s", e)
            raise
        except Exception as e:
            logger.error("PriceFeedWS: Connection failed with %s: %s", type(e).__name__, e)
            raise
