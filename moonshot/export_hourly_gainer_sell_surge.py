"""Export sell-surge metrics for symbols whose **rolling** hourly gain exceeds a threshold.

涨幅定义（与 R24 / ``rolling_data_feed._process_symbol_batch`` 一致）：
对每根 1h K 的 close，相对 **恰好 N 小时前**（默认 N=24）同一根 1h close 的涨跌幅（%），
不是单根 K 内 open→close。

用法::

    python -m moonshot.export_hourly_gainer_sell_surge --start 2025-01-01 --end 2025-01-31
    python -m moonshot.export_hourly_gainer_sell_surge --min-pct 10 --window-hours 24 --output reports/out.csv

默认只写入 **卖量倍数 > --min-sell-surge**（默认 10）的行；无卖量数据或倍数不足的丢弃。
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg2
from psycopg2 import sql

from moonshot.data_feed import DataFeed
from moonshot.db import get_postgres_db as get_db
from moonshot.sell_surge_signal import sell_surge_ratio_at_scan_hour

logger = logging.getLogger(__name__)

_DEFAULT_WORKERS = min(int(os.getenv("R24_WORKERS", "0")) or (os.cpu_count() or 4), 8)


def _get_db_params(db) -> dict:
    return {
        "host": db.host,
        "port": db.port,
        "database": db.database,
        "user": db.user,
        "password": db.password,
    }


def _process_symbol_batch_filtered(
    db_params: dict,
    symbols: list[str],
    query_start_ms: int,
    query_end_ms: int,
    start_ms: int,
    end_ms: int,
    window_ms: int,
    min_pct_chg: float,
) -> list[tuple[str, str, float]]:
    """Return list of (hour_key, symbol, pct_chg) where pct_chg >= min_pct_chg."""
    out: list[tuple[str, str, float]] = []

    try:
        conn = psycopg2.connect(**db_params)
        conn.autocommit = True
    except Exception as e:
        logger.error("Worker failed to connect to DB: %s", e)
        return out

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

                close_map: dict[int, float] = {}
                for ts, c in rows:
                    close_map[int(ts)] = float(c)

                for ts, c in rows:
                    ts_int = int(ts)
                    close_now = float(c)

                    if ts_int < start_ms or ts_int > end_ms:
                        continue

                    ts_window_ago = ts_int - window_ms
                    close_win = close_map.get(ts_window_ago)
                    if close_win and close_win > 0:
                        pct_chg = (close_now - close_win) / close_win * 100
                        if pct_chg >= min_pct_chg:
                            dt = datetime.fromtimestamp(ts_int / 1000, tz=UTC)
                            key = dt.strftime("%Y-%m-%d %H:00")
                            out.append((key, symbol, pct_chg))
            except Exception:
                continue
    finally:
        conn.close()

    return out


def scan_hourly_rolling_above_threshold(
    db,
    symbols: list[str],
    start: datetime,
    end: datetime,
    *,
    min_pct_chg: float,
    window_hours: int,
    workers: int,
) -> list[tuple[str, str, float]]:
    """Parallel scan: all (utc_hour_key, symbol, pct_chg) with pct_chg >= min_pct_chg."""
    if not symbols:
        return []

    window_ms = window_hours * 3_600_000
    query_start_ms = int((start - timedelta(hours=window_hours)).timestamp() * 1000)
    query_end_ms = int(end.timestamp() * 1000)
    start_ms = int(start.timestamp() * 1000)
    end_ms = query_end_ms

    db_params = _get_db_params(db)
    n_workers = max(1, min(workers, len(symbols)))
    batch_size = (len(symbols) + n_workers - 1) // n_workers
    batches = [symbols[i : i + batch_size] for i in range(0, len(symbols), batch_size)]

    logger.info(
        "Scanning %d symbols, %d workers, window=%dh, min_pct>=%.2f%%",
        len(symbols), len(batches), window_hours, min_pct_chg,
    )

    merged: list[tuple[str, str, float]] = []
    with ThreadPoolExecutor(max_workers=len(batches)) as executor:
        futures = {
            executor.submit(
                _process_symbol_batch_filtered,
                db_params,
                batch,
                query_start_ms,
                query_end_ms,
                start_ms,
                end_ms,
                window_ms,
                min_pct_chg,
            ): i
            for i, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            try:
                merged.extend(future.result())
            except Exception as e:
                logger.error("Batch failed: %s", e)

    merged.sort(key=lambda x: (x[0], x[1]))
    return merged


def export_csv(
    rows: list[tuple[str, str, float, float | None, float | None]],
    path: Path,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "utc_hour",
                "symbol",
                "pct_chg_roll",
                "sell_surge_ratio",
                "yesterday_avg_hour_sell_volume",
            ]
        )
        for r in rows:
            w.writerow(r)
    logger.info("Wrote %d rows to %s", len(rows), path)
    return path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    p = argparse.ArgumentParser(
        description="Export sell-surge for symbols with rolling hourly gain >= min-pct (default 10%%)."
    )
    p.add_argument("--start", required=True, help="Start date UTC YYYY-MM-DD")
    p.add_argument(
        "--end",
        required=True,
        help="End date UTC YYYY-MM-DD (inclusive; scans through that full calendar day)",
    )
    p.add_argument("--min-pct", type=float, default=10.0, help="Minimum rolling %% gain (default 10)")
    p.add_argument("--window-hours", type=int, default=24, help="Rolling lookback hours (default 24)")
    p.add_argument("--workers", type=int, default=0, help="Parallel workers (0=auto, cap 8)")
    p.add_argument(
        "--output",
        default=None,
        help="Output CSV path (default: reports/hourly_gainer_sell_surge_<timestamp>.csv)",
    )
    p.add_argument(
        "--min-sell-surge",
        type=float,
        default=10.0,
        metavar="X",
        help="Only keep rows with sell_surge_ratio strictly greater than this (default: 10)",
    )

    args = p.parse_args()

    # Inclusive UTC calendar range [start_date 00:00, end_date 23:59:59…] — upper bound = (end_date+1) 00:00 exclusive
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)

    workers = args.workers if args.workers > 0 else _DEFAULT_WORKERS

    db = get_db()
    db.connect()
    try:
        feed = DataFeed(db)
        symbols = feed.load_all_symbols()
        hits = scan_hourly_rolling_above_threshold(
            db,
            symbols,
            start,
            end,
            min_pct_chg=args.min_pct,
            window_hours=args.window_hours,
            workers=workers,
        )
        logger.info("Hits (hour,symbol) with pct >= %.2f: %d", args.min_pct, len(hits))

        out_rows: list[tuple[str, str, float, float | None, float | None]] = []
        conn = db.conn
        min_sr = args.min_sell_surge
        for hour_key, symbol, pct in hits:
            dt = datetime.strptime(hour_key, "%Y-%m-%d %H:00").replace(tzinfo=UTC)
            sr, yavg = sell_surge_ratio_at_scan_hour(conn, symbol, dt)
            if sr is None or sr <= min_sr:
                continue
            out_rows.append((hour_key, symbol, round(pct, 6), sr, yavg))

        logger.info(
            "Rows after sell_surge_ratio > %.2f: %d",
            min_sr,
            len(out_rows),
        )

        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        out_path = (
            Path(args.output)
            if args.output
            else Path("reports") / f"hourly_gainer_sell_surge_{ts}.csv"
        )
        export_csv(out_rows, out_path)
        print(out_path.resolve())
    finally:
        db.close()


if __name__ == "__main__":
    main()
