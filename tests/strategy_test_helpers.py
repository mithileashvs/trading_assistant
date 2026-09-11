"""Shared helpers for Phase 4 strategy/selector/scoring tests."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.features.engine import compute_features
from app.regimes.detector import RegimeDecision, Regime
from app.regimes.thresholds import RegimeThresholds
from app.strategies.context import StrategyContext


def synthetic_market_ohlcv(n=4000, seed=42, base=2650.0, freq="15min", start="2026-01-01", segment_bars=400):
    """A long, fully time-anchored (not wall-clock-anchored) synthetic
    M15 series with alternating trending/ranging segments, so a
    backtest run over it naturally exercises multiple regimes and
    strategies. Unlike MockMT5Client.get_ohlcv (which anchors to
    datetime.now() for live/dev use), this is 100% deterministic
    regardless of when the test runs -- important for backtest tests,
    where session-dependent scoring can otherwise flip trade outcomes
    between runs at different times of day."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")

    closes = np.empty(n)
    price = base
    i = 0
    segment_seed = seed
    while i < n:
        segment_seed += 1
        seg_rng = np.random.default_rng(segment_seed)
        length = min(segment_bars, n - i)
        kind = seg_rng.integers(0, 3)  # 0=uptrend, 1=downtrend, 2=range
        if kind == 0:
            drift = seg_rng.uniform(0.05, 0.25)
        elif kind == 1:
            drift = -seg_rng.uniform(0.05, 0.25)
        else:
            drift = 0.0
        noise_scale = seg_rng.uniform(0.15, 0.4)
        returns = drift + seg_rng.normal(0, noise_scale, length)
        segment_close = price + np.cumsum(returns)
        closes[i:i + length] = segment_close
        price = segment_close[-1]
        i += length

    close = pd.Series(closes, index=idx)
    open_ = close.shift(1)
    open_.iloc[0] = base
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.15, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.15, n))
    tick_volume = rng.integers(60, 400, n)
    spread = rng.integers(15, 30, n)
    return pd.DataFrame(
        {"open": open_.values, "high": high, "low": low, "close": close.values,
         "tick_volume": tick_volume, "spread": spread},
        index=idx,
    )


def flat_ohlcv(n=230, base=2000.0, wobble=0.05, seed=1, freq="15min"):
    """Very tight, directionless oscillation -- suitable for building
    the "consolidation" history before a synthetic breakout, and for
    keeping EMA200/ADX/BB-width all settled and low."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    close = base + rng.normal(0, wobble, n).cumsum() * 0.02  # tiny drift-free wobble
    close = base + (close - close.mean())  # recenter tightly around base
    open_ = np.roll(close, 1)
    open_[0] = base
    high = np.maximum(open_, close) + np.abs(rng.normal(0, wobble, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, wobble, n))
    tick_volume = rng.integers(80, 120, n)
    spread = rng.integers(15, 25, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "tick_volume": tick_volume, "spread": spread},
        index=idx,
    )


def trending_ohlcv(n=230, base=2000.0, bar_move=0.6, seed=2, bullish=True, freq="1h"):
    """A clean, steadily trending series (small consistent per-bar move,
    tiny noise) -- used to build confirmed TREND_BULLISH / TREND_BEARISH
    H4/H1 context deterministically."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    direction = 1.0 if bullish else -1.0
    drift = direction * bar_move
    close = base + np.cumsum(np.full(n, drift) + rng.normal(0, bar_move * 0.15, n))
    open_ = np.roll(close, 1)
    open_[0] = base
    noise = np.abs(rng.normal(0, bar_move * 0.2, n))
    high = np.maximum(open_, close) + noise
    low = np.minimum(open_, close) - noise
    tick_volume = rng.integers(80, 200, n)
    spread = rng.integers(15, 25, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "tick_volume": tick_volume, "spread": spread},
        index=idx,
    )


