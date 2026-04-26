"""Account — capital and PnL management for duo-moonshot.

Tracks running capital, margin, and settles trades using bt8-style accounting.
Includes commission + slippage fee modeling matching engine_v2.
"""

from __future__ import annotations

import bisect
from datetime import datetime

from moonshot.models import AmplitudeTrade

# Default fee assumptions (Binance/OKX contract tier)
DEFAULT_COMMISSION_RATE = 0.0005   # 0.05% per side (maker/taker average)
DEFAULT_SLIPPAGE_PCT    = 0.0005   # 0.05% market impact per side


class Account:
    """Manages capital, positions, fees, and PnL settlement."""

    def __init__(
        self,
        initial_capital: float,
        commission_rate: float = DEFAULT_COMMISSION_RATE,
        slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    ) -> None:
        self._initial_capital = initial_capital
        self._running_capital = initial_capital
        self._open_margin = 0.0
        self._commission_rate = commission_rate
        self._slippage_pct = slippage_pct

        # Accumulated fees (absolute USD)
        self._total_fees_usd: float = 0.0

        # Ledger: snapshots for capital_at(entry_time) lookup
        self._capital_snapshots: list[tuple[datetime, float]] = []
        self._snapshot_times: list[datetime] = []

        # Performance tracking
        self._equity_curve: list[tuple[datetime, float]] = []

    @property
    def initial_capital(self) -> float: return self._initial_capital
    @property
    def final_capital(self) -> float: return self._running_capital
    @property
    def equity_curve(self) -> list[tuple[datetime, float]]: return self._equity_curve

    @property
    def total_fees_usd(self) -> float:
        """Total fees paid (commission + slippage) across all settled trades."""
        return self._total_fees_usd

    @property
    def total_fees_pct(self) -> float:
        """Total fees as % of initial capital."""
        if self._initial_capital == 0:
            return 0.0
        return self._total_fees_usd / self._initial_capital * 100

    def capital_at(self, entry_time: datetime) -> float:
        """Available capital at entry_time (binary search)."""
        if not self._snapshot_times: return self._initial_capital
        idx = bisect.bisect_right(self._snapshot_times, entry_time) - 1
        return self._capital_snapshots[idx][1] if idx >= 0 else self._initial_capital

    def open_position_bt8(self, trade: AmplitudeTrade, invest_amount: float, leverage: float) -> None:
        """Lock margin and record entry."""
        cap = self.capital_at(trade.entry_time)
        trade.capital_before = cap
        trade.invest_amount = invest_amount
        trade.position_size = invest_amount / trade.entry_price
        trade.leverage = leverage

        self._running_capital -= invest_amount
        self._open_margin += invest_amount
        self._record_snapshot(trade.entry_time)

    def close_position_bt8(self, trade: AmplitudeTrade, result: str, exit_price: float, exit_time: datetime, holding_hours: int) -> None:
        """Settle trade and release margin + profit (with fee deduction)."""
        actual_pct = (trade.entry_price - exit_price) / trade.entry_price * 100
        leveraged_pct = actual_pct * trade.leverage
        gross_profit = trade.invest_amount * leveraged_pct / 100

        # Round-trip fee: entry + exit, each side commission + slippage
        fee_rate_rt = (self._commission_rate + self._slippage_pct) * 2
        fees = trade.invest_amount * fee_rate_rt
        self._total_fees_usd += fees

        net_profit = gross_profit - (trade.funding_fee_cost or 0.0) - fees

        trade.result, trade.exit_price, trade.exit_time, trade.holding_hours = result, exit_price, exit_time, holding_hours
        trade.actual_pct, trade.profit_pct, trade.profit_amount = actual_pct, leveraged_pct, net_profit

        self._running_capital += trade.invest_amount + net_profit
        self._open_margin -= trade.invest_amount
        self._record_snapshot(exit_time)
        trade.capital_after = self._running_capital

    def add_position_bt8(self, trade: AmplitudeTrade, add_price: float, add_time: datetime, multiplier: float = 1.0) -> None:
        """Double down on position."""
        original_size = trade.position_size
        add_size = original_size * multiplier
        total_size = original_size + add_size

        new_avg = (trade.entry_price * original_size + add_price * add_size) / total_size
        add_margin = add_size * add_price

        trade.avg_entry_price = trade.entry_price = new_avg
        # 同步「硬止损价」到最新平均建仓价，避免 CSV/verify 用旧止损价导致误判。
        # trade.stop_loss_pct 在 Runner 初始化时已按百分比设置（例如 16.0 表示 16%）。
        if trade.stop_loss_pct is not None:
            trade.stop_loss_price = trade.entry_price * (1 + trade.stop_loss_pct / 100)
        trade.position_size = total_size
        trade.invest_amount += add_margin
        trade.has_added_position, trade.add_position_price, trade.add_position_time = True, add_price, add_time
        self._running_capital -= add_margin
        self._open_margin += add_margin
        self._record_snapshot(add_time)

    def _record_snapshot(self, dt: datetime) -> None:
        self._capital_snapshots.append((dt, self._running_capital))
        self._snapshot_times.append(dt)
        self._equity_curve.append((dt, self._running_capital + self._open_margin))

    def max_drawdown_pct(self) -> float:
        if not self._equity_curve: return 0.0
        peak = self._initial_capital; max_dd = 0.0
        for _, eq in self._equity_curve:
            if eq > peak: peak = eq
            dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
            if dd > max_dd: max_dd = dd
        return max_dd
