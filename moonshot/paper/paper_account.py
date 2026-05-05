"""PaperAccount — Simulated account for paper trading.
"""

import logging
from datetime import UTC, datetime

from moonshot.paper.paper_store import MoonshotPosition, PaperStore
from moonshot.r24_raw_surge_config import RawSurgeR24Config

logger = logging.getLogger(__name__)


class PaperAccount:
    """Manages virtual capital and tracking for paper trading."""

    def __init__(
        self,
        store: PaperStore,
        initial_capital: float = 10000.0,
        *,
        config: RawSurgeR24Config | None = None,
    ):
        self.store = store
        self._config = config
        saved_capital = store.get_state("capital")
        self.capital = float(saved_capital) if saved_capital else initial_capital

    def _fee_rate(self) -> float:
        cfg = self._config
        return float(getattr(cfg, "taker_fee_rate", 0.0005) if cfg is not None else 0.0005)

    def _entry_slippage_bps(self) -> float:
        cfg = self._config
        return float(getattr(cfg, "entry_slippage_bps", 0.0) if cfg is not None else 0.0)

    def _exit_slippage_bps(self) -> float:
        cfg = self._config
        return float(getattr(cfg, "exit_slippage_bps", 0.0) if cfg is not None else 0.0)

    def _liquidation_slippage_bps(self) -> float:
        cfg = self._config
        return float(getattr(cfg, "liquidation_slippage_bps", 0.0) if cfg is not None else 0.0)

    @staticmethod
    def _apply_short_slippage(price: float, *, bps: float, side: str) -> float:
        """Short slippage model.

        - entry/add: worse fill => lower sell price
        - exit (buy to cover): worse fill => higher buy price
        """
        p = float(price)
        if p <= 0:
            return p
        f = 1.0 + abs(float(bps)) / 10_000.0
        if side in ("entry", "add"):
            return p / f
        return p * f

    def open_position(self, pos: MoonshotPosition) -> bool:
        """Return True if opened; False if insufficient capital (logged, no exception)."""
        notional = float(pos.invest_amount) * float(pos.leverage)
        entry_commission = notional * self._fee_rate()
        need = float(pos.invest_amount) + entry_commission
        if self.capital < need:
            logger.warning(
                "Paper skip open %s: insufficient capital %.4f < need %.4f",
                pos.symbol, self.capital, need,
            )
            return False
        if not self.store.get_state("initial_capital"):
            # Summary 收益率基准：首笔开仓前权益 = 可用现金 + 本笔将锁定的保证金
            self.store.set_state("initial_capital", str(self.capital + need))

        # Fees paid from wallet
        pos.entry_commission = float(entry_commission)
        self.capital -= need
        self.store.set_state("capital", str(self.capital))
        self.store.save_position(pos)
        self.store.log_event(
            "OPEN",
            pos.symbol,
            f"Opened {pos.symbol} @ {pos.entry_price}, invest={pos.invest_amount}, comm={entry_commission:.2f}",
        )
        return True

    async def close_position(
        self,
        pos: MoonshotPosition,
        exit_price: float,
        exit_time: str,
        result: str,
        feed=None,
    ):
        # Apply exit slippage (short exit: worse fill => higher buy price)
        exit_bps = self._liquidation_slippage_bps() if result == "liquidation" else self._exit_slippage_bps()
        fill_exit = self._apply_short_slippage(float(exit_price), bps=exit_bps, side="exit")

        actual_pct = (pos.entry_price - fill_exit) / pos.entry_price
        profit_amount = pos.invest_amount * actual_pct * pos.leverage

        # Commission: entry + exit
        notional = pos.invest_amount * pos.leverage
        entry_comm = float(getattr(pos, "entry_commission", 0.0) or 0.0)
        exit_comm = float(notional) * self._fee_rate()
        commission = entry_comm + exit_comm

        # Funding fee: fetch real rates from exchange (optional; align with backtest enable_funding_fee)
        funding_fee = 0.0
        funding_count = 0
        fund_on = self._config is None or getattr(self._config, "enable_funding_fee", True)
        if feed and fund_on:
            try:
                entry_dt = datetime.fromisoformat(pos.entry_time)
                exit_dt = datetime.fromisoformat(exit_time)
                funding_records = await feed.load_funding_history(pos.symbol, entry_dt, exit_dt)
                for _ts, rate in funding_records:
                    # Short position: pays negative rate, receives positive rate
                    # funding_fee > 0 means cost, < 0 means income
                    funding_fee += notional * (-rate)  # short pays opposite sign
                funding_count = len(funding_records)
            except Exception as e:
                logger.warning("Failed to fetch funding for %s: %s, using estimate", pos.symbol, e)
                # Fallback: estimate based on hold hours (8h intervals, ~0.01% average)
                entry_dt = datetime.fromisoformat(pos.entry_time)
                exit_dt = datetime.fromisoformat(exit_time)
                hold_hours = (exit_dt - entry_dt).total_seconds() / 3600
                funding_fee = notional * 0.0001 * (hold_hours / 8)

        total_fees = commission + funding_fee
        net_pnl = profit_amount - total_fees

        entry_dt = datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00"))
        exit_dt = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=UTC)
        if exit_dt.tzinfo is None:
            exit_dt = exit_dt.replace(tzinfo=UTC)
        holding_hours = int((exit_dt - entry_dt).total_seconds() / 3600)

        self.capital += (pos.invest_amount + net_pnl)
        self.store.set_state("capital", str(self.capital))
        self.store.remove_position(pos.symbol)

        trade_data = pos.model_dump()
        trade_data.update({
            "exit_price": float(fill_exit),
            "exit_price_raw": float(exit_price),
            "exit_time": exit_time,
            "result": result,
            "net_pnl": round(net_pnl, 4),
            "gross_pnl": round(profit_amount, 4),
            "commission": round(commission, 4),
            "entry_commission": round(entry_comm, 4),
            "exit_commission": round(exit_comm, 4),
            "funding_fee": round(funding_fee, 4),
            "funding_count": funding_count,
            "total_fees": round(total_fees, 4),
            "capital_after": round(self.capital, 4),
            "holding_hours": holding_hours,
        })
        self.store.add_trade(pos.symbol, pos.entry_time, exit_time, trade_data)

        fee_detail = f"comm={commission:.2f} fund={funding_fee:.2f}({funding_count}×)"
        self.store.log_event("CLOSE", pos.symbol,
            f"Closed @ {fill_exit}, pnl={net_pnl:.2f} ({fee_detail}), {result}")
        logger.info("  %s %s pnl=%.2f [%s]", "✅" if net_pnl > 0 else "❌", pos.symbol, net_pnl, fee_detail)

    def add_position(self, symbol: str, add_price: float, add_time: str, *, multiplier: float = 1.0):
        positions = self.store.get_open_positions()
        pos = next((p for p in positions if p.symbol == symbol), None)
        if not pos: return

        # Apply entry slippage (short add: worse fill => lower sell price)
        fill_add = self._apply_short_slippage(
            float(add_price),
            bps=self._entry_slippage_bps(),
            side="add",
        )
        original_size = pos.position_size
        add_size = original_size * float(multiplier)
        if add_size <= 0:
            return
        add_margin = add_size * float(fill_add)
        # 与回测 account.add_position_bt8 一致：补仓扣保证金，不单独收一笔「加仓佣金」。
        need = add_margin
        if self.capital < need:
            logger.warning("Insufficient capital for add_position on %s", symbol)
            return

        self.capital -= need
        self.store.set_state("capital", str(self.capital))

        # Align with backtest: add_size = original_size * multiplier
        total_size = original_size + add_size
        pos.entry_price = (pos.entry_price * original_size + float(fill_add) * add_size) / total_size
        pos.position_size = total_size
        pos.invest_amount += add_margin
        pos.has_added_position = True
        pos.add_price = float(fill_add)
        pos.add_price_raw = float(add_price)
        pos.add_time = add_time
        base_hi = pos.highest_price if pos.highest_price is not None else pos.entry_price
        pos.highest_price = max(base_hi, float(fill_add), pos.entry_price)
        # 与回测一致：补仓后新均价下，旧 lowest 会使追踪止盈失真；下一轮用现价重算 watermark
        pos.lowest_price = None
        self.store.save_position(pos)
        self.store.log_event(
            "ADD",
            symbol,
            f"Added to {symbol} @ {fill_add} (raw={add_price}), new avg={pos.entry_price:.4f}",
        )
