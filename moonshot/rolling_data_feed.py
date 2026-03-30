"""RollingDataFeed — DataFeed extension for Moonshot-R24 strategy.

Adds preload_hourly_gainers() which computes 24h rolling price change
for every hour using K1h tables, simulating Binance 24hr Ticker semantics.

Uses ThreadPoolExecutor for parallel DB queries across symbols.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta

import psycopg2
from psycopg2 import sql

from moonshot.data_feed import DataFeed

logger = logging.getLogger(__name__)

# Default worker count: use CPU count, cap at 8 to avoid DB connection exhaustion
_DEFAULT_WORKERS = min(int(os.getenv("R24_WORKERS", "0")) or (os.cpu_count() or 4), 8)


def _process_symbol_batch(
    db_params: dict,
    symbols: list[str],
    query_start_ms: int,
    query_end_ms: int,
    start_ms: int,
    end_ms: int,
    window_ms: int = 86_400_000,
) -> dict[str, list[tuple[str, float]]]:
    """Worker function: query K1h for a batch of symbols using its own DB connection.

    Returns {datetime_key: [(symbol, pct_chg), ...]}
    """
    result: dict[str, list[tuple[str, float]]] = defaultdict(list)

    try:
        conn = psycopg2.connect(**db_params)
        conn.autocommit = True
    except Exception as e:
        logger.error("Worker failed to connect to DB: %s", e)
        return dict(result)

    try:
        for symbol in symbols:
            try:
                tbl = f"K1h{symbol}"
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL(
                            "SELECT open_time, close FROM {} "
                            "WHERE open_time >= %s AND open_time <= %s "
                            "ORDER BY open_time"
                        ).format(sql.Identifier(tbl)),
                        [query_start_ms, query_end_ms],
                    )
                    rows = cur.fetchall()

                min_rows = max(int(window_ms / 3_600_000) + 1, 2)
                if len(rows) < min_rows:
                    continue

                # Build {open_time_ms: close} for O(1) lookback
                close_map: dict[int, float] = {}
                for ts, c in rows:
                    close_map[int(ts)] = float(c)

                # For each hour, compute 24h rolling change
                for ts, c in rows:
                    ts_int = int(ts)
                    close_now = float(c)

                    if ts_int < start_ms or ts_int > end_ms:
                        continue

                    ts_window_ago = ts_int - window_ms
                    close_24h = close_map.get(ts_window_ago)
                    if close_24h and close_24h > 0:
                        pct_chg = (close_now - close_24h) / close_24h * 100
                        dt = datetime.fromtimestamp(ts_int / 1000, tz=UTC)
                        key = dt.strftime('%Y-%m-%d %H:00')
                        result[key].append((symbol, pct_chg))
            except Exception:
                continue
    finally:
        conn.close()

    return dict(result)


class RollingDataFeed(DataFeed):
    """Extends DataFeed with hourly 24h-rolling gainer preloading (parallelized)."""

    def __init__(self, db, workers: int = _DEFAULT_WORKERS) -> None:
        super().__init__(db)
        self._hourly_gainers_cache: dict[str, list[tuple[str, float]]] = {}
        self._workers = workers

    def _get_db_params(self) -> dict:
        """Extract connection params from the DB instance for worker threads."""
        return {
            "host": self._db.host,
            "port": self._db.port,
            "database": self._db.database,
            "user": self._db.user,
            "password": self._db.password,
        }

    def preload_hourly_gainers(
        self,
        start: datetime,
        end: datetime,
        top_n: int = 3,
        window_hours: int = 24,
    ) -> dict[str, list[tuple[str, float]]]:
        """Preload 24h rolling top gainers for every hour in [start, end].

        Uses ThreadPoolExecutor to query K1h tables in parallel across symbols.
        Each worker gets its own DB connection and processes a batch of symbols.

        Workers can be controlled via:
          - Constructor: RollingDataFeed(db, workers=4)
          - Env var: R24_WORKERS=4

        Returns:
            {datetime_key: [(symbol, pct_chg), ...]} sorted desc, truncated to top_n.
        """
        symbols = self.load_all_symbols()
        if not symbols:
            return {}

        window_ms = window_hours * 3_600_000
        query_start_ms = int((start - timedelta(hours=window_hours)).timestamp() * 1000)
        query_end_ms = int(end.timestamp() * 1000)
        start_ms = int(start.timestamp() * 1000)
        end_ms = query_end_ms

        n_workers = min(self._workers, len(symbols))
        db_params = self._get_db_params()

        # Split symbols into batches
        batch_size = (len(symbols) + n_workers - 1) // n_workers
        batches = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]

        logger.info(
            "RollingDataFeed: preloading %d symbols with %d workers (%d per batch), window=%dh",
            len(symbols), len(batches), batch_size, window_hours,
        )

        # Parallel execution
        hourly_data: dict[str, list[tuple[str, float]]] = defaultdict(list)

        with ThreadPoolExecutor(max_workers=len(batches)) as executor:
            futures = {
                executor.submit(
                    _process_symbol_batch,
                    db_params, batch,
                    query_start_ms, query_end_ms,
                    start_ms, end_ms, window_ms,
                ): i
                for i, batch in enumerate(batches)
            }

            for future in as_completed(futures):
                batch_idx = futures[future]
                try:
                    batch_result = future.result()
                    for key, items in batch_result.items():
                        hourly_data[key].extend(items)
                except Exception as e:
                    logger.error("Worker batch %d failed: %s", batch_idx, e)

        # Sort and truncate
        result = {}
        for key, items in hourly_data.items():
            items.sort(key=lambda x: x[1], reverse=True)
            result[key] = items[:top_n]

        logger.info(
            "RollingDataFeed: preloaded %d hourly snapshots",
            len(result),
        )
        self._hourly_gainers_cache = result
        return result

    def load_hourly_top_gainers(
        self,
        dt: datetime,
        top_n: int = 3,
    ) -> list[tuple[str, float]]:
        """Get preloaded hourly gainers for a specific hour."""
        key = dt.strftime('%Y-%m-%d %H:00')
        return self._hourly_gainers_cache.get(key, [])[:top_n]
