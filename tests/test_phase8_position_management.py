"""
Phase 8 — Position Monitoring & Management.

Covers what Phase 1-7 didn't: restart-safe idempotency for the
Position Monitor (breakeven/partial-exit/trailing), UNKNOWN handling
for modify/close/partial-close actions taken on an ALREADY-OPEN
position (distinct from Phase 6/7's UNKNOWN handling for NEW order
submission), reconciliation of local vs. broker state, and the
kill-switch "new risk vs. risk-reducing management" distinction.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone

import pytest

from app.config.settings import Settings, TradingMode
from app.execution.engine import ExecutionEngine, ManagedPosition
from app.execution.state_store import SqliteExecutionStateStore
from app.journal.journal import TradeJournal
from app.mt5.interface import EXECUTION_STATUS_UNKNOWN, Tick
from app.mt5.mock_client import MockMT5Client
from app.news.filter import NewsFilter, NewsState, NewsStatus
from app.positions.monitor import ActionType, PositionMonitor, PositionMonitorConfig
from app.positions.reconciliation import DiscrepancyType, reconcile_positions, recover_state
from app.positions.state_store import (
    InMemoryPositionMonitorStateStore,
    PendingAction,
    PositionMonitorRecord,
    SqlitePositionMonitorStateStore,
)
from app.risk.kill_switch import KillSwitch
from app.runtime.loop import TradingLoop
from app.signals.models import Signal, SignalDirection


# =====================================================================
# Shared fixtures/helpers
# =====================================================================
def _position(ticket=1, direction="BUY", entry=2650.0, stop=2645.0, volume=0.2, tp=None, symbol="XAUUSD"):
    return ManagedPosition(
        ticket=ticket, symbol=symbol, direction=direction, volume=volume, price_open=entry,
        stop_loss=stop, take_profit=tp if tp is not None else (entry + 50 if direction == "BUY" else entry - 50),
        open_time=datetime.now(timezone.utc),
    )


def _tick(bid, ask=None):
    ask = ask if ask is not None else bid + 0.2
    return Tick(symbol="XAUUSD", time=datetime.now(timezone.utc), bid=bid, ask=ask, last=bid, volume=1.0)


def _client():
    c = MockMT5Client()
    c.connect()
    return c


def _engine(mode=TradingMode.LIVE, state_store=None, kill_switch=None):
    client = _client()
    spec = client.get_symbol_spec("XAUUSD")
    engine = ExecutionEngine(client, spec, mode, state_store=state_store, kill_switch=kill_switch)
    return engine, client, spec


class _AlwaysClear(NewsFilter):
    def check(self, at):
        return NewsStatus(state=NewsState.CLEAR, reason="No blocking event (test).")


def _journal():
    return TradeJournal(tempfile.mktemp(suffix=".db"))


def _loop(settings=None, kill_switch=None, clear_news=False, position_monitor=None):
    settings = settings or Settings(_env_file=None)
    client = MockMT5Client()
    client.connect()
    spec = client.get_symbol_spec("XAUUSD")
    journal = _journal()
    ks = kill_switch or KillSwitch(tempfile.mktemp(suffix=".json"))
    state_store = SqliteExecutionStateStore(tempfile.mktemp(suffix=".db"))
    pm_state_store = SqlitePositionMonitorStateStore(tempfile.mktemp(suffix=".db"))
    loop = TradingLoop(
        settings, client, spec, journal, kill_switch=ks, execution_state_store=state_store,
        position_monitor_state_store=pm_state_store, position_monitor=position_monitor,
    )
    if clear_news:
        loop.news_filter = _AlwaysClear()
    return loop, client, spec, journal


def _open_paper_position(loop, entry=2650.0, stop=2645.0, tp=2700.0):
    fake_signal = Signal(
        direction=SignalDirection.BUY, strategy="TEST", entry=entry, stop_loss=stop,
        take_profit=tp, confidence=0.8, score=8, score_label="VALID",
        meta={"entry_confirmed": True, "quality_confirmed": True},
    )
    loop.selector.best_signal = lambda context: fake_signal
    loop.run_once()
    positions = loop.execution_engine.get_open_positions()
    assert len(positions) == 1
    return positions[0]


def _profitable_tick_for(position, r_multiple=1.5):
    """Builds a Tick that is `r_multiple` R in profit for `position`,
    computed from its ACTUAL fill price/stop rather than a hardcoded
    absolute price.

    IMPORTANT: ExecutionEngine.submit_market_order's PAPER branch fills
    at the CURRENT simulated market tick (tick.ask for BUY / tick.bid
    for SELL) at submission time -- NOT at the signal's nominal
    `entry` field. So a position opened via a fake Signal(entry=2650.0,
    ...) will NOT necessarily have price_open == 2650.0; a hardcoded
    "2656.0 is profitable" tick silently assumed an entry that never
    actually happened, which is why loop-level breakeven/trailing
    tests must derive their trigger tick from position.price_open,
    exactly like this helper does."""
    risk = abs(position.price_open - position.stop_loss)
    assert risk > 0
    if position.direction == "BUY":
        price = position.price_open + risk * r_multiple
        return Tick(symbol=position.symbol, time=datetime.now(timezone.utc),
                    bid=price, ask=price + 0.2, last=price, volume=1.0)
    price = position.price_open - risk * r_multiple
    return Tick(symbol=position.symbol, time=datetime.now(timezone.utc),
                bid=price - 0.2, ask=price, last=price, volume=1.0)


# =====================================================================
# 1. Breakeven -- restart-safe idempotency
# =====================================================================
def test_breakeven_state_persists_across_monitor_instances():
    store = SqlitePositionMonitorStateStore(tempfile.mktemp(suffix=".db"))
    cfg = PositionMonitorConfig(_env_file=None, breakeven_trigger_r=1.0, enable_partial_exit=False, enable_trailing=False)

    monitor_a = PositionMonitor(cfg, state_store=store)
    pos = _position()
    actions = monitor_a.evaluate(pos, _tick(2656.0), tick_size=0.01)
    assert any(a.type == ActionType.MOVE_TO_BREAKEVEN for a in actions)
    monitor_a.mark_breakeven_applied(pos.ticket, confirmed_stop_loss=actions[0].new_stop_loss)

    # Simulate a restart: a brand-new PositionMonitor instance, same store.
    monitor_b = PositionMonitor(cfg, state_store=store)
    actions_after_restart = monitor_b.evaluate(pos, _tick(2657.0), tick_size=0.01)
    assert not any(a.type == ActionType.MOVE_TO_BREAKEVEN for a in actions_after_restart)


def test_breakeven_never_worsens_an_existing_better_stop():
    monitor = PositionMonitor(PositionMonitorConfig(
        _env_file=None, breakeven_trigger_r=0.02, breakeven_buffer_ticks=1.0,
        enable_partial_exit=False, enable_trailing=False,
    ))
    # Stop is already better (tighter, further from a losing exit) than
    # what the computed breakeven+buffer stop would be -- moving it
    # would actually WORSEN protection, so no action should fire.
    pos = _position(entry=2650.0, stop=2650.5)
    actions = monitor.evaluate(pos, _tick(2650.6), tick_size=0.01)
    assert not any(a.type == ActionType.MOVE_TO_BREAKEVEN for a in actions)


def test_breakeven_uncertain_result_does_not_mark_applied():
    """LIVE modify raises (connection lost) -> engine reports UNKNOWN ->
    caller must NEVER call mark_breakeven_applied for this."""
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p8-be-unc-1")
    assert result.success

    client.queue_connection_lost(applies_to="modify_position")
    modify_result = engine.modify_position(pos.ticket, stop_loss=2605.0, take_profit=None)
    assert modify_result.success is False
    assert modify_result.raw.get("status") == EXECUTION_STATUS_UNKNOWN

    monitor = PositionMonitor()
    # Caller-side contract: only mark_breakeven_applied on confirmed
    # success. Since result was UNKNOWN, the caller (TradingLoop) must
    # record_pending instead -- verify that path leaves _breakeven_moved
    # untouched and blocks further evaluation.
    monitor.record_pending(pos.ticket, ActionType.MOVE_TO_BREAKEVEN, requested_stop_loss=2605.0)
    assert pos.ticket not in monitor._breakeven_moved
    assert monitor.has_pending(pos.ticket)


# =====================================================================
# 2. Partial exit
# =====================================================================
def test_partial_exit_state_persists_across_restart():
    store = SqlitePositionMonitorStateStore(tempfile.mktemp(suffix=".db"))
    cfg = PositionMonitorConfig(_env_file=None, partial_exit_trigger_r=1.0, enable_trailing=False, breakeven_trigger_r=100.0)

    monitor_a = PositionMonitor(cfg, state_store=store)
    pos = _position(volume=0.2)
    actions = monitor_a.evaluate(pos, _tick(2656.0), tick_size=0.01)
    partial = next(a for a in actions if a.type == ActionType.PARTIAL_EXIT)
    monitor_a.mark_partial_taken(pos.ticket, confirmed_volume=partial.partial_volume)

    monitor_b = PositionMonitor(cfg, state_store=store)
    actions_after = monitor_b.evaluate(pos, _tick(2657.0), tick_size=0.01)
    assert not any(a.type == ActionType.PARTIAL_EXIT for a in actions_after)


def test_partial_exit_fraction_uses_original_volume_not_current():
    store = InMemoryPositionMonitorStateStore()
    cfg = PositionMonitorConfig(_env_file=None, partial_exit_trigger_r=1.0, partial_exit_fraction=0.5,
                                 enable_trailing=False, breakeven_trigger_r=100.0)
    monitor = PositionMonitor(cfg, state_store=store)
    pos = _position(volume=0.2)
    # First evaluate() call establishes initial_volume=0.2 in the record.
    monitor.evaluate(pos, _tick(2650.5), tick_size=0.01)  # below trigger -- just seeds the record

    # Volume changed externally (e.g. manual broker-side change) before
    # the trigger is reached -- the fraction must still be computed
    # against the ORIGINAL 0.2, not the new 0.5.
    pos.volume = 0.5
    actions = monitor.evaluate(pos, _tick(2656.0), tick_size=0.01)
    partial = next(a for a in actions if a.type == ActionType.PARTIAL_EXIT)
    assert partial.partial_volume == pytest.approx(0.1)  # 0.5 * 0.2, not 0.5 * 0.5


def test_partial_exit_never_takes_entire_position():
    store = InMemoryPositionMonitorStateStore()
    cfg = PositionMonitorConfig(_env_file=None, partial_exit_trigger_r=1.0, partial_exit_fraction=0.999999,
                                 enable_trailing=False, breakeven_trigger_r=100.0)
    monitor = PositionMonitor(cfg, state_store=store)
    pos = _position(volume=0.01)  # tiny volume, near broker minimums
    actions = monitor.evaluate(pos, _tick(2656.0), tick_size=0.01)
    partials = [a for a in actions if a.type == ActionType.PARTIAL_EXIT]
    for p in partials:
        assert p.partial_volume < pos.volume


def test_uncertain_partial_close_does_not_reduce_local_volume():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.3, 2600.0, 2700.0, "p8-partial-unc-1")
    assert result.success

    client.queue_connection_lost(applies_to="close_position_partial")
    partial_result = engine.close_position_partial(pos.ticket, 0.1)
    assert partial_result.success is False
    assert partial_result.raw.get("status") == EXECUTION_STATUS_UNKNOWN

    remaining = next(p for p in client.get_open_positions(spec.name) if p.ticket == pos.ticket)
    assert remaining.volume == pytest.approx(0.3)  # untouched


# =====================================================================
# 3. Trailing stop
# =====================================================================
def test_trailing_stop_does_not_resubmit_an_unchanged_stop():
    store = InMemoryPositionMonitorStateStore()
    cfg = PositionMonitorConfig(_env_file=None, trailing_trigger_r=1.0, enable_partial_exit=False,
                                 breakeven_trigger_r=100.0, trailing_atr_multiplier=1.0)
    monitor = PositionMonitor(cfg, state_store=store)
    pos = _position(entry=2650.0, stop=2645.0)

    actions = monitor.evaluate(pos, _tick(2660.0), tick_size=0.01, current_atr=2.0)
    trail = next(a for a in actions if a.type == ActionType.TRAIL_STOP)
    monitor.mark_trailing_applied(pos.ticket, trail.new_stop_loss)
    # position.stop_loss is NOT updated in this test on purpose -- simulates
    # the confirmed value having been applied broker-side but the local
    # `pos` object not yet refreshed; the monitor's own persisted record
    # must still be what prevents a resubmission.
    actions_again = monitor.evaluate(pos, _tick(2660.0), tick_size=0.01, current_atr=2.0)
    assert not any(a.type == ActionType.TRAIL_STOP for a in actions_again)


def test_trailing_stop_never_loosens_buy():
    store = InMemoryPositionMonitorStateStore()
    cfg = PositionMonitorConfig(_env_file=None, trailing_trigger_r=1.0, enable_partial_exit=False,
                                 breakeven_trigger_r=100.0, trailing_atr_multiplier=5.0)
    monitor = PositionMonitor(cfg, state_store=store)
    # Stop already close to price -- a wide ATR-based trail would be WORSE.
    pos = _position(entry=2650.0, stop=2658.0)
    actions = monitor.evaluate(pos, _tick(2660.0), tick_size=0.01, current_atr=2.0)
    assert not any(a.type == ActionType.TRAIL_STOP for a in actions)


def test_trailing_stop_no_action_when_atr_invalid():
    monitor = PositionMonitor(PositionMonitorConfig(_env_file=None, trailing_trigger_r=1.0, enable_partial_exit=False, breakeven_trigger_r=100.0))
    pos = _position(entry=2650.0, stop=2645.0)
    for bad_atr in (None, 0, -1.0):
        actions = monitor.evaluate(pos, _tick(2660.0), tick_size=0.01, current_atr=bad_atr)
        assert not any(a.type == ActionType.TRAIL_STOP for a in actions)


def test_uncertain_live_modify_leaves_sl_unchanged():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p8-trail-unc-1")
    assert result.success
    original_sl = next(p.stop_loss for p in client.get_open_positions(spec.name) if p.ticket == pos.ticket)

    client.queue_connection_lost(applies_to="modify_position")
    modify_result = engine.modify_position(pos.ticket, stop_loss=2650.0, take_profit=None)
    assert modify_result.success is False
    assert modify_result.raw.get("status") == EXECUTION_STATUS_UNKNOWN

    after_sl = next(p.stop_loss for p in client.get_open_positions(spec.name) if p.ticket == pos.ticket)
    assert after_sl == original_sl


# =====================================================================
# 4. Pending / uncertain-action bookkeeping (section 9)
# =====================================================================
def test_pending_action_blocks_further_evaluation():
    monitor = PositionMonitor(PositionMonitorConfig(_env_file=None, breakeven_trigger_r=1.0))
    pos = _position()
    monitor.evaluate(pos, _tick(2650.5), tick_size=0.01)  # seed the record
    monitor.record_pending(pos.ticket, ActionType.MOVE_TO_BREAKEVEN, requested_stop_loss=2650.05)
    assert monitor.has_pending(pos.ticket)

    actions = monitor.evaluate(pos, _tick(2660.0), tick_size=0.01)  # would otherwise trigger everything
    assert actions == []


def test_clear_pending_allows_evaluation_to_resume():
    monitor = PositionMonitor(PositionMonitorConfig(_env_file=None, breakeven_trigger_r=1.0, enable_partial_exit=False, enable_trailing=False))
    pos = _position()
    monitor.evaluate(pos, _tick(2650.5), tick_size=0.01)
    monitor.record_pending(pos.ticket, ActionType.MOVE_TO_BREAKEVEN)
    monitor.clear_pending(pos.ticket)
    assert not monitor.has_pending(pos.ticket)
    actions = monitor.evaluate(pos, _tick(2656.0), tick_size=0.01)
    assert any(a.type == ActionType.MOVE_TO_BREAKEVEN for a in actions)


def test_pending_state_survives_restart():
    store = SqlitePositionMonitorStateStore(tempfile.mktemp(suffix=".db"))
    monitor_a = PositionMonitor(state_store=store)
    pos = _position()
    monitor_a.evaluate(pos, _tick(2650.5), tick_size=0.01)
    monitor_a.record_pending(pos.ticket, ActionType.PARTIAL_EXIT, requested_partial_volume=0.1, note="conn lost")

    monitor_b = PositionMonitor(state_store=store)
    assert monitor_b.has_pending(pos.ticket)
    record = monitor_b.get_record(pos.ticket)
    assert record.pending_action.action_type == "PARTIAL_EXIT"
    assert record.pending_action.requested_partial_volume == 0.1


# =====================================================================
# 5. Full exit / closure detection
# =====================================================================
def test_uncertain_close_keeps_position_tracked_open():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p8-close-unc-1")
    assert result.success
    client.queue_connection_lost(applies_to="close_position")
    close_result = engine.close_position(pos.ticket)
    assert close_result.success is False
    assert close_result.raw.get("status") == EXECUTION_STATUS_UNKNOWN
    still_open = {p.ticket for p in engine.get_open_positions()}
    assert pos.ticket in still_open


def test_forget_marks_record_closed_but_keeps_audit_trail():
    store = InMemoryPositionMonitorStateStore()
    monitor = PositionMonitor(state_store=store)
    pos = _position()
    monitor.mark_breakeven_applied(pos.ticket)
    monitor.forget(pos.ticket)
    record = store.get(pos.ticket)
    assert record is not None  # kept, not deleted
    assert record.closed is True
    assert pos.ticket not in monitor._breakeven_moved


# =====================================================================
# 6. Reconciliation (sections 7-8)
# =====================================================================
def test_reconcile_flags_missing_at_broker():
    record = PositionMonitorRecord(ticket=1, last_confirmed_stop_loss=2645.0, initial_volume=0.2)
    discrepancies = reconcile_positions([record], broker_positions=[])
    assert any(d.type == DiscrepancyType.MISSING_AT_BROKER and d.ticket == 1 for d in discrepancies)


def test_reconcile_flags_untracked_locally():
    broker_pos = _position(ticket=99)
    discrepancies = reconcile_positions([], broker_positions=[broker_pos])
    assert any(d.type == DiscrepancyType.UNTRACKED_LOCALLY and d.ticket == 99 for d in discrepancies)


def test_reconcile_flags_externally_changed_stop_loss():
    record = PositionMonitorRecord(ticket=1, last_confirmed_stop_loss=2645.0, initial_volume=0.2)
    broker_pos = _position(ticket=1, stop=2650.0)  # someone moved it manually
    discrepancies = reconcile_positions([record], broker_positions=[broker_pos])
    assert any(d.type == DiscrepancyType.STOP_LOSS_CHANGED_EXTERNALLY for d in discrepancies)


def test_reconcile_flags_externally_changed_volume():
    record = PositionMonitorRecord(ticket=1, initial_volume=0.2, partial_exit_volume=0.1, last_confirmed_stop_loss=2645.0)
    # Expected remaining = 0.2 - 0.1 = 0.1, but broker reports 0.05.
    broker_pos = _position(ticket=1, volume=0.05, stop=2645.0)
    discrepancies = reconcile_positions([record], broker_positions=[broker_pos])
    assert any(d.type == DiscrepancyType.VOLUME_CHANGED_EXTERNALLY for d in discrepancies)


def test_reconcile_flags_unresolved_pending_action():
    record = PositionMonitorRecord(
        ticket=1, initial_volume=0.2, last_confirmed_stop_loss=2645.0,
        pending_action=PendingAction(action_type="MOVE_TO_BREAKEVEN"),
    )
    broker_pos = _position(ticket=1)
    discrepancies = reconcile_positions([record], broker_positions=[broker_pos])
    assert any(d.type == DiscrepancyType.UNRESOLVED_PENDING_ACTION for d in discrepancies)


def test_reconcile_no_discrepancies_for_matching_state():
    record = PositionMonitorRecord(ticket=1, initial_volume=0.2, last_confirmed_stop_loss=2645.0)
    broker_pos = _position(ticket=1, stop=2645.0, volume=0.2)
    discrepancies = reconcile_positions([record], broker_positions=[broker_pos])
    assert discrepancies == []


def test_reconcile_ignores_closed_records():
    record = PositionMonitorRecord(ticket=1, initial_volume=0.2, last_confirmed_stop_loss=2645.0, closed=True)
    discrepancies = reconcile_positions([record], broker_positions=[])
    assert discrepancies == []  # a closed ticket missing at the broker is expected, not a discrepancy


def test_recover_state_fails_closed_on_unreadable_store():
    class _BrokenStore:
        def all(self):
            raise RuntimeError("disk error")

    records, discrepancies = recover_state(_BrokenStore(), broker_positions=[])
    assert records == []
    assert len(discrepancies) == 1
    assert "fail" in discrepancies[0].detail.lower() or "closed" in discrepancies[0].detail.lower()


def test_recover_state_with_no_store_returns_empty():
    records, discrepancies = recover_state(None, broker_positions=[])
    assert records == [] and discrepancies == []


def test_recover_state_reconciles_persisted_records_against_broker():
    store = SqlitePositionMonitorStateStore(tempfile.mktemp(suffix=".db"))
    store.put(PositionMonitorRecord(ticket=1, initial_volume=0.2, last_confirmed_stop_loss=2645.0))
    records, discrepancies = recover_state(store, broker_positions=[])
    assert len(records) == 1
    assert any(d.type == DiscrepancyType.MISSING_AT_BROKER for d in discrepancies)


# =====================================================================
# 7. Idempotency across repeated cycles (section 13)
# =====================================================================
def test_repeated_evaluate_cycles_do_not_duplicate_breakeven():
    store = InMemoryPositionMonitorStateStore()
    monitor = PositionMonitor(
        PositionMonitorConfig(_env_file=None, breakeven_trigger_r=1.0, enable_partial_exit=False, enable_trailing=False),
        state_store=store,
    )
    pos = _position()
    breakeven_fires = 0
    for i in range(5):
        actions = monitor.evaluate(pos, _tick(2656.0 + i), tick_size=0.01)
        be_actions = [a for a in actions if a.type == ActionType.MOVE_TO_BREAKEVEN]
        if be_actions:
            breakeven_fires += 1
            monitor.mark_breakeven_applied(pos.ticket, confirmed_stop_loss=be_actions[0].new_stop_loss)
            pos.stop_loss = be_actions[0].new_stop_loss
    assert breakeven_fires == 1


def test_repeated_reconciliation_is_read_only_and_stable():
    record = PositionMonitorRecord(ticket=1, initial_volume=0.2, last_confirmed_stop_loss=2645.0)
    broker_pos = _position(ticket=1, stop=2645.0, volume=0.2)
    first = reconcile_positions([record], broker_positions=[broker_pos])
    second = reconcile_positions([record], broker_positions=[broker_pos])
    assert first == second == []


# =====================================================================
# 8. Adversarial: malicious/misleading results must fail safely
# =====================================================================
def test_engine_never_reports_modify_success_when_status_is_unknown():
    """Mirrors the Phase 6/7 defense-in-depth check for submit_order,
    now exercised via a connection-loss path for modify_position: an
    UNKNOWN-tagged outcome must never surface success=True."""
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p8-adv-1")
    assert result.success

    client.queue_connection_lost(applies_to="modify_position")
    modify_result = engine.modify_position(pos.ticket, stop_loss=2610.0, take_profit=None)
    assert modify_result.success is False
    if modify_result.raw.get("status") == EXECUTION_STATUS_UNKNOWN:
        assert modify_result.success is False  # never trusted, even implicitly


def test_position_monitor_never_double_counts_partial_exit_after_unknown():
    store = InMemoryPositionMonitorStateStore()
    monitor = PositionMonitor(
        PositionMonitorConfig(_env_file=None, partial_exit_trigger_r=1.0, enable_trailing=False, breakeven_trigger_r=100.0),
        state_store=store,
    )
    pos = _position(volume=0.2)
    actions = monitor.evaluate(pos, _tick(2656.0), tick_size=0.01)
    partial = next(a for a in actions if a.type == ActionType.PARTIAL_EXIT)
    # Simulate submitting it and getting UNKNOWN back.
    monitor.record_pending(pos.ticket, ActionType.PARTIAL_EXIT, requested_partial_volume=partial.partial_volume)
    # Even though price keeps moving favorably, no further action must
    # be generated while unresolved -- specifically, partial_exit_taken
    # must NOT have been silently set.
    record = monitor.get_record(pos.ticket)
    assert record.partial_exit_taken is False
    more_actions = monitor.evaluate(pos, _tick(2670.0), tick_size=0.01)
    assert more_actions == []


# =====================================================================
# 9. Kill switch: risk-reducing management continues; only NEW risk is blocked
# =====================================================================
def test_kill_switch_active_still_moves_existing_position_to_breakeven():
    loop, client, spec, journal = _loop(clear_news=True)
    position = _open_paper_position(loop, entry=2650.0, stop=2645.0, tp=2750.0)

    ks_path = tempfile.mktemp(suffix=".json")
    ks = KillSwitch(ks_path)
    ks.activate("halt new trades", activated_by="tester", close_positions=False)
    loop.kill_switch = ks

    profitable_tick = _profitable_tick_for(position)
    import unittest.mock
    with unittest.mock.patch.object(client, "get_tick", return_value=profitable_tick):
        summary = loop.run_once()

    assert "Kill switch is active" in " ".join(summary.errors)
    assert any("breakeven" in a.lower() for a in summary.actions_taken)
    # No second position was opened despite being flat-eligible logic --
    # the kill switch still blocked NEW entries; only one ticket exists.
    assert len({p.ticket for p in loop.execution_engine.get_open_positions()}) == 1


def test_kill_switch_never_opens_a_new_position():
    settings = Settings(_env_file=None, KILL_SWITCH=True)
    loop, client, spec, journal = _loop(settings=settings, clear_news=True)
    fake_signal = Signal(
        direction=SignalDirection.BUY, strategy="TEST", entry=2650.0, stop_loss=2645.0,
        take_profit=2700.0, confidence=0.8, score=8, score_label="VALID",
        meta={"entry_confirmed": True, "quality_confirmed": True},
    )
    loop.selector.best_signal = lambda context: fake_signal
    summary = loop.run_once()
    assert loop.execution_engine.get_open_positions() == []
    assert summary.signal_direction is None  # never even evaluated


def test_kill_switch_close_positions_flag_forces_full_exit():
    loop, client, spec, journal = _loop(clear_news=True)
    position = _open_paper_position(loop)

    ks_path = tempfile.mktemp(suffix=".json")
    ks = KillSwitch(ks_path)
    ks.activate("emergency flatten", activated_by="tester", close_positions=True)
    loop.kill_switch = ks

    summary = loop.run_once()
    assert loop.execution_engine.get_open_positions() == []
    assert any("KILL_SWITCH" in a for a in summary.actions_taken)


def test_pending_action_blocks_management_at_loop_level():
    loop, client, spec, journal = _loop(clear_news=True)
    position = _open_paper_position(loop, entry=2650.0, stop=2645.0, tp=2750.0)
    loop.position_monitor.record_pending(position.ticket, ActionType.MOVE_TO_BREAKEVEN, note="test")

    profitable_tick = _profitable_tick_for(position)
    import unittest.mock
    with unittest.mock.patch.object(client, "get_tick", return_value=profitable_tick):
        summary = loop.run_once()

    assert not any("breakeven" in a.lower() for a in summary.actions_taken)
    assert any("unresolved" in e.lower() for e in summary.errors)


# =====================================================================
# 10. Journal / audit trail (section 12)
# =====================================================================
def test_confirmed_breakeven_is_journaled():
    loop, client, spec, journal = _loop(clear_news=True)
    position = _open_paper_position(loop, entry=2650.0, stop=2645.0, tp=2750.0)

    profitable_tick = _profitable_tick_for(position)
    import unittest.mock
    with unittest.mock.patch.object(client, "get_tick", return_value=profitable_tick):
        loop.run_once()

    rows = [dict(r) for r in journal.position_actions_for_ticket(position.ticket)]
    assert any(r["action"] == "MOVE_TO_BREAKEVEN" and r["result_status"] == "CONFIRMED" for r in rows)


def test_uncertain_action_is_journaled_as_unknown_not_confirmed(monkeypatch):
    loop, client, spec, journal = _loop(clear_news=True)
    position = _open_paper_position(loop, entry=2650.0, stop=2645.0, tp=2750.0)

    # Force modify_position on the *execution engine* to look uncertain
    # by making the underlying (paper-mode) call raise -- simulate by
    # monkeypatching ExecutionEngine.modify_position directly since
    # paper mode has no client-level connection to drop.
    from app.mt5.interface import OrderResult
    def fake_modify(ticket, stop_loss, take_profit):
        return OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None,
                            retcode=None, comment="UNKNOWN_EXECUTION_RESULT: simulated",
                            raw={"status": EXECUTION_STATUS_UNKNOWN})
    monkeypatch.setattr(loop.execution_engine, "modify_position", fake_modify)

    profitable_tick = _profitable_tick_for(position)
    monkeypatch.setattr(client, "get_tick", lambda symbol=None: profitable_tick)

    summary = loop.run_once()
    assert not any("Moved ticket" in a for a in summary.actions_taken)
    assert loop.position_monitor.has_pending(position.ticket)
    rows = [dict(r) for r in journal.position_actions_for_ticket(position.ticket)]
    breakeven_rows = [r for r in rows if r["action"] == "MOVE_TO_BREAKEVEN"]
    assert any(r["result_status"] == "UNKNOWN" for r in breakeven_rows)
    assert not any(r["result_status"] == "CONFIRMED" for r in breakeven_rows)


# =====================================================================
# 11. PAPER mode safety (section 11) -- never touches real client paths
# =====================================================================
def test_paper_mode_modify_never_calls_real_client_modify(monkeypatch):
    engine, client, spec = _engine(TradingMode.PAPER)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p8-paper-1")
    assert result.success

    called = {"n": 0}
    def spy(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("real client modify_position must never be called in PAPER mode")
    monkeypatch.setattr(client, "modify_position", spy)

    modify_result = engine.modify_position(pos.ticket, stop_loss=2605.0, take_profit=None)
    assert modify_result.success is True
    assert called["n"] == 0


def test_paper_mode_close_never_calls_real_client_close(monkeypatch):
    engine, client, spec = _engine(TradingMode.PAPER)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "p8-paper-2")
    assert result.success

    called = {"n": 0}
    def spy(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("real client close_position must never be called in PAPER mode")
    monkeypatch.setattr(client, "close_position", spy)

    close_result = engine.close_position(pos.ticket)
    assert close_result.success is True
    assert called["n"] == 0
