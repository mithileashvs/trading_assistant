"""
Phase 9 — Journal & Audit System Tests.

Covers:
  1. AuditEvent data structure, defaults, UTC normalization, direction alias, serialization.
  2. Persistence and restart persistence across TradeJournal instances on the same SQLite file.
  3. Schema index verification for all indexed columns.
  4. Append-only immutability (no updates/deletions; row counts strictly grow).
  5. Idempotency (duplicate event_id returns existing record without duplicate insertion;
     distinct events are not accidentally merged).
  6. Failure semantics: safety-critical writes fail closed (AuditPersistenceError),
     informational writes fail open (logged, non-crashing).
  7. Deterministic query APIs: filtering by ticket, client_order_id, symbol, category,
     event_type, result_status, time range, pagination, order/position audit trails.
  8. Corrupted metadata handling (survives invalid JSON with _corrupted_raw).
  9. UNKNOWN resolution safety:
     - UNKNOWN is never resolved to REJECTED merely because ticket=None.
     - UNKNOWN becomes CONFIRMED only with evidence of execution.
     - UNKNOWN becomes REJECTED only with evidence of non-execution.
     - Unresolved checks preserve UNKNOWN status.
     - Resolutions append a NEW event; the original UNKNOWN event is NEVER mutated.
 10. Execution lifecycle auditing (INTENT -> GATE_DECISION -> SUBMISSION -> RESULT).
 11. Kill switch activation/deactivation auditing.
 12. Reconciliation discrepancy auditing (untracked broker positions, startup recovery).
 13. Position management auditing (breakeven, trailing, partial exit, full exit, SL/TP).
 14. PAPER mode safety (audited safely without real broker calls).
"""
from __future__ import annotations

import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config.settings import Settings, TradingMode
from app.execution.engine import ExecutionEngine, ManagedPosition
from app.execution.state_store import SqliteExecutionStateStore
from app.journal.journal import (
    AuditCategory,
    AuditEvent,
    AuditPersistenceError,
    AuditResultStatus,
    TradeJournal,
)
from app.mt5.interface import EXECUTION_STATUS_UNKNOWN, OrderResult, Position, Tick
from app.mt5.mock_client import MockMT5Client
from app.news.filter import NewsFilter, NewsState, NewsStatus
from app.positions.monitor import ActionType, PositionMonitor, PositionMonitorConfig
from app.positions.state_store import SqlitePositionMonitorStateStore
from app.risk.kill_switch import KillSwitch
from app.runtime.loop import TradingLoop
from app.signals.models import Signal, SignalDirection


# =====================================================================
# Fixtures and Helpers
# =====================================================================
@pytest.fixture()
def journal_path(tmp_path):
    return str(tmp_path / "test_audit_journal.db")


@pytest.fixture()
def journal(journal_path):
    return TradeJournal(journal_path)


class _AlwaysClearNews(NewsFilter):
    def check(self, at):
        return NewsStatus(state=NewsState.CLEAR, reason="Clear for test.")


def _client():
    c = MockMT5Client()
    c.connect()
    return c


# =====================================================================
# 1. AuditEvent Data Structure & Serialization
# =====================================================================
def test_audit_event_defaults_and_properties():
    evt = AuditEvent(
        event_type="ORDER_INTENT",
        category=AuditCategory.EXECUTION,
        source_component="ExecutionEngine",
        actor="TradingLoop",
        action="SUBMIT_ORDER",
        result_status=AuditResultStatus.PENDING,
        symbol="XAUUSD",
        order_type="BUY",
        lot_size=0.1,
        price=2650.0,
    )

    assert evt.event_id is not None
    assert len(evt.event_id) > 10  # Valid UUID string
    assert evt.timestamp.tzinfo == timezone.utc
    assert evt.direction == "BUY"

    # Test direction alias setter
    evt.direction = "SELL"
    assert evt.order_type == "SELL"
    assert evt.direction == "SELL"


def test_audit_event_timezone_normalization():
    # Naive datetime should be coerced to UTC
    naive_dt = datetime(2026, 9, 19, 12, 0, 0)
    evt = AuditEvent(
        event_type="TEST",
        category="SYSTEM",
        source_component="Test",
        actor="Tester",
        action="TEST",
        result_status="CONFIRMED",
        timestamp=naive_dt,
    )
    assert evt.timestamp.tzinfo == timezone.utc
    assert evt.timestamp.hour == 12


