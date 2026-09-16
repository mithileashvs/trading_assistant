"""
Tests for app.mt5.real_client.RealMT5Client and app.mt5.factory, using
a fake `MetaTrader5` module so these run without a real MT5 terminal
(the same way the real code would degrade if imported on a host
without the terminal, except here we control the fake's behavior to
exercise every code path).

These tests never touch a real broker -- `fake_mt5` is a pure-Python
stand-in swapped in via monkeypatch, so even the submit_order /
close_position tests exercise only request-building/response-mapping
logic against fabricated in-memory responses.
"""
from __future__ import annotations

import types

import pandas as pd
import pytest

import app.mt5.real_client as real_client_module
from app.config.settings import Settings
from app.mt5.factory import build_mt5_client
from app.mt5.interface import OrderRequest
from app.mt5.real_client import MT5ConnectionError, RealMT5Client


class _Obj:
    """Simple attribute bag, like the namedtuple-ish objects the real
    MetaTrader5 package returns."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def _asdict(self):
        return dict(self.__dict__)


def make_fake_mt5(**overrides):
    """Build a fake MetaTrader5 module. `overrides` lets each test
    swap in custom behavior for specific functions."""
    fake = types.SimpleNamespace()

    # -- constants, matching the real package's values closely enough
    # for our own code's comparisons (exact values don't matter, only
    # that they're distinct and self-consistent) --
    fake.SYMBOL_TRADE_MODE_DISABLED = 0
    fake.ORDER_TYPE_BUY = 0
    fake.ORDER_TYPE_SELL = 1
    fake.TRADE_ACTION_DEAL = 1
    fake.TRADE_ACTION_SLTP = 2
    fake.ORDER_TIME_GTC = 0
    fake.ORDER_FILLING_IOC = 1
    fake.TRADE_RETCODE_DONE = 10009
    fake.ACCOUNT_TRADE_MODE_DEMO = 0
    fake.ACCOUNT_TRADE_MODE_REAL = 1
    fake.TIMEFRAME_M15 = 15
    fake.TIMEFRAME_H1 = 60
    fake.TIMEFRAME_H4 = 240

    fake.initialize = overrides.get("initialize", lambda **kw: True)
    fake.login = overrides.get("login", lambda **kw: True)
    fake.shutdown = overrides.get("shutdown", lambda: None)
    fake.last_error = overrides.get("last_error", lambda: (0, "no error"))
    fake.symbol_info = overrides.get("symbol_info", lambda s: None)
    fake.symbol_select = overrides.get("symbol_select", lambda s, v: True)
    fake.account_info = overrides.get("account_info", lambda: None)
    fake.symbol_info_tick = overrides.get("symbol_info_tick", lambda s: None)
    fake.copy_rates_from_pos = overrides.get("copy_rates_from_pos", lambda *a, **kw: None)
    fake.positions_get = overrides.get("positions_get", lambda **kw: [])
    fake.order_send = overrides.get("order_send", lambda req: None)
    return fake


@pytest.fixture
def patch_mt5_available(monkeypatch):
    """Make RealMT5Client think the MetaTrader5 package imported fine,
    and return a function to install a fake `mt5` module."""
    monkeypatch.setattr(real_client_module, "_MT5_AVAILABLE", True)

    def _install(fake):
        monkeypatch.setattr(real_client_module, "mt5", fake)
        return fake

    return _install


# ---------------------------------------------------------------------
# Package-not-available (fails closed rather than silently degrading)
# ---------------------------------------------------------------------

def test_construction_fails_closed_when_package_unavailable(monkeypatch):
    monkeypatch.setattr(real_client_module, "_MT5_AVAILABLE", False)
    with pytest.raises(MT5ConnectionError):
        RealMT5Client(login=1, password="x", server="y")


# ---------------------------------------------------------------------
# Factory wiring (settings.mt5_use_mock is the single switch point)
# ---------------------------------------------------------------------

def test_factory_selects_mock_client_when_mock_true():
    from app.mt5.mock_client import MockMT5Client

    settings = Settings(TRADING_MODE="PAPER", MT5_USE_MOCK=True)
    client = build_mt5_client(settings)
    assert isinstance(client, MockMT5Client)


def test_factory_selects_real_client_when_mock_false(patch_mt5_available):
    patch_mt5_available(make_fake_mt5())
    settings = Settings(TRADING_MODE="PAPER", MT5_USE_MOCK=False, MT5_LOGIN=123, MT5_PASSWORD="pw", MT5_SERVER="srv")
    client = build_mt5_client(settings)
    assert isinstance(client, RealMT5Client)


def test_factory_real_client_fails_closed_without_package(monkeypatch):
    # Force the "MetaTrader5 package unavailable" condition explicitly
    # rather than relying on ambient environment state. Relying on
    # ambient state (no patch, hoping the package just isn't installed)
    # is not reproducible: a developer running the suite from a venv
    # that actually has the `MetaTrader5` pip package installed (e.g.
    # a Windows dev box being prepped for MT5 Demo work, where the
    # package imports fine even with no terminal running) would see
    # _MT5_AVAILABLE come back True and this test would falsely pass
    # the construction step, defeating the point of the test. Setting
    # _MT5_AVAILABLE directly exercises the exact same fail-closed
    # branch in RealMT5Client.__init__, deterministically, on every
    # machine.
    monkeypatch.setattr(real_client_module, "_MT5_AVAILABLE", False)
    settings = Settings(TRADING_MODE="PAPER", MT5_USE_MOCK=False)
    with pytest.raises(MT5ConnectionError):
        build_mt5_client(settings)


# ---------------------------------------------------------------------
# connect() / disconnect() / is_connected()
# ---------------------------------------------------------------------

def test_connect_success(patch_mt5_available):
    patch_mt5_available(make_fake_mt5())
    client = RealMT5Client(login=1, password="x", server="y")
    assert client.is_connected() is False
    assert client.connect() is True
    assert client.is_connected() is True


def test_connect_without_credentials_skips_login(patch_mt5_available):
    calls = {"login": 0}

    def login(**kw):
        calls["login"] += 1
        return True

    patch_mt5_available(make_fake_mt5(login=login))
    client = RealMT5Client(login=None, password=None, server=None)
    client.connect()
    assert calls["login"] == 0  # no credentials -> initialize() only, e.g. terminal already logged in manually


def test_connect_fails_closed_on_initialize_failure(patch_mt5_available):
    patch_mt5_available(make_fake_mt5(initialize=lambda **kw: False, last_error=lambda: (1, "boom")))
    client = RealMT5Client(login=1, password="x", server="y")
    with pytest.raises(MT5ConnectionError):
        client.connect()
    assert client.is_connected() is False


def test_connect_fails_closed_on_login_failure(patch_mt5_available):
    patch_mt5_available(make_fake_mt5(login=lambda **kw: False, last_error=lambda: (2, "bad creds")))
    client = RealMT5Client(login=1, password="wrong", server="y")
    with pytest.raises(MT5ConnectionError):
        client.connect()
    assert client.is_connected() is False


def test_disconnect_resets_connected_state(patch_mt5_available):
    patch_mt5_available(make_fake_mt5())
    client = RealMT5Client(login=1, password="x", server="y")
    client.connect()
    client.disconnect()
    assert client.is_connected() is False


# ---------------------------------------------------------------------
# Account info + demo-account detection
# ---------------------------------------------------------------------

def test_account_info_demo_detected(patch_mt5_available):
    fake = make_fake_mt5(account_info=lambda: _Obj(
        login=555, balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0,
        currency="USD", leverage=100, trade_allowed=True, trade_mode=0,  # matches ACCOUNT_TRADE_MODE_DEMO
    ))
    patch_mt5_available(fake)
    client = RealMT5Client(login=1, password="x", server="y")
    account = client.get_account_info()
    assert account.is_demo is True
    assert account.login == 555


def test_account_info_real_money_detected(patch_mt5_available):
    fake = make_fake_mt5(account_info=lambda: _Obj(
        login=555, balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0,
        currency="USD", leverage=100, trade_allowed=True, trade_mode=1,  # ACCOUNT_TRADE_MODE_REAL
    ))
    patch_mt5_available(fake)
    client = RealMT5Client(login=1, password="x", server="y")
    account = client.get_account_info()
    assert account.is_demo is False


def test_account_info_raises_when_unavailable(patch_mt5_available):
    patch_mt5_available(make_fake_mt5(account_info=lambda: None, last_error=lambda: (3, "no account")))
    client = RealMT5Client(login=1, password="x", server="y")
    with pytest.raises(MT5ConnectionError):
        client.get_account_info()


# ---------------------------------------------------------------------
# Symbol discovery + spec
# ---------------------------------------------------------------------

def test_discover_symbol_skips_unknown_and_disabled(patch_mt5_available):
    def symbol_info(name):
        if name == "UNKNOWN":
            return None
        if name == "DISABLED":
            return _Obj(visible=True, trade_mode=0)  # == SYMBOL_TRADE_MODE_DISABLED
        if name == "XAUUSD":
            return _Obj(visible=True, trade_mode=1)
        return None

    patch_mt5_available(make_fake_mt5(symbol_info=symbol_info))
    client = RealMT5Client(login=1, password="x", server="y")
    assert client.discover_symbol(["UNKNOWN", "DISABLED", "XAUUSD"]) == "XAUUSD"


def test_discover_symbol_selects_invisible_symbol(patch_mt5_available):
    calls = {"select": []}
    state = {"visible": False}

    def symbol_info(name):
        return _Obj(visible=state["visible"], trade_mode=1)

    def symbol_select(name, visible):
        calls["select"].append((name, visible))
        state["visible"] = True
        return True

    patch_mt5_available(make_fake_mt5(symbol_info=symbol_info, symbol_select=symbol_select))
    client = RealMT5Client(login=1, password="x", server="y")
    result = client.discover_symbol(["XAUUSD"])
    assert result == "XAUUSD"
    assert calls["select"] == [("XAUUSD", True)]


def test_discover_symbol_returns_none_when_nothing_tradable(patch_mt5_available):
    patch_mt5_available(make_fake_mt5(symbol_info=lambda s: None))
    client = RealMT5Client(login=1, password="x", server="y")
    assert client.discover_symbol(["A", "B"]) is None


def test_get_symbol_spec_maps_all_fields(patch_mt5_available):
    fake = make_fake_mt5(symbol_info=lambda s: _Obj(
        trade_contract_size=100.0, volume_min=0.01, volume_max=50.0, volume_step=0.01,
        trade_tick_size=0.01, trade_tick_value=1.0, digits=2,
        trade_stops_level=50, trade_freeze_level=10, trade_mode=1, spread=20,
    ))
    patch_mt5_available(fake)
    client = RealMT5Client(login=1, password="x", server="y")
    spec = client.get_symbol_spec("XAUUSD")
    assert spec.name == "XAUUSD"
    assert spec.volume_min == 0.01
    assert spec.trade_allowed is True
    assert spec.stops_level_points == 50


def test_get_symbol_spec_raises_when_not_found(patch_mt5_available):
    patch_mt5_available(make_fake_mt5(symbol_info=lambda s: None))
    client = RealMT5Client(login=1, password="x", server="y")
    with pytest.raises(ValueError):
        client.get_symbol_spec("NOTREAL")


# ---------------------------------------------------------------------
# Tick + OHLCV
# ---------------------------------------------------------------------

def test_get_tick_maps_fields(patch_mt5_available):
    import time as _time
    now_epoch = int(_time.time())
    fake = make_fake_mt5(symbol_info_tick=lambda s: _Obj(
        time=now_epoch, bid=2650.10, ask=2650.35, last=2650.20, volume=5,
    ))
    patch_mt5_available(fake)
    client = RealMT5Client(login=1, password="x", server="y")
    tick = client.get_tick("XAUUSD")
    assert tick.bid == 2650.10
    assert tick.ask == 2650.35
    assert round(tick.spread, 2) == 0.25


def test_get_tick_raises_when_unavailable(patch_mt5_available):
    patch_mt5_available(make_fake_mt5(symbol_info_tick=lambda s: None, last_error=lambda: (4, "no tick")))
    client = RealMT5Client(login=1, password="x", server="y")
    with pytest.raises(MT5ConnectionError):
        client.get_tick("XAUUSD")


def test_get_ohlcv_shape_and_columns(patch_mt5_available):
    import numpy as np
    n = 20
    rates = np.zeros(n, dtype=[
        ("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
        ("close", "f8"), ("tick_volume", "i8"), ("spread", "i8"), ("real_volume", "i8"),
    ])
    base = 1_700_000_000
    for i in range(n):
        rates[i] = (base + i * 900, 2650.0, 2651.0, 2649.0, 2650.5, 100, 20, 0)

    patch_mt5_available(make_fake_mt5(copy_rates_from_pos=lambda *a, **kw: rates))
    client = RealMT5Client(login=1, password="x", server="y")
    df = client.get_ohlcv("XAUUSD", "M15", count=n)
    assert list(df.columns) == ["open", "high", "low", "close", "tick_volume", "spread"]
    assert len(df) == n
    assert isinstance(df.index, pd.DatetimeIndex)


def test_get_ohlcv_raises_on_empty(patch_mt5_available):
    patch_mt5_available(make_fake_mt5(copy_rates_from_pos=lambda *a, **kw: []))
    client = RealMT5Client(login=1, password="x", server="y")
    with pytest.raises(MT5ConnectionError):
        client.get_ohlcv("XAUUSD", "M15", count=10)


def test_get_ohlcv_rejects_unsupported_timeframe(patch_mt5_available):
    patch_mt5_available(make_fake_mt5())
    client = RealMT5Client(login=1, password="x", server="y")
    with pytest.raises(ValueError):
        client.get_ohlcv("XAUUSD", "W1", count=10)


# ---------------------------------------------------------------------
# Order-path unit tests (fake module only -- see module docstring).
# Included for regression coverage of pre-existing code; NOT exercised
# by the self-test or by any Demo-connectivity-prep script.
# ---------------------------------------------------------------------

def test_submit_order_maps_success_result(patch_mt5_available):
    fake = make_fake_mt5(
        symbol_info_tick=lambda s: _Obj(time=1_700_000_000, bid=2650.0, ask=2650.3, last=2650.1, volume=1),
        order_send=lambda req: _Obj(retcode=10009, order=42, deal=99, price=2650.3, volume=0.1, comment="OK"),
    )
    patch_mt5_available(fake)
    client = RealMT5Client(login=1, password="x", server="y")
    result = client.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert result.success is True
    assert result.order_id == 42


def test_submit_order_maps_failure_when_send_returns_none(patch_mt5_available):
    fake = make_fake_mt5(
        symbol_info_tick=lambda s: _Obj(time=1_700_000_000, bid=2650.0, ask=2650.3, last=2650.1, volume=1),
        order_send=lambda req: None,
        last_error=lambda: (5, "rejected"),
    )
    patch_mt5_available(fake)
    client = RealMT5Client(login=1, password="x", server="y")
    result = client.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert result.success is False
