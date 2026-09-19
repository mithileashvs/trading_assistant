"""
Paper State Store (Phase 12).

Provides persistence for paper trading sessions, simulated accounts, positions,
and orders using the project's existing SQLite infrastructure.
"""
from __future__ import annotations

import abc
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from app.paper.account import PaperAccount
from app.paper.positions import PaperPosition
from app.paper.session import PaperSession, PaperSessionConfig


class StateCorruptedError(RuntimeError):
    """Raised when persisted paper state fails validation or contains contradictions."""
    pass


class PaperStateStore(abc.ABC):
    """Abstract interface for persisting and recovering paper trading state."""

    @abc.abstractmethod
    def save_session(self, session: PaperSession) -> None: ...

    @abc.abstractmethod
    def get_session(self, session_id: str) -> Optional[PaperSession]: ...

    def load_session(self, session_id: str) -> Optional[PaperSession]:
        return self.get_session(session_id)

    @abc.abstractmethod
    def list_sessions(self, limit: int = 50) -> list[PaperSession]: ...

    @abc.abstractmethod
    def save_account(self, session_id: str, account: PaperAccount) -> None: ...

    @abc.abstractmethod
    def load_account(self, session_id: str) -> Optional[PaperAccount]: ...

    @abc.abstractmethod
    def save_positions(self, session_id: str, positions: list[PaperPosition]) -> None: ...

    @abc.abstractmethod
    def load_positions(self, session_id: str, is_closed: Optional[bool] = None) -> list[PaperPosition]: ...


class InMemoryPaperStateStore(PaperStateStore):
    """Ephemeral in-memory store for unit tests and stateless runs."""

    def __init__(self):
        self._sessions: dict[str, PaperSession] = {}
        self._accounts: dict[str, PaperAccount] = {}
        self._positions: dict[str, list[PaperPosition]] = {}

    def save_session(self, session: PaperSession) -> None:
        self._sessions[session.session_id] = session

    def get_session(self, session_id: str) -> Optional[PaperSession]:
        return self._sessions.get(session_id)

    load_session = get_session

    def list_sessions(self, limit: int = 50) -> list[PaperSession]:
        return list(self._sessions.values())[:limit]

    def save_account(self, session_id: str, account: PaperAccount) -> None:
        self._accounts[session_id] = account

    def load_account(self, session_id: str) -> Optional[PaperAccount]:
        return self._accounts.get(session_id)

    def save_positions(self, session_id: str, positions: list[PaperPosition]) -> None:
        self._positions[session_id] = list(positions)

    def load_positions(self, session_id: str, is_closed: Optional[bool] = None) -> list[PaperPosition]:
        all_pos = self._positions.get(session_id, [])
        if is_closed is None:
            return all_pos
        return [p for p in all_pos if p.is_closed == is_closed]


_PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_sessions (
    session_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL,
    metrics_json TEXT,
    bars_processed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS paper_account (
    session_id TEXT PRIMARY KEY,
    current_balance REAL NOT NULL,
    equity REAL NOT NULL,
    used_margin REAL DEFAULT 0.0,
    free_margin REAL NOT NULL,
    realized_pnl REAL DEFAULT 0.0,
    unrealized_pnl REAL DEFAULT 0.0,
    commission REAL DEFAULT 0.0,
    swap REAL DEFAULT 0.0,
    spread_cost REAL DEFAULT 0.0,
    slippage_cost REAL DEFAULT 0.0,
    daily_pnl REAL DEFAULT 0.0,
    weekly_pnl REAL DEFAULT 0.0,
    consecutive_losses INTEGER DEFAULT 0,
    consecutive_wins INTEGER DEFAULT 0,
    number_of_trades INTEGER DEFAULT 0,
    account_json TEXT NOT NULL,
    last_updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    ticket INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    volume REAL NOT NULL,
    price_open REAL NOT NULL,
    current_price REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    strategy TEXT,
    regime TEXT,
    open_time TEXT NOT NULL,
    close_time TEXT,
    is_closed INTEGER NOT NULL,
    realized_pnl REAL DEFAULT 0.0,
    unrealized_pnl REAL DEFAULT 0.0,
    position_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_positions_session ON paper_positions(session_id);
CREATE INDEX IF NOT EXISTS idx_paper_positions_closed ON paper_positions(session_id, is_closed);
"""


class SqlitePaperStateStore(PaperStateStore):
    """Persistent SQLite store reusing the project's existing SQLite database infrastructure."""

    def __init__(self, db_path: str = "data/trading_journal.db"):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_PAPER_SCHEMA)

    def save_session(self, session: PaperSession) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO paper_sessions (
                    session_id, symbol, status, config_json, metrics_json,
                    bars_processed, created_at, start_time, end_time, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    status=excluded.status,
                    metrics_json=excluded.metrics_json,
                    bars_processed=excluded.bars_processed,
                    start_time=excluded.start_time,
                    end_time=excluded.end_time,
                    error=excluded.error
                """,
                (
                    session.session_id,
                    session.config.symbol,
                    session.status,
                    json.dumps(session.config.to_dict()),
                    json.dumps(session.metrics),
                    session.bars_processed,
                    session.created_at.isoformat() if session.created_at else datetime.now(timezone.utc).isoformat(),
                    session.start_time.isoformat() if session.start_time else None,
                    session.end_time.isoformat() if session.end_time else None,
                    session.error,
                ),
            )

    def get_session(self, session_id: str) -> Optional[PaperSession]:
        with self._conn() as conn:
            cur = conn.execute("SELECT * FROM paper_sessions WHERE session_id = ?", (session_id,))
            row = cur.fetchone()
            if not row:
                return None
            try:
                cfg_dict = json.loads(row["config_json"])
                cfg = PaperSessionConfig.from_dict(cfg_dict)
                metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
                return PaperSession(
                    config=cfg,
                    created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(timezone.utc),
                    start_time=datetime.fromisoformat(row["start_time"]) if row["start_time"] else None,
                    end_time=datetime.fromisoformat(row["end_time"]) if row["end_time"] else None,
                    status=row["status"],
                    bars_processed=row["bars_processed"],
                    error=row["error"],
                    metrics=metrics,
                )
            except Exception as e:
                raise StateCorruptedError(f"Corrupted session state for session {session_id}: {e}") from e

    load_session = get_session

    def list_sessions(self, limit: int = 50) -> list[PaperSession]:
        with self._conn() as conn:
            cur = conn.execute("SELECT session_id FROM paper_sessions ORDER BY created_at DESC LIMIT ?", (limit,))
            rows = cur.fetchall()
            return [self.get_session(r["session_id"]) for r in rows if r["session_id"]]

    def save_account(self, session_id: str, account: PaperAccount) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO paper_account (
                    session_id, current_balance, equity, used_margin, free_margin,
                    realized_pnl, unrealized_pnl, commission, swap, spread_cost, slippage_cost,
                    daily_pnl, weekly_pnl, consecutive_losses, consecutive_wins, number_of_trades,
                    account_json, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    current_balance=excluded.current_balance,
                    equity=excluded.equity,
                    used_margin=excluded.used_margin,
                    free_margin=excluded.free_margin,
                    realized_pnl=excluded.realized_pnl,
                    unrealized_pnl=excluded.unrealized_pnl,
                    commission=excluded.commission,
                    swap=excluded.swap,
                    spread_cost=excluded.spread_cost,
                    slippage_cost=excluded.slippage_cost,
                    daily_pnl=excluded.daily_pnl,
                    weekly_pnl=excluded.weekly_pnl,
                    consecutive_losses=excluded.consecutive_losses,
                    consecutive_wins=excluded.consecutive_wins,
                    number_of_trades=excluded.number_of_trades,
                    account_json=excluded.account_json,
                    last_updated=excluded.last_updated
                """,
                (
                    session_id,
                    account.current_balance,
                    account.equity,
                    account.used_margin,
                    account.free_margin,
                    account.realized_pnl,
                    account.unrealized_pnl,
                    account.commission,
                    account.swap,
                    account.spread_cost,
                    account.slippage_cost,
                    account.daily_pnl,
                    account.weekly_pnl,
                    account.consecutive_losses,
                    account.consecutive_wins,
                    account.number_of_trades,
                    json.dumps(account.to_dict()),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def load_account(self, session_id: str) -> Optional[PaperAccount]:
        with self._conn() as conn:
            cur = conn.execute("SELECT account_json FROM paper_account WHERE session_id = ?", (session_id,))
            row = cur.fetchone()
            if not row:
                return None
            try:
                acc_dict = json.loads(row["account_json"])
                return PaperAccount.from_dict(acc_dict)
            except Exception as e:
                raise StateCorruptedError(f"Corrupted account state for session {session_id}: {e}") from e

    def save_positions(self, session_id: str, positions: list[PaperPosition]) -> None:
        with self._conn() as conn:
            for p in positions:
                conn.execute(
                    """
                    INSERT INTO paper_positions (
                        ticket, session_id, client_order_id, symbol, direction, volume,
                        price_open, current_price, stop_loss, take_profit, strategy, regime,
                        open_time, close_time, is_closed, realized_pnl, unrealized_pnl, position_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ticket) DO UPDATE SET
                        current_price=excluded.current_price,
                        stop_loss=excluded.stop_loss,
                        take_profit=excluded.take_profit,
                        close_time=excluded.close_time,
                        is_closed=excluded.is_closed,
                        realized_pnl=excluded.realized_pnl,
                        unrealized_pnl=excluded.unrealized_pnl,
                        position_json=excluded.position_json
                    """,
                    (
                        p.ticket,
                        session_id,
                        p.client_order_id,
                        p.symbol,
                        p.direction,
                        p.volume,
                        p.price_open,
                        p.current_price,
                        p.stop_loss,
                        p.take_profit,
                        p.strategy,
                        p.regime,
                        p.open_time.isoformat() if p.open_time else datetime.now(timezone.utc).isoformat(),
                        p.close_time.isoformat() if p.close_time else None,
                        1 if p.is_closed else 0,
                        p.realized_pnl,
                        p.unrealized_pnl,
                        json.dumps(p.to_dict()),
                    ),
                )

    def load_positions(self, session_id: str, is_closed: Optional[bool] = None) -> list[PaperPosition]:
        with self._conn() as conn:
            if is_closed is None:
                cur = conn.execute("SELECT position_json FROM paper_positions WHERE session_id = ?", (session_id,))
            else:
                cur = conn.execute(
                    "SELECT position_json FROM paper_positions WHERE session_id = ? AND is_closed = ?",
                    (session_id, 1 if is_closed else 0),
                )
            rows = cur.fetchall()
            positions = []
            for r in rows:
                try:
                    p_dict = json.loads(r["position_json"])
                    positions.append(PaperPosition.from_dict(p_dict))
                except Exception as e:
                    raise StateCorruptedError(f"Corrupted position state in session {session_id}: {e}") from e
            return positions
