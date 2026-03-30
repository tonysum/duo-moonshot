"""Position sizing helpers for backtest and paper trading."""

from __future__ import annotations

from typing import Literal

PositionSizingMode = Literal["free_cash_pct", "equity_pct", "fixed_usd"]


def compute_order_margin(
    *,
    free_cash: float,
    total_equity: float,
    mode: PositionSizingMode,
    position_size_ratio: float,
    fixed_invest_usd: float | None,
) -> float:
    """USD margin for the next open; never exceeds ``free_cash``.

    - ``free_cash_pct``: ``free_cash * position_size_ratio``
    - ``equity_pct``: ``total_equity * position_size_ratio``, capped by ``free_cash``
    - ``fixed_usd``: ``min(fixed_invest_usd, free_cash)`` if ``fixed_invest_usd`` > 0
    """
    fc = max(0.0, float(free_cash))
    te = max(0.0, float(total_equity))
    if fc <= 0:
        return 0.0

    if mode == "fixed_usd":
        fix = fixed_invest_usd
        if fix is None or fix <= 0:
            return 0.0
        return min(float(fix), fc)

    if mode == "equity_pct":
        target = te * position_size_ratio
        return min(max(0.0, target), fc)

    # free_cash_pct
    return min(max(0.0, fc * position_size_ratio), fc)
