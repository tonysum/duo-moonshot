"""LiveFeed — Binance REST API data feed for paper trading.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from moonshot.client import BinanceFuturesClient
from moonshot.models import Candle
from moonshot.paper.ws_feed import PriceFeedWS

logger = logging.getLogger(__name__)

class LiveFeed:
    """Async Binance API data feed for Moonshot paper trading."""

    def __init__(self, client: BinanceFuturesClient, ws_feed: PriceFeedWS | None = None):
        self._client = client
        self._ws_feed = ws_feed
        self._usdt_symbols: Optional[list[str]] = None

    async def get_usdt_symbols(self) -> list[str]:
        """Get all tradeable USDT-margined perpetual symbols."""
        if self._usdt_symbols is not None:
            return self._usdt_symbols

        info = await self._client.get_exchange_info()
        self._usdt_symbols = [
            s.symbol for s in info.symbols
            if s.quote_asset == "USDT"
            and s.contract_type == "PERPETUAL"
            and s.status == "TRADING"
        ]
        logger.info("LiveFeed: %d USDT perpetual symbols", len(self._usdt_symbols))
        return self._usdt_symbols

    async def scan_daily_top_gainers(
        self,
        min_pct_chg: float = 10.0,
        top_n: int = 1,
    ) -> list[tuple[str, float]]:
        """Scan all symbols for yesterday's top N daily gainers."""
        symbols = await self.get_usdt_symbols()
        semaphore = asyncio.Semaphore(20)
        results: list[tuple[str, float]] = []

        async def fetch_one(symbol: str):
            async with semaphore:
                try:
                    klines = await self._client.get_klines(
                        symbol=symbol, interval="1d", limit=2,
                    )
                    if len(klines) < 2:
                        return
                    prev_close = float(klines[-2].close)
                    today_close = float(klines[-1].close)
                    if prev_close > 0:
                        pct_chg = (today_close - prev_close) / prev_close * 100
                        results.append((symbol, pct_chg))
                except Exception:
                    pass

        await asyncio.gather(*[fetch_one(s) for s in symbols])

        if not results:
            return []

        results.sort(key=lambda x: x[1], reverse=True)
        return [(s, p) for s, p in results[:top_n] if p >= min_pct_chg]

    async def load_30d_avg_price(self, symbol: str) -> Optional[float]:
        try:
            klines = await self._client.get_klines(
                symbol=symbol, interval="1d", limit=31,
            )
            if len(klines) < 2:
                return None
            closes = [float(k.close) for k in klines[:-1]]
            return sum(closes) / len(closes) if closes else None
        except Exception as e:
            logger.warning("Failed to load 30d avg for %s: %s", symbol, e)
            return None

    _listing_date_cache: dict[str, Optional[datetime]] = {}

    async def load_listing_date(self, symbol: str) -> Optional[datetime]:
        """Get listing date by querying the earliest 1d kline."""
        if symbol in self._listing_date_cache:
            return self._listing_date_cache[symbol]
        try:
            klines = await self._client.get_klines(
                symbol=symbol, interval="1d", limit=1, start_time=0,
            )
            if klines:
                listing = datetime.fromtimestamp(klines[0].open_time / 1000, tz=timezone.utc)
                self._listing_date_cache[symbol] = listing
                return listing
        except Exception as e:
            logger.warning("Failed to load listing date for %s: %s", symbol, e)
        self._listing_date_cache[symbol] = None
        return None

    async def load_1d_open(self, symbol: str, date: datetime) -> Optional[float]:
        try:
            day_start_ms = int(date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc).timestamp() * 1000)
            klines = await self._client.get_klines(
                symbol=symbol, interval="1d",
                start_time=day_start_ms,
                end_time=day_start_ms + 86_400_000,
                limit=1,
            )
            return float(klines[0].open) if klines else None
        except Exception:
            return None

    async def load_1d_close(self, symbol: str, date: datetime) -> Optional[float]:
        try:
            day_start_ms = int(date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc).timestamp() * 1000)
            klines = await self._client.get_klines(
                symbol=symbol, interval="1d",
                start_time=day_start_ms,
                end_time=day_start_ms + 86_400_000,
                limit=1,
            )
            return float(klines[0].close) if klines else None
        except Exception:
            return None

    async def load_24h_volume(self, symbol: str, dt: datetime) -> float:
        try:
            day_start_ms = int(dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc).timestamp() * 1000)
            klines = await self._client.get_klines(
                symbol=symbol, interval="1d",
                start_time=day_start_ms,
                end_time=day_start_ms + 86_400_000,
                limit=1,
            )
            return float(klines[0].quote_asset_volume) if klines else -1.0
        except Exception:
            return -1.0

    async def load_top_trader_ratio(self, symbol: str, dt: Optional[datetime] = None) -> Optional[float]:
        try:
            ratios = await self._client.get_top_long_short_account_ratio(symbol=symbol, period="1h", limit=1)
            return float(ratios[0].long_short_ratio) if ratios else None
        except Exception:
            return None

    async def load_1h_candle(self, symbol: str) -> Optional[Candle]:
        try:
            klines = await self._client.get_klines(symbol=symbol, interval="1h", limit=2)
            if len(klines) < 2: return None
            k = klines[-2]
            return Candle(
                open_time=datetime.fromtimestamp(k.open_time / 1000, tz=timezone.utc),
                open=float(k.open), high=float(k.high), low=float(k.low), close=float(k.close),
                volume=0.0 # Volume not strictly needed for exit check
            )
        except Exception:
            return None

    async def get_current_price(self, symbol: str) -> Optional[float]:
        # Try WebSocket cache first
        if self._ws_feed:
            ws_price = self._ws_feed.get_price(symbol)
            if ws_price is not None:
                return ws_price
        # Fallback to REST
        try:
            ticker = await self._client.get_ticker_price(symbol)
            return float(ticker.price)
        except Exception:
            return None

    async def load_funding_history(self, symbol: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        try:
            records = await self._client.get_funding_rate(
                symbol=symbol,
                start_time=int(start.timestamp() * 1000),
                end_time=int(end.timestamp() * 1000),
                limit=1000,
            )
            return [(datetime.fromtimestamp(r.funding_time / 1000, tz=timezone.utc), float(r.funding_rate)) for r in records]
        except Exception:
            return []
    async def load_supertrend(self, symbol: str, period: int = 10, multiplier: float = 3.0, timeframe: str = "1h") -> str:
        """Calculate Supertrend (bullish/bearish) using Binance API data."""
        try:
            # Fetch more candles than period to stabilize ATR
            limit = period * 3 + 10
            klines = await self._client.get_klines(symbol=symbol, interval=timeframe, limit=limit)
            if len(klines) < period + 1:
                return "unknown"

            # klines are in order: [oldest, ..., newest]
            # Convert to candles
            candles = [
                Candle(
                    open_time=datetime.fromtimestamp(k.open_time / 1000, tz=timezone.utc),
                    open=float(k.open), high=float(k.high), low=float(k.low), close=float(k.close)
                ) for k in klines
            ]

            trs = []
            hl2s = []
            for i in range(len(candles)):
                c = candles[i]
                hl2s.append((c.high + c.low) / 2)
                if i == 0:
                    trs.append(c.high - c.low)
                else:
                    prev = candles[i-1]
                    trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))

            # ATR calculation (SMA of TR)
            atrs = [0.0] * len(candles)
            for i in range(period - 1, len(candles)):
                atrs[i] = sum(trs[i - period + 1 : i + 1]) / period

            ubounds = [0.0] * len(candles)
            lbounds = [0.0] * len(candles)
            trend = [1] * len(candles) # 1 for bullish, -1 for bearish
            
            start_idx = period
            for i in range(start_idx, len(candles)):
                atr = atrs[i]
                hl2 = hl2s[i]
                curr_ub = hl2 + (multiplier * atr)
                curr_lb = hl2 - (multiplier * atr)
                
                prev_ub = ubounds[i-1]
                prev_lb = lbounds[i-1]
                prev_close = candles[i-1].close
                
                # Basic Supertrend logic
                if i == start_idx:
                    ubounds[i] = curr_ub
                    lbounds[i] = curr_lb
                else:
                    ubounds[i] = curr_ub if curr_ub < prev_ub or prev_close > prev_ub else prev_ub
                    lbounds[i] = curr_lb if curr_lb > prev_lb or prev_close < prev_lb else prev_lb
                
                if candles[i].close > ubounds[i]:
                    trend[i] = 1
                elif candles[i].close < lbounds[i]:
                    trend[i] = -1
                else:
                    trend[i] = trend[i-1]
                    # Refine bounds based on trend
                    if trend[i] == 1 and lbounds[i] < prev_lb:
                        lbounds[i] = prev_lb
                    if trend[i] == -1 and ubounds[i] > prev_ub:
                        ubounds[i] = prev_ub
            
            return "bullish" if trend[-1] == 1 else "bearish"
        except Exception as e:
            logger.error("Failed to calculate Supertrend for %s: %s", symbol, e)
            return "unknown"
