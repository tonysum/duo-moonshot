"""Sell-volume surge ratio — aligned with hm1l `get_daily_1hour_surge_signals` hour-bar logic.

At UTC scan time ``dt`` (typically on the hour), compares **this hour's** active sell volume
(``volume - active_buy_volume``) to **yesterday's** daily sell volume divided by 24.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from psycopg2 import sql

from moonshot.data_feed import _validate_symbol

logger = logging.getLogger(__name__)


def _utc_day_start_ms(d: date) -> int:
    dt = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def sell_surge_ratio_at_scan_hour(
    conn,
    symbol: str,
    dt: datetime,
    *,
    daily_sell_baseline_cache: dict[tuple[str, date], float | None] | None = None,
) -> tuple[float | None, float | None]:
    """Return ``(surge_ratio, yesterday_avg_hour_sell_volume)`` for the K1h bar at ``dt``'s hour.

    ``surge_ratio`` is hour_sell_volume / yesterday_avg_hour_sell_volume.
    Returns ``(None, None)`` if tables/rows are missing or invalid.

    If ``daily_sell_baseline_cache`` is provided, keys ``(symbol, yesterday_utc_date)`` cache
    the yesterday daily-derived ``yesterday_avg_hour`` (or ``None`` if unavailable) to avoid
    repeated K1d reads when scanning many hours for the same coin.
    """
    sym = _validate_symbol(symbol)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)

    hour_start = dt.replace(minute=0, second=0, microsecond=0)
    yday = hour_start.date() - timedelta(days=1)
    cache_key = (sym, yday)
    daily_ident = sql.Identifier(f"K1d{sym}")
    hourly_ident = sql.Identifier(f"K1h{sym}")

    try:
        with conn.cursor() as cur:
            yesterday_avg_hour: float | None
            if daily_sell_baseline_cache is not None and cache_key in daily_sell_baseline_cache:
                yesterday_avg_hour = daily_sell_baseline_cache[cache_key]
                if yesterday_avg_hour is None:
                    return None, None
            else:
                y0, y1 = _utc_day_start_ms(yday), _utc_day_start_ms(yday) + 86_400_000
                cur.execute(
                    sql.SQL(
                        "SELECT volume, active_buy_volume FROM {} "
                        "WHERE open_time >= %s AND open_time < %s"
                    ).format(daily_ident),
                    [y0, y1],
                )
                drow = cur.fetchone()
                if not drow or drow[0] is None or drow[1] is None:
                    if daily_sell_baseline_cache is not None:
                        daily_sell_baseline_cache[cache_key] = None
                    return None, None
                d_vol, d_buy = float(drow[0]), float(drow[1])
                if not d_vol or not d_buy:
                    if daily_sell_baseline_cache is not None:
                        daily_sell_baseline_cache[cache_key] = None
                    return None, None
                yesterday_sell = d_vol - d_buy
                yesterday_avg_hour = yesterday_sell / 24.0
                if yesterday_avg_hour <= 0:
                    if daily_sell_baseline_cache is not None:
                        daily_sell_baseline_cache[cache_key] = None
                    return None, None
                if daily_sell_baseline_cache is not None:
                    daily_sell_baseline_cache[cache_key] = yesterday_avg_hour

            h0 = int(hour_start.timestamp() * 1000)
            h1 = h0 + 3_600_000
            cur.execute(
                sql.SQL(
                    "SELECT volume, active_buy_volume FROM {} "
                    "WHERE open_time >= %s AND open_time < %s ORDER BY open_time ASC LIMIT 1"
                ).format(hourly_ident),
                [h0, h1],
            )
            hrow = cur.fetchone()
            if not hrow or hrow[0] is None or hrow[1] is None:
                return None, None
            h_vol, h_buy = float(hrow[0]), float(hrow[1])
            if not h_vol or not h_buy:
                return None, None
            hour_sell = h_vol - h_buy
            ratio = hour_sell / yesterday_avg_hour
            return ratio, yesterday_avg_hour
    except Exception as e:
        logger.debug("sell_surge_ratio_at_scan_hour %s @ %s: %s", sym, hour_start, e)
        return None, None


def first_sell_surge_hit_for_symbol_day(
    conn,
    symbol: str,
    check_date: str,
    *,
    threshold: float,
    max_ratio: float,
) -> dict | None:
    """First hour of UTC ``check_date`` where sell surge is in ``[threshold, max_ratio]``.

    Matches hm1l ``get_daily_1hour_surge_signals`` per-symbol loop: yesterday daily sell / 24
    as baseline; scan today's 1h rows in order; stop at first qualifying hour or first hour
    above ``max_ratio`` (subsequent hours skipped for that symbol).

    Returns a dict with ``symbol``, ``signal_datetime``, ``signal_price``, ``surge_ratio``,
    ``signal_hour_volume``, ``yesterday_avg_hour_volume``, or ``None``.
    """
    sym = _validate_symbol(symbol)
    try:
        d = datetime.strptime(check_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    yday = d - timedelta(days=1)
    y0, y1 = _utc_day_start_ms(yday), _utc_day_start_ms(yday) + 86_400_000
    d0, d1 = _utc_day_start_ms(d), _utc_day_start_ms(d) + 86_400_000

    daily_ident = sql.Identifier(f"K1d{sym}")
    hourly_ident = sql.Identifier(f"K1h{sym}")

    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT volume, active_buy_volume FROM {} "
                    "WHERE open_time >= %s AND open_time < %s"
                ).format(daily_ident),
                [y0, y1],
            )
            yesterday_row = cur.fetchone()
            if not yesterday_row or yesterday_row[0] is None or yesterday_row[1] is None:
                return None
            if not yesterday_row[0] or not yesterday_row[1]:
                return None
            yesterday_daily_sell = float(yesterday_row[0]) - float(yesterday_row[1])
            yesterday_avg_hour_volume = yesterday_daily_sell / 24.0
            if yesterday_avg_hour_volume <= 0:
                return None

            cur.execute(
                sql.SQL(
                    "SELECT open_time, volume, active_buy_volume, close FROM {} "
                    "WHERE open_time >= %s AND open_time < %s ORDER BY open_time ASC"
                ).format(hourly_ident),
                [d0, d1],
            )
            today_hours = cur.fetchall()
            if not today_hours:
                return None

            for hour_data in today_hours:
                hour_open_time, hour_total_volume, hour_buy_volume, hour_price = hour_data
                if not hour_total_volume or not hour_buy_volume or not hour_price:
                    continue
                hour_sell_volume = float(hour_total_volume) - float(hour_buy_volume)
                surge_ratio = hour_sell_volume / yesterday_avg_hour_volume

                if surge_ratio >= threshold and surge_ratio <= max_ratio:
                    signal_datetime = datetime.fromtimestamp(
                        int(hour_open_time) / 1000.0, tz=UTC
                    )
                    return {
                        "symbol": sym,
                        "signal_datetime": signal_datetime,
                        "signal_price": float(hour_price),
                        "surge_ratio": surge_ratio,
                        "signal_hour_volume": hour_sell_volume,
                        "yesterday_avg_hour_volume": yesterday_avg_hour_volume,
                    }
                if surge_ratio > max_ratio:
                    break
    except Exception as e:
        logger.debug("first_sell_surge_hit_for_symbol_day %s %s: %s", sym, check_date, e)
        return None
    return None
