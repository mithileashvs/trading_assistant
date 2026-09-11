"""
Volume indicators (section 6).

MT5 only provides tick volume (number of price changes), not true
traded volume like a centralized exchange — these functions are named
and documented accordingly so callers don't misinterpret the numbers.
"""
from __future__ import annotations

import pandas as pd


def volume_moving_average(tick_volume: pd.Series, period: int = 20) -> pd.Series:
    return tick_volume.rolling(window=period, min_periods=period).mean()


def relative_volume(tick_volume: pd.Series, period: int = 20) -> pd.Series:
    """Current tick volume relative to its recent average. >1 means
    more price-change activity than usual for this instrument/timeframe."""
    avg = volume_moving_average(tick_volume, period)
    return tick_volume / avg.replace(0, pd.NA)


def volume_expansion(tick_volume: pd.Series, period: int = 20, threshold: float = 1.5) -> pd.Series:
    """Boolean series: True where relative tick volume exceeds threshold."""
    rel = relative_volume(tick_volume, period)
    return rel > threshold
