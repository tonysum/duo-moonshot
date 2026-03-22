"""PaperStore — SQLite persistence for Moonshot paper trading.
"""

import sqlite3
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
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
    current_price: float = 0.0
    profit_pct: float = 0.0
    unrealized_pnl: float = 0.0
    has_added_position: bool = False
    add_price: Optional[float] = None
    add_time: Optional[str] = None
    lowest_price: Optional[float] = None
    entry_account_ratio: Optional[float] = None
    exit_account_ratio: Optional[float] = None
    account_ratio_change: Optional[float] = None

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
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    data TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    entry_time TEXT,
                    exit_time TEXT,
                    data TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS equity (
                    timestamp TEXT PRIMARY KEY,
                    total_equity REAL,
                    cash REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    event_type TEXT,
                    symbol TEXT,
                    message TEXT
                )
            """)

    def save_position(self, pos: MoonshotPosition):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO positions (symbol, data) VALUES (?, ?)", (pos.symbol, pos.model_dump_json()))

    def remove_position(self, symbol: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))

    def get_open_positions(self) -> list[MoonshotPosition]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT data FROM positions")
            return [MoonshotPosition.model_validate_json(row[0]) for row in cursor.fetchall()]

    def position_count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]

    def add_trade(self, symbol: str, entry_time: str, exit_time: str, data: dict):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO trades (symbol, entry_time, exit_time, data) VALUES (?, ?, ?, ?)",
                         (symbol, entry_time, exit_time, json.dumps(data)))

    def get_trades(self, limit: int = 50) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT data FROM trades ORDER BY id DESC LIMIT ?", (limit,))
            return [json.loads(row[0]) for row in cursor.fetchall()]

    def get_trade_count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    def set_state(self, key: str, value: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))

    def get_state(self, key: str) -> Optional[str]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    def append_equity_snapshot(self, timestamp: str, total_equity: float, cash: float):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO equity (timestamp, total_equity, cash) VALUES (?, ?, ?)",
                         (timestamp, total_equity, cash))

    def log_event(self, event_type: str, symbol: str, message: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO events (timestamp, event_type, symbol, message) VALUES (?, ?, ?, ?)",
                         (datetime.now(timezone.utc).isoformat(), event_type, symbol, message))

    def get_events(self, limit: int = 100) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT timestamp, event_type, symbol, message FROM events ORDER BY id DESC LIMIT ?", (limit,))
            return [{"timestamp": r[0], "event_type": r[1], "symbol": r[2], "message": r[3]} for r in cursor.fetchall()]

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
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT timestamp, total_equity, cash FROM equity ORDER BY timestamp ASC")
            return [{"timestamp": r[0], "total_equity": r[1], "cash": r[2]} for r in cursor.fetchall()]
