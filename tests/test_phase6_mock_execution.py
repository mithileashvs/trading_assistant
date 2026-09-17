"""
Phase 6 — Mock execution improvements.

Tests both layers the spec calls out:
  - MockMT5Client's own fault injection, tested directly (bypassing
    ExecutionEngine) where the scenario is specifically about the
    mock's broker-side validation (e.g. invalid volume, which
    ExecutionSafetyGate would otherwise catch first through the full
    pipeline -- testing the client directly proves the mock ALSO
    enforces it, independent of that gate).
  - The full Strategy-adjacent entry point, ExecutionEngine, for
    scenarios about how the EXECUTION LAYER handles what the mock
    reports (particularly section 10, UNKNOWN/uncertain results, and
    section 9, duplicate protection) -- these are meaningless tested
    against the mock alone, since the safety property is specifically
    about ExecutionEngine's client_order_id bookkeeping.

Every test in this file is deterministic: no real randomness is left
uncontrolled where the outcome matters (get_tick's small Gaussian noise
is either pinned via monkeypatching or the assertion is written to be
noise-tolerant, e.g. "outside a fixed valid range" rather than an exact
value).
"""
from __future__ import annotations

from app.config.settings import TradingMode
from app.execution.engine import ExecutionEngine
from app.mt5.interface import EXECUTION_STATUS_FILLED, EXECUTION_STATUS_REJECTED, EXECUTION_STATUS_UNKNOWN, OrderRequest
from app.mt5.mock_client import MockMT5Client
from app.mt5.real_client import MT5ConnectionError
import pytest


def _engine(mode=TradingMode.LIVE):
    client = MockMT5Client()
    client.connect()
    spec = client.get_symbol_spec("XAUUSD")
    return ExecutionEngine(client, spec, mode), client, spec


def _mock():
    c = MockMT5Client()
    c.connect()
    return c


# =====================================================================
# SUCCESS
# =====================================================================
def test_successful_buy_via_engine():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "buy-1")
    assert result.success
    assert result.raw.get("status") == EXECUTION_STATUS_FILLED
    assert pos is not None and pos.direction == "BUY"


def test_successful_sell_via_engine():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("SELL", 0.1, 2700.0, 2600.0, "sell-1")
    assert result.success
    assert pos is not None and pos.direction == "SELL"


def test_successful_close_via_engine():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "close-src-1")
    close_result = engine.close_position(pos.ticket)
    assert close_result.success
    assert engine.client.get_open_positions("XAUUSD") == []


def test_successful_modify_via_engine():
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "mod-src-1")
    modify_result = engine.modify_position(pos.ticket, stop_loss=2610.0, take_profit=2710.0)
    assert modify_result.success


def test_mock_client_direct_buy_sell_close():
    c = _mock()
    buy = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert buy.success and buy.raw.get("status") == EXECUTION_STATUS_FILLED
    sell = c.submit_order(OrderRequest(symbol="XAUUSD", direction="SELL", volume=0.1))
    assert sell.success
    close = c.close_position(buy.order_id)
    assert close.success


# =====================================================================
# FAILURES — order rejections
# =====================================================================
def test_generic_rejection_via_mock_and_engine():
    c = _mock()
    c.queue_order_rejection("BROKER_SERVER_REJECTED: test", retcode=99)
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert not r.success and r.retcode == 99 and "BROKER_SERVER_REJECTED" in r.comment
    assert c.get_open_positions("XAUUSD") == []

    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_order_rejection("BROKER_SERVER_REJECTED: test")
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "gen-rej-1")
    assert not result.success and pos is None


def test_invalid_symbol_rejected():
    c = _mock()
    r = c.submit_order(OrderRequest(symbol="NOTREAL", direction="BUY", volume=0.1))
    assert not r.success and "INVALID_SYMBOL" in r.comment
    assert r.raw.get("status") == EXECUTION_STATUS_REJECTED
    assert c.get_open_positions() == []


def test_invalid_volume_too_large_rejected():
    c = _mock()
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=999.0))
    assert not r.success and "INVALID_VOLUME" in r.comment


