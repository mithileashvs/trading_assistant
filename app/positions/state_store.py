"""
Position Monitor State Store (Phase 8: Position Monitoring & Management).

Mirrors app.execution.state_store's pattern (Phase 7) exactly, for the
same reason: what the Position Monitor has already done to a given
ticket -- breakeven applied, partial exit taken (and how much), the
last stop-loss value it successfully pushed to the broker/paper
engine, and any not-yet-resolved (UNKNOWN) management action -- must
never be forgotten across a process restart, or idempotency (section
13: "at most once per position") silently breaks the moment the
process bounces.

Two implementations, same reasoning as ExecutionStateStore:
  - InMemoryPositionMonitorStateStore: the default -- matches
    PositionMonitor's pre-Phase-8 behavior (state lives only as long
    as the process does). Safe default for tests/ad-hoc scripts,
    exactly like InMemoryExecutionStateStore.
  - SqlitePositionMonitorStateStore: file-backed, for production
    wiring (TradingLoop), so a restart can recover monitor state
    (section 8) instead of starting blind.

A record's `pending_action` field holds at most one in-flight,
not-yet-resolved management action (Phase 8 section 9: modify/close
UNKNOWN handling). While a ticket has a pending action, the Position
Monitor must not generate a new one of the same kind, and callers must
not blindly retry it -- reconciliation is what resolves it, never a
blind guess.
"""
from __future__ import annotations

import abc
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class PendingAction:
    """A submitted-but-unresolved management action (Phase 8 section 9).
    Never converted to SUCCESS or REJECTED without evidence -- only
    reconciliation (or an explicit operator resolution) clears it."""
    action_type: str  # "MOVE_TO_BREAKEVEN" | "TRAIL_STOP" | "PARTIAL_EXIT" | "FULL_EXIT"
    requested_stop_loss: Optional[float] = None
    requested_partial_volume: Optional[float] = None
    note: str = ""
    submitted_at: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps({
            "action_type": self.action_type,
            "requested_stop_loss": self.requested_stop_loss,
            "requested_partial_volume": self.requested_partial_volume,
            "note": self.note,
            "submitted_at": self.submitted_at,
        })

    @staticmethod
    def from_json(raw: Optional[str]) -> Optional["PendingAction"]:
        if not raw:
            return None
        data = json.loads(raw)
        return PendingAction(**data)


@dataclass
class PositionMonitorRecord:
    ticket: int
    symbol: Optional[str] = None
    direction: Optional[str] = None
    initial_volume: Optional[float] = None  # volume at first observation -- partial-exit fraction is always of THIS
    breakeven_applied: bool = False
    partial_exit_taken: bool = False
    partial_exit_volume: Optional[float] = None  # volume actually confirmed closed so far
    last_confirmed_stop_loss: Optional[float] = None
    last_trailing_stop_applied: Optional[float] = None
    pending_action: Optional[PendingAction] = None
    closed: bool = False  # confirmed closed (any reason) -- kept, not deleted, for audit continuity
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PositionMonitorStateStore(abc.ABC):
    """Everything PositionMonitor needs to persist/read back per-ticket
    monitoring state. put() is an upsert keyed on ticket."""

    @abc.abstractmethod
    def get(self, ticket: int) -> Optional[PositionMonitorRecord]: ...

    @abc.abstractmethod
    def put(self, record: PositionMonitorRecord) -> None: ...

    @abc.abstractmethod
    def all(self) -> list[PositionMonitorRecord]: ...

    @abc.abstractmethod
    def delete(self, ticket: int) -> None:
        """Only used to fully forget a ticket (e.g. paper-mode ticket
        reuse after N restarts is not a concern here); LIVE-mode
        recovery should prefer marking `closed=True` over deleting, to
        preserve the audit trail (section 7: "do not silently overwrite
        evidence")."""
        ...

    def pending(self) -> list[PositionMonitorRecord]:
        return [r for r in self.all() if r.pending_action is not None]


