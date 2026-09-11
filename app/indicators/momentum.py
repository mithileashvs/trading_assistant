"""
Momentum indicators (section 6).
"""
from __future__ import annotations

import pandas as pd

from app.indicators.trend import _wilder_smooth


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = _wilder_smooth(gain, period)
    avg_loss = _wilder_smooth(loss, period)

    rs = avg_gain / avg_loss.replace(0, pd.NA)
    result = 100 - (100 / (1 + rs))
    # When avg_loss is 0 and avg_gain > 0, RSI is 100 by definition.
    result = result.where(avg_loss != 0, 100.0)
    return result


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    ema_fast = series.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = series.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "histogram": histogram},
        index=series.index,
    )


def rate_of_change(series: pd.Series, period: int = 10) -> pd.Series:
    return (series / series.shift(period) - 1.0) * 100.0


def momentum(series: pd.Series, period: int = 10) -> pd.Series:
    return series - series.shift(period)
