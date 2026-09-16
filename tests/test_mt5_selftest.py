"""
Tests for app.mt5.selftest.run_mt5_selftest -- the read-only Demo-
connectivity self-test. Uses a controllable fake IMT5Client (not the
real or mock client) so each failure mode can be exercised precisely,
and so these tests double as a structural guarantee that the self-test
never calls anything beyond IMT5Client's read methods.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.config.settings import Settings
from app.mt5.interface import AccountInfo, IMT5Client, SymbolSpec, Tick
from app.mt5.selftest import run_mt5_selftest


def _spec(**overrides) -> SymbolSpec:
    base = dict(
        name="XAUUSD", contract_size=100.0, volume_min=0.01, volume_max=50.0,
        volume_step=0.01, tick_size=0.01, tick_value=1.0, digits=2,
        stops_level_points=50, freeze_level_points=10, trade_allowed=True,
        spread_points=20,
    )
    base.update(overrides)
    return SymbolSpec(**base)


def _tick(bid=2650.10, ask=2650.35, when=None) -> Tick:
    return Tick(symbol="XAUUSD", time=when or datetime.now(timezone.utc), bid=bid, ask=ask, last=bid, volume=1)


def _ohlcv(n=50, tf_minutes=15, fresh=True) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    if fresh:
        last_open = now - timedelta(minutes=1)
    else:
        last_open = now - timedelta(days=3)  # comfortably stale for every timeframe
    idx = [last_open - timedelta(minutes=tf_minutes * i) for i in range(n)][::-1]
    return pd.DataFrame(
        {"open": 2650.0, "high": 2651.0, "low": 2649.0, "close": 2650.5, "tick_volume": 100, "spread": 20},
        index=pd.DatetimeIndex(idx, name="time"),
    )


class FakeClient(IMT5Client):
    """Every method is independently overridable via constructor
    kwargs so tests can target one failure mode at a time without a
    mocking framework."""

    def __init__(self, **behavior):
        self.behavior = behavior
        self._connected = behavior.get("start_connected", False)

    def connect(self) -> bool:
        if "connect_raises" in self.behavior:
            raise self.behavior["connect_raises"]
        self._connected = self.behavior.get("connect_result", True)
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def discover_symbol(self, candidates):
        if "discover_symbol_raises" in self.behavior:
            raise self.behavior["discover_symbol_raises"]
        return self.behavior.get("discover_symbol_result", "XAUUSD")

    def get_symbol_spec(self, symbol):
        if "get_symbol_spec_raises" in self.behavior:
            raise self.behavior["get_symbol_spec_raises"]
        return self.behavior.get("get_symbol_spec_result", _spec())

    def get_account_info(self):
        if "get_account_info_raises" in self.behavior:
            raise self.behavior["get_account_info_raises"]
        return self.behavior.get("get_account_info_result", AccountInfo(
            login=1, balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0,
            currency="USD", leverage=100, trade_allowed=True, is_demo=True,
        ))

    def get_tick(self, symbol):
        if "get_tick_raises" in self.behavior:
            raise self.behavior["get_tick_raises"]
        return self.behavior.get("get_tick_result", _tick())

    def get_ohlcv(self, symbol, timeframe, count):
        if "get_ohlcv_raises" in self.behavior:
            raise self.behavior["get_ohlcv_raises"]
        per_tf = self.behavior.get("get_ohlcv_result_by_tf")
        if per_tf is not None:
            return per_tf[timeframe]
        return self.behavior.get("get_ohlcv_result", _ohlcv())

    def get_open_positions(self, symbol=None):
        return []

    # -- Order-path methods intentionally raise if ever called: the
    # self-test must NEVER call these. If a test ever fails because one
    # of these fired, that's the self-test violating its core contract. --
    def submit_order(self, request):
        raise AssertionError("run_mt5_selftest must never call submit_order")

    def close_position(self, ticket):
        raise AssertionError("run_mt5_selftest must never call close_position")

    def close_position_partial(self, ticket, volume):
        raise AssertionError("run_mt5_selftest must never call close_position_partial")

    def modify_position(self, ticket, stop_loss, take_profit):
        raise AssertionError("run_mt5_selftest must never call modify_position")


def _settings(**overrides) -> Settings:
    kwargs = dict(TRADING_MODE="PAPER", MT5_USE_MOCK=False)
    kwargs.update(overrides)
    return Settings(**kwargs)


def _status(report, name):
    return next(c for c in report.checks if c.name == name).status


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------

def test_all_checks_pass_and_overall_pass_true():
    client = FakeClient()
    report = run_mt5_selftest(client, _settings())
    assert report.overall_pass is True
    names = {c.name for c in report.checks}
    assert names == {
        "mt5_terminal_connection", "account_info", "demo_account_status",
        "symbol_discovery", "symbol_specification", "volume_constraints",
        "stop_level_constraints", "tick", "spread", "h4_data", "h1_data",
        "m15_data", "market_data_freshness",
    }
    assert all(c.passed for c in report.checks)


def test_connects_if_not_already_connected():
    client = FakeClient(start_connected=False)
    assert client.is_connected() is False
    report = run_mt5_selftest(client, _settings())
    assert client.is_connected() is True
    assert _status(report, "mt5_terminal_connection") == "PASS"


def test_does_not_reconnect_if_already_connected():
    calls = {"connect": 0}

    class CountingClient(FakeClient):
        def connect(self):
            calls["connect"] += 1
            return super().connect()

    client = CountingClient(start_connected=True)
    run_mt5_selftest(client, _settings())
    assert calls["connect"] == 0


# ---------------------------------------------------------------------
# Fail-closed: connection failure blocks everything downstream
# ---------------------------------------------------------------------

def test_connection_failure_blocks_all_downstream_checks():
    client = FakeClient(connect_raises=ConnectionRefusedError("no terminal"))
    report = run_mt5_selftest(client, _settings())
    assert report.overall_pass is False
    assert _status(report, "mt5_terminal_connection") == "FAIL"
    for name in ("account_info", "symbol_discovery", "tick", "h4_data", "market_data_freshness"):
        assert _status(report, name) == "BLOCKED"


def test_connect_returns_false_without_raising_is_still_a_fail():
    client = FakeClient(connect_result=False)
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "mt5_terminal_connection") == "FAIL"
    assert report.overall_pass is False


# ---------------------------------------------------------------------
# Demo-account status: the one check where valid data can still FAIL
# ---------------------------------------------------------------------

def test_real_money_account_hard_fails_even_though_data_is_valid():
    real_account = AccountInfo(
        login=1, balance=5000.0, equity=5000.0, margin=0.0, margin_free=5000.0,
        currency="USD", leverage=100, trade_allowed=True, is_demo=False,
    )
    client = FakeClient(get_account_info_result=real_account)
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "demo_account_status") == "FAIL"
    assert report.overall_pass is False
    # every other check is independent of demo-status and should still
    # be evaluated/pass, so the report is informative, not just "FAIL"
    assert _status(report, "account_info") == "PASS"
    assert _status(report, "tick") == "PASS"


def test_unknown_demo_status_fails_closed_rather_than_assumed_safe():
    unknown_account = AccountInfo(
        login=1, balance=5000.0, equity=5000.0, margin=0.0, margin_free=5000.0,
        currency="USD", leverage=100, trade_allowed=True, is_demo=None,
    )
    client = FakeClient(get_account_info_result=unknown_account)
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "demo_account_status") == "FAIL"
    assert report.overall_pass is False


def test_account_info_exception_blocks_demo_status_check():
    client = FakeClient(get_account_info_raises=RuntimeError("account_info() failed"))
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "account_info") == "FAIL"
    assert _status(report, "demo_account_status") == "BLOCKED"


# ---------------------------------------------------------------------
# Symbol discovery / spec / volume / stop-level
# ---------------------------------------------------------------------

def test_symbol_discovery_failure_blocks_spec_and_downstream():
    client = FakeClient(discover_symbol_result=None)
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "symbol_discovery") == "FAIL"
    for name in ("symbol_specification", "volume_constraints", "stop_level_constraints", "tick", "spread"):
        assert _status(report, name) == "BLOCKED"


def test_trade_disabled_symbol_fails_spec_and_blocks_constraints():
    client = FakeClient(get_symbol_spec_result=_spec(trade_allowed=False))
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "symbol_specification") == "FAIL"
    assert _status(report, "volume_constraints") == "BLOCKED"
    assert _status(report, "stop_level_constraints") == "BLOCKED"


@pytest.mark.parametrize("bad_spec", [
    _spec(volume_min=0),
    _spec(volume_step=0),
    _spec(volume_max=0.001, volume_min=0.01),  # max < min
])
def test_invalid_volume_constraints_fail(bad_spec):
    client = FakeClient(get_symbol_spec_result=bad_spec)
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "volume_constraints") == "FAIL"


@pytest.mark.parametrize("bad_spec", [
    _spec(stops_level_points=-1),
    _spec(freeze_level_points=-1),
])
def test_invalid_stop_level_constraints_fail(bad_spec):
    client = FakeClient(get_symbol_spec_result=bad_spec)
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "stop_level_constraints") == "FAIL"


# ---------------------------------------------------------------------
# Tick sanity + spread
# ---------------------------------------------------------------------

@pytest.mark.parametrize("bad_tick", [
    _tick(bid=0, ask=2650.35),
    _tick(bid=2650.10, ask=0),
    _tick(bid=2650.50, ask=2650.10),  # ask < bid
])
def test_invalid_tick_fails_and_blocks_spread(bad_tick):
    client = FakeClient(get_tick_result=bad_tick)
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "tick") == "FAIL"
    assert _status(report, "spread") == "BLOCKED"


def test_wide_spread_is_reported_but_does_not_fail_the_selftest():
    # A wide spread is a live-market condition for the risk engine to
    # gate at signal time, not a connectivity fault -- the self-test
    # should surface it, not fail on it.
    wide_tick = _tick(bid=2650.00, ask=2653.00)  # far exceeds default max_spread_points
    client = FakeClient(get_tick_result=wide_tick)
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "spread") == "PASS"
    check = next(c for c in report.checks if c.name == "spread")
    assert "exceeds" in check.detail


# ---------------------------------------------------------------------
# H4/H1/M15 + freshness
# ---------------------------------------------------------------------

def test_stale_data_fails_freshness_and_the_specific_timeframe():
    client = FakeClient(get_ohlcv_result=_ohlcv(fresh=False))
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "h4_data") == "FAIL"
    assert _status(report, "market_data_freshness") == "FAIL"
    assert report.overall_pass is False


def test_one_stale_timeframe_fails_freshness_even_if_others_are_fresh():
    per_tf = {"H4": _ohlcv(fresh=True), "H1": _ohlcv(fresh=False), "M15": _ohlcv(fresh=True)}
    client = FakeClient(get_ohlcv_result_by_tf=per_tf)
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "h4_data") == "PASS"
    assert _status(report, "h1_data") == "FAIL"
    assert _status(report, "m15_data") == "PASS"
    assert _status(report, "market_data_freshness") == "FAIL"


def test_ohlcv_exception_fails_that_timeframe_and_freshness():
    client = FakeClient(get_ohlcv_raises=RuntimeError("copy_rates_from_pos returned None"))
    report = run_mt5_selftest(client, _settings())
    assert _status(report, "h4_data") == "FAIL"
    assert _status(report, "market_data_freshness") == "FAIL"


# ---------------------------------------------------------------------
# Report shape / serialization
# ---------------------------------------------------------------------

def test_as_dict_is_json_serializable():
    import json

    client = FakeClient()
    report = run_mt5_selftest(client, _settings())
    serialized = json.dumps(report.as_dict())
    assert '"overall_pass": true' in serialized
