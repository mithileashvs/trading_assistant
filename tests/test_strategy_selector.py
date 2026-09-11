import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import strategy_test_helpers as h
from app.regimes.detector import Regime, RegimeDecision
from app.signals.models import SignalDirection
from app.strategies.selector import StrategySelector
from app.strategies.trend_pullback import TrendPullbackConfig, TrendPullbackStrategy


def test_uncertain_regime_produces_no_trade_without_running_any_strategy():
    ctx = h.trend_pullback_context(bullish=True)
    ctx.h4_regime = RegimeDecision(regime=Regime.UNCERTAIN, confidence=0.3, reasons=["forced"])
    selector = StrategySelector()
    signals = selector.generate_signals(ctx)
    assert len(signals) == 1
    assert signals[0].direction == SignalDirection.NO_SIGNAL
    assert signals[0].strategy == "STRATEGY_SELECTOR"


def test_trend_bullish_regime_runs_trend_pullback_and_breakout():
    ctx = h.trend_pullback_context(bullish=True)
    selector = StrategySelector()
    names = selector.active_strategy_names(ctx.h4_regime.regime)
    assert set(names) == {"TREND_PULLBACK", "BREAKOUT"}


def test_range_regime_runs_mean_reversion_and_breakout():
    ctx = h.mean_reversion_context(bullish=True)
    selector = StrategySelector()
    names = selector.active_strategy_names(ctx.h4_regime.regime)
    assert set(names) == {"MEAN_REVERSION", "BREAKOUT"}


def test_high_volatility_only_runs_breakout():
    selector = StrategySelector()
    names = selector.active_strategy_names(Regime.HIGH_VOLATILITY)
    assert names == ["BREAKOUT"]


def test_best_signal_picks_highest_score_among_actionable():
    ctx = h.trend_pullback_context(bullish=True)
    cfg = TrendPullbackConfig(_env_file=None, max_rsi_buy=72.0)

    selector = StrategySelector(strategies={"TREND_PULLBACK": TrendPullbackStrategy(cfg)}, enabled={"TREND_PULLBACK": True})
    best = selector.best_signal(ctx)
    assert best.direction == SignalDirection.BUY
    assert best.score is not None


def test_best_signal_is_no_signal_when_nothing_triggers():
    ctx = h.trend_pullback_context(bullish=True)
    ctx.h4_regime = RegimeDecision(regime=Regime.UNCERTAIN, confidence=0.3, reasons=["forced"])
    selector = StrategySelector()
    best = selector.best_signal(ctx)
    assert best.direction == SignalDirection.NO_SIGNAL


def test_disabling_a_strategy_removes_it_from_active_list():
    selector = StrategySelector(enabled={"TREND_PULLBACK": False, "BREAKOUT": True, "MEAN_REVERSION": True})
    names = selector.active_strategy_names(Regime.TREND_BULLISH)
    assert "TREND_PULLBACK" not in names
    assert "BREAKOUT" in names


def test_generate_signals_never_returns_empty_list():
    ctx = h.trend_pullback_context(bullish=True)
    for regime in Regime:
        ctx.h4_regime = RegimeDecision(regime=regime, confidence=0.5, reasons=["forced"])
        selector = StrategySelector()
        signals = selector.generate_signals(ctx)
        assert len(signals) >= 1
