"""
Phase 7 — Execution & Recovery.

Builds on Phase 6 (app.execution.engine's UNKNOWN handling and
duplicate-order protection, already covered by
tests/test_phase6_mock_execution.py) to add what Phase 6 didn't have:

  - Persistence: an UNKNOWN outcome (or the idempotency record of an
    already-FILLED order) must survive a process restart, via
    app.execution.state_store.SqliteExecutionStateStore.
  - Reconciliation: ExecutionEngine.reconcile_unknown() resolves an
    UNKNOWN execution automatically ONLY when the broker provides
    unambiguous evidence, and ExecutionEngine.resolve_unknown_execution()
    is the explicit, operator-driven path for every other case.
  - Recovery for close/modify: a connection loss during close, partial
    close, or modify must never be reported as success, and must never
    silently mutate broker-side state.

Per the "no trade is better than an unsafe trade" principle, most
assertions here are about what must NOT happen (no second order, no
false success, no silently mutated state) as much as what should.
"""
from __future__ import annotations

import pytest

from app.config.settings import TradingMode
from app.execution.engine import ExecutionEngine
from app.execution.state_store import STATUS_UNKNOWN, SqliteExecutionStateStore
from app.mt5.interface import EXECUTION_STATUS_UNKNOWN, OrderRequest
from app.mt5.mock_client import MockMT5Client
from app.risk.kill_switch import KillSwitch


def _client():
    c = MockMT5Client()
    c.connect()
    return c


def _engine(mode=TradingMode.LIVE, state_store=None, kill_switch=None):
    client = _client()
    spec = client.get_symbol_spec("XAUUSD")
    engine = ExecutionEngine(client, spec, mode, state_store=state_store, kill_switch=kill_switch)
    return engine, client, spec


