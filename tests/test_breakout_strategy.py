import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import strategy_test_helpers as h
from app.regimes.detector import Regime, RegimeDecision
from app.signals.models import SignalDirection
from app.strategies.breakout import BreakoutConfig, BreakoutStrategy


def test_buy_signal_on_confirmed_bullish_breakout():
    ctx = h.breakout_context(bullish=True)
    strategy = BreakoutStrategy()
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.BUY
    assert signal.stop_loss < signal.entry
    assert signal.take_profit > signal.entry
    assert signal.meta.get("quality_confirmed") is True


def test_sell_signal_on_confirmed_bearish_breakout():
    ctx = h.breakout_context(bullish=False)
    strategy = BreakoutStrategy()
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.SELL
    assert signal.stop_loss > signal.entry
    assert signal.take_profit < signal.entry


def test_no_signal_when_price_has_not_broken_range():
    ctx = h.breakout_context(bullish=True)
    # Force the breakout level far above current price -- effectively
    # un-breaking it.
    ctx.m15_features["price_structure"]["breakout_up"] = ctx.m15_features["close"] + 1000.0
    ctx.m15_features["price_structure"]["breakout_down"] = ctx.m15_features["close"] - 1000.0
    strategy = BreakoutStrategy()
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.NO_SIGNAL
    assert "not broken" in signal.reasons[0]


def test_counter_trend_breakout_rejected_against_confirmed_h4_trend():
    ctx = h.breakout_context(bullish=True)
    ctx.h4_regime = RegimeDecision(regime=Regime.TREND_BEARISH, confidence=0.8, reasons=["forced for test"])
    strategy = BreakoutStrategy()
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.NO_SIGNAL
    assert "confirmed bearish" in signal.reasons[0]


def test_no_signal_without_prior_consolidation():
    ctx = h.breakout_context(bullish=True)
    # Simulate "was already volatile before the move" by raising the
    # threshold so nothing counts as a valid consolidation.
    strategy = BreakoutStrategy(BreakoutConfig(_env_file=None, consolidation_bb_width_threshold=0.0001))
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.NO_SIGNAL
    assert "consolidation" in signal.reasons[0]


def test_no_signal_without_volume_confirmation():
    ctx = h.breakout_context(bullish=True)
    ctx.m15_features["volume"]["volume_expansion"] = False
    ctx.m15_features["volume"]["relative_volume"] = 1.0
    strategy = BreakoutStrategy()
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.NO_SIGNAL
    assert "volume" in signal.reasons[0]


def test_extended_move_is_rejected():
    ctx = h.breakout_context(bullish=True)
    strategy = BreakoutStrategy(BreakoutConfig(_env_file=None, max_extension_atr=0.01))
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.NO_SIGNAL
    assert "extended" in signal.reasons[0]


def test_high_volatility_regime_flags_reduced_risk():
    ctx = h.breakout_context(bullish=True)
    ctx.h4_regime = RegimeDecision(regime=Regime.HIGH_VOLATILITY, confidence=0.7, reasons=["forced for test"])
    strategy = BreakoutStrategy()
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.BUY
    assert signal.meta.get("reduced_risk") is True
