"""R24 + Raw Surge 三阶段 Optuna 优化（对齐 ``rolling_optimizer``）。

预加载为 ``build_raw_surge_hourly_gainers``（滚动涨幅门槛 + 卖量倍数），执行层为
``R24RawSurgeRunner`` / ``RawSurgeRollingStrategy``。

Phase 1: 原始信号 + 扫描参数（含 ``candidate_rank_mode``、``max_sr_probe``、min_pct_chg、raw_min_sell_surge、window、scan、cooldown、top_n）
Phase 2: 止盈止损 / 追踪 / 补仓（锁定 Phase 1）
Phase 3: 联合搜索（以 Phase 1+2 为先验），含杠杆、仓位数、持仓天数等

用法::

    python -m moonshot.r24_raw_surge_optimizer --phase 1 --trials 60
    python -m moonshot.r24_raw_surge_optimizer --phase 2 --trials 80
    python -m moonshot.r24_raw_surge_optimizer --phase 3 --trials 100
    python -m moonshot.r24_raw_surge_optimizer --phase 3 --export-config .
    python -m moonshot.r24_raw_surge_optimizer --workers 8 --phase 1 --trials 40
    python -m moonshot.r24_raw_surge_optimizer --phase 1 --trials 80 --phase1-rank-mode pct_log_sr

结果写入 ``reports/optimizer/r24_raw_surge_phase{N}_best.json``；
``--export-config .`` 写入 ``config/r24_raw_surge_params.json``（经 ``export_raw_surge_params_json`` 过滤合法字段）。

**断点续跑（Optuna RDB）**：默认 Study 在内存中，**中断后 trial 会丢失**。若传入 ``--storage``（如
``sqlite:///reports/optimizer/r24_optuna.sqlite3``），同一 ``--phase`` 与同一 storage 下再次运行会
**追加** ``--trials`` 个新 trial，不会从头算。若修改 ``--start``/``--end`` 训练区间，请换新文件名或删库，
避免不同数据区间混在同一 study 里。
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

from moonshot.account import Account
from moonshot.db import get_postgres_db as get_db
from moonshot.models import RunResult
from moonshot.r24_raw_surge_config import RawSurgeR24Config
from moonshot.r24_raw_surge_config_load import DEFAULT_CANONICAL, export_raw_surge_params_json
from moonshot.r24_raw_surge_runner import R24RawSurgeRunner
from moonshot.r24_raw_surge_strategy import RawSurgeRollingStrategy
from moonshot.rolling_data_feed import RollingDataFeed
from moonshot.rolling_optimizer import _sizing_fields, r24_score
from moonshot.sizing import PositionSizingMode

logger = logging.getLogger(__name__)

RESULTS_DIR = Path("reports") / "optimizer"
RESULT_PREFIX = "r24_raw_surge"


def _run_trial_raw_surge(
    cfg: RawSurgeR24Config,
    start: datetime,
    end: datetime,
    feed: RollingDataFeed,
    initial_capital: float = 10_000.0,
) -> RunResult:
    strategy = RawSurgeRollingStrategy(config=cfg)
    account = Account(initial_capital)
    runner = R24RawSurgeRunner(feed=feed, account=account, strategy=strategy, verbose=False)
    return runner.run(start, end)


def _phase1_objective(
    trial: optuna.Trial,
    start: datetime,
    end: datetime,
    feed: RollingDataFeed,
    initial_capital: float,
    position_sizing_mode: PositionSizingMode,
    fixed_invest_usd: float | None,
    phase1_rank_mode: str | None = None,
) -> float:
    """``phase1_rank_mode``：``None`` 时在 sr / pct_log_sr 间搜索；``\"sr\"`` / ``\"pct_log_sr\"`` 时固定该模式。"""
    if phase1_rank_mode == "sr":
        rank_choices = ["sr"]
    elif phase1_rank_mode == "pct_log_sr":
        rank_choices = ["pct_log_sr"]
    else:
        rank_choices = ["sr", "pct_log_sr"]
    cfg = RawSurgeR24Config(
        candidate_rank_mode=trial.suggest_categorical("candidate_rank_mode", rank_choices),
        max_sr_probe=trial.suggest_int("max_sr_probe", 20, 120, step=10),
        raw_min_sell_surge=trial.suggest_float("raw_min_sell_surge", 5.0, 20.0, step=2.5),
        rolling_window_hours=trial.suggest_int("rolling_window_hours", 6, 48, step=6),
        scan_interval_hours=trial.suggest_int("scan_interval_hours", 1, 8, step=1),
        signal_cooldown_hours=trial.suggest_int("signal_cooldown_hours", 4, 48, step=4),
        top_n=trial.suggest_int("top_n", 1, 5),
        min_pct_chg=trial.suggest_float("min_pct_chg", 3.0, 15.0, step=2.5),
        tp_initial=0.34,
        sl_threshold=0.44,
        **_sizing_fields(position_sizing_mode, fixed_invest_usd),
    )
    try:
        return r24_score(_run_trial_raw_surge(cfg, start, end, feed, initial_capital))
    except Exception as e:
        logger.debug("Trial failed: %s", e)
        return -999.0


