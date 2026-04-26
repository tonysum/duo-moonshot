"""R24 Raw Surge — 聚焦信号/扫描/时间参数的单阶段 Optuna 优化。

相对 ``r24_raw_surge_optimizer`` 的三阶段全参搜索，这个 optimizer **只动 7 个参数**：

1. ``raw_min_sell_surge``     — 卖量倍数门（严格 >），搜索 1.0 起
2. ``min_pct_chg``            — 滚动涨幅门，搜索 5.0 起
3. ``top_n``                  — 卖量门后按 ``candidate_rank_mode`` 截断
4. ``scan_interval_hours``    — 扫描周期（小时）
5. ``signal_cooldown_hours``  — 同符号信号冷却（小时）
6. ``max_hold_days``          — 最大持仓天数
7. ``candidate_rank_mode``    — {sr, pct_log_sr}（见 ``docs/signal-scan-order.md``）

其它字段（``tp_initial`` / ``sl_threshold`` / ``trailing_*`` / ``leverage`` /
``max_positions`` / ``position_size_ratio`` / ``rolling_window_hours`` / ``max_sr_probe`` / ``min_listed_days`` …）
从 ``--base-config`` 指定的 JSON（默认同回测入口 ``config/r24_raw_surge_params.json``）加载并**固定不动**，
便于单独评估"信号发现 + 时间节奏"的边际收益。

用法::

    # 80 trials，内存 study（中断即丢）
    python -m moonshot.r24_signal_focus_optimizer --trials 80

    # 带断点续跑（同 --storage + 同 --start/--end 可追加 trials）
    python -m moonshot.r24_signal_focus_optimizer --trials 200 \\
        --storage sqlite:///reports/optimizer/r24_signal_focus_optuna.sqlite3

    # 优化完写回 canonical JSON（与 base 合并）
    python -m moonshot.r24_signal_focus_optimizer --trials 200 --export-config .

    # 指定训练区间 + OOS 比例
    python -m moonshot.r24_signal_focus_optimizer --start 2025-06-01 --end 2025-12-31 \\
        --oos-ratio 0.3 --trials 150 --workers 8

结果：
  - ``reports/optimizer/r24_signal_focus_best.json``（Optuna best 参数）
  - ``--export-config .`` → ``config/r24_raw_surge_params.json``
    （将 best 的 7 个字段合并回 baseline，其它字段保留原值）

**断点续跑说明**：同 ``--storage`` 下重复运行会**追加** trials（不清零），前提是
``--start``/``--end``/``--base-config`` 代表同一搜索空间 + 同一训练数据；若改动这三者请换新
storage URL，避免不同数据/空间的 trials 混在同一 study 里。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import optuna

from moonshot.db import get_postgres_db as get_db
from moonshot.r24_raw_surge_config import RawSurgeR24Config
from moonshot.r24_raw_surge_config_load import (
    DEFAULT_CANONICAL,
    export_raw_surge_params_json,
    get_last_loaded_raw_surge_config_path,
    load_raw_surge_r24_config,
)
from moonshot.r24_raw_surge_optimizer import _run_trial_raw_surge
from moonshot.rolling_data_feed import RollingDataFeed
from moonshot.rolling_optimizer import _sizing_fields, r24_score
from moonshot.sizing import PositionSizingMode

logger = logging.getLogger(__name__)

RESULTS_DIR = Path("reports") / "optimizer"
RESULT_PREFIX = "r24_signal_focus"

# 被本 optimizer 搜索的字段（写回 best JSON 与日志时使用）
SEARCH_FIELDS: tuple[str, ...] = (
    "raw_min_sell_surge",
    "min_pct_chg",
    "top_n",
    "scan_interval_hours",
    "signal_cooldown_hours",
    "max_hold_days",
    "candidate_rank_mode",
)


def _signal_objective(
    trial: optuna.Trial,
    start: datetime,
    end: datetime,
    feed: RollingDataFeed,
    initial_capital: float,
    base_cfg: RawSurgeR24Config,
    position_sizing_mode: PositionSizingMode,
    fixed_invest_usd: float | None,
) -> float:
    raw_min_sell_surge = trial.suggest_float("raw_min_sell_surge", 1.0, 20.0, step=0.5)
    min_pct_chg = trial.suggest_float("min_pct_chg", 5.0, 20.0, step=1.0)
    top_n = trial.suggest_int("top_n", 1, 5)
    scan_interval_hours = trial.suggest_int("scan_interval_hours", 1, 8)
    signal_cooldown_hours = trial.suggest_int("signal_cooldown_hours", 4, 48, step=4)
    max_hold_days = trial.suggest_int("max_hold_days", 1, 15)
    candidate_rank_mode = trial.suggest_categorical(
        "candidate_rank_mode", ["sr", "pct_log_sr"]
    )

    cfg = dataclasses.replace(
        base_cfg,
        raw_min_sell_surge=raw_min_sell_surge,
        min_pct_chg=min_pct_chg,
        top_n=top_n,
        scan_interval_hours=scan_interval_hours,
        signal_cooldown_hours=signal_cooldown_hours,
        max_hold_days=max_hold_days,
        candidate_rank_mode=candidate_rank_mode,
        **_sizing_fields(position_sizing_mode, fixed_invest_usd),
    )
    try:
        return r24_score(_run_trial_raw_surge(cfg, start, end, feed, initial_capital))
    except Exception as e:
        logger.debug("Trial failed: %s", e)
        return -999.0


def _create_or_load_study(storage: str | None) -> optuna.Study:
    """无 storage 时用内存；有 storage 时持久化并可 ``load_if_exists`` 续跑。"""
    if storage:
        return optuna.create_study(
            direction="maximize",
            study_name=RESULT_PREFIX,
            storage=storage,
            load_if_exists=True,
        )
    return optuna.create_study(direction="maximize", study_name=RESULT_PREFIX)


def _save_best_params(params: dict, score: float) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{RESULT_PREFIX}_best.json"
    data = {"optimizer": RESULT_PREFIX, "score": score, "params": params}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _merge_best_with_baseline(base_cfg: RawSurgeR24Config, best: dict) -> dict:
    """把 best 的 7 参数叠加到 baseline 完整 dict 上，供 export_raw_surge_params_json 写回。"""
    merged = dataclasses.asdict(base_cfg)
    for k in SEARCH_FIELDS:
        if k in best:
            merged[k] = best[k]
    merged["position_sizing_mode"] = best.get(
        "position_sizing_mode", merged.get("position_sizing_mode")
    )
    if best.get("fixed_invest_usd") is not None:
        merged["fixed_invest_usd"] = best["fixed_invest_usd"]
    return merged


def optimize(
    n_trials: int = 80,
    start: datetime | None = None,
    end: datetime | None = None,
    initial_capital: float = 10_000.0,
    oos_ratio: float = 0.25,
    position_sizing_mode: PositionSizingMode = "free_cash_pct",
    fixed_invest_usd: float | None = None,
    base_config_path: str | None = None,
    export_config_path: Path | None = None,
    workers: int | None = None,
    storage: str | None = None,
) -> optuna.Study:
    """在 train 段优化；对 best 在 OOS 段跑一次报告。"""
    if start is None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
    if end is None:
        end = datetime.now(UTC)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    total_delta = end - start
    train_delta = total_delta * (1 - oos_ratio)
    train_end = start + train_delta
    oos_start = train_end

    db = get_db()
    db.connect()
    feed = RollingDataFeed(db, workers=workers) if workers is not None else RollingDataFeed(db)

    base_cfg = load_raw_surge_r24_config(base_config_path)
    base_src = get_last_loaded_raw_surge_config_path()

    print(f"\n{'=' * 64}")
    print("  🔬 R24 Raw Surge — Signal Focus Optimizer (7 参数)")
    print(f"  Period: {start.date()} to {end.date()}")
    print(f"  Train:  {start.date()} → {train_end.date()} ({100 * (1 - oos_ratio):.0f}%)")
    print(f"  OOS:    {oos_start.date()} → {end.date()} ({100 * oos_ratio:.0f}%)")
    print(f"  Trials: {n_trials}")
    print(f"  Preload workers: {getattr(feed, '_workers', 'default')}")
    print(f"  Base config: {base_src if base_src else '(defaults — 未找到 JSON)'}")
    print("  ── 搜索空间（7 参数）──")
    print("    raw_min_sell_surge    ∈ [1.0, 20.0]  step 0.5")
    print("    min_pct_chg           ∈ [5.0, 20.0]  step 1.0")
    print("    top_n                 ∈ [1, 5]")
    print("    scan_interval_hours   ∈ [1, 8]")
    print("    signal_cooldown_hours ∈ [4, 48] step 4")
    print("    max_hold_days         ∈ [1, 15]")
    print("    candidate_rank_mode   ∈ {sr, pct_log_sr}")
    print("  ── 锁定（来自 base config）──")
    locked_keys = [
        "rolling_window_hours",
        "max_sr_probe",
        "min_listed_days",
        "tp_initial",
        "sl_threshold",
        "tp_reduced",
        "tp_hours_threshold",
        "trailing_activation_pct",
        "trailing_distance_pct",
        "enable_trailing_stop",
        "enable_add_position",
        "add_position_threshold",
        "tp_after_add",
        "leverage",
        "max_positions",
        "position_size_ratio",
    ]
    base_dict = dataclasses.asdict(base_cfg)
    for k in locked_keys:
        if k in base_dict:
            print(f"    {k:<28s} = {base_dict[k]}")
    if position_sizing_mode == "fixed_usd":
        print(f"  Sizing: fixed_usd  fixed_invest_usd={fixed_invest_usd}")
    else:
        print(f"  Sizing: {position_sizing_mode}")
    if storage:
        print(f"  Optuna storage (resume OK): {storage}")
    else:
        print("  Optuna storage: in-memory (interrupt = lose trials; use --storage sqlite:///... to persist)")
    print(f"{'=' * 64}\n")

    study = _create_or_load_study(storage)
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    t0 = time.perf_counter()
    study.optimize(
        lambda trial: _signal_objective(
            trial,
            start,
            train_end,
            feed,
            initial_capital,
            base_cfg,
            position_sizing_mode,
            fixed_invest_usd,
        ),
        n_trials=n_trials,
        show_progress_bar=True,
    )
    elapsed = time.perf_counter() - t0

    best = dict(study.best_params)
    best_score = study.best_value
    best["position_sizing_mode"] = position_sizing_mode
    best["fixed_invest_usd"] = fixed_invest_usd

    saved = _save_best_params(best, best_score)
    if export_config_path is not None:
        merged = _merge_best_with_baseline(base_cfg, best)
        out = export_raw_surge_params_json(merged, Path(export_config_path))
        print(f"  📄 Exported merged params: {out}")

    print(f"\n{'=' * 64}")
    print("  ✅ Signal Focus Optimizer Complete")
    print(f"  Best Score (train): {best_score:.2f}")
    print(f"  candidate_rank_mode: {best.get('candidate_rank_mode', '—')}")
    print(f"  Time: {elapsed:.1f}s ({elapsed / max(1, n_trials):.1f}s/trial)")
    print(f"  Saved: {saved}")
    print(f"{'─' * 64}")
    print("  🏆 Best Parameters (searched):")
    for k in SEARCH_FIELDS:
        if k in best:
            print(f"    {k:<25s} = {best[k]}")
    print(f"{'=' * 64}")

    if oos_ratio > 0 and (oos_start < end):
        try:
            cfg_oos = dataclasses.replace(
                base_cfg,
                raw_min_sell_surge=best["raw_min_sell_surge"],
                min_pct_chg=best["min_pct_chg"],
                top_n=best["top_n"],
                scan_interval_hours=best["scan_interval_hours"],
                signal_cooldown_hours=best["signal_cooldown_hours"],
                max_hold_days=best["max_hold_days"],
                candidate_rank_mode=best["candidate_rank_mode"],
                **_sizing_fields(position_sizing_mode, fixed_invest_usd),
            )
            oos_result = _run_trial_raw_surge(
                cfg_oos, oos_start, end, feed, initial_capital
            )
            oos_s = r24_score(oos_result)
            print(f"\n  📊 OOS Validation ({oos_start.date()} → {end.date()}):")
            print(
                f"     Score: {oos_s:.2f}  |  rank_mode={best.get('candidate_rank_mode', '—')}  |  "
                f"WinRate: {oos_result.win_rate:.1%}  |  DD: {oos_result.max_drawdown_pct:.1f}%  |  "
                f"Trades/mo: {oos_result.trades_per_month:.1f}"
            )
        except Exception as e:
            print(f"\n  ⚠️ OOS run failed: {e}")

    trials_sorted = sorted(
        study.trials,
        key=lambda t: t.value if t.value is not None else -9999,
        reverse=True,
    )
    print("\n  📊 Top 5 Trials:")
    print(f"  {'#':<4s} {'Score':>10s}  Key Params")
    print(f"  {'─' * 56}")
    for i, t in enumerate(trials_sorted[:5]):
        if t.value is None:
            continue
        params_str = ", ".join(f"{k}={v}" for k, v in sorted(t.params.items()))
        print(f"  {i + 1:<4d} {t.value:>10.2f}  {params_str}")

    db.close()
    return study


def main() -> None:
    parser = argparse.ArgumentParser(
        description="R24 Raw Surge 聚焦 7 参数 Optuna 优化（信号发现 + 扫描节奏 + 持仓/冷却）"
    )
    parser.add_argument("--trials", type=int, default=80, help="Optuna trials (default 80)")
    parser.add_argument("--start", default="2025-01-01", help="Start YYYY-MM-DD (UTC)")
    parser.add_argument("--end", default=None, help="End YYYY-MM-DD (UTC)")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--oos-ratio", type=float, default=0.25, help="OOS fraction")
    parser.add_argument(
        "--sizing",
        choices=["free_cash_pct", "equity_pct", "fixed_usd"],
        default="free_cash_pct",
        help="Position sizing mode",
    )
    parser.add_argument(
        "--fixed-invest",
        type=float,
        default=None,
        help="Fixed margin per trade when --sizing fixed_usd",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="RollingDataFeed / raw preload parallel workers (0=default env/CPU cap)",
    )
    parser.add_argument(
        "--base-config",
        default=None,
        metavar="PATH",
        help=(
            "Baseline params JSON for locked fields "
            "(default: same lookup as backtest_rolling_raw_surge.py)"
        ),
    )
    parser.add_argument(
        "--export-config",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            f"Merge best 7 params with baseline and write to PATH "
            f"(use '.' for {DEFAULT_CANONICAL})"
        ),
    )
    parser.add_argument(
        "--storage",
        type=str,
        default=None,
        metavar="URL",
        help=(
            "Optuna RDB URL for resume, e.g. "
            "sqlite:///reports/optimizer/r24_signal_focus_optuna.sqlite3 "
            "(same URL continues study; default in-memory)"
        ),
    )

    args = parser.parse_args()
    if args.sizing == "fixed_usd" and (args.fixed_invest is None or args.fixed_invest <= 0):
        parser.error("--fixed-invest must be positive when --sizing fixed_usd")

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC) if args.end else None

    export_path: Path | None = None
    if args.export_config:
        export_path = (
            DEFAULT_CANONICAL
            if args.export_config.strip() == "."
            else Path(args.export_config)
        )

    workers_kw: int | None = args.workers if args.workers > 0 else None

    optimize(
        n_trials=args.trials,
        start=start,
        end=end,
        initial_capital=args.capital,
        oos_ratio=args.oos_ratio,
        position_sizing_mode=args.sizing,
        fixed_invest_usd=args.fixed_invest,
        base_config_path=args.base_config,
        export_config_path=export_path,
        workers=workers_kw,
        storage=args.storage,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    main()
