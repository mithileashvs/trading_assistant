from datetime import datetime, timedelta, timezone

import pytest

from app.journal.journal import SignalLogEntry, TradeJournal, TradeLogEntry


@pytest.fixture()
def journal(tmp_path):
    return TradeJournal(str(tmp_path / "journal.db"))


def test_log_signal_returns_id(journal):
    sig_id = journal.log_signal(SignalLogEntry(
        timestamp=datetime.now(timezone.utc), symbol="XAUUSD", direction="BUY", strategy="TREND_PULLBACK",
        regime="TREND_BULLISH", score=8, entry=2650.0, stop_loss=2645.0, take_profit=2660.0,
        session="LONDON", approved=True, rejection_reasons=[],
    ))
    assert sig_id == 1


def test_open_and_close_trade_round_trip(journal):
    now = datetime.now(timezone.utc)
    trade_id = journal.open_trade(TradeLogEntry(
        symbol="XAUUSD", direction="BUY", strategy="TREND_PULLBACK", regime="TREND_BULLISH", score=8,
        open_time=now, entry_price=2650.5, lots=0.1, stop_loss=2645.0, take_profit=2660.0, session="LONDON",
    ))
    assert len(journal.open_trades()) == 1

    journal.close_trade(trade_id, close_time=now + timedelta(hours=1), exit_price=2658.0,
                         exit_reason="TAKE_PROFIT", pnl=75.0, r_multiple=1.5)
    assert len(journal.open_trades()) == 0
    all_trades = journal.all_trades()
    assert len(all_trades) == 1
    assert all_trades[0]["pnl"] == 75.0
    assert all_trades[0]["exit_reason"] == "TAKE_PROFIT"


def test_trades_since_filters_by_close_time(journal):
    now = datetime.now(timezone.utc)
    old_id = journal.open_trade(TradeLogEntry(
        symbol="XAUUSD", direction="BUY", strategy="X", regime="R", score=5,
        open_time=now - timedelta(days=5), entry_price=2650.0, lots=0.1,
    ))
    journal.close_trade(old_id, close_time=now - timedelta(days=4), exit_price=2655.0, exit_reason="TP", pnl=50.0)

    recent_id = journal.open_trade(TradeLogEntry(
        symbol="XAUUSD", direction="SELL", strategy="X", regime="R", score=5,
        open_time=now - timedelta(hours=2), entry_price=2650.0, lots=0.1,
    ))
    journal.close_trade(recent_id, close_time=now - timedelta(hours=1), exit_price=2640.0, exit_reason="TP", pnl=100.0)

    result = journal.trades_since(now - timedelta(days=1))
    assert len(result) == 1
    assert result[0].pnl == 100.0


def test_recent_trades_used_by_risk_guards_interface(journal):
    now = datetime.now(timezone.utc)
    for i in range(3):
        trade_id = journal.open_trade(TradeLogEntry(
            symbol="XAUUSD", direction="BUY", strategy="X", regime="R", score=5,
            open_time=now - timedelta(hours=3 - i), entry_price=2650.0, lots=0.1,
        ))
        journal.close_trade(trade_id, close_time=now - timedelta(hours=2 - i), exit_price=2650.0,
                             exit_reason="TP", pnl=float(i) * 10 - 10)
    recent = journal.recent_trades(2)
    assert len(recent) == 2
    assert recent[0].pnl < recent[1].pnl  # oldest -> newest ordering preserved


def test_open_trades_only_returns_unclosed(journal):
    now = datetime.now(timezone.utc)
    open_id = journal.open_trade(TradeLogEntry(
        symbol="XAUUSD", direction="BUY", strategy="X", regime="R", score=5,
        open_time=now, entry_price=2650.0, lots=0.1,
    ))
    closed_id = journal.open_trade(TradeLogEntry(
        symbol="XAUUSD", direction="SELL", strategy="X", regime="R", score=5,
        open_time=now, entry_price=2650.0, lots=0.1,
    ))
    journal.close_trade(closed_id, close_time=now, exit_price=2650.0, exit_reason="TP", pnl=0.0)

    open_rows = journal.open_trades()
    assert len(open_rows) == 1
    assert open_rows[0]["id"] == open_id


def test_persists_across_new_instances_same_file(tmp_path):
    path = str(tmp_path / "journal.db")
    j1 = TradeJournal(path)
    now = datetime.now(timezone.utc)
    trade_id = j1.open_trade(TradeLogEntry(
        symbol="XAUUSD", direction="BUY", strategy="X", regime="R", score=5,
        open_time=now, entry_price=2650.0, lots=0.1,
    ))
    j1.close_trade(trade_id, close_time=now, exit_price=2655.0, exit_reason="TP", pnl=50.0)

    j2 = TradeJournal(path)  # simulates a process restart
    assert len(j2.all_trades()) == 1
