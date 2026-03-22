"""Rolling Optimizer — Three-phase Optuna optimizer for Moonshot-R24.

Phase 1: Signal params (window, scan_interval, cooldown, top_n, min_pct_chg)
Phase 2: Position management (TP/SL/trailing/add) — locks Phase 1 best
Phase 3: Full joint search — uses Phase 1+2 as prior

Uses train/OOS split (default 75% train, 25% OOS) to reduce overfitting.
Each trial runs runner.run() which preloads hourly gainers for the trial's
window_hours — no cross-trial cache (different windows need different pct_chg).

Position sizing (aligned with backtest_rolling.py):
    Default: position_sizing_mode=free_cash_pct (剩余现金 × position_size_ratio).
    Use --sizing equity_pct to match the older "总权益 × ratio" behaviour.
    Use --sizing fixed_usd --fixed-invest N for fixed margin per trade.
    Historical JSON from runs before sizing flags were saved may omit these keys;
    paper loads defaults — scores and params are not directly comparable across modes.

Usage:
    python -m moonshot.rolling_optimizer --phase 1 --trials 60
    python -m moonshot.rolling_optimizer --phase 2 --trials 80
    python -m moonshot.rolling_optimizer --phase 3 --trials 100
    python -m moonshot.rolling_optimizer --phase 1 --oos-ratio 0.3
    python -m moonshot.rolling_optimizer --sizing equity_pct --phase 3 --trials 80
    python -m moonshot.rolling_optimizer --sizing fixed_usd --fixed-invest 400 --phase 1
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import optuna

from moonshot.db import get_postgres_db as get_db
from moonshot.rolling_strategy import RollingStrategy, RollingConfig
from moonshot.rolling_data_feed import RollingDataFeed
from moonshot.rolling_runner import RollingRunner
from moonshot.account import Account
from moonshot.models import RunResult
from moonshot.sizing import PositionSizingMode

logger = logging.getLogger(__name__)


def _sizing_fields(
    position_sizing_mode: PositionSizingMode,
    fixed_invest_usd: Optional[float],
) -> dict:
    """RollingConfig kwargs for position sizing (all phases + OOS)."""
    return {
        "position_sizing_mode": position_sizing_mode,
        "fixed_invest_usd": fixed_invest_usd,
    }

# ── Scoring ─────────────────────────────────────────────────────────────────

def r24_score(result: RunResult) -> float:
    """Multi-dimensional scoring: profit × win_rate × frequency, penalised by drawdown."""
    if result.active_trades < 5:
        return -999.0

    net_profit = result.final_capital - result.initial_capital
    if net_profit <= 0:
        return net_profit

    # Base = net profit × win rate
    base = net_profit * result.win_rate

    # Drawdown penalty: starts biting above 20%, cap at 1.0 to avoid negative multiplier
    dd_penalty = min(1.0, max(0, result.max_drawdown_pct - 20) * 0.05)

    # Frequency bonus: more trades/month → higher score (capped at 1.5×)
    freq_bonus = min(result.trades_per_month / 10, 1.5)

    return base * freq_bonus * max(0, 1 - dd_penalty)


# ── Trial runner ────────────────────────────────────────────────────────────

def _run_trial(
    cfg: RollingConfig,
    start: datetime,
    end: datetime,
    feed: RollingDataFeed,
    initial_capital: float = 10_000.0,
) -> RunResult:
    """Run a single backtest. Runner preloads hourly gainers per trial (window-specific)."""
    strategy = RollingStrategy(config=cfg)
    account = Account(initial_capital)
    runner = RollingRunner(feed=feed, account=account, strategy=strategy, verbose=False)
    return runner.run(start, end)


# ── Phase builders ──────────────────────────────────────────────────────────

def _phase1_objective(
    trial: optuna.Trial,
    start: datetime,
    end: datetime,
    feed: RollingDataFeed,
    initial_capital: float,
    position_sizing_mode: PositionSizingMode,
    fixed_invest_usd: Optional[float],
) -> float:
    """Phase 1: Signal quality parameters."""
    cfg = RollingConfig(
        # Search space
        rolling_window_hours=trial.suggest_int("rolling_window_hours", 6, 48, step=6),
        scan_interval_hours=trial.suggest_int("scan_interval_hours", 1, 8, step=1),
        signal_cooldown_hours=trial.suggest_int("signal_cooldown_hours", 4, 48, step=4),
        top_n=trial.suggest_int("top_n", 1, 5),
        min_pct_chg=trial.suggest_float("min_pct_chg", 5.0, 25.0, step=2.5),
        # Defaults for position management
        tp_initial=0.34,
        sl_threshold=0.44,
        enable_funding_fee=True,
        **_sizing_fields(position_sizing_mode, fixed_invest_usd),
    )
    try:
        result = _run_trial(cfg, start, end, feed, initial_capital)
        return r24_score(result)
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
    fixed_invest_usd: Optional[float],
) -> float:
    """Phase 2: Position management parameters (Phase 1 params locked)."""
    cfg = RollingConfig(
        # Locked from Phase 1
        rolling_window_hours=locked_params.get("rolling_window_hours", 24),
        scan_interval_hours=locked_params.get("scan_interval_hours", 1),
        signal_cooldown_hours=locked_params.get("signal_cooldown_hours", 24),
        top_n=locked_params.get("top_n", 3),
        min_pct_chg=locked_params.get("min_pct_chg", 10.0),
        # Search space
        tp_initial=trial.suggest_float("tp_initial", 0.10, 0.40, step=0.02),
        sl_threshold=trial.suggest_float("sl_threshold", 0.20, 0.50, step=0.02),
        tp_reduced=trial.suggest_float("tp_reduced", 0.08, 0.25, step=0.02),
        tp_hours_threshold=trial.suggest_int("tp_hours_threshold", 6, 18, step=2),
        trailing_activation_pct=trial.suggest_float("trailing_activation_pct", 0.08, 0.25, step=0.02),
        trailing_distance_pct=trial.suggest_float("trailing_distance_pct", 0.04, 0.15, step=0.01),
        add_position_threshold=trial.suggest_float("add_position_threshold", 0.20, 0.45, step=0.02),
        tp_after_add=trial.suggest_float("tp_after_add", 0.30, 0.55, step=0.02),
        enable_funding_fee=True,
        **_sizing_fields(position_sizing_mode, fixed_invest_usd),
    )
    try:
        result = _run_trial(cfg, start, end, feed, initial_capital)
        return r24_score(result)
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
    fixed_invest_usd: Optional[float],
) -> float:
    """Phase 3: Full joint search with Phase 1+2 as informed prior."""
    # Use prior best as centre for tighter ranges
    p = prior_params

    cfg = RollingConfig(
        # Signal params (±30% around Phase 1 best)
        rolling_window_hours=trial.suggest_int(
            "rolling_window_hours",
            max(6, p.get("rolling_window_hours", 24) - 12),
            min(48, p.get("rolling_window_hours", 24) + 12),
            step=6,
        ),
        scan_interval_hours=trial.suggest_int(
            "scan_interval_hours",
            max(1, p.get("scan_interval_hours", 1) - 2),
            min(8, p.get("scan_interval_hours", 1) + 2),
            step=1,
        ),
        signal_cooldown_hours=trial.suggest_int(
            "signal_cooldown_hours",
            max(4, p.get("signal_cooldown_hours", 24) - 12),
            min(48, p.get("signal_cooldown_hours", 24) + 12),
            step=4,
        ),
        top_n=trial.suggest_int("top_n", 1, 5),
        min_pct_chg=trial.suggest_float("min_pct_chg", 5.0, 25.0, step=2.5),
        # Position params (±30% around Phase 2 best)
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
        # Advanced params
        leverage=trial.suggest_int("leverage", 1, 3),
        max_positions=trial.suggest_int("max_positions", 3, 10),
        position_size_ratio=trial.suggest_float("position_size_ratio", 0.02, 0.08, step=0.01),
        max_hold_days=trial.suggest_int("max_hold_days", 5, 15, step=2),
        enable_funding_fee=True,
        **_sizing_fields(position_sizing_mode, fixed_invest_usd),
    )
    try:
        result = _run_trial(cfg, start, end, feed, initial_capital)
        return r24_score(result)
    except Exception as e:
        logger.debug("Trial failed: %s", e)
        return -999.0


# ── Orchestration ───────────────────────────────────────────────────────────

RESULTS_DIR = Path("reports") / "optimizer"


def _load_best_params(phase: int) -> dict:
    """Load best params from a previous phase."""
    path = RESULTS_DIR / f"r24_phase{phase}_best.json"
    if path.exists():
        params = json.loads(path.read_text())
        print(f"  📂 Loaded Phase {phase} best: {path.name}")
        return params
    return {}


def _save_best_params(phase: int, params: dict, score: float) -> Path:
    """Save best params to JSON."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"r24_phase{phase}_best.json"
    data = {"phase": phase, "score": score, "params": params}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return path


