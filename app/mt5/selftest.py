"""
Read-only MT5 connectivity self-test (Phase 9 prep: "MT5 Demo
connectivity", see app/runtime/loop.py's docstring).

This module NEVER imports app.execution.engine and NEVER calls
client.submit_order / close_position / close_position_partial /
modify_position -- it only calls the read side of IMT5Client
(connect, is_connected, get_account_info, discover_symbol,
get_symbol_spec, get_tick, get_ohlcv). That is enforced structurally
(no execution-engine import exists here at all), not just by
convention.

Design: every check is independent and wrapped so one failure doesn't
crash the run -- but checks that depend on an earlier check having
succeeded (e.g. you can't fetch a tick without a resolved symbol) are
marked BLOCKED rather than attempted, since attempting them would
either throw an unrelated exception or silently pass on stale/garbage
inputs. The overall report is fail-closed: `overall_pass` is True only
if every single check passed; any FAIL or BLOCKED check makes the
whole run fail.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.config.settings import Settings
from app.market_data.engine import MarketDataEngine, StaleMarketDataError
from app.mt5.interface import IMT5Client, SymbolSpec, Tick


@dataclass
class SelfTestCheck:
    name: str
    status: str  # "PASS" | "FAIL" | "BLOCKED"
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


@dataclass
class SelfTestReport:
    generated_at: datetime
    trading_mode: str
    mt5_use_mock: bool
    checks: list[SelfTestCheck] = field(default_factory=list)

    @property
    def overall_pass(self) -> bool:
        return len(self.checks) > 0 and all(c.passed for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "trading_mode": self.trading_mode,
            "mt5_use_mock": self.mt5_use_mock,
            "overall_pass": self.overall_pass,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "data": c.data}
                for c in self.checks
            ],
        }


def _blocked(name: str, reason: str) -> SelfTestCheck:
    return SelfTestCheck(name=name, status="BLOCKED", detail=f"Blocked: {reason}")


def run_mt5_selftest(client: IMT5Client, settings: Settings) -> SelfTestReport:
    """Run every read-only connectivity/sanity check against `client`,
    in dependency order, and return a fail-closed report.

    Does not call client.connect() itself if the caller already has a
    connected client (e.g. the FastAPI AppState's client, connected at
    startup) -- but will call it if `client.is_connected()` is False,
    so this also works as a standalone pre-flight check.
    """
    report = SelfTestReport(
        generated_at=datetime.now(timezone.utc),
        trading_mode=settings.trading_mode.value,
        mt5_use_mock=settings.mt5_use_mock,
    )
    checks = report.checks

    # -- 1. Terminal connection -----------------------------------------
    connect_ok = False
    try:
        if not client.is_connected():
            client.connect()
        connect_ok = client.is_connected()
        if connect_ok:
            checks.append(SelfTestCheck("mt5_terminal_connection", "PASS", "Connected to the MT5 terminal."))
        else:
            checks.append(SelfTestCheck(
                "mt5_terminal_connection", "FAIL",
                "connect() did not raise but is_connected() is still False -- treating as not connected.",
            ))
    except Exception as exc:  # noqa: BLE001 - report every failure mode, not just the expected ones
        checks.append(SelfTestCheck("mt5_terminal_connection", "FAIL", f"{type(exc).__name__}: {exc}"))

    if not connect_ok:
        # Everything else needs a live connection -- fail closed rather
        # than attempting calls that would throw confusing secondary
        # errors (or, worse, calls that happen to succeed against
        # stale/cached state and mask the real problem).
        for name in (
            "account_info", "demo_account_status", "symbol_discovery",
            "symbol_specification", "tick", "h4_data", "h1_data", "m15_data",
            "spread", "volume_constraints", "stop_level_constraints",
            "market_data_freshness",
        ):
            checks.append(_blocked(name, "MT5 terminal connection check did not pass."))
        return report

    # -- 2. Account info + demo status -----------------------------------
    account = None
    try:
        account = client.get_account_info()
        checks.append(SelfTestCheck(
            "account_info", "PASS",
            f"login={account.login} currency={account.currency} leverage={account.leverage}",
            data={
                "login": account.login, "balance": account.balance, "equity": account.equity,
                "margin": account.margin, "margin_free": account.margin_free,
                "currency": account.currency, "leverage": account.leverage,
                "trade_allowed": account.trade_allowed,
            },
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(SelfTestCheck("account_info", "FAIL", f"{type(exc).__name__}: {exc}"))

    if account is None:
        checks.append(_blocked("demo_account_status", "account_info check did not pass."))
    elif account.is_demo is True:
        checks.append(SelfTestCheck("demo_account_status", "PASS", "Broker confirms this is a DEMO account."))
    elif account.is_demo is False:
        # This is the one check where "the data is fine" is still a
        # hard FAIL: the whole point of this self-test is to certify
        # Demo connectivity. A confirmed real-money account must never
        # be waved through by an automated check.
        checks.append(SelfTestCheck(
            "demo_account_status", "FAIL",
            "Broker reports this is a REAL-MONEY account, not demo. Refusing to certify "
            "this connection for the Demo-connectivity stage.",
        ))
    else:
        checks.append(SelfTestCheck(
            "demo_account_status", "FAIL",
            "Broker did not report a determinable demo/real status (is_demo=None). "
            "Treating unknown as not-safe-to-certify rather than assuming demo.",
        ))

    # -- 3. Symbol discovery ----------------------------------------------
    symbol: Optional[str] = None
    try:
        candidates = settings.symbol_candidate_list()
        symbol = client.discover_symbol(candidates)
        if symbol:
            checks.append(SelfTestCheck(
                "symbol_discovery", "PASS", f"Resolved '{symbol}' from candidates {candidates}.",
                data={"symbol": symbol, "candidates": candidates},
            ))
        else:
            checks.append(SelfTestCheck(
                "symbol_discovery", "FAIL",
                f"None of the candidate symbols {candidates} are tradable on this broker.",
            ))
    except Exception as exc:  # noqa: BLE001
        checks.append(SelfTestCheck("symbol_discovery", "FAIL", f"{type(exc).__name__}: {exc}"))

    # -- 4. Symbol specification (+ volume / stop-level constraints) ------
    spec: Optional[SymbolSpec] = None
    if symbol is None:
        for name in ("symbol_specification", "volume_constraints", "stop_level_constraints"):
            checks.append(_blocked(name, "symbol_discovery check did not pass."))
    else:
        try:
            spec = client.get_symbol_spec(symbol)
            if not spec.trade_allowed:
                checks.append(SelfTestCheck(
                    "symbol_specification", "FAIL", f"'{symbol}' was found but trading is disabled on it (trade_allowed=False).",
                ))
                spec = None  # treat as unusable for downstream checks
            else:
                checks.append(SelfTestCheck(
                    "symbol_specification", "PASS",
                    f"digits={spec.digits} contract_size={spec.contract_size} tick_size={spec.tick_size}",
                    data={
                        "digits": spec.digits, "contract_size": spec.contract_size,
                        "tick_size": spec.tick_size, "tick_value": spec.tick_value,
                        "trade_allowed": spec.trade_allowed,
                    },
                ))
        except Exception as exc:  # noqa: BLE001
            checks.append(SelfTestCheck("symbol_specification", "FAIL", f"{type(exc).__name__}: {exc}"))

        if spec is None:
            checks.append(_blocked("volume_constraints", "symbol_specification check did not pass."))
            checks.append(_blocked("stop_level_constraints", "symbol_specification check did not pass."))
        else:
            vol_problems = []
            if spec.volume_min <= 0:
                vol_problems.append(f"volume_min={spec.volume_min} must be > 0")
            if spec.volume_step <= 0:
                vol_problems.append(f"volume_step={spec.volume_step} must be > 0")
            if spec.volume_max < spec.volume_min:
                vol_problems.append(f"volume_max={spec.volume_max} < volume_min={spec.volume_min}")
            if vol_problems:
                checks.append(SelfTestCheck("volume_constraints", "FAIL", "; ".join(vol_problems)))
            else:
                checks.append(SelfTestCheck(
                    "volume_constraints", "PASS",
                    f"min={spec.volume_min} max={spec.volume_max} step={spec.volume_step}",
                    data={"volume_min": spec.volume_min, "volume_max": spec.volume_max, "volume_step": spec.volume_step},
                ))

            stop_problems = []
            if spec.stops_level_points < 0:
                stop_problems.append(f"stops_level_points={spec.stops_level_points} must be >= 0")
            if spec.freeze_level_points < 0:
                stop_problems.append(f"freeze_level_points={spec.freeze_level_points} must be >= 0")
            if stop_problems:
                checks.append(SelfTestCheck("stop_level_constraints", "FAIL", "; ".join(stop_problems)))
            else:
                checks.append(SelfTestCheck(
                    "stop_level_constraints", "PASS",
                    f"stops_level={spec.stops_level_points}pt freeze_level={spec.freeze_level_points}pt",
                    data={"stops_level_points": spec.stops_level_points, "freeze_level_points": spec.freeze_level_points},
                ))

    # -- 5. Tick + spread ---------------------------------------------------
    tick: Optional[Tick] = None
    if symbol is None:
        checks.append(_blocked("tick", "symbol_discovery check did not pass."))
        checks.append(_blocked("spread", "symbol_discovery check did not pass."))
    else:
        try:
            tick = client.get_tick(symbol)
            if tick.bid <= 0 or tick.ask <= 0:
                checks.append(SelfTestCheck(
                    "tick", "FAIL", f"Non-positive price in tick: bid={tick.bid} ask={tick.ask}.",
                ))
                tick = None
            elif tick.ask < tick.bid:
                checks.append(SelfTestCheck(
                    "tick", "FAIL", f"Inconsistent tick: ask={tick.ask} < bid={tick.bid}.",
                ))
                tick = None
            else:
                checks.append(SelfTestCheck(
                    "tick", "PASS", f"bid={tick.bid} ask={tick.ask} time={tick.time.isoformat()}",
                    data={"bid": tick.bid, "ask": tick.ask, "time": tick.time.isoformat()},
                ))
        except Exception as exc:  # noqa: BLE001
            checks.append(SelfTestCheck("tick", "FAIL", f"{type(exc).__name__}: {exc}"))

        if tick is None:
            checks.append(_blocked("spread", "tick check did not pass."))
        else:
            spread = tick.spread
            spread_points = spread / spec.tick_size if spec is not None and spec.tick_size else None
            max_spread_points = settings.risk.max_spread_points
            detail = f"spread={spread:.5f} price"
            data: dict[str, Any] = {"spread_price": spread}
            if spread_points is not None:
                detail += f" ({spread_points:.1f} points)"
                data["spread_points"] = spread_points
                data["max_spread_points"] = max_spread_points
                if spread_points > max_spread_points:
                    detail += f" -- exceeds RISK_MAX_SPREAD_POINTS={max_spread_points}; this is a live-market condition, not a connectivity fault, so it's reported but does not fail the self-test"
            checks.append(SelfTestCheck("spread", "PASS", detail, data=data))

    # -- 6. H4 / H1 / M15 OHLCV + market-data freshness --------------------
    if symbol is None:
        for name in ("h4_data", "h1_data", "m15_data", "market_data_freshness"):
            checks.append(_blocked(name, "symbol_discovery check did not pass."))
    else:
        market_data = MarketDataEngine(client, settings)
        freshness_ok = True
        for tf_name, tf_check in (("H4", "h4_data"), ("H1", "h1_data"), ("M15", "m15_data")):
            try:
                df = market_data.get_ohlcv(tf_name, count=50)
                last_ts = df.index[-1]
                checks.append(SelfTestCheck(
                    tf_check, "PASS",
                    f"{len(df)} bars, last candle open={last_ts.isoformat()}",
                    data={"bars": len(df), "last_open_time": last_ts.isoformat()},
                ))
            except StaleMarketDataError as exc:
                freshness_ok = False
                checks.append(SelfTestCheck(tf_check, "FAIL", f"Stale data: {exc}"))
            except Exception as exc:  # noqa: BLE001
                freshness_ok = False
                checks.append(SelfTestCheck(tf_check, "FAIL", f"{type(exc).__name__}: {exc}"))

        if freshness_ok:
            checks.append(SelfTestCheck(
                "market_data_freshness", "PASS",
                "H4/H1/M15 candles and the last tick all passed their timeframe-aware freshness checks.",
            ))
        else:
            checks.append(SelfTestCheck(
                "market_data_freshness", "FAIL",
                "One or more timeframes failed their freshness check -- see h4_data/h1_data/m15_data above.",
            ))

    return report


def format_report_text(report: SelfTestReport) -> str:
    lines = [
        "=" * 70,
        "MT5 SELF-TEST REPORT (read-only -- no orders were placed)",
        "=" * 70,
        f"Generated:      {report.generated_at.isoformat()}",
        f"TRADING_MODE:   {report.trading_mode}",
        f"MT5_USE_MOCK:   {report.mt5_use_mock}",
        "-" * 70,
    ]
    for c in report.checks:
        lines.append(f"[{c.status:^7}] {c.name:<28} {c.detail}")
    lines.append("-" * 70)
    verdict = "READY FOR CONTROLLED MT5 DEMO EXECUTION (all checks passed)" if report.overall_pass \
        else "NOT READY -- one or more checks failed or were blocked (see above)"
    lines.append(verdict)
    lines.append("=" * 70)
    return "\n".join(lines)
