"""
Tests for app.safety.startup_check.run_startup_safety_check.

Uses a controllable FakeClient (same pattern as
tests/test_mt5_selftest.py) so each failure mode can be exercised
precisely, and so these tests double as a structural guarantee that
the startup safety check never calls an order-submission method.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.config.settings import RiskSettings, Settings
from app.execution.execution_safety_gate import ExecutionSafetyGate
from app.execution.state_store import ExecutionRecord, STATUS_UNKNOWN, SqliteExecutionStateStore
from app.journal.journal import TradeJournal
from app.mt5.interface import AccountInfo, IMT5Client, SymbolSpec, Tick
from app.news.filter import AlwaysClearNewsFilter, BlockedNewsFilter, NewsFilter, NewsState, NewsStatus, UnavailableNewsFilter
from app.risk.kill_switch import KillSwitch
from app.safety.gate import SafetyGate
from app.safety.startup_check import (
    CheckStatus,
    format_startup_report_text,
    run_startup_safety_check,
)


# ---------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------
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


def _ohlcv(n=250, tf_minutes=15, fresh=True) -> pd.DataFrame:
    """Constant-price OHLCV -- fine for freshness/staleness checks
    (app.mt5.selftest, market_data_freshness), which only look at bar
    count and timestamps. NOT suitable for feature computation: with
    every high/low/close identical bar-over-bar, +DM/-DM are zero
    everywhere, which makes ADX a 0/0 -> NaN degenerate case. Use
    _realistic_ohlcv() (below) wherever compute_features() needs to
    succeed with finite values."""
    now = datetime.now(timezone.utc)
    last_open = now - timedelta(minutes=1) if fresh else now - timedelta(days=3)
    idx = [last_open - timedelta(minutes=tf_minutes * i) for i in range(n)][::-1]
    return pd.DataFrame(
        {"open": 2650.0, "high": 2655.0, "low": 2645.0, "close": 2650.5, "tick_volume": 100, "spread": 20},
        index=pd.DatetimeIndex(idx, name="time"),
    )


def _realistic_ohlcv(n=250, tf_minutes=15, fresh=True, seed=7) -> pd.DataFrame:
    """A seeded random-walk OHLCV series with genuine bar-to-bar price
    movement, so trend/momentum/volatility indicators (ADX, RSI, ATR,
    ...) are well-defined finite values rather than the constant-price
    fixture's 0/0 -> NaN degenerate case."""
    import numpy as np

    rng = np.random.default_rng(seed)
    now = datetime.now(timezone.utc)
    last_open = now - timedelta(minutes=1) if fresh else now - timedelta(days=3)
    idx = [last_open - timedelta(minutes=tf_minutes * i) for i in range(n)][::-1]

    steps = rng.normal(0, 1.0, size=n)
    close = 2650.0 + np.cumsum(steps)
    open_ = close - rng.normal(0, 0.3, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.5, size=n))
    tick_volume = rng.integers(50, 500, size=n)

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "tick_volume": tick_volume, "spread": 20},
        index=pd.DatetimeIndex(idx, name="time"),
    )


class FakeClient(IMT5Client):
    """Every method independently overridable via constructor kwargs,
    same pattern as tests/test_mt5_selftest.py's FakeClient."""

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
        # Default is the REALISTIC (varying-price) fixture, not the
        # constant-price one, so that "everything should pass" tests
        # don't spuriously fail required_features on a degenerate
        # NaN ADX -- see _ohlcv()'s docstring above.
        return self.behavior.get("get_ohlcv_result", _realistic_ohlcv())

    def get_open_positions(self, symbol=None):
        return []

    # Order-path methods must NEVER be called by the startup safety
    # check -- if any of these fire, that's this module violating its
    # core contract.
    def submit_order(self, request):
        raise AssertionError("StartupSafetyCheck must never call submit_order")

    def close_position(self, ticket):
        raise AssertionError("StartupSafetyCheck must never call close_position")

    def close_position_partial(self, ticket, volume):
        raise AssertionError("StartupSafetyCheck must never call close_position_partial")

    def modify_position(self, ticket, stop_loss, take_profit):
        raise AssertionError("StartupSafetyCheck must never call modify_position")


