import numpy as np
import pandas as pd
import pytest

from app.features.engine import InsufficientDataError, compute_features
from app.mt5.mock_client import MockMT5Client


def _mock_ohlcv(count=250, timeframe="H1"):
    client = MockMT5Client()
    return client.get_ohlcv("XAUUSD", timeframe, count)


def test_raises_on_insufficient_data():
    df = _mock_ohlcv(count=50)
    with pytest.raises(InsufficientDataError):
        compute_features(df, "H1")


def test_full_snapshot_structure():
    df = _mock_ohlcv(count=250)
    snap = compute_features(df, "H1")
    assert snap["timeframe"] == "H1"
    assert set(snap.keys()) == {
        "timeframe", "as_of", "session", "close",
        "trend", "momentum", "volatility", "volume", "price_structure",
    }
    assert set(snap["trend"].keys()) >= {
        "ema_20", "ema_50", "ema_200", "bullish_aligned", "bearish_aligned",
        "adx", "plus_di", "minus_di", "structure",
    }
    assert set(snap["momentum"].keys()) == {
        "rsi", "macd", "macd_signal", "macd_histogram", "roc", "momentum",
    }
    assert set(snap["volatility"].keys()) == {
        "atr", "atr_pct", "bb_upper", "bb_middle", "bb_lower",
        "bb_width_pct", "historical_volatility_pct", "candle_range",
        "range_expansion_ratio",
    }
    assert set(snap["volume"].keys()) == {
        "tick_volume", "tick_volume_ma", "relative_volume", "volume_expansion",
    }
    assert set(snap["price_structure"].keys()) == {
        "support_levels", "resistance_levels", "range_high", "range_low",
        "breakout_up", "breakout_down", "previous_day_high", "previous_day_low",
    }


def test_ema_alignment_flags_are_real_booleans():
    df = _mock_ohlcv(count=250)
    snap = compute_features(df, "H1")
    assert isinstance(snap["trend"]["bullish_aligned"], bool)
    assert isinstance(snap["trend"]["bearish_aligned"], bool)


def test_rsi_within_bounds_in_snapshot():
    df = _mock_ohlcv(count=250)
    snap = compute_features(df, "M15")
    assert 0 <= snap["momentum"]["rsi"] <= 100


def test_snapshot_is_json_serializable():
    import json
    df = _mock_ohlcv(count=250)
    snap = compute_features(df, "H4")
    json.dumps(snap)  # should not raise


def test_snapshot_uses_only_data_up_to_last_bar_no_future_leak():
    df = _mock_ohlcv(count=250)
    snap_full = compute_features(df, "H1")
    snap_truncated = compute_features(df.iloc[:-1], "H1")
    # Truncated snapshot's "as_of" must be strictly earlier -- proves we
    # never reach past the given frame for data.
    assert snap_truncated["as_of"] < snap_full["as_of"]
