"""
Monte Carlo Testing (section 28).

Randomizes the SEQUENCE of already-closed backtest trades (bootstrap
resampling), rebuilding synthetic equity curves to estimate the
distribution of drawdowns and losing streaks the strategy could have
produced under a different ordering of the same trade outcomes. This
does not re-run the trading engine — it reuses the trade P&L
distribution from a completed BacktestResult, which is standard trade-
sequence Monte Carlo and keeps this fast even for large trade counts.

Results are a distribution, not a guarantee (section 28).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.backtesting.results import BacktestResult


@dataclass
class MonteCarloReport:
    iterations: int
    starting_balance: float
    max_drawdown_pct_distribution: list[float]
    max_losing_streak_distribution: list[int]
    ending_balance_distribution: list[float]

    def percentile(self, values: list[float], pct: float) -> float:
        return float(np.percentile(values, pct))

    def summary(self) -> dict:
        return {
            "iterations": self.iterations,
            "max_drawdown_pct": {
                "p50": self.percentile(self.max_drawdown_pct_distribution, 50),
                "p90": self.percentile(self.max_drawdown_pct_distribution, 90),
                "p99": self.percentile(self.max_drawdown_pct_distribution, 99),
                "worst": min(self.max_drawdown_pct_distribution) if self.max_drawdown_pct_distribution else 0.0,
            },
            "max_losing_streak": {
                "p50": self.percentile(self.max_losing_streak_distribution, 50),
                "p90": self.percentile(self.max_losing_streak_distribution, 90),
                "worst": max(self.max_losing_streak_distribution) if self.max_losing_streak_distribution else 0,
            },
            "ending_balance": {
                "p10": self.percentile(self.ending_balance_distribution, 10),
                "p50": self.percentile(self.ending_balance_distribution, 50),
                "p90": self.percentile(self.ending_balance_distribution, 90),
            },
            "probability_of_ruin_below_50pct": (
                sum(1 for b in self.ending_balance_distribution if b < self.starting_balance * 0.5)
                / len(self.ending_balance_distribution)
                if self.ending_balance_distribution else 0.0
            ),
        }


def _max_drawdown_pct(equity_curve: np.ndarray) -> float:
    running_max = np.maximum.accumulate(equity_curve)
    drawdown_pct = (equity_curve - running_max) / running_max
    return float(drawdown_pct.min())


def _max_losing_streak(pnls: np.ndarray) -> int:
    longest = current = 0
    for pnl in pnls:
        if pnl < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def run_monte_carlo(
    result: BacktestResult,
    iterations: int = 1000,
    seed: int = 42,
) -> MonteCarloReport:
    pnls = np.array([t.pnl for t in result.trades], dtype=float)
    if len(pnls) == 0:
        return MonteCarloReport(
            iterations=0, starting_balance=result.starting_balance,
            max_drawdown_pct_distribution=[], max_losing_streak_distribution=[], ending_balance_distribution=[],
        )

    rng = np.random.default_rng(seed)
    dd_dist: list[float] = []
    streak_dist: list[int] = []
    ending_dist: list[float] = []

    for _ in range(iterations):
        shuffled = rng.permutation(pnls)
        equity_curve = result.starting_balance + np.cumsum(shuffled)
        equity_curve = np.insert(equity_curve, 0, result.starting_balance)
        dd_dist.append(_max_drawdown_pct(equity_curve))
        streak_dist.append(_max_losing_streak(shuffled))
        ending_dist.append(float(equity_curve[-1]))

    return MonteCarloReport(
        iterations=iterations,
        starting_balance=result.starting_balance,
        max_drawdown_pct_distribution=dd_dist,
        max_losing_streak_distribution=streak_dist,
        ending_balance_distribution=ending_dist,
    )
