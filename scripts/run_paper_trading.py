"""
Run the trading loop (Phase 8 Paper Trading / Phase 9 Demo / Phase 10
Live, all via the same code path — see app/runtime/loop.py) against
the configured MT5 client.

Usage:
    PYTHONPATH=. python scripts/run_paper_trading.py [--iterations N] [--sleep-seconds N]

Defaults to TRADING_MODE=PAPER against the mock client unless .env
says otherwise. This script places NO real orders unless
TRADING_MODE=LIVE and every safeguard in Settings.validate_live_safety()
is satisfied -- and even then, only against whatever MT5 client
MT5_USE_MOCK selects.
"""
from __future__ import annotations

import argparse
import sys

from app.config.settings import get_settings
from app.journal.journal import TradeJournal
from app.logging_config import configure_logging, log_event
from app.mt5.factory import build_mt5_client
from app.risk.kill_switch import KillSwitch
from app.runtime.loop import TradingLoop
from app.safety.startup_check import format_startup_report_text, run_startup_safety_check


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--sleep-seconds", type=float, default=5.0)
    parser.add_argument(
        "--live-real-money", action="store_true",
        help=(
            "Only meaningful in LIVE trading mode. By default a LIVE-family run must verify against a "
            "DEMO account (StartupSafetyCheck fails closed otherwise). Pass this flag to instead require "
            "and verify a REAL-MONEY account. Refuses to start either way if the account type can't be "
            "determined, or if it doesn't match what was requested."
        ),
    )
    args = parser.parse_args()

    settings = get_settings()
    logger = configure_logging(settings.log_level, settings.log_dir)

    try:
        settings.validate_live_safety()
    except RuntimeError as exc:
        log_event(logger, "SYSTEM_ERROR", str(exc), level=40)
        return 1

    if settings.kill_switch:
        log_event(logger, "KILL_SWITCH", "Kill switch is active in config. Halting startup.", level=30)
        return 1

    client = build_mt5_client(settings)
    client.connect()

    symbol = client.discover_symbol(settings.symbol_candidate_list())
    if symbol is None:
        print("No tradable symbol found among the configured candidates.", file=sys.stderr)
        return 1
    spec = client.get_symbol_spec(symbol)

    journal = TradeJournal(settings.database_url.replace("sqlite:///", ""))
    kill_switch = KillSwitch(default_active=False)

    # StartupSafetyCheck runs BEFORE the trading runtime (TradingLoop)
    # is constructed. This is the actual, non-bypassable gate on
    # trading start -- nothing in the AI layer, frontend, or API can
    # reach this code path. See app/safety/startup_check.py.
    safety_result = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch,
        require_demo_account=not args.live_real_money,
    )
    print(format_startup_report_text(safety_result))
    log_event(
        logger, "STARTUP_SAFETY",
        f"Startup safety check: {safety_result.overall_status} (monitoring_only={safety_result.monitoring_only})",
        {"failed_checks": safety_result.failed_checks, "warnings": safety_result.warnings},
        level=30 if not safety_result.trading_allowed else 20,
    )
    if not safety_result.trading_allowed and not safety_result.monitoring_only:
        log_event(logger, "SYSTEM_ERROR",
                   f"Startup safety check failed: {safety_result.failed_checks}. Trading will not start.",
                   level=40)
        client.disconnect()
        return 1

    loop = TradingLoop(settings, client, spec, journal, kill_switch=kill_switch)

    if safety_result.monitoring_only:
        log_event(logger, "KILL_SWITCH",
                   "Starting in monitoring-only mode: kill switch is active, no new trades will be submitted.",
                   level=30)

    log_event(logger, "MT5_CONNECT", f"Trading loop starting. {loop.account_safety_note}",
              {"trading_mode": settings.trading_mode.value, "symbol": symbol})

    summaries = loop.run(iterations=args.iterations, sleep_seconds=args.sleep_seconds)
    for i, summary in enumerate(summaries):
        log_event(
            logger, "SIGNAL_GENERATED" if summary.signal_approved else "SIGNAL_REJECTED",
            f"Cycle {i + 1}: regime={summary.regime} signal={summary.signal_direction} "
            f"approved={summary.signal_approved} actions={summary.actions_taken} errors={summary.errors}",
        )

    client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
