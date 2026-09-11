from datetime import datetime, timedelta, timezone

from app.risk.history import InMemoryTradeHistory, TradeRecord


def test_record_and_recent_trades_ordering():
    history = InMemoryTradeHistory()
    now = datetime.now(timezone.utc)
    history.record(TradeRecord(closed_at=now - timedelta(hours=2), symbol="XAUUSD", direction="BUY", pnl=10.0))
    history.record(TradeRecord(closed_at=now - timedelta(hours=1), symbol="XAUUSD", direction="SELL", pnl=-5.0))
    recent = history.recent_trades(10)
    assert [t.pnl for t in recent] == [10.0, -5.0]


def test_trades_since_filters_correctly():
    history = InMemoryTradeHistory()
    now = datetime.now(timezone.utc)
    history.record(TradeRecord(closed_at=now - timedelta(days=2), symbol="XAUUSD", direction="BUY", pnl=10.0))
    history.record(TradeRecord(closed_at=now - timedelta(hours=1), symbol="XAUUSD", direction="SELL", pnl=-5.0))
    since = now - timedelta(hours=12)
    result = history.trades_since(since)
    assert len(result) == 1
    assert result[0].pnl == -5.0


def test_recent_trades_respects_limit():
    history = InMemoryTradeHistory()
    now = datetime.now(timezone.utc)
    for i in range(5):
        history.record(TradeRecord(closed_at=now - timedelta(hours=5 - i), symbol="XAUUSD", direction="BUY", pnl=float(i)))
    recent = history.recent_trades(2)
    assert [t.pnl for t in recent] == [3.0, 4.0]
