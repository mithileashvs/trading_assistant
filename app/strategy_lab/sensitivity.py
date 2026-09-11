"""
Parameter Sensitivity Analysis (section 27).

"Show how performance changes when parameters vary... A strategy that
only works with one extremely specific parameter combination should be
flagged as fragile." This sweeps ONE numeric parameter across a small
grid of values, re-running the backtest at each, and flags fragility
when performance is not reasonably stable across the neighborhood.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from app.backtesting.engine import BacktestEngine
from app.backtesting.metrics import compute_metrics


@dataclass
class SensitivityPoint:
    value: float
    net_profit: float
    profit_factor: float
    number_of_trades: int
    win_rate: float


@dataclass
class SensitivityReport:
    parameter_name: str
    points: list[SensitivityPoint]
    fragile: bool
    fragility_reason: str


def run_sensitivity_sweep(
    engine_factory: Callable[[float], BacktestEngine],
    m15_df: pd.DataFrame,
    parameter_name: str,
    values: list[float],
    fragility_profit_sign_flip_threshold: float = 0.5,
) -> SensitivityReport:
    """`engine_factory(value)` must return a BacktestEngine configured
    with the swept parameter set to `value`. Runs the same dataset at
    every value in `values` (ascending or as given) and reports how net
    profit, profit factor, trade count, and win rate move.

    Flags fragility when the sign of net profit flips across more than
    `fragility_profit_sign_flip_threshold` fraction of adjacent pairs
    in the swept range — i.e. profitability isn't robust to small
    parameter changes, only to one specific value.
    """
    points: list[SensitivityPoint] = []
    for value in values:
        engine = engine_factory(value)
        result = engine.run(m15_df)
        metrics = compute_metrics(result)
        points.append(
            SensitivityPoint(
                value=value,
                net_profit=metrics.get("net_profit", 0.0),
                profit_factor=metrics.get("profit_factor", 0.0) if metrics.get("number_of_trades", 0) else 0.0,
                number_of_trades=metrics.get("number_of_trades", 0),
                win_rate=metrics.get("win_rate", 0.0),
            )
        )

    flips = 0
    comparable_pairs = 0
    for a, b in zip(points, points[1:]):
        comparable_pairs += 1
        if (a.net_profit > 0) != (b.net_profit > 0):
            flips += 1

    flip_ratio = (flips / comparable_pairs) if comparable_pairs else 0.0
    fragile = flip_ratio > fragility_profit_sign_flip_threshold
    reason = (
        f"Net profit changes sign across {flips}/{comparable_pairs} adjacent parameter steps "
        f"({flip_ratio:.0%}) — profitability appears to depend on a narrow, specific value."
        if fragile
        else f"Net profit sign is stable across {comparable_pairs - flips}/{comparable_pairs} adjacent parameter steps."
    )

    return SensitivityReport(parameter_name=parameter_name, points=points, fragile=fragile, fragility_reason=reason)
