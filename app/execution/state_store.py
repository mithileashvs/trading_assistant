"""
Execution State Store (Phase 7: Execution & Recovery).

Persists what ExecutionEngine knows about each client_order_id it has
ever submitted -- specifically the two outcomes that must never be
forgotten, including across a process restart:

  - FILLED: the order definitely went through, with its ticket. A
    resubmission under the same client_order_id must be rejected
    forever (it would create a second position for the same logical
    order).
  - UNKNOWN: the outcome could not be determined (Phase 6). A
    resubmission under the same client_order_id must be blocked until
    the record is explicitly resolved (see ExecutionEngine.
    resolve_unknown_execution).

REJECTED outcomes are deliberately never recorded here, matching
ExecutionEngine's behavior since Phase 6 (see
tests/test_phase6_mock_execution.py::
test_ordinary_rejection_does_not_permanently_block_a_later_legitimate_resubmission):
a deterministic rejection means the logical order never happened at
the broker, so there is nothing to protect against re-trying under
the same id.

Two implementations:
  - InMemoryExecutionStateStore: the default, matching every
    ExecutionEngine's behavior before this phase -- state lives only
    as long as the process does. This is the safe default for tests
    and ad-hoc scripts, for the exact same reason ExecutionEngine's
    default kill_switch is a _NullKillSwitch rather than a
    file-backed one: a persistent default would mean every
    standalone ExecutionEngine (including every unit test) silently
    shares whatever happens to be on disk at a fixed default path,
    which is a real test-isolation and correctness hazard.
  - SqliteExecutionStateStore: file-backed, for production wiring
    (TradingLoop) that must survive a restart without forgetting an
    unresolved UNKNOWN execution or losing idempotency protection for
    an already-FILLED one. Mirrors app.journal.journal.TradeJournal's
    plain-stdlib-sqlite3 pattern rather than introducing a new
    persistence framework.
"""
from __future__ import annotations

import abc
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# -- status values -----------------------------------------------------
STATUS_FILLED = "FILLED"
STATUS_UNKNOWN = "UNKNOWN"
# Terminal resolutions applied to a previously-UNKNOWN record via
# ExecutionEngine.resolve_unknown_execution() (manual) or
# ExecutionEngine.reconcile_unknown() (automatic, only when the broker
# provides unambiguous evidence). Once resolved:
#   RESOLVED_FILLED     -- a real position exists; treated exactly like
#                           FILLED for duplicate protection (never
#                           resubmit under this client_order_id).
#   RESOLVED_NOT_FOUND  -- confirmed the order never reached/executed
#                           at the broker; treated like "never seen" --
#                           a resubmission under the SAME
#                           client_order_id is allowed. This is
#                           deliberate: the spec requires never
#                           inventing a new order id to bypass
#                           protection, so the same id must become
#                           usable again once (and only once) the
#                           uncertainty is genuinely resolved.
STATUS_RESOLVED_FILLED = "RESOLVED_FILLED"
STATUS_RESOLVED_NOT_FOUND = "RESOLVED_NOT_FOUND"

# Statuses that must continue to block a resubmission under the same
# client_order_id (see ExecutionEngine.submit_market_order).
BLOCKING_STATUSES = (STATUS_FILLED, STATUS_UNKNOWN, STATUS_RESOLVED_FILLED)


@dataclass
class ExecutionRecord:
    client_order_id: str
    status: str
    ticket: Optional[int]
    symbol: Optional[str] = None
    direction: Optional[str] = None
    volume: Optional[float] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    note: str = ""


class ExecutionStateStore(abc.ABC):
    """Everything ExecutionEngine needs to persist/read back
    client_order_id outcomes. Implementations must make put()
    effectively an upsert keyed on client_order_id."""

    @abc.abstractmethod
    def get(self, client_order_id: str) -> Optional[ExecutionRecord]: ...

    @abc.abstractmethod
    def put(self, record: ExecutionRecord) -> None: ...

    @abc.abstractmethod
    def all(self) -> list[ExecutionRecord]: ...

    def unresolved(self) -> list[ExecutionRecord]:
        """Records still awaiting reconciliation (Phase 7's
        EXECUTION_STATUS_UNKNOWN case). Default implementation filters
        all(); override for efficiency if a subclass can query this
        directly."""
        return [r for r in self.all() if r.status == STATUS_UNKNOWN]


class InMemoryExecutionStateStore(ExecutionStateStore):
    """Default store -- no persistence. Matches ExecutionEngine's
    pre-Phase-7 behavior exactly (an in-memory dict that resets on
    restart)."""

    def __init__(self):
        self._records: dict[str, ExecutionRecord] = {}

    def get(self, client_order_id: str) -> Optional[ExecutionRecord]:
        return self._records.get(client_order_id)

    def put(self, record: ExecutionRecord) -> None:
        existing = self._records.get(record.client_order_id)
        now = datetime.now(timezone.utc)
        if record.created_at is None:
            record.created_at = existing.created_at if existing is not None else now
        if record.updated_at is None:
            record.updated_at = now
        self._records[record.client_order_id] = record

    def all(self) -> list[ExecutionRecord]:
        return list(self._records.values())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_state (
    client_order_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    ticket INTEGER,
    symbol TEXT,
    direction TEXT,
    volume REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note TEXT
);
"""


class SqliteExecutionStateStore(ExecutionStateStore):
    """File-backed so an unresolved UNKNOWN execution -- or the
    idempotency record of an already-FILLED one -- survives a process
    restart (Phase 7's "RECOVERY AFTER RESTART" requirement). One
    instance per deployment should own a given db path; concurrent
    processes should share the path, not separate instances with
    divergent state (same convention as app.risk.kill_switch.
    KillSwitch)."""

    def __init__(self, db_path: str = "./data/execution_state.db"):
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

    def get(self, client_order_id: str) -> Optional[ExecutionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM execution_state WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def put(self, record: ExecutionRecord) -> None:
        now = datetime.now(timezone.utc)
        updated_at = record.updated_at or now
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM execution_state WHERE client_order_id = ?",
                (record.client_order_id,),
            ).fetchone()
            created_at = (
                datetime.fromisoformat(existing["created_at"]) if existing is not None
                else (record.created_at or now)
            )
            conn.execute(
                """INSERT INTO execution_state
                   (client_order_id, status, ticket, symbol, direction, volume, created_at, updated_at, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(client_order_id) DO UPDATE SET
                     status=excluded.status, ticket=excluded.ticket, symbol=excluded.symbol,
                     direction=excluded.direction, volume=excluded.volume, updated_at=excluded.updated_at,
                     note=excluded.note""",
                (
                    record.client_order_id, record.status, record.ticket, record.symbol, record.direction,
                    record.volume, created_at.isoformat(), updated_at.isoformat(), record.note,
                ),
            )

    def all(self) -> list[ExecutionRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM execution_state ORDER BY created_at").fetchall()
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            client_order_id=row["client_order_id"],
            status=row["status"],
            ticket=row["ticket"],
            symbol=row["symbol"],
            direction=row["direction"],
            volume=row["volume"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            note=row["note"] or "",
        )