def test_invalid_volume_too_small_rejected():
    c = _mock()
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.001))
    assert not r.success and "INVALID_VOLUME" in r.comment


def test_invalid_volume_misaligned_step_rejected():
    c = _mock()
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.015))
    assert not r.success and "INVALID_VOLUME" in r.comment


def test_invalid_stops_too_close_rejected(monkeypatch):
    c = _mock()
    fixed_tick = c.get_tick("XAUUSD")
    monkeypatch.setattr(c, "get_tick", lambda symbol: fixed_tick)
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1, stop_loss=fixed_tick.ask - 0.01))
    assert not r.success and "INVALID_STOPS" in r.comment


def test_valid_stops_far_enough_accepted(monkeypatch):
    c = _mock()
    fixed_tick = c.get_tick("XAUUSD")
    monkeypatch.setattr(c, "get_tick", lambda symbol: fixed_tick)
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1, stop_loss=fixed_tick.ask - 10.0))
    assert r.success


def test_market_closed_rejected_via_mock_and_engine():
    c = _mock()
    c.set_market_closed(True)
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert not r.success and "MARKET_CLOSED" in r.comment

    engine, client, spec = _engine(TradingMode.LIVE)
    client.set_market_closed(True)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "mc-1")
    assert not result.success and pos is None


def test_trading_disabled_rejected():
    c = _mock()
    c.set_trading_disabled("XAUUSD", True)
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert not r.success and "TRADING_DISABLED" in r.comment
    c.set_trading_disabled("XAUUSD", False)
    r2 = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert r2.success


def test_insufficient_margin_rejected():
    c = _mock()
    c.set_margin_insufficient(True)
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert not r.success and "INSUFFICIENT_MARGIN" in r.comment
    assert c.get_account_info().margin_free == 0.0
    assert c.get_open_positions() == []


# =====================================================================
# FAILURES — connectivity / uncertainty
# =====================================================================
def test_connection_lost_before_order_raises_and_creates_nothing():
    c = _mock()
    c.queue_connection_lost("submit_order")
    with pytest.raises(MT5ConnectionError):
        c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert c.get_open_positions() == []


def test_connection_lost_fetching_account_info():
    c = _mock()
    c.queue_connection_lost("get_account_info")
    with pytest.raises(MT5ConnectionError):
        c.get_account_info()


def test_connection_lost_fetching_tick():
    c = _mock()
    c.queue_connection_lost("get_tick")
    with pytest.raises(MT5ConnectionError):
        c.get_tick("XAUUSD")


def test_reconnect_unavailable_after_disconnect():
    c = MockMT5Client()
    c.connect()
    c.disconnect()
    c.set_reconnect_unavailable(True)
    with pytest.raises(MT5ConnectionError):
        c.connect()


def test_connection_unavailable_before_execution_blocks_engine_via_account_info():
    """ExecutionSafetyGate's pre-flight get_account_info() call already
    fails closed on ANY exception (pre-existing behavior) -- this just
    confirms the Phase 6 mock's own connection-lost fault flows through
    that existing protection correctly."""
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_connection_lost("get_account_info")
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "acct-lost-1")
    assert not result.success and pos is None
    assert "EXECUTION_SAFETY_GATE_REJECTED" in result.comment


# =====================================================================
# FAILURES — stale / missing / invalid market data
# =====================================================================
def test_stale_tick_rejected_via_mock_and_paper_engine():
    c = _mock()
    c.set_tick_state("stale")
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert not r.success and "STALE_QUOTE" in r.comment

    engine, client, spec = _engine(TradingMode.PAPER)
    client.set_tick_state("stale")
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "stale-1")
    assert not result.success and pos is None
    assert engine.get_open_positions() == []


def test_missing_tick_rejected_via_mock_and_paper_engine():
    c = _mock()
    c.set_tick_state("missing")
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert not r.success and "NO_QUOTE" in r.comment

    engine, client, spec = _engine(TradingMode.PAPER)
    client.set_tick_state("missing")
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "missing-1")
    assert not result.success and pos is None