def _phase2_objective(
    trial: optuna.Trial,
    start: datetime,
    end: datetime,
    feed: RollingDataFeed,
    initial_capital: float,
    locked_params: dict,
    position_sizing_mode: PositionSizingMode,
    fixed_invest_usd: float | None,
) -> float:
    lp = locked_params
    cfg = RawSurgeR24Config(
        raw_min_sell_surge=lp.get("raw_min_sell_surge", 10.0),
        rolling_window_hours=lp.get("rolling_window_hours", 24),
        scan_interval_hours=lp.get("scan_interval_hours", 2),
        signal_cooldown_hours=lp.get("signal_cooldown_hours", 8),
        top_n=lp.get("top_n", 5),
        min_pct_chg=lp.get("min_pct_chg", 5.0),
        candidate_rank_mode=str(lp.get("candidate_rank_mode", "sr")),
        max_sr_probe=int(lp.get("max_sr_probe", 50)),
        tp_initial=trial.suggest_float("tp_initial", 0.10, 0.40, step=0.02),
        sl_threshold=trial.suggest_float("sl_threshold", 0.20, 0.50, step=0.02),
        tp_reduced=trial.suggest_float("tp_reduced", 0.08, 0.25, step=0.02),
        tp_hours_threshold=trial.suggest_int("tp_hours_threshold", 6, 18, step=2),
        trailing_activation_pct=trial.suggest_float("trailing_activation_pct", 0.08, 0.25, step=0.02),
        trailing_distance_pct=trial.suggest_float("trailing_distance_pct", 0.04, 0.15, step=0.01),
        add_position_threshold=trial.suggest_float("add_position_threshold", 0.20, 0.45, step=0.02),
        tp_after_add=trial.suggest_float("tp_after_add", 0.30, 0.55, step=0.02),
        **_sizing_fields(position_sizing_mode, fixed_invest_usd),
    )
    try:
        return r24_score(_run_trial_raw_surge(cfg, start, end, feed, initial_capital))
    except Exception as e:
        logger.debug("Trial failed: %s", e)
        return -999.0


