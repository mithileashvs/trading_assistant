"""
Strategy 1 — Trend Pullback (section 8).

BUY: H4 confirmed bullish trend, H1 EMA20>EMA50 with structure not
bearish, M15 pullback to a dynamic level (EMA20/50) followed by a
bullish rejection candle and momentum recovery. SELL is the exact
logical inverse. Never buys merely because price touches an EMA —
rejection + momentum recovery are both mandatory.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.indicators import momentum as mom
from app.regimes.detector import Regime
from app.signals.models import Signal, SignalDirection, no_signal
from app.strategies.base import Strategy
from app.strategies.context import StrategyContext


class TrendPullbackConfig(BaseSettings):
    pullback_atr_multiplier: float = Field(1.5, gt=0, description="How close price must get to the dynamic level, in ATR.")
    min_rsi_buy: float = Field(40.0, ge=0, le=100)
    max_rsi_buy: float = Field(65.0, ge=0, le=100)
    min_rsi_sell: float = Field(35.0, ge=0, le=100)
    max_rsi_sell: float = Field(60.0, ge=0, le=100)
    stop_atr_multiplier: float = Field(1.5, gt=0)
    risk_reward: float = Field(2.0, gt=0)

    model_config = SettingsConfigDict(env_prefix="TREND_PULLBACK_", extra="ignore")


class TrendPullbackStrategy(Strategy):
    name = "TREND_PULLBACK"

    def __init__(self, config: TrendPullbackConfig | None = None):
        self.config = config or TrendPullbackConfig()

    def generate(self, context: StrategyContext) -> Signal:
        c = self.config

        h4_regime = context.h4_regime.regime
        if h4_regime not in (Regime.TREND_BULLISH, Regime.TREND_BEARISH):
            return no_signal(self.name, f"H4 regime is {h4_regime.value}, not a confirmed trend.")
        direction = SignalDirection.BUY if h4_regime == Regime.TREND_BULLISH else SignalDirection.SELL
        is_buy = direction == SignalDirection.BUY

        # --- H1: trend structure / setup quality --------------------------------
        h1_trend = context.h1_features["trend"]
        h1_ema20, h1_ema50 = h1_trend.get("ema_20"), h1_trend.get("ema_50")
        if h1_ema20 is None or h1_ema50 is None:
            return no_signal(self.name, "H1 EMA data unavailable.")
        h1_aligned = (h1_ema20 > h1_ema50) if is_buy else (h1_ema20 < h1_ema50)
        if not h1_aligned:
            return no_signal(self.name, "H1 EMA20/EMA50 does not confirm H4 trend direction.")

        h1_structure = h1_trend.get("structure")
        opposite = "BEARISH" if is_buy else "BULLISH"
        if h1_structure == opposite:
            return no_signal(self.name, f"H1 market structure ({h1_structure}) contradicts the trend direction.")

        # --- M15: pullback + rejection + momentum recovery ------------------------
        m15_df = context.m15_df
        m15_features = context.m15_features
        m15_trend = m15_features["trend"]
        m15_ema20, m15_ema50 = m15_trend.get("ema_20"), m15_trend.get("ema_50")
        atr_val = m15_features["volatility"].get("atr")
        if m15_ema20 is None or atr_val is None:
            return no_signal(self.name, "M15 EMA/ATR data unavailable.")

        buffer = atr_val * c.pullback_atr_multiplier
        last = m15_df.iloc[-1]
        prev = m15_df.iloc[-2]

        # A pullback "reaches" a dynamic level if the candle came within
        # `buffer` of EMA20 or EMA50 (not just touching exactly).
        dynamic_levels = [lvl for lvl in (m15_ema20, m15_ema50) if lvl is not None]
        if is_buy:
            reached = any(abs(last["low"] - lvl) <= buffer for lvl in dynamic_levels)
        else:
            reached = any(abs(last["high"] - lvl) <= buffer for lvl in dynamic_levels)
        if not reached:
            return no_signal(self.name, "M15 price has not pulled back to a dynamic level (EMA20/EMA50).")

        # Bullish/bearish rejection candle: body closes back in the
        # trend's favor, not merely touching the level.
        rejection = (last["close"] > last["open"]) if is_buy else (last["close"] < last["open"])
        if not rejection:
            return no_signal(self.name, "No rejection candle at the pullback level yet.")

        # Momentum recovery: RSI in a healthy (not extreme) range, and
        # MACD histogram improving vs the prior bar in the trend direction.
        rsi = m15_features["momentum"].get("rsi")
        macd_df = mom.macd(m15_df["close"])
        hist_now, hist_prev = macd_df["histogram"].iloc[-1], macd_df["histogram"].iloc[-2]
        if rsi is None or hist_now is None or hist_prev is None:
            return no_signal(self.name, "M15 momentum data unavailable.")

        if is_buy:
            rsi_ok = c.min_rsi_buy <= rsi <= c.max_rsi_buy
            momentum_recovering = hist_now > hist_prev
        else:
            rsi_ok = c.min_rsi_sell <= rsi <= c.max_rsi_sell
            momentum_recovering = hist_now < hist_prev

        if not rsi_ok:
            return no_signal(self.name, f"M15 RSI ({rsi:.1f}) outside the acceptable pullback-recovery range.")
        if not momentum_recovering:
            return no_signal(self.name, "M15 momentum is not yet recovering (MACD histogram not improving).")

        # --- build the signal -----------------------------------------------------
        entry = float(last["close"])
        stop = entry - atr_val * c.stop_atr_multiplier if is_buy else entry + atr_val * c.stop_atr_multiplier
        risk = abs(entry - stop)
        target = entry + risk * c.risk_reward if is_buy else entry - risk * c.risk_reward

        reasons = [
            f"H4 regime confirmed {h4_regime.value}",
            "H1 EMA20/EMA50 alignment confirms trend direction",
            "M15 pullback reached a dynamic support/resistance level (EMA20/EMA50)",
            "Rejection candle confirmed in the trend's favor",
            "MACD histogram improving (momentum recovering)",
            f"RSI ({rsi:.1f}) in a healthy, non-extreme range",
        ]

        confidence = 0.6
        if h1_structure == ("BULLISH" if is_buy else "BEARISH"):
            confidence += 0.15
            reasons.append("H1 market structure explicitly confirms trend direction")
        if context.h4_regime.confidence >= 0.75:
            confidence += 0.1
        confidence = min(confidence, 0.9)

        return Signal(
            direction=direction,
            strategy=self.name,
            reasons=reasons,
            confidence=confidence,
            entry=entry,
            stop_loss=stop,
            take_profit=target,
            invalidation=stop,
            meta={"entry_confirmed": True, "quality_confirmed": bool(rejection and momentum_recovering)},
        )
