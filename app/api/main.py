"""
FastAPI Dashboard (section 33).

Every endpoint reads from the SAME components the trading loop uses
(MarketDataEngine, StrategySelector, TradeValidator, TradeJournal,
KillSwitch) — nothing here is a separate mock data path. Endpoints
recompute live on each request rather than caching, since this is a
low-frequency (human-refresh-rate) dashboard, not a market-data feed.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.ai.explain import explain_trade
from app.ai.query import net_pnl_by_strategy, win_rate_for_strategy
from app.api.performance import compute_live_performance
from app.api.state import AppState, build_app_state
from app.config.settings import TradingMode
from app.features.engine import InsufficientDataError, compute_features
from app.market_data.engine import StaleMarketDataError
from app.paper.metrics import compute_paper_metrics
from app.paper.runtime import PaperTradingRuntime
from app.risk.guards import GuardCheckInput
from app.strategies.context import build_context
from app.strategy_lab.models import ExperimentConfig
from app.strategy_lab.runner import StrategyLabRunner
from app.strategy_lab.store import SqliteResearchStore

_state: AppState | None = None


def get_state() -> AppState:
    global _state
    if _state is None:
        _state = build_app_state()
    return _state


@asynccontextmanager
async def _lifespan(app: FastAPI):
    get_state()
    yield


app = FastAPI(title="XAU/USD Trading Assistant Dashboard", version="1.0", lifespan=_lifespan)


@app.get("/")
def index():
    static_path = Path(__file__).parent / "static" / "dashboard.html"
    if static_path.exists():
        return FileResponse(static_path)
    return JSONResponse({"message": "Dashboard UI not found; use the JSON endpoints directly."})


@app.get("/api/startup-safety")
def startup_safety_endpoint():
    """Read-only observability endpoint (see app/safety/startup_check.py
    and AppState.startup_safety's docstring). This reports the result
    computed once at dashboard startup -- it does not itself gate or
    permit trading; the authoritative gate lives in
    scripts/run_paper_trading.py, which this endpoint has no path to
    influence."""
    state = get_state()
    if state.startup_safety is None:
        return JSONResponse({"message": "Startup safety check has not been run for this process."})
    return state.startup_safety.to_dict()


@app.get("/api/account")
def account_endpoint():
    state = get_state()
    account = state.client.get_account_info()
    day_start = datetime.combine(datetime.now(timezone.utc).date(), dtime.min, tzinfo=timezone.utc)
    trades_today_rows = state.journal.trades_since(day_start)
    daily_pnl = sum(t.pnl for t in trades_today_rows)
    return {
        "login": account.login, "balance": account.balance, "equity": account.equity,
        "margin": account.margin, "margin_free": account.margin_free, "currency": account.currency,
        "leverage": account.leverage, "trade_allowed": account.trade_allowed, "is_demo": account.is_demo,
        "daily_pnl": daily_pnl,
    }


@app.get("/api/market")
def market_endpoint():
    state = get_state()
    try:
        tick = state.market_data.get_tick()
        h4_df = state.market_data.get_ohlcv("H4", 250)
        h4_features = compute_features(h4_df, "H4")
    except (StaleMarketDataError, InsufficientDataError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    from app.regimes.detector import detect_regime
    regime = detect_regime(h4_features, state.regime_thresholds)

    return {
        "symbol": state.symbol_spec.name, "bid": tick.bid, "ask": tick.ask, "spread": tick.spread,
        "regime": regime.regime.value, "regime_confidence": regime.confidence, "regime_reasons": regime.reasons,
        "atr": h4_features["volatility"].get("atr"), "atr_pct": h4_features["volatility"].get("atr_pct"),
        "adx": h4_features["trend"].get("adx"), "rsi": h4_features["momentum"].get("rsi"),
        "trend_direction": (
            "BULLISH" if h4_features["trend"].get("bullish_aligned")
            else "BEARISH" if h4_features["trend"].get("bearish_aligned") else "NEUTRAL"
        ),
    }


@app.get("/api/signal")
def signal_endpoint():
    state = get_state()
    try:
        context = build_context(state.market_data, state.regime_thresholds, bars=250)
    except (StaleMarketDataError, InsufficientDataError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    best = state.selector.best_signal(context)
    account = state.client.get_account_info()
    day_start = datetime.combine(datetime.now(timezone.utc).date(), dtime.min, tzinfo=timezone.utc)
    week_start = datetime.now(timezone.utc) - timedelta(days=datetime.now(timezone.utc).weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    from app.config.settings import TradingMode
    open_positions_count = (
        len(state.execution_engine.get_open_positions()) if state.settings.trading_mode == TradingMode.LIVE
        else len(state.journal.open_trades())  # see /api/positions docstring: journal is the cross-process source of truth in PAPER mode
    )

    guard_input = GuardCheckInput(
        equity=account.equity,
        day_start_equity=account.equity - sum(t.pnl for t in state.journal.trades_since(day_start)),
        week_start_equity=account.equity - sum(t.pnl for t in state.journal.trades_since(week_start)),
        trades_today_count=len(state.journal.trades_since(day_start)),
        open_positions_count=open_positions_count,
        current_spread_points=(context.current_ask - context.current_bid) / state.symbol_spec.tick_size
        if context.current_ask and context.current_bid else 0.0,
        mt5_connected=state.client.is_connected(), broker_trade_allowed=account.trade_allowed,
        market_data_fresh=True,
        kill_switch_active=state.settings.kill_switch or state.kill_switch.is_active(),
        recent_trades=state.journal.recent_trades(20),
    )
    validation = state.validator.validate(best, context, state.symbol_spec, account, guard_input)
    explanation = explain_trade(best, context.h4_regime, validation)

    return {
        "signal": best.to_dict(),
        "validation": validation.to_dict(),
        "explanation": explanation.to_dict(),
    }


@app.get("/api/positions")
def positions_endpoint():
    """LIVE mode: broker state via ExecutionEngine is authoritative
    (real positions can be verified against the broker). PAPER mode:
    ExecutionEngine's paper positions are in-memory and PER-PROCESS —
    if the trading loop runs as a separate process from this dashboard
    (the normal deployment), this dashboard's own ExecutionEngine
    instance never sees them. TradeJournal.open_trades() is the actual
    cross-process source of truth for paper positions, so that's what
    we read from in that mode."""
    state = get_state()
    from app.config.settings import TradingMode

    try:
        tick = state.market_data.get_tick()
    except StaleMarketDataError:
        tick = None

    if state.settings.trading_mode == TradingMode.LIVE:
        positions = state.execution_engine.get_open_positions()
        rows = [
            {"ticket": p.ticket, "direction": p.direction, "volume": p.volume, "entry_price": p.price_open,
             "stop_loss": p.stop_loss, "take_profit": p.take_profit, "strategy": p.strategy,
             "regime": p.regime, "open_time": p.open_time.isoformat()}
            for p in positions
        ]
    else:
        rows = [
            {"ticket": r["id"], "direction": r["direction"], "volume": r["lots"], "entry_price": r["entry_price"],
             "stop_loss": r["stop_loss"], "take_profit": r["take_profit"], "strategy": r["strategy"],
             "regime": r["regime"], "open_time": r["open_time"]}
            for r in (dict(row) for row in state.journal.open_trades())
        ]

    out = []
    for row in rows:
        pnl_ticks = None
        r_multiple = None
        if tick:
            price = tick.bid if row["direction"] == "BUY" else tick.ask
            pnl_ticks = (
                (price - row["entry_price"]) / state.symbol_spec.tick_size if row["direction"] == "BUY"
                else (row["entry_price"] - price) / state.symbol_spec.tick_size
            )
            if row["stop_loss"] is not None:
                risk_ticks = abs(row["entry_price"] - row["stop_loss"]) / state.symbol_spec.tick_size
                r_multiple = (pnl_ticks / risk_ticks) if risk_ticks else None
        out.append({**row, "unrealized_pnl_ticks": pnl_ticks, "r_multiple": r_multiple})
    return {"positions": out}


@app.get("/api/risk")
def risk_endpoint():
    state = get_state()
    account = state.client.get_account_info()
    day_start = datetime.combine(datetime.now(timezone.utc).date(), dtime.min, tzinfo=timezone.utc)
    week_start = datetime.now(timezone.utc) - timedelta(days=datetime.now(timezone.utc).weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    trades_today_rows = state.journal.trades_since(day_start)
    trades_week_rows = state.journal.trades_since(week_start)
    daily_pnl = sum(t.pnl for t in trades_today_rows)
    weekly_pnl = sum(t.pnl for t in trades_week_rows)

    recent = state.journal.recent_trades(20)
    consecutive_losses = 0
    for t in reversed(recent):
        if t.pnl < 0:
            consecutive_losses += 1
        else:
            break

    return {
        "daily_pnl": daily_pnl, "daily_loss_pct": (-daily_pnl / account.equity * 100) if daily_pnl < 0 else 0.0,
        "weekly_pnl": weekly_pnl, "weekly_loss_pct": (-weekly_pnl / account.equity * 100) if weekly_pnl < 0 else 0.0,
        "trades_today": len(trades_today_rows),
        "consecutive_losses": consecutive_losses,
        "kill_switch_active": state.settings.kill_switch or state.kill_switch.is_active(),
        "kill_switch_status": state.kill_switch.status().__dict__,
        "risk_per_trade_pct": state.settings.risk.risk_per_trade_pct,
        "max_daily_loss_pct": state.settings.risk.max_daily_loss_pct,
        "max_open_positions": state.settings.risk.max_open_positions,
    }


@app.get("/api/performance")
def performance_endpoint():
    state = get_state()
    account = state.client.get_account_info()
    metrics = compute_live_performance(state.journal, starting_balance=account.balance)
    metrics["net_pnl_by_strategy"] = net_pnl_by_strategy(state.journal)
    return metrics


@app.post("/api/kill-switch/activate")
def activate_kill_switch(reason: str = "Activated via dashboard.", activated_by: str = "dashboard_user"):
    state = get_state()
    result = state.kill_switch.activate(reason=reason, activated_by=activated_by)
    return result.__dict__


@app.post("/api/kill-switch/deactivate")
def deactivate_kill_switch(deactivated_by: str = "dashboard_user"):
    state = get_state()
    result = state.kill_switch.deactivate(deactivated_by=deactivated_by)
    return result.__dict__


# =====================================================================
# Phase 11: Strategy Lab / Research Endpoints (Simulation-Only)
# =====================================================================

class RunExperimentPayload(BaseModel):
    strategy_name: str
    strategy_version: str = "1.0.0"
    parameters: dict[str, Any] = Field(default_factory=dict)
    timeframe: str = "M15"
    bars: int = Field(500, ge=50, le=10000)
    regime_thresholds: Optional[dict[str, Any]] = None
    scoring_weights: Optional[dict[str, Any]] = None
    risk_settings: Optional[dict[str, Any]] = None
    execution_costs: Optional[dict[str, Any]] = None
    backtest_config: Optional[dict[str, Any]] = None
    random_seed: Optional[int] = None


class CompareStrategiesPayload(BaseModel):
    bars: int = Field(500, ge=50, le=10000)
    strategy_params: Optional[dict[str, dict[str, Any]]] = None


class WalkForwardPayload(BaseModel):
    strategy_name: str
    strategy_version: str = "1.0.0"
    parameters: dict[str, Any] = Field(default_factory=dict)
    timeframe: str = "M15"
    bars: int = Field(500, ge=100, le=10000)
    n_folds: int = Field(3, ge=2, le=10)
    random_seed: Optional[int] = None


@app.get("/api/research/experiments")
def list_experiments_endpoint(strategy_name: Optional[str] = None, limit: int = 50):
    state = get_state()
    store = state.research_store or SqliteResearchStore(state.settings.database_url.replace("sqlite:///", ""))
    experiments = store.list_experiments(strategy_name=strategy_name, limit=limit)
    return {"experiments": [e.to_dict() for e in experiments]}


@app.get("/api/research/experiments/{experiment_id}")
def get_experiment_endpoint(experiment_id: str):
    state = get_state()
    store = state.research_store or SqliteResearchStore(state.settings.database_url.replace("sqlite:///", ""))
    experiment = store.get_experiment(experiment_id)
    if not experiment:
        raise HTTPException(status_code=404, detail=f"Experiment '{experiment_id}' not found.")
    return experiment.to_dict()


@app.post("/api/research/experiments/run")
def run_experiment_endpoint(payload: RunExperimentPayload):
    state = get_state()
    if state.settings.trading_mode == TradingMode.LIVE:
        raise HTTPException(status_code=400, detail="Safety violation: Strategy research cannot run in LIVE mode.")

    store = state.research_store or SqliteResearchStore(state.settings.database_url.replace("sqlite:///", ""))
    runner = StrategyLabRunner(
        symbol_spec=state.symbol_spec,
        risk_settings=state.settings.risk,
        store=store,
        trading_mode=TradingMode.BACKTEST,
    )

    m15_df = state.client.get_ohlcv(state.symbol_spec.name, payload.timeframe, payload.bars)

    config = ExperimentConfig(
        strategy_name=payload.strategy_name,
        strategy_version=payload.strategy_version,
        parameters=payload.parameters,
        timeframe=payload.timeframe,
        regime_thresholds=payload.regime_thresholds,
        scoring_weights=payload.scoring_weights,
        risk_settings=payload.risk_settings,
        execution_costs=payload.execution_costs,
        backtest_config=payload.backtest_config,
        random_seed=payload.random_seed,
    )

    result = runner.run_experiment(config, m15_df)
    return result.to_dict()


@app.post("/api/research/compare")
def compare_strategies_endpoint(payload: CompareStrategiesPayload):
    state = get_state()
    if state.settings.trading_mode == TradingMode.LIVE:
        raise HTTPException(status_code=400, detail="Safety violation: Strategy research cannot run in LIVE mode.")

    store = state.research_store or SqliteResearchStore(state.settings.database_url.replace("sqlite:///", ""))
    runner = StrategyLabRunner(
        symbol_spec=state.symbol_spec,
        risk_settings=state.settings.risk,
        store=store,
        trading_mode=TradingMode.BACKTEST,
    )

    m15_df = state.client.get_ohlcv(state.symbol_spec.name, "M15", payload.bars)
    entries = runner.compare_strategies(m15_df, strategy_params=payload.strategy_params)
    return {
        name: {
            "strategy_name": entry.strategy_name,
            "metrics": entry.metrics,
            "reproducibility_hash": entry.result.reproducibility_hash,
            "trades_count": len(entry.result.trades),
        }
        for name, entry in entries.items()
    }


@app.post("/api/research/walk-forward")
def walk_forward_endpoint(payload: WalkForwardPayload):
    state = get_state()
    if state.settings.trading_mode == TradingMode.LIVE:
        raise HTTPException(status_code=400, detail="Safety violation: Strategy research cannot run in LIVE mode.")

    runner = StrategyLabRunner(
        symbol_spec=state.symbol_spec,
        risk_settings=state.settings.risk,
        trading_mode=TradingMode.BACKTEST,
    )

    m15_df = state.client.get_ohlcv(state.symbol_spec.name, payload.timeframe, payload.bars)
    config = ExperimentConfig(
        strategy_name=payload.strategy_name,
        strategy_version=payload.strategy_version,
        parameters=payload.parameters,
        timeframe=payload.timeframe,
        random_seed=payload.random_seed,
    )

    folds = runner.run_walk_forward(config, m15_df, n_folds=payload.n_folds)
    return {
        "folds": [
            {
                "label": f.label,
                "start": str(f.start) if f.start else "",
                "end": str(f.end) if f.end else "",
                "bars": f.bars,
                "metrics": f.metrics,
            }
            for f in folds
        ]
    }


# ============================================================================
# Phase 12 — Paper Trading Endpoints (Section 34)
# Strictly isolated simulation runtime: rejects LIVE mode fail-closed
# ============================================================================

class PaperStartPayload(BaseModel):
    initial_balance: float = Field(default=10_000.0, gt=0)
    bars_to_process: int = Field(default=100, gt=0, le=10_000)
    timeframe: str = Field(default="M15")
    trading_mode: str = Field(default="PAPER")
    slippage_points: float = Field(default=0.0, ge=0.0)
    spread_multiplier: float = Field(default=1.0, ge=0.0)


def _get_or_create_paper_runtime(state: AppState) -> PaperTradingRuntime:
    if state.paper_runtime is not None:
        return state.paper_runtime

    from app.paper.broker import PaperExecutionAdapter
    from app.paper.store import InMemoryPaperStateStore

    adapter = PaperExecutionAdapter(
        symbol_spec=state.symbol_spec,
        journal=state.journal,
        initial_balance=10_000.0,
        trading_mode=TradingMode.PAPER,
    )
    runtime = PaperTradingRuntime(
        symbol_spec=state.symbol_spec,
        settings=state.settings,
        risk_settings=state.settings.risk,
        paper_broker=adapter,
        journal=state.journal,
        kill_switch=state.kill_switch,
        position_monitor=state.position_monitor,
        state_store=InMemoryPaperStateStore(),
        trading_mode=TradingMode.PAPER,
    )
    state.paper_runtime = runtime
    return runtime


@app.get("/api/paper/status")
def paper_status_endpoint():
    state = get_state()
    if state.paper_runtime is None:
        return {
            "active": False,
            "session_id": None,
            "trading_mode": "PAPER",
            "bars_processed": 0,
            "open_positions_count": 0,
            "reproducibility_hash": None,
        }
    return {
        "active": state.paper_runtime.is_running,
        "session_id": state.paper_runtime.session.session_id if state.paper_runtime.session else None,
        "trading_mode": state.paper_runtime.trading_mode.value,
        "bars_processed": state.paper_runtime.bars_processed,
        "open_positions_count": len(state.paper_runtime.broker.positions),
        "reproducibility_hash": (
            state.paper_runtime.session.config.reproducibility_hash
            if state.paper_runtime.session
            else None
        ),
    }


@app.get("/api/paper/account")
def paper_account_endpoint():
    state = get_state()
    runtime = _get_or_create_paper_runtime(state)
    acc = runtime.broker.account
    return {
        "balance": acc.balance,
        "equity": acc.equity,
        "floating_pnl": acc.floating_pnl,
        "realized_pnl": acc.realized_pnl,
        "margin": acc.margin,
        "free_margin": acc.free_margin,
        "margin_level": acc.margin_level if acc.used_margin > 0 else 0.0,
        "consecutive_losses": acc.consecutive_losses,
        "daily_realized_pnl": acc.daily_realized_pnl,
        "trading_mode": runtime.trading_mode.value,
    }


@app.get("/api/paper/positions")
def paper_positions_endpoint():
    state = get_state()
    runtime = _get_or_create_paper_runtime(state)
    return [pos.to_dict() for pos in runtime.broker.positions]


@app.get("/api/paper/trades")
def paper_trades_endpoint():
    state = get_state()
    runtime = _get_or_create_paper_runtime(state)
    return [pos.to_dict() for pos in runtime.broker.closed_positions]


@app.get("/api/paper/metrics")
def paper_metrics_endpoint():
    state = get_state()
    runtime = _get_or_create_paper_runtime(state)
    trades = runtime.broker.closed_positions
    starting_balance = (
        runtime.session.config.initial_balance
        if runtime.session
        else 10_000.0
    )
    return compute_paper_metrics(trades, starting_balance=starting_balance)


@app.post("/api/paper/start")
def paper_start_endpoint(payload: PaperStartPayload):
    state = get_state()
    if payload.trading_mode.upper() == "LIVE" or state.settings.trading_mode == TradingMode.LIVE:
        raise HTTPException(
            status_code=400,
            detail="Safety violation: LIVE trading mode is strictly prohibited in Paper Trading runtime.",
        )

    runtime = _get_or_create_paper_runtime(state)
    if not runtime.is_running:
        from app.paper.session import PaperSessionConfig

        cfg = PaperSessionConfig(
            initial_balance=payload.initial_balance,
            trading_mode=TradingMode.PAPER,
            slippage_points=payload.slippage_points,
            spread_multiplier=payload.spread_multiplier,
        )
        runtime.start_session(cfg)

    try:
        df = state.client.get_ohlcv(state.symbol_spec.name, payload.timeframe, payload.bars_to_process)
        if df is not None and not df.empty:
            runtime.process_chronological_bars(df)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error executing paper trading simulation: {exc}")

    return {
        "status": "success",
        "session_id": runtime.session.session_id if runtime.session else None,
        "bars_processed": runtime.bars_processed,
        "open_positions": len(runtime.broker.positions),
        "closed_positions": len(runtime.broker.closed_positions),
        "equity": runtime.broker.account.equity,
        "balance": runtime.broker.account.balance,
    }


@app.post("/api/paper/stop")
def paper_stop_endpoint():
    state = get_state()
    if state.paper_runtime is None or not state.paper_runtime.is_running:
        return {"status": "not_running", "message": "No active paper trading session to stop."}

    session = state.paper_runtime.stop_session("API user stopped session")
    trades = state.paper_runtime.broker.closed_positions
    starting_balance = session.config.initial_balance if session else 10_000.0
    metrics = compute_paper_metrics(trades, starting_balance=starting_balance)
    return {
        "status": "stopped",
        "session_id": session.session_id if session else None,
        "bars_processed": state.paper_runtime.bars_processed,
        "closed_positions": len(trades),
        "metrics": metrics,
    }
