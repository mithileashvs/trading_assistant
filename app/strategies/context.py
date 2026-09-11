"""
Strategy context (sections 5, 8-10): the multi-timeframe bundle every
strategy receives. H4 determines broad regime, H1 determines trend
structure/setup quality, M15 finds the actual entry — strategies must
never use M15 signals independently of the higher-timeframe context
(section 5), which is why this bundle always carries all three.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.market_data.engine import MarketDataEngine
from app.regimes.detector import RegimeDecision, detect_regime
from app.regimes.thresholds import RegimeThresholds
from app.features.engine import compute_features


@dataclass
class StrategyContext:
    symbol: str
    h4_df: pd.DataFrame
    h1_df: pd.DataFrame
    m15_df: pd.DataFrame
    h4_features: dict
    h1_features: dict
    m15_features: dict
    h4_regime: RegimeDecision
    current_bid: float | None = None
    current_ask: float | None = None


def build_context(
    engine: MarketDataEngine,
    thresholds: RegimeThresholds | None = None,
    bars: int = 250,
) -> StrategyContext:
    """Fetch H4/H1/M15 data, compute features, and classify the H4
    regime — the standard context every strategy call needs."""
    symbol = engine.resolve_symbol().name

    h4_df = engine.get_ohlcv("H4", bars)
    h1_df = engine.get_ohlcv("H1", bars)
    m15_df = engine.get_ohlcv("M15", bars)

    h4_features = compute_features(h4_df, "H4")
    h1_features = compute_features(h1_df, "H1")
    m15_features = compute_features(m15_df, "M15")

    h4_regime = detect_regime(h4_features, thresholds)

    tick = engine.get_tick()

    return StrategyContext(
        symbol=symbol,
        h4_df=h4_df,
        h1_df=h1_df,
        m15_df=m15_df,
        h4_features=h4_features,
        h1_features=h1_features,
        m15_features=m15_features,
        h4_regime=h4_regime,
        current_bid=tick.bid,
        current_ask=tick.ask,
    )
