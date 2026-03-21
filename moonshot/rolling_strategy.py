"""RollingStrategy (Moonshot-R24) — 24h rolling top gainer short with hourly scanning.

Independent strategy that scans every hour using K1h-based 24h rolling change,
instead of daily K1d change. Shares position management / exit logic with Moonshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from moonshot.models import AmplitudeTrade
from moonshot.data_feed import DataFeed

logger = logging.getLogger(__name__)


@dataclass
class RollingConfig:
    """Strategy configuration for Moonshot-R24 (24h rolling)."""

    # ── 1. Signal Generation ─────────────────────────────────────────
    top_n: int = 3                        # 每次扫描取涨幅前 N 名
    min_pct_chg: float = 10.0             # 最小涨幅要求 10%
    min_listed_days: int = 10             # 新币过滤
    signal_cooldown_hours: int = 24       # 同币种信号冷却期(小时), 可调
    rolling_window_hours: int = 24        # 滚动窗口大小(小时), 可调
    scan_interval_hours: int = 1          # 扫描间隔(小时), 1=每小时, 4=每4小时

    enable_main_profit_check: bool = True     # 主力获利检查
    main_profit_thresholds: list = field(default_factory=lambda: [
        (40,  51),
        (60,  45),
        (999, 35),
    ])

    # ── 2. Entry Filters (门控) ──────────────────────────────────────
    # Gate A: 多空比延迟 (小时级)
    enable_top_trader_delay: bool = False
    top_trader_delay_threshold: float = 0.75
    top_trader_delay_hours: int = 24          # 延迟小时数 (原版按天, R24按小时)
    top_trader_data_start: str = '2025-12-11'

    # Gate B: 成交额检查
    enable_volume_filter: bool = False
    high_pct_chg_threshold: float = 50.0
    min_volume_for_high_pct: float = 0.8e8
    volume_delay_hours: int = 24              # 延迟小时数

    # Gate C: 延迟建仓价格检查 (小时级)
    max_price_drop_for_delay: float = 11.0    # 跌幅 > 11% → 放弃
    price_drop_check_window_hours: int = 24   # 检查延迟期间价格跌幅的时间窗口

    # Gate D: Supertrend
    enable_supertrend_gate: bool = False
    st_period: int = 6
    st_multiplier: float = 4.0
    st_timeframe: str = '1h'

    # ── 3. Position Management ───────────────────────────────────────
    position_size_ratio: float = 0.04
    max_positions: int = 7
    leverage: int = 1
    max_hold_days: int = 11
    enable_funding_fee: bool = True

    # Take Profit
    tp_initial: float = 0.34
    tp_reduced: float = 0.14
    tp_hours_threshold: int = 10
    tp_after_add: float = 0.45

    # Stop Loss
    sl_threshold: float = 0.44

    # Trailing Stop
    enable_trailing_stop: bool = True
    trailing_activation_pct: float = 0.16
    trailing_distance_pct: float = 0.09

    # Dynamic Ratio SL
    enable_dynamic_ratio_sl: bool = False
    ratio_change_threshold: float = -0.18
    ratio_data_start: str = '2025-12-12'

    # Add Position
    enable_add_position: bool = True
    add_position_threshold: float = 0.36
    add_position_multiplier: float = 1.0


class RollingStrategy:
    """Moonshot-R24 — hourly rolling 24h top gainer short strategy."""

    def __init__(self, config: Optional[RollingConfig] = None):
        self.config = config or RollingConfig()
        self.last_signal_details = []
        self.last_gate_details = {}

    def select_signals(
        self,
        feed: DataFeed,
        dt: datetime,
        preloaded_gainers: Optional[dict] = None,
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
                avg_price = feed.load_30d_avg_price(symbol, dt)
                if avg_price and avg_price > 0:
                    current_close = feed.load_1d_close(symbol, dt)
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
        open_positions: Optional[list] = None,
    ) -> tuple[bool, str, int]:
        """Check entry gates. Returns (accept, reason, delay_hours).

        Unlike Moonshot which returns delay_days, R24 returns delay_hours.
        """
        cfg = self.config
        delay_hours = 0
        delay_reasons = []
        self.last_gate_details = {'ratio': None, 'volume_100m': None}

        if open_positions and symbol in open_positions:
            return False, "already_in_position", 0

        if cfg.min_listed_days > 0:
            listing_date = feed.load_listing_date(symbol)
            if listing_date and (signal_dt - listing_date).days < cfg.min_listed_days:
                return False, "新币过滤", 0

        if cfg.enable_main_profit_check:
            avg_price = feed.load_30d_avg_price(symbol, signal_dt)
            if avg_price and avg_price > 0:
                current_close = feed.load_1d_close(symbol, signal_dt)
                if current_close and current_close > 0:
                    from_avg = (current_close - avg_price) / avg_price * 100
                    threshold = self._get_main_profit_threshold(pct_chg)
                    if from_avg < threshold:
                        return False, "主力未获利", 0

        if cfg.enable_top_trader_delay:
            data_start = datetime.strptime(cfg.top_trader_data_start, '%Y-%m-%d').replace(tzinfo=timezone.utc)
            if signal_dt >= data_start:
                ratio = feed.load_top_trader_ratio(symbol, signal_dt)
                self.last_gate_details['ratio'] = ratio
                if ratio is not None and ratio < cfg.top_trader_delay_threshold:
                    delay_hours = cfg.top_trader_delay_hours
                    delay_reasons.append("gate2多空比")

        if cfg.enable_volume_filter and pct_chg >= cfg.high_pct_chg_threshold:
            vol = feed.load_24h_volume(symbol, signal_dt + timedelta(hours=48))
            if vol >= 0:
                self.last_gate_details['volume_100m'] = vol / 1e8
            if 0 <= vol < cfg.min_volume_for_high_pct:
                delay_hours = cfg.volume_delay_hours
                delay_reasons.append("gate3成交额")

        # Price drop check during delay period (hourly)
        if delay_hours > 0 and cfg.max_price_drop_for_delay > 0:
            check_start = signal_dt
            check_end = signal_dt + timedelta(hours=delay_hours)
            candles = feed.load_1h(symbol, check_start, check_end)
            if len(candles) >= 2:
                first_price = candles[0].open
                last_price = candles[-1].close
                if first_price > 0:
                    drop_pct = (first_price - last_price) / first_price * 100
                    if drop_pct > cfg.max_price_drop_for_delay:
                        return False, "delay_price_drop", 0

        return True, (",".join(delay_reasons) if delay_reasons else "即时建仓"), delay_hours

    def _get_main_profit_threshold(self, pct_chg: float) -> float:
        for max_pct, threshold in self.config.main_profit_thresholds:
            if pct_chg < max_pct:
                return threshold
        return self.config.main_profit_thresholds[-1][1]

    def check_exit(
        self,
        trade: AmplitudeTrade,
        candle_high: float,
        candle_low: float,
        current_price: float,
        current_time: datetime,
        hold_hours: float,
    ) -> Optional[tuple[str, float]]:
        """Check exit conditions — identical to Moonshot."""
        cfg = self.config
        entry_price = trade.entry_price

        tp_threshold = (
            cfg.tp_after_add if trade.has_added_position else
            (cfg.tp_reduced if hold_hours >= cfg.tp_hours_threshold else cfg.tp_initial)
        )

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

        # Trailing stop
        if cfg.enable_trailing_stop and trade.lowest_price is not None:
            if (entry_price - trade.lowest_price) / entry_price >= cfg.trailing_activation_pct:
                ts_price = trade.lowest_price * (1 + cfg.trailing_distance_pct)
                if candle_high >= ts_price:
                    return "trailing_stop", ts_price

        # Update lowest
        if trade.lowest_price is None or candle_low < trade.lowest_price:
            trade.lowest_price = candle_low

        return None

    def check_dynamic_ratio_sl(
        self,
        trade: AmplitudeTrade,
        current_ratio: Optional[float],
        current_time: datetime,
        current_price: float,
    ) -> Optional[tuple[str, float]]:
        cfg = self.config
        if not cfg.enable_dynamic_ratio_sl:
            return None
        data_start = datetime.strptime(cfg.ratio_data_start, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        if current_time < data_start or trade.entry_account_ratio is None or current_ratio is None:
            return None
        if current_ratio - trade.entry_account_ratio <= cfg.ratio_change_threshold:
            trade.exit_account_ratio = current_ratio
            trade.account_ratio_change = current_ratio - trade.entry_account_ratio
            return "dynamic_ratio_sl", current_price
        return None

    def invest_amount(self, total_asset: float) -> float:
        return total_asset * self.config.position_size_ratio