def _settings(**overrides) -> Settings:
    kwargs = dict(_env_file=None, TRADING_MODE="PAPER", MT5_USE_MOCK=False)
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.fixture()
def journal(tmp_path) -> TradeJournal:
    return TradeJournal(str(tmp_path / "journal.db"))


@pytest.fixture()
def kill_switch(tmp_path) -> KillSwitch:
    return KillSwitch(str(tmp_path / "kill_switch.json"))


def _by_name(result, name):
    return next(c for c in result.checks if c.name == name)


# ---------------------------------------------------------------------
# 1. All checks pass -> startup allowed
# ---------------------------------------------------------------------
def test_all_checks_pass_startup_allowed(journal, kill_switch):
    client = FakeClient()
    result = run_startup_safety_check(
        _settings(), client=client, journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_ALLOWED"
    assert result.trading_allowed is True
    assert result.failed_checks == []
    assert result.monitoring_only is False


# ---------------------------------------------------------------------
# 2. Invalid configuration -> startup blocked
# ---------------------------------------------------------------------
def test_invalid_configuration_blocks_startup(journal, kill_switch):
    settings = _settings(SYMBOL_CANDIDATES="")
    client = FakeClient()
    result = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch, news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "configuration" in result.failed_checks


# ---------------------------------------------------------------------
# 3. MT5 unavailable -> startup blocked
# ---------------------------------------------------------------------
def test_mt5_unavailable_blocks_startup(journal, kill_switch):
    client = FakeClient(connect_raises=ConnectionError("terminal not found"))
    result = run_startup_safety_check(
        _settings(), client=client, journal=journal, kill_switch=kill_switch, news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "mt5_connectivity" in result.failed_checks
    # Everything downstream of MT5 must also be blocked, not silently passed.
    assert _by_name(result, "account_information").status in (CheckStatus.FAIL, CheckStatus.BLOCKED)


def test_no_mt5_client_provided_blocks_startup_when_required(journal, kill_switch):
    result = run_startup_safety_check(
        _settings(), client=None, journal=journal, kill_switch=kill_switch, news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "mt5_connectivity" in result.failed_checks


# ---------------------------------------------------------------------
# 4. Account information unavailable -> startup blocked
# ---------------------------------------------------------------------
def test_account_info_unavailable_blocks_startup(journal, kill_switch):
    client = FakeClient(get_account_info_raises=RuntimeError("no account"))
    result = run_startup_safety_check(
        _settings(), client=client, journal=journal, kill_switch=kill_switch, news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "account_information" in result.failed_checks


# ---------------------------------------------------------------------
# 5-7. DEMO mode account verification
# ---------------------------------------------------------------------
def test_demo_mode_with_demo_account_allowed(journal, kill_switch):
    settings = _settings(
        TRADING_MODE="LIVE", MT5_LOGIN=1, MT5_PASSWORD="x", MT5_SERVER="s",
    )
    client = FakeClient(get_account_info_result=AccountInfo(
        login=1, balance=1000.0, equity=1000.0, margin=0.0, margin_free=1000.0,
        currency="USD", leverage=100, trade_allowed=True, is_demo=True,
    ))
    result = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), require_demo_account=True,
    )
    assert result.overall_status == "TRADING_ALLOWED"
    assert _by_name(result, "account_type_verification").status == CheckStatus.PASS


def test_demo_mode_with_real_account_blocked(journal, kill_switch):
    settings = _settings(TRADING_MODE="LIVE", MT5_LOGIN=1, MT5_PASSWORD="x", MT5_SERVER="s")
    client = FakeClient(get_account_info_result=AccountInfo(
        login=1, balance=1000.0, equity=1000.0, margin=0.0, margin_free=1000.0,
        currency="USD", leverage=100, trade_allowed=True, is_demo=False,
    ))
    result = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), require_demo_account=True,
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "account_type_verification" in result.failed_checks


def test_demo_mode_with_unknown_account_type_blocked(journal, kill_switch):
    settings = _settings(TRADING_MODE="LIVE", MT5_LOGIN=1, MT5_PASSWORD="x", MT5_SERVER="s")
    client = FakeClient(get_account_info_result=AccountInfo(
        login=1, balance=1000.0, equity=1000.0, margin=0.0, margin_free=1000.0,
        currency="USD", leverage=100, trade_allowed=True, is_demo=None,
    ))
    result = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), require_demo_account=True,
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "account_type_verification" in result.failed_checks


