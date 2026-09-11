import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import strategy_test_helpers as h
from app.regimes.detector import Regime, RegimeDecision
from app.signals.models import SignalDirection
from app.strategies.mean_reversion import MeanReversionConfig, MeanReversionStrategy


def test_buy_signal_on_confirmed_oversold_range():
    ctx = h.mean_reversion_context(bullish=True)
    strategy = MeanReversionStrategy()
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.BUY
    assert signal.stop_loss < signal.entry
    assert signal.take_profit > signal.entry


def test_sell_signal_on_confirmed_overbought_range():
    ctx = h.mean_reversion_context(bullish=False)
    strategy = MeanReversionStrategy()
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.SELL
    assert signal.stop_loss > signal.entry
    assert signal.take_profit < signal.entry


def test_disabled_outside_range_regime():
    ctx = h.mean_reversion_context(bullish=True)
    ctx.h4_regime = RegimeDecision(regime=Regime.TREND_BEARISH, confidence=0.8, reasons=["forced for test"])
    strategy = MeanReversionStrategy()
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.NO_SIGNAL
    assert "not RANGE" in signal.reasons[0]


def test_never_buys_oversold_during_strong_bearish_trend():
    # Even if H4 hasn't been overridden, a strongly trending H1 (per
    # its own ADX) must disable mean reversion outright (section 10).
    ctx = h.mean_reversion_context(bullish=True)
    ctx.h1_features["trend"]["adx"] = 40.0
    strategy = MeanReversionStrategy()
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.NO_SIGNAL
    assert "trending strongly" in signal.reasons[0]


def test_no_signal_when_deviation_too_small():
    ctx = h.mean_reversion_context(bullish=True)
    strategy = MeanReversionStrategy(MeanReversionConfig(_env_file=None, min_deviation_atr=100.0))
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.NO_SIGNAL


def test_target_defaults_to_mean():
    ctx = h.mean_reversion_context(bullish=True)
    strategy = MeanReversionStrategy()
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.BUY
    assert signal.take_profit == ctx.m15_features["volatility"]["bb_middle"]
