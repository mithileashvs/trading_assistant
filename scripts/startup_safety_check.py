"""
Standalone StartupSafetyCheck runner.

Runs every startup safety check (configuration, trading mode, MT5,
account, symbol, market data, features, news, journal, kill switch,
risk configuration, safety/execution gates, clock, dependencies) and
prints a fail-closed report -- the same check that gates
scripts/run_paper_trading.py, runnable on its own for manual
inspection or as a CI/deployment gate.

This script NEVER places an order: it never imports
app.execution.engine, and app.safety.startup_check itself only ever
calls the read side of IMT5Client (via app.mt5.selftest).

Usage:
    PYTHONPATH=. python scripts/startup_safety_check.py
    PYTHONPATH=. python scripts/startup_safety_check.py --live-real-money
    PYTHONPATH=. python scripts/startup_safety_check.py --json

Exit code is 0 only if trading is allowed to start right now (including
the monitoring-only case where the sole blocker is a deliberately
active kill switch). Any other outcome exits 1.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.config.settings import get_settings
from app.journal.journal import TradeJournal
from app.mt5.factory import build_mt5_client
from app.risk.kill_switch import KillSwitch
from app.safety.startup_check import format_startup_report_text, run_startup_safety_check


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--live-real-money", action="store_true",
        help="Only meaningful in LIVE trading mode -- require/verify a REAL-MONEY account instead of DEMO.",
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON instead of text.")
    args = parser.parse_args()

    settings = get_settings()

    client = None
    from app.config.settings import TradingMode
    if settings.trading_mode != TradingMode.BACKTEST:
        try:
            client = build_mt5_client(settings)
            client.connect()
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed mt5_connectivity check, not a crash
            print(f"Could not construct/connect the MT5 client: {type(exc).__name__}: {exc}", file=sys.stderr)
            client = None

    journal = None
    try:
        journal = TradeJournal(settings.database_url.replace("sqlite:///", ""))
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed journal_writable check, not a crash
        print(f"Could not construct the journal: {type(exc).__name__}: {exc}", file=sys.stderr)

    kill_switch = KillSwitch(default_active=False)

    result = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch,
        require_demo_account=not args.live_real_money,
    )

    if client is not None:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 - best-effort cleanup, don't mask the real result
            pass

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_startup_report_text(result))

    return 0 if result.trading_allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