# ---------------------------------------------------------------------
# 8-9. LIVE mode: mock rejected / unknown account type blocked
# ---------------------------------------------------------------------
def test_live_mode_with_mock_mt5_blocked(journal, kill_switch):
    settings = _settings(TRADING_MODE="LIVE", MT5_USE_MOCK=True, MT5_LOGIN=1, MT5_PASSWORD="x", MT5_SERVER="s")
    client = FakeClient()
    result = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), require_demo_account=False,
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "trading_mode" in result.failed_checks


def test_live_mode_with_unknown_account_type_blocked(journal, kill_switch):
    settings = _settings(TRADING_MODE="LIVE", MT5_LOGIN=1, MT5_PASSWORD="x", MT5_SERVER="s")
    client = FakeClient(get_account_info_result=AccountInfo(
        login=1, balance=1000.0, equity=1000.0, margin=0.0, margin_free=1000.0,
        currency="USD", leverage=100, trade_allowed=True, is_demo=None,
    ))
    result = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), require_demo_account=False,
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "account_type_verification" in result.failed_checks


def test_live_mode_with_verified_real_account_allowed(journal, kill_switch):
    settings = _settings(TRADING_MODE="LIVE", MT5_LOGIN=1, MT5_PASSWORD="x", MT5_SERVER="s")
    client = FakeClient(get_account_info_result=AccountInfo(
        login=1, balance=1000.0, equity=1000.0, margin=0.0, margin_free=1000.0,
        currency="USD", leverage=100, trade_allowed=True, is_demo=False,
    ))
    result = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), require_demo_account=False,
    )
    assert result.overall_status == "TRADING_ALLOWED"