def _phase3_objective(
    trial: optuna.Trial,
    start: datetime,
    end: datetime,
    feed: RollingDataFeed,
    initial_capital: float,
    prior_params: dict,
    position_sizing_mode: PositionSizingMode,
    fixed_invest_usd: float | None,
) -> float:
    p = prior_params
    cfg = RawSurgeR24Config(
        candidate_rank_mode=str(p.get("candidate_rank_mode", "sr")),
        max_sr_probe=int(p.get("max_sr_probe", 50)),
        raw_min_sell_surge=trial.suggest_float(
            "raw_min_sell_surge",
            max(5.0, round(p.get("raw_min_sell_surge", 10.0) - 5.0, 2)),
            min(20.0, round(p.get("raw_min_sell_surge", 10.0) + 5.0, 2)),
            step=2.5,
        ),
        rolling_window_hours=trial.suggest_int(
            "rolling_window_hours",
            max(6, p.get("rolling_window_hours", 24) - 12),
            min(48, p.get("rolling_window_hours", 24) + 12),
            step=6,
        ),
        scan_interval_hours=trial.suggest_int(
            "scan_interval_hours",
            max(1, p.get("scan_interval_hours", 2) - 2),
            min(8, p.get("scan_interval_hours", 2) + 2),
            step=1,
        ),
        signal_cooldown_hours=trial.suggest_int(
            "signal_cooldown_hours",
            max(4, p.get("signal_cooldown_hours", 8) - 12),
            min(48, p.get("signal_cooldown_hours", 8) + 12),
            step=4,
        ),
        top_n=trial.suggest_int("top_n", 1, 5),
        min_pct_chg=trial.suggest_float("min_pct_chg", 3.0, 15.0, step=2.5),
        tp_initial=trial.suggest_float(
            "tp_initial",
            max(0.10, round(p.get("tp_initial", 0.34) - 0.10, 2)),
            min(0.40, round(p.get("tp_initial", 0.34) + 0.10, 2)),
            step=0.02,
        ),
        sl_threshold=trial.suggest_float(
            "sl_threshold",
            max(0.20, round(p.get("sl_threshold", 0.44) - 0.10, 2)),
            min(0.50, round(p.get("sl_threshold", 0.44) + 0.10, 2)),
            step=0.02,
        ),
        tp_reduced=trial.suggest_float(
            "tp_reduced",
            max(0.08, round(p.get("tp_reduced", 0.14) - 0.06, 2)),
            min(0.25, round(p.get("tp_reduced", 0.14) + 0.06, 2)),
            step=0.02,
        ),
        tp_hours_threshold=trial.suggest_int(
            "tp_hours_threshold",
            max(6, p.get("tp_hours_threshold", 10) - 4),
            min(18, p.get("tp_hours_threshold", 10) + 4),
            step=2,
        ),
        trailing_activation_pct=trial.suggest_float(
            "trailing_activation_pct",
            max(0.08, round(p.get("trailing_activation_pct", 0.16) - 0.06, 2)),
            min(0.25, round(p.get("trailing_activation_pct", 0.16) + 0.06, 2)),
            step=0.02,
        ),
        trailing_distance_pct=trial.suggest_float(
            "trailing_distance_pct",
            max(0.04, round(p.get("trailing_distance_pct", 0.09) - 0.04, 2)),
            min(0.15, round(p.get("trailing_distance_pct", 0.09) + 0.04, 2)),
            step=0.01,
        ),
        add_position_threshold=trial.suggest_float(
            "add_position_threshold",
            max(0.20, round(p.get("add_position_threshold", 0.36) - 0.10, 2)),
            min(0.45, round(p.get("add_position_threshold", 0.36) + 0.10, 2)),
            step=0.02,
        ),
        tp_after_add=trial.suggest_float(
            "tp_after_add",
            max(0.30, round(p.get("tp_after_add", 0.45) - 0.10, 2)),
            min(0.55, round(p.get("tp_after_add", 0.45) + 0.10, 2)),
            step=0.02,
        ),
        leverage=trial.suggest_int("leverage", 1, 3),
        max_positions=trial.suggest_int("max_positions", 3, 10),
        position_size_ratio=trial.suggest_float("position_size_ratio", 0.02, 0.08, step=0.01),
        max_hold_days=trial.suggest_int("max_hold_days", 5, 15, step=2),
        **_sizing_fields(position_sizing_mode, fixed_invest_usd),
    )
    try:
        return r24_score(_run_trial_raw_surge(cfg, start, end, feed, initial_capital))
    except Exception as e:
        logger.debug("Trial failed: %s", e)
        return -999.0


def _load_best_params(phase: int) -> dict:
    path = RESULTS_DIR / f"{RESULT_PREFIX}_phase{phase}_best.json"
    if path.exists():
        params = json.loads(path.read_text(encoding="utf-8"))
        print(f"  📂 Loaded Phase {phase} best: {path.name}")
        return params
    return {}


def _save_best_params(phase: int, params: dict, score: float) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{RESULT_PREFIX}_phase{phase}_best.json"
    data = {"phase": phase, "score": score, "params": params}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _create_or_load_study(phase: int, storage: str | None) -> optuna.Study:
    """无 storage 时用内存；有 storage 时持久化并可 ``load_if_exists`` 续跑。"""
    name = f"{RESULT_PREFIX}_phase{phase}"
    if storage:
        return optuna.create_study(
            direction="maximize",
            study_name=name,
            storage=storage,
            load_if_exists=True,
        )
    return optuna.create_study(direction="maximize", study_name=name)


