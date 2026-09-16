import tempfile
from datetime import datetime, timezone

import pytest

from app.config.settings import Settings, TradingMode
from app.journal.journal import TradeJournal
from app.mt5.mock_client import MockMT5Client
from app.news.filter import NewsFilter, NewsState, NewsStatus
from app.risk.kill_switch import KillSwitch
from app.runtime.loop import TradingLoop
from app.signals.models import Signal, SignalDirection


class _AlwaysClear(NewsFilter):
    """TradingLoop's default news filter (UnavailableNewsFilter) now
    correctly BLOCKS all new trades (see the news-state-semantics
    safety fix) -- tests exercising the "signal gets approved and
    opens a position" path must inject an explicit clear news source,
    the same way a real deployment would need to configure a real
    calendar before it could ever approve a trade."""
    def check(self, at):
        return NewsStatus(state=NewsState.CLEAR, reason="No blocking event (test).")


def _journal():
    return TradeJournal(tempfile.mktemp(suffix=".db"))


def _loop(settings=None, kill_switch=None, clear_news=False):
    settings = settings or Settings(_env_file=None)
    client = MockMT5Client()
    client.connect()
    spec = client.get_symbol_spec("XAUUSD")
    journal = _journal()
    ks = kill_switch or KillSwitch(tempfile.mktemp(suffix=".json"))
    loop = TradingLoop(settings, client, spec, journal, kill_switch=ks)
    if clear_news:
        loop.news_filter = _AlwaysClear()  # property setter keeps validator/safety-gate in sync
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


def test_news_filter_property_keeps_validator_and_safety_gate_in_sync():
    """Regression test for a real bug caught while wiring SafetyGate:
    overriding the news filter via one attribute silently left
    SafetyGate reading a stale/different filter, since it and
    TradeValidator each held their own separate reference. The
    news_filter property must make a single assignment update both."""
    loop, client, spec, journal = _loop()
    new_filter = _AlwaysClear()
    loop.news_filter = new_filter
    assert loop.validator.news_filter is new_filter
    assert loop.news_filter is new_filter


def test_default_news_filter_blocks_all_new_trades():
    """Core safety-fix regression test at the full-loop level: with NO
    news calendar configured (the out-of-the-box default), the loop
    must NEVER approve a new trade, even when the signal itself is
    otherwise perfect -- this is the direct consequence of fixing
    news_available=false from silently meaning news_ok=true."""
    loop, client, spec, journal = _loop()  # clear_news=False (default)
    close = 2650.0
    fake_signal = Signal(
        direction=SignalDirection.BUY, strategy="TEST", entry=close, stop_loss=close - 5.0,
        take_profit=close + 15.0, confidence=0.8, score=8, score_label="VALID",
        meta={"entry_confirmed": True, "quality_confirmed": True},
    )
    loop.selector.best_signal = lambda context: fake_signal

    summary = loop.run_once()
    assert summary.signal_direction == "BUY"
    assert summary.signal_approved is False
    assert loop.execution_engine.get_open_positions() == []


def test_safety_gate_can_reject_even_when_risk_engine_approves():
    """The core "two independent validations" property (audit section
    18), proven at the full-loop level: a stop distance far below the
    broker's minimum stops_level is something TradeValidator/RiskGuardEngine
    does NOT check at all, but SafetyGate does -- so a signal the risk
    engine approves can still be correctly rejected by the independent
    gate, and no position opens."""
    loop, client, spec, journal = _loop(clear_news=True)
    assert spec.stops_level_points > 0

    close = 2650.0
    # Distance in points must be well below stops_level_points.
    tiny_distance = spec.tick_size * (spec.stops_level_points / 10)
    tight_signal = Signal(
        direction=SignalDirection.BUY, strategy="TEST", entry=close, stop_loss=close - tiny_distance,
        take_profit=close + 15.0, score=8, score_label="VALID",
        meta={"entry_confirmed": True, "quality_confirmed": True},
    )
    loop.selector.best_signal = lambda context: tight_signal

    summary = loop.run_once()
    assert summary.signal_approved is True  # risk engine has no stops_level check
    assert summary.safety_gate_approved is False  # safety gate independently catches it
    assert loop.execution_engine.get_open_positions() == []
    assert any("SafetyGate rejected" in e for e in summary.errors)


def test_approved_signal_opens_a_paper_position_and_journals_it():
    loop, client, spec, journal = _loop(clear_news=True)

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
    loop, client, spec, journal = _loop(clear_news=True)
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
    loop, client, spec, journal = _loop(clear_news=True)
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
    loop.news_filter = _AlwaysClear()

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
