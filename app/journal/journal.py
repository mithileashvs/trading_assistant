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
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.risk.history import TradeHistoryProvider, TradeRecord

logger = logging.getLogger(__name__)

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

-- Phase 8: position-management audit trail (section 12). One row per
-- attempted management action (breakeven, trailing, partial exit,
-- full exit) -- including UNKNOWN/uncertain outcomes, which must stay
-- distinguishable from confirmed ones rather than being silently
-- dropped or merged with them.
CREATE TABLE IF NOT EXISTS position_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    ticket INTEGER NOT NULL,
    symbol TEXT,
    action TEXT NOT NULL,
    old_stop_loss REAL,
    new_stop_loss REAL,
    old_volume REAL,
    new_volume REAL,
    trigger_reason TEXT,
    r_multiple REAL,
    atr REAL,
    result_status TEXT NOT NULL,
    broker_comment TEXT
);

CREATE INDEX IF NOT EXISTS idx_position_actions_ticket ON position_actions(ticket);

-- Phase 9: persistent append-only audit trail.
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    category TEXT NOT NULL,
    symbol TEXT,
    ticket INTEGER,
    client_order_id TEXT,
    side TEXT,
    volume REAL,
    price REAL,
    stop_loss REAL,
    take_profit REAL,
    strategy TEXT,
    regime TEXT,
    result_status TEXT,
    reason TEXT,
    error TEXT,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_events_event_id ON audit_events(event_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_timestamp ON audit_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_events_ticket ON audit_events(ticket);
CREATE INDEX IF NOT EXISTS idx_audit_events_client_order_id ON audit_events(client_order_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_category ON audit_events(category);
CREATE INDEX IF NOT EXISTS idx_audit_events_event_type ON audit_events(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_events_result_status ON audit_events(result_status);
CREATE INDEX IF NOT EXISTS idx_audit_events_symbol ON audit_events(symbol);
"""


class AuditPersistenceError(RuntimeError):
    """Raised when persisting a safety-critical audit event fails."""
    pass


class AuditCategory:
    SYSTEM = "system"
    SIGNAL = "signal"
    RISK = "risk"
    ORDER = "order"
    EXECUTION = "order"
    POSITION = "position"
    POSITION_MANAGEMENT = "position"
    KILL_SWITCH = "kill_switch"
    RECOVERY = "recovery"
    RECONCILIATION = "recovery"
    AUDIT_ERROR = "audit_error"


class AuditResultStatus:
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"


@dataclass
class AuditEvent:
    """Phase 9: Persistent append-only audit event record.
    `result_status` is one of 'CONFIRMED', 'REJECTED', 'UNKNOWN', 'PENDING' (or None).
    """
    event_id: str
    timestamp: datetime
    event_type: str
    category: str
    symbol: Optional[str] = None
    ticket: Optional[int] = None
    client_order_id: Optional[str] = None
    side: Optional[str] = None
    volume: Optional[float] = None
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy: Optional[str] = None
    regime: Optional[str] = None
    result_status: Optional[str] = None
    reason: Optional[str] = None
    error: Optional[str] = None
    metadata: Optional[dict] = None
    source_component: Optional[str] = None
    actor: Optional[str] = None
    action: Optional[str] = None
    execution_latency_ms: Optional[float] = None

    def __init__(
        self,
        event_id: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        event_type: str = "",
        category: str = "",
        symbol: Optional[str] = None,
        ticket: Optional[int] = None,
        client_order_id: Optional[str] = None,
        side: Optional[str] = None,
        direction: Optional[str] = None,
        order_type: Optional[str] = None,
        volume: Optional[float] = None,
        lot_size: Optional[float] = None,
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        strategy: Optional[str] = None,
        regime: Optional[str] = None,
        result_status: Optional[str] = None,
        reason: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[dict] = None,
        source_component: Optional[str] = None,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        execution_latency_ms: Optional[float] = None,
    ):
        self.event_id = event_id or f"evt_{uuid.uuid4().hex}"
        if timestamp is None:
            self.timestamp = datetime.now(timezone.utc)
        elif timestamp.tzinfo is None:
            self.timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            self.timestamp = timestamp
        self.event_type = event_type
        self.category = category
        self.symbol = symbol
        self.ticket = ticket
        self.client_order_id = client_order_id
        resolved_side = side if side is not None else (direction if direction is not None else order_type)
        self.side = resolved_side
        self.volume = volume if volume is not None else lot_size
        self.price = price
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.strategy = strategy
        self.regime = regime
        self.result_status = result_status
        self.reason = reason
        self.error = error
        meta = dict(metadata) if metadata else {}
        if source_component is not None:
            meta["source_component"] = source_component
        if actor is not None:
            meta["actor"] = actor
        if action is not None:
            meta["action"] = action
        if execution_latency_ms is not None:
            meta["execution_latency_ms"] = execution_latency_ms
        self.metadata = meta if meta else None
        self.source_component = source_component or (meta.get("source_component") if meta else None)
        self.actor = actor or (meta.get("actor") if meta else None)
        self.action = action or (meta.get("action") if meta else None)
        self.execution_latency_ms = execution_latency_ms or (meta.get("execution_latency_ms") if meta else None)

    @property
    def direction(self) -> Optional[str]:
        return self.side

    @direction.setter
    def direction(self, val: Optional[str]) -> None:
        self.side = val

    @property
    def order_type(self) -> Optional[str]:
        return self.side

    @order_type.setter
    def order_type(self, val: Optional[str]) -> None:
        self.side = val

    @property
    def lot_size(self) -> Optional[float]:
        return self.volume

    @lot_size.setter
    def lot_size(self, val: Optional[float]) -> None:
        self.volume = val

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type,
            "category": self.category,
            "symbol": self.symbol,
            "ticket": self.ticket,
            "client_order_id": self.client_order_id,
            "side": self.side,
            "order_type": self.side,
            "volume": self.volume,
            "lot_size": self.volume,
            "price": self.price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "strategy": self.strategy,
            "regime": self.regime,
            "result_status": self.result_status,
            "reason": self.reason,
            "error": self.error,
            "metadata": self.metadata,
            "source_component": self.source_component,
            "actor": self.actor,
            "action": self.action,
            "execution_latency_ms": self.execution_latency_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuditEvent":
        ts = d.get("timestamp")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return cls(
            event_id=d.get("event_id"),
            timestamp=ts,
            event_type=d.get("event_type", ""),
            category=d.get("category", ""),
            symbol=d.get("symbol"),
            ticket=d.get("ticket"),
            client_order_id=d.get("client_order_id"),
            side=d.get("side"),
            direction=d.get("direction"),
            order_type=d.get("order_type"),
            volume=d.get("volume"),
            lot_size=d.get("lot_size"),
            price=d.get("price"),
            stop_loss=d.get("stop_loss"),
            take_profit=d.get("take_profit"),
            strategy=d.get("strategy"),
            regime=d.get("regime"),
            result_status=d.get("result_status"),
            reason=d.get("reason"),
            error=d.get("error"),
            metadata=d.get("metadata"),
            source_component=d.get("source_component"),
            actor=d.get("actor"),
            action=d.get("action"),
            execution_latency_ms=d.get("execution_latency_ms"),
        )


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
class PositionActionLogEntry:
    """Phase 8, section 12. `result_status` is one of "CONFIRMED",
    "REJECTED", or "UNKNOWN" -- an UNKNOWN row must never be confused
    with a CONFIRMED one when reading the audit trail back."""
    timestamp: datetime
    ticket: int
    action: str
    result_status: str
    symbol: Optional[str] = None
    old_stop_loss: Optional[float] = None
    new_stop_loss: Optional[float] = None
    old_volume: Optional[float] = None
    new_volume: Optional[float] = None
    trigger_reason: Optional[str] = None
    r_multiple: Optional[float] = None
    atr: Optional[float] = None
    broker_comment: Optional[str] = None


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

    # -- position management actions (Phase 8, section 12) ------------------
    def log_position_action(self, entry: PositionActionLogEntry) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO position_actions
                   (timestamp, ticket, symbol, action, old_stop_loss, new_stop_loss, old_volume,
                    new_volume, trigger_reason, r_multiple, atr, result_status, broker_comment)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.timestamp.isoformat(), entry.ticket, entry.symbol, entry.action,
                    entry.old_stop_loss, entry.new_stop_loss, entry.old_volume, entry.new_volume,
                    entry.trigger_reason, entry.r_multiple, entry.atr, entry.result_status,
                    entry.broker_comment,
                ),
            )
            return cur.lastrowid

    def position_actions_for_ticket(self, ticket: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM position_actions WHERE ticket = ? ORDER BY timestamp", (ticket,)
            ).fetchall()

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

    # -- Phase 9: Audit Trail (append-only) -----------------------------------
    def log_audit_event(self, entry: AuditEvent, critical: bool = False) -> Optional[int]:
        """Appends an event to the persistent audit trail.
        Idempotent: if an event with the same event_id was already persisted,
        returns the existing row id without creating a duplicate row.
        If critical=True and write fails, raises AuditPersistenceError.
        If critical=False and write fails, returns None (fail-open for informational writes).
        """
        meta_json = json.dumps(entry.metadata) if entry.metadata is not None else None
        ts_str = entry.timestamp.isoformat() if isinstance(entry.timestamp, datetime) else str(entry.timestamp)
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """INSERT INTO audit_events
                       (event_id, timestamp, event_type, category, symbol, ticket,
                        client_order_id, side, volume, price, stop_loss, take_profit,
                        strategy, regime, result_status, reason, error, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.event_id, ts_str, entry.event_type, entry.category,
                        entry.symbol, entry.ticket, entry.client_order_id, entry.side,
                        entry.volume, entry.price, entry.stop_loss, entry.take_profit,
                        entry.strategy, entry.regime, entry.result_status, entry.reason,
                        entry.error, meta_json,
                    ),
                )
                return cur.lastrowid
        except sqlite3.IntegrityError as exc:
            # UNIQUE constraint on event_id: return existing id for idempotency
            if "UNIQUE constraint failed: audit_events.event_id" in str(exc) or "audit_events.event_id" in str(exc):
                with self._connect() as conn:
                    existing = conn.execute(
                        "SELECT id FROM audit_events WHERE event_id = ?", (entry.event_id,)
                    ).fetchone()
                    if existing:
                        return existing["id"]
            if critical:
                raise AuditPersistenceError(f"Failed to persist safety-critical audit event {entry.event_id}: {exc}") from exc
            logger.warning(f"Failed to persist informational audit event {entry.event_id}: {exc}")
            return None
        except Exception as exc:
            if critical:
                raise AuditPersistenceError(f"Failed to persist safety-critical audit event {entry.event_id}: {exc}") from exc
            logger.warning(f"Failed to persist informational audit event {entry.event_id}: {exc}")
            return None

    def append_unknown_resolution(
        self,
        client_order_id: Optional[str] = None,
        ticket: Optional[int] = None,
        result_status: Optional[str] = None,
        reason: str = "",
        original_event_id: Optional[str] = None,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        metadata: Optional[dict] = None,
        actor: Optional[str] = None,
        new_status: Optional[str] = None,
    ) -> AuditEvent:
        """Appends a NEW resolution audit event for an UNKNOWN record.
        Strictly append-only: the original UNKNOWN event is NEVER mutated or deleted.
        `result_status` must be 'CONFIRMED', 'REJECTED', or 'UNKNOWN'.
        """
        effective_status = new_status or result_status
        if effective_status is None:
            effective_status = AuditResultStatus.CONFIRMED if (ticket is not None and ticket > 0) else AuditResultStatus.UNKNOWN
        if effective_status not in (AuditResultStatus.CONFIRMED, AuditResultStatus.REJECTED, AuditResultStatus.UNKNOWN):
            raise ValueError(
                f"Invalid result_status {effective_status!r}; must be one of CONFIRMED, REJECTED, UNKNOWN"
            )
        meta = dict(metadata or {})
        if original_event_id:
            meta["original_event_id"] = original_event_id
        if actor:
            meta["actor"] = actor

        res_event = AuditEvent(
            event_id=f"res_{uuid.uuid4().hex}",
            timestamp=datetime.now(timezone.utc),
            event_type="UNKNOWN_RESOLVED" if effective_status != AuditResultStatus.UNKNOWN else "RECONCILIATION_CHECK",
            category=AuditCategory.RECOVERY,
            symbol=symbol,
            ticket=ticket,
            client_order_id=client_order_id,
            side=side,
            result_status=effective_status,
            reason=reason,
            actor=actor,
            metadata=meta,
        )
        self.log_audit_event(res_event, critical=(effective_status != AuditResultStatus.UNKNOWN))
        return res_event

    def get_audit_event(self, event_id: str) -> Optional[AuditEvent]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM audit_events WHERE event_id = ?", (event_id,)).fetchone()
        return self._row_to_audit_event(row) if row else None

    def all_audit_events(self, limit: Optional[int] = None, reverse: bool = False) -> list[AuditEvent]:
        return self.query_audit_events(limit=limit, order_desc=reverse)

    def recent_audit_events(self, limit: int = 50, reverse: bool = False) -> list[AuditEvent]:
        evts = self.query_audit_events(limit=limit, order_desc=True)
        return list(reversed(evts)) if reverse else evts

    def audit_events_by_ticket(self, ticket: int) -> list[AuditEvent]:
        return self.query_audit_events(ticket=ticket)

    def audit_events_by_client_order_id(self, client_order_id: str) -> list[AuditEvent]:
        return self.query_audit_events(client_order_id=client_order_id)

    def audit_events_by_symbol(self, symbol: str) -> list[AuditEvent]:
        return self.query_audit_events(symbol=symbol)

    def audit_events_by_event_type(self, event_type: str) -> list[AuditEvent]:
        return self.query_audit_events(event_type=event_type)

    def audit_events_by_category(self, category: str) -> list[AuditEvent]:
        return self.query_audit_events(category=category)

    def audit_events_by_result_status(self, result_status: str) -> list[AuditEvent]:
        return self.query_audit_events(result_status=result_status)

    def audit_events_in_range(self, start: datetime, end: datetime) -> list[AuditEvent]:
        return self.query_audit_events(start=start, end=end)

    def order_audit_trail(self, client_order_id: Optional[str] = None, ticket: Optional[int] = None) -> list[AuditEvent]:
        if client_order_id is not None and ticket is not None:
            sql = "SELECT * FROM audit_events WHERE client_order_id = ? OR ticket = ? ORDER BY timestamp ASC, id ASC"
            with self._connect() as conn:
                rows = conn.execute(sql, (client_order_id, ticket)).fetchall()
            return [self._row_to_audit_event(r) for r in rows]
        if client_order_id is not None:
            return self.query_audit_events(client_order_id=client_order_id)
        if ticket is not None:
            return self.query_audit_events(ticket=ticket)
        return self.query_audit_events()

    def position_audit_trail(self, ticket: int) -> list[AuditEvent]:
        return self.query_audit_events(ticket=ticket)

    def query_audit_events(
        self,
        ticket: Optional[int] = None,
        client_order_id: Optional[str] = None,
        symbol: Optional[str] = None,
        category: Optional[str] = None,
        event_type: Optional[str] = None,
        result_status: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_desc: bool = False,
    ) -> list[AuditEvent]:
        clauses = []
        params = []
        if ticket is not None:
            clauses.append("ticket = ?")
            params.append(ticket)
        if client_order_id is not None:
            clauses.append("client_order_id = ?")
            params.append(client_order_id)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if category is not None:
            cat_str = getattr(category, "value", category)
            if hasattr(AuditCategory, str(cat_str).upper()):
                cat_str = getattr(AuditCategory, str(cat_str).upper())
            clauses.append("(category = ? OR category = ?)")
            params.extend([str(cat_str).lower(), str(cat_str).upper()])
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if result_status is not None:
            res_str = getattr(result_status, "value", result_status)
            clauses.append("(result_status = ? OR result_status = ?)")
            params.extend([str(res_str).upper(), str(res_str).lower()])
        if start is not None:
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            clauses.append("timestamp >= ?")
            params.append(start.isoformat())
        if end is not None:
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            clauses.append("timestamp <= ?")
            params.append(end.isoformat())

        sql = "SELECT * FROM audit_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        order_dir = "DESC" if order_desc else "ASC"
        sql += f" ORDER BY timestamp {order_dir}, id {order_dir}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
            if offset is not None:
                sql += f" OFFSET {int(offset)}"
        elif offset is not None:
            sql += f" LIMIT -1 OFFSET {int(offset)}"

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_audit_event(r) for r in rows]

    @staticmethod
    def _row_to_audit_event(row: sqlite3.Row) -> AuditEvent:
        ts_val = row["timestamp"]
        try:
            ts = datetime.fromisoformat(ts_val)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            ts = datetime.now(timezone.utc)

        meta_raw = row["metadata"]
        meta = None
        if meta_raw:
            try:
                meta = json.loads(meta_raw)
            except Exception:
                meta = {"_corrupted_raw": meta_raw}

        return AuditEvent(
            event_id=row["event_id"],
            timestamp=ts,
            event_type=row["event_type"],
            category=row["category"],
            symbol=row["symbol"],
            ticket=row["ticket"],
            client_order_id=row["client_order_id"],
            side=row["side"],
            volume=row["volume"],
            price=row["price"],
            stop_loss=row["stop_loss"],
            take_profit=row["take_profit"],
            strategy=row["strategy"],
            regime=row["regime"],
            result_status=row["result_status"],
            reason=row["reason"],
            error=row["error"],
            metadata=meta,
        )
