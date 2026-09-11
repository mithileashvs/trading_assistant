from app.backtesting.costs import ExecutionCosts
from app.config.settings import TradingMode
from app.execution.engine import ExecutionEngine
from app.mt5.mock_client import MockMT5Client


def _engine(mode=TradingMode.PAPER):
    client = MockMT5Client()
    client.connect()
    spec = client.get_symbol_spec("XAUUSD")
    return ExecutionEngine(client, spec, mode), client, spec


def test_paper_mode_never_calls_real_submit_order(monkeypatch):
    engine, client, spec = _engine(TradingMode.PAPER)

    called = {"submit": False}
    original = client.submit_order
    def spy(*args, **kwargs):
        called["submit"] = True
        return original(*args, **kwargs)
    monkeypatch.setattr(client, "submit_order", spy)

    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "order-1")
    assert result.success
    assert called["submit"] is False  # paper mode must never hit the real order path


def test_live_mode_calls_real_submit_order(monkeypatch):
    engine, client, spec = _engine(TradingMode.LIVE)
    called = {"submit": False}
    original = client.submit_order
    def spy(*args, **kwargs):
        called["submit"] = True
        return original(*args, **kwargs)
    monkeypatch.setattr(client, "submit_order", spy)

    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "order-1")
    assert result.success
    assert called["submit"] is True


def test_paper_fill_uses_entry_cost_model(monkeypatch):
    from datetime import datetime, timezone
    from app.mt5.interface import Tick

    engine, client, spec = _engine(TradingMode.PAPER)
    engine.costs = ExecutionCosts(_env_file=None, spread_points=20, slippage_points=5)

    fixed_tick = Tick(symbol=spec.name, time=datetime.now(timezone.utc), bid=2650.0, ask=2650.2, last=2650.0, volume=1.0)
    monkeypatch.setattr(client, "get_tick", lambda symbol: fixed_tick)

    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "order-1")
    assert result.price > fixed_tick.ask  # spread + slippage applied on top of raw ask


def test_duplicate_client_order_id_rejected_in_both_modes():
    for mode in (TradingMode.PAPER, TradingMode.LIVE):
        engine, client, spec = _engine(mode)
        first, pos1 = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "dup-1")
        second, pos2 = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "dup-1")
        assert first.success
        assert not second.success
        assert second.comment == "DUPLICATE_ORDER_REJECTED"
        assert pos2 is None


def test_open_positions_reflects_paper_fills():
    engine, client, spec = _engine(TradingMode.PAPER)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "o1")
    open_positions = engine.get_open_positions()
    assert len(open_positions) == 1
    assert open_positions[0].ticket == pos.ticket


def test_close_position_removes_from_open_positions():
    engine, client, spec = _engine(TradingMode.PAPER)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "o1")
    close_result = engine.close_position(pos.ticket, reason="manual")
    assert close_result.success
    assert engine.get_open_positions() == []


def test_close_nonexistent_position_fails_safely():
    engine, client, spec = _engine(TradingMode.PAPER)
    result = engine.close_position(999999)
    assert not result.success
    assert result.comment == "POSITION_NOT_FOUND"


def test_partial_close_reduces_volume():
    engine, client, spec = _engine(TradingMode.PAPER)
    result, pos = engine.submit_market_order("BUY", 0.2, 2600.0, 2700.0, "o1")
    partial = engine.close_position_partial(pos.ticket, 0.1)
    assert partial.success
    remaining = engine.get_open_positions()[0]
    assert remaining.volume == 0.1


def test_partial_close_rejects_full_or_over_volume():
    engine, client, spec = _engine(TradingMode.PAPER)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "o1")
    over = engine.close_position_partial(pos.ticket, 0.1)  # equals full volume
    assert not over.success


def test_modify_position_updates_stop_and_target():
    engine, client, spec = _engine(TradingMode.PAPER)
    result, pos = engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "o1")
    modify_result = engine.modify_position(pos.ticket, stop_loss=2610.0, take_profit=2710.0)
    assert modify_result.success
    updated = engine.get_open_positions()[0]
    assert updated.stop_loss == 2610.0
    assert updated.take_profit == 2710.0


def test_reconcile_empty_in_paper_mode():
    engine, client, spec = _engine(TradingMode.PAPER)
    engine.submit_market_order("BUY", 0.1, 2600.0, 2700.0, "o1")
    assert engine.reconcile() == []  # nothing to reconcile without a real broker


def test_reconcile_detects_untracked_broker_position():
    engine, client, spec = _engine(TradingMode.LIVE)
    # Simulate a position that exists at the broker but was never
    # submitted through this engine instance (e.g. a restart).
    from app.mt5.interface import OrderRequest
    client.submit_order(OrderRequest(symbol=spec.name, direction="BUY", volume=0.1))
    notes = engine.reconcile()
    assert len(notes) == 1
    assert "not tracked internally" in notes[0]