def test_invalid_crossed_tick_rejected_via_mock_and_paper_engine():
    c = _mock()
    c.set_tick_state("invalid")
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert not r.success and "INVALID_QUOTE" in r.comment

    engine, client, spec = _engine(TradingMode.PAPER)
    client.set_tick_state("invalid")
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "invalid-tick-1")
    assert not result.success and pos is None


def test_paper_mode_normal_fill_unaffected_by_new_validation():
    engine, client, spec = _engine(TradingMode.PAPER)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "paper-ok-1")
    assert result.success and pos is not None


# =====================================================================
# HIGH PRIORITY: unknown execution result (section 10)
# =====================================================================
def test_unknown_execution_result_is_not_success_and_creates_no_position():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result()
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "unk-1")
    assert result.success is False
    assert result.raw.get("status") == EXECUTION_STATUS_UNKNOWN
    assert pos is None
    assert engine.get_open_positions() == []


def test_unknown_execution_result_blocks_blind_resubmission_same_client_order_id(monkeypatch):
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result()
    first, pos1 = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "unk-2")
    assert first.success is False

    called = {"count": 0}
    original = client.submit_order

    def spy(*args, **kwargs):
        called["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "submit_order", spy)

    second, pos2 = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "unk-2")
    assert second.success is False
    assert "UNKNOWN" in second.comment
    assert called["count"] == 0, "resubmission under the same client_order_id must never reach the broker"
    assert pos2 is None


def test_connection_lost_during_submit_order_treated_as_unknown_not_crash():
    """A raised exception from client.submit_order() (not just an
    explicit UNKNOWN return value) must ALSO be converted to a safe
    UNKNOWN result -- the engine cannot distinguish "failed before
    send" from "failed after send" from outside the client, so ANY
    exception there is treated as uncertain, never silently
    success/failure."""
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_connection_lost("submit_order")
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "conn-mid-1")
    assert result.success is False
    assert result.raw.get("status") == EXECUTION_STATUS_UNKNOWN
    assert pos is None


def test_connection_lost_during_submit_order_also_blocks_resubmission(monkeypatch):
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_connection_lost("submit_order")
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "conn-mid-2")

    called = {"count": 0}
    original = client.submit_order

    def spy(*args, **kwargs):
        called["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "submit_order", spy)

    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "conn-mid-2")
    assert result.success is False
    assert called["count"] == 0


def test_reconcile_flags_unknown_outcomes_without_fabricating_ticket_none():
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_unknown_execution_result()
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "unk-reconcile-1")

    notes = engine.reconcile()
    assert any("UNKNOWN" in n for n in notes)
    assert not any("Ticket None" in n for n in notes)


def test_ordinary_rejection_does_not_permanently_block_a_later_legitimate_resubmission():
    """Contrast with UNKNOWN: an ordinary (non-uncertain) rejection was
    never recorded as "seen" at all, so a later, distinct submission
    attempt under the same client_order_id can still succeed -- Phase 6
    only tightens behavior for the genuinely uncertain case."""
    engine, client, spec = _engine(TradingMode.LIVE)
    client.queue_order_rejection("BROKER_SERVER_REJECTED: test")
    first, _ = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "ord-rej-1")
    assert not first.success
    second, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "ord-rej-1")
    assert second.success and pos is not None


# =====================================================================
# Duplicate order protection (section 9)
# =====================================================================
def test_duplicate_order_first_succeeds_second_rejected_no_second_position():
    engine, client, spec = _engine(TradingMode.LIVE)
    first, pos1 = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "dup-1")
    second, pos2 = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "dup-1")
    assert first.success and pos1 is not None
    assert not second.success and pos2 is None
    assert second.comment == "DUPLICATE_ORDER_REJECTED"
    assert len(engine.get_open_positions()) == 1


def test_duplicate_order_rejected_at_mock_client_level_too():
    c = _mock()
    req = OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1, client_order_id="dup-client-1")
    first = c.submit_order(req)
    second = c.submit_order(req)
    assert first.success and not second.success
    assert second.comment == "DUPLICATE_ORDER_REJECTED"
    assert len(c.get_open_positions("XAUUSD")) == 1


