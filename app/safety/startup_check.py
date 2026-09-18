"""
Startup Safety Self-Test.

Runs BEFORE the trading runtime (TradingLoop) is allowed to start and
answers one question: is it safe to let this process submit orders?
If any safety-critical check fails, the answer is fail-closed --
trading must not start. This module produces the answer; it does not
enforce it by itself. The caller (see scripts/run_paper_trading.py)
is responsible for actually refusing to construct/run TradingLoop when
`StartupSafetyResult.trading_allowed` is False.

DESIGN / REUSE (see the "Do NOT rebuild components that already
exist" rule this module was built under):
  - MT5 connectivity, account info, symbol discovery/spec, tick,
    OHLCV, and market-data-freshness checks are NOT reimplemented
    here. They are delegated to app.mt5.selftest.run_mt5_selftest,
    the existing read-only MT5 self-test, and its per-check results
    are translated into this module's required-check taxonomy.
  - News state comes from app.news.filter.NewsStatus.permits_new_trade
    -- the ONE sanctioned way to turn a NewsStatus into a trade/no-
    trade decision (see that module's docstring for why re-deriving
    this boolean anywhere else was a real, shipped bug).
  - SafetyGate / ExecutionSafetyGate are only checked for availability
    here (they are evaluated for real, per-trade, inside TradingLoop
    and the order-execution pipeline -- this module does not call
    .evaluate() on them, since there is no candidate order yet at
    startup time).
  - Kill switch state comes from the existing app.risk.kill_switch.
    KillSwitch file-backed store.

NEVER submits an order. This module does not import the order-
execution engine module at all (matching app/mt5/selftest.py's
structural guarantee), and never calls submit_order / close_position /
close_position_partial / modify_position on any client.

TRADING MODE MAPPING: the existing app.config.settings.TradingMode
enum has three values (BACKTEST, PAPER, LIVE) -- it is NOT changed
here (rule: preserve existing trading modes). This module's spec
describes four conceptual modes (RESEARCH, PAPER, DEMO, LIVE):

    RESEARCH  -> TradingMode.BACKTEST (no live execution; BacktestEngine
                 reads historical data directly and never touches MT5,
                 the journal, the kill switch, or the safety gates, so
                 none of those checks are required in this mode)
    PAPER     -> TradingMode.PAPER (simulated fills; MT5 may be mock or
                 real depending on MT5_USE_MOCK, but connectivity is
                 still verified either way)
    DEMO      -> TradingMode.LIVE, MT5_USE_MOCK=false, and the connected
                 account verified as a DEMO account (see
                 `require_demo_account` below)
    LIVE      -> TradingMode.LIVE, MT5_USE_MOCK=false, and the connected
                 account verified as a REAL-MONEY account

The existing TradingMode enum has no separate DEMO value, so there is
nothing in Settings itself that distinguishes "DEMO" from "LIVE" --
that distinction only exists once a real account is connected and its
`is_demo` flag can be read. This module resolves that distinction via
an explicit `require_demo_account` parameter (default True: safer,
since it means an operator must explicitly opt out to reach real-money
LIVE semantics) supplied by the caller -- not a new Settings field, so
Settings/TradingMode are left completely untouched.
"""
from __future__ import annotations

import enum
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.config.settings import Settings, TradingMode
from app.execution.execution_safety_gate import ExecutionSafetyGate
from app.execution.state_store import ExecutionStateStore
from app.features.engine import InsufficientDataError, compute_features
from app.journal.journal import TradeJournal
from app.market_data.engine import MarketDataEngine, StaleMarketDataError, SymbolDiscoveryError
from app.mt5.interface import IMT5Client
from app.mt5.selftest import run_mt5_selftest
from app.news.calendar import build_news_filter
from app.news.filter import NewsFilter
from app.risk.kill_switch import KillSwitch
from app.safety.gate import SafetyGate

# Sentinel distinct from `None` so callers can explicitly pass
# safety_gate=None / execution_safety_gate=None to simulate "this gate
# is unavailable" in tests, while omitting the argument still means
# "construct the real default gate".
_UNSET = object()


