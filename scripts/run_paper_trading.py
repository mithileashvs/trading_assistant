"""
Phase 12 — Paper Trading / Simulation Runtime CLI Runner.

Runs the strictly isolated PaperTradingRuntime using controlled historical/chronological
market data or step-by-step paper simulation.
Under NO circumstances can this script place real broker orders or connect to live execution.
TRADING_MODE=LIVE is strictly prohibited and fails closed immediately.

Usage:
    python scripts/run_paper_trading.py [--balance 10000] [--bars 500] [--timeframe M15]
"""
from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from app.config.settings import TradingMode, get_settings
from app.journal.journal import TradeJournal
from app.logging_config import configure_logging, log_event
from app.mt5.factory import build_mt5_client
from app.paper.broker import PaperExecutionAdapter
from app.paper.metrics import compute_paper_metrics
from app.paper.runtime import PaperTradingRuntime
from app.paper.session import PaperSessionConfig
from app.paper.store import InMemoryPaperStateStore, SqlitePaperStateStore
from app.positions.monitor import PositionMonitor
from app.risk.kill_switch import KillSwitch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 12 Paper Trading Simulation Runtime")
    parser.add_argument("--balance", type=float, default=10_000.0, help="Initial simulation balance (USD)")
    parser.add_argument("--bars", type=int, default=500, help="Number of historical bars to simulate")
    parser.add_argument("--timeframe", type=str, default="M15", help="Timeframe (e.g. M15, H1)")
    parser.add_argument("--slippage", type=float, default=0.0, help="Simulated slippage in points")
    parser.add_argument("--spread-mult", type=float, default=1.0, help="Spread multiplier")
    parser.add_argument("--db-path", type=str, default="", help="Path to SQLite paper state store")
    parser.add_argument("--clean-db", action="store_true", help="Remove existing DB file before starting")
    args = parser.parse_args(argv)

    settings = get_settings()
    logger = configure_logging(settings.log_level, settings.log_dir)

    # STRICT SAFETY GATE: reject LIVE mode fail-closed
    if settings.trading_mode == TradingMode.LIVE:
        print("FATAL: Cannot run paper trading with TRADING_MODE=LIVE", file=sys.stderr)
        log_event(logger, "SYSTEM_ERROR", "Attempted to run paper trading in LIVE mode. Halting.", level=50)
        return 1

    if settings.kill_switch:
        print("HALTED: Kill switch is active in configuration.", file=sys.stderr)
        log_event(logger, "KILL_SWITCH", "Kill switch is active in config. Halting startup.", level=30)
        return 1

    print("=" * 60)
    print("PHASE 12 — PAPER TRADING RUNTIME")
    print("Mode: SIMULATION ONLY (Zero broker order capability)")
    print("=" * 60)

    # Build market data client (read-only data feed, no live execution)
    client = build_mt5_client(settings)
    client.connect()

    symbol = client.discover_symbol(settings.symbol_candidate_list())
    if symbol is None:
        print("No tradable symbol found among the configured candidates.", file=sys.stderr)
        client.disconnect()
        return 1
    spec = client.get_symbol_spec(symbol)

    # Initialize store
    db_file: Path | None = None
    if args.clean_db and args.db_path:
        p = Path(args.db_path)
        if p.exists():
            p.unlink()

    if args.db_path:
        state_store = SqlitePaperStateStore(args.db_path)
    else:
        state_store = InMemoryPaperStateStore()

    journal = TradeJournal(settings.database_url.replace("sqlite:///", ""))
    kill_switch = KillSwitch(default_active=False)
    pos_monitor = PositionMonitor()

    # Create paper execution adapter (structurally isolated from live execution)
    paper_broker = PaperExecutionAdapter(
        symbol_spec=spec,
        journal=journal,
        initial_balance=args.balance,
        trading_mode=TradingMode.PAPER,
    )

    runtime = PaperTradingRuntime(
        symbol_spec=spec,
        settings=settings,
        risk_settings=settings.risk,
        paper_broker=paper_broker,
        journal=journal,
        kill_switch=kill_switch,
        position_monitor=pos_monitor,
        state_store=state_store,
        trading_mode=TradingMode.PAPER,
    )

    session_cfg = PaperSessionConfig(
        initial_balance=args.balance,
        trading_mode=TradingMode.PAPER,
        slippage_points=args.slippage,
        spread_multiplier=args.spread_mult,
    )
    session = runtime.start_session(session_cfg)

    print(f"Session ID:            {session.session_id}")
    print(f"Initial Balance:       ${args.balance:,.2f}")
    print(f"Reproducibility Hash:  {session_cfg.reproducibility_hash[:16]}...")
    print(f"Timeframe / Bars:      {args.timeframe} / {args.bars}")
    print("=" * 60)

    interrupted = False

    def handle_sigint(signum, frame):
        nonlocal interrupted
        interrupted = True
        print("\nShutdown requested (Ctrl+C)... Stopping paper trading session.")

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        df = client.get_ohlcv(symbol, args.timeframe, args.bars)
        if df is None or df.empty:
            print("No OHLCV data returned by client.", file=sys.stderr)
            runtime.stop_session("No market data")
            client.disconnect()
            return 1

        print(f"Running simulation over {len(df)} chronological bars...")
        runtime.process_chronological_bars(df)

    except Exception as exc:
        print(f"Simulation error: {exc}", file=sys.stderr)
        log_event(logger, "PAPER_ERROR", str(exc), level=40)
    finally:
        if runtime.is_running:
            runtime.stop_session("CLI simulation complete")
        client.disconnect()

    # Display summary metrics
    trades = runtime.broker.closed_positions
    metrics = compute_paper_metrics(trades, starting_balance=args.balance)

    print("\n" + "=" * 60)
    print("SIMULATION SUMMARY METRICS")
    print("=" * 60)
    print(f"Total Bars Processed:  {runtime.bars_processed}")
    print(f"Final Balance:         ${runtime.broker.account.balance:,.2f}")
    print(f"Final Equity:          ${runtime.broker.account.equity:,.2f}")
    print(f"Realized PnL:          ${runtime.broker.account.realized_pnl:,.2f}")
    print(f"Total Trades:          {metrics.get('total_trades', 0)}")
    print(f"Win Rate:              {metrics.get('win_rate', 0.0) * 100:.1f}%")
    print(f"Profit Factor:         {metrics.get('profit_factor', 0.0):.2f}")
    print(f"Max Drawdown:          {metrics.get('max_drawdown_pct', 0.0):.2f}%")
    print(f"Sharpe Ratio:          {metrics.get('sharpe_ratio', 0.0):.2f}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
