"""
Strategy 2 — Volatility Breakout (section 9).

Consolidation -> range compression -> volatility expansion -> range
breakout -> confirmation. Avoids entering after extremely extended
moves (distance-from-EMA20 filter) and requires a configurable
breakout buffer (already baked into the M15 feature snapshot's
breakout_up/breakout_down levels — see app.indicators.structure).
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.indicators import volatility as vol
from app.regimes.detector import Regime
from app.signals.models import Signal, SignalDirection, no_signal
from app.strategies.base import Strategy
from app.strategies.context import StrategyContext


class BreakoutConfig(BaseSettings):
    consolidation_lookback_bars: int = Field(20, gt=0)
    consolidation_bb_width_threshold: float = Field(0.6, gt=0, description="BB width %% that counts as 'was consolidating'.")
    expansion_ratio_threshold: float = Field(1.4, gt=0)
    min_relative_volume: float = Field(1.3, gt=0)
    max_extension_atr: float = Field(3.0, gt=0, description="Reject breakouts already this many ATRs past EMA20.")
    stop_atr_multiplier: float = Field(1.5, gt=0)
    risk_reward: float = Field(1.8, gt=0)

    model_config = SettingsConfigDict(env_prefix="BREAKOUT_", extra="ignore")


class BreakoutStrategy(Strategy):
    name = "BREAKOUT"

    def __init__(self, config: BreakoutConfig | None = None):
        self.config = config or BreakoutConfig()

    def generate(self, context: StrategyContext) -> Signal:
        c = self.config
        m15_features = context.m15_features
        m15_df = context.m15_df
        price_structure = m15_features["price_structure"]

        breakout_up = price_structure.get("breakout_up")
        breakout_down = price_structure.get("breakout_down")
        last_close = m15_features["close"]
        atr_val = m15_features["volatility"].get("atr")
        ema20 = m15_features["trend"].get("ema_20")

        if breakout_up is None or breakout_down is None or atr_val is None:
            return no_signal(self.name, "Insufficient M15 price-structure/volatility data.")

        if last_close > breakout_up:
            direction = SignalDirection.BUY
        elif last_close < breakout_down:
            direction = SignalDirection.SELL
        else:
            return no_signal(self.name, "Price has not broken beyond the recent range plus buffer.")
        is_buy = direction == SignalDirection.BUY

        h4_regime = context.h4_regime.regime
        if is_buy and h4_regime == Regime.TREND_BEARISH:
            return no_signal(self.name, "H4 is confirmed bearish; skipping counter-trend breakout.")
        if not is_buy and h4_regime == Regime.TREND_BULLISH:
            return no_signal(self.name, "H4 is confirmed bullish; skipping counter-trend breakout.")

        # --- consolidation -> expansion sequence -----------------------------------
        bb_width_series = vol.bollinger_band_width(m15_df["close"])
        lookback_window = bb_width_series.iloc[-(c.consolidation_lookback_bars + 1):-1]
        recent_min_width = lookback_window.min() if not lookback_window.empty else None
        consolidation_detected = (
            recent_min_width is not None and recent_min_width < c.consolidation_bb_width_threshold
        )
        if not consolidation_detected:
            return no_signal(self.name, "No prior consolidation/range compression detected before this move.")

        range_exp_val = m15_features["volatility"].get("range_expansion_ratio")
        expansion_ok = range_exp_val is not None and range_exp_val > c.expansion_ratio_threshold
        if not expansion_ok:
            return no_signal(self.name, "Breakout candle lacks volatility-expansion confirmation.")

        rel_vol = m15_features["volume"].get("relative_volume") or 0
        volume_confirm = m15_features["volume"].get("volume_expansion") or rel_vol > c.min_relative_volume
        if not volume_confirm:
            return no_signal(self.name, "Breakout lacks tick-volume confirmation.")

        if ema20 is not None and atr_val:
            distance_atr = abs(last_close - ema20) / atr_val
            if distance_atr > c.max_extension_atr:
                return no_signal(self.name, "Price is too extended from EMA20 to chase this breakout safely.")

        # --- build the signal -----------------------------------------------------
        entry = float(last_close)
        range_low = price_structure.get("range_low")
        range_high = price_structure.get("range_high")
        if is_buy:
            stop = range_low if (range_low is not None and range_low < entry) else entry - atr_val * c.stop_atr_multiplier
        else:
            stop = range_high if (range_high is not None and range_high > entry) else entry + atr_val * c.stop_atr_multiplier

        risk = abs(entry - stop)
        target = entry + risk * c.risk_reward if is_buy else entry - risk * c.risk_reward

        reasons = [
            "Price broke beyond the recent range plus configured buffer",
            "Prior consolidation (tight Bollinger Band width) confirmed before the move",
            "Volatility expansion confirmed (range-expansion ratio above threshold)",
            "Tick-volume expansion confirms the breakout",
            f"H4 regime ({h4_regime.value}) does not contradict breakout direction",
        ]

        confidence = 0.55
        reduced_risk = False
        if (is_buy and h4_regime == Regime.TREND_BULLISH) or (not is_buy and h4_regime == Regime.TREND_BEARISH):
            confidence += 0.15
            reasons.append("H4 trend direction reinforces the breakout")
        if h4_regime == Regime.HIGH_VOLATILITY:
            # Section 11: HIGH_VOLATILITY -> "optionally reduced-risk breakout".
            confidence -= 0.1
            reduced_risk = True
            reasons.append("H4 regime is HIGH_VOLATILITY — treating as reduced-risk breakout")
        confidence = max(0.3, min(confidence, 0.9))

        return Signal(
            direction=direction,
            strategy=self.name,
            reasons=reasons,
            confidence=confidence,
            entry=entry,
            stop_loss=stop,
            take_profit=target,
            invalidation=stop,
            meta={
                "entry_confirmed": bool(volume_confirm),
                "quality_confirmed": bool(expansion_ok and consolidation_detected),
                "reduced_risk": reduced_risk,
            },
        )
