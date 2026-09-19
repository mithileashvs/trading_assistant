"""
Benchmark Baseline Calculation (Phase 10, section 17).

Provides simple, deterministic baseline comparisons:
- Buy-and-hold equivalent
- No-trade baseline

Does NOT rank strategies or declare a "winner" -- its sole purpose is
to provide descriptive context for raw backtest results.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from app.mt5.interface import SymbolSpec


def compute_benchmark(
    df: pd.DataFrame,
    starting_balance: float,
    symbol_spec: SymbolSpec,
    warmup_bars: int = 0,
    base_lots: float = 0.1,
) -> dict:
    """Computes descriptive benchmark baselines across the evaluated data window.

    Parameters:
        df: The historical M15 DataFrame.
        starting_balance: Starting account balance.
        symbol_spec: Contract specification for tick value/size conversions.
        warmup_bars: Number of warmup bars preceding the active evaluation window.
        base_lots: Position size in lots for the simulated buy-and-hold comparison.

    Returns:
        Dictionary containing buy-and-hold and no-trade baseline metrics.
    """
    if df is None or len(df) <= warmup_bars:
        return {
            "buy_and_hold_pnl": 0.0,
            "buy_and_hold_return_pct": 0.0,
            "buy_and_hold_start_price": None,
            "buy_and_hold_end_price": None,
            "no_trade_pnl": 0.0,
            "no_trade_return_pct": 0.0,
            "eval_bars": 0,
        }

    eval_df = df.iloc[warmup_bars:]
    start_price = float(eval_df.iloc[0]["open"])
    end_price = float(eval_df.iloc[-1]["close"])

    price_diff = end_price - start_price
    ticks = price_diff / symbol_spec.tick_size if symbol_spec.tick_size > 0 else 0.0
    pnl = ticks * symbol_spec.tick_value * base_lots

    ret_pct = (price_diff / start_price * 100.0) if start_price > 0 else 0.0

    return {
        "buy_and_hold_pnl": float(round(pnl, 2)),
        "buy_and_hold_return_pct": float(round(ret_pct, 4)),
        "buy_and_hold_start_price": start_price,
        "buy_and_hold_end_price": end_price,
        "buy_and_hold_lots": base_lots,
        "no_trade_pnl": 0.0,
        "no_trade_return_pct": 0.0,
        "eval_bars": len(eval_df),
    }
