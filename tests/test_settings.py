import pytest

from app.config.settings import Settings, TradingMode


def test_default_mode_is_paper():
    s = Settings(_env_file=None)
    assert s.trading_mode == TradingMode.PAPER


def test_default_kill_switch_off():
    s = Settings(_env_file=None)
    assert s.kill_switch is False


def test_live_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("MT5_USE_MOCK", "false")
    monkeypatch.setenv("MT5_LOGIN", "12345")
    monkeypatch.setenv("MT5_PASSWORD", "secret")
    monkeypatch.setenv("MT5_SERVER", "Broker-Live")
    s = Settings(_env_file=None)
    assert s.trading_mode == TradingMode.LIVE
    s.validate_live_safety()  # should not raise


def test_live_refused_with_mock_client(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("MT5_USE_MOCK", "true")
    s = Settings(_env_file=None)
    with pytest.raises(RuntimeError):
        s.validate_live_safety()


def test_live_refused_without_credentials(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("MT5_USE_MOCK", "false")
    s = Settings(_env_file=None)
    with pytest.raises(RuntimeError):
        s.validate_live_safety()


def test_live_refused_when_kill_switch_active(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("MT5_USE_MOCK", "false")
    monkeypatch.setenv("MT5_LOGIN", "12345")
    monkeypatch.setenv("MT5_PASSWORD", "secret")
    monkeypatch.setenv("MT5_SERVER", "Broker-Live")
    monkeypatch.setenv("KILL_SWITCH", "true")
    s = Settings(_env_file=None)
    with pytest.raises(RuntimeError):
        s.validate_live_safety()


def test_symbol_candidate_list_parsing():
    s = Settings(_env_file=None, SYMBOL_CANDIDATES="XAUUSD, XAUUSDm ,GOLD")
    assert s.symbol_candidate_list() == ["XAUUSD", "XAUUSDm", "GOLD"]


def test_risk_defaults_are_sane():
    s = Settings(_env_file=None)
    assert 0 < s.risk.risk_per_trade_pct <= 5.0
    assert s.risk.max_open_positions >= 1
