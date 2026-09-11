"""
Strategy Comparison (section 29).

Runs the backtest engine with only ONE strategy enabled at a time
(via StrategySelector's enabled-map) over the same dataset, so their
metrics are directly comparable. Only strategies demonstrating robust
performance should be promoted toward paper/live testing (section 29)
— this module produces the comparison, not the promotion decision.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.backtesting.engine import BacktestConfig, BacktestEngine
from app.backtesting.metrics import compute_metrics
from app.backtesting.results import BacktestResult
from app.config.settings import RiskSettings
from app.mt5.interface import SymbolSpec
from app.strategies.breakout import BreakoutStrategy
from app.strategies.mean_reversion import MeanReversionStrategy
from app.strategies.selector import StrategySelector
from app.strategies.trend_pullback import TrendPullbackStrategy

ALL_STRATEGY_NAMES = ("TREND_PULLBACK", "BREAKOUT", "MEAN_REVERSION")


@dataclass
class StrategyComparisonEntry:
    strategy_name: str
    result: BacktestResult
    metrics: dict


def compare_strategies(
    symbol_spec: SymbolSpec,
    risk_settings: RiskSettings,
    m15_df: pd.DataFrame,
    strategy_names: tuple[str, ...] = ALL_STRATEGY_NAMES,
    config: BacktestConfig | None = None,
) -> dict[str, StrategyComparisonEntry]:
    strategies = {
        "TREND_PULLBACK": TrendPullbackStrategy(),
        "BREAKOUT": BreakoutStrategy(),
        "MEAN_REVERSION": MeanReversionStrategy(),
    }

    entries: dict[str, StrategyComparisonEntry] = {}
    for name in strategy_names:
        selector = StrategySelector(
            strategies=strategies,
            enabled={n: (n == name) for n in strategies},
        )
        engine = BacktestEngine(symbol_spec, risk_settings, config=config, selector=selector)
        result = engine.run(m15_df)
        entries[name] = StrategyComparisonEntry(strategy_name=name, result=result, metrics=compute_metrics(result))
    return entries
