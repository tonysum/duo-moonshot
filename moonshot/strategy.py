"""Moonshot Strategy (狙击top1) — daily top gainer short with simplified risk controls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from moonshot.models import AmplitudeTrade
from moonshot.sizing import PositionSizingMode, compute_order_margin as _compute_order_margin
from moonshot.data_feed import DataFeed

logger = logging.getLogger(__name__)


@dataclass
class MoonshotConfig:
    """Strategy configuration for Moonshot (狙击top1)."""

    # ── 1. Signal Generation (信号生成) ───────────────────────────────
    top_n: int = 3                        # 每日最多选择涨幅前 N 名
    min_pct_chg: float = 10.0             # 最小涨幅要求 10%
    min_listed_days: int = 10              # 新币过滤
    
    enable_main_profit_check: bool = True     # 结合30日均线的“主力获利”检查
    # (涨幅上限%, 30日均价涨幅阈值%)
    main_profit_thresholds: list = field(default_factory=lambda: [
        (40,  51),   # 涨幅<40%: 要求30日均价涨幅>51%
        (60,  45),   # 涨幅40-60%: 要求>45%
        (999, 35),   # 涨幅≥60%: 要求>35%
    ])

    # ── 2. Entry Filters (入场风控门控) ───────────────────────────────
    # Supertrend 检查
    enable_supertrend_gate: bool = False      # 是否启用超级趋势门控
    st_period: int = 6                       # Supertrend 周期
    st_multiplier: float = 4.0                # Supertrend 乘数
    st_timeframe: str = '1h'                  # Supertrend 周期 ('1h' or '15m')

    # ── 3. Position Management (仓位管理与退出机制) ───────────────────
    # Capital & Sizing
    # position_sizing_mode:
    #   free_cash_pct — 每笔保证金 = 剩余现金 × position_size_ratio（默认）
    #   equity_pct    — 每笔目标 = 总权益 × ratio，但不得超过当前剩余现金
    #   fixed_usd     — 每笔固定保证金 fixed_invest_usd（不得超过剩余现金）
    position_sizing_mode: PositionSizingMode = "free_cash_pct"
    position_size_ratio: float = 0.04      # 用于 free_cash_pct / equity_pct
    fixed_invest_usd: Optional[float] = None  # fixed_usd 模式下的固定保证金
    max_positions: int = 7                # 最大同时持仓
    leverage: int = 2                     # 固定杠杆 1x
    max_hold_days: int = 11               # 超时强制平仓天数
    enable_funding_fee: bool = True       # 开启并计算资金费率成本

    # Take Profit
    tp_initial: float = 0.34              # 初始止盈 34%
    tp_reduced: float = 0.14              # 超时后止盈 14%
    tp_hours_threshold: int = 10          # 降低止盈的时间阈值 10h
    tp_after_add: float = 0.45            # 补仓后止盈 45%

    # Stop Loss
    sl_threshold: float = 0.44            # 固定止损 44%

    # Trailing take profit（空仓：相对入场价浮盈达阈值后记录最低价，价格自低点反弹比例触发平仓）
    enable_trailing_take_profit: bool = True
    trailing_activation_pct: float = 0.16     # 浮盈≥该比例(相对入场价)后激活
    trailing_distance_pct: float = 0.09       # 自持仓以来最低价向上反弹该比例则平仓
    
    # Dynamic Stop Loss
    enable_dynamic_ratio_sl: bool = False
    ratio_change_threshold: float = -0.18     # 多空比下降 ≥ 18% → 止损
    ratio_data_start: str = '2025-12-12'

    # Add Position
    enable_add_position: bool = True   # 是否启用补仓
    add_position_threshold: float = 0.36  # 补仓触发：涨36%
    add_position_multiplier: float = 1.0  # 等量补仓


class MoonshotStrategy:
    """Moonshot — simplified daily top gainer short strategy."""

    def __init__(self, config: Optional[MoonshotConfig] = None):
        self.config = config or MoonshotConfig()
        self.last_signal_details = []
        self.last_gate_details = {}

    def select_signals(self, feed: DataFeed, date: datetime, preloaded_gainers: Optional[dict] = None) -> list[tuple[str, float]]:
        self.last_signal_details = []
        if preloaded_gainers is not None:
            gainers = preloaded_gainers.get(date.strftime('%Y-%m-%d'), [])[:self.config.top_n]
        else:
            gainers = feed.load_daily_top_gainers(date, top_n=self.config.top_n)

        results = []
        for symbol, pct_chg in gainers:
            detail = {
                'symbol': symbol, 'pct_chg': pct_chg, 'listed_days': None,
                'avg_price_30d': None, 'close_1d': None, 'from_avg_pct': None,
                'profit_threshold': None, 'filter_result': '通过'
            }
            if pct_chg >= self.config.min_pct_chg:
                if self.config.min_listed_days > 0:
                    listing_date = feed.load_listing_date(symbol)
                    if listing_date is not None:
                        days_listed = (date - listing_date).days
                        detail['listed_days'] = days_listed
                        if days_listed < self.config.min_listed_days:
                            detail['filter_result'] = '剔除:上市天数不足'
                            self.last_signal_details.append(detail); continue

                if self.config.enable_main_profit_check:
                    avg_price = feed.load_30d_avg_price(symbol, date)
                    if avg_price and avg_price > 0:
                        current_close = feed.load_1d_close(symbol, date)
                        if current_close and current_close > 0:
                            from_avg_pct = (current_close - avg_price) / avg_price * 100
                            threshold = self._get_main_profit_threshold(pct_chg)
                            detail.update({'avg_price_30d': avg_price, 'close_1d': current_close, 'from_avg_pct': from_avg_pct, 'profit_threshold': threshold})
                            if from_avg_pct < threshold:
                                detail['filter_result'] = '剔除:主力未获利'
                                self.last_signal_details.append(detail); continue

                self.last_signal_details.append(detail)
                results.append((symbol, pct_chg))
            else:
                detail['filter_result'] = '剔除:涨幅不达标'
                self.last_signal_details.append(detail)
        return results

    def should_enter(self, symbol: str, pct_chg: float, feed: DataFeed, signal_date: datetime, open_positions: Optional[list] = None) -> tuple[bool, str, int]:
        cfg = self.config
        self.last_gate_details = {'ratio': None, 'volume_100m': None}

        if open_positions and symbol in open_positions:
            return False, "already_in_position", 0

        if cfg.min_listed_days > 0:
            listing_date = feed.load_listing_date(symbol)
            if listing_date and (signal_date - listing_date).days < cfg.min_listed_days:
                return False, f"新币过滤", 0

        if cfg.enable_main_profit_check:
            avg_price = feed.load_30d_avg_price(symbol, signal_date)
            if avg_price and avg_price > 0:
                current_close = feed.load_1d_close(symbol, signal_date)
                if current_close and current_close > 0:
                    from_avg = (current_close - avg_price) / avg_price * 100
                    threshold = self._get_main_profit_threshold(pct_chg)
                    if from_avg < threshold: return False, f"主力未获利", 0

        return True, "即时建仓", 0

    def _get_main_profit_threshold(self, pct_chg: float) -> float:
        for max_pct, threshold in self.config.main_profit_thresholds:
            if pct_chg < max_pct: return threshold
        return self.config.main_profit_thresholds[-1][1]

    def resolve_tp_threshold(
        self,
        trade: AmplitudeTrade,
        hold_hours: float,
        session_low: Optional[float] = None,
    ) -> float:
        """当前固定止盈比例（不含追踪）；`session_low` 保留签名供 runner 歧义 K 线调用，此处不使用。"""
        _ = session_low
        cfg = self.config
        if trade.has_added_position:
            return cfg.tp_after_add
        if hold_hours >= cfg.tp_hours_threshold:
            return cfg.tp_reduced
        return cfg.tp_initial

    def check_exit(self, trade: AmplitudeTrade, candle_high: float, candle_low: float, current_price: float, current_time: datetime, hold_hours: float) -> Optional[tuple[str, float]]:
        cfg = self.config; entry_price = trade.entry_price
        if not entry_price or entry_price <= 0:
            return None

        if (candle_high - entry_price) / entry_price >= cfg.sl_threshold:
            return "stop_loss", entry_price * (1 + cfg.sl_threshold)
        if cfg.enable_add_position and not trade.has_added_position and (candle_high - entry_price) / entry_price >= cfg.add_position_threshold:
            return "add_position", entry_price * (1 + cfg.add_position_threshold)

        tp_threshold = (
            cfg.tp_after_add if trade.has_added_position else
            (cfg.tp_reduced if hold_hours >= cfg.tp_hours_threshold else cfg.tp_initial)
        )

        if (candle_low - entry_price) / entry_price <= -tp_threshold:
            res = (
                "tp_after_add" if trade.has_added_position else
                ("tp_reduced" if hold_hours >= cfg.tp_hours_threshold else "tp_initial")
            )
            return res, entry_price * (1 - tp_threshold)
        # 本根 K 的 low 可能刷新最低；须先并入再判追踪，否则同根「深跌后反弹」会漏触发
        if cfg.enable_trailing_take_profit:
            low_for_trail = (
                min(trade.lowest_price, candle_low)
                if trade.lowest_price is not None
                else candle_low
            )
            if (entry_price - low_for_trail) / entry_price >= cfg.trailing_activation_pct:
                ts_price = low_for_trail * (1 + cfg.trailing_distance_pct)
                if candle_high >= ts_price:
                    return "trailing_take_profit", ts_price
        if trade.lowest_price is None or candle_low < trade.lowest_price:
            trade.lowest_price = candle_low
        return None

    def check_dynamic_ratio_sl(self, trade: AmplitudeTrade, current_ratio: Optional[float], current_time: datetime, current_price: float) -> Optional[tuple[str, float]]:
        cfg = self.config
        if not cfg.enable_dynamic_ratio_sl: return None
        data_start = datetime.strptime(cfg.ratio_data_start, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        if current_time < data_start or trade.entry_account_ratio is None or current_ratio is None: return None
        if current_ratio - trade.entry_account_ratio <= cfg.ratio_change_threshold:
            trade.exit_account_ratio, trade.account_ratio_change = current_ratio, current_ratio - trade.entry_account_ratio
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
