"""PaperStore — SQLite persistence for Moonshot paper trading.
"""

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

class MoonshotPosition(BaseModel):
    model_config = ConfigDict(extra="ignore")
    symbol: str
    entry_price: float
    entry_time: str
    invest_amount: float
    position_size: float
    leverage: float
    surge_pct: float
    entry_reason: str
    tp_price: float
    sl_price: float
    target_pct: float
    stop_loss_pct: float
    capital_before: float
    tp_initial_price: float | None = None  # 开仓时初始止盈价（供 CSV 对齐回测；tp_price 可能被 monitor 更新）
    signal_price: float | None = None  # 扫描/成交参考价，CSV「信号价格」
    current_price: float = 0.0
    profit_pct: float = 0.0
    unrealized_pnl: float = 0.0
    has_added_position: bool = False
    add_price: float | None = None
    add_time: str | None = None
    lowest_price: float | None = None
    highest_price: float | None = None  # 持仓以来最高价（空仓：越接近 SL 越危险）
    entry_account_ratio: float | None = None
    exit_account_ratio: float | None = None
    account_ratio_change: float | None = None
    entry_commission: float | None = None
    add_price_raw: float | None = None
    sell_surge_ratio: float | None = None
    yesterday_avg_hour_sell_volume: float | None = None

class PendingSignal(BaseModel):
    symbol: str
    pct_chg: float
    signal_date_str: str
    delay_reason: str

class PendingSupertrendSignal(BaseModel):
    symbol: str
    pct_chg: float
    entry_reason: str

