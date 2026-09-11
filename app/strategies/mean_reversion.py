"""
Strategy 3 — Mean Reversion (section 10).

Only runs when H4 regime is RANGE — never blindly buys oversold
conditions during a strong bearish trend (or sells overbought during a
strong bullish trend). Disabled outright if H1 shows the market has
started trending strongly, even if H4 hasn't reclassified yet.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.regimes.detector import Regime
from app.signals.models import Signal, SignalDirection, no_signal
from app.strategies.base import Strategy
from app.strategies.context import StrategyContext


class MeanReversionConfig(BaseSettings):
    oversold_rsi: float = Field(32.0, ge=0, le=100)
    overbought_rsi: float = Field(68.0, ge=0, le=100)
    min_deviation_atr: float = Field(1.0, gt=0, description="Minimum distance from the mean (in ATR) to consider a reversion trade.")
    disable_if_h1_adx_above: float = Field(28.0, gt=0)
    stop_atr_buffer: float = Field(0.5, gt=0)
    target_at_mean: bool = Field(True, description="If true, target the middle Bollinger Band (the mean) rather than a fixed R multiple.")
    risk_reward_fallback: float = Field(1.5, gt=0)

    model_config = SettingsConfigDict(env_prefix="MEAN_REVERSION_", extra="ignore")


class MeanReversionStrategy(Strategy):
    name = "MEAN_REVERSION"

    def __init__(self, config: MeanReversionConfig | None = None):
        self.config = config or MeanReversionConfig()

    def generate(self, context: StrategyContext) -> Signal:
        c = self.config

        h4_regime = context.h4_regime.regime
        if h4_regime != Regime.RANGE:
            return no_signal(self.name, f"H4 regime is {h4_regime.value}, not RANGE — mean reversion disabled.")

        h1_adx = context.h1_features["trend"].get("adx")
        if h1_adx is not None and h1_adx > c.disable_if_h1_adx_above:
            return no_signal(
                self.name,
                f"H1 ADX ({h1_adx:.1f}) suggests the market has started trending strongly; disabling mean reversion.",
            )

        m15_features = context.m15_features
        m15_df = context.m15_df
        vol_data = m15_features["volatility"]
        rsi = m15_features["momentum"].get("rsi")
        close = m15_features["close"]
        bb_lower, bb_upper, bb_middle = vol_data.get("bb_lower"), vol_data.get("bb_upper"), vol_data.get("bb_middle")
        atr_val = vol_data.get("atr")

        if None in (rsi, bb_lower, bb_upper, bb_middle, atr_val) or atr_val == 0:
            return no_signal(self.name, "Insufficient M15 Bollinger/RSI/ATR data.")

        last = m15_df.iloc[-1]
        prev = m15_df.iloc[-2]

        deviation_atr = abs(close - bb_middle) / atr_val

        is_buy_candidate = close <= bb_lower and rsi <= c.oversold_rsi
        is_sell_candidate = close >= bb_upper and rsi >= c.overbought_rsi

        if is_buy_candidate:
            direction = SignalDirection.BUY
        elif is_sell_candidate:
            direction = SignalDirection.SELL
        else:
            return no_signal(self.name, "Price/RSI does not show a large enough deviation from the mean yet.")
        is_buy = direction == SignalDirection.BUY

        if deviation_atr < c.min_deviation_atr:
            return no_signal(self.name, f"Deviation from mean ({deviation_atr:.2f} ATR) is below the minimum required.")

        # Reversal confirmation: a candle closing back toward the mean.
        reversal_confirmed = (last["close"] > last["open"] and last["close"] > prev["close"]) if is_buy else (
            last["close"] < last["open"] and last["close"] < prev["close"]
        )
        if not reversal_confirmed:
            return no_signal(self.name, "No reversal confirmation candle yet at the range boundary.")

        # Range-boundary proximity: use the range low/high from price structure.
        range_low = m15_features["price_structure"].get("range_low")
        range_high = m15_features["price_structure"].get("range_high")

        entry = float(close)
        if is_buy:
            boundary = range_low if (range_low is not None and range_low <= entry) else bb_lower
            stop = boundary - atr_val * c.stop_atr_buffer
        else:
            boundary = range_high if (range_high is not None and range_high >= entry) else bb_upper
            stop = boundary + atr_val * c.stop_atr_buffer

        if c.target_at_mean:
            target = bb_middle
        else:
            risk = abs(entry - stop)
            target = entry + risk * c.risk_reward_fallback if is_buy else entry - risk * c.risk_reward_fallback

        reasons = [
            f"H4 regime confirmed RANGE (H1 ADX {h1_adx:.1f} does not contradict this)",
            f"Price at/beyond the {'lower' if is_buy else 'upper'} Bollinger Band",
            f"RSI ({rsi:.1f}) confirms {'oversold' if is_buy else 'overbought'} condition",
            f"Deviation from mean ({deviation_atr:.2f} ATR) exceeds the minimum threshold",
            "Reversal confirmation candle closed back toward the mean",
        ]

        confidence = 0.55
        if range_low is not None and range_high is not None:
            confidence += 0.1
            reasons.append("Price is at a defined range boundary, not just a Bollinger extreme")
        if deviation_atr >= c.min_deviation_atr * 1.5:
            confidence += 0.1
        confidence = min(confidence, 0.85)

        return Signal(
            direction=direction,
            strategy=self.name,
            reasons=reasons,
            confidence=confidence,
            entry=entry,
            stop_loss=stop,
            take_profit=target,
            invalidation=stop,
            meta={"entry_confirmed": bool(reversal_confirmed), "quality_confirmed": bool(deviation_atr >= c.min_deviation_atr)},
        )