def append_pullback_and_rejection_bar(df: pd.DataFrame, bullish: bool, ema_col_period=20, bounce=0.5):
    """Append two M15 bars to `df`: a dip bar that brings price down to
    EMA20 (or up to it, for a bearish pullback), then a rejection bar
    that closes back in the trend's favor with its low/high still
    touching EMA20 — this rejection bar becomes the "current" bar the
    strategy evaluates, with the dip bar as its immediate predecessor
    (so MACD histogram improves bar-over-bar as price reverses off the
    level, matching how TrendPullbackStrategy reads the last two bars)."""
    from app.indicators.trend import ema

    ema20 = ema(df["close"], ema_col_period).iloc[-1]
    last_close = df["close"].iloc[-1]
    freq = df.index.freq or (df.index[-1] - df.index[-2])
    dip_time = df.index[-1] + freq
    rej_time = dip_time + freq

    if bullish:
        dip_open, dip_close = last_close, ema20
        dip_high, dip_low = max(dip_open, dip_close) + 0.05, min(dip_open, dip_close) - 0.05

        rej_open, rej_close = dip_close, ema20 + bounce
        rej_low = ema20 - 0.05  # brief wick back to the level before rallying
        rej_high = rej_close + 0.05
    else:
        dip_open, dip_close = last_close, ema20
        dip_high, dip_low = max(dip_open, dip_close) + 0.05, min(dip_open, dip_close) - 0.05

        rej_open, rej_close = dip_close, ema20 - bounce
        rej_high = ema20 + 0.05
        rej_low = rej_close - 0.05

    dip_bar = pd.DataFrame(
        {"open": [dip_open], "high": [dip_high], "low": [dip_low], "close": [dip_close],
         "tick_volume": [140], "spread": [20]},
        index=[dip_time],
    )
    rejection_bar = pd.DataFrame(
        {"open": [rej_open], "high": [rej_high], "low": [rej_low], "close": [rej_close],
         "tick_volume": [170], "spread": [20]},
        index=[rej_time],
    )
    return pd.concat([df, dip_bar, rejection_bar])


def trend_pullback_context(bullish: bool, bounce: float = 0.5, thresholds: RegimeThresholds | None = None) -> StrategyContext:
    """Full H4/H1/M15 context for a confirmed trend with a valid
    pullback + rejection + momentum-recovery setup on M15, ready to
    hand to TrendPullbackStrategy.generate()."""
    seed_offset = 0 if bullish else 10
    h4_df = trending_ohlcv(n=230, base=2000, bar_move=6.0, seed=10 + seed_offset, bullish=bullish, freq="4h")
    h1_df = trending_ohlcv(n=230, base=2000, bar_move=1.5, seed=11 + seed_offset, bullish=bullish, freq="1h")
    m15_base = trending_ohlcv(n=230, base=2000, bar_move=0.06, seed=12 + seed_offset, bullish=bullish, freq="15min")
    m15_df = append_pullback_and_rejection_bar(m15_base, bullish=bullish, bounce=bounce)
    return build_context_from_frames(h4_df, h1_df, m15_df, thresholds=thresholds)