class PaperStore:
    """Handles SQLite persistence for trades, equity, and state."""

    def __init__(self, db_path: str = "paper_trading.db"):
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_db()

    def _init_db(self):
        conn = self._conn
        with conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    data TEXT
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    entry_time TEXT,
                    exit_time TEXT,
                    data TEXT
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS equity (
                    timestamp TEXT PRIMARY KEY,
                    total_equity REAL,
                    cash REAL
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    event_type TEXT,
                    symbol TEXT,
                    message TEXT
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS scan_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_time TEXT,
                    symbol TEXT,
                    data TEXT
                )
            """)

    def save_position(self, pos: MoonshotPosition):
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO positions (symbol, data) VALUES (?, ?)", (pos.symbol, pos.model_dump_json()))

    def remove_position(self, symbol: str):
        with self._conn:
            self._conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))

    def get_open_positions(self) -> list[MoonshotPosition]:
        with self._conn:
            cursor = self._conn.execute("SELECT data FROM positions")
            positions = [MoonshotPosition.model_validate_json(row[0]) for row in cursor.fetchall()]
        # ISO-8601 字符串与时间点顺序一致；升序 = 先开仓的在前
        positions.sort(key=lambda p: p.entry_time)
        return positions

    def position_count(self) -> int:
        with self._conn:
            return self._conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]

    def add_trade(self, symbol: str, entry_time: str, exit_time: str, data: dict):
        with self._conn:
            self._conn.execute("INSERT INTO trades (symbol, entry_time, exit_time, data) VALUES (?, ?, ?, ?)",
                         (symbol, entry_time, exit_time, json.dumps(data)))

    def get_trades(self, limit: int = 50) -> list[dict]:
        with self._conn:
            cursor = self._conn.execute("SELECT data FROM trades ORDER BY id DESC LIMIT ?", (limit,))
            return [json.loads(row[0]) for row in cursor.fetchall()]

    def get_trade_count(self) -> int:
        with self._conn:
            return self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    def total_realized_pnl(self) -> float:
        """已平仓合计净盈亏（与 trades 中 net_pnl 一致；全表 SUM，供看板高频读）。"""
        with self._conn:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(CAST(COALESCE(json_extract(data, '$.net_pnl'), '0') AS REAL)), 0)
                FROM trades
                """
            ).fetchone()
        v = float(row[0]) if row and row[0] is not None else 0.0
        return round(v, 2)

    def set_state(self, key: str, value: str):
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))

    def get_state(self, key: str) -> str | None:
        with self._conn:
            row = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    def append_equity_snapshot(self, timestamp: str, total_equity: float, cash: float):
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO equity (timestamp, total_equity, cash) VALUES (?, ?, ?)",
                         (timestamp, total_equity, cash))

    def log_event(self, event_type: str, symbol: str, message: str):
        with self._conn:
            self._conn.execute("INSERT INTO events (timestamp, event_type, symbol, message) VALUES (?, ?, ?, ?)",
                         (datetime.now(UTC).isoformat(), event_type, symbol, message))

    def get_events(self, limit: int = 100) -> list[dict]:
        with self._conn:
            cursor = self._conn.execute("SELECT timestamp, event_type, symbol, message FROM events ORDER BY id DESC LIMIT ?", (limit,))
            return [{"timestamp": r[0], "event_type": r[1], "symbol": r[2], "message": r[3]} for r in cursor.fetchall()]

    def save_scan_signals(self, scan_time: str, details: list[dict]) -> None:
        """按 scan_time 全量保存信号明细（覆盖同一 scan_time 的旧记录）。"""
        with self._conn:
            self._conn.execute("DELETE FROM scan_signals WHERE scan_time = ?", (scan_time,))
            rows = [
                (scan_time, str(d.get("symbol", "")), json.dumps(d, ensure_ascii=False))
                for d in details
            ]
            self._conn.executemany(
                "INSERT INTO scan_signals (scan_time, symbol, data) VALUES (?, ?, ?)",
                rows,
            )

    def list_scan_times(self, limit: int = 50) -> list[str]:
        with self._conn:
            cursor = self._conn.execute(
                "SELECT DISTINCT scan_time FROM scan_signals ORDER BY scan_time DESC LIMIT ?",
                (limit,),
            )
            return [r[0] for r in cursor.fetchall()]

    def get_scan_signals(self, scan_time: str, limit: int = 5000) -> list[dict]:
        with self._conn:
            cursor = self._conn.execute(
                "SELECT data FROM scan_signals WHERE scan_time = ? ORDER BY symbol ASC LIMIT ?",
                (scan_time, limit),
            )
            return [json.loads(r[0]) for r in cursor.fetchall()]

    def save_pending_signal(self, sig: PendingSignal):
        pending = self.get_pending_signals()
        pending = [s for s in pending if s['symbol'] != sig.symbol]
        pending.append(sig.model_dump())
        self.set_state("pending_signals", json.dumps(pending))

    def remove_pending_signal(self, symbol: str):
        pending = self.get_pending_signals()
        pending = [s for s in pending if s['symbol'] != symbol]
        self.set_state("pending_signals", json.dumps(pending))

    def get_pending_signals(self) -> list[PendingSignal]:
        data = self.get_state("pending_signals")
        if not data: return []
        return [PendingSignal.model_validate(s) for s in json.loads(data)]

    def save_pending_st_signal(self, sig: PendingSupertrendSignal):
        pending = self.get_state("pending_st_signals")
        pending_list = json.loads(pending) if pending else []
        pending_list = [s for s in pending_list if s['symbol'] != sig.symbol]
        pending_list.append(sig.model_dump())
        self.set_state("pending_st_signals", json.dumps(pending_list))

    def remove_pending_st_signal(self, symbol: str):
        pending = self.get_state("pending_st_signals")
        if not pending: return
        pending_list = json.loads(pending)
        pending_list = [s for s in pending_list if s['symbol'] != symbol]
        self.set_state("pending_st_signals", json.dumps(pending_list))

    def get_pending_st_signals(self) -> list[PendingSupertrendSignal]:
        data = self.get_state("pending_st_signals")
        if not data: return []
        return [PendingSupertrendSignal.model_validate(s) for s in json.loads(data)]

    def get_equity_curve(self) -> list[dict]:
        with self._conn:
            cursor = self._conn.execute("SELECT timestamp, total_equity, cash FROM equity ORDER BY timestamp ASC")
            return [{"timestamp": r[0], "total_equity": r[1], "cash": r[2]} for r in cursor.fetchall()]