def optimize(
    phase: int = 1,
    n_trials: int = 60,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    initial_capital: float = 10_000.0,
    oos_ratio: float = 0.25,
    position_sizing_mode: PositionSizingMode = "free_cash_pct",
    fixed_invest_usd: Optional[float] = None,
):
    """Optimize with train/OOS split. Optimize on train; report OOS metrics for best params."""
    if start is None:
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    if end is None:
        end = datetime.now(timezone.utc)

    total_delta = end - start
    train_delta = total_delta * (1 - oos_ratio)
    train_end = start + train_delta
    oos_start = train_end

    db = get_db()
    db.connect()
    feed = RollingDataFeed(db)

    print(f"\n{'='*60}")
    print(f"  🔬 R24 Optimizer — Phase {phase}")
    print(f"  Period: {start.date()} to {end.date()}")
    print(f"  Train:  {start.date()} → {train_end.date()} ({100*(1-oos_ratio):.0f}%)")
    print(f"  OOS:    {oos_start.date()} → {end.date()} ({100*oos_ratio:.0f}%)")
    print(f"  Trials: {n_trials}")
    if position_sizing_mode == "fixed_usd":
        print(f"  Sizing: fixed_usd  fixed_invest_usd={fixed_invest_usd}")
    else:
        print(f"  Sizing: {position_sizing_mode} (position_size_ratio from trial defaults / Phase 3 search)")
    print(f"{'='*60}\n")

    study = optuna.create_study(
        direction="maximize",
        study_name=f"r24_phase{phase}",
    )
    # Suppress Optuna trial logs
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    t0 = time.perf_counter()

    if phase == 1:
        study.optimize(
            lambda trial: _phase1_objective(
                trial, start, train_end, feed, initial_capital, position_sizing_mode, fixed_invest_usd
            ),
            n_trials=n_trials,
            show_progress_bar=True,
        )
    elif phase == 2:
        locked = _load_best_params(1)
        if not locked:
            print("  ⚠️  No Phase 1 results found. Using defaults.")
            locked = {}
        else:
            locked = locked.get("params", locked)
        study.optimize(
            lambda trial: _phase2_objective(
                trial, start, train_end, feed, initial_capital, locked, position_sizing_mode, fixed_invest_usd
            ),
            n_trials=n_trials,
            show_progress_bar=True,
        )
    elif phase == 3:
        p1 = _load_best_params(1)
        p2 = _load_best_params(2)
        prior = {**(p1.get("params", {}) if p1 else {}), **(p2.get("params", {}) if p2 else {})}
        if not prior:
            print("  ⚠️  No prior results found. Running full search with defaults.")
        study.optimize(
            lambda trial: _phase3_objective(
                trial, start, train_end, feed, initial_capital, prior, position_sizing_mode, fixed_invest_usd
            ),
            n_trials=n_trials,
            show_progress_bar=True,
        )
    else:
        print(f"  ❌ Unknown phase: {phase}")
        sys.exit(1)

    elapsed = time.perf_counter() - t0

    # Save and display results (merge sizing into JSON for reproducibility with paper/backtest)
    best = dict(study.best_params)
    best_score = study.best_value
    best["position_sizing_mode"] = position_sizing_mode
    best["fixed_invest_usd"] = fixed_invest_usd
    saved = _save_best_params(phase, best, best_score)

    print(f"\n{'='*60}")
    print(f"  ✅ Phase {phase} Complete")
    print(f"  Best Score (train): {best_score:.2f}")
    print(f"  Time: {elapsed:.1f}s ({elapsed/n_trials:.1f}s/trial)")
    print(f"  Saved: {saved}")
    print(f"{'─'*60}")
    print(f"  🏆 Best Parameters:")
    for k, v in sorted(best.items()):
        print(f"    {k:<30s} = {v}")
    print(f"{'='*60}")

    # OOS validation
    if oos_ratio > 0 and (oos_start < end):
        valid = {f.name for f in dataclasses.fields(RollingConfig)}
        cfg_dict = {k: v for k, v in best.items() if k in valid}
        cfg_dict["position_sizing_mode"] = position_sizing_mode
        cfg_dict["fixed_invest_usd"] = fixed_invest_usd
        cfg = RollingConfig(**cfg_dict)
        try:
            oos_result = _run_trial(cfg, oos_start, end, feed, initial_capital)
            oos_s = r24_score(oos_result)
            print(f"\n  📊 OOS Validation ({oos_start.date()} → {end.date()}):")
            print(f"     Score: {oos_s:.2f}  |  WinRate: {oos_result.win_rate:.1%}  |  DD: {oos_result.max_drawdown_pct:.1f}%  |  Trades/mo: {oos_result.trades_per_month:.1f}")
        except Exception as e:
            print(f"\n  ⚠️ OOS run failed: {e}")

    # Top 5 trials
    trials_sorted = sorted(study.trials, key=lambda t: t.value if t.value is not None else -9999, reverse=True)
    print(f"\n  📊 Top 5 Trials:")
    print(f"  {'#':<4s} {'Score':>10s}  Key Params")
    print(f"  {'─'*56}")
    for i, t in enumerate(trials_sorted[:5]):
        params_str = ", ".join(f"{k}={v}" for k, v in sorted(t.params.items()))
        print(f"  {i+1:<4d} {t.value:>10.2f}  {params_str}")

    db.close()
    return study


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="R24 Rolling Strategy Optimizer")
    parser.add_argument("--phase", type=int, default=1, choices=[1, 2, 3], help="Optimization phase (1/2/3)")
    parser.add_argument("--trials", type=int, default=60, help="Number of Optuna trials")
    parser.add_argument("--start", default="2025-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--oos-ratio", type=float, default=0.25, help="OOS fraction (0.25 = 75%% train, 25%% OOS)")
    parser.add_argument(
        "--sizing",
        choices=["free_cash_pct", "equity_pct", "fixed_usd"],
        default="free_cash_pct",
        help="Position sizing (same as backtest_rolling.py)",
    )
    parser.add_argument(
        "--fixed-invest",
        type=float,
        default=None,
        help="Fixed margin per trade when --sizing fixed_usd",
    )

    args = parser.parse_args()
    if args.sizing == "fixed_usd" and (args.fixed_invest is None or args.fixed_invest <= 0):
        parser.error("--fixed-invest must be a positive number when --sizing fixed_usd")
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.end else None

    optimize(
        phase=args.phase,
        n_trials=args.trials,
        start=start,
        end=end,
        initial_capital=args.capital,
        oos_ratio=args.oos_ratio,
        position_sizing_mode=args.sizing,
        fixed_invest_usd=args.fixed_invest,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    main()
