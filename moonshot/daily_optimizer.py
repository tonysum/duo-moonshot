"""Daily (Moonshot) Optimizer — three-phase Optuna search, aligned with ``rolling_optimizer``.

Phase 1: Signal params (top_n, min_pct_chg, min_listed_days)
Phase 2: Position management (TP/SL/trailing/add) — locks Phase 1 best
Phase 3: Full joint search around Phase 1+2 priors (+ leverage, max_positions, ratio, max_hold_days)

Train / OOS split (default 75%% train, 25%% OOS). Scoring uses the same multi-metric
function as R24 (``r24_score``) for comparable objective.

Position sizing flags match ``backtest.py`` / paper (``--sizing``, ``--fixed-invest``).

Usage:
    python -m moonshot.daily_optimizer --phase 1 --trials 60
    python -m moonshot.daily_optimizer --phase 2 --trials 80
    python -m moonshot.daily_optimizer --phase 3 --trials 100
    python -m moonshot.daily_optimizer --phase 3 --export-config .
    python -m moonshot.daily_optimizer --phase 1 --config config/moonshot_params.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import optuna

from moonshot.account import Account
from moonshot.data_feed import DataFeed
from moonshot.db import get_postgres_db as get_db
from moonshot.models import RunResult
from moonshot.moonshot_config_load import (
    DEFAULT_CANONICAL,
    export_moonshot_flat_params_json,
    load_moonshot_config,
)
from moonshot.runner import MoonshotRunner
from moonshot.rolling_optimizer import r24_score
from moonshot.strategy import MoonshotConfig, MoonshotStrategy
from moonshot.sizing import PositionSizingMode

logger = logging.getLogger(__name__)

RESULTS_DIR = Path("reports") / "optimizer"


def _sizing_fields(
    position_sizing_mode: PositionSizingMode,
    fixed_invest_usd: float | None,
) -> dict:
    return {
        "position_sizing_mode": position_sizing_mode,
        "fixed_invest_usd": fixed_invest_usd,
    }


def _run_trial(
    cfg: MoonshotConfig,
    start: datetime,
    end: datetime,
    feed: DataFeed,
    initial_capital: float = 10_000.0,
) -> RunResult:
    strategy = MoonshotStrategy(config=cfg)
    account = Account(initial_capital, commission_rate=0.0005, slippage_pct=0.001)
    runner = MoonshotRunner(feed=feed, account=account, strategy=strategy, verbose=False)
    return runner.run(start, end)


def _phase1_objective(
    trial: optuna.Trial,
    base: MoonshotConfig,
    start: datetime,
    end: datetime,
    feed: DataFeed,
    initial_capital: float,
    position_sizing_mode: PositionSizingMode,
    fixed_invest_usd: float | None,
) -> float:
    cfg = replace(
        base,
        top_n=trial.suggest_int("top_n", 1, 5),
        min_pct_chg=trial.suggest_float("min_pct_chg", 5.0, 25.0, step=2.5),
        min_listed_days=trial.suggest_int("min_listed_days", 0, 30, step=5),
        tp_initial=0.34,
        sl_threshold=0.44,
        enable_funding_fee=True,
        **_sizing_fields(position_sizing_mode, fixed_invest_usd),
    )
    try:
        return r24_score(_run_trial(cfg, start, end, feed, initial_capital))
    except Exception as e:
        logger.debug("Trial failed: %s", e)
        return -999.0


def _phase2_objective(
    trial: optuna.Trial,
    base: MoonshotConfig,
    start: datetime,
    end: datetime,
    feed: DataFeed,
    initial_capital: float,
    locked: dict,
    position_sizing_mode: PositionSizingMode,
    fixed_invest_usd: float | None,
) -> float:
    cfg = replace(
        base,
        top_n=int(locked.get("top_n", base.top_n)),
        min_pct_chg=float(locked.get("min_pct_chg", base.min_pct_chg)),
        min_listed_days=int(locked.get("min_listed_days", base.min_listed_days)),
        tp_initial=trial.suggest_float("tp_initial", 0.10, 0.40, step=0.02),
        sl_threshold=trial.suggest_float("sl_threshold", 0.20, 0.50, step=0.02),
        tp_reduced=trial.suggest_float("tp_reduced", 0.08, 0.25, step=0.02),
        tp_hours_threshold=trial.suggest_int("tp_hours_threshold", 6, 18, step=2),
        trailing_activation_pct=trial.suggest_float(
            "trailing_activation_pct", 0.08, 0.25, step=0.02
        ),
        trailing_distance_pct=trial.suggest_float(
            "trailing_distance_pct", 0.04, 0.15, step=0.01
        ),
        add_position_threshold=trial.suggest_float(
            "add_position_threshold", 0.20, 0.45, step=0.02
        ),
        tp_after_add=trial.suggest_float("tp_after_add", 0.30, 0.55, step=0.02),
        enable_funding_fee=True,
        **_sizing_fields(position_sizing_mode, fixed_invest_usd),
    )
    try:
        return r24_score(_run_trial(cfg, start, end, feed, initial_capital))
    except Exception as e:
        logger.debug("Trial failed: %s", e)
        return -999.0


def _phase3_objective(
    trial: optuna.Trial,
    base: MoonshotConfig,
    start: datetime,
    end: datetime,
    feed: DataFeed,
    initial_capital: float,
    prior: dict,
    position_sizing_mode: PositionSizingMode,
    fixed_invest_usd: float | None,
) -> float:
    p = prior

    cfg = replace(
        base,
        top_n=trial.suggest_int(
            "top_n",
            max(1, int(p.get("top_n", 3)) - 1),
            min(5, int(p.get("top_n", 3)) + 1),
        ),
        min_pct_chg=trial.suggest_float("min_pct_chg", 5.0, 25.0, step=2.5),
        min_listed_days=trial.suggest_int(
            "min_listed_days",
            max(0, int(p.get("min_listed_days", 10)) - 10),
            min(30, int(p.get("min_listed_days", 10)) + 10),
            step=5,
        ),
        tp_initial=trial.suggest_float(
            "tp_initial",
            max(0.10, round(float(p.get("tp_initial", 0.34)) - 0.10, 2)),
            min(0.40, round(float(p.get("tp_initial", 0.34)) + 0.10, 2)),
            step=0.02,
        ),
        sl_threshold=trial.suggest_float(
            "sl_threshold",
            max(0.20, round(float(p.get("sl_threshold", 0.44)) - 0.10, 2)),
            min(0.50, round(float(p.get("sl_threshold", 0.44)) + 0.10, 2)),
            step=0.02,
        ),
        tp_reduced=trial.suggest_float(
            "tp_reduced",
            max(0.08, round(float(p.get("tp_reduced", 0.14)) - 0.06, 2)),
            min(0.25, round(float(p.get("tp_reduced", 0.14)) + 0.06, 2)),
            step=0.02,
        ),
        tp_hours_threshold=trial.suggest_int(
            "tp_hours_threshold",
            max(6, int(p.get("tp_hours_threshold", 10)) - 4),
            min(18, int(p.get("tp_hours_threshold", 10)) + 4),
            step=2,
        ),
        trailing_activation_pct=trial.suggest_float(
            "trailing_activation_pct",
            max(     
                0.08,
                round(float(p.get("trailing_activation_pct", 0.16)) - 0.06, 2),
            ),
            min(
                0.25,
                round(float(p.get("trailing_activation_pct", 0.16)) + 0.06, 2),
            ),
            step=0.02,
        ),
        trailing_distance_pct=trial.suggest_float(
            "trailing_distance_pct",
            max(     
                0.04,
                round(float(p.get("trailing_distance_pct", 0.09)) - 0.04, 2),
            ),
            min(
                0.15,
                round(float(p.get("trailing_distance_pct", 0.09)) + 0.04, 2),
            ),
            step=0.01,
        ),
        add_position_threshold=trial.suggest_float(
            "add_position_threshold",
            max(
                0.20,
                round(float(p.get("add_position_threshold", 0.36)) - 0.10, 2),
            ),
            min(
                0.45,
                round(float(p.get("add_position_threshold", 0.36)) + 0.10, 2),
            ),
            step=0.02,
        ),
        tp_after_add=trial.suggest_float(
            "tp_after_add",
            max(0.30, round(float(p.get("tp_after_add", 0.45)) - 0.10, 2)),
            min(0.55, round(float(p.get("tp_after_add", 0.45)) + 0.10, 2)),
            step=0.02,
        ),
        leverage=trial.suggest_int("leverage", 1, 3),
        max_positions=trial.suggest_int("max_positions", 3, 10),
        position_size_ratio=trial.suggest_float(
            "position_size_ratio", 0.02, 0.08, step=0.01
        ),
        max_hold_days=trial.suggest_int("max_hold_days", 5, 15, step=2),
        enable_funding_fee=True,
        **_sizing_fields(position_sizing_mode, fixed_invest_usd),
    )
    try:
        return r24_score(_run_trial(cfg, start, end, feed, initial_capital))
    except Exception as e:
        logger.debug("Trial failed: %s", e)
        return -999.0


def _load_best_params(phase: int) -> dict:
    path = RESULTS_DIR / f"moonshot_phase{phase}_best.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"  📂 Loaded Phase {phase} best: {path.name}")
        return data
    return {}


def _save_best_params(phase: int, params: dict, score: float) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"moonshot_phase{phase}_best.json"
    payload = {"phase": phase, "score": score, "params": params}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


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
    baseline_config_path: Path | str | None = None,
):
    """Optimize on train window; report OOS metrics for best params."""
    if start is None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
    if end is None:
        end = datetime.now(UTC)

    base = load_moonshot_config(baseline_config_path)

    total_delta = end - start
    train_delta = total_delta * (1 - oos_ratio)
    train_end = start + train_delta
    oos_start = train_end

    db = get_db()
    db.connect()
    feed = DataFeed(db)

    print(f"\n{'='*60}")
    print(f"  🌙 Daily (Moonshot) Optimizer — Phase {phase}")
    print(f"  Period: {start.date()} to {end.date()}")
    print(f"  Train:  {start.date()} → {train_end.date()} ({100 * (1 - oos_ratio):.0f}%)")
    print(f"  OOS:    {oos_start.date()} → {end.date()} ({100 * oos_ratio:.0f}%)")
    print(f"  Trials: {n_trials}")
    if position_sizing_mode == "fixed_usd":
        print(f"  Sizing: fixed_usd  fixed_invest_usd={fixed_invest_usd}")
    else:
        print(f"  Sizing: {position_sizing_mode}")
    print(f"{'='*60}\n")

    study = optuna.create_study(direction="maximize", study_name=f"moonshot_phase{phase}")
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    t0 = time.perf_counter()

    if phase == 1:
        study.optimize(
            lambda trial: _phase1_objective(
                trial,
                base,
                start,
                train_end,
                feed,
                initial_capital,
                position_sizing_mode,
                fixed_invest_usd,
            ),
            n_trials=n_trials,
            show_progress_bar=True,
        )
    elif phase == 2:
        locked_raw = _load_best_params(1)
        locked = locked_raw.get("params", locked_raw) if locked_raw else {}
        if not locked:
            print("  ⚠️  No Phase 1 results found. Using baseline JSON only for signal params.")
        study.optimize(
            lambda trial: _phase2_objective(
                trial,
                base,
                start,
                train_end,
                feed,
                initial_capital,
                locked,
                position_sizing_mode,
                fixed_invest_usd,
            ),
            n_trials=n_trials,
            show_progress_bar=True,
        )
    elif phase == 3:
        p1 = _load_best_params(1)
        p2 = _load_best_params(2)
        prior = {
            **(p1.get("params", {}) if p1 else {}),
            **(p2.get("params", {}) if p2 else {}),
        }
        if not prior:
            print("  ⚠️  No prior phase JSON; search uses baseline + wide priors.")
        study.optimize(
            lambda trial: _phase3_objective(
                trial,
                base,
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
    best["position_sizing_mode"] = position_sizing_mode
    best["fixed_invest_usd"] = fixed_invest_usd

    saved = _save_best_params(phase, best, best_score)

    if export_config_path is not None:
        out = export_moonshot_flat_params_json(best, Path(export_config_path))
        print(f"  📄 Exported canonical params: {out}")

    print(f"\n{'='*60}")
    print(f"  ✅ Phase {phase} Complete")
    print(f"  Best Score (train): {best_score:.2f}")
    print(f"  Time: {elapsed:.1f}s ({elapsed / n_trials:.1f}s/trial)")
    print(f"  Saved: {saved}")
    print(f"{'─'*60}")
    print("  🏆 Best Parameters:")
    for k, v in sorted(best.items()):
        print(f"    {k:<30s} = {v}")
    print(f"{'='*60}")

    if oos_ratio > 0 and oos_start < end:
        valid = {f.name for f in dataclasses.fields(MoonshotConfig)}
        cfg_dict = {k: v for k, v in best.items() if k in valid}
        cfg_dict["position_sizing_mode"] = position_sizing_mode
        cfg_dict["fixed_invest_usd"] = fixed_invest_usd
        cfg = MoonshotConfig(**cfg_dict)
        try:
            oos_result = _run_trial(cfg, oos_start, end, feed, initial_capital)
            oos_s = r24_score(oos_result)
            print(f"\n  📊 OOS Validation ({oos_start.date()} → {end.date()}):")
            print(
                f"     Score: {oos_s:.2f}  |  WinRate: {oos_result.win_rate:.1%}  "
                f"|  DD: {oos_result.max_drawdown_pct:.1f}%  |  Trades/mo: {oos_result.trades_per_month:.1f}"
            )
        except Exception as e:
            print(f"\n  ⚠️ OOS run failed: {e}")

    trials_sorted = sorted(
        study.trials, key=lambda t: t.value if t.value is not None else -9999, reverse=True
    )
    print("\n  📊 Top 5 Trials:")
    print(f"  {'#':<4s} {'Score':>10s}  Key Params")
    print(f"  {'─'*56}")
    for i, t in enumerate(trials_sorted[:5]):
        ps = ", ".join(f"{k}={v}" for k, v in sorted(t.params.items()))
        print(f"  {i + 1:<4d} {t.value:>10.2f}  {ps}")

    db.close()
    return study


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily Moonshot Optimizer (3-phase, Optuna)")
    parser.add_argument("--phase", type=int, default=1, choices=[1, 2, 3])
    parser.add_argument("--trials", type=int, default=60)
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument("--oos-ratio", type=float, default=0.25)
    parser.add_argument(
        "--sizing",
        choices=["free_cash_pct", "equity_pct", "fixed_usd"],
        default="free_cash_pct",
    )
    parser.add_argument("--fixed-invest", type=float, default=None)
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Baseline Moonshot JSON (merged under trial params); default search if omitted",
    )
    parser.add_argument(
        "--export-config",
        default=None,
        metavar="PATH",
        help=f"Write params dict to PATH after this phase (use '.' for {DEFAULT_CANONICAL})",
    )

    args = parser.parse_args()
    if args.sizing == "fixed_usd" and (args.fixed_invest is None or args.fixed_invest <= 0):
        parser.error("--fixed-invest must be positive when --sizing fixed_usd")

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
    end = (
        datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC)
        if args.end
        else datetime.now(UTC)
    )

    export_path: Path | None = None
    if args.export_config:
        export_path = DEFAULT_CANONICAL if args.export_config.strip() == "." else Path(args.export_config)

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
        baseline_config_path=args.config,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    main()