def consolidation_then_breakout_ohlcv(n=230, base=2000.0, seed=5, freq="15min"):
    """A long, gently-noisy history whose FINAL `consolidation_bars`
    bars are tight relative to the settle phase (lower BB width) —
    realistic prelude to a breakout. Noise scale is chosen so ATR ends
    up in the same ballpark as the breakout buffer (0.05% of price),
    so a genuine breakout move doesn't also trip the "too extended"
    filter. Does not include the breakout bar itself; append one with
    `append_breakout_bar`."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    settle_bars = n - 20
    settle = base + np.cumsum(rng.normal(0, 0.15, settle_bars))
    settle = base + (settle - settle.mean())
    tight = base + rng.normal(0, 0.3, 20)
    close = np.concatenate([settle, tight])
    open_ = np.roll(close, 1)
    open_[0] = base
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.25, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.25, n))
    tick_volume = rng.integers(80, 120, n)
    spread = rng.integers(15, 25, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "tick_volume": tick_volume, "spread": spread},
        index=idx,
    )


def append_breakout_bar(df: pd.DataFrame, bullish: bool, move: float = 3.0, volume: int = 500):
    """Append one large-range, high-volume bar that breaks decisively
    beyond the recent consolidation range."""
    freq = df.index.freq or (df.index[-1] - df.index[-2])
    next_time = df.index[-1] + freq
    last_close = df["close"].iloc[-1]
    if bullish:
        open_, close_ = last_close, last_close + move
        high_, low_ = close_ + 0.2, last_close - 0.1
    else:
        open_, close_ = last_close, last_close - move
        high_, low_ = last_close + 0.1, close_ - 0.2
    bar = pd.DataFrame(
        {"open": [open_], "high": [high_], "low": [low_], "close": [close_],
         "tick_volume": [volume], "spread": [30]},
        index=[next_time],
    )
    return pd.concat([df, bar])


def breakout_context(bullish: bool, thresholds: RegimeThresholds | None = None) -> StrategyContext:
    """Full context with H4/H1 in a neutral RANGE-ish state (so it
    doesn't itself veto the breakout direction) and an M15 series that
    consolidates tightly then breaks out decisively."""
    h4_df = flat_ohlcv(n=230, base=2000.0, wobble=1.0, seed=100)
    h1_df = flat_ohlcv(n=230, base=2000.0, wobble=0.3, seed=101)
    m15_pre = consolidation_then_breakout_ohlcv(n=230, base=2000.0, seed=102)
    move = 2.8 if bullish else 2.75
    m15_df = append_breakout_bar(m15_pre, bullish=bullish, move=move)
    return build_context_from_frames(h4_df, h1_df, m15_df, thresholds=thresholds)


def append_mean_reversion_setup(df: pd.DataFrame, bullish: bool, decline_bars: int = 6, step: float = 0.6, reversal_fraction: float = 0.25):
    """Append a multi-bar decline (or rally) pushing price to an
    oversold/overbought extreme relative to its own recent Bollinger
    Bands, followed by one reversal-confirmation bar back toward the
    mean — the setup MeanReversionStrategy looks for."""
    freq = df.index.freq or (df.index[-1] - df.index[-2])
    cursor_time = df.index[-1]
    cursor_close = df["close"].iloc[-1]
    bars = []
    direction = -1.0 if bullish else 1.0  # bullish setup needs a DECLINE first
    for _ in range(decline_bars):
        cursor_time = cursor_time + freq
        open_i = cursor_close
        close_i = cursor_close + direction * step
        bars.append({
            "time": cursor_time, "open": open_i,
            "high": max(open_i, close_i) + 0.05, "low": min(open_i, close_i) - 0.05,
            "close": close_i, "tick_volume": 110, "spread": 20,
        })
        cursor_close = close_i

    # Reversal confirmation bar: closes back toward the mean, above the
    # prior bar's close (bullish) or below it (bearish).
    cursor_time = cursor_time + freq
    reversal_move = step * reversal_fraction
    open_r = cursor_close
    close_r = cursor_close - direction * reversal_move
    bars.append({
        "time": cursor_time, "open": open_r,
        "high": max(open_r, close_r) + 0.05, "low": min(open_r, close_r) - 0.05,
        "close": close_r, "tick_volume": 140, "spread": 20,
    })

    extra = pd.DataFrame(bars).set_index("time")
    extra.index.name = df.index.name
    return pd.concat([df, extra])


def mean_reversion_context(bullish: bool, thresholds: RegimeThresholds | None = None) -> StrategyContext:
    """Full context: H4/H1 flat enough to classify RANGE, M15 pushed to
    an oversold/overbought extreme with a reversal confirmation bar."""
    h4_df = flat_ohlcv(n=230, base=2000.0, wobble=1.0, seed=200)
    h1_df = flat_ohlcv(n=230, base=2000.0, wobble=0.5, seed=201)
    m15_base = flat_ohlcv(n=230, base=2000.0, wobble=0.3, seed=202)
    m15_df = append_mean_reversion_setup(m15_base, bullish=bullish)
    return build_context_from_frames(h4_df, h1_df, m15_df, thresholds=thresholds)


def build_context_from_frames(
    h4_df: pd.DataFrame,
    h1_df: pd.DataFrame,
    m15_df: pd.DataFrame,
    h4_regime_override: RegimeDecision | None = None,
    thresholds: RegimeThresholds | None = None,
) -> StrategyContext:
    from app.regimes.detector import detect_regime

    h4_features = compute_features(h4_df, "H4")
    h1_features = compute_features(h1_df, "H1")
    m15_features = compute_features(m15_df, "M15")

    h4_regime = h4_regime_override or detect_regime(h4_features, thresholds)

    return StrategyContext(
        symbol="XAUUSD",
        h4_df=h4_df,
        h1_df=h1_df,
        m15_df=m15_df,
        h4_features=h4_features,
        h1_features=h1_features,
        m15_features=m15_features,
        h4_regime=h4_regime,
        current_bid=float(m15_df["close"].iloc[-1]) - 0.1,
        current_ask=float(m15_df["close"].iloc[-1]) + 0.1,
    )