# =====================================================================
# MARKET CONDITIONS — spread
# =====================================================================
@pytest.mark.parametrize("points", [0.05, 2.0, 50.0])
def test_spread_simulation_normal_widened_extreme(points):
    c = _mock()
    c.set_spread_points(points)
    tick = c.get_tick("XAUUSD")
    assert abs((tick.ask - tick.bid) - points) < 1e-9


def test_widened_spread_still_produces_a_valid_fill():
    c = _mock()
    c.set_spread_points(5.0)
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert r.success


# =====================================================================
# MARKET CONDITIONS — slippage
# =====================================================================
def test_positive_slippage_worsens_buy_fill(monkeypatch):
    c = _mock()
    fixed_tick = c.get_tick("XAUUSD")
    monkeypatch.setattr(c, "get_tick", lambda symbol: fixed_tick)
    c.set_slippage_points(1.5)
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert abs(r.price - (fixed_tick.ask + 1.5)) < 1e-9


def test_negative_slippage_improves_buy_fill(monkeypatch):
    c = _mock()
    fixed_tick = c.get_tick("XAUUSD")
    monkeypatch.setattr(c, "get_tick", lambda symbol: fixed_tick)
    c.set_slippage_points(-1.5)
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert abs(r.price - (fixed_tick.ask - 1.5)) < 1e-9


def test_positive_slippage_worsens_sell_fill(monkeypatch):
    c = _mock()
    fixed_tick = c.get_tick("XAUUSD")
    monkeypatch.setattr(c, "get_tick", lambda symbol: fixed_tick)
    c.set_slippage_points(1.5)
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="SELL", volume=0.1))
    assert abs(r.price - (fixed_tick.bid - 1.5)) < 1e-9


def test_slippage_result_fields_are_internally_consistent(monkeypatch):
    """requested price (the tick at submission time), fill price, and
    the resulting slippage amount must agree with each other."""
    c = _mock()
    fixed_tick = c.get_tick("XAUUSD")
    monkeypatch.setattr(c, "get_tick", lambda symbol: fixed_tick)
    c.set_slippage_points(2.0)
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    requested_price = fixed_tick.ask
    slippage_amount = r.price - requested_price
    assert abs(slippage_amount - 2.0) < 1e-9


def test_close_position_slippage_direction_is_against_the_trader(monkeypatch):
    c = _mock()
    open_tick = c.get_tick("XAUUSD")
    monkeypatch.setattr(c, "get_tick", lambda symbol: open_tick)
    opened = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    c.set_slippage_points(1.0)
    closed = c.close_position(opened.order_id)
    # Closing a BUY = selling; positive slippage must move the close
    # price AGAINST the trader, i.e. LOWER than the plain bid.
    assert abs(closed.price - (open_tick.bid - 1.0)) < 1e-9


# =====================================================================
# SAFETY
# =====================================================================
def test_mock_client_never_imports_or_references_metatrader5_package():
    src = open("app/mt5/mock_client.py", encoding="utf-8").read()
    assert "import MetaTrader5" not in src
    assert "mt5.order_send" not in src


def test_paper_mode_never_calls_broker_submit_order_even_on_new_fault_paths(monkeypatch):
    engine, client, spec = _engine(TradingMode.PAPER)
    called = {"count": 0}
    original = client.submit_order

    def spy(*args, **kwargs):
        called["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "submit_order", spy)

    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "paper-safety-1")
    client.set_tick_state("stale")
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "paper-safety-2")
    assert called["count"] == 0


