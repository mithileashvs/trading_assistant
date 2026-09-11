import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import pytest
import strategy_test_helpers as h
from app.regimes.detector import Regime, RegimeDecision
from app.signals.models import SignalDirection
from app.strategies.trend_pullback import TrendPullbackConfig, TrendPullbackStrategy

# Widened RSI bounds used across these tests: the strategy's DEFAULT
# bounds (40-65 / 35-60) are intentionally tight, and this synthetic
# low-volatility test data makes RSI swing further per bar than real
# gold ticks would -- the strategy's own numeric thresholds are
# explicitly configurable (section 8), so the "happy path" scenario
# uses a slightly wider config rather than fighting for a precise
# synthetic price path.
_WIDE_CONFIG = TrendPullbackConfig(_env_file=None, max_rsi_buy=72.0, min_rsi_sell=28.0)


def test_buy_signal_on_confirmed_bullish_pullback():
    ctx = h.trend_pullback_context(bullish=True)
    strategy = TrendPullbackStrategy(_WIDE_CONFIG)
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.BUY
    assert signal.entry is not None
    assert signal.stop_loss < signal.entry
    assert signal.take_profit > signal.entry
    assert signal.risk_reward == TrendPullbackConfig(_env_file=None).risk_reward
    assert signal.meta.get("entry_confirmed") is True


def test_sell_signal_on_confirmed_bearish_pullback():
    ctx = h.trend_pullback_context(bullish=False)
    strategy = TrendPullbackStrategy(_WIDE_CONFIG)
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.SELL
    assert signal.stop_loss > signal.entry
    assert signal.take_profit < signal.entry


def test_no_signal_when_h4_regime_not_trending():
    ctx = h.trend_pullback_context(bullish=True)
    ctx.h4_regime = RegimeDecision(regime=Regime.RANGE, confidence=0.6, reasons=["forced for test"])
    strategy = TrendPullbackStrategy(_WIDE_CONFIG)
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.NO_SIGNAL
    assert "not a confirmed trend" in signal.reasons[0]


def test_no_signal_when_h1_ema_contradicts_h4_trend():
    ctx = h.trend_pullback_context(bullish=True)
    # Force H1 EMA20 below EMA50 -- contradicts the bullish H4 trend.
    ctx.h1_features["trend"]["ema_20"] = ctx.h1_features["trend"]["ema_50"] - 1.0
    strategy = TrendPullbackStrategy(_WIDE_CONFIG)
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.NO_SIGNAL
    assert "does not confirm" in signal.reasons[0]


def test_no_signal_when_rsi_outside_default_bounds():
    # Using the STRICT default config on the same fixture that produces
    # a valid signal under the widened config -- the fixture pushes RSI
    # to ~70, outside the default max_rsi_buy of 65.
    ctx = h.trend_pullback_context(bullish=True)
    strategy = TrendPullbackStrategy()  # default (strict) config
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.NO_SIGNAL
    assert "RSI" in signal.reasons[0]


def test_never_buys_merely_because_price_touches_ema_without_rejection():
    ctx = h.trend_pullback_context(bullish=True)
    # Flatten the rejection bar into a doji so it's no longer a genuine
    # bullish-body rejection candle.
    last_idx = ctx.m15_df.index[-1]
    ctx.m15_df.loc[last_idx, "close"] = ctx.m15_df.loc[last_idx, "open"]
    ctx.m15_features = h.compute_features(ctx.m15_df, "M15")
    strategy = TrendPullbackStrategy(_WIDE_CONFIG)
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.NO_SIGNAL


def test_stop_and_target_use_atr_multiples():
    ctx = h.trend_pullback_context(bullish=True)
    cfg = TrendPullbackConfig(_env_file=None, max_rsi_buy=72.0, stop_atr_multiplier=2.0, risk_reward=3.0)
    strategy = TrendPullbackStrategy(cfg)
    signal = strategy.generate(ctx)
    assert signal.direction == SignalDirection.BUY
    atr = ctx.m15_features["volatility"]["atr"]
    expected_stop = signal.entry - atr * 2.0
    assert signal.stop_loss == pytest.approx(expected_stop)
    assert signal.risk_reward == pytest.approx(3.0)
