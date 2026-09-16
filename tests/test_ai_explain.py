import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import strategy_test_helpers as h

from app.ai.explain import explain_trade
from app.regimes.detector import Regime, RegimeDecision
from app.risk.guards import GuardCheckInput
from app.risk.validator import TradeValidator
from app.config.settings import RiskSettings
from app.mt5.interface import AccountInfo, SymbolSpec
from app.news.filter import NewsFilter, NewsState, NewsStatus
from app.signals.models import Signal, SignalDirection, no_signal


class _AlwaysClear(NewsFilter):
    def check(self, at):
        return NewsStatus(state=NewsState.CLEAR, reason="No blocking event (test).")


def _spec():
    return SymbolSpec(
        name="XAUUSD", contract_size=100.0, volume_min=0.01, volume_max=50.0, volume_step=0.01,
        tick_size=0.01, tick_value=1.0, digits=2, stops_level_points=50, freeze_level_points=0,
        trade_allowed=True, spread_points=20.0,
    )


def _account():
    return AccountInfo(login=1, balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0,
                        currency="USD", leverage=100, trade_allowed=True)


def _guard_input():
    return GuardCheckInput(
        equity=10000.0, day_start_equity=10000.0, week_start_equity=10000.0,
        trades_today_count=0, open_positions_count=0, current_spread_points=20.0,
        mt5_connected=True, broker_trade_allowed=True, market_data_fresh=True, kill_switch_active=False,
    )


def test_no_signal_explanation_says_no_trade():
    ctx = h.trend_pullback_context(bullish=True)
    sig = no_signal("STRATEGY_SELECTOR", "nothing triggered")
    validator = TradeValidator(RiskSettings(_env_file=None))
    validation = validator.validate(sig, ctx, _spec(), _account(), _guard_input())
    explanation = explain_trade(sig, ctx.h4_regime, validation)
    assert explanation.decision == "NO TRADE"
    assert "N/A" in explanation.why_this_entry


def test_approved_trade_explanation_has_all_sections():
    ctx = h.trend_pullback_context(bullish=True)
    close = ctx.m15_features["close"]
    sig = Signal(
        direction=SignalDirection.BUY, strategy="TREND_PULLBACK", entry=close, stop_loss=close - 5.0,
        take_profit=close + 15.0, score=8, score_label="VALID", reasons=["H4 trend confirmed", "Pullback confirmed"],
        meta={"entry_confirmed": True, "quality_confirmed": True},
    )
    validator = TradeValidator(RiskSettings(_env_file=None), news_filter=_AlwaysClear())
    validation = validator.validate(sig, ctx, _spec(), _account(), _guard_input())
    explanation = explain_trade(sig, ctx.h4_regime, validation)

    assert explanation.decision == "APPROVED"
    for field_value in (explanation.why, explanation.why_now, explanation.why_this_strategy,
                        explanation.why_this_entry, explanation.why_this_stop, explanation.why_this_target,
                        explanation.how_much_risk, explanation.what_would_invalidate_it):
        assert field_value and field_value != "N/A"


def test_rejected_trade_explanation_includes_rejection_reason():
    ctx = h.trend_pullback_context(bullish=True)
    close = ctx.m15_features["close"]
    sig = Signal(
        direction=SignalDirection.BUY, strategy="TREND_PULLBACK", entry=close, stop_loss=close - 5.0,
        take_profit=close + 15.0, score=3, score_label="NO_TRADE",
        meta={"entry_confirmed": True, "quality_confirmed": True},
    )
    validator = TradeValidator(RiskSettings(_env_file=None), min_score_label="VALID")
    validation = validator.validate(sig, ctx, _spec(), _account(), _guard_input())
    explanation = explain_trade(sig, ctx.h4_regime, validation)
    assert explanation.decision == "REJECTED"
    assert "score label" in explanation.decision_detail.lower()


def test_to_text_contains_all_required_headers():
    ctx = h.trend_pullback_context(bullish=True)
    close = ctx.m15_features["close"]
    sig = Signal(
        direction=SignalDirection.BUY, strategy="TREND_PULLBACK", entry=close, stop_loss=close - 5.0,
        take_profit=close + 15.0, score=8, score_label="VALID",
        meta={"entry_confirmed": True, "quality_confirmed": True},
    )
    validator = TradeValidator(RiskSettings(_env_file=None))
    validation = validator.validate(sig, ctx, _spec(), _account(), _guard_input())
    text = explain_trade(sig, ctx.h4_regime, validation).to_text()
    for header in ("WHY?", "WHY NOW?", "WHY THIS STRATEGY?", "WHY THIS ENTRY?", "WHY THIS STOP?",
                   "WHY THIS TARGET?", "HOW MUCH RISK?", "WHAT WOULD INVALIDATE IT?"):
        assert header in text


def test_to_dict_is_json_serializable():
    import json
    ctx = h.trend_pullback_context(bullish=True)
    sig = no_signal("X", "nothing")
    validator = TradeValidator(RiskSettings(_env_file=None))
    validation = validator.validate(sig, ctx, _spec(), _account(), _guard_input())
    explanation = explain_trade(sig, ctx.h4_regime, validation)
    json.dumps(explanation.to_dict())  # must not raise