def test_audit_event_to_dict_and_from_dict():
    original = AuditEvent(
        event_id="custom-evt-123",
        timestamp=datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc),
        event_type="ORDER_RESULT",
        category=AuditCategory.EXECUTION,
        source_component="ExecutionEngine",
        actor="SYSTEM",
        action="ORDER_FILLED",
        result_status=AuditResultStatus.CONFIRMED,
        symbol="XAUUSD",
        order_type="BUY",
        lot_size=0.25,
        price=2650.50,
        stop_loss=2645.0,
        take_profit=2660.0,
        ticket=998877,
        client_order_id="cl-ord-001",
        reason="Market execution successful",
        metadata={"spread": 1.2, "slippage": 0.1},
        execution_latency_ms=45.2,
    )

    d = original.to_dict()
    assert d["event_id"] == "custom-evt-123"
    assert d["category"] in ("EXECUTION", "order")
    assert d["result_status"] == "CONFIRMED"
    assert d["metadata"]["spread"] == 1.2

    reconstructed = AuditEvent.from_dict(d)
    assert reconstructed.event_id == original.event_id
    assert reconstructed.event_type == original.event_type
    assert reconstructed.category == original.category
    assert reconstructed.result_status == original.result_status
    assert reconstructed.ticket == 998877
    assert reconstructed.metadata["spread"] == 1.2
    assert reconstructed.execution_latency_ms == 45.2


# =====================================================================
# 2. Persistence and Restart Persistence
# =====================================================================
def test_audit_event_persistence_and_restart(journal_path):
    j1 = TradeJournal(journal_path)
    evt = AuditEvent(
        event_id="persisted-evt-1",
        timestamp=datetime.now(timezone.utc),
        event_type="ORDER_SUBMISSION",
        category=AuditCategory.EXECUTION,
        source_component="ExecutionEngine",
        actor="SYSTEM",
        action="SUBMIT_MARKET_ORDER",
        result_status=AuditResultStatus.CONFIRMED,
        symbol="XAUUSD",
        order_type="BUY",
        lot_size=0.15,
        price=2655.20,
        ticket=1001,
        client_order_id="ord-abc-1",
        metadata={"test_run": True},
    )
    row_id = j1.log_audit_event(evt, critical=True)
    assert row_id is not None
    assert row_id > 0

    # Simulate restart with a fresh TradeJournal instance targeting the same DB file
    j2 = TradeJournal(journal_path)
    retrieved = j2.get_audit_event("persisted-evt-1")
    assert retrieved is not None
    assert retrieved.event_id == "persisted-evt-1"
    assert retrieved.event_type == "ORDER_SUBMISSION"
    assert retrieved.symbol == "XAUUSD"
    assert retrieved.ticket == 1001
    assert retrieved.lot_size == 0.15
    assert retrieved.metadata.get("test_run") is True