# ---------------------------------------------------------------------
# 10-11. Symbol unavailable / trading disabled -> blocked
# ---------------------------------------------------------------------
def test_symbol_unavailable_blocks_startup(journal, kill_switch):
    client = FakeClient(discover_symbol_result=None)
    result = run_startup_safety_check(
        _settings(), client=client, journal=journal, kill_switch=kill_switch, news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "symbol_availability" in result.failed_checks


def test_symbol_trading_disabled_blocks_startup(journal, kill_switch):
    client = FakeClient(get_symbol_spec_result=_spec(trade_allowed=False))
    result = run_startup_safety_check(
        _settings(), client=client, journal=journal, kill_switch=kill_switch, news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "symbol_trading_permissions" in result.failed_checks


# ---------------------------------------------------------------------
# 12. Market data stale -> blocked
# ---------------------------------------------------------------------
def test_market_data_stale_blocks_startup(journal, kill_switch):
    client = FakeClient(get_ohlcv_result=_ohlcv(fresh=False))
    result = run_startup_safety_check(
        _settings(), client=client, journal=journal, kill_switch=kill_switch, news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "market_data_freshness" in result.failed_checks


# ---------------------------------------------------------------------
# 13-14. Required feature missing / NaN -> blocked
# ---------------------------------------------------------------------
def test_required_feature_missing_insufficient_history_blocks_startup(journal, kill_switch):
    # Too few bars for compute_features' MIN_BARS_REQUIRED (210).
    short_df = _ohlcv(n=50, fresh=True)
    client = FakeClient(get_ohlcv_result=short_df)
    result = run_startup_safety_check(
        _settings(), client=client, journal=journal, kill_switch=kill_switch, news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "required_features" in result.failed_checks


def test_required_feature_nan_blocks_startup(journal, kill_switch):
    df = _realistic_ohlcv(n=250, fresh=True).copy()
    df.loc[df.index[-1], "close"] = float("nan")
    client = FakeClient(get_ohlcv_result=df)
    result = run_startup_safety_check(
        _settings(), client=client, journal=journal, kill_switch=kill_switch, news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "required_features" in result.failed_checks


def test_no_strategies_enabled_features_not_required(journal, kill_switch):
    settings = _settings(
        STRATEGY_TREND_PULLBACK_ENABLED=False,
        STRATEGY_BREAKOUT_ENABLED=False,
        STRATEGY_MEAN_REVERSION_ENABLED=False,
    )
    client = FakeClient(get_ohlcv_result=_ohlcv(n=5))  # would otherwise fail
    result = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch, news_filter=AlwaysClearNewsFilter(),
    )
    assert _by_name(result, "required_features").status == CheckStatus.NOT_REQUIRED
    assert result.overall_status == "TRADING_ALLOWED"


# ---------------------------------------------------------------------
# 15-18. News state
# ---------------------------------------------------------------------
def test_news_clear_allowed_if_all_else_passes(journal, kill_switch):
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_ALLOWED"
    assert _by_name(result, "news_state").status == CheckStatus.PASS


def test_news_blocked_blocks_startup(journal, kill_switch):
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=BlockedNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "news_state" in result.failed_checks


def test_news_unavailable_blocks_startup(journal, kill_switch):
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=UnavailableNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "news_state" in result.failed_checks
    assert _by_name(result, "news_state").details["state"] == NewsState.UNAVAILABLE.value


class _UnknownNewsFilter(NewsFilter):
    def check(self, at):
        return NewsStatus(state=NewsState.UNKNOWN, reason="Outside known coverage window.")


def test_news_unknown_blocks_startup(journal, kill_switch):
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=_UnknownNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "news_state" in result.failed_checks
    assert _by_name(result, "news_state").details["state"] == NewsState.UNKNOWN.value


def test_research_mode_news_not_required(journal, kill_switch):
    settings = _settings(TRADING_MODE="BACKTEST")
    result = run_startup_safety_check(settings, client=None, journal=journal, kill_switch=kill_switch)
    assert _by_name(result, "news_state").status == CheckStatus.NOT_REQUIRED


# ---------------------------------------------------------------------
# 19. Journal unavailable -> blocked
# ---------------------------------------------------------------------
def test_journal_unavailable_blocks_startup(tmp_path, kill_switch):
    class BrokenJournal:
        def _connect(self):
            raise OSError("disk full")

    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=BrokenJournal(), kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "journal_writable" in result.failed_checks


def test_journal_writable_passes_and_leaves_no_residue(journal, kill_switch):
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(),
    )
    assert _by_name(result, "journal_writable").status == CheckStatus.PASS
    with journal._connect() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM _startup_writability_probe").fetchone()
        assert rows[0] == 0


# ---------------------------------------------------------------------
# 20-21. Kill switch
# ---------------------------------------------------------------------
def test_kill_switch_active_blocks_new_trades_but_is_monitoring_only(journal, kill_switch):
    kill_switch.activate("manual halt", activated_by="tester")
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert result.monitoring_only is True
    assert _by_name(result, "kill_switch").status == CheckStatus.BLOCKED


def test_kill_switch_state_unavailable_blocks_startup(journal):
    class BrokenKillSwitch:
        def status(self):
            raise OSError("cannot read state file")

    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=BrokenKillSwitch(),
        news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert result.monitoring_only is False
    assert "kill_switch" in result.failed_checks


# ---------------------------------------------------------------------
# 22. Invalid risk configuration -> blocked
# ---------------------------------------------------------------------
def test_invalid_risk_configuration_blocks_startup(journal, kill_switch):
    risk = RiskSettings(_env_file=None, risk_per_trade_pct=4.0, max_daily_loss_pct=2.0)
    settings = _settings(risk=risk)
    result = run_startup_safety_check(
        settings, client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "risk_configuration" in result.failed_checks


# ---------------------------------------------------------------------
# 23-24. Safety / execution gate unavailable -> blocked
# ---------------------------------------------------------------------
def test_safety_gate_unavailable_blocks_startup(journal, kill_switch):
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), safety_gate=None,
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "safety_gate" in result.failed_checks


def test_execution_gate_unavailable_blocks_startup(journal, kill_switch):
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), execution_safety_gate=None,
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "execution_gate" in result.failed_checks


def test_safety_and_execution_gate_available_by_default(journal, kill_switch):
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(),
    )
    assert _by_name(result, "safety_gate").status == CheckStatus.PASS
    assert _by_name(result, "execution_gate").status == CheckStatus.PASS


def test_explicit_gate_instances_are_used(journal, kill_switch):
    sg = SafetyGate()
    esg = ExecutionSafetyGate()
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), safety_gate=sg, execution_safety_gate=esg,
    )
    assert _by_name(result, "safety_gate").status == CheckStatus.PASS
    assert _by_name(result, "execution_gate").status == CheckStatus.PASS


