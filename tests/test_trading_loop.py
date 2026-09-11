import tempfile
from datetime import datetime, timezone

import pytest

from app.config.settings import Settings, TradingMode
from app.journal.journal import TradeJournal
from app.mt5.mock_client import MockMT5Client
from app.risk.kill_switch import KillSwitch
from app.runtime.loop import TradingLoop
from app.signals.models import Signal, SignalDirection


def _journal():
    return TradeJournal(tempfile.mktemp(suffix=".db"))


def _loop(settings=None, kill_switch=None):
    settings = settings or Settings(_env_file=None)
    client = MockMT5Client()
    client.connect()
    spec = client.get_symbol_spec("XAUUSD")
    journal = _journal()
    ks = kill_switch or KillSwitch(tempfile.mktemp(suffix=".json"))
    loop = TradingLoop(settings, client, spec, journal, kill_switch=ks)
    return loop, client, spec, journal


def test_run_once_never_crashes_on_no_signal():
    loop, client, spec, journal = _loop()
    summary = loop.run_once()
    assert summary.errors == [] or all("stale" not in e.lower() for e in summary.errors)


def test_kill_switch_config_flag_skips_cycle():
    settings = Settings(_env_file=None, KILL_SWITCH=True)
    loop, client, spec, journal = _loop(settings=settings)
    summary = loop.run_once()
    assert "Kill switch is active" in summary.errors[0]
    assert summary.signal_direction is None  # never even evaluated


def test_persisted_kill_switch_also_skips_cycle():
    ks_path = tempfile.mktemp(suffix=".json")
    ks = KillSwitch(ks_path)
    ks.activate("test halt", activated_by="tester")
    loop, client, spec, journal = _loop(kill_switch=ks)
    summary = loop.run_once()
    assert "Kill switch is active" in summary.errors[0]


def test_approved_signal_opens_a_paper_position_and_journals_it():
    loop, client, spec, journal = _loop()

    close = 2650.0
    fake_signal = Signal(
        direction=SignalDirection.BUY, strategy="TEST", entry=close, stop_loss=close - 5.0,
        take_profit=close + 15.0, confidence=0.8, score=8, score_label="VALID",
        meta={"entry_confirmed": True, "quality_confirmed": True},
    )
    loop.selector.best_signal = lambda context: fake_signal  # force an approvable signal

    summary = loop.run_once()
    assert summary.signal_direction == "BUY"
    assert summary.signal_approved is True
    assert any("Opened BUY" in a for a in summary.actions_taken)

    open_positions = loop.execution_engine.get_open_positions()
    assert len(open_positions) == 1
    assert len(journal.open_trades()) == 1


def test_open_position_is_not_duplicated_on_next_cycle():
    loop, client, spec, journal = _loop()
    close = 2650.0
    fake_signal = Signal(
        direction=SignalDirection.BUY, strategy="TEST", entry=close, stop_loss=close - 5.0,
        take_profit=close + 15.0, score=8, score_label="VALID",
        meta={"entry_confirmed": True, "quality_confirmed": True},
    )
    loop.selector.best_signal = lambda context: fake_signal

    loop.run_once()
    loop.run_once()  # already have a position -- must not evaluate for a new entry
    assert len(loop.execution_engine.get_open_positions()) == 1


