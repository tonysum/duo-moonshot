"""LiveFeed — Binance REST API data feed for paper trading.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from moonshot.client import BinanceFuturesClient
from moonshot.models import Candle
from moonshot.paper.ws_feed import PriceFeedWS

logger = logging.getLogger(__name__)

class LiveFeed:
    """Async Binance API data feed for Moonshot paper trading."""

    def __init__(self, client: BinanceFuturesClient, ws_feed: PriceFeedWS | None = None):
        self._client = client
        self._ws_feed = ws_feed
        self._usdt_symbols: list[str] | None = None

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
        diagnostics: dict[str, Any] | None = None,
    ) -> list[tuple[str, float]]:
        """Scan all symbols for yesterday's top N daily gainers.

        If ``diagnostics`` is a dict, it is cleared and filled with scan metadata for logging
        (best symbol/pct, top preview, counts) when the caller needs to explain empty results.
        """
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
                    # klines[-2] = 昨日已完成的日K, klines[-1] = 今日进行中
                    # 昨日全天涨幅 = (昨日收 - 昨日开) / 昨日开 (原公式误用今日收-昨日收=隔夜涨跌)
                    yesterday = klines[-2]
                    y_open = float(yesterday.open)
                    y_close = float(yesterday.close)
                    if y_open > 0:
                        pct_chg = (y_close - y_open) / y_open * 100
                        results.append((symbol, pct_chg))
                except Exception:
                    pass

        await asyncio.gather(*[fetch_one(s) for s in symbols])

        if not results:
            if diagnostics is not None:
                diagnostics.clear()
                diagnostics["symbols_total"] = len(symbols)
                diagnostics["symbols_with_klines"] = 0
                diagnostics["best_symbol"] = None
                diagnostics["best_pct"] = None
                diagnostics["top_preview"] = []
                diagnostics["count_ge_min"] = 0
            return []

        results.sort(key=lambda x: x[1], reverse=True)

        if diagnostics is not None:
            diagnostics.clear()
            diagnostics["symbols_total"] = len(symbols)
            diagnostics["symbols_with_klines"] = len(results)
            diagnostics["best_symbol"] = results[0][0]
            diagnostics["best_pct"] = float(results[0][1])
            diagnostics["top_preview"] = [(s, round(p, 2)) for s, p in results[:5]]
            diagnostics["count_ge_min"] = sum(1 for _, p in results if p >= min_pct_chg)

        return [(s, p) for s, p in results[:top_n] if p >= min_pct_chg]

    async def scan_rolling_top_gainers(
        self,
        min_pct_chg: float = 10.0,
        top_n: int = 3,
        window_hours: int = 24,
    ) -> list[tuple[str, float]]:
        """Scan for top N rolling gainers.

        window_hours=24: uses Binance 24hr ticker (single batch call, fast).
        window_hours!=24: fetches 1h klines per symbol to compute exact rolling pct
                          (consistent with backtest rolling_window_hours).
        """
        if window_hours == 24:
            tradeable = set(await self.get_usdt_symbols())
            tickers = await self._client.get_24hr_tickers()
            usdt_perps = [
                (t["symbol"], float(t.get("priceChangePercent", 0)))
                for t in tickers
                if t.get("symbol", "") in tradeable
                and float(t.get("priceChangePercent", 0)) >= min_pct_chg
            ]
            usdt_perps.sort(key=lambda x: x[1], reverse=True)
            return usdt_perps[:top_n]

        # Custom window: pre-filter with 24hr ticker (single call), then fetch klines
        # only for top candidates — avoids fetching klines for all ~300 symbols.
        tradeable = set(await self.get_usdt_symbols())
        tickers = await self._client.get_24hr_tickers()
        ticker_pct: dict[str, float] = {
            t["symbol"]: float(t.get("priceChangePercent", 0))
            for t in tickers
            if t.get("symbol", "") in tradeable
        }
        # Take all symbols whose 24hr pct meets the proxy threshold (lower than exact threshold
        # to avoid missing symbols whose rolling pct differs from 24hr pct)
        pre_candidates = sorted(
            [(s, p) for s, p in ticker_pct.items() if p >= min_pct_chg * 0.6],
            key=lambda x: x[1],
            reverse=True,
        )
        symbols_to_check = [s for s, _ in pre_candidates]

        semaphore = asyncio.Semaphore(20)
        results: list[tuple[str, float]] = []

        async def fetch_one(symbol: str):
            async with semaphore:
                try:
                    klines = await self._client.get_klines(
                        symbol=symbol, interval="1h", limit=window_hours + 1,
                    )
                    if len(klines) < window_hours + 1:
                        return
                    base_close = float(klines[0].close)
                    curr_close = float(klines[-1].close)
                    if base_close <= 0:
                        return
                    pct = (curr_close - base_close) / base_close * 100
                    if pct >= min_pct_chg:
                        results.append((symbol, pct))
                except Exception:
                    pass

        await asyncio.gather(*[fetch_one(s) for s in symbols_to_check])
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_n]

    async def load_30d_avg_price(self, symbol: str) -> float | None:
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

    _listing_date_cache: dict[str, datetime | None] = {}

    async def load_listing_date(self, symbol: str) -> datetime | None:
        """Get listing date by querying the earliest 1d kline."""
        if symbol in self._listing_date_cache:
            return self._listing_date_cache[symbol]
        try:
            klines = await self._client.get_klines(
                symbol=symbol, interval="1d", limit=1, start_time=0,
            )
            if klines:
                listing = datetime.fromtimestamp(klines[0].open_time / 1000, tz=UTC)
                self._listing_date_cache[symbol] = listing
                return listing
        except Exception as e:
            logger.warning("Failed to load listing date for %s: %s", symbol, e)
        self._listing_date_cache[symbol] = None
        return None

    async def load_1d_open(self, symbol: str, date: datetime) -> float | None:
        try:
            day_start_ms = int(date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC).timestamp() * 1000)
            klines = await self._client.get_klines(
                symbol=symbol, interval="1d",
                start_time=day_start_ms,
                end_time=day_start_ms + 86_400_000,
                limit=1,
            )
            return float(klines[0].open) if klines else None
        except Exception:
            return None

    async def load_1d_close(self, symbol: str, date: datetime) -> float | None:
        try:
            day_start_ms = int(date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC).timestamp() * 1000)
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
        """24h quote volume ending at dt. Aligns with DataFeed: SUM of 1h candles over [dt-24h, dt)."""
        try:
            end_ms = int(dt.replace(tzinfo=UTC).timestamp() * 1000)
            start_ms = end_ms - 86400_000
            klines = await self._client.get_klines(symbol=symbol, interval="1h", start_time=start_ms, end_time=end_ms, limit=25)
            if not klines:
                return -1.0
            return sum(float(k.quote_asset_volume) for k in klines)
        except Exception:
            return -1.0

    async def load_top_trader_ratio(self, symbol: str, dt: datetime | None = None) -> float | None:
        try:
            ratios = await self._client.get_top_long_short_account_ratio(symbol=symbol, period="1h", limit=1)
            return float(ratios[0].long_short_ratio) if ratios else None
        except Exception:
            return None

    async def load_1h_candle(self, symbol: str) -> Candle | None:
        try:
            klines = await self._client.get_klines(symbol=symbol, interval="1h", limit=2)
            if len(klines) < 2: return None
            k = klines[-2]
            return Candle(
                open_time=datetime.fromtimestamp(k.open_time / 1000, tz=UTC),
                open=float(k.open), high=float(k.high), low=float(k.low), close=float(k.close),
                volume=0.0 # Volume not strictly needed for exit check
            )
        except Exception:
            return None

    async def load_1h_sell_quote(self, symbol: str, hour_dt: datetime) -> float | None:
        """该 UTC 小时已完成 1h K 的主动卖出成交额（quote）。

        近似：sell_quote = quote_total - taker_buy_quote
        """
        try:
            h0 = hour_dt.replace(minute=0, second=0, microsecond=0, tzinfo=UTC)
            start_ms = int(h0.timestamp() * 1000)
            end_ms = start_ms + 3_600_000
            klines = await self._client.get_klines(
                symbol=symbol,
                interval="1h",
                start_time=start_ms,
                end_time=end_ms,
                limit=2,
            )
            if not klines:
                return None
            # 取该小时开始的那根（若返回两根，第一根应是 [start_ms, start_ms+1h)）
            k = klines[0]
            q_total = float(k.quote_asset_volume)
            q_buy = float(k.taker_buy_quote_asset_volume)
            return max(0.0, q_total - q_buy)
        except Exception:
            return None

    async def load_1d_sell_quote(self, symbol: str, day_dt: datetime) -> float | None:
        """某自然日（UTC 00:00 起）的已完成 1d K 主动卖出成交额（quote）。"""
        try:
            d0 = day_dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC)
            start_ms = int(d0.timestamp() * 1000)
            end_ms = start_ms + 86_400_000
            klines = await self._client.get_klines(
                symbol=symbol,
                interval="1d",
                start_time=start_ms,
                end_time=end_ms,
                limit=2,
            )
            if not klines:
                return None
            k = klines[0]
            q_total = float(k.quote_asset_volume)
            q_buy = float(k.taker_buy_quote_asset_volume)
            return max(0.0, q_total - q_buy)
        except Exception:
            return None

    async def sell_surge_ratio_at_hour(
        self, symbol: str, hour_dt: datetime
    ) -> tuple[float | None, float | None]:
        """返回 (sell_surge_ratio, yesterday_avg_hour_sell_quote)。"""
        try:
            h0 = hour_dt.replace(minute=0, second=0, microsecond=0, tzinfo=UTC)
            sell_1h = await self.load_1h_sell_quote(symbol, h0)
            if sell_1h is None:
                return None, None

            yday0 = (h0 - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            sell_1d = await self.load_1d_sell_quote(symbol, yday0)
            if sell_1d is None:
                return None, None

            yavg = sell_1d / 24.0
            if yavg <= 0:
                return None, yavg
            return sell_1h / yavg, yavg
        except Exception:
            return None, None

    async def scan_sell_surge_rank(
        self,
        hour_dt: datetime | None = None,
        concurrency: int = 20,
    ) -> list[dict]:
        """返回所有 USDT 合约上一小时卖量倍数，降序排列，含 24hr 涨幅。

        每条记录: {symbol, sell_surge_ratio, yesterday_avg_hour_sell_volume, pct_chg_24h}
        """
        if hour_dt is None:
            now = datetime.now(UTC)
            hour_dt = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)

        tradeable = set(await self.get_usdt_symbols())
        tickers = await self._client.get_24hr_tickers()
        pct_map: dict[str, float] = {
            t["symbol"]: float(t.get("priceChangePercent", 0))
            for t in tickers
            if t.get("symbol", "") in tradeable
        }

        semaphore = asyncio.Semaphore(concurrency)
        results: list[dict] = []

        async def fetch_one(symbol: str):
            async with semaphore:
                sr, yavg = await self.sell_surge_ratio_at_hour(symbol, hour_dt)
                if sr is not None and sr > 0:
                    results.append({
                        "symbol": symbol,
                        "sell_surge_ratio": round(sr, 2),
                        "yesterday_avg_hour_sell_volume": round(yavg, 2) if yavg is not None else None,
                        "pct_chg_24h": round(pct_map.get(symbol, 0.0), 2),
                    })

        await asyncio.gather(*[fetch_one(s) for s in tradeable])
        results.sort(key=lambda x: x["sell_surge_ratio"], reverse=True)
        return results

    async def get_current_price(self, symbol: str) -> float | None:
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
            return [(datetime.fromtimestamp(r.funding_time / 1000, tz=UTC), float(r.funding_rate)) for r in records]
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
                    open_time=datetime.fromtimestamp(k.open_time / 1000, tz=UTC),
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
