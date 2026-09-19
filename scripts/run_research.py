"""
Strategy Lab CLI Runner (Phase 11).

Executes strategy research experiments, parameter sweeps, out-of-sample
evaluations, walk-forward tests, or multi-strategy comparisons.

SIMULATION ONLY: TradingMode.LIVE is strictly rejected.

Usage:
    python scripts/run_research.py --mode single --strategy TREND_PULLBACK
    python scripts/run_research.py --mode compare
    python scripts/run_research.py --mode oos --strategy BREAKOUT --train-frac 0.7
    python scripts/run_research.py --mode walk-forward --strategy MEAN_REVERSION --folds 3
    python scripts/run_research.py --mode grid --strategy TREND_PULLBACK --grid '{"pullback_atr_multiplier": [1.0, 1.5]}'
"""
from __future__ import annotations

import argparse
import json
import sys

from app.backtesting.engine import BacktestConfig
from app.config.settings import TradingMode, get_settings
from app.mt5.factory import build_mt5_client
from app.strategy_lab.models import (
    ExperimentConfig,
    ParameterGrid,
)
from app.strategy_lab.runner import StrategyLabRunner
from app.strategy_lab.store import SqliteResearchStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["single", "compare", "oos", "walk-forward", "grid"],
        default="single",
        help="Research execution mode.",
    )
    parser.add_argument(
        "--strategy",
        default="TREND_PULLBACK",
        help="Strategy name (TREND_PULLBACK, BREAKOUT, MEAN_REVERSION, or ALL).",
    )
    parser.add_argument("--bars", type=int, default=5000, help="Number of M15 bars to fetch.")
    parser.add_argument("--warmup-bars", type=int, default=3400, help="Warmup bars.")
    parser.add_argument("--step", type=int, default=4, help="Evaluation step.")
    parser.add_argument("--train-frac", type=float, default=0.7, help="In-sample fraction (0-1).")
    parser.add_argument("--folds", type=int, default=3, help="Number of walk-forward folds.")
    parser.add_argument("--params", type=str, default="{}", help="JSON string of strategy parameters.")
    parser.add_argument("--grid", type=str, default="{}", help="JSON string of parameter ranges for grid mode.")
    parser.add_argument("--starting-balance", type=float, default=10_000.0)
    args = parser.parse_args()

    settings = get_settings()

    # Strict Safety Check
    if settings.trading_mode == TradingMode.LIVE:
        print("Safety violation: Strategy Lab is simulation-only and cannot run in LIVE mode.", file=sys.stderr)
        return 1

    client = build_mt5_client(settings)
    client.connect()

    symbol = client.discover_symbol(settings.symbol_candidate_list())
    if symbol is None:
        print("No tradable symbol found among the configured candidates.", file=sys.stderr)
        client.disconnect()
        return 1
    spec = client.get_symbol_spec(symbol)

    print(f"Fetching {args.bars} M15 bars for {symbol}...")
    m15_df = client.get_ohlcv(symbol, "M15", args.bars)

    db_path = settings.database_url.replace("sqlite:///", "")
    store = SqliteResearchStore(db_path=db_path)

    bt_cfg = BacktestConfig(
        starting_balance=args.starting_balance,
        warmup_bars=args.warmup_bars,
        step=args.step,
    )

    runner = StrategyLabRunner(
        symbol_spec=spec,
        risk_settings=settings.risk,
        backtest_config=bt_cfg,
        store=store,
        trading_mode=TradingMode.BACKTEST,
    )

    try:
        parsed_params = json.loads(args.params)
    except Exception as e:
        print(f"Invalid JSON for --params: {e}", file=sys.stderr)
        client.disconnect()
        return 1

    print(f"Starting Strategy Lab run (mode: {args.mode})...")

    if args.mode == "single":
        config = ExperimentConfig(
            strategy_name=args.strategy,
            parameters=parsed_params,
        )
        res = runner.run_experiment(config, m15_df)
        print("\n=== Experiment Result ===")
        print(f"ID:                   {res.experiment_id}")
        print(f"Status:               {res.status}")
        print(f"Reproducibility Hash: {res.reproducibility_hash}")
        print(f"Net Profit:           {res.metrics.get('net_profit', 0.0):.2f}")
        print(f"Total Trades:         {res.metrics.get('total_trades', 0)}")
        print(f"Win Rate:             {res.metrics.get('win_rate', 0.0):.2%}")
        print(f"Max Drawdown:         {res.metrics.get('max_drawdown_pct', 0.0):.2%}")

    elif args.mode == "compare":
        entries = runner.compare_strategies(m15_df)
        print("\n=== Strategy Comparison (Descriptive Only) ===")
        for name, entry in entries.items():
            m = entry.metrics
            print(f"\n[{name}]")
            print(f"  Net Profit:   {m.get('net_profit', 0.0):.2f}")
            print(f"  Trades:       {m.get('total_trades', 0)}")
            print(f"  Win Rate:     {m.get('win_rate', 0.0):.2%}")
            print(f"  Max Drawdown: {m.get('max_drawdown_pct', 0.0):.2%}")

    elif args.mode == "oos":
        config = ExperimentConfig(
            strategy_name=args.strategy,
            parameters=parsed_params,
        )
        report = runner.run_in_sample_out_of_sample(config, m15_df, train_frac=args.train_frac)
        print("\n=== In-Sample / Out-of-Sample Degradation Report ===")
        print(f"In-Sample Net Profit:      {report.is_net_profit:.2f} ({report.is_trades} trades)")
        print(f"Out-of-Sample Net Profit:  {report.oos_net_profit:.2f} ({report.oos_trades} trades)")
        print(f"In-Sample Win Rate:        {report.is_win_rate:.2%}")
        print(f"Out-of-Sample Win Rate:    {report.oos_win_rate:.2%}")
        print(f"Profit Degradation:        {report.profit_degradation_pct}%")

    elif args.mode == "walk-forward":
        config = ExperimentConfig(
            strategy_name=args.strategy,
            parameters=parsed_params,
        )
        folds = runner.run_walk_forward(config, m15_df, n_folds=args.folds)
        print(f"\n=== Walk-Forward Folds ({len(folds)} Folds) ===")
        for fold in folds:
            m = fold.metrics
            print(f"\n[{fold.label}] {fold.start} -> {fold.end} ({fold.bars} bars)")
            print(f"  Net Profit:   {m.get('net_profit', 0.0):.2f}")
            print(f"  Trades:       {m.get('total_trades', 0)}")
            print(f"  Win Rate:     {m.get('win_rate', 0.0):.2%}")

    elif args.mode == "grid":
        try:
            parsed_grid = json.loads(args.grid)
        except Exception as e:
            print(f"Invalid JSON for --grid: {e}", file=sys.stderr)
            client.disconnect()
            return 1

        grid = ParameterGrid(param_ranges=parsed_grid)
        base_config = ExperimentConfig(
            strategy_name=args.strategy,
            parameters=parsed_params,
        )
        results = runner.run_grid_search(base_config, grid, m15_df)
        print(f"\n=== Parameter Grid Search ({len(results)} Combinations) ===")
        for r in results:
            print(f"Params: {r.config.get('parameters')} -> Profit: {r.metrics.get('net_profit', 0.0):.2f} (Status: {r.status})")

    client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
