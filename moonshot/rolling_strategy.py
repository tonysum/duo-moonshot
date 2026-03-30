"""RollingStrategy (Moonshot-R24) — 24h rolling top gainer short with hourly scanning.

Independent strategy that scans every hour using K1h-based 24h rolling change,
instead of daily K1d change. Shares position management / exit logic with Moonshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from moonshot.data_feed import DataFeed
from moonshot.models import AmplitudeTrade
from moonshot.sizing import PositionSizingMode
from moonshot.sizing import compute_order_margin as _compute_order_margin

logger = logging.getLogger(__name__)


@dataclass
class RollingConfig:
    """Strategy configuration for Moonshot-R24 (24h rolling)."""

    # ── 1. Signal Generation ─────────────────────────────────────────
    top_n: int = 5                        # 每次扫描取涨幅前 N 名
    min_pct_chg: float = 5.0             # 最小涨幅要求 5%
    min_listed_days: int = 10             # 新币过滤
    signal_cooldown_hours: int = 8       # 同币种信号冷却期(小时), 可调
    rolling_window_hours: int = 42        # 滚动窗口大小(小时), 可调
    scan_interval_hours: int = 2          # 扫描间隔(小时), 1=每小时, 4=每4小时

    enable_main_profit_check: bool = True     # 主力获利检查
    main_profit_thresholds: list = field(default_factory=lambda: [
        (40,  51),
        (60,  45),
        (999, 35),
    ])

    # ── 2. Entry Filters (门控) ──────────────────────────────────────
    # Supertrend
    enable_supertrend_gate: bool = False
    st_period: int = 6
    st_multiplier: float = 4.0
    st_timeframe: str = '1h'

    # ── 3. Position Management ───────────────────────────────────────
    # free_cash_pct | equity_pct | fixed_usd（见 MoonshotConfig 注释）
    position_sizing_mode: PositionSizingMode = "free_cash_pct"
    position_size_ratio: float = 0.08
    fixed_invest_usd: float | None = None
    max_positions: int = 8
    leverage: int = 3
    max_hold_days: int = 7
    enable_funding_fee: bool = True

    # Take Profit
    tp_initial: float = 0.16
    tp_reduced: float = 0.08
    tp_hours_threshold: int = 6
    tp_after_add: float = 0.45

    # Stop Loss
    sl_threshold: float = 0.42

    # Trailing Stop
    enable_trailing_stop: bool = True
    trailing_activation_pct: float = 0.08
    trailing_distance_pct: float = 0.04

    # Dynamic Ratio SL
    enable_dynamic_ratio_sl: bool = False
    ratio_change_threshold: float = -0.18
    ratio_data_start: str = '2025-12-12'

    # Add Position
    enable_add_position: bool = True
    add_position_threshold: float = 0.2
    add_position_multiplier: float = 1.0


class RollingStrategy:
    """Moonshot-R24 — hourly rolling 24h top gainer short strategy."""

    def __init__(self, config: RollingConfig | None = None):
        self.config = config or RollingConfig()
        self.last_signal_details = []
        self.last_gate_details = {}

    def select_signals(
        self,
        feed: DataFeed,
        dt: datetime,
        preloaded_gainers: dict | None = None,
    ) -> list[tuple[str, float]]:
        """Select signals from hourly top gainers.

        Args:
            feed: DataFeed for auxiliary lookups (listing date, 30d avg).
            dt: Current scan datetime.
            preloaded_gainers: {datetime_key: [(symbol, pct_chg), ...]}
        """
        self.last_signal_details = []

        if preloaded_gainers is not None:
            key = dt.strftime('%Y-%m-%d %H:00')
            gainers = preloaded_gainers.get(key, [])[:self.config.top_n]
        else:
            gainers = []

        results = []
        for symbol, pct_chg in gainers:
            detail = {
                'symbol': symbol, 'pct_chg': pct_chg, 'listed_days': None,
                'avg_price_30d': None, 'close_1d': None, 'from_avg_pct': None,
                'profit_threshold': None, 'filter_result': '通过'
            }

            if pct_chg < self.config.min_pct_chg:
                detail['filter_result'] = '剔除:涨幅不达标'
                self.last_signal_details.append(detail)
                continue

            if self.config.min_listed_days > 0:
                listing_date = feed.load_listing_date(symbol)
                if listing_date is not None:
                    days_listed = (dt - listing_date).days
                    detail['listed_days'] = days_listed
                    if days_listed < self.config.min_listed_days:
                        detail['filter_result'] = '剔除:上市天数不足'
                        self.last_signal_details.append(detail)
                        continue

            if self.config.enable_main_profit_check:
                # 与 paper 一致：用「上一自然日」已完成日K 对 30d 均价，避免扫描当日日K未收盘/与 24h ticker 不同步
                profit_dt = dt - timedelta(days=1)
                avg_price = feed.load_30d_avg_price(symbol, profit_dt)
                if avg_price and avg_price > 0:
                    current_close = feed.load_1d_close(symbol, profit_dt)
                    if current_close and current_close > 0:
                        from_avg_pct = (current_close - avg_price) / avg_price * 100
                        threshold = self._get_main_profit_threshold(pct_chg)
                        detail.update({
                            'avg_price_30d': avg_price, 'close_1d': current_close,
                            'from_avg_pct': from_avg_pct, 'profit_threshold': threshold
                        })
                        if from_avg_pct < threshold:
                            detail['filter_result'] = '剔除:主力未获利'
                            self.last_signal_details.append(detail)
                            continue

            self.last_signal_details.append(detail)
            results.append((symbol, pct_chg))

        return results

    def should_enter(
        self,
        symbol: str,
        pct_chg: float,
        feed: DataFeed,
        signal_dt: datetime,
        open_positions: list | None = None,
    ) -> tuple[bool, str, int]:
        """Check entry gates. Returns (accept, reason, delay_hours). delay_hours always 0."""
        cfg = self.config
        self.last_gate_details = {'ratio': None, 'volume_100m': None}

        if open_positions and symbol in open_positions:
            return False, "already_in_position", 0

        if cfg.min_listed_days > 0:
            listing_date = feed.load_listing_date(symbol)
            if listing_date and (signal_dt - listing_date).days < cfg.min_listed_days:
                return False, "新币过滤", 0

        if cfg.enable_main_profit_check:
            profit_dt = signal_dt - timedelta(days=1)
            avg_price = feed.load_30d_avg_price(symbol, profit_dt)
            if avg_price and avg_price > 0:
                current_close = feed.load_1d_close(symbol, profit_dt)
                if current_close and current_close > 0:
                    from_avg = (current_close - avg_price) / avg_price * 100
                    threshold = self._get_main_profit_threshold(pct_chg)
                    if from_avg < threshold:
                        return False, "主力未获利", 0

        return True, "即时建仓", 0

    def _get_main_profit_threshold(self, pct_chg: float) -> float:
        for max_pct, threshold in self.config.main_profit_thresholds:
            if pct_chg < max_pct:
                return threshold
        return self.config.main_profit_thresholds[-1][1]

    def resolve_tp_threshold(
        self,
        trade: AmplitudeTrade,
        hold_hours: float,
        session_low: float | None = None,
    ) -> float:
        """与 check_exit 固定止盈段一致（供 runner 歧义 K 线检测）；R24 忽略 session_low。"""
        _ = session_low
        cfg = self.config
        if trade.has_added_position:
            return cfg.tp_after_add
        if hold_hours >= cfg.tp_hours_threshold:
            return cfg.tp_reduced
        return cfg.tp_initial

    def check_exit(
        self,
        trade: AmplitudeTrade,
        candle_high: float,
        candle_low: float,
        current_price: float,
        current_time: datetime,
        hold_hours: float,
    ) -> tuple[str, float] | None:
        """Check exit conditions — identical to Moonshot."""
        cfg = self.config
        entry_price = trade.entry_price
        if not entry_price or entry_price <= 0:
            return None

        tp_threshold = self.resolve_tp_threshold(trade, hold_hours, None)

        # Stop loss
        if (candle_high - entry_price) / entry_price >= cfg.sl_threshold:
            return "stop_loss", entry_price * (1 + cfg.sl_threshold)

        # Add position
        if (cfg.enable_add_position and not trade.has_added_position
                and (candle_high - entry_price) / entry_price >= cfg.add_position_threshold):
            return "add_position", entry_price * (1 + cfg.add_position_threshold)

        # Take profit
        if (candle_low - entry_price) / entry_price <= -tp_threshold:
            res = (
                "tp_after_add" if trade.has_added_position else
                ("tp_reduced" if hold_hours >= cfg.tp_hours_threshold else "tp_initial")
            )
            return res, entry_price * (1 - tp_threshold)

        # 同根 K 须并入 candle_low 再判追踪，避免漏触发
        if cfg.enable_trailing_stop:
            low_for_trail = (
                min(trade.lowest_price, candle_low)
                if trade.lowest_price is not None
                else candle_low
            )
            if (entry_price - low_for_trail) / entry_price >= cfg.trailing_activation_pct:
                ts_price = low_for_trail * (1 + cfg.trailing_distance_pct)
                if candle_high >= ts_price:
                    return "trailing_stop", ts_price

        if trade.lowest_price is None or candle_low < trade.lowest_price:
            trade.lowest_price = candle_low

        return None

    def check_dynamic_ratio_sl(
        self,
        trade: AmplitudeTrade,
        current_ratio: float | None,
        current_time: datetime,
        current_price: float,
    ) -> tuple[str, float] | None:
        cfg = self.config
        if not cfg.enable_dynamic_ratio_sl:
            return None
        data_start = datetime.strptime(cfg.ratio_data_start, '%Y-%m-%d').replace(tzinfo=UTC)
        if current_time < data_start or trade.entry_account_ratio is None or current_ratio is None:
            return None
        if current_ratio - trade.entry_account_ratio <= cfg.ratio_change_threshold:
            trade.exit_account_ratio = current_ratio
            trade.account_ratio_change = current_ratio - trade.entry_account_ratio
            return "dynamic_ratio_sl", current_price
        return None

    def compute_order_margin(self, free_cash: float, total_equity: float) -> float:
        """下一笔开仓保证金（USD），不超过 free_cash。"""
        c = self.config
        return _compute_order_margin(
            free_cash=free_cash,
            total_equity=total_equity,
            mode=c.position_sizing_mode,
            position_size_ratio=c.position_size_ratio,
            fixed_invest_usd=c.fixed_invest_usd,
        )
