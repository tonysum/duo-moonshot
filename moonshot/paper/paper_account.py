"""PaperAccount — Simulated account for paper trading.
"""

import logging
from datetime import UTC, datetime

from moonshot.paper.paper_store import MoonshotPosition, PaperStore

logger = logging.getLogger(__name__)

# Commission rate: 0.05% per side (maker), 0.1% roundtrip
COMMISSION_RATE = 0.0005


class PaperAccount:
    """Manages virtual capital and tracking for paper trading."""

    def __init__(self, store: PaperStore, initial_capital: float = 10000.0):
        self.store = store
        saved_capital = store.get_state("capital")
        self.capital = float(saved_capital) if saved_capital else initial_capital

    def open_position(self, pos: MoonshotPosition):
        if self.capital < pos.invest_amount:
            raise ValueError(f"Insufficient capital: {self.capital} < {pos.invest_amount}")
        if not self.store.get_state("initial_capital"):
            # Summary 收益率基准：首笔开仓前权益 = 可用现金 + 本笔将锁定的保证金
            self.store.set_state("initial_capital", str(self.capital + pos.invest_amount))

        self.capital -= pos.invest_amount
        self.store.set_state("capital", str(self.capital))
        self.store.save_position(pos)
        self.store.log_event("OPEN", pos.symbol, f"Opened {pos.symbol} @ {pos.entry_price}, invest={pos.invest_amount}")

    async def close_position(self, pos: MoonshotPosition, exit_price: float, exit_time: str, result: str, feed=None):
        actual_pct = (pos.entry_price - exit_price) / pos.entry_price
        profit_amount = pos.invest_amount * actual_pct * pos.leverage

        # Commission: 0.05% per side × 2 (roundtrip)
        notional = pos.invest_amount * pos.leverage
        commission = notional * COMMISSION_RATE * 2

        # Funding fee: fetch real rates from exchange
        funding_fee = 0.0
        funding_count = 0
        if feed:
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
            "exit_price": exit_price,
            "exit_time": exit_time,
            "result": result,
            "net_pnl": round(net_pnl, 4),
            "gross_pnl": round(profit_amount, 4),
            "commission": round(commission, 4),
            "funding_fee": round(funding_fee, 4),
            "funding_count": funding_count,
            "total_fees": round(total_fees, 4),
            "capital_after": round(self.capital, 4),
            "holding_hours": holding_hours,
        })
        self.store.add_trade(pos.symbol, pos.entry_time, exit_time, trade_data)

        fee_detail = f"comm={commission:.2f} fund={funding_fee:.2f}({funding_count}×)"
        self.store.log_event("CLOSE", pos.symbol,
            f"Closed @ {exit_price}, pnl={net_pnl:.2f} ({fee_detail}), {result}")
        logger.info("  %s %s pnl=%.2f [%s]", "✅" if net_pnl > 0 else "❌", pos.symbol, net_pnl, fee_detail)

    def add_position(self, symbol: str, add_price: float, add_time: str):
        positions = self.store.get_open_positions()
        pos = next((p for p in positions if p.symbol == symbol), None)
        if not pos: return

        add_margin = pos.position_size * add_price
        if self.capital < add_margin:
            logger.warning("Insufficient capital for add_position on %s", symbol)
            return

        self.capital -= add_margin
        self.store.set_state("capital", str(self.capital))

        # Align with backtest: add same contract size, new_avg = (entry*orig + add*add_size)/total_size
        original_size = pos.position_size
        add_size = original_size
        total_size = original_size + add_size
        pos.entry_price = (pos.entry_price * original_size + add_price * add_size) / total_size
        pos.position_size = total_size
        pos.invest_amount += add_margin
        pos.has_added_position = True
        pos.add_price = add_price
        pos.add_time = add_time
        base_hi = pos.highest_price if pos.highest_price is not None else pos.entry_price
        pos.highest_price = max(base_hi, add_price, pos.entry_price)
        self.store.save_position(pos)
        self.store.log_event("ADD", symbol, f"Added to {symbol} @ {add_price}, new avg={pos.entry_price:.4f}")
