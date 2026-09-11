"""
Volatility indicators (section 6).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.indicators.trend import _wilder_smooth


def true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    return pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = true_range(df)
    return _wilder_smooth(tr, period)


def atr_percent(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR expressed as a percentage of closing price — comparable
    across instruments/timeframes, unlike raw ATR."""
    atr_series = atr(df, period)
    return (atr_series / df["close"]) * 100.0


def bollinger_bands(
    series: pd.Series, period: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    mid = series.rolling(window=period, min_periods=period).mean()
    std = series.rolling(window=period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return pd.DataFrame(
        {"upper": upper, "middle": mid, "lower": lower}, index=series.index
    )


def bollinger_band_width(
    series: pd.Series, period: int = 20, num_std: float = 2.0
) -> pd.Series:
    bands = bollinger_bands(series, period, num_std)
    return (bands["upper"] - bands["lower"]) / bands["middle"].replace(0, np.nan) * 100.0


def historical_volatility(series: pd.Series, period: int = 20, annualize_factor: int | None = None) -> pd.Series:
    """Rolling standard deviation of log returns. If annualize_factor is
    given (e.g. bars-per-year for the timeframe), scales by sqrt(N)."""
    log_returns = np.log(series / series.shift(1))
    vol = log_returns.rolling(window=period, min_periods=period).std(ddof=0)
    if annualize_factor:
        vol = vol * np.sqrt(annualize_factor)
    return vol * 100.0


def candle_range(df: pd.DataFrame) -> pd.Series:
    return df["high"] - df["low"]


def range_expansion(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Current candle range relative to its recent average — values > 1
    indicate range expansion, < 1 indicate contraction/consolidation."""
    rng = candle_range(df)
    avg_rng = rng.rolling(window=period, min_periods=period).mean()
    return rng / avg_rng.replace(0, np.nan)
