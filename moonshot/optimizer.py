"""Bayesian Optimizer for Moonshot Strategy.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import optuna

from moonshot.account import Account
from moonshot.data_feed import DataFeed
from moonshot.db import get_postgres_db as get_db
from moonshot.models import RunResult
from moonshot.runner import MoonshotRunner
from moonshot.strategy import MoonshotConfig, MoonshotStrategy

logger = logging.getLogger(__name__)

def neutral_score(result: RunResult) -> float:
    """Neutral scoring using RunResult metrics."""
    if result.active_trades < 5: return -999.0

    total_profit = result.final_capital - result.initial_capital
    if total_profit <= 0: return total_profit

    return total_profit * result.win_rate

def _run_with_config(cfg: MoonshotConfig, start: datetime, end: datetime, feed: DataFeed, initial_capital: float = 10_000.0) -> RunResult:
    strategy = MoonshotStrategy(config=cfg)
    account = Account(initial_capital, commission_rate=0.0005, slippage_pct=0.001)
    runner = MoonshotRunner(feed=feed, account=account, strategy=strategy, verbose=False)
    return runner.run(start, end)

def _build_objective(start: datetime, end: datetime, feed: DataFeed, initial_capital: float):
    def objective(trial: optuna.Trial) -> float:
        sl_threshold = trial.suggest_float("sl_threshold", 0.20, 0.50, step=0.02)
        tp_initial = trial.suggest_float("tp_initial", 0.10, 0.40, step=0.02)

        cfg = MoonshotConfig(
            sl_threshold=sl_threshold,
            tp_initial=tp_initial,
            enable_funding_fee=True,
        )

        try:
            result = _run_with_config(cfg, start, end, feed, initial_capital)
            return neutral_score(result)
        except Exception as e:
            logger.debug(f"Trial failed: {e}")
            return -999.0
    return objective

def _apply_params(bp: dict) -> None:
    strategy_file = Path(__file__).parent / "strategy.py"
    content = strategy_file.read_text(encoding="utf-8")
    for param, val in bp.items():
        pattern = rf"^(\s+{param}:\s*\w+\s*=\s*)([^\s#]+)(.*)"
        match = re.search(pattern, content, re.MULTILINE)
        if match:
            content = content.replace(match.group(0), f"{match.group(1)}{val:.2f}{match.group(3)}", 1)
    strategy_file.write_text(content, encoding="utf-8")
    print("  ✏️  Updated strategy.py with best params.")

def optimize(n_trials: int = 50, start: datetime | None = None, end: datetime | None = None):
    if start is None: start = datetime(2025, 1, 1, tzinfo=UTC)
    if end is None: end = datetime.now(UTC)

    db = get_db()
    db.connect()
    feed = DataFeed(db)

    study = optuna.create_study(direction="maximize")
    objective = _build_objective(start, end, feed, 10000.0)

    print(f"🚀 Starting optimization: {n_trials} trials...")
    study.optimize(objective, n_trials=n_trials)

    print(f"✅ Best value: {study.best_value}")
    print(f"🏆 Best params: {study.best_params}")

    db.close()

if __name__ == "__main__":
    optimize()