def _spy(client, method_name):
    """Wraps client.<method_name> to count calls without changing
    behavior, and returns the counter dict."""
    calls = {"n": 0}
    original = getattr(client, method_name)

    def wrapper(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    setattr(client, method_name, wrapper)
    return calls


# =====================================================================
# 1-8: baseline outcomes still hold with the new state store wired in
# (Phase 6 already covers these in depth; these confirm Phase 7's
# plumbing change didn't alter any of them).
# =====================================================================
def test_successful_execution_remains_successful():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-ok-1")
    assert result.success is True
    assert pos is not None and pos.ticket == result.order_id


def test_deterministic_rejection_remains_rejected():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_order_rejection("BROKER_SERVER_REJECTED: test")
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-rej-1")
    assert result.success is False
    assert pos is None
    assert engine.unresolved_executions() == []  # a plain rejection was never "UNKNOWN"


def test_unknown_execution_is_preserved_not_collapsed_to_rejected():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result()
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-unk-1")
    assert result.success is False
    assert result.raw.get("status") == EXECUTION_STATUS_UNKNOWN
    records = engine.unresolved_executions()
    assert len(records) == 1
    assert records[0].status == STATUS_UNKNOWN
    assert records[0].client_order_id == "p7-unk-1"


def test_unknown_execution_cannot_be_automatically_retried():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result()
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-unk-2")

    calls = _spy(client, "submit_order")
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-unk-2")
    assert result.success is False
    assert pos is None
    assert calls["n"] == 0  # never even reached the broker a second time


def test_duplicate_client_order_id_is_rejected():
    engine, client, spec = _engine(TradingMode.LIVE)
    first, pos1 = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-dup-1")
    second, pos2 = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-dup-1")
    assert first.success and pos1 is not None
    assert not second.success and pos2 is None
    assert second.comment == "DUPLICATE_ORDER_REJECTED"


def test_connection_loss_before_order_submission():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_connection_lost(applies_to="submit_order")
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-connbefore-1")
    assert result.success is False
    assert pos is None
    assert result.raw.get("status") == EXECUTION_STATUS_UNKNOWN
    assert engine.unresolved_executions()[0].client_order_id == "p7-connbefore-1"


def test_connection_loss_during_or_after_order_submission():
    """Modeled the same way as 'before' at this layer -- from outside
    the client, ExecutionEngine cannot distinguish WHEN a connection
    dropped, only that it did; both must fail closed to UNKNOWN, never
    to a false success or a false REJECTED."""
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result()  # the mock's own model of "sent, confirmation lost"
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-connduring-1")
    assert result.success is False
    assert pos is None
    assert result.raw.get("status") == EXECUTION_STATUS_UNKNOWN


def test_timeout_uncertain_result_via_exception_from_client():
    """submit_order() raising outright (e.g. a socket timeout) must be
    treated identically to an explicit UNKNOWN status -- not as a
    crash, not as a rejection, not as a success."""
    engine, client, spec = _engine(TradingMode.LIVE)

    def boom(*args, **kwargs):
        raise TimeoutError("mock broker: timed out waiting for a response")

    client.submit_order = boom
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-timeout-1")
    assert result.success is False
    assert pos is None
    assert result.raw.get("status") == EXECUTION_STATUS_UNKNOWN
    assert engine.unresolved_executions()[0].client_order_id == "p7-timeout-1"


# =====================================================================
# 9-10: reconciliation against broker state
# =====================================================================
def test_unknown_followed_by_broker_reconciliation_showing_filled():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result(broker_filled=True)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-recon-filled-1")
    assert result.success is False
    assert pos is None
    assert len(engine.unresolved_executions()) == 1

    outcomes = engine.reconcile_unknown()
    assert len(outcomes) == 1
    assert outcomes[0].client_order_id == "p7-recon-filled-1"
    assert outcomes[0].outcome == "RESOLVED_FILLED"
    assert outcomes[0].ticket is not None
    assert engine.unresolved_executions() == []

    # A matching open position really exists at the broker now (the mock's
    # ground truth), consistent with the resolution:
    broker_tickets = {p.ticket for p in client.get_open_positions(spec.name)}
    assert outcomes[0].ticket in broker_tickets

    # And the resolved record now behaves exactly like an ordinary FILLED
    # entry: the same client_order_id is blocked forever, never silently
    # allowed to create a second order for the same logical trade.
    calls = _spy(client, "submit_order")
    second, pos2 = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-recon-filled-1")
    assert not second.success and pos2 is None
    assert second.comment == "DUPLICATE_ORDER_REJECTED"
    assert second.order_id == outcomes[0].ticket
    assert calls["n"] == 0


def test_unknown_followed_by_broker_reconciliation_showing_not_found():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result(broker_filled=False)
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-recon-nf-1")

    outcomes = engine.reconcile_unknown()
    assert len(outcomes) == 1
    assert outcomes[0].client_order_id == "p7-recon-nf-1"
    # Genuinely ambiguous (never happened vs. filled-then-closed) --
    # reconcile_unknown() must NOT guess. It stays UNKNOWN.
    assert outcomes[0].outcome == "STILL_UNKNOWN"
    assert engine.unresolved_executions()[0].client_order_id == "p7-recon-nf-1"

    # Still blocks resubmission -- automatic reconciliation alone is never
    # enough to unblock an ambiguous case.
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-recon-nf-1")
    assert not result.success and pos is None


def test_reconcile_unknown_matches_long_client_order_id_via_hash_fallback():
    """client_order_ids too long for the raw comment tag must still be
    matchable -- via the deterministic hash fallback in
    ExecutionEngine._broker_comment_tag -- not silently give up."""
    long_id = "XAUUSD-2026-09-18T10:15:30.123456-momentum-strategy-v3"
    assert len(long_id) > 26  # forces the hash fallback, not the raw-id path
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result(broker_filled=True)
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, long_id)

    outcomes = engine.reconcile_unknown()
    assert len(outcomes) == 1
    assert outcomes[0].outcome == "RESOLVED_FILLED"


# =====================================================================
# 11: unresolved UNKNOWN remains unresolved (no silent resolution ever
# happens on its own, e.g. from merely calling reconcile() or from time
# passing).
# =====================================================================
def test_unresolved_unknown_remains_unresolved_until_explicitly_handled():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result()
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-stays-unk-1")

    engine.reconcile()  # the plain, pre-Phase-7 reconcile() -- must not resolve anything
    assert engine.unresolved_executions()[0].status == STATUS_UNKNOWN
    assert engine.unresolved_executions()[0].client_order_id == "p7-stays-unk-1"


