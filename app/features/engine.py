"""
Feature Engine (section 6).

Takes an OHLCV DataFrame for a single timeframe and computes the full
feature set the regime detector and strategies will consume. Returns a
plain dict (JSON-serializable) rather than a bespoke class hierarchy,
matching the explainable, loggable style used elsewhere (trade
validator object, regime decision object).

This module does not know about MT5, timeframes-as-a-concept beyond a
label, or trading decisions — it is a pure function of the DataFrame
it's given, which keeps it trivially unit-testable and reusable across
H4/H1/M15 (section 5).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from app.indicators import momentum as mom
from app.indicators import structure as struct
from app.indicators import trend as trend_ind
from app.indicators import volatility as vol
from app.indicators import volume as vol_ind
from app.features.sessions import session_label

MIN_BARS_REQUIRED = 210  # enough for EMA200 to be defined


class InsufficientDataError(ValueError):
    pass


def compute_features(
    df: pd.DataFrame,
    timeframe: str,
    ema_periods: tuple[int, int, int] = (20, 50, 200),
    rsi_period: int = 14,
    atr_period: int = 14,
    adx_period: int = 14,
    bb_period: int = 20,
    bb_std: float = 2.0,
    swing_lookback: int = 3,
    range_period: int = 20,
    volume_period: int = 20,
) -> dict:
    """Compute the full feature snapshot for the latest bar of `df`.

    df must have columns: open, high, low, close, tick_volume, spread,
    and a tz-aware DatetimeIndex named 'time' (as returned by
    MarketDataEngine.get_ohlcv). Raises InsufficientDataError if there
    isn't enough history for the slowest indicator (EMA200 by default).
    """
    if len(df) < MIN_BARS_REQUIRED:
        raise InsufficientDataError(
            f"Need at least {MIN_BARS_REQUIRED} bars for a full feature "
            f"snapshot (got {len(df)}). Fetch more history."
        )

    close, high, low = df["close"], df["high"], df["low"]
    fast, mid, slow = ema_periods

    # --- trend -----------------------------------------------------------
    ema_align = trend_ind.ema_alignment(close, fast, mid, slow)
    sma_slow = trend_ind.sma(close, slow).iloc[-1]
    adx_df = trend_ind.adx(df, adx_period)
    market_structure = struct.classify_market_structure(df, swing_lookback)

    # --- momentum -----------------------------------------------------------
    rsi_val = mom.rsi(close, rsi_period).iloc[-1]
    macd_df = mom.macd(close)
    roc_val = mom.rate_of_change(close).iloc[-1]
    momentum_val = mom.momentum(close).iloc[-1]

    # --- volatility -----------------------------------------------------------
    atr_val = vol.atr(df, atr_period).iloc[-1]
    atr_pct_val = vol.atr_percent(df, atr_period).iloc[-1]
    bb = vol.bollinger_bands(close, bb_period, bb_std)
    bb_width_val = vol.bollinger_band_width(close, bb_period, bb_std).iloc[-1]
    hist_vol_val = vol.historical_volatility(close, bb_period).iloc[-1]
    candle_range_val = vol.candle_range(df).iloc[-1]
    range_exp_val = vol.range_expansion(df, range_period).iloc[-1]

    # --- volume (MT5 tick volume, not centralized-exchange volume) -----------
    tick_volume = df["tick_volume"]
    rel_vol_val = vol_ind.relative_volume(tick_volume, volume_period).iloc[-1]
    vol_ma_val = vol_ind.volume_moving_average(tick_volume, volume_period).iloc[-1]
    vol_expansion_val = bool(vol_ind.volume_expansion(tick_volume, volume_period).iloc[-1])

    # --- price structure -----------------------------------------------------
    sr = struct.support_resistance_levels(df, swing_lookback)
    rng = struct.recent_range(df, range_period)
    breakout = struct.breakout_levels(df, range_period)
    prev_day = struct.previous_period_high_low(df, freq="D")

    latest_ts: pd.Timestamp = df.index[-1]
    latest_dt = latest_ts.to_pydatetime()
    if latest_dt.tzinfo is None:
        latest_dt = latest_dt.replace(tzinfo=timezone.utc)

    return {
        "timeframe": timeframe,
        "as_of": latest_dt.isoformat(),
        "session": session_label(latest_dt),
        "close": float(close.iloc[-1]),
        "trend": {
            **{
                k: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                for k, v in ema_align.items()
            },
            f"sma_{slow}": float(sma_slow) if pd.notna(sma_slow) else None,
            "adx": _safe_float(adx_df["adx"].iloc[-1]),
            "plus_di": _safe_float(adx_df["plus_di"].iloc[-1]),
            "minus_di": _safe_float(adx_df["minus_di"].iloc[-1]),
            "structure": market_structure.label,
            "structure_reasons": market_structure.reasons,
            "recent_swing_highs": market_structure.last_swing_highs,
            "recent_swing_lows": market_structure.last_swing_lows,
        },
        "momentum": {
            "rsi": _safe_float(rsi_val),
            "macd": _safe_float(macd_df["macd"].iloc[-1]),
            "macd_signal": _safe_float(macd_df["signal"].iloc[-1]),
            "macd_histogram": _safe_float(macd_df["histogram"].iloc[-1]),
            "roc": _safe_float(roc_val),
            "momentum": _safe_float(momentum_val),
        },
        "volatility": {
            "atr": _safe_float(atr_val),
            "atr_pct": _safe_float(atr_pct_val),
            "bb_upper": _safe_float(bb["upper"].iloc[-1]),
            "bb_middle": _safe_float(bb["middle"].iloc[-1]),
            "bb_lower": _safe_float(bb["lower"].iloc[-1]),
            "bb_width_pct": _safe_float(bb_width_val),
            "historical_volatility_pct": _safe_float(hist_vol_val),
            "candle_range": _safe_float(candle_range_val),
            "range_expansion_ratio": _safe_float(range_exp_val),
        },
        "volume": {
            "tick_volume": float(tick_volume.iloc[-1]),
            "tick_volume_ma": _safe_float(vol_ma_val),
            "relative_volume": _safe_float(rel_vol_val),
            "volume_expansion": vol_expansion_val,
        },
        "price_structure": {
            "support_levels": sr["support"],
            "resistance_levels": sr["resistance"],
            "range_high": rng["range_high"],
            "range_low": rng["range_low"],
            "breakout_up": breakout["breakout_up"],
            "breakout_down": breakout["breakout_down"],
            "previous_day_high": prev_day["previous_high"],
            "previous_day_low": prev_day["previous_low"],
        },
    }


def _safe_float(v) -> Optional[float]:
    if v is None or pd.isna(v):
        return None
    return float(v)
