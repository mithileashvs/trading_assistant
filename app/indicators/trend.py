"""
Trend indicators (section 6).

All functions take/return pandas Series or DataFrames indexed the same
way as the input OHLCV frame, so callers can align columns without
worrying about off-by-one index shifts. No look-ahead: every value at
index i is computed only from data at or before i.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    if period < 1:
        raise ValueError("period must be >= 1")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    if period < 1:
        raise ValueError("period must be >= 1")
    return series.rolling(window=period, min_periods=period).mean()


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (used by ADX/RSI/ATR in their original
    definitions) — equivalent to an EMA with alpha = 1/period."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average Directional Index with +DI / -DI.

    df must have columns: high, low, close.
    Returns a DataFrame with columns: plus_di, minus_di, adx.
    """
    high, low, close = df["high"], df["low"], df["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr_smooth = _wilder_smooth(tr, period)
    plus_di = 100 * _wilder_smooth(plus_dm, period) / atr_smooth.replace(0, np.nan)
    minus_di = 100 * _wilder_smooth(minus_dm, period) / atr_smooth.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_line = _wilder_smooth(dx, period)

    return pd.DataFrame(
        {"plus_di": plus_di, "minus_di": minus_di, "adx": adx_line}, index=df.index
    )


def ema_alignment(close: pd.Series, fast: int = 20, mid: int = 50, slow: int = 200) -> dict:
    """Returns whether EMAs are in bullish/bearish stacked order at the
    latest bar, plus the raw values — used directly by the regime
    detector's explainability output (section 7)."""
    ema_fast = ema(close, fast).iloc[-1]
    ema_mid = ema(close, mid).iloc[-1]
    ema_slow = ema(close, slow).iloc[-1]
    bullish = ema_fast > ema_mid > ema_slow
    bearish = ema_fast < ema_mid < ema_slow
    return {
        f"ema_{fast}": ema_fast,
        f"ema_{mid}": ema_mid,
        f"ema_{slow}": ema_slow,
        "bullish_aligned": bool(bullish),
        "bearish_aligned": bool(bearish),
    }
