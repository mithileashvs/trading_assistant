import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

import strategy_test_helpers as h

from app.ai.llm_client import AnthropicLLMClient, LLMClient, NullLLMClient, build_llm_client
from app.ai.query import (
    net_pnl_by_strategy,
    open_positions_summary,
    trades_in_range,
    trades_this_week,
    trades_today,
    win_rate_for_strategy,
)
from app.ai.summarize import polish, summarize_backtest, summarize_market_conditions, summarize_trade_history
from app.backtesting.metrics import compute_metrics
from app.backtesting.results import BacktestResult
from app.journal.journal import TradeJournal, TradeLogEntry
from app.regimes.detector import detect_regime


# --- summarize ---------------------------------------------------------------

def test_summarize_market_conditions_includes_regime_and_key_numbers():
    ctx = h.trend_pullback_context(bullish=True)
    text = summarize_market_conditions(ctx.h4_features, ctx.h1_features, ctx.m15_features, ctx.h4_regime)
    assert ctx.h4_regime.regime.value in text
    assert "RSI" in text


def test_summarize_backtest_empty_result_says_no_trades():
    result = BacktestResult(symbol="XAUUSD", starting_balance=10000.0, ending_balance=10000.0)
    metrics = compute_metrics(result)
    text = summarize_backtest(metrics)
    assert "No trades" in text


def test_summarize_trade_history_empty():
    assert "No closed trades" in summarize_trade_history([])


def test_summarize_trade_history_computes_win_rate():
    trades = [
        {"close_time": "2026-01-01T00:00:00+00:00", "pnl": 50.0, "direction": "BUY", "symbol": "XAUUSD", "exit_reason": "TP"},
        {"close_time": "2026-01-02T00:00:00+00:00", "pnl": -20.0, "direction": "SELL", "symbol": "XAUUSD", "exit_reason": "SL"},
    ]
    text = summarize_trade_history(trades)
    assert "50%" in text or "2 closed trades" in text


def test_polish_returns_original_text_when_llm_unavailable():
    text = "Some deterministic summary."
    result = polish(text, NullLLMClient())
    assert result == text


def test_polish_returns_original_text_when_llm_raises():
    class _Broken(LLMClient):
        def is_available(self): return True
        def complete(self, s, u): raise RuntimeError("boom")
    text = "Some deterministic summary."
    assert polish(text, _Broken()) == text


def test_polish_uses_llm_output_when_available():
    class _Echo(LLMClient):
        def is_available(self): return True
        def complete(self, system_prompt, user_prompt): return "polished version"
    assert polish("original", _Echo()) == "polished version"


# --- query -----------------------------------------------------------------

def _journal_with_trades(tmp_path):
    """Uses a FIXED reference time (not real wall-clock 'now') so day/
    week boundary edge cases can't make this test flaky depending on
    when the suite happens to run -- same lesson as the Phase 6
    MockMT5Client wall-clock issue documented in the main README."""
    journal = TradeJournal(str(tmp_path / "j.db"))
    now = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)  # a Wednesday afternoon, far from any boundary
    for i, (strategy, pnl) in enumerate([("TREND_PULLBACK", 50.0), ("BREAKOUT", -20.0), ("TREND_PULLBACK", 30.0)]):
        trade_id = journal.open_trade(TradeLogEntry(
            symbol="XAUUSD", direction="BUY", strategy=strategy, regime="TREND_BULLISH", score=8,
            open_time=now - timedelta(hours=3 - i), entry_price=2650.0, lots=0.1,
        ))
        journal.close_trade(trade_id, close_time=now - timedelta(hours=2 - i), exit_price=2655.0,
                             exit_reason="TP" if pnl > 0 else "SL", pnl=pnl)
    return journal, now


def test_trades_in_range_filters_correctly(tmp_path):
    journal, now = _journal_with_trades(tmp_path)
    result = trades_in_range(journal, now - timedelta(days=1), now)
    assert len(result) == 3


def test_trades_today_and_this_week(tmp_path):
    journal, now = _journal_with_trades(tmp_path)
    assert len(trades_today(journal, now=now)) == 3
    assert len(trades_this_week(journal, now=now)) == 3


def test_win_rate_for_strategy(tmp_path):
    journal, now = _journal_with_trades(tmp_path)
    assert win_rate_for_strategy(journal, "TREND_PULLBACK") == 1.0
    assert win_rate_for_strategy(journal, "BREAKOUT") == 0.0
    assert win_rate_for_strategy(journal, "NONEXISTENT") is None


def test_net_pnl_by_strategy(tmp_path):
    journal, now = _journal_with_trades(tmp_path)
    totals = net_pnl_by_strategy(journal)
    assert totals["TREND_PULLBACK"] == 80.0
    assert totals["BREAKOUT"] == -20.0


def test_open_positions_summary_excludes_closed(tmp_path):
    journal, now = _journal_with_trades(tmp_path)
    journal.open_trade(TradeLogEntry(
        symbol="XAUUSD", direction="SELL", strategy="MEAN_REVERSION", regime="RANGE", score=6,
        open_time=now, entry_price=2650.0, lots=0.1,
    ))
    open_rows = open_positions_summary(journal)
    assert len(open_rows) == 1
    assert open_rows[0]["strategy"] == "MEAN_REVERSION"


# --- LLM client ----------------------------------------------------------------

def test_null_client_reports_unavailable():
    client = NullLLMClient()
    assert client.is_available() is False


def test_null_client_raises_on_complete():
    import pytest
    client = NullLLMClient()
    with pytest.raises(RuntimeError):
        client.complete("system", "user")


def test_anthropic_client_unavailable_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = AnthropicLLMClient(api_key=None)
    assert client.is_available() is False


def test_build_llm_client_falls_back_to_null_without_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = build_llm_client()
    assert isinstance(client, NullLLMClient)
