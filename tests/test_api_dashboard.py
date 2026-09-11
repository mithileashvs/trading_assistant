import pytest
from fastapi.testclient import TestClient

import app.api.main as main_module
from app.api.state import build_app_state


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Fresh, isolated state per test: separate journal DB and kill
    # switch file so tests never see each other's kill-switch state.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/journal.db")
    main_module._state = None
    state = build_app_state()
    state.kill_switch = state.kill_switch.__class__(str(tmp_path / "kill_switch.json"))
    main_module._state = state
    with TestClient(main_module.app) as c:
        yield c
    main_module._state = None


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "XAU/USD" in r.text


def test_account_endpoint_shape(client):
    r = client.get("/api/account")
    assert r.status_code == 200
    data = r.json()
    for key in ("login", "balance", "equity", "margin_free", "currency", "trade_allowed", "daily_pnl"):
        assert key in data


def test_market_endpoint_shape(client):
    r = client.get("/api/market")
    assert r.status_code == 200
    data = r.json()
    for key in ("symbol", "bid", "ask", "spread", "regime", "regime_confidence", "adx", "rsi", "trend_direction"):
        assert key in data
    assert data["regime"] in {"TREND_BULLISH", "TREND_BEARISH", "RANGE", "HIGH_VOLATILITY", "UNCERTAIN"}


def test_signal_endpoint_shape(client):
    r = client.get("/api/signal")
    assert r.status_code == 200
    data = r.json()
    assert "signal" in data and "validation" in data and "explanation" in data
    assert data["signal"]["direction"] in {"BUY", "SELL", "NO_SIGNAL"}
    assert isinstance(data["validation"]["approved"], bool)
    assert "decision" in data["explanation"]


def test_positions_endpoint_empty_initially(client):
    r = client.get("/api/positions")
    assert r.status_code == 200
    assert r.json() == {"positions": []}


def test_risk_endpoint_shape(client):
    r = client.get("/api/risk")
    assert r.status_code == 200
    data = r.json()
    for key in ("daily_pnl", "weekly_pnl", "trades_today", "consecutive_losses", "kill_switch_active", "risk_per_trade_pct"):
        assert key in data
    assert data["kill_switch_active"] is False


def test_performance_endpoint_empty_journal(client):
    r = client.get("/api/performance")
    assert r.status_code == 200
    data = r.json()
    assert data["number_of_trades"] == 0
    assert "note" in data


def test_kill_switch_activate_and_deactivate_round_trip(client):
    r = client.post("/api/kill-switch/activate", params={"reason": "test halt", "activated_by": "tester"})
    assert r.status_code == 200
    assert r.json()["active"] is True

    risk = client.get("/api/risk").json()
    assert risk["kill_switch_active"] is True

    r2 = client.post("/api/kill-switch/deactivate", params={"deactivated_by": "tester"})
    assert r2.status_code == 200
    assert r2.json()["active"] is False

    risk2 = client.get("/api/risk").json()
    assert risk2["kill_switch_active"] is False


def test_dashboard_reads_from_same_journal_as_direct_access(client, tmp_path):
    from datetime import datetime, timezone
    from app.journal.journal import TradeLogEntry

    state = main_module.get_state()
    now = datetime.now(timezone.utc)
    trade_id = state.journal.open_trade(TradeLogEntry(
        symbol="XAUUSD", direction="BUY", strategy="TREND_PULLBACK", regime="TREND_BULLISH", score=8,
        open_time=now, entry_price=2650.0, lots=0.1,
    ))
    state.journal.close_trade(trade_id, close_time=now, exit_price=2660.0, exit_reason="TAKE_PROFIT", pnl=100.0)

    r = client.get("/api/performance")
    data = r.json()
    assert data["number_of_trades"] == 1
    assert data["net_profit"] == 100.0


def test_paper_mode_positions_reflect_journal_not_local_execution_engine(client):
    """The dashboard's own ExecutionEngine instance never opened
    anything -- if a SEPARATE trading-loop process had opened a paper
    position and journaled it, /api/positions must still show it by
    reading the journal (the cross-process source of truth for PAPER
    mode), not the dashboard's own empty in-memory paper-position dict."""
    from datetime import datetime, timezone
    from app.journal.journal import TradeLogEntry

    state = main_module.get_state()
    assert state.execution_engine.get_open_positions() == []  # confirms it's genuinely empty locally

    now = datetime.now(timezone.utc)
    state.journal.open_trade(TradeLogEntry(
        symbol="XAUUSD", direction="SELL", strategy="BREAKOUT", regime="RANGE", score=7,
        open_time=now, entry_price=2650.0, lots=0.2, stop_loss=2660.0, take_profit=2630.0,
    ))

    r = client.get("/api/positions")
    positions = r.json()["positions"]
    assert len(positions) == 1
    assert positions[0]["direction"] == "SELL"
    assert positions[0]["strategy"] == "BREAKOUT"
    assert positions[0]["volume"] == 0.2