def test_schema_indexes_exist(journal_path):
    _ = TradeJournal(journal_path)
    with sqlite3.connect(journal_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_events';")
        indexes = {row[0] for row in cursor.fetchall()}

    expected_indexes = {
        "idx_audit_events_event_id",
        "idx_audit_events_timestamp",
        "idx_audit_events_ticket",
        "idx_audit_events_client_order_id",
        "idx_audit_events_category",
        "idx_audit_events_event_type",
        "idx_audit_events_result_status",
        "idx_audit_events_symbol",
    }
    for idx in expected_indexes:
        assert idx in indexes, f"Index {idx} not found in sqlite_master"


# =====================================================================
# 3. Append-Only Immutability & Resolution Behavior
# =====================================================================
def test_append_only_immutability(journal):
    # Log 3 events
    for i in range(3):
        journal.log_audit_event(AuditEvent(
            event_id=f"evt-{i}",
            event_type="TEST_EVENT",
            category=AuditCategory.SYSTEM,
            source_component="Test",
            actor="Tester",
            action="APPEND",
            result_status=AuditResultStatus.CONFIRMED,
        ))

    events = journal.all_audit_events()
    assert len(events) == 3

    # Resolve an event - verifies it appends a NEW event rather than updating the old one
    unknown_evt = AuditEvent(
        event_id="unknown-evt-1",
        event_type="ORDER_RESULT",
        category=AuditCategory.EXECUTION,
        source_component="ExecutionEngine",
        actor="SYSTEM",
        action="ORDER_SUBMISSION",
        result_status=AuditResultStatus.UNKNOWN,
        symbol="XAUUSD",
        ticket=None,
        client_order_id="cl-001",
    )
    journal.log_audit_event(unknown_evt, critical=True)
    assert len(journal.all_audit_events()) == 4

    # Now append resolution
    res_event = journal.append_unknown_resolution(
        original_event_id="unknown-evt-1",
        new_status=AuditResultStatus.CONFIRMED,
        actor="Operator",
        reason="Found ticket 7777 on broker terminal",
        ticket=7777,
        client_order_id="cl-001",
        symbol="XAUUSD",
    )
    assert res_event is not None

    # Verify count is now 5
    all_events = journal.all_audit_events()
    assert len(all_events) == 5

    # Original event must be UNTOUCHED
    orig = journal.get_audit_event("unknown-evt-1")
    assert orig is not None
    assert orig.result_status == AuditResultStatus.UNKNOWN
    assert orig.ticket is None

    # Resolution event is distinct and new
    last_event = all_events[-1]
    assert last_event.event_type == "UNKNOWN_RESOLVED"
    assert last_event.result_status == AuditResultStatus.CONFIRMED
    assert last_event.ticket == 7777
    assert last_event.metadata.get("original_event_id") == "unknown-evt-1"


# =====================================================================
# 4. Idempotency & Duplicate Event ID Protection
# =====================================================================
def test_idempotency_duplicate_event_id(journal):
    evt_id = "same-event-uuid-1"
    evt = AuditEvent(
        event_id=evt_id,
        event_type="ORDER_RESULT",
        category=AuditCategory.EXECUTION,
        source_component="ExecutionEngine",
        actor="SYSTEM",
        action="SUBMIT",
        result_status=AuditResultStatus.CONFIRMED,
        symbol="XAUUSD",
    )

    row1 = journal.log_audit_event(evt, critical=True)
    row2 = journal.log_audit_event(evt, critical=True)

    # Must return the same row ID and not create duplicate rows
    assert row1 == row2
    events = journal.all_audit_events()
    assert len(events) == 1
    assert events[0].event_id == evt_id


def test_distinct_events_with_same_type_not_merged(journal):
    # Two distinct order intents for XAUUSD at the same timestamp
    now = datetime.now(timezone.utc)
    evt1 = AuditEvent(
        event_id="event-A",
        timestamp=now,
        event_type="ORDER_INTENT",
        category=AuditCategory.EXECUTION,
        source_component="ExecutionEngine",
        actor="SYSTEM",
        action="SUBMIT",
        result_status=AuditResultStatus.PENDING,
        symbol="XAUUSD",
    )
    evt2 = AuditEvent(
        event_id="event-B",
        timestamp=now,
        event_type="ORDER_INTENT",
        category=AuditCategory.EXECUTION,
        source_component="ExecutionEngine",
        actor="SYSTEM",
        action="SUBMIT",
        result_status=AuditResultStatus.PENDING,
        symbol="XAUUSD",
    )

    r1 = journal.log_audit_event(evt1)
    r2 = journal.log_audit_event(evt2)
    assert r1 != r2

    events = journal.all_audit_events()
    assert len(events) == 2
    assert {e.event_id for e in events} == {"event-A", "event-B"}


# =====================================================================
# 5. Failure Semantics (Critical vs Informational)
# =====================================================================
def test_critical_failure_raises_audit_persistence_error(journal_path):
    j = TradeJournal(journal_path)

    # Sabotage the journal by closing its underlying connection or dropping table
    with sqlite3.connect(journal_path) as conn:
        conn.execute("DROP TABLE audit_events;")

    crit_event = AuditEvent(
        event_type="ORDER_RESULT",
        category=AuditCategory.EXECUTION,
        source_component="ExecutionEngine",
        actor="SYSTEM",
        action="ORDER_FILLED",
        result_status=AuditResultStatus.CONFIRMED,
    )

    # Critical=True must raise AuditPersistenceError (fails closed)
    with pytest.raises(AuditPersistenceError):
        j.log_audit_event(crit_event, critical=True)


def test_informational_failure_fails_open_without_raising(journal_path):
    j = TradeJournal(journal_path)

    # Sabotage table
    with sqlite3.connect(journal_path) as conn:
        conn.execute("DROP TABLE audit_events;")

    info_event = AuditEvent(
        event_type="SYSTEM_TELEMETRY",
        category=AuditCategory.SYSTEM,
        source_component="Monitor",
        actor="SYSTEM",
        action="HEARTBEAT",
        result_status=AuditResultStatus.CONFIRMED,
    )

    # Critical=False must return None and NOT raise (fails open)
    result = j.log_audit_event(info_event, critical=False)
    assert result is None


# =====================================================================
# 6. Corrupted Metadata Handling
# =====================================================================
def test_corrupted_metadata_parsing(journal, journal_path):
    # Directly insert corrupted JSON into metadata
    with sqlite3.connect(journal_path) as conn:
        conn.execute(
            """
            INSERT INTO audit_events (
                event_id, timestamp, category, event_type,
                result_status, metadata
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "corrupted-meta-1",
                datetime.now(timezone.utc).isoformat(),
                "system",
                "TEST_CORRUPT",
                "CONFIRMED",
                "{invalid_json_string:: missing quotes}",
            ),
        )

    evt = journal.get_audit_event("corrupted-meta-1")
    assert evt is not None
    assert "_corrupted_raw" in evt.metadata
    assert evt.metadata["_corrupted_raw"] == "{invalid_json_string:: missing quotes}"


# =====================================================================
# 7. Deterministic Query APIs
# =====================================================================
def test_query_apis_by_filters(journal):
    now = datetime.now(timezone.utc)

    e1 = AuditEvent(
        event_id="q-1",
        timestamp=now - timedelta(minutes=10),
        event_type="ORDER_INTENT",
        category=AuditCategory.EXECUTION,
        source_component="ExecutionEngine",
        actor="SYSTEM",
        action="INTENT",
        result_status=AuditResultStatus.PENDING,
        symbol="XAUUSD",
        ticket=101,
        client_order_id="cl-101",
    )
    e2 = AuditEvent(
        event_id="q-2",
        timestamp=now - timedelta(minutes=5),
        event_type="KILL_SWITCH_ACTIVATED",
        category=AuditCategory.KILL_SWITCH,
        source_component="KillSwitch",
        actor="RiskGuard",
        action="ACTIVATE",
        result_status=AuditResultStatus.CONFIRMED,
        symbol=None,
    )
    e3 = AuditEvent(
        event_id="q-3",
        timestamp=now,
        event_type="POSITION_BREAKEVEN_MOVED",
        category=AuditCategory.POSITION_MANAGEMENT,
        source_component="PositionMonitor",
        actor="SYSTEM",
        action="BREAKEVEN",
        result_status=AuditResultStatus.CONFIRMED,
        symbol="XAUUSD",
        ticket=101,
        client_order_id="cl-101",
    )

    journal.log_audit_event(e1)
    journal.log_audit_event(e2)
    journal.log_audit_event(e3)

    # Filter by ticket
    t_evts = journal.audit_events_by_ticket(101)
    assert len(t_evts) == 2
    assert [e.event_id for e in t_evts] == ["q-1", "q-3"]

    # Filter by client_order_id
    c_evts = journal.audit_events_by_client_order_id("cl-101")
    assert len(c_evts) == 2

    # Filter by category
    ks_evts = journal.audit_events_by_category(AuditCategory.KILL_SWITCH)
    assert len(ks_evts) == 1
    assert ks_evts[0].event_id == "q-2"

    # Filter by category string
    pm_evts = journal.audit_events_by_category("POSITION_MANAGEMENT")
    assert len(pm_evts) == 1
    assert pm_evts[0].event_id == "q-3"

    # Filter by result_status
    pending_evts = journal.audit_events_by_result_status(AuditResultStatus.PENDING)
    assert len(pending_evts) == 1
    assert pending_evts[0].event_id == "q-1"

    # Filter by event_type
    ks_type_evts = journal.audit_events_by_event_type("KILL_SWITCH_ACTIVATED")
    assert len(ks_type_evts) == 1

    # Filter in range
    range_evts = journal.audit_events_in_range(now - timedelta(minutes=7), now + timedelta(minutes=1))
    assert len(range_evts) == 2
    assert {e.event_id for e in range_evts} == {"q-2", "q-3"}

    # Recent audit events (limit)
    recent = journal.recent_audit_events(limit=2)
    assert len(recent) == 2
    assert recent[0].event_id == "q-3"  # Newest first

    # Order and position audit trails
    order_trail = journal.order_audit_trail(client_order_id="cl-101", ticket=101)
    assert len(order_trail) == 2
    assert [e.event_id for e in order_trail] == ["q-1", "q-3"]

    pos_trail = journal.position_audit_trail(ticket=101)
    assert len(pos_trail) == 2


def test_query_audit_events_pagination_and_sorting(journal):
    for i in range(10):
        journal.log_audit_event(AuditEvent(
            event_id=f"page-{i:02d}",
            event_type="PAGE_TEST",
            category=AuditCategory.SYSTEM,
            source_component="Test",
            actor="SYSTEM",
            action="PAGE",
            result_status=AuditResultStatus.CONFIRMED,
        ))

    # Test limit and offset
    page1 = journal.query_audit_events(limit=3, offset=0, order_desc=False)
    assert len(page1) == 3
    assert [e.event_id for e in page1] == ["page-00", "page-01", "page-02"]

    page2 = journal.query_audit_events(limit=3, offset=3, order_desc=False)
    assert len(page2) == 3
    assert [e.event_id for e in page2] == ["page-03", "page-04", "page-05"]

    # Test order_desc=True
    desc_page = journal.query_audit_events(limit=2, order_desc=True)
    assert len(desc_page) == 2
    assert desc_page[0].event_id == "page-09"
    assert desc_page[1].event_id == "page-08"


# =====================================================================
# 8. UNKNOWN Resolution Safety
# =====================================================================
def test_unknown_resolution_safety_preserves_unknown_when_no_evidence(tmp_path):
    """
    CRITICAL SAFETY REQUIREMENT:
    Do NOT resolve UNKNOWN to REJECTED merely because ticket=None.
    If neither execution nor rejection is established, preserve UNKNOWN.
    A resolution/check event must be appended as a NEW event.
    """
    db_path = str(tmp_path / "journal.db")
    j = TradeJournal(db_path)
    state_store = SqliteExecutionStateStore(str(tmp_path / "exec_state.db"))
    client = _client()
    spec = client.get_symbol_spec("XAUUSD")

    engine = ExecutionEngine(
        client=client,
        symbol_spec=spec,
        mode=TradingMode.LIVE,
        state_store=state_store,
        journal=j,
    )

    # Force an UNKNOWN execution by throwing an exception inside submit_order
    def fail_send(*args, **kwargs):
        raise ConnectionResetError("Connection lost during broker transmission")

    client.submit_order = fail_send

    res, pos = engine.submit_market_order(
        direction="BUY",
        volume=0.1,
        stop_loss=2645.0,
        take_profit=2660.0,
        client_order_id="unknown-test-1",
    )
    assert res.raw.get("status") == EXECUTION_STATUS_UNKNOWN
    assert pos is None

    # Check journal has UNKNOWN result
    initial_events = j.audit_events_by_client_order_id("unknown-test-1")
    assert len(initial_events) >= 3  # INTENT, SUBMISSION, RESULT
    result_evt = [e for e in initial_events if e.event_type == "ORDER_RESULT"][0]
    assert result_evt.result_status == AuditResultStatus.UNKNOWN

    # Now run reconcile_unknown when broker has NO matching positions
    results = engine.reconcile_unknown()

    # MUST NOT be resolved to REJECTED! Must remain UNKNOWN
    assert len(results) == 1
    assert results[0].outcome == "STILL_UNKNOWN"
    assert results[0].ticket is None

    # Check that a RECONCILIATION_CHECK event was appended, preserving UNKNOWN status
    events_after = j.audit_events_by_client_order_id("unknown-test-1")
    assert len(events_after) == len(initial_events) + 1
    check_evt = events_after[-1]
    assert check_evt.event_type == "RECONCILIATION_CHECK"
    assert check_evt.result_status == AuditResultStatus.UNKNOWN
    assert "No matching open position" in check_evt.reason

    # The original result event must still be UNKNOWN
    orig_check = j.get_audit_event(result_evt.event_id)
    assert orig_check.result_status == AuditResultStatus.UNKNOWN


def test_unknown_resolution_to_confirmed_when_evidence_found(tmp_path):
    """
    UNKNOWN becomes CONFIRMED only when independent reconciliation/evidence
    establishes that execution occurred.
    """
    db_path = str(tmp_path / "journal.db")
    j = TradeJournal(db_path)
    state_store = SqliteExecutionStateStore(str(tmp_path / "exec_state.db"))
    client = _client()
    spec = client.get_symbol_spec("XAUUSD")

    engine = ExecutionEngine(
        client=client,
        symbol_spec=spec,
        mode=TradingMode.LIVE,
        state_store=state_store,
        journal=j,
    )

    # Simulate UNKNOWN
    def fail_send(*args, **kwargs):
        raise TimeoutError("Broker timeout")

    client.submit_order = fail_send
    res, pos = engine.submit_market_order(
        direction="BUY",
        volume=0.1,
        stop_loss=2645.0,
        take_profit=2660.0,
        client_order_id="unknown-test-2",
    )
    assert res.raw.get("status") == EXECUTION_STATUS_UNKNOWN

    # Now broker terminal confirms the position actually opened with ticket 88888
    # Matching requires the broker comment tag
    coid_tag = engine._broker_comment_tag("unknown-test-2")
    pos_broker = Position(
        ticket=88888,
        symbol="XAUUSD",
        direction="BUY",
        volume=0.1,
        price_open=2650.0,
        price_current=2650.0,
        stop_loss=2645.0,
        take_profit=2660.0,
        profit=0.0,
        open_time=datetime.now(timezone.utc),
        comment=coid_tag,
    )
    client.get_open_positions = lambda symbol=None: [pos_broker]

    # Reconcile unknown
    results = engine.reconcile_unknown()
    assert len(results) == 1
    assert results[0].outcome == "RESOLVED_FILLED"
    assert results[0].ticket == 88888

    # Verify journal appended UNKNOWN_RESOLVED with CONFIRMED
    events = j.audit_events_by_client_order_id("unknown-test-2")
    res_evt = [e for e in events if e.event_type == "UNKNOWN_RESOLVED"][0]
    assert res_evt.result_status == AuditResultStatus.CONFIRMED
    assert res_evt.ticket == 88888


def test_unknown_resolution_explicit_rejection_only_with_evidence(tmp_path):
    """
    UNKNOWN may become REJECTED only when independent evidence establishes
    that the order was actually rejected/not executed (e.g. via explicit operator resolution).
    """
    db_path = str(tmp_path / "journal.db")
    j = TradeJournal(db_path)
    state_store = SqliteExecutionStateStore(str(tmp_path / "exec_state.db"))
    client = _client()
    spec = client.get_symbol_spec("XAUUSD")

    engine = ExecutionEngine(
        client=client,
        symbol_spec=spec,
        mode=TradingMode.LIVE,
        state_store=state_store,
        journal=j,
    )

    client.submit_order = lambda *a, **kw: (_ for _ in ()).throw(ConnectionResetError("drop"))
    res, pos = engine.submit_market_order(
        direction="BUY",
        volume=0.1,
        stop_loss=2645.0,
        take_profit=2660.0,
        client_order_id="unknown-test-3",
    )
    assert res.raw.get("status") == EXECUTION_STATUS_UNKNOWN

    # Explicit resolution by operator verifying broker order history showed REJECTED
    engine.resolve_unknown_execution(
        client_order_id="unknown-test-3",
        ticket=None,  # Verified not executed
        note="Broker logs show order was rejected for insufficient margin",
    )

    # Journal must have UNKNOWN_RESOLVED event with REJECTED status
    events = j.audit_events_by_client_order_id("unknown-test-3")
    res_evt = [e for e in events if e.event_type == "UNKNOWN_RESOLVED"][0]
    assert res_evt.result_status == AuditResultStatus.REJECTED
    assert "insufficient margin" in res_evt.reason

    # Operator attempt with ticket=None but WITHOUT evidence of rejection:
    # Must NEVER infer REJECTED merely because ticket is None. Must preserve UNKNOWN.
    res2, pos2 = engine.submit_market_order(
        direction="SELL",
        volume=0.1,
        stop_loss=2660.0,
        take_profit=2645.0,
        client_order_id="unknown-test-no-evidence",
    )
    assert res2.raw.get("status") == EXECUTION_STATUS_UNKNOWN

    engine.resolve_unknown_execution(
        client_order_id="unknown-test-no-evidence",
        ticket=None,
        note="Investigated broker terminal, status remains unclear",
    )
    events2 = j.audit_events_by_client_order_id("unknown-test-no-evidence")
    check_evts = [e for e in events2 if e.event_type == "RECONCILIATION_CHECK"]
    assert len(check_evts) == 1
    assert check_evts[0].result_status == AuditResultStatus.UNKNOWN
    # Engine must still track this as unresolved:
    assert any(rec.client_order_id == "unknown-test-no-evidence" for rec in engine.unresolved_executions())


# =====================================================================
# 9. Execution Lifecycle Auditing
# =====================================================================
def test_execution_engine_full_lifecycle_auditing(tmp_path):
    db_path = str(tmp_path / "journal.db")
    j = TradeJournal(db_path)
    client = _client()
    spec = client.get_symbol_spec("XAUUSD")

    engine = ExecutionEngine(
        client=client,
        symbol_spec=spec,
        mode=TradingMode.LIVE,
        journal=j,
    )

    res, pos = engine.submit_market_order(
        direction="BUY",
        volume=0.1,
        stop_loss=2645.0,
        take_profit=2660.0,
        client_order_id="lifecycle-ord-1",
    )
    assert res.success is True

    trail = j.order_audit_trail(client_order_id="lifecycle-ord-1")
    event_types = [e.event_type for e in trail]

    assert "ORDER_INTENT" in event_types
    assert "EXECUTION_SAFETY_GATE_DECISION" in event_types
    assert "ORDER_SUBMISSION" in event_types
    assert "ORDER_RESULT" in event_types

    # Order result must be confirmed and have ticket
    res_evt = [e for e in trail if e.event_type == "ORDER_RESULT"][0]
    assert res_evt.result_status == AuditResultStatus.CONFIRMED
    assert res_evt.ticket == res.order_id


def test_duplicate_order_rejection_audited(tmp_path):
    db_path = str(tmp_path / "journal.db")
    j = TradeJournal(db_path)
    state_store = SqliteExecutionStateStore(str(tmp_path / "exec_state.db"))
    client = _client()
    spec = client.get_symbol_spec("XAUUSD")

    engine = ExecutionEngine(
        client=client,
        symbol_spec=spec,
        mode=TradingMode.LIVE,
        state_store=state_store,
        journal=j,
    )

    # First submission succeeds
    res1, pos1 = engine.submit_market_order(
        direction="BUY",
        volume=0.1,
        stop_loss=2645.0,
        take_profit=2660.0,
        client_order_id="dup-ord-1",
    )
    assert res1.success is True

    # Second submission with same client_order_id must be rejected
    res2, pos2 = engine.submit_market_order(
        direction="BUY",
        volume=0.1,
        stop_loss=2645.0,
        take_profit=2660.0,
        client_order_id="dup-ord-1",
    )
    assert res2.success is False

    trail = j.order_audit_trail(client_order_id="dup-ord-1")
    rejected_evts = [e for e in trail if e.event_type == "ORDER_REJECTED"]
    assert len(rejected_evts) >= 1
    assert "duplicate" in rejected_evts[0].reason.lower()


# =====================================================================
# 10. Kill Switch Auditing
# =====================================================================
def test_kill_switch_activation_and_deactivation_auditing(tmp_path):
    db_path = str(tmp_path / "journal.db")
    j = TradeJournal(db_path)
    ks_path = str(tmp_path / "ks.json")
    ks = KillSwitch(ks_path, journal=j)

    ks.activate(actor="RiskManager", reason="Excessive volatility", close_positions=True)
    assert ks.is_active() is True

    ks.deactivate(actor="ComplianceOfficer", reason="Market stabilized")
    assert ks.is_active() is False

    events = j.audit_events_by_category(AuditCategory.KILL_SWITCH)
    assert len(events) == 2

    act_evt = events[0]
    assert act_evt.event_type == "KILL_SWITCH_ACTIVATED"
    assert act_evt.actor == "RiskManager"
    assert act_evt.reason == "Excessive volatility"
    assert act_evt.metadata.get("close_positions") is True

    deact_evt = events[1]
    assert deact_evt.event_type == "KILL_SWITCH_DEACTIVATED"
    assert deact_evt.actor == "ComplianceOfficer"
    assert deact_evt.reason == "Market stabilized"


# =====================================================================
# 11. Reconciliation Discrepancy Auditing
# =====================================================================
def test_reconciliation_discrepancy_auditing(tmp_path):
    db_path = str(tmp_path / "journal.db")
    j = TradeJournal(db_path)
    client = _client()
    spec = client.get_symbol_spec("XAUUSD")

    # Put a rogue position on the broker client that engine is not tracking
    rogue_pos = ManagedPosition(
        ticket=99999,
        symbol="XAUUSD",
        direction="BUY",
        volume=0.5,
        price_open=2650.0,
        stop_loss=2640.0,
        take_profit=2670.0,
        open_time=datetime.now(timezone.utc),
    )
    client.get_open_positions = lambda symbol=None: [rogue_pos]

    engine = ExecutionEngine(
        client=client,
        symbol_spec=spec,
        mode=TradingMode.LIVE,
        journal=j,
    )

    # Run reconcile
    discrepancies = engine.reconcile()
    assert len(discrepancies) == 1
    assert "99999" in discrepancies[0]

    # Journal must have RECONCILIATION_DISCREPANCY audit event
    events = j.audit_events_by_event_type("RECONCILIATION_DISCREPANCY")
    assert len(events) == 1
    assert events[0].ticket == 99999
    assert events[0].category == AuditCategory.RECONCILIATION


# =====================================================================
# 12. TradingLoop Startup Recovery & Safety Gate Auditing
# =====================================================================
def test_trading_loop_startup_recovery_audited(tmp_path):
    settings = Settings(_env_file=None, trading_mode=TradingMode.PAPER)
    client = _client()
    spec = client.get_symbol_spec("XAUUSD")
    journal = TradeJournal(str(tmp_path / "journal.db"))
    ks = KillSwitch(str(tmp_path / "ks.json"), journal=journal)
    exec_store = SqliteExecutionStateStore(str(tmp_path / "exec.db"))
    pos_store = SqlitePositionMonitorStateStore(str(tmp_path / "pos.db"))

    # Put untracked position on engine
    broker_pos = ManagedPosition(
        ticket=12345,
        symbol="XAUUSD",
        direction="BUY",
        volume=0.2,
        price_open=2650.0,
        stop_loss=2645.0,
        take_profit=2660.0,
        open_time=datetime.now(timezone.utc),
    )
    engine = ExecutionEngine(
        client=client,
        symbol_spec=spec,
        mode=TradingMode.PAPER,
        journal=journal,
        state_store=exec_store,
        kill_switch=ks,
    )
    engine._paper_positions[12345] = broker_pos

    # Initialize TradingLoop
    _ = TradingLoop(
        settings=settings,
        client=client,
        symbol_spec=spec,
        journal=journal,
        kill_switch=ks,
        execution_engine=engine,
        position_monitor_state_store=pos_store,
    )

    # Must log STARTUP_RECOVERY audit event
    events = journal.audit_events_by_event_type("STARTUP_RECOVERY")
    assert len(events) == 1
    assert events[0].category == AuditCategory.RECONCILIATION
    assert events[0].ticket == 12345
    import hashlib
    expected_digest = hashlib.sha256(f"12345_{events[0].metadata['discrepancy_type']}_{events[0].reason}".encode("utf-8")).hexdigest()[:16]
    assert events[0].event_id == f"startup_rec_12345_{events[0].metadata['discrepancy_type']}_{expected_digest}"


# =====================================================================
# 13. Position Management Auditing in Trading Loop
# =====================================================================
def test_position_actions_and_sl_tp_detection_audited(tmp_path):
    settings = Settings(_env_file=None, trading_mode=TradingMode.PAPER)
    client = _client()
    spec = client.get_symbol_spec("XAUUSD")
    journal = TradeJournal(str(tmp_path / "journal.db"))
    ks = KillSwitch(str(tmp_path / "ks.json"), journal=journal)
    exec_store = SqliteExecutionStateStore(str(tmp_path / "exec.db"))
    pos_store = SqlitePositionMonitorStateStore(str(tmp_path / "pos.db"))

    engine = ExecutionEngine(
        client=client,
        symbol_spec=spec,
        mode=TradingMode.PAPER,
        journal=journal,
        state_store=exec_store,
        kill_switch=ks,
    )

    loop = TradingLoop(
        settings=settings,
        client=client,
        symbol_spec=spec,
        journal=journal,
        kill_switch=ks,
        execution_engine=engine,
        position_monitor_state_store=pos_store,
    )

    # Open position on engine and register in position monitor
    pos = ManagedPosition(
        ticket=555,
        symbol="XAUUSD",
        direction="BUY",
        volume=0.2,
        price_open=2650.0,
        stop_loss=2645.0,
        take_profit=2670.0,
        open_time=datetime.now(timezone.utc),
    )
    engine._paper_positions[555] = pos

    # 1. Trigger breakeven by setting tick price high enough (entry + 1.0 * ATR = 2655.0)
    from app.runtime.loop import CycleSummary
    summary = CycleSummary(timestamp=datetime.now(timezone.utc))
    tick = Tick(symbol="XAUUSD", time=datetime.now(timezone.utc), bid=2656.0, ask=2656.2, last=2656.0, volume=1.0)
    loop._manage_position(pos, tick, summary)

    be_events = journal.audit_events_by_event_type("POSITION_MOVE_TO_BREAKEVEN")
    assert len(be_events) == 1
    assert be_events[0].ticket == 555
    assert be_events[0].category == AuditCategory.POSITION

    # 2. Trigger stop loss detection by tick dropping below stop_loss
    tick_sl = Tick(symbol="XAUUSD", time=datetime.now(timezone.utc), bid=2644.0, ask=2644.2, last=2644.0, volume=1.0)
    loop._manage_position(pos, tick_sl, summary)

    sl_events = journal.audit_events_by_event_type("STOP_LOSS_DETECTED")
    assert len(sl_events) == 1
    assert sl_events[0].ticket == 555
    assert sl_events[0].category == AuditCategory.POSITION


# =====================================================================
# 14. Paper Mode Safety & Auditing
# =====================================================================
def test_paper_mode_execution_safety_and_auditing(tmp_path):
    """
    In TradingMode.PAPER, execution is simulated safely, MT5 submit_order is never
    called, but all lifecycle events are thoroughly recorded in the audit trail.
    """
    db_path = str(tmp_path / "journal.db")
    j = TradeJournal(db_path)
    client = _client()
    spec = client.get_symbol_spec("XAUUSD")

    # Spy on client.submit_order to verify it is NEVER called in PAPER mode
    send_called = False

    def spy_send(*args, **kwargs):
        nonlocal send_called
        send_called = True
        return OrderResult(success=True, order_id=999, deal_id=999, price=2650.0, volume=0.1, retcode=0, comment="")

    client.submit_order = spy_send

    engine = ExecutionEngine(
        client=client,
        symbol_spec=spec,
        mode=TradingMode.PAPER,
        journal=j,
    )

    res, pos = engine.submit_market_order(
        direction="BUY",
        volume=0.1,
        stop_loss=2645.0,
        take_profit=2660.0,
        client_order_id="paper-ord-1",
    )
    assert res.success is True
    assert pos is not None
    assert pos.ticket is not None
    assert send_called is False  # Never called real broker in PAPER mode!

    trail = j.order_audit_trail(client_order_id="paper-ord-1")
    assert len(trail) >= 3
    res_evt = [e for e in trail if e.event_type == "ORDER_RESULT"][0]
    assert res_evt.result_status == AuditResultStatus.CONFIRMED
    assert res_evt.ticket == pos.ticket

