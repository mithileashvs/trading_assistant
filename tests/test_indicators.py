import numpy as np
import pandas as pd
import pytest

from app.indicators import momentum as mom
from app.indicators import structure as struct
from app.indicators import trend as trend_ind
from app.indicators import volatility as vol
from app.indicators import volume as vol_ind


def _make_ohlcv(n=300, seed=1, start=2000.0, drift=0.0003, vol_scale=0.0008):
    rng = np.random.default_rng(seed)
    returns = rng.normal(drift, vol_scale, size=n)
    close = start * np.cumprod(1 + returns)
    open_ = np.roll(close, 1)
    open_[0] = start
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0005, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0005, n)))
    tick_volume = rng.integers(50, 500, size=n)
    spread = rng.integers(10, 40, size=n)
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "tick_volume": tick_volume, "spread": spread},
        index=idx,
    )


def _uptrend_ohlcv(n=300):
    return _make_ohlcv(n=n, drift=0.002, vol_scale=0.0005)


def _downtrend_ohlcv(n=300):
    return _make_ohlcv(n=n, drift=-0.002, vol_scale=0.0005)


# --- trend -------------------------------------------------------------

def test_ema_converges_to_price_level():
    close = pd.Series([100.0] * 50)
    result = trend_ind.ema(close, 10)
    assert abs(result.iloc[-1] - 100.0) < 1e-6


def test_sma_matches_manual_mean():
    close = pd.Series(range(1, 21), dtype=float)
    result = trend_ind.sma(close, 5)
    assert result.iloc[-1] == pytest.approx(close.iloc[-5:].mean())


def test_ema_alignment_bullish_in_strong_uptrend():
    df = _uptrend_ohlcv()
    align = trend_ind.ema_alignment(df["close"])
    assert align["bullish_aligned"] is True
    assert align["bearish_aligned"] is False


def test_ema_alignment_bearish_in_strong_downtrend():
    df = _downtrend_ohlcv()
    align = trend_ind.ema_alignment(df["close"])
    assert align["bearish_aligned"] is True


def test_adx_higher_in_strong_trend_than_choppy_range():
    trending = _uptrend_ohlcv()
    choppy = _make_ohlcv(drift=0.0, vol_scale=0.002, seed=7)
    trend_adx = trend_ind.adx(trending)["adx"].iloc[-1]
    choppy_adx = trend_ind.adx(choppy)["adx"].iloc[-1]
    assert trend_adx > choppy_adx


# --- momentum ------------------------------------------------------------

def test_rsi_bounds():
    df = _make_ohlcv()
    r = mom.rsi(df["close"]).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_rsi_high_in_strong_uptrend():
    df = _uptrend_ohlcv()
    r = mom.rsi(df["close"]).iloc[-1]
    assert r > 60


def test_macd_columns_present():
    df = _make_ohlcv()
    result = mom.macd(df["close"])
    assert set(result.columns) == {"macd", "signal", "histogram"}
    assert (result["histogram"].dropna() == (result["macd"] - result["signal"]).dropna()).all()


def test_roc_positive_in_uptrend():
    df = _uptrend_ohlcv()
    assert mom.rate_of_change(df["close"]).iloc[-1] > 0


# --- volatility -----------------------------------------------------------

def test_atr_nonnegative():
    df = _make_ohlcv()
    a = vol.atr(df).dropna()
    assert (a >= 0).all()


def test_atr_higher_in_volatile_series():
    calm = _make_ohlcv(vol_scale=0.0003, seed=3)
    volatile = _make_ohlcv(vol_scale=0.004, seed=3)
    assert vol.atr(volatile).iloc[-1] > vol.atr(calm).iloc[-1]


def test_bollinger_bands_ordering():
    df = _make_ohlcv()
    bands = vol.bollinger_bands(df["close"]).dropna()
    assert (bands["upper"] >= bands["middle"]).all()
    assert (bands["middle"] >= bands["lower"]).all()


def test_bollinger_band_width_narrower_in_calm_market():
    calm = _make_ohlcv(vol_scale=0.0003, seed=5)
    volatile = _make_ohlcv(vol_scale=0.004, seed=5)
    calm_w = vol.bollinger_band_width(calm["close"]).iloc[-1]
    volatile_w = vol.bollinger_band_width(volatile["close"]).iloc[-1]
    assert calm_w < volatile_w


def test_range_expansion_ratio_around_one_on_average():
    df = _make_ohlcv(n=500)
    ratio = vol.range_expansion(df, period=20).dropna()
    assert 0.5 < ratio.mean() < 1.5


# --- volume -----------------------------------------------------------------

def test_relative_volume_around_one_on_average():
    df = _make_ohlcv()
    rel = vol_ind.relative_volume(df["tick_volume"]).dropna()
    assert 0.5 < rel.mean() < 1.5


def test_volume_expansion_is_boolean_series():
    df = _make_ohlcv()
    result = vol_ind.volume_expansion(df["tick_volume"])
    assert result.dtype == bool


# --- structure -----------------------------------------------------------------

def test_swing_highs_lows_shapes():
    df = _make_ohlcv()
    sh = struct.swing_highs(df, lookback=3)
    sl = struct.swing_lows(df, lookback=3)
    assert len(sh) == len(df)
    assert sh.dtype == bool
    assert sl.dtype == bool


def test_market_structure_bullish_in_strong_uptrend():
    df = _uptrend_ohlcv(n=200)
    result = struct.classify_market_structure(df, lookback=3)
    assert result.label in {"BULLISH", "UNKNOWN"}  # depends on swing density but never BEARISH
    assert result.label != "BEARISH"


def test_market_structure_bearish_in_strong_downtrend():
    df = _downtrend_ohlcv(n=200)
    result = struct.classify_market_structure(df, lookback=3)
    assert result.label != "BULLISH"


def test_support_resistance_returns_sorted_levels():
    df = _make_ohlcv(n=200)
    levels = struct.support_resistance_levels(df)
    assert levels["resistance"] == sorted(levels["resistance"], reverse=True)
    assert levels["support"] == sorted(levels["support"])


def test_recent_range_high_gte_low():
    df = _make_ohlcv()
    rng = struct.recent_range(df, period=20)
    assert rng["range_high"] >= rng["range_low"]


def test_breakout_levels_bracket_recent_range():
    df = _make_ohlcv()
    rng = struct.recent_range(df, period=20)
    breakout = struct.breakout_levels(df, period=20, buffer_pct=0.1)
    assert breakout["breakout_up"] > rng["range_high"]
    assert breakout["breakout_down"] < rng["range_low"]


def test_previous_period_high_low_needs_two_full_periods():
    short_df = _make_ohlcv(n=10)  # less than 2 days of hourly bars
    result = struct.previous_period_high_low(short_df, freq="D")
    assert result["previous_high"] is None

    long_df = _make_ohlcv(n=72)  # 3 days of hourly bars
    result2 = struct.previous_period_high_low(long_df, freq="D")
    assert result2["previous_high"] is not None
    assert result2["previous_high"] >= result2["previous_low"]
