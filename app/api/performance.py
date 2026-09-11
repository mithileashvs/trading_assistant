"""
Live performance metrics for the dashboard (section 33's "Performance"
panel: win rate, profit factor, drawdown, expectancy, strategy
performance). Reads directly from TradeJournal — this is a lighter
sibling of app.backtesting.metrics for journal rows rather than
BacktestTrade objects, since the shapes differ (sqlite3.Row vs
dataclass) and a live dashboard doesn't need the full backtest metric
suite (Sharpe/Sortino need a real equity time series most live
deployments won't have accumulated yet).
"""
from __future__ import annotations

from app.journal.journal import TradeJournal


def compute_live_performance(journal: TradeJournal, starting_balance: float) -> dict:
    rows = [dict(r) for r in journal.all_trades() if r["close_time"] is not None]
    if not rows:
        return {
            "number_of_trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "expectancy": 0.0,
            "max_drawdown_abs": 0.0, "max_drawdown_pct": 0.0, "net_profit": 0.0,
            "note": "No closed trades yet.",
        }

    pnls = [r["pnl"] or 0.0 for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)  # negative or zero

    win_rate = len(wins) / len(pnls)
    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss < 0 else (None if gross_profit > 0 else 0.0)
    expectancy = sum(pnls) / len(pnls)
    net_profit = sum(pnls)

    equity = starting_balance
    peak = starting_balance
    max_dd_abs = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd_abs = min(max_dd_abs, equity - peak)
    max_dd_pct = (max_dd_abs / peak) if peak else 0.0

    return {
        "number_of_trades": len(rows),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "net_profit": net_profit,
        "max_drawdown_abs": max_dd_abs,
        "max_drawdown_pct": max_dd_pct,
        "starting_balance": starting_balance,
        "current_balance": starting_balance + net_profit,
    }
