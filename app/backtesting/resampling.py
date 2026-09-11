"""
No-lookahead resampling (section 24: "Avoid using future candles to
make historical decisions").

Given an M15 window that ends at some "current" bar, this derives H1
and H4 candles using ONLY fully-closed higher-timeframe periods —
never a partially-formed H1/H4 bar that includes the still-forming
current M15 bar. This is what lets the backtest engine build genuine
H4/H1/M15 StrategyContext objects at each step without peeking ahead.
"""
from __future__ import annotations

import pandas as pd

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last",
        "tick_volume": "sum", "spread": "mean"}

_RULE = {"H1": "1h", "H4": "4h"}


def resample_closed_only(m15_window: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample an M15 window to H1 or H4, keeping only bars whose full
    period is contained within the window — i.e. drop the last
    resampled bar if the M15 window doesn't yet cover its entire span.
    """
    if timeframe not in _RULE:
        raise ValueError(f"Unsupported timeframe '{timeframe}' (use 'H1' or 'H4').")
    if m15_window.empty:
        return m15_window

    rule = _RULE[timeframe]
    resampled = m15_window.resample(rule, label="left", closed="left").agg(_AGG).dropna()
    if resampled.empty:
        return resampled

    # A resampled bar is "closed" only if the window's last M15
    # timestamp is at or past that bar's period end (next bar's start).
    period_minutes = 60 if timeframe == "H1" else 240
    last_bar_start = resampled.index[-1]
    period_end = last_bar_start + pd.Timedelta(minutes=period_minutes)
    window_end = m15_window.index[-1] + pd.Timedelta(minutes=15)  # end of the last M15 bar

    if window_end < period_end:
        resampled = resampled.iloc[:-1]

    return resampled
