import pytest

from app.regimes.detector import Regime, detect_regime
from app.regimes.thresholds import RegimeThresholds


def _thresholds():
    return RegimeThresholds(_env_file=None)


def _base_features(**overrides) -> dict:
    """A minimal but complete feature snapshot shape, as produced by
    compute_features(), with sane neutral defaults that can be
    overridden per test."""
    base = {
        "timeframe": "H4",
        "trend": {
            "ema_20": 100.0, "ema_50": 100.0, "ema_200": 100.0,
            "bullish_aligned": False, "bearish_aligned": False,
            "adx": 15.0, "plus_di": 20.0, "minus_di": 20.0,
            "structure": "SIDEWAYS",
        },
        "momentum": {"rsi": 50.0, "macd": 0.0, "macd_signal": 0.0, "macd_histogram": 0.0, "roc": 0.0, "momentum": 0.0},
        "volatility": {
            "atr": 3.0, "atr_pct": 0.15, "bb_upper": 101, "bb_middle": 100, "bb_lower": 99,
            "bb_width_pct": 0.4, "historical_volatility_pct": 0.1, "candle_range": 2.0,
            "range_expansion_ratio": 1.0,
        },
        "volume": {"tick_volume": 100, "tick_volume_ma": 100, "relative_volume": 1.0, "volume_expansion": False},
        "price_structure": {},
    }
    for section, values in overrides.items():
        base[section].update(values)
    return base


def test_trend_bullish_when_all_mandatory_conditions_met():
    features = _base_features(
        trend={"bullish_aligned": True, "adx": 28.0, "plus_di": 30.0, "minus_di": 15.0, "structure": "BULLISH"},
        momentum={"macd_histogram": 0.5, "rsi": 60.0},
    )
    decision = detect_regime(features, _thresholds())
    assert decision.regime == Regime.TREND_BULLISH
    assert decision.confidence > 0.6
    assert len(decision.reasons) >= 3


def test_trend_bearish_when_all_mandatory_conditions_met():
    features = _base_features(
        trend={"bearish_aligned": True, "adx": 30.0, "plus_di": 10.0, "minus_di": 28.0, "structure": "BEARISH"},
        momentum={"macd_histogram": -0.5, "rsi": 35.0},
    )
    decision = detect_regime(features, _thresholds())
    assert decision.regime == Regime.TREND_BEARISH
    assert decision.confidence > 0.6


def test_ema_bullish_alone_without_adx_confirmation_is_not_trend():
    # bullish EMA alignment but weak ADX -> should NOT be classified as trending
    features = _base_features(
        trend={"bullish_aligned": True, "adx": 12.0, "plus_di": 22.0, "minus_di": 18.0},
    )
    decision = detect_regime(features, _thresholds())
    assert decision.regime != Regime.TREND_BULLISH


def test_high_adx_without_ema_alignment_is_not_trend():
    features = _base_features(
        trend={"bullish_aligned": False, "bearish_aligned": False, "adx": 30.0, "plus_di": 30.0, "minus_di": 10.0},
    )
    decision = detect_regime(features, _thresholds())
    assert decision.regime not in {Regime.TREND_BULLISH, Regime.TREND_BEARISH}


def test_high_volatility_detected_from_atr():
    features = _base_features(
        volatility={"atr_pct": 0.9, "bb_width_pct": 0.4, "range_expansion_ratio": 1.0},
    )
    decision = detect_regime(features, _thresholds())
    assert decision.regime == Regime.HIGH_VOLATILITY


def test_high_volatility_overrides_weak_trend_signal():
    # ADX/EMA alignment don't clear the mandatory trend bar, but ATR is extreme
    features = _base_features(
        trend={"bullish_aligned": True, "adx": 19.0, "plus_di": 21.0, "minus_di": 19.5},
        volatility={"atr_pct": 1.0, "bb_width_pct": 2.0, "range_expansion_ratio": 2.5},
    )
    decision = detect_regime(features, _thresholds())
    assert decision.regime == Regime.HIGH_VOLATILITY


def test_confirmed_trend_is_not_reclassified_as_high_volatility():
    # Strong, confirmed trend should win even if ATR is somewhat elevated,
    # since trending markets are often more volatile than ranges.
    features = _base_features(
        trend={"bullish_aligned": True, "adx": 35.0, "plus_di": 35.0, "minus_di": 10.0, "structure": "BULLISH"},
        volatility={"atr_pct": 0.5, "bb_width_pct": 0.4, "range_expansion_ratio": 1.0},
        momentum={"macd_histogram": 1.0, "rsi": 65.0},
    )
    decision = detect_regime(features, _thresholds())
    assert decision.regime == Regime.TREND_BULLISH


def test_range_detected_when_low_adx_and_tight_bands():
    features = _base_features(
        trend={"adx": 12.0, "structure": "SIDEWAYS"},
        volatility={"atr_pct": 0.1, "bb_width_pct": 0.3, "range_expansion_ratio": 0.8},
    )
    decision = detect_regime(features, _thresholds())
    assert decision.regime == Regime.RANGE
    assert decision.confidence > 0.5


def test_uncertain_when_nothing_decisive():
    features = _base_features(
        trend={"adx": 19.0, "plus_di": 21.0, "minus_di": 19.0, "structure": "SIDEWAYS"},
        volatility={"atr_pct": 0.2, "bb_width_pct": 0.8, "range_expansion_ratio": 1.0},
    )
    decision = detect_regime(features, _thresholds())
    assert decision.regime == Regime.UNCERTAIN
    assert decision.confidence < 0.5


def test_confidence_always_between_zero_and_one():
    scenarios = [
        _base_features(trend={"bullish_aligned": True, "adx": 40, "plus_di": 40, "minus_di": 5, "structure": "BULLISH"}, momentum={"macd_histogram": 2, "rsi": 80}),
        _base_features(volatility={"atr_pct": 5.0, "bb_width_pct": 5.0, "range_expansion_ratio": 5.0}),
        _base_features(trend={"adx": 5.0}, volatility={"bb_width_pct": 0.1}),
        _base_features(),
    ]
    for features in scenarios:
        decision = detect_regime(features, _thresholds())
        assert 0.0 <= decision.confidence <= 1.0


def test_to_dict_matches_documented_shape():
    features = _base_features(
        trend={"bullish_aligned": True, "adx": 28.0, "plus_di": 30.0, "minus_di": 15.0, "structure": "BULLISH"},
        momentum={"macd_histogram": 0.5, "rsi": 60.0},
    )
    decision = detect_regime(features, _thresholds())
    d = decision.to_dict()
    assert set(d.keys()) == {"regime", "confidence", "reasons", "timeframe"}
    assert isinstance(d["regime"], str)
    assert isinstance(d["confidence"], float)
    assert isinstance(d["reasons"], list) and all(isinstance(r, str) for r in d["reasons"])


def test_thresholds_are_configurable_and_change_outcome():
    features = _base_features(
        trend={"bullish_aligned": True, "adx": 22.0, "plus_di": 25.0, "minus_di": 20.0, "structure": "BULLISH"},
    )
    strict = RegimeThresholds(_env_file=None, trend_adx_threshold=30.0)
    lenient = RegimeThresholds(_env_file=None, trend_adx_threshold=15.0)

    strict_decision = detect_regime(features, strict)
    lenient_decision = detect_regime(features, lenient)

    assert strict_decision.regime != Regime.TREND_BULLISH
    assert lenient_decision.regime == Regime.TREND_BULLISH
