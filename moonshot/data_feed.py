"""DataFeed — DB access layer for duo-moonshot.

Only contains methods required by the Moonshot strategy.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg2 import sql

from moonshot.models import Candle

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^[A-Z0-9]+$")


def _validate_symbol(symbol: str) -> str:
    """Validate and normalize symbol name to prevent SQL injection."""
    sym = symbol.upper()
    if not _SYMBOL_RE.match(sym):
        raise ValueError(f"Invalid symbol {symbol!r}: must contain only A-Z and 0-9")
    return sym


class DataFeed:
    """Fetches K-line data from PostgreSQL for Moonshot strategy."""

    def __init__(self, db) -> None:
        self._db = db
        self._cache_4h = {}
        self._cache_15m = {}
        self._cache_5m = {}
        self._cache_top_ratio = {}
        self._cache_listing_date = {}
        self._daily_gainers_cache: dict = {}

    def load_1h(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        sym = _validate_symbol(symbol)
        query = sql.SQL(
            "SELECT open_time, open, high, low, close, volume FROM {} "
            "WHERE open_time >= %s AND open_time <= %s ORDER BY open_time"
        ).format(sql.Identifier(f"K1h{sym}"))
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        try:
            with self._db.conn.cursor() as cur:
                cur.execute(query, [start_ms, end_ms])
                rows = cur.fetchall()
            return [
                Candle(
                    open_time=datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
                    open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
                    volume=float(r[5]) if r[5] is not None else 0.0,
                )
                for r in rows
            ]
        except Exception as e:
            logger.warning("Failed to load 1h candles for %s: %s", symbol, e)
            return []

    def load_4h(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        key = (symbol, start, end)
        if key in self._cache_4h: return self._cache_4h[key]
        sym = _validate_symbol(symbol)
        query = sql.SQL(
            "SELECT open_time, open, high, low, close, volume FROM {} "
            "WHERE open_time >= %s AND open_time <= %s ORDER BY open_time"
        ).format(sql.Identifier(f"K4h{sym}"))
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        try:
            with self._db.conn.cursor() as cur:
                cur.execute(query, [start_ms, end_ms])
                rows = cur.fetchall()
            res = [
                Candle(
                    open_time=datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
                    open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
                    volume=float(r[5]) if r[5] is not None else 0.0,
                )
                for r in rows
            ]
            self._cache_4h[key] = res
            return res
        except Exception as e:
            logger.warning("Failed to load 4h candles for %s: %s", symbol, e)
            return []

    def load_15m(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        key = (symbol, start, end)
        if key in self._cache_15m: return self._cache_15m[key]
        sym = _validate_symbol(symbol)
        query = sql.SQL(
            "SELECT open_time, open, high, low, close FROM {} "
            "WHERE open_time >= %s AND open_time <= %s ORDER BY open_time"
        ).format(sql.Identifier(f"K15m{sym}"))
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        try:
            with self._db.conn.cursor() as cur:
                cur.execute(query, [start_ms, end_ms])
                rows = cur.fetchall()
            res = [
                Candle(
                    open_time=datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
                    open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
                )
                for r in rows
            ]
            self._cache_15m[key] = res
            return res
        except Exception as e:
            logger.warning("Failed to load 15m candles for %s: %s", symbol, e)
            return []

    def load_5m(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        key = (symbol, start, end)
        if key in self._cache_5m: return self._cache_5m[key]
        sym = _validate_symbol(symbol)
        query = sql.SQL(
            "SELECT open_time, open, high, low, close FROM {} "
            "WHERE open_time >= %s AND open_time <= %s ORDER BY open_time"
        ).format(sql.Identifier(f"K5m{sym}"))
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        try:
            with self._db.conn.cursor() as cur:
                cur.execute(query, [start_ms, end_ms])
                rows = cur.fetchall()
            res = [
                Candle(
                    open_time=datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
                    open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
                )
                for r in rows
            ]
            self._cache_5m[key] = res
            return res
        except Exception as e:
            logger.warning("Failed to load 5m candles for %s: %s", symbol, e)
            return []

    def load_listing_date(self, symbol: str) -> Optional[datetime]:
        if symbol in self._cache_listing_date: return self._cache_listing_date[symbol]
        sym = _validate_symbol(symbol)
        query = sql.SQL("SELECT MIN(open_time) FROM {}").format(sql.Identifier(f"K1d{sym}"))
        try:
            with self._db.conn.cursor() as cur:
                cur.execute(query)
                row = cur.fetchone()
            if row and row[0] is not None:
                dt = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc)
                self._cache_listing_date[symbol] = dt
                return dt
        except Exception: pass
        return None

    def load_all_symbols(self) -> list[str]:
        if hasattr(self, '_symbols_cache'): return self._symbols_cache
        try:
            with self._db.conn.cursor() as cur:
                cur.execute("SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'K1d%'")
                rows = cur.fetchall()
            self._symbols_cache = sorted(r[0][3:] for r in rows if _SYMBOL_RE.match(r[0][3:]))
            return self._symbols_cache
        except Exception: return []

    def preload_daily_gainers(self, start: datetime, end: datetime, top_n: int = 1) -> dict:
        symbols = self.load_all_symbols()
        if not symbols: return {}
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000) + 86400000
        from collections import defaultdict
        daily_data = defaultdict(list)
        for symbol in symbols:
            try:
                tbl = f"K1d{symbol}"
                with self._db.conn.cursor() as cur:
                    cur.execute(sql.SQL("SELECT open_time, close FROM {} WHERE open_time >= %s AND open_time < %s ORDER BY open_time").format(sql.Identifier(tbl)), [start_ms - 86400000, end_ms])
                    rows = cur.fetchall()
                for i in range(1, len(rows)):
                    prev, today, ts = float(rows[i-1][1]), float(rows[i][1]), rows[i][0]
                    if prev > 0:
                        daily_data[datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')].append((symbol, (today - prev) / prev * 100))
            except Exception: continue
        res = {}
        for d, items in daily_data.items():
            items.sort(key=lambda x: x[1], reverse=True)
            res[d] = items[:top_n]
        return res

    def load_daily_top_gainers(self, date: datetime, top_n: int = 1) -> list:
        if hasattr(self, '_daily_gainers_cache'):
            return self._daily_gainers_cache.get(date.strftime('%Y-%m-%d'), [])[:top_n]
        return [] # Backtest should always use preload

    def load_24h_volume(self, symbol: str, dt: datetime) -> float:
        sym = _validate_symbol(symbol)
        end_ms = int(dt.timestamp() * 1000)
        start_ms = end_ms - 86400000
        try:
            with self._db.conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT SUM(quote_volume) FROM {} WHERE open_time >= %s AND open_time < %s").format(sql.Identifier(f"K1h{sym}")), [start_ms, end_ms])
                row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else -1.0
        except Exception: return -1.0

    def load_1d_open(self, symbol: str, date: datetime) -> Optional[float]:
        sym = _validate_symbol(symbol)
        day_ms = int(date.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        try:
            with self._db.conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT open FROM {} WHERE open_time >= %s AND open_time < %s LIMIT 1").format(sql.Identifier(f"K1d{sym}")), [day_ms, day_ms + 86400000])
                row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None
        except Exception: return None

    def load_funding_history(self, symbol: str, start: datetime, end: datetime) -> list:
        start_n = start.replace(tzinfo=None); end_n = end.replace(tzinfo=None)
        try:
            with self._db.conn.cursor() as cur:
                cur.execute("SELECT funding_time, funding_rate FROM funding_rates WHERE symbol = %s AND funding_time >= %s AND funding_time <= %s ORDER BY funding_time", [symbol, start_n, end_n])
                rows = cur.fetchall()
            return [(r[0].replace(tzinfo=timezone.utc) if r[0].tzinfo is None else r[0], float(r[1])) for r in rows]
        except Exception: return []

    def load_30d_avg_price(self, symbol: str, dt: datetime) -> Optional[float]:
        sym = _validate_symbol(symbol)
        end_ms = int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        start_ms = end_ms - 30 * 86400000
        try:
            with self._db.conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT AVG(close) FROM {} WHERE open_time >= %s AND open_time < %s").format(sql.Identifier(f"K1d{sym}")), [start_ms, end_ms])
                row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None
        except Exception: return None

    def load_1d_close(self, symbol: str, date: datetime) -> Optional[float]:
        sym = _validate_symbol(symbol)
        day_ms = int(date.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        try:
            with self._db.conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT close FROM {} WHERE open_time >= %s AND open_time < %s LIMIT 1").format(sql.Identifier(f"K1d{sym}")), [day_ms, day_ms + 86400000])
                row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None
        except Exception: return None

    def load_top_trader_ratio(self, symbol: str, dt: datetime) -> Optional[float]:
        key = (symbol, dt)
        if key in self._cache_top_ratio: return self._cache_top_ratio[key]
        t_ms = int(dt.timestamp() * 1000)
        try:
            with self._db.conn.cursor() as cur:
                cur.execute("SELECT long_short_ratio FROM top_account_ratio WHERE symbol = %s AND timestamp >= %s AND timestamp <= %s ORDER BY ABS(timestamp - %s) ASC LIMIT 1", [symbol, t_ms - 86400000, t_ms + 86400000, t_ms])
                row = cur.fetchone()
            res = float(row[0]) if row and row[0] is not None else None
            self._cache_top_ratio[key] = res
            return res
        except Exception: return None

    def load_supertrend(self, symbol: str, dt: datetime, period: int = 10, multiplier: float = 3.0, timeframe: str = "1h") -> str:
        if timeframe == "15m": candles = self.load_15m(symbol, dt - timedelta(hours=max(15, (period*3*15)//60+1)), dt)
        elif timeframe == "4h": candles = self.load_4h(symbol, dt - timedelta(hours=max(150, period*3*4)), dt)
        else: candles = self.load_1h(symbol, dt - timedelta(hours=max(50, period*3)), dt)
        if len(candles) < period + 1: return "unknown"
        trs = []; hl2s = []
        for i in range(len(candles)):
            c = candles[i]; hl2s.append((c.high + c.low) / 2)
            if i == 0: trs.append(c.high - c.low)
            else: prev = candles[i-1]; trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
        atrs = [0.0] * len(candles)
        for i in range(period - 1, len(candles)): atrs[i] = sum(trs[i - period + 1 : i + 1]) / period
        ubounds = [0.0] * len(candles); lbounds = [0.0] * len(candles); trend = [1] * len(candles)
        start = period
        for i in range(start, len(candles)):
            atr, hl2 = atrs[i], hl2s[i]; cub, clb = hl2 + multiplier * atr, hl2 - multiplier * atr
            pub, plb, pc = ubounds[i-1], lbounds[i-1], candles[i-1].close
            if i == start: ubounds[i], lbounds[i] = cub, clb
            else:
                ubounds[i] = cub if cub < pub or pc > pub else pub
                lbounds[i] = clb if clb > plb or pc < plb else plb
            if candles[i].close > ubounds[i]: trend[i] = 1
            elif candles[i].close < lbounds[i]: trend[i] = -1
            else:
                trend[i] = trend[i-1]
                if trend[i] == 1 and lbounds[i] < plb: lbounds[i] = plb
                if trend[i] == -1 and ubounds[i] > pub: ubounds[i] = pub
        return "bullish" if trend[-1] == 1 else "bearish"