def test_unsafe_execution_never_silently_becomes_success():
    """Sweep every rejection-producing scenario in this file and assert
    none of them ever have success=True -- a rejection accidentally
    read as a fill would be the single worst outcome this phase exists
    to prevent."""
    c = _mock()
    scenarios = []

    c1 = _mock(); c1.set_market_closed(True)
    scenarios.append(c1.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1)))

    c2 = _mock(); c2.set_trading_disabled("XAUUSD", True)
    scenarios.append(c2.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1)))

    c3 = _mock(); c3.set_margin_insufficient(True)
    scenarios.append(c3.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1)))

    c4 = _mock(); c4.set_tick_state("stale")
    scenarios.append(c4.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1)))

    c5 = _mock(); c5.set_tick_state("invalid")
    scenarios.append(c5.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1)))

    c6 = _mock(); c6.set_tick_state("missing")
    scenarios.append(c6.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1)))

    c7 = _mock()
    scenarios.append(c7.submit_order(OrderRequest(symbol="NOTREAL", direction="BUY", volume=0.1)))

    c8 = _mock()
    scenarios.append(c8.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=999.0)))

    c9 = _mock(); c9.queue_unknown_execution_result()
    scenarios.append(c9.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1)))

    for r in scenarios:
        assert r.success is False, f"a rejection scenario reported success=True: {r}"


# =====================================================================
# ADVERSARIAL AUDIT (post-implementation) — regressions for three real
# defects found by adversarially probing the initial implementation.
# Each of these failed before its corresponding fix; kept here so they
# never silently regress.
# =====================================================================
def test_unknown_result_blocks_duplicate_at_mock_client_level_directly():
    """Even bypassing ExecutionEngine entirely and calling the mock
    client directly twice with the same client_order_id, an UNKNOWN
    first outcome must still block the second call at the MOCK's own
    layer -- not rely solely on ExecutionEngine's bookkeeping. Found by
    adversarially calling client.submit_order() directly a second time
    after an UNKNOWN result: it originally succeeded and created a
    second position."""
    c = _mock()
    c.queue_unknown_execution_result()
    req = OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1, client_order_id="dup-unknown-1")
    first = c.submit_order(req)
    assert first.success is False and first.raw.get("status") == EXECUTION_STATUS_UNKNOWN

    second = c.submit_order(req)
    assert second.success is False
    assert second.comment == "DUPLICATE_ORDER_REJECTED"
    assert c.get_open_positions("XAUUSD") == []


def test_engine_never_trusts_success_true_alongside_unknown_status(monkeypatch):
    """A (hypothetically malformed/malicious) client that returns
    success=True alongside raw["status"]=="UNKNOWN" must still be
    reported to the CALLER as success=False -- UNKNOWN always takes
    precedence, the flag is never passed through untouched. Found by
    constructing exactly this malformed result: the caller originally
    saw success=True even though no position was tracked."""
    engine, client, spec = _engine(TradingMode.LIVE)

    def malformed_submit_order(request):
        return OrderResult(success=True, order_id=55555, deal_id=55555, price=2650.0, volume=0.1,
                            retcode=10009, comment="claims FILLED but also UNKNOWN",
                            raw={"status": EXECUTION_STATUS_UNKNOWN})

    monkeypatch.setattr(client, "submit_order", malformed_submit_order)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "malformed-1")
    assert result.success is False
    assert pos is None
    assert engine.get_open_positions() == []


def test_close_position_connection_failure_does_not_lose_the_position():
    """A connection failure fetching the tick DURING close_position must
    never cause the position to vanish from tracking while the close
    was never confirmed. Found by queuing a connection-lost fault for
    get_tick and calling close_position(): the position was originally
    popped before the failing tick fetch, permanently losing it."""
    c = _mock()
    r = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    ticket = r.order_id

    c.queue_connection_lost("get_tick")
    with pytest.raises(MT5ConnectionError):
        c.close_position(ticket)

    assert len(c.get_open_positions("XAUUSD")) == 1
    assert c.get_open_positions("XAUUSD")[0].ticket == ticket


def test_engine_live_close_position_failure_returns_safe_result_not_a_crash(monkeypatch):
    """ExecutionEngine.close_position() in LIVE mode must convert a
    client exception into a safe OrderResult(success=False), the same
    fail-closed pattern already used for submit_market_order — not let
    it propagate uncaught."""
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "close-fail-1")
    assert result.success

    client.queue_connection_lost("get_tick")
    close_result = engine.close_position(pos.ticket)
    assert close_result.success is False
    assert "CLOSE_FAILED" in close_result.comment
    # and the position is still genuinely open at the client
    assert len(client.get_open_positions("XAUUSD")) == 1


