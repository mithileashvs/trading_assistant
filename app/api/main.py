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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from app.ai.explain import explain_trade
from app.ai.query import net_pnl_by_strategy, win_rate_for_strategy
from app.api.performance import compute_live_performance
from app.api.state import AppState, build_app_state
from app.features.engine import InsufficientDataError, compute_features
from app.market_data.engine import StaleMarketDataError
from app.risk.guards import GuardCheckInput
from app.strategies.context import build_context

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