# ---------------------------------------------------------------------
# 25. Clock/time invalid -> blocked
# ---------------------------------------------------------------------
def test_naive_clock_reference_blocks_startup(journal, kill_switch):
    naive_now = datetime.now()  # no tzinfo
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), now=naive_now,
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "clock" in result.failed_checks


def test_impossible_clock_skew_blocks_startup(journal, kill_switch):
    far_future = datetime.now(timezone.utc) + timedelta(hours=6)
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), now=far_future,
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "clock" in result.failed_checks


# ---------------------------------------------------------------------
# 26. Required dependency unavailable -> blocked
# ---------------------------------------------------------------------
def test_missing_client_dependency_blocks_startup(journal, kill_switch):
    # A required MT5 client dependency that simply isn't available.
    result = run_startup_safety_check(
        _settings(), client=None, journal=journal, kill_switch=kill_switch, news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "dependency_health" in result.failed_checks


def test_missing_core_data_dependency_blocks_startup(journal, kill_switch, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("No module named 'numpy'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = run_startup_safety_check(
        _settings(TRADING_MODE="BACKTEST"), client=None, journal=journal, kill_switch=kill_switch,
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert "dependency_health" in result.failed_checks


# ---------------------------------------------------------------------
# 27. PAPER + MOCK works without a real MT5 terminal
# ---------------------------------------------------------------------
def test_paper_mock_works_without_real_mt5(journal, kill_switch):
    from app.mt5.mock_client import MockMT5Client

    settings = _settings(TRADING_MODE="PAPER", MT5_USE_MOCK=True)
    client = MockMT5Client()
    result = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_ALLOWED"


# ---------------------------------------------------------------------
# 28-29. Startup check never submits an order
# ---------------------------------------------------------------------
def test_startup_check_never_touches_order_methods_on_failure(journal, kill_switch):
    # FakeClient's submit_order/close_position/etc. raise AssertionError
    # if called -- a failing run (market data stale) must still never
    # reach them.
    client = FakeClient(get_ohlcv_result=_ohlcv(fresh=False))
    result = run_startup_safety_check(
        _settings(), client=client, journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"  # got here without any AssertionError


def test_startup_check_module_never_imports_execution_engine():
    import app.safety.startup_check as mod
    src = open(mod.__file__, encoding="utf-8").read()
    assert "execution.engine" not in src
    assert "ExecutionEngine" not in src


# ---------------------------------------------------------------------
# 30. Deterministic re-run
# ---------------------------------------------------------------------
def test_rerun_is_deterministic(journal, kill_switch):
    settings = _settings()
    client = FakeClient()
    now = datetime.now(timezone.utc)
    r1 = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), now=now,
    )
    r2 = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), now=now,
    )
    assert r1.overall_status == r2.overall_status
    assert [c.name for c in r1.checks] == [c.name for c in r2.checks]
    assert [c.status for c in r1.checks] == [c.status for c in r2.checks]


