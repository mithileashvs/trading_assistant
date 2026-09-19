"""
Out-of-Sample and Walk-Forward Testing (sections 26-27).

"Do NOT optimize and evaluate on the same dataset." This module splits
history into sequential, non-overlapping periods and runs the SAME
fixed-rule engine independently on each — there is no parameter
re-fitting here (this system doesn't optimize hundreds of parameters
per section 27), so "walk-forward" here means checking whether a
fixed rule set holds up consistently across successive time windows,
not re-optimizing per fold.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.backtesting.engine import BacktestEngine
from app.backtesting.metrics import compute_metrics
from app.backtesting.results import BacktestResult
from app.strategy_lab.models import OverfittingReport


@dataclass
class SplitResult:
    label: str  # "IN_SAMPLE" | "OUT_OF_SAMPLE" | fold label
    start: pd.Timestamp
    end: pd.Timestamp
    bars: int
    result: BacktestResult
    metrics: dict


def train_test_split(m15_df: pd.DataFrame, train_frac: float = 0.7) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Simple chronological split: train_frac of the bars (earliest)
    for in-sample, the remainder (latest) for out-of-sample. No shuffling
    — a shuffled split would leak future information into training."""
    if not 0 < train_frac < 1:
        raise ValueError("train_frac must be between 0 and 1.")
    cut = int(len(m15_df) * train_frac)
    return m15_df.iloc[:cut], m15_df.iloc[cut:]


def run_in_sample_out_of_sample(
    engine_factory,
    m15_df: pd.DataFrame,
    train_frac: float = 0.7,
) -> dict[str, SplitResult]:
    """Runs the engine independently on the in-sample and out-of-sample
    slices. `engine_factory` is a zero-arg callable returning a fresh
    BacktestEngine (fresh, since engines are effectively stateless per
    run but this keeps each run's config explicit and isolated)."""
    train_df, test_df = train_test_split(m15_df, train_frac)

    results = {}
    for label, df in (("IN_SAMPLE", train_df), ("OUT_OF_SAMPLE", test_df)):
        engine: BacktestEngine = engine_factory()
        result = engine.run(df)
        results[label] = SplitResult(
            label=label, start=df.index[0] if len(df) else None, end=df.index[-1] if len(df) else None,
            bars=len(df), result=result, metrics=compute_metrics(result),
        )
    return results


def walk_forward_folds(
    engine_factory,
    m15_df: pd.DataFrame,
    n_folds: int = 3,
) -> list[SplitResult]:
    """Splits the data into `n_folds` sequential, non-overlapping
    windows and runs the (fixed-rule) engine independently on each,
    reporting per-fold metrics. Consistent performance across folds is
    a sign of a robust edge; a strategy that only works in one fold and
    collapses in others should be treated with suspicion (section 27)."""
    if n_folds < 2:
        raise ValueError("n_folds must be at least 2.")

    fold_size = len(m15_df) // n_folds
    if fold_size == 0:
        raise ValueError("Not enough bars to form the requested number of folds.")

    folds = []
    for i in range(n_folds):
        start = i * fold_size
        end = len(m15_df) if i == n_folds - 1 else (i + 1) * fold_size
        fold_df = m15_df.iloc[start:end]
        if fold_df.empty:
            continue
        engine: BacktestEngine = engine_factory()
        result = engine.run(fold_df)
        folds.append(
            SplitResult(
                label=f"FOLD_{i + 1}", start=fold_df.index[0], end=fold_df.index[-1],
                bars=len(fold_df), result=result, metrics=compute_metrics(result),
            )
        )
    return folds


def compute_overfitting_analysis(is_split: SplitResult, oos_split: SplitResult) -> OverfittingReport:
    """Compute descriptive overfitting degradation between In-Sample and Out-of-Sample splits.

    Provides purely descriptive statistics (never subjective ranking or recommendation).
    """
    is_metrics = is_split.metrics or {}
    oos_metrics = oos_split.metrics or {}

    is_net_profit = float(is_metrics.get("net_profit", 0.0))
    oos_net_profit = float(oos_metrics.get("net_profit", 0.0))

    is_trades = int(is_metrics.get("total_trades", is_metrics.get("number_of_trades", 0)))
    oos_trades = int(oos_metrics.get("total_trades", oos_metrics.get("number_of_trades", 0)))

    is_win_rate = float(is_metrics.get("win_rate", 0.0))
    oos_win_rate = float(oos_metrics.get("win_rate", 0.0))

    is_dd = float(is_metrics.get("max_drawdown_pct", 0.0))
    oos_dd = float(oos_metrics.get("max_drawdown_pct", 0.0))

    if is_net_profit != 0:
        profit_deg = ((is_net_profit - oos_net_profit) / abs(is_net_profit)) * 100.0
    else:
        profit_deg = 0.0 if oos_net_profit == 0 else None

    return OverfittingReport(
        is_net_profit=is_net_profit,
        oos_net_profit=oos_net_profit,
        is_trades=is_trades,
        oos_trades=oos_trades,
        is_win_rate=is_win_rate,
        oos_win_rate=oos_win_rate,
        is_max_drawdown=is_dd,
        oos_max_drawdown=oos_dd,
        profit_degradation_pct=round(profit_deg, 4) if profit_deg is not None else None,
        is_period={
            "start": str(is_split.start) if is_split.start is not None else "",
            "end": str(is_split.end) if is_split.end is not None else "",
            "bars": is_split.bars,
        },
        oos_period={
            "start": str(oos_split.start) if oos_split.start is not None else "",
            "end": str(oos_split.end) if oos_split.end is not None else "",
            "bars": oos_split.bars,
        },
        in_sample_metrics=is_metrics,
        out_of_sample_metrics=oos_metrics,
    )
