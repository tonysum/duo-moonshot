"""Preload hourly buckets: rolling pct >= min_pct_chg AND sell_surge_ratio > raw_min_sell_surge.

漏斗顺序与 duo-live ``live/rolling_scanner.py`` / ``docs/signal-scan-order.md`` 对齐：

1. ``scan_hourly_rolling_above_threshold`` → 涨幅合格集合
2. 每小时按涨幅降序截 ``max_sr_probe``（探测集，防 DB/REST 风暴；``<=0`` 表示不截断）
3. 对探测集算 ``sell_surge_ratio``，保留 **严格大于** ``min_sell_surge``
4. 按 ``candidate_rank_mode`` 降序截 ``top_n``（见 ``candidate_rank_score``）

供 ``R24RawSurgeRunner`` 使用。
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime

import psycopg2

from moonshot.export_hourly_gainer_sell_surge import _get_db_params, scan_hourly_rolling_above_threshold
from moonshot.sell_surge_signal import sell_surge_ratio_at_scan_hour

logger = logging.getLogger(__name__)


def candidate_rank_score(
    pct_chg: float,
    sr: float,
    mode: str,
    yavg: float | None = None,
) -> float:
    """卖量门之后的截断排序分数（越大越优先）。Paper / 回测 / live 共用同一套口径。"""
    m = (mode or "sr").strip().lower()
    y = 0.0
    if yavg is not None and yavg == yavg:  # not NaN
        y = max(float(yavg), 0.0)
    if m == "pct_log_sr":
        return float(pct_chg) * math.log(1.0 + max(float(sr), 0.0))
    if m == "pct_log_sr_liq":
        base = float(pct_chg) * math.log(1.0 + max(float(sr), 0.0))
        liq = math.log(1.0 + y)
        return base * max(liq, 0.2)
    if m != "sr":
        logger.warning("[R24-RS] unknown candidate_rank_mode %r — using sr", mode)
    return float(sr)


def _passes_sell_surge_gate(sr: float | None, *, min_sr: float, max_sr: float | None) -> bool:
    """原始信号层卖量门：严格大于 min_sr；若 max_sr 非空则必须 <= max_sr。"""
    if sr is None:
        return False
    if sr <= min_sr:
        return False
    if max_sr is not None and sr > max_sr:
        return False
    return True


def _passes_yavg_gate(yavg: float | None, min_yavg: float | None) -> bool:
    if min_yavg is None or min_yavg <= 0:
        return True
    if yavg is None:
        return False
    try:
        return float(yavg) >= float(min_yavg)
    except (TypeError, ValueError):
        return False


def _enrich_hits_sell_surge_batch(
    db_params: dict,
    batch: list[tuple[str, str, float]],
    min_sell_surge: float,
    max_sell_surge: float | None,
    min_yavg: float | None = None,
) -> list[tuple[str, str, float, float, float]]:
    """One worker: filter ``batch`` by sell surge; cache K1d baseline per (symbol, day)."""
    if not batch:
        return []
    baseline_cache: dict[tuple[str, date], float | None] = {}
    out: list[tuple[str, str, float, float, float]] = []
    try:
        conn = psycopg2.connect(**db_params)
        conn.autocommit = True
    except Exception as e:
        logger.error("[R24-RS] enrich worker DB connect failed: %s", e)
        return out

    try:
        for hour_key, symbol, pct in batch:
            dt = datetime.strptime(hour_key, "%Y-%m-%d %H:00").replace(tzinfo=UTC)
            sr, yavg = sell_surge_ratio_at_scan_hour(
                conn, symbol, dt, daily_sell_baseline_cache=baseline_cache
            )
            if not _passes_sell_surge_gate(sr, min_sr=min_sell_surge, max_sr=max_sell_surge):
                continue
            if not _passes_yavg_gate(yavg, min_yavg):
                continue
            yv = float(yavg) if yavg is not None else 0.0
            out.append((hour_key, symbol, float(pct), float(sr), yv))
    finally:
        conn.close()
    return out


def build_raw_surge_hourly_gainers(
    db,
    symbols: list[str],
    start: datetime,
    end: datetime,
    *,
    min_pct_chg: float,
    min_sell_surge: float,
    max_sell_surge: float | None = None,
    window_hours: int,
    workers: int,
    top_n: int = 5,
    max_sr_probe: int = 50,
    candidate_rank_mode: str = "sr",
    min_yavg_sell_volume: float | None = None,
) -> dict[str, list[tuple[str, float, float, float]]]:
    """Return ``{ 'YYYY-MM-DD HH:00': [(symbol, pct, sell_surge_ratio, yday_avg_hour_sell), ...] }``.

    仅包含卖量倍数 **严格大于** ``min_sell_surge`` 的行。
    每个 UTC 小时最多 ``top_n`` 条，顺序由 ``candidate_rank_mode`` 决定。
    """
    hits = scan_hourly_rolling_above_threshold(
        db,
        symbols,
        start,
        end,
        min_pct_chg=min_pct_chg,
        window_hours=window_hours,
        workers=workers,
    )

    by_hour_pct: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for hour_key, symbol, pct in hits:
        by_hour_pct[hour_key].append((symbol, float(pct)))

    probe_rows: list[tuple[str, str, float]] = []
    for hour_key, sym_pcts in by_hour_pct.items():
        sym_pcts.sort(key=lambda x: x[1], reverse=True)
        if max_sr_probe > 0:
            sym_pcts = sym_pcts[:max_sr_probe]
        for sym, pct in sym_pcts:
            probe_rows.append((hour_key, sym, pct))

    by_hour: dict[str, list[tuple[str, float, float, float]]] = defaultdict(list)
    if probe_rows:
        db_params = _get_db_params(db)
        nw = max(1, min(int(workers), len(probe_rows)))
        chunk = (len(probe_rows) + nw - 1) // nw
        batches = [probe_rows[i : i + chunk] for i in range(0, len(probe_rows), chunk)]
        logger.info(
            "[R24-RS] sell_surge filter: %d pct-hits → %d probe rows → %d workers (~%d rows each); "
            "rank=%s top_n=%s max_probe=%s",
            len(hits),
            len(probe_rows),
            len(batches),
            chunk,
            candidate_rank_mode,
            top_n,
            max_sr_probe if max_sr_probe > 0 else "∞",
        )
        merged_rows: list[tuple[str, str, float, float, float]] = []
        with ThreadPoolExecutor(max_workers=len(batches)) as ex:
            futs = {
                ex.submit(
                    _enrich_hits_sell_surge_batch,
                    db_params,
                    b,
                    min_sell_surge,
                    max_sell_surge,
                    min_yavg_sell_volume,
                ): i
                for i, b in enumerate(batches)
            }
            for fut in as_completed(futs):
                try:
                    merged_rows.extend(fut.result())
                except Exception as e:
                    logger.error("[R24-RS] enrich batch failed: %s", e)

        for hour_key, symbol, pct, sr, yv in merged_rows:
            by_hour[hour_key].append((symbol, pct, sr, yv))

    mode = (candidate_rank_mode or "sr").strip().lower()
    for k in list(by_hour.keys()):
        rows = by_hour[k]
        rows.sort(
            key=lambda x: candidate_rank_score(x[1], x[2], mode, x[3]),
            reverse=True,
        )
        if top_n > 0:
            by_hour[k] = rows[:top_n]
        else:
            by_hour[k] = rows

    out = dict(by_hour)
    logger.info(
        "[R24-RS] raw preload: %d UTC hours with >=1 signal (pct>=%.2f, sell>%.2f)",
        len(out),
        min_pct_chg,
        min_sell_surge,
    )
    return out
