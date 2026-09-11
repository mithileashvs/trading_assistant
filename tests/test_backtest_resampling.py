import numpy as np
import pandas as pd
import pytest

from app.backtesting.resampling import resample_closed_only


def _m15_df(n=50, start="2026-01-01 00:00"):
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    return pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 0.0, "close": 1.5, "tick_volume": 10, "spread": 5},
        index=idx,
    )


def test_partial_h1_period_is_dropped():
    df = _m15_df(n=3)  # 45 minutes, less than one full H1
    h1 = resample_closed_only(df, "H1")
    assert h1.empty


def test_exactly_one_closed_h1_period():
    df = _m15_df(n=4)  # 00:00-00:45 = one full H1 candle (00:00-01:00)
    h1 = resample_closed_only(df, "H1")
    assert len(h1) == 1
    assert h1.index[0] == pd.Timestamp("2026-01-01 00:00", tz="UTC")


def test_partial_second_h1_period_excluded():
    df = _m15_df(n=5)  # one full H1 + one bar into the next
    h1 = resample_closed_only(df, "H1")
    assert len(h1) == 1  # the second (partial) H1 candle must not appear


def test_two_closed_h1_periods():
    df = _m15_df(n=8)  # exactly two full H1 candles
    h1 = resample_closed_only(df, "H1")
    assert len(h1) == 2


def test_h4_requires_sixteen_m15_bars_per_candle():
    df = _m15_df(n=15)  # one bar short of a full H4 candle
    h4 = resample_closed_only(df, "H4")
    assert h4.empty

    df2 = _m15_df(n=16)
    h4_2 = resample_closed_only(df2, "H4")
    assert len(h4_2) == 1


def test_empty_input_returns_empty():
    df = _m15_df(n=0)
    h1 = resample_closed_only(df, "H1")
    assert h1.empty


def test_unsupported_timeframe_raises():
    df = _m15_df(n=20)
    with pytest.raises(ValueError):
        resample_closed_only(df, "M30")


def test_ohlc_aggregation_is_correct():
    idx = pd.date_range("2026-01-01 00:00", periods=4, freq="15min", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [10, 11, 9, 12],
            "high": [15, 14, 13, 16],
            "low": [9, 10, 8, 11],
            "close": [11, 9, 12, 15],
            "tick_volume": [100, 200, 150, 300],
            "spread": [10, 12, 8, 14],
        },
        index=idx,
    )
    h1 = resample_closed_only(df, "H1")
    assert len(h1) == 1
    row = h1.iloc[0]
    assert row["open"] == 10       # first bar's open
    assert row["close"] == 15      # last bar's close
    assert row["high"] == 16       # max of all highs
    assert row["low"] == 8         # min of all lows
    assert row["tick_volume"] == 750  # summed


def test_growing_window_never_changes_already_closed_candles():
    """The key no-lookahead property: a candle that resample_closed_only
    already returned as closed must never change value as more bars
    are appended -- proving future bars can't retroactively alter it."""
    df = _m15_df(n=40)
    h1_at_8 = resample_closed_only(df.iloc[:8], "H1")
    h1_at_40 = resample_closed_only(df.iloc[:40], "H1")
    # The first two H1 candles are closed in both windows -- compare them.
    pd.testing.assert_frame_equal(h1_at_8.iloc[:2], h1_at_40.iloc[:2])
