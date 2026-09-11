"""
Market Regime Detector (section 7).

Deterministic, explainable, rule-based classification — no ML here by
design ("Do NOT immediately use an ML classifier. The first version
should be explainable."). Every decision returns a
{regime, confidence, reasons} object suitable for direct logging.

Input is a single-timeframe feature snapshot as produced by
app.features.engine.compute_features(). The higher-timeframe (H4) trend
snapshot is the intended input for the *primary* regime call in the
pipeline (see architecture diagram, section 1), but the function itself
is timeframe-agnostic so it can also be used to sanity-check H1/M15.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

from app.regimes.thresholds import RegimeThresholds, get_default_regime_thresholds


class Regime(str, enum.Enum):
    TREND_BULLISH = "TREND_BULLISH"
    TREND_BEARISH = "TREND_BEARISH"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNCERTAIN = "UNCERTAIN"


@dataclass
class RegimeDecision:
    regime: Regime
    confidence: float
    reasons: list[str] = field(default_factory=list)
    timeframe: str | None = None

    def to_dict(self) -> dict:
        return {
            "regime": self.regime.value,
            "confidence": round(self.confidence, 2),
            "reasons": self.reasons,
            "timeframe": self.timeframe,
        }


def _volatility_flags(features: dict, t: RegimeThresholds) -> tuple[list[str], int]:
    volatility = features["volatility"]
    reasons = []
    count = 0
    atr_pct = volatility.get("atr_pct")
    bb_width = volatility.get("bb_width_pct")
    range_exp = volatility.get("range_expansion_ratio")

    if atr_pct is not None and atr_pct > t.high_vol_atr_pct_threshold:
        reasons.append(f"ATR% ({atr_pct:.2f}) above high-volatility threshold ({t.high_vol_atr_pct_threshold})")
        count += 1
    if bb_width is not None and bb_width > t.high_vol_bb_width_pct_threshold:
        reasons.append(f"Bollinger Band width ({bb_width:.2f}%) above high-volatility threshold ({t.high_vol_bb_width_pct_threshold}%)")
        count += 1
    if range_exp is not None and range_exp > t.high_vol_range_expansion_threshold:
        reasons.append(f"Range expansion ratio ({range_exp:.2f}) above threshold ({t.high_vol_range_expansion_threshold})")
        count += 1
    return reasons, count


def detect_regime(
    features: dict, thresholds: RegimeThresholds | None = None
) -> RegimeDecision:
    """Classify the market regime from one feature snapshot.

    features must be the dict returned by
    app.features.engine.compute_features().
    """
    t = thresholds or get_default_regime_thresholds()

    trend = features["trend"]
    momentum = features["momentum"]
    timeframe = features.get("timeframe")

    adx = trend.get("adx")
    plus_di = trend.get("plus_di")
    minus_di = trend.get("minus_di")
    bullish_aligned = trend.get("bullish_aligned", False)
    bearish_aligned = trend.get("bearish_aligned", False)
    structure = trend.get("structure")

    vol_reasons, vol_trigger_count = _volatility_flags(features, t)

    # --- 1. Trend check (mandatory conditions must ALL hold) -----------------
    trend_ok = adx is not None and plus_di is not None and minus_di is not None
    di_diff = (plus_di - minus_di) if trend_ok else None

    bullish_trend = (
        trend_ok
        and bullish_aligned
        and adx > t.trend_adx_threshold
        and di_diff > t.trend_di_separation
    )
    bearish_trend = (
        trend_ok
        and bearish_aligned
        and adx > t.trend_adx_threshold
        and (-di_diff) > t.trend_di_separation
    )

    if bullish_trend or bearish_trend:
        direction = "BULLISH" if bullish_trend else "BEARISH"
        reasons = [
            f"EMA alignment is {direction.lower()} (fast > mid > slow)" if direction == "BULLISH"
            else "EMA alignment is bearish (fast < mid < slow)",
            f"ADX ({adx:.1f}) above trend threshold ({t.trend_adx_threshold})",
            f"+DI/-DI separation confirms {direction.lower()} direction "
            f"(+DI={plus_di:.1f}, -DI={minus_di:.1f})",
        ]
        confidence = 0.6

        if structure == direction:
            reasons.append(f"Market structure confirms {direction.lower()} sequence (HH/HL or LH/LL)")
            confidence += 0.1

        macd_hist = momentum.get("macd_histogram")
        rsi = momentum.get("rsi")
        momentum_confirms = (
            (direction == "BULLISH" and macd_hist is not None and macd_hist > 0 and rsi is not None and rsi > 50)
            or (direction == "BEARISH" and macd_hist is not None and macd_hist < 0 and rsi is not None and rsi < 50)
        )
        if momentum_confirms:
            reasons.append("Momentum (MACD histogram / RSI) confirms trend direction")
            confidence += 0.1

        if vol_trigger_count == 0:
            reasons.append("Volatility within acceptable range for a clean trend read")
            confidence += 0.1
        elif vol_trigger_count >= 2:
            reasons.append("Caution: elevated volatility alongside the trend signal")
            confidence -= 0.1

        confidence = max(0.5, min(confidence, 0.95))
        regime = Regime.TREND_BULLISH if direction == "BULLISH" else Regime.TREND_BEARISH
        return RegimeDecision(regime=regime, confidence=confidence, reasons=reasons, timeframe=timeframe)

    # --- 2. High volatility check (only reached if not a confirmed trend) ------
    if vol_trigger_count >= 1:
        confidence = 0.4 + 0.15 * vol_trigger_count  # 1 trigger -> 0.55, 3 -> 0.85
        confidence = min(confidence, 0.9)
        reasons = list(vol_reasons)
        if adx is not None and adx <= t.trend_adx_threshold:
            reasons.append(f"ADX ({adx:.1f}) does not confirm a sustained directional trend")
        return RegimeDecision(
            regime=Regime.HIGH_VOLATILITY, confidence=confidence, reasons=reasons, timeframe=timeframe
        )

    # --- 3. Range check ---------------------------------------------------------
    bb_width = features["volatility"].get("bb_width_pct")
    range_ok = (
        adx is not None
        and adx < t.range_adx_threshold
        and bb_width is not None
        and bb_width < t.range_bb_width_pct_threshold
    )
    if range_ok:
        reasons = [
            f"ADX ({adx:.1f}) below range threshold ({t.range_adx_threshold})",
            f"Bollinger Band width ({bb_width:.2f}%) below range threshold ({t.range_bb_width_pct_threshold}%)",
        ]
        confidence = 0.55
        if structure == "SIDEWAYS":
            reasons.append("Market structure confirms sideways/no clear HH-HL or LH-LL sequence")
            confidence += 0.15
        confidence = min(confidence, 0.85)
        return RegimeDecision(regime=Regime.RANGE, confidence=confidence, reasons=reasons, timeframe=timeframe)

    # --- 4. Uncertain (fallback — NO TRADE is a valid decision, section 11) ----
    reasons = ["No trend, high-volatility, or range conditions were decisively met."]
    if adx is not None:
        reasons.append(f"ADX ({adx:.1f}) is between range and trend thresholds — ambiguous.")
    return RegimeDecision(regime=Regime.UNCERTAIN, confidence=0.3, reasons=reasons, timeframe=timeframe)