# ---------------------------------------------------------------------
# Security / bypass tests
# ---------------------------------------------------------------------
def test_unexpected_internal_error_fails_closed(journal, kill_switch, monkeypatch):
    import app.safety.startup_check as mod

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_check_configuration", _boom)
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(),
    )
    assert result.overall_status == "TRADING_BLOCKED"
    assert result.trading_allowed is False


def test_research_mode_all_live_checks_not_required(journal, kill_switch):
    settings = _settings(TRADING_MODE="BACKTEST")
    result = run_startup_safety_check(settings, client=None, journal=journal, kill_switch=kill_switch)
    for name in ("mt5_connectivity", "account_information", "symbol_availability",
                 "symbol_trading_permissions", "market_data_freshness", "required_features", "news_state"):
        assert _by_name(result, name).status == CheckStatus.NOT_REQUIRED
    # RESEARCH mode's own checks (config, journal, kill switch, risk,
    # gates, clock, dependencies) still run and can still pass.
    assert result.overall_status == "TRADING_ALLOWED"


def test_format_report_text_mentions_failure_reason(journal, kill_switch):
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=UnavailableNewsFilter(),
    )
    text = format_startup_report_text(result)
    assert "TRADING BLOCKED" in text
    assert "News" in text


# ---------------------------------------------------------------------
# Phase 7: execution recovery check
# ---------------------------------------------------------------------
def test_execution_recovery_not_required_when_no_store_provided(journal, kill_switch):
    """Pre-Phase-7 callers that don't pass execution_state_store at all
    must see identical behavior to before -- this check simply doesn't
    run for them."""
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(),
    )
    check = _by_name(result, "execution_recovery")
    assert check.status == CheckStatus.NOT_REQUIRED
    assert result.overall_status == "TRADING_ALLOWED"


def test_execution_recovery_warns_but_does_not_block_on_unresolved_unknown(journal, kill_switch, tmp_path):
    store = SqliteExecutionStateStore(str(tmp_path / "execution_state.db"))
    store.put(ExecutionRecord(
        client_order_id="leftover-unknown-1", status=STATUS_UNKNOWN, ticket=None,
        symbol="XAUUSD", direction="BUY", volume=0.1,
    ))
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), execution_state_store=store,
    )
    check = _by_name(result, "execution_recovery")
    assert check.status == CheckStatus.WARNING
    assert "leftover-unknown-1" in check.message
    # A WARNING-severity check must not, by itself, block startup -- the
    # existing per-client_order_id block in ExecutionEngine is what
    # actually protects against a blind resubmission; this check only
    # surfaces it to an operator.
    assert result.overall_status == "TRADING_ALLOWED"


def test_execution_recovery_passes_clean_when_store_has_no_unresolved_records(journal, kill_switch, tmp_path):
    store = SqliteExecutionStateStore(str(tmp_path / "execution_state.db"))
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), execution_state_store=store,
    )
    check = _by_name(result, "execution_recovery")
    assert check.status == CheckStatus.PASS


def test_execution_recovery_fails_closed_and_blocks_startup_when_state_unreadable(
    journal, kill_switch, tmp_path, monkeypatch,
):
    store = SqliteExecutionStateStore(str(tmp_path / "execution_state.db"))

    def boom():
        raise RuntimeError("disk read error")

    monkeypatch.setattr(store, "unresolved", boom)
    result = run_startup_safety_check(
        _settings(), client=FakeClient(), journal=journal, kill_switch=kill_switch,
        news_filter=AlwaysClearNewsFilter(), execution_state_store=store,
    )
    check = _by_name(result, "execution_recovery")
    assert check.status == CheckStatus.FAIL
    assert result.overall_status == "TRADING_BLOCKED"
    assert result.trading_allowed is False
