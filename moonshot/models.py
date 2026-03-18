"""Core data models for duo-moonshot.

Candle      — OHLCV candle bar
AmplitudeTrade — Trade signal + result record
RunResult   — Outcome of a completed backtest run
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Candle:
    """A single OHLCV candle."""
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def amplitude(self) -> float:
        """(high - low) / low * 100"""
        return (self.high - self.low) / self.low * 100 if self.low > 0 else 0.0

    @property
    def hour_pct(self) -> float:
        """(close - open) / open * 100"""
        return (self.close - self.open) / self.open * 100 if self.open > 0 else 0.0


@dataclass
class AmplitudeTrade:
    """A single trade signal + result.

    Fields are grouped into four lifecycle stages:
        Signal     → initial detection / trigger
        Order      → price levels and sizing intent
        Fill       → actual execution details
        Settlement → outcome after position close
    """

    # ── Signal (信号发出) ────────────────────────────────────────────
    entry_time: datetime          # Signal trigger time
    base_price: float             # Signal base price
    direction: str                # 'long' | 'short'
    level: str                    # Used as symbol (e.g. 'BTCUSDT')

    # ── Order (挂单参数) ────────────────────────────────────────────
    entry_price: float            # Intended entry price
    target_pct: float             # Take-profit %
    target_price: float           # Take-profit price
    leverage: float               # Leverage multiplier
    stop_loss_pct: float = 0.0    # Stop-loss %
    stop_loss_price: float = 0.0  # Stop-loss price

    # ── Fill (成交) ─────────────────────────────────────────────────
    status: str = "pending"       # pending | filled | cancelled
    filled_time: Optional[datetime] = None
    invest_amount: float = 0.0    # Capital deployed
    position_size: float = 0.0    # Position quantity
    capital_before: float = 0.0   # Account balance at fill

    # ── Settlement (结算) ───────────────────────────────────────────
    result: str = "pending"       # pending | success | failed | timeout | cancelled
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    holding_hours: Optional[int] = None
    actual_pct: float = 0.0       # Raw price change %
    profit_pct: float = 0.0       # Leveraged return %
    profit_amount: float = 0.0    # Dollar PnL
    capital_after: float = 0.0    # Account balance after close
    cancel_reason: str = ""

    # ── Moonshot-specific extensions ─────────────────────────────────
    surge_pct: float = 0.0
    has_added_position: bool = False
    add_position_price: Optional[float] = None
    add_position_time: Optional[datetime] = None
    avg_entry_price: Optional[float] = None
    funding_fee_cost: float = 0.0
    entry_premium_avg: Optional[float] = None
    volume_24h: float = 0.0
    signal_price: Optional[float] = None
    entry_reason: str = ""
    entry_account_ratio: Optional[float] = None
    exit_account_ratio: Optional[float] = None
    account_ratio_change: Optional[float] = None
    lowest_price: Optional[float] = None
    _add_position_multiplier: float = 1.0

    # ── Computed Properties ─────────────────────────────────────────

    @property
    def is_win(self) -> bool:
        return self.result in ("success", "tp_initial", "tp_reduced", "tp_after_add")

    @property
    def is_closed(self) -> bool:
        return self.result in (
            "success", "failed", "timeout",
            "tp_initial", "tp_reduced", "tp_after_add",
            "stop_loss", "trailing_stop", "dynamic_ratio_sl",
        )

    @property
    def net_return_pct(self) -> float:
        return self.profit_pct


@dataclass
class RunResult:
    """Outcome of a completed backtest run."""
    # ── Core ──────────────────────────────────────────────────────────
    trades: list[AmplitudeTrade]
    initial_capital: float
    final_capital: float
    total_return_pct: float
    win_rate: float
    total_signals: int
    active_trades: int
    winning_trades: int
    losing_trades: int
    cancelled_trades: int
    avg_holding_hours: float
    max_drawdown_pct: float
    execution_time: float

    # ── Risk-Adjusted ─────────────────────────────────────────────────
    profit_factor: float = 0.0        # total_win_amount / total_loss_amount
    expectancy_pct: float = 0.0       # WR*avg_win + (1-WR)*avg_loss (per close)
    avg_win_pct: float = 0.0          # average profit % of winning trades
    avg_loss_pct: float = 0.0         # average loss % (negative) of losing trades
    rr_ratio: float = 0.0             # |avg_win_pct| / |avg_loss_pct|
    sharpe_ratio: float = 0.0         # per-trade mean/std of returns

    # ── Time Efficiency ───────────────────────────────────────────────
    trades_per_month: float = 0.0     # active_trades / backtest months
    avg_win_hold_h: float = 0.0       # avg holding hours for winning trades
    avg_loss_hold_h: float = 0.0      # avg holding hours for losing trades
    capital_utilization_pct: float = 0.0  # sum(hold_hours) / total_hours * 100
    signal_conversion_pct: float = 0.0   # active / total_signals * 100

    # ── Advanced Metrics ──────────────────────────────────────────────
    trailing_stop_count: int = 0      # trades exited via trailing stop
    added_position_count: int = 0     # times a position was added
    signal_history: list[dict] = field(default_factory=list)

    # ── Drawdown Quality ──────────────────────────────────────────────
    max_consecutive_losses: int = 0   # worst losing streak (count)
    max_consec_loss_pct: float = 0.0  # cumulative profit_pct during worst streak

    # ── Direction Breakdown ───────────────────────────────────────────
    long_trades: int = 0
    short_trades: int = 0
    long_win_rate: float = 0.0
    short_win_rate: float = 0.0

    # ── Level Breakdown ───────────────────────────────────────────────
    level_stats: dict = field(default_factory=dict)

    # ── Fees ──────────────────────────────────────────────────────────
    total_fees_pct: float = 0.0

    # ── Equity Curve ──────────────────────────────────────────────────
    equity_curve: list = field(default_factory=list)

    # ── Ambiguous Bars ────────────────────────────────────────────────
    ambiguous_bars: int = 0


# ── Result helpers ──────────────────────────────────────────────────────────

_TP_RESULTS = frozenset({"success", "tp_initial", "tp_reduced", "tp_after_add"})
_SL_RESULTS = frozenset({"failed", "stop_loss", "dynamic_ratio_sl", "trailing_stop"})


def is_win_result(result: str | None) -> bool:
    return result in _TP_RESULTS


def is_loss_result(result: str | None) -> bool:
    return result in _SL_RESULTS


def derive_exit_reason(result: str | None) -> str:
    """Map trade result to human-readable exit_reason for CSV export."""
    _MAP = {
        "success": "止盈",
        "tp_initial": "止盈(初始目标)",
        "tp_reduced": "止盈(10h降盈)",
        "tp_after_add": "止盈(补仓后)",
        "failed": "止损",
        "stop_loss": "止损",
        "trailing_stop": "追踪止损",
        "dynamic_ratio_sl": "动态止损(多空比)",
        "timeout": "超时平仓",
        "cancelled": "已取消",
    }
    return _MAP.get(result or "", result or "")