class InMemoryPositionMonitorStateStore(PositionMonitorStateStore):
    def __init__(self):
        self._records: dict[int, PositionMonitorRecord] = {}

    def get(self, ticket: int) -> Optional[PositionMonitorRecord]:
        return self._records.get(ticket)

    def put(self, record: PositionMonitorRecord) -> None:
        existing = self._records.get(record.ticket)
        now = datetime.now(timezone.utc)
        if record.created_at is None:
            record.created_at = existing.created_at if existing is not None else now
        record.updated_at = now
        self._records[record.ticket] = record

    def all(self) -> list[PositionMonitorRecord]:
        return list(self._records.values())

    def delete(self, ticket: int) -> None:
        self._records.pop(ticket, None)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS position_monitor_state (
    ticket INTEGER PRIMARY KEY,
    symbol TEXT,
    direction TEXT,
    initial_volume REAL,
    breakeven_applied INTEGER NOT NULL DEFAULT 0,
    partial_exit_taken INTEGER NOT NULL DEFAULT 0,
    partial_exit_volume REAL,
    last_confirmed_stop_loss REAL,
    last_trailing_stop_applied REAL,
    pending_action TEXT,
    closed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class SqlitePositionMonitorStateStore(PositionMonitorStateStore):
    """File-backed so position-monitor state (breakeven/partial-exit
    idempotency, unresolved management actions) survives a process
    restart (Phase 8 section 8). Same plain-stdlib-sqlite3 pattern as
    app.execution.state_store.SqliteExecutionStateStore and
    app.journal.journal.TradeJournal -- one instance per deployment
    should own a given db path."""

    def __init__(self, db_path: str = "./data/position_monitor_state.db"):
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

    def get(self, ticket: int) -> Optional[PositionMonitorRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM position_monitor_state WHERE ticket = ?", (ticket,)
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def put(self, record: PositionMonitorRecord) -> None:
        now = datetime.now(timezone.utc)
        updated_at = record.updated_at or now
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM position_monitor_state WHERE ticket = ?", (record.ticket,)
            ).fetchone()
            created_at = (
                datetime.fromisoformat(existing["created_at"]) if existing is not None
                else (record.created_at or now)
            )
            conn.execute(
                """INSERT INTO position_monitor_state
                   (ticket, symbol, direction, initial_volume, breakeven_applied, partial_exit_taken,
                    partial_exit_volume, last_confirmed_stop_loss, last_trailing_stop_applied,
                    pending_action, closed, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ticket) DO UPDATE SET
                     symbol=excluded.symbol, direction=excluded.direction,
                     initial_volume=excluded.initial_volume, breakeven_applied=excluded.breakeven_applied,
                     partial_exit_taken=excluded.partial_exit_taken, partial_exit_volume=excluded.partial_exit_volume,
                     last_confirmed_stop_loss=excluded.last_confirmed_stop_loss,
                     last_trailing_stop_applied=excluded.last_trailing_stop_applied,
                     pending_action=excluded.pending_action, closed=excluded.closed,
                     updated_at=excluded.updated_at""",
                (
                    record.ticket, record.symbol, record.direction, record.initial_volume,
                    int(record.breakeven_applied), int(record.partial_exit_taken), record.partial_exit_volume,
                    record.last_confirmed_stop_loss, record.last_trailing_stop_applied,
                    record.pending_action.to_json() if record.pending_action else None,
                    int(record.closed), created_at.isoformat(), updated_at.isoformat(),
                ),
            )

    def all(self) -> list[PositionMonitorRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM position_monitor_state ORDER BY created_at").fetchall()
        return [self._row_to_record(r) for r in rows]

    def delete(self, ticket: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM position_monitor_state WHERE ticket = ?", (ticket,))

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> PositionMonitorRecord:
        return PositionMonitorRecord(
            ticket=row["ticket"],
            symbol=row["symbol"],
            direction=row["direction"],
            initial_volume=row["initial_volume"],
            breakeven_applied=bool(row["breakeven_applied"]),
            partial_exit_taken=bool(row["partial_exit_taken"]),
            partial_exit_volume=row["partial_exit_volume"],
            last_confirmed_stop_loss=row["last_confirmed_stop_loss"],
            last_trailing_stop_applied=row["last_trailing_stop_applied"],
            pending_action=PendingAction.from_json(row["pending_action"]),
            closed=bool(row["closed"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