def test_resolve_unknown_execution_requires_an_actual_unresolved_record():
    engine, client, spec = _engine(TradingMode.LIVE)
    with pytest.raises(ValueError):
        engine.resolve_unknown_execution("never-submitted", ticket=None, note="n/a")

    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-already-filled-1")
    with pytest.raises(ValueError):
        # Already FILLED, not UNKNOWN -- nothing to resolve.
        engine.resolve_unknown_execution("p7-already-filled-1", ticket=None, note="n/a")


def test_resolve_unknown_execution_not_found_unblocks_same_client_order_id():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result()
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-resolve-nf-1")

    engine.resolve_unknown_execution(
        "p7-resolve-nf-1", ticket=None,
        note="Confirmed via broker terminal trade history: this order never reached the broker.",
    )
    assert engine.unresolved_executions() == []

    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-resolve-nf-1")
    assert result.success is True
    assert pos is not None


def test_resolve_unknown_execution_filled_permanently_blocks_same_client_order_id():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result()
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-resolve-filled-1")

    engine.resolve_unknown_execution(
        "p7-resolve-filled-1", ticket=424242,
        note="Confirmed FILLED via broker trade history export.",
    )
    assert engine.unresolved_executions() == []

    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-resolve-filled-1")
    assert result.success is False
    assert pos is None
    assert result.comment == "DUPLICATE_ORDER_REJECTED"
    assert result.order_id == 424242


# =====================================================================
# 12: restart preserves unresolved execution if persistent state is used
# =====================================================================
def test_restart_preserves_unresolved_unknown_execution(tmp_path):
    db_path = str(tmp_path / "execution_state.db")

    store1 = SqliteExecutionStateStore(db_path)
    engine1, client1, spec = _engine(TradingMode.LIVE, state_store=store1)
    client1.queue_unknown_execution_result()
    engine1.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-restart-1")
    assert len(engine1.unresolved_executions()) == 1

    # Simulate a full process restart: a fresh store instance pointed at
    # the same file, a fresh (mock) broker connection, a fresh engine --
    # nothing carried over in memory.
    store2 = SqliteExecutionStateStore(db_path)
    engine2, client2, _ = _engine(TradingMode.LIVE, state_store=store2)
    recovered = engine2.unresolved_executions()
    assert len(recovered) == 1
    assert recovered[0].client_order_id == "p7-restart-1"
    assert recovered[0].status == STATUS_UNKNOWN

    # And it still blocks a blind resubmission post-restart, never
    # reaching the broker a second time for the same logical order.
    calls = _spy(client2, "submit_order")
    result, pos = engine2.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-restart-1")
    assert result.success is False
    assert pos is None
    assert calls["n"] == 0


def test_restart_preserves_filled_idempotency_protection_too():
    """Not just UNKNOWN entries -- an already-FILLED order's
    client_order_id must also stay blocked after a restart, or a
    resubmission could create a genuine second position."""
    import tempfile
    db_path = tempfile.mktemp(suffix=".db")

    store1 = SqliteExecutionStateStore(db_path)
    engine1, client1, spec = _engine(TradingMode.LIVE, state_store=store1)
    first, pos1 = engine1.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-restart-filled-1")
    assert first.success

    store2 = SqliteExecutionStateStore(db_path)
    engine2, client2, _ = _engine(TradingMode.LIVE, state_store=store2)
    calls = _spy(client2, "submit_order")
    second, pos2 = engine2.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-restart-filled-1")
    assert second.success is False
    assert pos2 is None
    assert second.order_id == first.order_id
    assert calls["n"] == 0


def test_restart_recovery_fails_closed_if_persisted_state_cannot_be_read(tmp_path, monkeypatch):
    """If the persisted execution state is unreadable, recovery must
    not silently proceed as though nothing were unresolved -- exercised
    directly against the store, and via the startup check that wraps
    it (app.safety.startup_check._check_execution_recovery)."""
    from app.safety.startup_check import _check_execution_recovery, CheckStatus, Severity

    db_path = str(tmp_path / "execution_state.db")
    store = SqliteExecutionStateStore(db_path)

    def boom():
        raise RuntimeError("disk read error")

    monkeypatch.setattr(store, "unresolved", boom)
    with pytest.raises(RuntimeError):
        store.unresolved()

    check = _check_execution_recovery(store)
    assert check.status == CheckStatus.FAIL
    assert check.severity == Severity.CRITICAL