def test_position_closes_on_stop_loss_hit_and_journal_updated(monkeypatch):
    loop, client, spec, journal = _loop()
    close = 2650.0
    fake_signal = Signal(
        direction=SignalDirection.BUY, strategy="TEST", entry=close, stop_loss=close - 5.0,
        take_profit=close + 50.0, score=8, score_label="VALID",
        meta={"entry_confirmed": True, "quality_confirmed": True},
    )
    loop.selector.best_signal = lambda context: fake_signal
    loop.run_once()
    assert len(loop.execution_engine.get_open_positions()) == 1

    # Force the next tick to be far below the stop loss.
    from app.mt5.interface import Tick
    losing_tick = Tick(symbol=spec.name, time=datetime.now(timezone.utc), bid=2600.0, ask=2600.2, last=2600.0, volume=1.0)
    monkeypatch.setattr(client, "get_tick", lambda symbol=None: losing_tick)

    summary = loop.run_once()
    assert any("STOP_LOSS" in a for a in summary.actions_taken)
    # The losing position closed and was journaled correctly. A NEW
    # position may legitimately open in the same cycle since the
    # fake signal is unconditionally approvable and the loop is flat
    # again after the close -- that's correct re-entry behavior, not
    # a bug, so we don't assert "no open positions" here.
    closed_trades = [dict(r) for r in journal.all_trades() if r["close_time"] is not None]
    assert len(closed_trades) == 1
    assert closed_trades[0]["exit_reason"] == "STOP_LOSS"
    assert closed_trades[0]["pnl"] < 0


def test_no_signal_never_opens_a_position():
    loop, client, spec, journal = _loop()
    no_sig = Signal(direction=SignalDirection.NO_SIGNAL, strategy="TEST", reasons=["nothing"])
    loop.selector.best_signal = lambda context: no_sig
    summary = loop.run_once()
    assert summary.signal_direction == "NO_SIGNAL"
    assert loop.execution_engine.get_open_positions() == []


def test_unsafe_live_config_refused_at_construction():
    settings = Settings(_env_file=None, TRADING_MODE=TradingMode.LIVE, MT5_USE_MOCK=True)
    client = MockMT5Client()
    client.connect()
    spec = client.get_symbol_spec("XAUUSD")
    with pytest.raises(RuntimeError):
        TradingLoop(settings, client, spec, _journal())


def test_account_safety_note_for_paper_mode():
    loop, client, spec, journal = _loop()
    assert "PAPER" in loop.account_safety_note


def test_account_safety_note_for_live_mode_with_demo_account():
    settings = Settings(_env_file=None, TRADING_MODE=TradingMode.LIVE, MT5_USE_MOCK=False,
                         MT5_LOGIN=1, MT5_PASSWORD="x", MT5_SERVER="y")
    client = MockMT5Client()  # MockMT5Client.get_account_info() always reports is_demo=True
    client.connect()
    spec = client.get_symbol_spec("XAUUSD")
    loop = TradingLoop(settings, client, spec, _journal())
    assert "DEMO" in loop.account_safety_note


def test_live_mode_order_routes_through_full_loop_to_real_submit(monkeypatch):
    settings = Settings(_env_file=None, TRADING_MODE=TradingMode.LIVE, MT5_USE_MOCK=False,
                         MT5_LOGIN=1, MT5_PASSWORD="x", MT5_SERVER="y")
    client = MockMT5Client()
    client.connect()
    spec = client.get_symbol_spec("XAUUSD")
    loop = TradingLoop(settings, client, spec, _journal())

    called = {"submit": False}
    original = client.submit_order
    def spy(*args, **kwargs):
        called["submit"] = True
        return original(*args, **kwargs)
    monkeypatch.setattr(client, "submit_order", spy)

    close = 2650.0
    fake_signal = Signal(
        direction=SignalDirection.BUY, strategy="TEST", entry=close, stop_loss=close - 5.0,
        take_profit=close + 15.0, score=8, score_label="VALID",
        meta={"entry_confirmed": True, "quality_confirmed": True},
    )
    loop.selector.best_signal = lambda context: fake_signal

    loop.run_once()
    assert called["submit"] is True


def test_run_multiple_iterations_without_sleep_between_last_call():
    import time as time_module
    loop, client, spec, journal = _loop()
    called = {"sleep": 0}
    original_sleep = time_module.sleep
    time_module.sleep = lambda s: called.__setitem__("sleep", called["sleep"] + 1)
    try:
        summaries = loop.run(iterations=3, sleep_seconds=0.01)
    finally:
        time_module.sleep = original_sleep
    assert len(summaries) == 3
    assert called["sleep"] == 2  # sleeps between cycles, not after the last one
