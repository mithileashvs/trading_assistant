"""
Trade Journal (section 23).

Persists every signal and trade to SQLite (per the tech stack in
section 2: "SQLite initially, with PostgreSQL-ready architecture" —
plain SQL through stdlib sqlite3 keeps the schema portable). Also
implements TradeHistoryProvider so the risk guards (Phase 5) can run
against real persisted history instead of the in-memory precursor once
this is wired into live/paper trading.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.risk.history import TradeHistoryProvider, TradeRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    regime TEXT,
    score INTEGER,
    entry REAL, stop_loss REAL, take_profit REAL,
    session TEXT,
    approved INTEGER NOT NULL,
    rejection_reasons TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    regime TEXT,
    score INTEGER,
    open_time TEXT NOT NULL,
    close_time TEXT,
    entry_price REAL NOT NULL,
    exit_price REAL,
    stop_loss REAL,
    take_profit REAL,
    lots REAL NOT NULL,
    risk_pct REAL,
    session TEXT,
    news_state TEXT,
    exit_reason TEXT,
    pnl REAL,
    r_multiple REAL,
    mae REAL,
    mfe REAL,
    broker_response TEXT,
    atr REAL, adx REAL, rsi REAL, ema_20 REAL, ema_50 REAL, ema_200 REAL
);

CREATE INDEX IF NOT EXISTS idx_trades_close_time ON trades(close_time);
CREATE INDEX IF NOT EXISTS idx_trades_open_time ON trades(open_time);
"""


@dataclass
class SignalLogEntry:
    timestamp: datetime
    symbol: str
    direction: str
    strategy: str
    regime: Optional[str]
    score: Optional[int]
    entry: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    session: Optional[str]
    approved: bool
    rejection_reasons: list[str]


@dataclass
class TradeLogEntry:
    symbol: str
    direction: str
    strategy: str
    regime: Optional[str]
    score: Optional[int]
    open_time: datetime
    entry_price: float
    lots: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_pct: Optional[float] = None
    session: Optional[str] = None
    news_state: Optional[str] = None
    atr: Optional[float] = None
    adx: Optional[float] = None
    rsi: Optional[float] = None
    ema_20: Optional[float] = None
    ema_50: Optional[float] = None
    ema_200: Optional[float] = None
    close_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None
    r_multiple: Optional[float] = None
    mae: Optional[float] = None
    mfe: Optional[float] = None
    broker_response: Optional[dict] = None


class TradeJournal(TradeHistoryProvider):
    def __init__(self, db_path: str = "./data/trading_journal.db"):
        self._path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- signals ------------------------------------------------------------
    def log_signal(self, entry: SignalLogEntry) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO signals
                   (timestamp, symbol, direction, strategy, regime, score, entry, stop_loss,
                    take_profit, session, approved, rejection_reasons)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.timestamp.isoformat(), entry.symbol, entry.direction, entry.strategy,
                    entry.regime, entry.score, entry.entry, entry.stop_loss, entry.take_profit,
                    entry.session, int(entry.approved), json.dumps(entry.rejection_reasons),
                ),
            )
            return cur.lastrowid

    # -- trades -------------------------------------------------------------
    def open_trade(self, entry: TradeLogEntry) -> int:
        """Record a newly-opened trade. Returns the trade's row id, to
        be passed to close_trade() when it exits."""
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO trades
                   (symbol, direction, strategy, regime, score, open_time, entry_price, lots,
                    stop_loss, take_profit, risk_pct, session, news_state,
                    atr, adx, rsi, ema_20, ema_50, ema_200)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.symbol, entry.direction, entry.strategy, entry.regime, entry.score,
                    entry.open_time.isoformat(), entry.entry_price, entry.lots,
                    entry.stop_loss, entry.take_profit, entry.risk_pct, entry.session, entry.news_state,
                    entry.atr, entry.adx, entry.rsi, entry.ema_20, entry.ema_50, entry.ema_200,
                ),
            )
            return cur.lastrowid

    def close_trade(
        self,
        trade_id: int,
        close_time: datetime,
        exit_price: float,
        exit_reason: str,
        pnl: float,
        r_multiple: Optional[float] = None,
        mae: Optional[float] = None,
        mfe: Optional[float] = None,
        broker_response: Optional[dict] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE trades SET close_time=?, exit_price=?, exit_reason=?, pnl=?,
                   r_multiple=?, mae=?, mfe=?, broker_response=? WHERE id=?""",
                (
                    close_time.isoformat(), exit_price, exit_reason, pnl, r_multiple, mae, mfe,
                    json.dumps(broker_response) if broker_response else None, trade_id,
                ),
            )

    def all_trades(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM trades ORDER BY open_time").fetchall()

    def open_trades(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM trades WHERE close_time IS NULL").fetchall()

    # -- TradeHistoryProvider interface (consumed by app.risk.guards) -----------
    def trades_since(self, since: datetime) -> list[TradeRecord]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE close_time IS NOT NULL AND close_time >= ? ORDER BY close_time",
                (since.isoformat(),),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def recent_trades(self, limit: int) -> list[TradeRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE close_time IS NOT NULL ORDER BY close_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(r) for r in reversed(rows)]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TradeRecord:
        return TradeRecord(
            closed_at=datetime.fromisoformat(row["close_time"]),
            symbol=row["symbol"],
            direction=row["direction"],
            pnl=row["pnl"] or 0.0,
            r_multiple=row["r_multiple"],
        )