# =====================================================================
# 13: local/broker discrepancies are detected
# =====================================================================
def test_reconcile_detects_locally_filled_ticket_no_longer_open_at_broker():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-mismatch-1")
    assert result.success
    # Position closed directly at the broker, bypassing the engine
    # entirely (e.g. closed manually on the broker's platform).
    close = client.close_position(result.order_id)
    assert close.success
    notes = engine.reconcile()
    assert any("no longer open at the broker" in n for n in notes)


def test_reconcile_detects_broker_position_untracked_locally():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.submit_order(OrderRequest(symbol=spec.name, direction="BUY", volume=0.1))
    notes = engine.reconcile()
    assert any("not tracked internally" in n for n in notes)


# =====================================================================
# 14: uncertain close does not delete/falsely-close a local position
# =====================================================================
def test_uncertain_live_close_does_not_report_success_and_position_stays_open():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-close-unc-1")
    assert result.success is True  # sanity: the position must actually exist before we test its recovery
    ticket = result.order_id

    client.queue_connection_lost(applies_to="close_position")
    close_result = engine.close_position(ticket)
    assert close_result.success is False
    assert "CLOSE_FAILED" in close_result.comment

    still_open = {p.ticket for p in engine.get_open_positions()}
    assert ticket in still_open


def test_uncertain_live_partial_close_does_not_reduce_broker_volume():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.3, 2600.0, 2700.0, "p7-partial-unc-1")
    assert result.success is True  # sanity: the position must actually exist before we test its recovery
    ticket = result.order_id

    client.queue_connection_lost(applies_to="close_position_partial")
    partial_result = engine.close_position_partial(ticket, 0.1)
    assert partial_result.success is False

    remaining = next(p for p in client.get_open_positions(spec.name) if p.ticket == ticket)
    assert remaining.volume == pytest.approx(0.3)


# =====================================================================
# 15: uncertain modify does not falsely update local/broker state
# =====================================================================
def test_uncertain_live_modify_does_not_report_success_and_sl_is_unchanged():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-modify-unc-1")
    assert result.success is True  # sanity: the position must actually exist before we test its recovery
    ticket = result.order_id
    original_sl = next(p.stop_loss for p in client.get_open_positions(spec.name) if p.ticket == ticket)

    client.queue_connection_lost(applies_to="modify_position")
    modify_result = engine.modify_position(ticket, stop_loss=2550.0, take_profit=None)
    assert modify_result.success is False
    assert "MODIFY_FAILED" in modify_result.comment

    after_sl = next(p.stop_loss for p in client.get_open_positions(spec.name) if p.ticket == ticket)
    assert after_sl == original_sl


# =====================================================================
# 16: reconciliation never creates a new order
# =====================================================================
def test_reconcile_and_reconcile_unknown_never_submit_a_new_order():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result()
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-no-new-order-1")

    calls = _spy(client, "submit_order")
    engine.reconcile()
    engine.reconcile_unknown()
    assert calls["n"] == 0


# =====================================================================
# 17-19: safety gates remain enforced alongside the new recovery wiring
# =====================================================================
def test_kill_switch_still_blocks_execution_with_persistent_state_store(tmp_path):
    ks = KillSwitch(str(tmp_path / "kill_switch_state.json"))
    ks.activate("phase7 test halt", activated_by="tester")
    store = SqliteExecutionStateStore(str(tmp_path / "execution_state.db"))
    engine, client, spec = _engine(TradingMode.LIVE, state_store=store, kill_switch=ks)

    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-ks-1")
    assert result.success is False
    assert pos is None
    assert "EXECUTION_SAFETY_GATE_REJECTED" in result.comment
    # Blocked at the gate -- nothing was even attempted, so nothing needs
    # reconciling.
    assert engine.unresolved_executions() == []


