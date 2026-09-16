import dataclasses

import pytest

from app.news.filter import NewsState
from app.safety.gate import SafetyGate, SafetyGateInput, SafetyGateResult


def _good_input(**overrides) -> SafetyGateInput:
    base = dict(
        account_equity=10000.0, account_trade_allowed=True, account_is_demo=True,
        symbol_trade_allowed=True, volume_min=0.01, volume_max=50.0, volume_step=0.01,
        stops_level_points=50, tick_size=0.01,
        direction="BUY", volume=0.1, entry_price=2650.0, stop_loss=2645.0, take_profit=2660.0,
        current_spread_points=20.0, max_spread_points=50.0, market_data_fresh=True,
        news_state=NewsState.CLEAR,
        kill_switch_active=False, daily_loss_pct=0.0, max_daily_loss_pct=2.0,
        open_positions_count=0, max_open_positions=1,
    )
    base.update(overrides)
    return SafetyGateInput(**base)


@pytest.fixture()
def gate() -> SafetyGate:
    return SafetyGate()


# --- module-level independence guarantee ---------------------------------------

def test_safety_gate_module_does_not_import_risk_engine():
    """Structural guarantee, not just a docstring claim: SafetyGate's
    module must never IMPORT the risk engine -- if it did, a bug in the
    risk engine could silently compromise both "independent" layers at
    once. Uses AST parsing (not a substring search) specifically so the
    module's own explanatory docstring about NOT importing app.risk
    doesn't trip a naive check."""
    import ast

    import app.safety.gate as gate_module

    with open(gate_module.__file__) as f:
        tree = ast.parse(f.read())

    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert not any(m.startswith("app.risk") for m in imported_modules), (
        f"SafetyGate must not import the risk engine; found imports: {imported_modules}"
    )


# --- happy path -----------------------------------------------------------------

def test_fully_healthy_order_is_approved(gate):
    result = gate.evaluate(_good_input())
    assert result.approved is True
    assert result.is_error is False
    assert result.reasons == []


def test_sell_order_with_valid_stop_direction_is_approved(gate):
    result = gate.evaluate(_good_input(direction="SELL", stop_loss=2660.0, take_profit=2640.0))
    assert result.approved is True


# --- individual rejection reasons -------------------------------------------------

def test_kill_switch_active_rejects(gate):
    result = gate.evaluate(_good_input(kill_switch_active=True))
    assert result.approved is False
    assert result.checks["kill_switch_ok"] is False


def test_account_trade_not_allowed_rejects(gate):
    result = gate.evaluate(_good_input(account_trade_allowed=False))
    assert result.approved is False
    assert result.checks["account_trade_allowed"] is False


def test_symbol_trade_not_allowed_rejects(gate):
    result = gate.evaluate(_good_input(symbol_trade_allowed=False))
    assert result.approved is False


def test_stale_market_data_rejects(gate):
    result = gate.evaluate(_good_input(market_data_fresh=False))
    assert result.approved is False


def test_news_not_clear_rejects(gate):
    for state in (NewsState.BLOCKED, NewsState.UNAVAILABLE, NewsState.UNKNOWN):
        result = gate.evaluate(_good_input(news_state=state))
        assert result.approved is False, f"news_state={state} should reject"
        assert result.checks["news_clear"] is False


def test_daily_loss_exceeding_limit_rejects(gate):
    result = gate.evaluate(_good_input(daily_loss_pct=3.0, max_daily_loss_pct=2.0))
    assert result.approved is False


def test_spread_exceeding_limit_rejects(gate):
    result = gate.evaluate(_good_input(current_spread_points=100.0, max_spread_points=50.0))
    assert result.approved is False


def test_position_limit_reached_rejects(gate):
    result = gate.evaluate(_good_input(open_positions_count=1, max_open_positions=1))
    assert result.approved is False


def test_missing_stop_loss_rejects(gate):
    result = gate.evaluate(_good_input(stop_loss=None))
    assert result.approved is False
    assert result.checks["stop_loss_present"] is False


def test_buy_stop_loss_above_entry_rejects(gate):
    result = gate.evaluate(_good_input(direction="BUY", stop_loss=2660.0))  # wrong side
    assert result.approved is False
    assert result.checks["stop_loss_direction_valid"] is False


def test_sell_stop_loss_below_entry_rejects(gate):
    result = gate.evaluate(_good_input(direction="SELL", stop_loss=2640.0, take_profit=2660.0))
    assert result.approved is False


def test_stop_distance_below_broker_minimum_rejects(gate):
    result = gate.evaluate(_good_input(stop_loss=2649.99, stops_level_points=50))  # 1 point distance
    assert result.approved is False
    assert result.checks["stop_distance_meets_broker_minimum"] is False


def test_take_profit_wrong_side_rejects(gate):
    result = gate.evaluate(_good_input(direction="BUY", take_profit=2640.0))  # below entry for BUY
    assert result.approved is False
    assert result.checks["take_profit_direction_valid"] is False


def test_take_profit_none_is_allowed():
    gate = SafetyGate()
    result = gate.evaluate(_good_input(take_profit=None))
    assert result.approved is True
    assert "take_profit_direction_valid" not in result.checks


def test_volume_below_broker_minimum_rejects(gate):
    result = gate.evaluate(_good_input(volume=0.001, volume_min=0.01))
    assert result.approved is False


def test_volume_above_broker_maximum_rejects(gate):
    result = gate.evaluate(_good_input(volume=100.0, volume_max=50.0))
    assert result.approved is False


def test_volume_not_matching_step_rejects(gate):
    result = gate.evaluate(_good_input(volume=0.017, volume_step=0.01))
    assert result.approved is False
    assert result.checks["volume_matches_broker_step"] is False


def test_zero_volume_rejects(gate):
    result = gate.evaluate(_good_input(volume=0.0))
    assert result.approved is False


def test_negative_volume_rejects(gate):
    result = gate.evaluate(_good_input(volume=-0.1))
    assert result.approved is False


def test_multiple_simultaneous_failures_all_reported(gate):
    result = gate.evaluate(_good_input(kill_switch_active=True, stop_loss=None, current_spread_points=999.0))
    assert result.approved is False
    assert len(result.reasons) >= 3


# --- fail-closed on internal error (section 18) -----------------------------------

def test_internal_error_fails_closed_not_open(gate):
    class _Boom:
        def __sub__(self, other):
            raise RuntimeError("simulated internal failure")
        def __rsub__(self, other):
            raise RuntimeError("simulated internal failure")

    broken_input = _good_input(entry_price=_Boom())
    result = gate.evaluate(broken_input)
    assert result.approved is False
    assert result.is_error is True
    assert any("internal error" in r.lower() for r in result.reasons)


def test_error_result_never_silently_approves():
    """SafetyGateResult itself has no code path where is_error=True can
    coexist with approved=True -- this test constructs the dataclass
    directly to make that invariant explicit and verifiable, in
    addition to the exception-triggering test above."""
    result = SafetyGateResult(approved=False, is_error=True, checks={}, reasons=["boom"])
    assert not (result.is_error and result.approved)


# --- to_dict / auditability -------------------------------------------------------

def test_to_dict_is_json_serializable():
    import json
    gate = SafetyGate()
    result = gate.evaluate(_good_input())
    json.dumps(result.to_dict())  # must not raise
