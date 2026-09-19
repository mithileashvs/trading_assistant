"""
Shared application state for the FastAPI dashboard (section 33).

Built once at startup and reused across requests — recreating the MT5
connection, journal, etc. per-request would be wasteful and would also
break the kill switch / position-monitor state that needs to persist
across calls.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.ai.llm_client import build_llm_client
from app.config.settings import Settings, TradingMode, get_settings
from app.execution.engine import ExecutionEngine
from app.execution.state_store import SqliteExecutionStateStore
from app.journal.journal import TradeJournal
from app.market_data.engine import MarketDataEngine
from app.mt5.factory import build_mt5_client
from app.mt5.interface import IMT5Client, SymbolSpec
from app.news.calendar import build_news_filter
from app.paper.runtime import PaperTradingRuntime
from app.positions.monitor import PositionMonitor
from app.positions.state_store import SqlitePositionMonitorStateStore
from app.regimes.thresholds import RegimeThresholds
from app.risk.guards import RiskGuardEngine
from app.risk.kill_switch import KillSwitch
from app.risk.validator import TradeValidator
from app.safety.startup_check import StartupSafetyResult, run_startup_safety_check
from app.strategies.selector import StrategySelector
from app.strategy_lab.store import ResearchStore, SqliteResearchStore


@dataclass
class AppState:
    settings: Settings
    client: IMT5Client
    symbol_spec: SymbolSpec
    market_data: MarketDataEngine
    selector: StrategySelector
    validator: TradeValidator
    journal: TradeJournal
    kill_switch: KillSwitch
    execution_engine: ExecutionEngine
    position_monitor: PositionMonitor
    regime_thresholds: RegimeThresholds | None = None
    startup_safety: StartupSafetyResult | None = None
    research_store: ResearchStore | None = None
    paper_runtime: PaperTradingRuntime | None = None


def build_app_state(settings: Settings | None = None) -> AppState:
    settings = settings or get_settings()
    client = build_mt5_client(settings)
    client.connect()

    symbol = client.discover_symbol(settings.symbol_candidate_list())
    if symbol is None:
        raise RuntimeError("No tradable symbol found among the configured candidates.")
    spec = client.get_symbol_spec(symbol)

    market_data = MarketDataEngine(client, settings)
    journal = TradeJournal(settings.database_url.replace("sqlite:///", ""))
    kill_switch = KillSwitch(default_active=False)
    news_filter = build_news_filter(
        settings.news_calendar_path,
        minutes_before=settings.news_blackout_minutes_before,
        minutes_after=settings.news_blackout_minutes_after,
    )
    validator = TradeValidator(settings.risk, guard_engine=RiskGuardEngine(settings.risk), news_filter=news_filter)
    # Phase 7: same persistent store TradingLoop uses (see app.runtime.loop),
    # for consistency -- this dashboard is read-only and doesn't submit
    # orders (see AppState.startup_safety's docstring above), but should
    # still observe the real, shared execution-recovery state rather than
    # an empty in-memory one, the same way its KillSwitch above already
    # shares TradingLoop's default state file.
    execution_state_store = SqliteExecutionStateStore(settings.execution_state_db_path)
    execution_engine = ExecutionEngine(client, spec, settings.trading_mode, state_store=execution_state_store)
    # Phase 8: same reasoning as execution_state_store immediately above
    # -- observe the real, shared position-monitor state (breakeven/
    # partial-exit/trailing idempotency, any unresolved UNKNOWN action)
    # rather than an empty in-memory one.
    position_monitor_state_store = SqlitePositionMonitorStateStore(settings.position_monitor_state_db_path)
    position_monitor = PositionMonitor(state_store=position_monitor_state_store)

    # Observability only -- see AppState.startup_safety's docstring.
    # A failed/blocked result here does not stop the dashboard from
    # starting; it is surfaced via GET /api/startup-safety instead.
    startup_safety = run_startup_safety_check(
        settings, client=client, journal=journal, kill_switch=kill_switch, news_filter=news_filter,
        execution_state_store=execution_state_store,
    )

    research_store = SqliteResearchStore(settings.database_url.replace("sqlite:///", ""))

    paper_runtime = None
    if settings.trading_mode != TradingMode.LIVE:
        try:
            paper_runtime = PaperTradingRuntime(
                symbol_spec=spec,
                settings=settings,
                risk_settings=settings.risk,
                journal=journal,
                kill_switch=kill_switch,
                position_monitor=position_monitor,
                trading_mode=TradingMode.PAPER,
            )
        except Exception:
            paper_runtime = None

    return AppState(
        settings=settings,
        client=client,
        symbol_spec=spec,
        market_data=market_data,
        selector=StrategySelector(),
        validator=validator,
        journal=journal,
        kill_switch=kill_switch,
        execution_engine=execution_engine,
        position_monitor=position_monitor,
        startup_safety=startup_safety,
        research_store=research_store,
        paper_runtime=paper_runtime,
    )