def optimize(
    phase: int = 1,
    n_trials: int = 60,
    start: datetime | None = None,
    end: datetime | None = None,
    initial_capital: float = 10_000.0,
    oos_ratio: float = 0.25,
    position_sizing_mode: PositionSizingMode = "free_cash_pct",
    fixed_invest_usd: float | None = None,
    export_config_path: Path | None = None,
    workers: int | None = None,
    phase1_rank_mode: str | None = None,
    storage: str | None = None,
):
    """在 train 段优化；对最佳参数在 OOS 段跑一次报告。"""
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

    _p1_blob = _load_best_params(1)
    _p1_params: dict = _p1_blob.get("params", _p1_blob) if _p1_blob else {}

    print(f"\n{'='*60}")
    print(f"  🔬 R24 Raw Surge Optimizer — Phase {phase}")
    print(f"  Period: {start.date()} to {end.date()}")
    print(f"  Train:  {start.date()} → {train_end.date()} ({100 * (1 - oos_ratio):.0f}%)")
    print(f"  OOS:    {oos_start.date()} → {end.date()} ({100 * oos_ratio:.0f}%)")
    print(f"  Trials: {n_trials}")
    print(f"  Preload workers: {getattr(feed, '_workers', 'default')}")
    if position_sizing_mode == "fixed_usd":
        print(f"  Sizing: fixed_usd  fixed_invest_usd={fixed_invest_usd}")
    else:
        print(f"  Sizing: {position_sizing_mode} (ratio from trial / Phase 3)")
    if phase == 1:
        if phase1_rank_mode:
            print(f"  candidate_rank_mode: {phase1_rank_mode!r} (fixed via --phase1-rank-mode)")
        else:
            print("  candidate_rank_mode: search per trial (sr | pct_log_sr)")
    else:
        _crm_lock = _p1_params.get("candidate_rank_mode")
        print(
            f"  candidate_rank_mode: {_crm_lock!r} (locked from Phase 1 best json)"
            if _crm_lock is not None
            else "  candidate_rank_mode: — (no Phase 1 best json; using strategy defaults)"
        )
    if storage:
        print(f"  Optuna storage (resume OK): {storage}")
    else:
        print("  Optuna storage: in-memory (interrupt = lose trials; use --storage sqlite:///... to persist)")
    print(f"{'='*60}\n")

    study = _create_or_load_study(phase, storage)
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    t0 = time.perf_counter()

    if phase == 1:
        study.optimize(
            lambda trial: _phase1_objective(
                trial,
                start,
                train_end,
                feed,
                initial_capital,
                position_sizing_mode,
                fixed_invest_usd,
                phase1_rank_mode,
            ),
            n_trials=n_trials,
            show_progress_bar=True,
        )
    elif phase == 2:
        locked = _p1_blob
        if not locked:
            print("  ⚠️  No Phase 1 results found. Using defaults.")
            locked_params: dict = {}
        else:
            locked_params = locked.get("params", locked)
        study.optimize(
            lambda trial: _phase2_objective(
                trial,
                start,
                train_end,
                feed,
                initial_capital,
                locked_params,
                position_sizing_mode,
                fixed_invest_usd,
            ),
            n_trials=n_trials,
            show_progress_bar=True,
        )
    elif phase == 3:
        p1 = _p1_blob
        p2 = _load_best_params(2)
        prior = {**(p1.get("params", {}) if p1 else {}), **(p2.get("params", {}) if p2 else {})}
        if not prior:
            print("  ⚠️  No prior results found. Running full search with defaults.")
        study.optimize(
            lambda trial: _phase3_objective(
                trial,
                start,
                train_end,
                feed,
                initial_capital,
                prior,
                position_sizing_mode,
                fixed_invest_usd,
            ),
            n_trials=n_trials,
            show_progress_bar=True,
        )
    else:
        print(f"  ❌ Unknown phase: {phase}")
        sys.exit(1)

    elapsed = time.perf_counter() - t0

    best = dict(study.best_params)
    best_score = study.best_value
    if "candidate_rank_mode" not in best and _p1_params.get("candidate_rank_mode") is not None:
        best["candidate_rank_mode"] = _p1_params["candidate_rank_mode"]
    best["position_sizing_mode"] = position_sizing_mode
    best["fixed_invest_usd"] = fixed_invest_usd
    saved = _save_best_params(phase, best, best_score)
    if export_config_path is not None:
        out = export_raw_surge_params_json(best, Path(export_config_path))
        print(f"  📄 Exported raw-surge params: {out}")

    print(f"\n{'='*60}")
    print(f"  ✅ Phase {phase} Complete (Raw Surge)")
    print(f"  Best Score (train): {best_score:.2f}")
    print(f"  candidate_rank_mode: {best.get('candidate_rank_mode', '—')}")
    print(f"  Time: {elapsed:.1f}s ({elapsed / n_trials:.1f}s/trial)")
    print(f"  Saved: {saved}")
    print(f"{'─'*60}")
    print("  🏆 Best Parameters:")
    for k, v in sorted(best.items()):
        print(f"    {k:<30s} = {v}")
    print(f"{'='*60}")

    valid = {f.name for f in dataclasses.fields(RawSurgeR24Config)}
    if oos_ratio > 0 and (oos_start < end):
        cfg_dict = {k: v for k, v in best.items() if k in valid}
        cfg_dict["position_sizing_mode"] = position_sizing_mode
        cfg_dict["fixed_invest_usd"] = fixed_invest_usd
        cfg = RawSurgeR24Config(**cfg_dict)
        try:
            oos_result = _run_trial_raw_surge(cfg, oos_start, end, feed, initial_capital)
            oos_s = r24_score(oos_result)
            print(f"\n  📊 OOS Validation ({oos_start.date()} → {end.date()}):")
            print(
                f"     Score: {oos_s:.2f}  |  candidate_rank_mode={best.get('candidate_rank_mode', '—')}  |  "
                f"WinRate: {oos_result.win_rate:.1%}  |  "
                f"DD: {oos_result.max_drawdown_pct:.1f}%  |  Trades/mo: {oos_result.trades_per_month:.1f}"
            )
        except Exception as e:
            print(f"\n  ⚠️ OOS run failed: {e}")

    trials_sorted = sorted(
        study.trials, key=lambda t: t.value if t.value is not None else -9999, reverse=True
    )
    print("\n  📊 Top 5 Trials:")
    print(f"  {'#':<4s} {'Score':>10s}  Key Params")
    print(f"  {'─'*56}")
    _crm_for_top = best.get("candidate_rank_mode")
    for i, t in enumerate(trials_sorted[:5]):
        merged = dict(t.params)
        if "candidate_rank_mode" not in merged and _crm_for_top is not None:
            merged["candidate_rank_mode"] = _crm_for_top
        params_str = ", ".join(f"{k}={v}" for k, v in sorted(merged.items()))
        print(f"  {i + 1:<4d} {t.value:>10.2f}  {params_str}")

    db.close()
    return study