class CheckStatus(str, enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    WARNING = "WARNING"
    NOT_REQUIRED = "NOT_REQUIRED"


class Severity(str, enum.Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass
class StartupCheck:
    name: str
    status: CheckStatus
    severity: Severity
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "severity": self.severity.value,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class StartupSafetyResult:
    overall_status: str  # "TRADING_ALLOWED" | "TRADING_BLOCKED"
    mode: str
    checks: list[StartupCheck]
    failed_checks: list[str]
    warnings: list[str]
    timestamp: datetime
    # True only when the SOLE reason trading is blocked is a
    # deliberately-active, reliably-read kill switch -- the one case
    # the spec allows the application to still start in, since
    # TradingLoop.run_once() already no-ops new-trade cycles for an
    # active kill switch on its own.
    monitoring_only: bool = False

    @property
    def trading_allowed(self) -> bool:
        return self.overall_status == "TRADING_ALLOWED"

    def to_dict(self) -> dict:
        return {
            "overall_status": self.overall_status,
            "mode": self.mode,
            "monitoring_only": self.monitoring_only,
            "checks": [c.to_dict() for c in self.checks],
            "failed_checks": self.failed_checks,
            "warnings": self.warnings,
            "timestamp": self.timestamp.isoformat(),
        }


def _requires_mt5(settings: Settings) -> bool:
    """RESEARCH (BACKTEST) never touches MT5 -- everything else does."""
    return settings.trading_mode != TradingMode.BACKTEST


# ---------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------
def _check_configuration(settings: Settings) -> StartupCheck:
    problems: list[str] = []
    if not settings.symbol_candidate_list():
        problems.append("SYMBOL_CANDIDATES resolves to an empty candidate list.")
    if settings.market_data_max_staleness_seconds <= 0:
        problems.append("MARKET_DATA_MAX_STALENESS_SECONDS must be positive.")
    if settings.trading_mode == TradingMode.LIVE:
        try:
            settings.validate_live_safety()
        except RuntimeError as exc:
            problems.append(str(exc))
    if problems:
        return StartupCheck("configuration", CheckStatus.FAIL, Severity.CRITICAL, "; ".join(problems))
    return StartupCheck("configuration", CheckStatus.PASS, Severity.CRITICAL, "Configuration is valid.")


# ---------------------------------------------------------------------
# 2. Trading mode
# ---------------------------------------------------------------------
def _check_trading_mode(settings: Settings, require_demo_account: bool) -> StartupCheck:
    mode = settings.trading_mode
    details: dict[str, Any] = {"trading_mode": mode.value, "mt5_use_mock": settings.mt5_use_mock}

    if mode == TradingMode.BACKTEST:
        return StartupCheck(
            "trading_mode", CheckStatus.PASS, Severity.CRITICAL,
            "RESEARCH (BACKTEST) mode: no live trading execution.", details,
        )
    if mode == TradingMode.PAPER:
        return StartupCheck(
            "trading_mode", CheckStatus.PASS, Severity.CRITICAL,
            "PAPER mode: mock/simulated execution only.", details,
        )
    if mode == TradingMode.LIVE:
        details["require_demo_account"] = require_demo_account
        if settings.mt5_use_mock:
            return StartupCheck(
                "trading_mode", CheckStatus.FAIL, Severity.CRITICAL,
                "LIVE-family trading mode (DEMO/LIVE) with MT5_USE_MOCK=true is never allowed.",
                details,
            )
        try:
            settings.validate_live_safety()
        except RuntimeError as exc:
            return StartupCheck("trading_mode", CheckStatus.FAIL, Severity.CRITICAL, str(exc), details)
        label = "DEMO" if require_demo_account else "LIVE"
        return StartupCheck(
            "trading_mode", CheckStatus.PASS, Severity.CRITICAL,
            f"LIVE-family trading mode configuration verified for {label} execution "
            "(account type verified separately by account_type_verification).",
            details,
        )
    return StartupCheck("trading_mode", CheckStatus.FAIL, Severity.CRITICAL, f"Unrecognized trading mode: {mode!r}", details)


# ---------------------------------------------------------------------
# 3-7. MT5 connectivity / symbol availability / symbol permissions /
#      account information / market-data freshness -- delegated to the
#      existing read-only app.mt5.selftest.run_mt5_selftest.
# ---------------------------------------------------------------------
_MT5_BLOCK_NAMES = (
    "mt5_connectivity",
    "account_information",
    "symbol_availability",
    "symbol_trading_permissions",
    "market_data_freshness",
)


def _not_required_mt5_block(message: str) -> list[StartupCheck]:
    return [StartupCheck(name, CheckStatus.NOT_REQUIRED, Severity.INFO, message, {}) for name in _MT5_BLOCK_NAMES]


def _failed_mt5_block(message: str) -> list[StartupCheck]:
    return [StartupCheck(name, CheckStatus.FAIL, Severity.CRITICAL, message, {}) for name in _MT5_BLOCK_NAMES]


def _combine_mt5_sub_checks(by_name: dict, names: tuple[str, ...]) -> tuple[CheckStatus, str, dict]:
    """Combine one or more app.mt5.selftest SelfTestCheck rows into a
    single StartupCheck's (status, message, details). FAIL beats
    BLOCKED beats PASS, matching run_mt5_selftest's own fail-closed
    ordering."""
    present = [by_name[n] for n in names if n in by_name]
    if not present:
        return CheckStatus.FAIL, f"Expected self-test check(s) {names} were not produced.", {}
    statuses = {c.status for c in present}
    if "FAIL" in statuses:
        combined = CheckStatus.FAIL
    elif "BLOCKED" in statuses:
        combined = CheckStatus.BLOCKED
    else:
        combined = CheckStatus.PASS
    message = "; ".join(f"{c.name}: {c.detail}" for c in present)
    details = {c.name: {"status": c.status, "detail": c.detail, **c.data} for c in present}
    return combined, message, details


def _check_mt5_block(settings: Settings, client: Optional[IMT5Client]) -> list[StartupCheck]:
    if not _requires_mt5(settings):
        return _not_required_mt5_block("RESEARCH (BACKTEST) mode does not require MT5.")

    if client is None:
        return _failed_mt5_block(
            "This trading mode requires an MT5 client, but none was provided to the startup safety check."
        )

    try:
        report = run_mt5_selftest(client, settings)
    except Exception as exc:  # noqa: BLE001 - the self-test itself must never crash startup silently
        return _failed_mt5_block(
            f"MT5 self-test raised unexpectedly and is treated as a failure: {type(exc).__name__}: {exc}"
        )

    by_name = {c.name: c for c in report.checks}

    status, message, details = _combine_mt5_sub_checks(by_name, ("mt5_terminal_connection",))
    conn_check = StartupCheck("mt5_connectivity", status, Severity.CRITICAL, message or "MT5 connectivity not reported.", details)

    status, message, details = _combine_mt5_sub_checks(by_name, ("account_info",))
    account_check = StartupCheck("account_information", status, Severity.CRITICAL, message or "Account information not reported.", details)

    status, message, details = _combine_mt5_sub_checks(by_name, ("symbol_discovery",))
    symbol_check = StartupCheck("symbol_availability", status, Severity.CRITICAL, message or "Symbol availability not reported.", details)

    status, message, details = _combine_mt5_sub_checks(
        by_name, ("symbol_specification", "volume_constraints", "stop_level_constraints")
    )
    perms_check = StartupCheck("symbol_trading_permissions", status, Severity.CRITICAL, message or "Symbol permissions not reported.", details)

    status, message, details = _combine_mt5_sub_checks(
        by_name, ("tick", "h4_data", "h1_data", "m15_data", "market_data_freshness")
    )
    market_data_check = StartupCheck("market_data_freshness", status, Severity.CRITICAL, message or "Market data freshness not reported.", details)

    return [conn_check, account_check, symbol_check, perms_check, market_data_check]


# ---------------------------------------------------------------------
# Bonus: account type verification for the DEMO vs LIVE distinction
# (see module docstring -- not a numbered spec check on its own, but
# required to implement the DEMO/LIVE test matrix without touching
# TradingMode).
# ---------------------------------------------------------------------
def _check_account_type(settings: Settings, client: Optional[IMT5Client], require_demo_account: bool) -> StartupCheck:
    if settings.trading_mode != TradingMode.LIVE:
        return StartupCheck(
            "account_type_verification", CheckStatus.NOT_REQUIRED, Severity.INFO,
            "Only applicable to LIVE-family (DEMO/LIVE) trading mode.", {},
        )
    if client is None:
        return StartupCheck(
            "account_type_verification", CheckStatus.FAIL, Severity.CRITICAL,
            "No MT5 client available to verify account type.", {},
        )
    try:
        account = client.get_account_info()
    except Exception as exc:  # noqa: BLE001
        return StartupCheck(
            "account_type_verification", CheckStatus.FAIL, Severity.CRITICAL,
            f"Could not fetch account info to verify account type: {type(exc).__name__}: {exc}", {},
        )

    is_demo = account.is_demo
    details = {"is_demo": is_demo, "require_demo_account": require_demo_account}

    if require_demo_account:
        if is_demo is True:
            return StartupCheck("account_type_verification", CheckStatus.PASS, Severity.CRITICAL,
                                 "Broker confirms this is a DEMO account.", details)
        if is_demo is False:
            return StartupCheck("account_type_verification", CheckStatus.FAIL, Severity.CRITICAL,
                                 "DEMO mode requires a DEMO account, but the broker reports a REAL-MONEY account.",
                                 details)
        return StartupCheck(
            "account_type_verification", CheckStatus.FAIL, Severity.CRITICAL,
            "DEMO mode requires a verified DEMO account; the broker did not report a determinable "
            "account type (unknown is treated as unsafe, not assumed demo).", details,
        )
    else:
        if is_demo is False:
            return StartupCheck("account_type_verification", CheckStatus.PASS, Severity.CRITICAL,
                                 "Broker confirms this is a REAL-MONEY account.", details)
        if is_demo is True:
            return StartupCheck("account_type_verification", CheckStatus.FAIL, Severity.CRITICAL,
                                 "LIVE mode requires a verified REAL-MONEY account, but the broker reports "
                                 "a DEMO account.", details)
        return StartupCheck(
            "account_type_verification", CheckStatus.FAIL, Severity.CRITICAL,
            "LIVE mode requires a verified account type; the broker did not report a determinable "
            "account type (unknown is treated as unsafe, not assumed real).", details,
        )


# ---------------------------------------------------------------------
# 8. Required indicators / features
# ---------------------------------------------------------------------
def _check_features(settings: Settings, client: Optional[IMT5Client]) -> StartupCheck:
    if not _requires_mt5(settings):
        return StartupCheck("required_features", CheckStatus.NOT_REQUIRED, Severity.INFO,
                             "RESEARCH (BACKTEST) mode does not require live features.", {})

    any_strategy_enabled = (
        settings.strategy_trend_pullback_enabled
        or settings.strategy_breakout_enabled
        or settings.strategy_mean_reversion_enabled
    )
    if not any_strategy_enabled:
        return StartupCheck("required_features", CheckStatus.NOT_REQUIRED, Severity.INFO,
                             "No strategies are enabled; no live features are required.", {})

    if client is None:
        return StartupCheck("required_features", CheckStatus.FAIL, Severity.CRITICAL,
                             "No MT5 client available to compute required features.", {})

    market_data = MarketDataEngine(client, settings)
    problems: list[str] = []
    checked: list[str] = []

    for tf in (settings.tf_trend.value, settings.tf_structure.value, settings.tf_entry.value):
        try:
            df = market_data.get_ohlcv(tf, count=250)
        except StaleMarketDataError as exc:
            problems.append(f"{tf}: stale market data ({exc})")
            continue
        except SymbolDiscoveryError as exc:
            problems.append(f"{tf}: symbol discovery failed ({exc})")
            continue
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{tf}: could not fetch OHLCV ({type(exc).__name__}: {exc})")
            continue

        try:
            snapshot = compute_features(df, tf)
        except InsufficientDataError as exc:
            problems.append(f"{tf}: insufficient history for required features ({exc})")
            continue
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{tf}: feature computation failed ({type(exc).__name__}: {exc})")
            continue

        required_values = {
            "trend.adx": snapshot.get("trend", {}).get("adx"),
            "momentum.rsi": snapshot.get("momentum", {}).get("rsi"),
            "volatility.atr": snapshot.get("volatility", {}).get("atr"),
            "volatility.atr_pct": snapshot.get("volatility", {}).get("atr_pct"),
            "close": snapshot.get("close"),
        }
        for key, val in required_values.items():
            if val is None:
                problems.append(f"{tf}: required feature '{key}' is missing/NaN.")
            elif isinstance(val, float) and not math.isfinite(val):
                problems.append(f"{tf}: required feature '{key}' is not finite ({val}).")

        checked.append(tf)

    if problems:
        return StartupCheck("required_features", CheckStatus.FAIL, Severity.CRITICAL, "; ".join(problems),
                             {"checked_timeframes": checked})
    return StartupCheck("required_features", CheckStatus.PASS, Severity.CRITICAL,
                         f"Required features available and valid for {checked}.", {"checked_timeframes": checked})


# ---------------------------------------------------------------------
# 9. News provider state
# ---------------------------------------------------------------------
def _check_news(settings: Settings, news_filter: Optional[NewsFilter], now: datetime) -> StartupCheck:
    if not _requires_mt5(settings):
        return StartupCheck("news_state", CheckStatus.NOT_REQUIRED, Severity.INFO,
                             "RESEARCH (BACKTEST) mode: the news gate is not part of the live startup path "
                             "(BacktestEngine's own news handling is internal to that engine and is left "
                             "unchanged here).", {})

    nf = news_filter if news_filter is not None else build_news_filter(
        settings.news_calendar_path,
        minutes_before=settings.news_blackout_minutes_before,
        minutes_after=settings.news_blackout_minutes_after,
    )
    try:
        status = nf.check(now)
    except Exception as exc:  # noqa: BLE001 - news provider failure must fail closed, never silently pass
        return StartupCheck("news_state", CheckStatus.FAIL, Severity.CRITICAL,
                             f"News filter raised unexpectedly and is treated as blocked: {type(exc).__name__}: {exc}",
                             {})

    details = {"state": status.state.value, "reason": status.reason}
    # The ONLY sanctioned way to turn a NewsStatus into a trade/no-trade
    # decision -- see app.news.filter's module docstring. Never
    # recompute this boolean by hand here.
    if status.permits_new_trade:
        return StartupCheck("news_state", CheckStatus.PASS, Severity.CRITICAL, status.reason, details)
    return StartupCheck("news_state", CheckStatus.FAIL, Severity.CRITICAL, status.reason, details)


# ---------------------------------------------------------------------
# 10. Journal writable
# ---------------------------------------------------------------------
def _check_journal(settings: Settings, journal: Optional[TradeJournal]) -> StartupCheck:
    db_path = settings.database_url.replace("sqlite:///", "")
    try:
        jr = journal if journal is not None else TradeJournal(db_path)
        # A lightweight, self-cleaning write probe against the SAME
        # sqlite file/connection factory the journal itself uses --
        # this actually exercises write access (schema creation alone
        # is not proof: a read-only file can still pass "CREATE TABLE
        # IF NOT EXISTS" if the table already exists).
        with jr._connect() as conn:  # noqa: SLF001 - reusing the journal's own connection factory, not a new sqlite path
            conn.execute("CREATE TABLE IF NOT EXISTS _startup_writability_probe (id INTEGER PRIMARY KEY)")
            conn.execute("DELETE FROM _startup_writability_probe")
            conn.execute("INSERT INTO _startup_writability_probe (id) VALUES (1)")
            conn.execute("DELETE FROM _startup_writability_probe")
    except (sqlite3.Error, OSError) as exc:
        return StartupCheck("journal_writable", CheckStatus.FAIL, Severity.CRITICAL,
                             f"Journal storage is not writable: {type(exc).__name__}: {exc}", {"db_path": db_path})
    except Exception as exc:  # noqa: BLE001 - any other failure here also fails closed
        return StartupCheck("journal_writable", CheckStatus.FAIL, Severity.CRITICAL,
                             f"Journal writability check failed unexpectedly: {type(exc).__name__}: {exc}",
                             {"db_path": db_path})
    return StartupCheck("journal_writable", CheckStatus.PASS, Severity.CRITICAL,
                         "Journal storage is writable.", {"db_path": db_path})


# ---------------------------------------------------------------------
# 11. Kill switch state
# ---------------------------------------------------------------------
def _check_kill_switch(settings: Settings, kill_switch: Optional[KillSwitch]) -> StartupCheck:
    ks = kill_switch if kill_switch is not None else KillSwitch(default_active=False)
    try:
        state = ks.status()
    except Exception as exc:  # noqa: BLE001 - unreadable kill-switch state must fail closed
        return StartupCheck("kill_switch", CheckStatus.FAIL, Severity.CRITICAL,
                             f"Could not reliably read the kill-switch state: {type(exc).__name__}: {exc}", {})

    active = bool(state.active) or bool(settings.kill_switch)
    details = {"active": active, "reason": state.reason, "config_kill_switch": settings.kill_switch}

    if active:
        # Deliberately BLOCKED (not FAIL): the state WAS read
        # reliably, it just says "no new trades". TradingLoop.run_once()
        # already no-ops new-entry evaluation while the kill switch is
        # active, so the application may still start in this
        # monitoring/read-only state (see module docstring).
        return StartupCheck("kill_switch", CheckStatus.BLOCKED, Severity.WARNING,
                             "Kill switch is active. No new trades will be submitted.", details)
    return StartupCheck("kill_switch", CheckStatus.PASS, Severity.CRITICAL, "Kill switch is not active.", details)


# ---------------------------------------------------------------------
# 12. Risk configuration
# ---------------------------------------------------------------------
def _check_risk_config(settings: Settings) -> StartupCheck:
    risk = settings.risk
    problems: list[str] = []

    # Field-level ranges (risk-per-trade, daily/weekly loss limits, max
    # trades/day, max open positions, max consecutive losses, max
    # spread, max slippage, min equity) are already enforced by
    # RiskSettings' pydantic Field(...) bounds at construction time --
    # if we have a `settings.risk` object at all, those already hold.
    # What is NOT enforced there is cross-field consistency, which is
    # what we validate here (never silently clamped -- rejected).
    if risk.risk_per_trade_pct > risk.max_daily_loss_pct:
        problems.append(
            f"risk_per_trade_pct ({risk.risk_per_trade_pct}) exceeds max_daily_loss_pct "
            f"({risk.max_daily_loss_pct}); a single losing trade could breach the daily loss limit."
        )
    if risk.max_daily_loss_pct > risk.max_weekly_loss_pct:
        problems.append(
            f"max_daily_loss_pct ({risk.max_daily_loss_pct}) exceeds max_weekly_loss_pct "
            f"({risk.max_weekly_loss_pct}); a single day could breach the weekly loss limit."
        )

    if problems:
        return StartupCheck("risk_configuration", CheckStatus.FAIL, Severity.CRITICAL, "; ".join(problems),
                             risk.model_dump())
    return StartupCheck("risk_configuration", CheckStatus.PASS, Severity.CRITICAL,
                         "Risk configuration is within bounds and internally consistent.", risk.model_dump())


# ---------------------------------------------------------------------
# 13. Safety configuration (SafetyGate / ExecutionSafetyGate)
# ---------------------------------------------------------------------
def _check_safety_gate(safety_gate) -> StartupCheck:
    if safety_gate is None:
        return StartupCheck("safety_gate", CheckStatus.FAIL, Severity.CRITICAL, "SafetyGate is unavailable.", {})
    if not callable(getattr(safety_gate, "evaluate", None)):
        return StartupCheck("safety_gate", CheckStatus.FAIL, Severity.CRITICAL,
                             "SafetyGate does not implement the expected evaluate() interface.", {})
    return StartupCheck("safety_gate", CheckStatus.PASS, Severity.CRITICAL,
                         "SafetyGate is available and initialized.", {})


def _check_execution_gate(execution_safety_gate) -> StartupCheck:
    if execution_safety_gate is None:
        return StartupCheck("execution_gate", CheckStatus.FAIL, Severity.CRITICAL,
                             "ExecutionSafetyGate is unavailable.", {})
    if not callable(getattr(execution_safety_gate, "evaluate", None)):
        return StartupCheck("execution_gate", CheckStatus.FAIL, Severity.CRITICAL,
                             "ExecutionSafetyGate does not implement the expected evaluate() interface.", {})
    return StartupCheck("execution_gate", CheckStatus.PASS, Severity.CRITICAL,
                         "ExecutionSafetyGate is available and initialized.", {})


# ---------------------------------------------------------------------
# 13b. Execution recovery (Phase 7): unresolved UNKNOWN executions
# ---------------------------------------------------------------------
def _check_execution_recovery(execution_state_store: Optional[ExecutionStateStore]) -> StartupCheck:
    """Surfaces any UNKNOWN/uncertain executions left over from a
    previous run (see app.execution.state_store) so an operator never
    silently loses track of them across a restart. Deliberately
    WARNING severity, not CRITICAL: the order-execution layer that
    owns this state store already fail-closes at the point of
    resubmission for the specific client_order_id(s) involved (its
    own duplicate check runs before any resubmission attempt reaches
    the broker), so this check's job is to make unresolved executions
    visible, not to halt the whole process -- which would also stop
    position monitoring for any legitimately open positions.
    execution_state_store is optional and defaults to None (unlike
    safety_gate/execution_safety_gate, this has no free-constructed
    default) so this check is NOT_REQUIRED unless the caller opts in
    by passing one, matching the pre-Phase-7 startup check contract
    exactly for anyone who doesn't."""
    if execution_state_store is None:
        return StartupCheck("execution_recovery", CheckStatus.NOT_REQUIRED, Severity.INFO,
                             "No persistent execution state store was provided; nothing to reconcile.", {})
    try:
        unresolved = execution_state_store.unresolved()
    except Exception as exc:  # noqa: BLE001 - can't read persisted state -> fail closed
        return StartupCheck(
            "execution_recovery", CheckStatus.FAIL, Severity.CRITICAL,
            f"Could not read persisted execution state: {type(exc).__name__}: {exc}. Recovery cannot safely "
            "proceed until this is resolved.", {},
        )
    if unresolved:
        ids = [r.client_order_id for r in unresolved]
        return StartupCheck(
            "execution_recovery", CheckStatus.WARNING, Severity.WARNING,
            f"{len(unresolved)} execution(s) from a previous run have an UNKNOWN/uncertain outcome and "
            "require reconciliation before their client_order_id can be reused for a new order: "
            + ", ".join(ids[:10]) + (", ..." if len(ids) > 10 else ""),
            {"unresolved_client_order_ids": ids},
        )
    return StartupCheck("execution_recovery", CheckStatus.PASS, Severity.INFO,
                         "No unresolved executions pending reconciliation.", {})


# ---------------------------------------------------------------------
# 14. Clock / time consistency
# ---------------------------------------------------------------------
def _check_clock(now: datetime, max_skew_seconds: float = 300.0) -> StartupCheck:
    if now.tzinfo is None:
        return StartupCheck(
            "clock", CheckStatus.FAIL, Severity.CRITICAL,
            "Time reference is naive (no timezone). A safety-critical startup check must never compare "
            "naive and timezone-aware timestamps.", {},
        )
    system_utc = datetime.now(timezone.utc)
    skew_seconds = abs((system_utc - now).total_seconds())
    details = {
        "system_utc": system_utc.isoformat(),
        "reference_utc": now.isoformat(),
        "skew_seconds": skew_seconds,
        "max_skew_seconds": max_skew_seconds,
    }
    if skew_seconds > max_skew_seconds:
        return StartupCheck("clock", CheckStatus.FAIL, Severity.CRITICAL,
                             f"Clock skew of {skew_seconds:.0f}s exceeds the maximum trusted skew of "
                             f"{max_skew_seconds:.0f}s.", details)
    return StartupCheck("clock", CheckStatus.PASS, Severity.CRITICAL,
                         f"System clock is timezone-aware and within tolerance (skew={skew_seconds:.1f}s).",
                         details)


# ---------------------------------------------------------------------
# 15. Dependency health
# ---------------------------------------------------------------------
def _check_dependencies(settings: Settings, client: Optional[IMT5Client]) -> StartupCheck:
    """Core data-pipeline dependencies, plus MT5 client presence when
    required. Deliberately does NOT re-check the real `MetaTrader5`
    package's importability here: app.mt5.factory.build_mt5_client
    already fails closed on that (it lazily imports app.mt5.real_client,
    which imports the MetaTrader5 package, only when MT5_USE_MOCK is
    false) -- by the time a `client` object exists at all, that
    dependency was already resolved. Re-checking it here would just
    duplicate that failure mode while coupling this check to whether
    the real package happens to be installed in whatever environment
    is running the check (e.g. a mock-only CI/dev sandbox), for no
    additional safety benefit."""
    problems: list[str] = []
    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Core data dependency unavailable: {type(exc).__name__}: {exc}")

    if _requires_mt5(settings) and client is None:
        problems.append("An MT5 client dependency is required for this trading mode but was not provided.")

    if problems:
        return StartupCheck("dependency_health", CheckStatus.FAIL, Severity.CRITICAL, "; ".join(problems), {})
    return StartupCheck("dependency_health", CheckStatus.PASS, Severity.CRITICAL,
                         "Required runtime dependencies are available.", {})


# ---------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------
def _run_startup_safety_check(
    settings: Settings,
    client: Optional[IMT5Client],
    journal: Optional[TradeJournal],
    kill_switch: Optional[KillSwitch],
    news_filter: Optional[NewsFilter],
    safety_gate,
    execution_safety_gate,
    require_demo_account: bool,
    now: datetime,
    execution_state_store: Optional[ExecutionStateStore] = None,
) -> StartupSafetyResult:
    checks: list[StartupCheck] = []

    checks.append(_check_configuration(settings))
    checks.append(_check_trading_mode(settings, require_demo_account))
    checks.extend(_check_mt5_block(settings, client))
    checks.append(_check_account_type(settings, client, require_demo_account))
    checks.append(_check_features(settings, client))
    checks.append(_check_news(settings, news_filter, now))
    checks.append(_check_journal(settings, journal))

    kill_check = _check_kill_switch(settings, kill_switch)
    checks.append(kill_check)

    checks.append(_check_risk_config(settings))

    sg = SafetyGate() if safety_gate is _UNSET else safety_gate
    esg = ExecutionSafetyGate() if execution_safety_gate is _UNSET else execution_safety_gate
    checks.append(_check_safety_gate(sg))
    checks.append(_check_execution_gate(esg))
    checks.append(_check_execution_recovery(execution_state_store))

    checks.append(_check_clock(now))
    checks.append(_check_dependencies(settings, client))

    # Any CRITICAL-severity check that is FAIL *or* BLOCKED is a hard
    # blocker. Deliberately treats BLOCKED the same as FAIL here (not
    # just FAIL): a critical check can legitimately be reported
    # BLOCKED rather than FAIL when it's downstream of an earlier
    # dependency failure (e.g. symbol_trading_permissions is BLOCKED
    # when symbol discovery itself failed) -- that must still block
    # trading. The ONLY check allowed to be BLOCKED without blocking
    # trading outright is kill_switch, and that's because its severity
    # is WARNING, not CRITICAL, precisely to carve out that one
    # deliberate exception (see _check_kill_switch).
    critical_blocking = [
        c for c in checks if c.severity == Severity.CRITICAL and c.status in (CheckStatus.FAIL, CheckStatus.BLOCKED)
    ]
    kill_switch_blocking = kill_check.status == CheckStatus.BLOCKED

    if critical_blocking:
        overall = "TRADING_BLOCKED"
        monitoring_only = False
    elif kill_switch_blocking:
        overall = "TRADING_BLOCKED"
        monitoring_only = True
    else:
        overall = "TRADING_ALLOWED"
        monitoring_only = False

    failed_checks = [c.name for c in checks if c.status in (CheckStatus.FAIL, CheckStatus.BLOCKED)]
    warnings = [f"{c.name}: {c.message}" for c in checks if c.status == CheckStatus.WARNING]

    return StartupSafetyResult(
        overall_status=overall,
        mode=settings.trading_mode.value,
        checks=checks,
        failed_checks=failed_checks,
        warnings=warnings,
        timestamp=now,
        monitoring_only=monitoring_only,
    )


def run_startup_safety_check(
    settings: Settings,
    client: Optional[IMT5Client] = None,
    journal: Optional[TradeJournal] = None,
    kill_switch: Optional[KillSwitch] = None,
    news_filter: Optional[NewsFilter] = None,
    safety_gate=_UNSET,
    execution_safety_gate=_UNSET,
    require_demo_account: bool = True,
    now: Optional[datetime] = None,
    execution_state_store: Optional[ExecutionStateStore] = None,
) -> StartupSafetyResult:
    """Run every startup safety check and return a fail-closed,
    deterministic StartupSafetyResult.

    This function NEVER raises for an unsafe/invalid startup state --
    every failure mode is captured as a FAIL/BLOCKED check and reflected
    in `overall_status`. If something inside this function itself
    misbehaves in a way none of the individual checks anticipated, the
    whole result still fails closed (TRADING_BLOCKED), mirroring
    SafetyGate/ExecutionSafetyGate's "any internal error is itself a
    rejection" rule.

    Args:
        settings: application Settings (already constructed/validated
            by pydantic itself).
        client: the connected (or connectable) IMT5Client to check.
            May be None only for RESEARCH (BACKTEST) mode.
        journal: existing TradeJournal to probe for writability. If
            omitted, one is constructed from settings.database_url.
        kill_switch: existing KillSwitch to read. If omitted, a
            default (inactive) one is constructed.
        news_filter: existing NewsFilter to check. If omitted, one is
            built the same way app.runtime.loop.TradingLoop and
            app.api.state.build_app_state do (build_news_filter).
        safety_gate / execution_safety_gate: pass an explicit instance
            (including None, to simulate "unavailable") to control
            availability checking in tests. Omit to construct the real
            default gate.
        require_demo_account: only meaningful when
            settings.trading_mode is LIVE. True (default) means this
            LIVE-family run is expected to be DEMO -- the connected
            account must verify as a demo account. False means this is
            genuine real-money LIVE -- the connected account must
            verify as a real-money account. An operator must
            explicitly pass False to reach real-money LIVE semantics.
        now: reference timestamp for the news and clock checks.
            Defaults to the current UTC time.
        execution_state_store: existing ExecutionStateStore (Phase 7) to
            check for unresolved UNKNOWN executions left over from a
            previous run. Omit (default None) to skip this check
            entirely (NOT_REQUIRED) -- matching the pre-Phase-7
            contract for anyone who doesn't opt in.
    """
    now = now or datetime.now(timezone.utc)
    try:
        return _run_startup_safety_check(
            settings, client, journal, kill_switch, news_filter,
            safety_gate, execution_safety_gate, require_demo_account, now,
            execution_state_store=execution_state_store,
        )
    except Exception as exc:  # noqa: BLE001 - the orchestrator itself must fail closed, never propagate
        mode_value = "UNKNOWN"
        try:
            mode_value = settings.trading_mode.value
        except Exception:  # noqa: BLE001
            pass
        check = StartupCheck(
            "startup_safety_check", CheckStatus.FAIL, Severity.CRITICAL,
            f"The startup safety check itself failed unexpectedly and is failing closed: "
            f"{type(exc).__name__}: {exc}", {},
        )
        return StartupSafetyResult(
            overall_status="TRADING_BLOCKED", mode=mode_value, checks=[check],
            failed_checks=["startup_safety_check"], warnings=[], timestamp=now, monitoring_only=False,
        )


_LABELS = {
    "configuration": "Configuration",
    "trading_mode": "Trading Mode",
    "mt5_connectivity": "MT5",
    "account_information": "Account",
    "account_type_verification": "Account Type",
    "symbol_availability": "Symbol",
    "symbol_trading_permissions": "Symbol Perms",
    "market_data_freshness": "Market Data",
    "required_features": "Features",
    "news_state": "News",
    "journal_writable": "Journal",
    "kill_switch": "Kill Switch",
    "risk_configuration": "Risk Config",
    "safety_gate": "Safety Gate",
    "execution_gate": "Execution Gate",
    "execution_recovery": "Execution Recovery",
    "clock": "Clock",
    "dependency_health": "Dependencies",
}


def format_startup_report_text(result: StartupSafetyResult) -> str:
    lines = [
        "=" * 70,
        "STARTUP SAFETY CHECK",
        "=" * 70,
        f"Generated:      {result.timestamp.isoformat()}",
        f"TRADING_MODE:   {result.mode}",
        "-" * 70,
    ]
    for c in result.checks:
        label = _LABELS.get(c.name, c.name)
        lines.append(f"{label:<20} {c.status.value}")
    lines.append("-" * 70)
    lines.append("RESULT:")
    lines.append("TRADING ALLOWED" if result.trading_allowed else "TRADING BLOCKED")
    if result.monitoring_only:
        lines.append("(monitoring-only: kill switch active, no new trades will be submitted)")
    if result.failed_checks:
        lines.append("")
        lines.append("Reason:")
        for name in result.failed_checks:
            check = next((c for c in result.checks if c.name == name), None)
            if check is not None:
                lines.append(f"  {_LABELS.get(name, name)}: {check.message}")
    lines.append("=" * 70)
    return "\n".join(lines)
