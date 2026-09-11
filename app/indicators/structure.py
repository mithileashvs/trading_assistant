"""
Price structure indicators (section 6).

Swing detection uses a simple fractal rule: a bar is a swing high if
its high is the highest within `lookback` bars on both sides (and
symmetric for swing lows). This is deterministic and explainable,
matching the "first version should be explainable" principle applied
to the regime detector (section 7) — the same philosophy applies here.

IMPORTANT no-lookahead note: a swing point at bar i is only confirmed
once `lookback` bars *after* it exist. Callers that need "confirmed
swings as of now" should ignore the last `lookback` bars of the output
series when using it to drive live decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd


def swing_highs(df: pd.DataFrame, lookback: int = 3) -> pd.Series:
    high = df["high"]
    is_max = pd.Series(True, index=df.index)
    for shift in list(range(1, lookback + 1)) + [-i for i in range(1, lookback + 1)]:
        is_max &= high > high.shift(shift)
    return is_max.fillna(False)


def swing_lows(df: pd.DataFrame, lookback: int = 3) -> pd.Series:
    low = df["low"]
    is_min = pd.Series(True, index=df.index)
    for shift in list(range(1, lookback + 1)) + [-i for i in range(1, lookback + 1)]:
        is_min &= low < low.shift(shift)
    return is_min.fillna(False)


@dataclass
class MarketStructure:
    label: str  # "BULLISH" | "BEARISH" | "SIDEWAYS" | "UNKNOWN"
    last_swing_highs: list[float]
    last_swing_lows: list[float]
    reasons: list[str]


def classify_market_structure(df: pd.DataFrame, lookback: int = 3, n_swings: int = 4) -> MarketStructure:
    """Classify structure from the sequence of the most recent confirmed
    swing highs/lows (section 6: HH/HL vs LH/LL)."""
    sh = swing_highs(df, lookback)
    sl = swing_lows(df, lookback)

    # Drop the last `lookback` bars: swings there aren't confirmed yet
    # (no look-ahead into "future" bars that don't really exist live).
    confirmed = df.iloc[: len(df) - lookback] if lookback > 0 else df
    sh = sh.loc[confirmed.index]
    sl = sl.loc[confirmed.index]

    highs = confirmed.loc[sh, "high"].tail(n_swings).tolist()
    lows = confirmed.loc[sl, "low"].tail(n_swings).tolist()

    reasons = []
    higher_highs = len(highs) >= 2 and all(highs[i] < highs[i + 1] for i in range(len(highs) - 1))
    higher_lows = len(lows) >= 2 and all(lows[i] < lows[i + 1] for i in range(len(lows) - 1))
    lower_highs = len(highs) >= 2 and all(highs[i] > highs[i + 1] for i in range(len(highs) - 1))
    lower_lows = len(lows) >= 2 and all(lows[i] > lows[i + 1] for i in range(len(lows) - 1))

    if higher_highs and higher_lows:
        label = "BULLISH"
        reasons = ["sequence of higher highs", "sequence of higher lows"]
    elif lower_highs and lower_lows:
        label = "BEARISH"
        reasons = ["sequence of lower highs", "sequence of lower lows"]
    elif highs and lows:
        label = "SIDEWAYS"
        reasons = ["no consistent higher-high/higher-low or lower-high/lower-low sequence"]
    else:
        label = "UNKNOWN"
        reasons = ["not enough confirmed swing points yet"]

    return MarketStructure(label=label, last_swing_highs=highs, last_swing_lows=lows, reasons=reasons)


def support_resistance_levels(df: pd.DataFrame, lookback: int = 3, n_levels: int = 5) -> dict:
    """Naive support/resistance from recent confirmed swing points,
    merged when within a small tolerance of each other."""
    sh = swing_highs(df, lookback)
    sl = swing_lows(df, lookback)
    confirmed = df.iloc[: len(df) - lookback] if lookback > 0 else df
    sh = sh.loc[confirmed.index]
    sl = sl.loc[confirmed.index]

    resistance = sorted(confirmed.loc[sh, "high"].tail(n_levels * 3).unique().tolist(), reverse=True)[:n_levels]
    support = sorted(confirmed.loc[sl, "low"].tail(n_levels * 3).unique().tolist())[:n_levels]
    return {"resistance": resistance, "support": support}


def previous_period_high_low(df: pd.DataFrame, freq: str = "D") -> dict:
    """Previous completed day/session high & low (section 6). `freq`
    follows pandas resample rules, e.g. 'D' for calendar day.

    df.index must be a tz-aware DatetimeIndex.
    """
    if df.empty:
        return {"previous_high": None, "previous_low": None}
    resampled = df.resample(freq).agg({"high": "max", "low": "min"}).dropna()
    if len(resampled) < 2:
        return {"previous_high": None, "previous_low": None}
    prev = resampled.iloc[-2]
    return {"previous_high": float(prev["high"]), "previous_low": float(prev["low"])}


def recent_range(df: pd.DataFrame, period: int = 20) -> dict:
    """Highest high / lowest low over the `period` bars strictly BEFORE
    the most recent one — used for breakout-level detection (section 6).

    Deliberately excludes the current/last bar: a level computed from a
    window that includes the very candle being tested against it would
    make a genuine breakout of that candle's own high/low impossible.
    """
    prior = df.iloc[:-1] if len(df) > 1 else df.iloc[0:0]
    window = prior.tail(period)
    if window.empty:
        return {"range_high": None, "range_low": None}
    return {
        "range_high": float(window["high"].max()),
        "range_low": float(window["low"].min()),
    }


def breakout_levels(df: pd.DataFrame, period: int = 20, buffer_pct: float = 0.05) -> dict:
    """Breakout trigger levels: recent-range high/low offset by a
    configurable buffer (section 9: "Require a configurable breakout
    buffer")."""
    rng = recent_range(df, period)
    if rng["range_high"] is None:
        return {"breakout_up": None, "breakout_down": None}
    buffer = buffer_pct / 100.0
    return {
        "breakout_up": rng["range_high"] * (1 + buffer),
        "breakout_down": rng["range_low"] * (1 - buffer),
    }