def test_safety_gate_volume_limits_still_enforced_after_phase7_wiring():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.0001, 2600.0, 2700.0, "p7-gate-vol-1")
    assert result.success is False
    assert pos is None
    assert "EXECUTION_SAFETY_GATE_REJECTED" in result.comment


def test_execution_safety_gate_still_wraps_every_live_submission_path():
    """ExecutionSafetyGate is evaluated unconditionally, before ANY
    client-submission path -- confirmed here via the volume check
    above and via test_execution_safety_gate.py's unit-level coverage
    of every other rejection reason (kill switch, account/symbol
    trade_allowed, stop-loss requirement). Phase 7 does not add any
    new path to client.submit_order that skips it."""
    engine, client, spec = _engine(TradingMode.LIVE)
    # No stop-loss provided where the gate requires one for a market
    # order -- still rejected exactly as before Phase 7.
    result, pos = engine.submit_market_order("BUY", 0.1, None, 2700.0, "p7-gate-sl-1")
    assert result.success is False
    assert pos is None
    assert "EXECUTION_SAFETY_GATE_REJECTED" in result.comment


# =====================================================================
# 20: PAPER mode never touches real MT5 execution, even with the new
# persistent store wired in.
# =====================================================================
def test_paper_mode_never_calls_submit_order_with_persistent_store(tmp_path):
    store = SqliteExecutionStateStore(str(tmp_path / "execution_state.db"))
    engine, client, spec = _engine(TradingMode.PAPER, state_store=store)
    calls = _spy(client, "submit_order")

    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-paper-1")
    assert result.success is True
    assert pos is not None
    assert calls["n"] == 0


def test_paper_mode_reconcile_and_reconcile_unknown_are_inert():
    engine, client, spec = _engine(TradingMode.PAPER)
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-paper-recon-1")
    assert engine.reconcile() == []
    assert engine.reconcile_unknown() == []


# =====================================================================
# Adversarial
# =====================================================================
def test_adversarial_client_order_id_never_bypassed_by_new_id_after_unknown():
    """The spec explicitly forbids inventing a new client_order_id to
    route around blocked UNKNOWN protection for the SAME logical order.
    This test doesn't (and can't) stop a caller from choosing a
    different id -- it documents and confirms the one guarantee this
    engine actually provides: reusing the ORIGINAL id stays blocked
    until reconciled, with no time-based or count-based escape hatch."""
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result()
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-adv-1")

    for _ in range(5):
        result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-adv-1")
        assert result.success is False
        assert pos is None


def test_adversarial_unknown_result_with_success_true_is_never_trusted():
    """Defense in depth already present since Phase 6: even if a client
    misbehaves and reports success=True alongside an UNKNOWN status,
    ExecutionEngine must never pass that success through, and Phase 7's
    state store must still record it as UNKNOWN, not FILLED."""
    from app.mt5.interface import OrderResult

    engine, client, spec = _engine(TradingMode.LIVE)

    def lying_submit(request):
        return OrderResult(
            success=True, order_id=123, deal_id=123, price=2650.0, volume=request.volume,
            retcode=10009, comment="claims success despite being UNKNOWN",
            raw={"status": EXECUTION_STATUS_UNKNOWN},
        )

    client.submit_order = lying_submit
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-adv-lying-1")
    assert result.success is False  # never trusted
    assert pos is None
    records = engine.unresolved_executions()
    assert len(records) == 1 and records[0].status == STATUS_UNKNOWN


def test_adversarial_reconcile_unknown_does_not_resolve_other_symbols_positions():
    """A position at the broker for a DIFFERENT client_order_id's tag
    must never be adopted as the resolution for an unrelated UNKNOWN
    record -- only an exact tag match counts."""
    engine, client, spec = _engine(TradingMode.LIVE)
    # An unrelated position exists at the broker, tagged for a
    # completely different logical order.
    client.submit_order(OrderRequest(
        symbol=spec.name, direction="BUY", volume=0.1, comment="coid:someone-elses-order",
    ))
    client.queue_unknown_execution_result(broker_filled=False)
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p7-adv-nomatch-1")

    outcomes = engine.reconcile_unknown()
    assert len(outcomes) == 1
    assert outcomes[0].outcome == "STILL_UNKNOWN"  # no false match to the unrelated position
