"""Standalone config for R24 + raw surge (no dependency on ``RollingConfig``).

运行时参数请用 JSON 加载：``load_raw_surge_r24_config``（默认读取 ``config/r24_raw_surge_params.json``）。
字段与原先继承 ``RollingConfig`` 时保持一致，便于现有 JSON 与优化结果继续可用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from moonshot.sizing import PositionSizingMode


@dataclass
class RawSurgeR24Config:
    """执行层参数 + 原始信号层（raw_*）：滚动涨幅门槛 + 卖量倍数严格大于阈值。"""

    # ── 1. Signal Generation ─────────────────────────────────────────
    top_n: int = 5
    min_pct_chg: float = 5.0
    min_listed_days: int = 10
    signal_cooldown_hours: int = 8
    rolling_window_hours: int = 24
    scan_interval_hours: int = 2
    # 定时扫描在整点后延迟（分钟），避免“刚到 00:00”上一小时 K 线/成交量未落盘。
    scan_delay_minutes: int = 1

    # 是否允许手动触发扫描（CLI `scan` / API `POST /scan`）。
    manual_scan_enabled: bool = True

    # ── 2. Entry Filters ─────────────────────────────────────────────
    enable_sell_surge_gate: bool = False
    sell_surge_threshold: float = 10.0
    sell_surge_max: float = 1e12


    # ── 3. Position Management ───────────────────────────────────────
    position_sizing_mode: PositionSizingMode = "free_cash_pct"
    position_size_ratio: float = 0.08
    fixed_invest_usd: float | None = None
    max_positions: int = 7
    leverage: int = 3
    max_hold_days: int = 3
    enable_funding_fee: bool = True

    tp_initial: float = 0.16
    tp_reduced: float = 0.08
    tp_hours_threshold: int = 6
    tp_after_add: float = 0.45

    sl_threshold: float = 0.42

    enable_trailing_stop: bool = True
    trailing_activation_pct: float = 0.08
    trailing_distance_pct: float = 0.04


    enable_add_position: bool = False
    add_position_threshold: float = 0.2
    add_position_multiplier: float = 1.0

    # ── Raw signal preload (scan_hourly_rolling + sell surge) ────────
    raw_min_sell_surge: float = 10.0
    # 原始信号层 sr（sell_surge_ratio）上限；None 表示不设上限。
    # 主要用于剔除极端异常倍数（例如昨日基线过小导致的巨大倍数）。
    raw_max_sell_surge: float | None = None
    raw_max_signals_per_hour: int | None = None
    # 与 duo-live ``rolling_scanner`` / ``docs/signal-scan-order.md`` 对齐：
    # 涨幅合格 → 按涨幅截 max_sr_probe → 算 sr → sr 门 → 按 candidate_rank_mode 截 top_n。
    candidate_rank_mode: str = "pct_log_sr"  # "sr" | "pct_log_sr"
    max_sr_probe: int = 50

    # ── Paper realism knobs (optional) ───────────────────────────────
    # Slippage in basis points (bps). Short entry/add: worse fill = lower price; exit: worse fill = higher price.
    entry_slippage_bps: float = 0.0
    exit_slippage_bps: float = 0.0
    liquidation_slippage_bps: float = 0.0

    # Fee rates (fraction). Use taker by default for market-like fills.
    maker_fee_rate: float = 0.0002
    taker_fee_rate: float = 0.0005

    # Maintenance margin approximation: trigger liquidation when (margin + unrealized_pnl) <= margin * maintenance_margin_rate
    maintenance_margin_rate: float = 0.0

    # Dynamic ratio stop loss
    enable_dynamic_ratio_sl: bool = False
    ratio_change_threshold: float = -0.18
    ratio_data_start: str = "2025-12-12"
