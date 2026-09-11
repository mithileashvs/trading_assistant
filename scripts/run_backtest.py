"""
Run a backtest against the configured MT5 client (mock by default) and
print a summary of the results and metrics.

Usage:
    PYTHONPATH=. python scripts/run_backtest.py [--bars N] [--step N]

This is a convenience wrapper around app.backtesting.engine.BacktestEngine
for ad-hoc runs; it is not a substitute for the Strategy Laboratory
(Phase 7), which will add out-of-sample/walk-forward/Monte Carlo testing.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.backtesting.engine import BacktestConfig, BacktestEngine
from app.backtesting.metrics import compute_metrics
from app.config.settings import get_settings
from app.mt5.factory import build_mt5_client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=int, default=6000, help="Number of M15 bars of history to fetch.")
    parser.add_argument("--warmup-bars", type=int, default=3400, help="M15 bars required before the first evaluation.")
    parser.add_argument("--step", type=int, default=4, help="Evaluate every Nth M15 bar for new entries.")
    parser.add_argument("--starting-balance", type=float, default=10_000.0)
    args = parser.parse_args()

    settings = get_settings()
    client = build_mt5_client(settings)
    client.connect()

    symbol = client.discover_symbol(settings.symbol_candidate_list())
    if symbol is None:
        print("No tradable symbol found among the configured candidates.", file=sys.stderr)
        return 1
    spec = client.get_symbol_spec(symbol)

    print(f"Fetching {args.bars} M15 bars for {symbol}...")
    m15_df = client.get_ohlcv(symbol, "M15", args.bars)

    cfg = BacktestConfig(
        starting_balance=args.starting_balance,
        warmup_bars=args.warmup_bars,
        step=args.step,
    )
    engine = BacktestEngine(spec, settings.risk, config=cfg)

    print("Running backtest (this walks forward bar-by-bar; may take a while)...")
    result = engine.run(m15_df)
    metrics = compute_metrics(result)

    print(f"\nTrades: {len(result.trades)}")
    print(f"Starting balance: {result.starting_balance:.2f}")
    print(f"Ending balance:   {result.ending_balance:.2f}")
    print(f"Net profit:       {result.net_profit:.2f}")
    print("\nMetrics:")
    print(json.dumps(metrics, indent=2, default=str))

    client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
