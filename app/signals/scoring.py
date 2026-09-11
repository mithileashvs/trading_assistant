"""
Signal Scoring (section 12).

A transparent, additive scoring system. The weights and interpretation
breakpoints below are development defaults, not sacred constants —
"Make them configurable and test them statistically" (section 12).

Scoring is deliberately generic across strategies: it reads the
StrategyContext (H4/H1/M15 features, regime) plus a few booleans each
strategy sets on Signal.meta (entry_confirmed, quality_confirmed), so
the scorer doesn't need strategy-specific logic. A high score does NOT
by itself approve a trade — the risk engine (Phase 5) still must
approve it (section 12).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.regimes.detector import Regime
from app.signals.models import Signal, SignalDirection

if TYPE_CHECKING:
    # Avoids a runtime circular import: app.strategies imports
    # app.signals.scoring (via the selector), so this module must not
    # import app.strategies at module load time. `from __future__ import
    # annotations` above means this is only needed for type checkers.
    from app.strategies.context import StrategyContext


class ScoringWeights(BaseSettings):
    h4_trend_alignment: int = Field(2, ge=0)
    h1_trend_alignment: int = Field(2, ge=0)
    m15_momentum: int = Field(1, ge=0)
    entry_confirmation: int = Field(2, ge=0)
    setup_quality: int = Field(2, ge=0)
    volatility_suitable: int = Field(1, ge=0)
    market_context: int = Field(1, ge=0)

    # Interpretation breakpoints (section 12): score >= threshold -> label.
    # Checked from highest to lowest.
    strong_threshold: int = Field(9, ge=0)
    valid_threshold: int = Field(7, ge=0)
    weak_threshold: int = Field(5, ge=0)

    # Volatility suitability band, expressed as ATR% of price.
    min_atr_pct: float = Field(0.03, ge=0)
    max_atr_pct: float = Field(0.6, ge=0)

    model_config = SettingsConfigDict(env_prefix="SCORE_", extra="ignore")

    @property
    def max_score(self) -> int:
        return (
            self.h4_trend_alignment
            + self.h1_trend_alignment
            + self.m15_momentum
            + self.entry_confirmation
            + self.setup_quality
            + self.volatility_suitable
            + self.market_context
        )


def classify_score(score: int, weights: ScoringWeights) -> str:
    if score >= weights.strong_threshold:
        return "STRONG"
    if score >= weights.valid_threshold:
        return "VALID"
    if score >= weights.weak_threshold:
        return "WEAK"
    return "NO_TRADE"


def score_signal(
    signal: Signal, context: StrategyContext, weights: ScoringWeights | None = None
) -> Signal:
    """Mutates and returns `signal` with score/score_breakdown/score_label
    filled in. NO_SIGNAL signals are returned unscored (score stays None)
    since there's nothing to score."""
    if signal.direction == SignalDirection.NO_SIGNAL:
        return signal

    w = weights or ScoringWeights()
    breakdown: dict[str, int] = {}
    is_buy = signal.direction == SignalDirection.BUY

    # --- H4 trend alignment ---------------------------------------------
    h4_regime = context.h4_regime.regime
    if (is_buy and h4_regime == Regime.TREND_BULLISH) or (
        not is_buy and h4_regime == Regime.TREND_BEARISH
    ):
        breakdown["h4_trend_alignment"] = w.h4_trend_alignment
    elif h4_regime == Regime.RANGE:
        # Partial credit: a range-bound H4 is the correct context for
        # mean reversion, just not "trend alignment" in the strict sense.
        breakdown["h4_trend_alignment"] = w.h4_trend_alignment // 2
    else:
        breakdown["h4_trend_alignment"] = 0

    # --- H1 trend alignment -----------------------------------------------
    h1_trend = context.h1_features["trend"]
    h1_ema20, h1_ema50 = h1_trend.get("ema_20"), h1_trend.get("ema_50")
    h1_aligned = (
        h1_ema20 is not None
        and h1_ema50 is not None
        and ((is_buy and h1_ema20 > h1_ema50) or (not is_buy and h1_ema20 < h1_ema50))
    )
    breakdown["h1_trend_alignment"] = w.h1_trend_alignment if h1_aligned else 0

    # --- M15 momentum ---------------------------------------------------------
    m15_mom = context.m15_features["momentum"]
    rsi, macd_hist = m15_mom.get("rsi"), m15_mom.get("macd_histogram")
    momentum_ok = (
        rsi is not None
        and macd_hist is not None
        and ((is_buy and rsi >= 45 and macd_hist >= 0) or (not is_buy and rsi <= 55 and macd_hist <= 0))
    )
    breakdown["m15_momentum"] = w.m15_momentum if momentum_ok else 0

    # --- entry confirmation / setup quality (strategy-supplied flags) ------------
    breakdown["entry_confirmation"] = (
        w.entry_confirmation if signal.meta.get("entry_confirmed") else 0
    )
    breakdown["setup_quality"] = (
        w.setup_quality if signal.meta.get("quality_confirmed") else 0
    )

    # --- volatility suitable ----------------------------------------------------
    atr_pct = context.m15_features["volatility"].get("atr_pct")
    vol_ok = atr_pct is not None and w.min_atr_pct <= atr_pct <= w.max_atr_pct
    breakdown["volatility_suitable"] = w.volatility_suitable if vol_ok else 0

    # --- market context (session) -------------------------------------------------
    market_ok = context.m15_features.get("session") not in (None, "OFF_HOURS")
    breakdown["market_context"] = w.market_context if market_ok else 0

    total = sum(breakdown.values())
    signal.score = total
    signal.score_breakdown = breakdown
    signal.score_label = classify_score(total, w)
    return signal
