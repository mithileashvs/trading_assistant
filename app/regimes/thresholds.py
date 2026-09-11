"""
Regime detector thresholds (section 7).

"The thresholds must be configurable" — these are not sacred constants,
they're pydantic Settings sourced from environment variables (prefix
REGIME_), with the values below as development defaults only.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RegimeThresholds(BaseSettings):
    # --- trend detection --------------------------------------------------
    trend_adx_threshold: float = Field(
        20.0, gt=0, description="ADX above this suggests a real trend is present."
    )
    trend_di_separation: float = Field(
        2.0, ge=0, description="Minimum +DI/-DI separation to confirm trend direction."
    )

    # --- range detection ------------------------------------------------------
    range_adx_threshold: float = Field(
        18.0, gt=0, description="ADX below this suggests no sustained trend (range candidate)."
    )
    range_bb_width_pct_threshold: float = Field(
        0.6, gt=0, description="Bollinger Band width (% of price) below this suggests compression/range."
    )

    # --- high volatility detection ------------------------------------------
    high_vol_atr_pct_threshold: float = Field(
        0.35, gt=0, description="ATR as % of price above this is classified high volatility."
    )
    high_vol_bb_width_pct_threshold: float = Field(
        1.2, gt=0, description="Bollinger Band width (% of price) above this is high volatility."
    )
    high_vol_range_expansion_threshold: float = Field(
        1.8, gt=0, description="Candle range vs its recent average above this signals expansion."
    )

    model_config = SettingsConfigDict(env_prefix="REGIME_", extra="ignore")


def get_default_regime_thresholds() -> RegimeThresholds:
    return RegimeThresholds()
