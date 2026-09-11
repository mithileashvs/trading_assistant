import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import strategy_test_helpers as h
from app.signals.models import SignalDirection
from app.signals.scoring import ScoringWeights, classify_score, score_signal
from app.strategies.trend_pullback import TrendPullbackConfig, TrendPullbackStrategy


def test_no_signal_is_not_scored():
    ctx = h.trend_pullback_context(bullish=True)
    ctx.h4_regime.regime = ctx.h4_regime.regime  # no-op
    from app.signals.models import no_signal
    sig = no_signal("X", "nothing")
    scored = score_signal(sig, ctx)
    assert scored.score is None
    assert scored.score_label is None


def test_full_signal_scores_within_max():
    ctx = h.trend_pullback_context(bullish=True)
    cfg = TrendPullbackConfig(_env_file=None, max_rsi_buy=72.0)
    strategy = TrendPullbackStrategy(cfg)
    sig = strategy.generate(ctx)
    assert sig.direction == SignalDirection.BUY
    scored = score_signal(sig, ctx)
    weights = ScoringWeights()
    assert scored.score is not None
    assert 0 <= scored.score <= weights.max_score
    assert scored.score_label in {"NO_TRADE", "WEAK", "VALID", "STRONG"}


def test_h4_trend_alignment_awarded_when_regime_matches_direction():
    ctx = h.trend_pullback_context(bullish=True)
    cfg = TrendPullbackConfig(_env_file=None, max_rsi_buy=72.0)
    sig = TrendPullbackStrategy(cfg).generate(ctx)
    scored = score_signal(sig, ctx)
    weights = ScoringWeights()
    assert scored.score_breakdown["h4_trend_alignment"] == weights.h4_trend_alignment


def test_range_regime_gives_partial_h4_credit_for_mean_reversion():
    from app.strategies.mean_reversion import MeanReversionStrategy
    ctx = h.mean_reversion_context(bullish=True)
    sig = MeanReversionStrategy().generate(ctx)
    scored = score_signal(sig, ctx)
    weights = ScoringWeights()
    assert scored.score_breakdown["h4_trend_alignment"] == weights.h4_trend_alignment // 2


def test_classify_score_breakpoints():
    weights = ScoringWeights()
    assert classify_score(weights.strong_threshold, weights) == "STRONG"
    assert classify_score(weights.valid_threshold, weights) == "VALID"
    assert classify_score(weights.weak_threshold, weights) == "WEAK"
    assert classify_score(weights.weak_threshold - 1, weights) == "NO_TRADE"


def test_weights_are_configurable_and_change_max_score():
    default = ScoringWeights()
    custom = ScoringWeights(_env_file=None, h4_trend_alignment=5)
    assert custom.max_score == default.max_score + (5 - default.h4_trend_alignment)


def test_entry_confirmation_and_setup_quality_come_from_signal_meta():
    ctx = h.trend_pullback_context(bullish=True)
    cfg = TrendPullbackConfig(_env_file=None, max_rsi_buy=72.0)
    sig = TrendPullbackStrategy(cfg).generate(ctx)
    sig.meta["entry_confirmed"] = False
    sig.meta["quality_confirmed"] = False
    scored = score_signal(sig, ctx)
    assert scored.score_breakdown["entry_confirmation"] == 0
    assert scored.score_breakdown["setup_quality"] == 0
