"""
Read-only MT5 connectivity self-test (Phase 9 prep: "MT5 Demo
connectivity"). Verifies the terminal connection, account info + demo
status, symbol discovery/spec, tick, H4/H1/M15 data, spread, volume
and stop-level constraints, and market-data freshness -- and nothing
else. This script NEVER calls order_send / submit_order /
close_position / modify_position; it does not even import
app.execution.engine (see app/mt5/selftest.py for the enforced
read-only check logic).

Usage:
    PYTHONPATH=. python scripts/mt5_selftest.py

By default this REFUSES to run against MT5_USE_MOCK=true, since the
whole point is to certify a real MT5 Demo connection -- the mock
client already has its own dedicated test suite
(tests/test_mock_mt5_client.py). Pass --allow-mock only if you
specifically want to sanity-check the self-test's own logic against
the mock client (e.g. in a dev sandbox with no real MT5 terminal
available).

Exit code is 0 only if every check PASSed. Any FAIL or BLOCKED check
exits non-zero -- this script fails closed, on purpose, so it's safe
to use as a gate in a startup script or CI step.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.config.settings import get_settings
from app.mt5.factory import build_mt5_client
from app.mt5.selftest import format_report_text, run_mt5_selftest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--allow-mock", action="store_true",
        help="Allow running against MT5_USE_MOCK=true (for sanity-checking this script itself, not for certifying Demo readiness).",
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON instead of a text summary.")
    args = parser.parse_args()

    settings = get_settings()

    if settings.mt5_use_mock and not args.allow_mock:
        print(
            "REFUSING TO RUN: MT5_USE_MOCK=true. This self-test exists to certify a real "
            "MT5 Demo connection (set MT5_USE_MOCK=false and MT5_LOGIN/MT5_PASSWORD/MT5_SERVER "
            "in .env first). Pass --allow-mock if you specifically want to exercise this "
            "script's own logic against the mock client instead.",
            file=sys.stderr,
        )
        return 2

    try:
        client = build_mt5_client(settings)
    except Exception as exc:  # noqa: BLE001 - this IS the fail-closed report for this case
        print(
            f"REFUSING TO RUN: could not construct the MT5 client: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        report = run_mt5_selftest(client, settings)
    finally:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 - best-effort cleanup, don't mask the real result
            pass

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(format_report_text(report))

    return 0 if report.overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