def test_engine_live_close_partial_failure_returns_safe_result(monkeypatch):
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.2, 2600.0, 2700.0, "partial-fail-1")
    assert result.success

    client.queue_connection_lost("get_tick")
    partial_result = engine.close_position_partial(pos.ticket, 0.1)
    assert partial_result.success is False
    assert "PARTIAL_CLOSE_FAILED" in partial_result.comment
    assert client.get_open_positions("XAUUSD")[0].volume == 0.2  # untouched


def test_engine_live_modify_failure_returns_safe_result(monkeypatch):
    engine, client, spec = _engine(TradingMode.LIVE)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "modify-fail-1")
    assert result.success

    def broken_modify(ticket, stop_loss, take_profit):
        raise MT5ConnectionError("simulated")

    monkeypatch.setattr(client, "modify_position", broken_modify)
    modify_result = engine.modify_position(pos.ticket, stop_loss=2610.0, take_profit=2710.0)
    assert modify_result.success is False
    assert "MODIFY_FAILED" in modify_result.comment


def test_paper_close_position_rejects_stale_tick():
    engine, client, spec = _engine(TradingMode.PAPER)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "paper-close-stale-1")
    assert result.success

    client.set_tick_state("stale")
    close_result = engine.close_position(pos.ticket)
    assert close_result.success is False
    assert "STALE_QUOTE" in close_result.comment
    # position must remain open/tracked since the close was never confirmed
    assert pos.ticket in engine._paper_positions


def test_paper_close_position_partial_rejects_invalid_tick():
    engine, client, spec = _engine(TradingMode.PAPER)
    result, pos = engine.submit_market_order("BUY", 0.2, 2600.0, 2700.0, "paper-partial-invalid-1")
    assert result.success

    client.set_tick_state("invalid")
    partial_result = engine.close_position_partial(pos.ticket, 0.1)
    assert partial_result.success is False
    assert "INVALID_QUOTE" in partial_result.comment
    assert engine._paper_positions[pos.ticket].volume == 0.2  # untouched


def test_paper_mode_never_touches_any_order_submission_client_method_across_full_lifecycle(monkeypatch):
    """Open, modify, partially close, then fully close a position
    entirely in PAPER mode, and confirm submit_order/close_position/
    close_position_partial/modify_position are never called on the
    client at any point in that lifecycle -- not just at order entry."""
    engine, client, spec = _engine(TradingMode.PAPER)
    calls = {"submit_order": 0, "close_position": 0, "close_position_partial": 0, "modify_position": 0}
    for name in calls:
        original = getattr(client, name)

        def make_spy(counter_key, orig):
            def spy(*args, **kwargs):
                calls[counter_key] += 1
                return orig(*args, **kwargs)
            return spy

        monkeypatch.setattr(client, name, make_spy(name, original))

    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "paper-lifecycle-1")
    assert result.success and pos is not None
    engine.modify_position(pos.ticket, stop_loss=2610.0, take_profit=2710.0)
    engine.close_position_partial(pos.ticket, 0.02)
    engine.close_position(pos.ticket)

    assert all(v == 0 for v in calls.values()), f"PAPER mode reached a real order-submission method: {calls}"


def test_live_stale_tick_still_rejected_downstream_despite_gate_blind_spot():
    """ExecutionSafetyGateInput.market_data_fresh is hardcoded True
    (known limitation) -- this confirms that limitation does NOT create
    an actual safety hole: a stale tick still causes the order to be
    safely rejected by the client's own downstream check, even though
    the gate itself approved it thinking data was fresh."""
    engine, client, spec = _engine(TradingMode.LIVE)
    client.set_tick_state("stale")
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "live-stale-gate-blindspot-1")
    assert result.success is False
    assert "STALE_QUOTE" in result.comment
    assert pos is None
