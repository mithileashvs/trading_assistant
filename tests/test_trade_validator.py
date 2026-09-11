import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import strategy_test_helpers as h
from app.config.settings import RiskSettings
from app.mt5.interface import AccountInfo, SymbolSpec
from app.news.filter import NewsFilter, NewsStatus
from app.risk.guards import GuardCheckInput
from app.risk.validator import TradeValidator
from app.signals.models import Signal, SignalDirection


def _risk_settings(**overrides) -> RiskSettings:
    return RiskSettings(_env_file=None, **overrides)


def _symbol_spec(**overrides) -> SymbolSpec:
    base = dict(
        name="XAUUSD", contract_size=100.0, volume_min=0.01, volume_max=50.0,
        volume_step=0.01, tick_size=0.01, tick_value=1.0, digits=2,
        stops_level_points=50, freeze_level_points=0, trade_allowed=True, spread_points=20.0,
    )
    base.update(overrides)
    return SymbolSpec(**base)


def _account(**overrides) -> AccountInfo:
    base = dict(login=1, balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0,
                currency="USD", leverage=100, trade_allowed=True)
    base.update(overrides)
    return AccountInfo(**base)


def _guard_input(**overrides) -> GuardCheckInput:
    base = dict(
        equity=10000.0, day_start_equity=10000.0, week_start_equity=10000.0,
        trades_today_count=0, open_positions_count=0, current_spread_points=20.0,
        mt5_connected=True, broker_trade_allowed=True, market_data_fresh=True,
        kill_switch_active=False, recent_trades=[],
    )
    base.update(overrides)
    return GuardCheckInput(**base)


def _approvable_signal(ctx) -> Signal:
    close = ctx.m15_features["close"]
    return Signal(
        direction=SignalDirection.BUY, strategy="TEST", entry=close, stop_loss=close - 5.0,
        take_profit=close + 15.0, confidence=0.8, score=8, score_label="VALID",
        meta={"entry_confirmed": True, "quality_confirmed": True},
    )


class _AlwaysBlackout(NewsFilter):
    def check(self, at):
        return NewsStatus(available=True, blackout_active=True, reason="High-impact event window (test).")


def test_no_signal_is_never_approved():
    ctx = h.trend_pullback_context(bullish=True)
    from app.signals.models import no_signal
    sig = no_signal("X", "nothing to trade")
    validator = TradeValidator(_risk_settings())
    v = validator.validate(sig, ctx, _symbol_spec(), _account(), _guard_input())
    assert v.approved is False
    assert v.direction == "NO_SIGNAL"


def test_healthy_signal_is_approved():
    ctx = h.trend_pullback_context(bullish=True)
    sig = _approvable_signal(ctx)
    validator = TradeValidator(_risk_settings())
    v = validator.validate(sig, ctx, _symbol_spec(), _account(), _guard_input())
    assert v.approved is True
    assert v.lots > 0
    assert v.rejection_reasons == []


def test_matches_documented_json_shape():
    ctx = h.trend_pullback_context(bullish=True)
    sig = _approvable_signal(ctx)
    validator = TradeValidator(_risk_settings())
    v = validator.validate(sig, ctx, _symbol_spec(), _account(), _guard_input())
    d = v.to_dict()
    required_keys = {
        "symbol", "direction", "strategy", "regime", "score", "entry",
        "stop_loss", "take_profit", "risk_reward", "risk_percent",
        "spread_ok", "news_ok", "daily_loss_limit_ok", "position_limit_ok",
        "market_data_fresh", "approved",
    }
    assert required_keys.issubset(d.keys())


def test_guard_failure_blocks_approval_even_with_great_signal():
    ctx = h.trend_pullback_context(bullish=True)
    sig = _approvable_signal(ctx)
    validator = TradeValidator(_risk_settings())
    v = validator.validate(sig, ctx, _symbol_spec(), _account(), _guard_input(kill_switch_active=True))
    assert v.approved is False
    assert any("kill switch" in r.lower() for r in v.rejection_reasons)


def test_low_score_signal_rejected():
    ctx = h.trend_pullback_context(bullish=True)
    sig = _approvable_signal(ctx)
    sig.score_label = "WEAK"
    validator = TradeValidator(_risk_settings(), min_score_label="VALID")
    v = validator.validate(sig, ctx, _symbol_spec(), _account(), _guard_input())
    assert v.approved is False
    assert any("score label" in r.lower() for r in v.rejection_reasons)


def test_poor_risk_reward_rejected():
    ctx = h.trend_pullback_context(bullish=True)
    close = ctx.m15_features["close"]
    sig = Signal(
        direction=SignalDirection.BUY, strategy="TEST", entry=close, stop_loss=close - 5.0,
        take_profit=close + 2.0,  # RR = 0.4, below any reasonable minimum
        score=8, score_label="VALID", meta={"entry_confirmed": True, "quality_confirmed": True},
    )
    validator = TradeValidator(_risk_settings(), min_risk_reward=1.5)
    v = validator.validate(sig, ctx, _symbol_spec(), _account(), _guard_input())
    assert v.approved is False
    assert any("risk:reward" in r.lower() for r in v.rejection_reasons)


def test_news_blackout_blocks_approval():
    ctx = h.trend_pullback_context(bullish=True)
    sig = _approvable_signal(ctx)
    validator = TradeValidator(_risk_settings(), news_filter=_AlwaysBlackout())
    v = validator.validate(sig, ctx, _symbol_spec(), _account(), _guard_input())
    assert v.approved is False
    assert v.news_ok is False
    assert v.news_available is True


def test_unavailable_news_filter_does_not_block_by_default():
    ctx = h.trend_pullback_context(bullish=True)
    sig = _approvable_signal(ctx)
    validator = TradeValidator(_risk_settings())  # default UnavailableNewsFilter
    v = validator.validate(sig, ctx, _symbol_spec(), _account(), _guard_input())
    assert v.news_available is False
    assert v.news_ok is True  # unavailable != blackout


def test_position_too_small_blocks_approval():
    ctx = h.trend_pullback_context(bullish=True)
    sig = _approvable_signal(ctx)
    validator = TradeValidator(_risk_settings(risk_per_trade_pct=0.01))
    v = validator.validate(sig, ctx, _symbol_spec(volume_min=10.0), _account(equity=100.0), _guard_input(equity=100.0, day_start_equity=100.0, week_start_equity=100.0))
    assert v.approved is False
    assert v.lots == 0.0