def main() -> None:
    parser = argparse.ArgumentParser(description="R24 + Raw Surge strategy optimizer (Optuna)")
    parser.add_argument("--phase", type=int, default=1, choices=[1, 2, 3], help="Phase 1/2/3")
    parser.add_argument("--trials", type=int, default=60, help="Optuna trials")
    parser.add_argument("--start", default="2025-01-01", help="Start YYYY-MM-DD (UTC)")
    parser.add_argument("--end", default=None, help="End YYYY-MM-DD (UTC)")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--oos-ratio", type=float, default=0.25, help="OOS fraction")
    parser.add_argument(
        "--sizing",
        choices=["free_cash_pct", "equity_pct", "fixed_usd"],
        default="free_cash_pct",
        help="Position sizing",
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
        "--export-config",
        type=str,
        default=None,
        metavar="PATH",
        help=f"Write params JSON to PATH (use '.' for {DEFAULT_CANONICAL})",
    )
    parser.add_argument(
        "--phase1-rank-mode",
        choices=["search", "sr", "pct_log_sr"],
        default="search",
        help="Phase 1 only: search both rank modes (default), or fix candidate_rank_mode",
    )
    parser.add_argument(
        "--storage",
        type=str,
        default=None,
        metavar="URL",
        help=(
            "Optuna RDB URL for resume, e.g. sqlite:///reports/optimizer/r24_optuna.sqlite3 "
            "(same --phase + same URL continues study; default in-memory)"
        ),
    )

    args = parser.parse_args()
    if args.sizing == "fixed_usd" and (args.fixed_invest is None or args.fixed_invest <= 0):
        parser.error("--fixed-invest must be positive when --sizing fixed_usd")

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC) if args.end else None

    export_path: Path | None = None
    if args.export_config:
        export_path = DEFAULT_CANONICAL if args.export_config.strip() == "." else Path(args.export_config)

    workers_kw: int | None = args.workers if args.workers > 0 else None

    p1_rm: str | None = None if args.phase1_rank_mode == "search" else args.phase1_rank_mode

    optimize(
        phase=args.phase,
        n_trials=args.trials,
        start=start,
        end=end,
        initial_capital=args.capital,
        oos_ratio=args.oos_ratio,
        position_sizing_mode=args.sizing,
        fixed_invest_usd=args.fixed_invest,
        export_config_path=export_path,
        workers=workers_kw,
        phase1_rank_mode=p1_rm,
        storage=args.storage,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    main()
