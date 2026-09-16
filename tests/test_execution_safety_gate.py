import tempfile

import pytest

from app.execution.execution_safety_gate import ExecutionSafetyGate, ExecutionSafetyGateInput, ExecutionSafetyGateResult


def _good_input(**overrides) -> ExecutionSafetyGateInput:
    base = dict(
        is_duplicate=False,
        kill_switch_active=False,
        account_trade_allowed=True,
        symbol_trade_allowed=True,
        direction="BUY",
        volume=0.1,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
        stop_loss=2645.0,
        market_data_fresh=True,
    )
    base.update(overrides)
    return ExecutionSafetyGateInput(**base)


@pytest.fixture()
def gate() -> ExecutionSafetyGate:
    return ExecutionSafetyGate()


def test_healthy_order_is_approved(gate):
    result = gate.evaluate(_good_input())
    assert result.approved is True
    assert result.is_error is False
    assert result.reasons == []


def test_duplicate_rejects(gate):
    result = gate.evaluate(_good_input(is_duplicate=True))
    assert result.approved is False
    assert result.checks["not_duplicate"] is False


def test_kill_switch_active_rejects(gate):
    result = gate.evaluate(_good_input(kill_switch_active=True))
    assert result.approved is False


def test_account_trade_not_allowed_rejects(gate):
    result = gate.evaluate(_good_input(account_trade_allowed=False))
    assert result.approved is False


def test_symbol_trade_not_allowed_rejects(gate):
    result = gate.evaluate(_good_input(symbol_trade_allowed=False))
    assert result.approved is False


def test_stale_market_data_rejects(gate):
    result = gate.evaluate(_good_input(market_data_fresh=False))
    assert result.approved is False


def test_invalid_direction_rejects(gate):
    result = gate.evaluate(_good_input(direction="SIDEWAYS"))
    assert result.approved is False
    assert result.checks["direction_valid"] is False


def test_missing_stop_loss_rejects(gate):
    result = gate.evaluate(_good_input(stop_loss=None))
    assert result.approved is False
    assert result.checks["stop_loss_present"] is False


def test_volume_below_minimum_rejects(gate):
    result = gate.evaluate(_good_input(volume=0.001, volume_min=0.01))
    assert result.approved is False


def test_volume_above_maximum_rejects(gate):
    result = gate.evaluate(_good_input(volume=100.0, volume_max=50.0))
    assert result.approved is False


def test_volume_not_matching_step_rejects(gate):
    result = gate.evaluate(_good_input(volume=0.017, volume_step=0.01))
    assert result.approved is False


def test_zero_or_negative_volume_rejects(gate):
    assert gate.evaluate(_good_input(volume=0.0)).approved is False
    assert gate.evaluate(_good_input(volume=-0.1)).approved is False


def test_multiple_failures_all_reported(gate):
    result = gate.evaluate(_good_input(kill_switch_active=True, stop_loss=None, volume=-1))
    assert result.approved is False
    assert len(result.reasons) >= 3


def test_internal_error_fails_closed(gate):
    class _Boom:
        def __le__(self, other):
            raise RuntimeError("simulated failure")
        def __ge__(self, other):
            raise RuntimeError("simulated failure")

    result = gate.evaluate(_good_input(volume=_Boom()))
    assert result.approved is False
    assert result.is_error is True


def test_error_result_never_coexists_with_approval():
    result = ExecutionSafetyGateResult(approved=False, is_error=True, checks={}, reasons=["boom"])
    assert not (result.is_error and result.approved)


def test_kill_switch_blocks_a_direct_call_that_bypasses_the_risk_engine_entirely():
    """The core architectural property of this phase (audit section 56:
    "NO DIRECT STRATEGY -> MT5 ACCESS"): a caller that skips TradeValidator
    and SafetyGate entirely and calls ExecutionEngine.submit_market_order()
    directly -- exactly what a bug, a future UI button, or a careless
    integration might do -- is STILL blocked, because the check lives
    inside the one function that is allowed to reach MT5."""
    from app.config.settings import TradingMode
    from app.execution.engine import ExecutionEngine
    from app.mt5.mock_client import MockMT5Client
    from app.risk.kill_switch import KillSwitch

    client = MockMT5Client()
    client.connect()
    spec = client.get_symbol_spec("XAUUSD")
    ks = KillSwitch(tempfile.mktemp(suffix=".json"))
    ks.activate("test halt", activated_by="tester")

    engine = ExecutionEngine(client, spec, TradingMode.PAPER, kill_switch=ks)
    result, position = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "direct-call-1")
    assert result.success is False
    assert "EXECUTION_SAFETY_GATE_REJECTED" in result.comment
    assert "Kill switch" in result.comment
    assert position is None
    assert engine.get_open_positions() == []


def test_missing_stop_loss_blocked_even_on_a_direct_call():
    from app.config.settings import TradingMode
    from app.execution.engine import ExecutionEngine
    from app.mt5.mock_client import MockMT5Client

    client = MockMT5Client()
    client.connect()
    spec = client.get_symbol_spec("XAUUSD")
    engine = ExecutionEngine(client, spec, TradingMode.PAPER)

    result, position = engine.submit_market_order("BUY", 0.1, None, 2700.0, "direct-call-2")
    assert result.success is False
    assert "EXECUTION_SAFETY_GATE_REJECTED" in result.comment
    assert position is None


def test_invalid_volume_blocked_even_on_a_direct_call():
    from app.config.settings import TradingMode
    from app.execution.engine import ExecutionEngine
    from app.mt5.mock_client import MockMT5Client

    client = MockMT5Client()
    client.connect()
    spec = client.get_symbol_spec("XAUUSD")
    engine = ExecutionEngine(client, spec, TradingMode.PAPER)

    result, position = engine.submit_market_order("BUY", 999.0, 2600.0, 2700.0, "direct-call-3")
    assert result.success is False
    assert position is None


def test_healthy_direct_order_still_succeeds():
    """The gate must not be so strict it blocks legitimate orders --
    confirms the happy path still works when everything is valid."""
    from app.config.settings import TradingMode
    from app.execution.engine import ExecutionEngine
    from app.mt5.mock_client import MockMT5Client

    client = MockMT5Client()
    client.connect()
    spec = client.get_symbol_spec("XAUUSD")
    engine = ExecutionEngine(client, spec, TradingMode.PAPER)

    result, position = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "direct-call-4")
    assert result.success is True
    assert position is not None
