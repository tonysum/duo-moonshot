"""R24 raw-surge 策略：预加载为 (symbol, pct, sr, yavg) 时走原始信号筛选；平仓/止盈止损逻辑自包含。

不继承 ``RollingStrategy``；仅依赖 ``DataFeed`` 接口与 ``moonshot.sizing``。
"""

from __future__ import annotations

from datetime import datetime

from moonshot.data_feed import DataFeed
from moonshot.models import AmplitudeTrade
from moonshot.r24_raw_surge_config import RawSurgeR24Config
from moonshot.sizing import compute_order_margin as _compute_order_margin


class RawSurgeRollingStrategy:
    """预加载字典的 value 可为 ``(symbol, pct)`` 或 ``(symbol, pct, sr, yavg)``。

    预加载阶段：漏斗见 ``r24_raw_surge_preload.build_raw_surge_hourly_gainers``（涨幅门 →
    ``max_sr_probe`` → sr 门 → 可选 ``raw_min_yavg_sell_volume`` → ``candidate_rank_mode`` 截 ``top_n``）。若配置了
    ``raw_max_signals_per_hour``，在 ``select_signals`` 内再截断到该上限。
    """

    def __init__(self, config: RawSurgeR24Config | None = None) -> None:
        self.config = config or RawSurgeR24Config()
        self.last_signal_details: list = []
        self.last_gate_details: dict = {}

    def select_signals(
        self,
        feed: DataFeed,
        dt,
        preloaded_gainers: dict | None = None,
    ) -> list[tuple[str, float]]:
        self.last_signal_details = []
        key = dt.strftime("%Y-%m-%d %H:00")
        raw = preloaded_gainers.get(key, []) if preloaded_gainers else []

        cfg = self.config
        rows: list[tuple] = []
        for item in raw:
            if len(item) >= 4:
                rows.append((item[0], item[1], item[2], item[3]))
            else:
                rows.append((item[0], item[1], None, None))

        if cfg.raw_max_signals_per_hour is not None:
            rows = rows[: cfg.raw_max_signals_per_hour]

        # 同一小时同一 symbol 只保留一条（preload 重复行会导致重复 pending / verify 异常）
        merged: dict[str, tuple] = {}
        merge_order: list[str] = []
        for tup in rows:
            sym = tup[0]
            if sym not in merged:
                merged[sym] = tup
                merge_order.append(sym)
                continue
            cur = merged[sym]
            ns = tup[2] if len(tup) > 2 and tup[2] is not None else float("-inf")
            os_ = cur[2] if len(cur) > 2 and cur[2] is not None else float("-inf")
            if ns > os_ or (ns == os_ and tup[1] > cur[1]):
                merged[sym] = tup
        rows = [merged[s] for s in merge_order]

        results = []
        for symbol, pct_chg, pre_sr, pre_yavg in rows:
            detail = {
                "symbol": symbol,
                "pct_chg": pct_chg,
                "listed_days": None,
                "avg_price_30d": None,
                "close_1d": None,
                "from_avg_pct": None,
                "profit_threshold": None,
                "filter_result": "通过",
                "sell_surge_ratio": None,
                "yesterday_avg_hour_sell_volume": None,
            }

            # 此处不再对涨幅进行初筛，仅保留卖量倍数等后续筛选

            if cfg.min_listed_days > 0:
                listing_date = feed.load_listing_date(symbol)
                if listing_date is not None:
                    days_listed = (dt - listing_date).days
                    detail["listed_days"] = days_listed
                    if days_listed < cfg.min_listed_days:
                        detail["filter_result"] = "剔除:上市天数不足"
                        self.last_signal_details.append(detail)
                        continue

            if pre_sr is not None:
                detail["sell_surge_ratio"] = pre_sr
                detail["yesterday_avg_hour_sell_volume"] = pre_yavg
            else:
                loader = getattr(feed, "load_sell_surge_detail", None)
                if loader is not None:
                    sr, yavg = loader(symbol, dt)
                    detail["sell_surge_ratio"] = sr
                    detail["yesterday_avg_hour_sell_volume"] = yavg

            if cfg.enable_sell_surge_gate:
                if detail.get("sell_surge_ratio") is None:
                    loader = getattr(feed, "load_sell_surge_detail", None)
                    if loader is None:
                        detail["filter_result"] = "剔除:卖量门控需要RollingDataFeed"
                        self.last_signal_details.append(detail)
                        continue
                    sr, yavg = loader(symbol, dt)
                    detail["sell_surge_ratio"] = sr
                    detail["yesterday_avg_hour_sell_volume"] = yavg

                sr = detail.get("sell_surge_ratio")
                if sr is None:
                    detail["filter_result"] = "剔除:卖量数据不足"
                    self.last_signal_details.append(detail)
                    continue
                if sr < cfg.sell_surge_threshold:
                    detail["filter_result"] = "剔除:卖量未暴涨"
                    self.last_signal_details.append(detail)
                    continue
                if sr > cfg.sell_surge_max:
                    detail["filter_result"] = "剔除:卖量倍数过高"
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
        """R24-raw-surge 不使用主力获利门控（不读 30d 均价 / 日收）。"""
        cfg = self.config
        self.last_gate_details = {"ratio": None, "volume_100m": None}

        if open_positions and symbol in open_positions:
            return False, "already_in_position", 0

        if cfg.min_listed_days > 0:
            listing_date = feed.load_listing_date(symbol)
            if listing_date and (signal_dt - listing_date).days < cfg.min_listed_days:
                return False, "新币过滤", 0

        return True, "即时建仓", 0

    def resolve_tp_threshold(
        self,
        trade: AmplitudeTrade,
        hold_hours: float,
        session_low: float | None = None,
    ) -> float:
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
        cfg = self.config
        entry_price = trade.entry_price
        if not entry_price or entry_price <= 0:
            return None

        tp_threshold = self.resolve_tp_threshold(trade, hold_hours, None)

        if (candle_high - entry_price) / entry_price >= cfg.sl_threshold:
            return "stop_loss", entry_price * (1 + cfg.sl_threshold)

        if (
            cfg.enable_add_position
            and not trade.has_added_position
            and (candle_high - entry_price) / entry_price >= cfg.add_position_threshold
        ):
            return "add_position", entry_price * (1 + cfg.add_position_threshold)

        if candle_low <= entry_price * (1 - tp_threshold):
            res = (
                "tp_after_add"
                if trade.has_added_position
                else ("tp_reduced" if hold_hours >= cfg.tp_hours_threshold else "tp_initial")
            )
            return res, entry_price * (1 - tp_threshold)

        if cfg.enable_trailing_stop:
            low_for_trail = (
                min(trade.lowest_price, candle_low) if trade.lowest_price is not None else candle_low
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
        """R24-raw-surge 不使用多空比动态止损。"""
        return None

    def compute_order_margin(self, free_cash: float, total_equity: float) -> float:
        c = self.config
        return _compute_order_margin(
            free_cash=free_cash,
            total_equity=total_equity,
            mode=c.position_sizing_mode,
            position_size_ratio=c.position_size_ratio,
            fixed_invest_usd=c.fixed_invest_usd,
        )
