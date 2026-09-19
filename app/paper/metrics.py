"""
Paper Trading Metrics (Phase 12).

Computes standardized session performance statistics reusing Phase 10 metric
definitions without creating conflicting or duplicate abstractions.
"""
from __future__ import annotations

from typing import Any

from app.paper.account import PaperAccount
from app.paper.positions import PaperPosition


def compute_paper_metrics(
    account_or_trades: PaperAccount | list[PaperPosition],
    rejected_signals: int = 0,
    unknown_actions: int = 0,
    starting_balance: float = 10_000.0,
) -> dict[str, Any]:
    """Computes descriptive performance metrics across closed paper positions and account balances."""
    if isinstance(account_or_trades, PaperAccount):
        account = account_or_trades
        trades = account.closed_positions
        starting_bal = account.starting_balance
        curr_bal = account.current_balance
        eq = account.equity
        c_wins = account.consecutive_wins
        c_losses = account.consecutive_losses
        sp_cost = account.spread_cost
        sl_cost = account.slippage_cost
        comm = account.commission
        swap = account.swap
        net_profit = round(account.realized_pnl, 2)
    else:
        trades = list(account_or_trades)
        starting_bal = starting_balance
        net_profit = round(sum(p.realized_pnl for p in trades), 2)
        curr_bal = round(starting_bal + net_profit, 2)
        eq = curr_bal
        c_wins = 0
        c_losses = 0
        sp_cost = sum(getattr(p, "spread_cost", 0.0) for p in trades)
        sl_cost = sum(getattr(p, "slippage_cost", 0.0) for p in trades)
        comm = sum(getattr(p, "commission", 0.0) for p in trades)
        swap = sum(getattr(p, "swap", 0.0) for p in trades)

    n = len(trades)
    gross_profit = sum(p.realized_pnl for p in trades if p.realized_pnl > 0)
    gross_loss = sum(p.realized_pnl for p in trades if p.realized_pnl < 0)

    winning_trades = [p for p in trades if p.realized_pnl > 0]
    losing_trades = [p for p in trades if p.realized_pnl <= 0]

    win_rate = (len(winning_trades) / n) if n > 0 else 0.0
    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss < 0 else (float("inf") if gross_profit > 0 else 0.0)
    expectancy = (net_profit / n) if n > 0 else 0.0

    avg_win = (gross_profit / len(winning_trades)) if winning_trades else 0.0
    avg_loss = (gross_loss / len(losing_trades)) if losing_trades else 0.0

    largest_win = max((p.realized_pnl for p in trades), default=0.0)
    largest_loss = min((p.realized_pnl for p in trades), default=0.0)

    # Calculate Max Drawdown from trade P/L trajectory
    running_equity = starting_bal
    peak = running_equity
    max_dd_abs = 0.0
    max_dd_pct = 0.0

    for p in trades:
        running_equity += p.realized_pnl
        if running_equity > peak:
            peak = running_equity
        dd = peak - running_equity
        if dd > max_dd_abs:
            max_dd_abs = dd
        if peak > 0:
            dd_pct = (dd / peak) * 100.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

    return {
        "starting_balance": starting_bal,
        "ending_balance": curr_bal,
        "equity": eq,
        "net_profit": net_profit,
        "net_pnl": net_profit,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else 0.0,
        "expectancy": round(expectancy, 2),
        "max_drawdown_abs": round(max_dd_abs, 2),
        "max_drawdown_pct": round(max_dd_pct, 4),
        "total_trades": n,
        "winning_trades": len(winning_trades),
        "losing_trades": len(losing_trades),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "largest_win": round(largest_win, 2),
        "largest_loss": round(largest_loss, 2),
        "consecutive_wins": c_wins,
        "consecutive_losses": c_losses,
        "total_spread_cost": round(sp_cost, 2),
        "total_slippage_cost": round(sl_cost, 2),
        "total_commission": round(comm, 2),
        "total_swap": round(swap, 2),
        "rejected_signals": rejected_signals,
        "unknown_actions": unknown_actions,
    }
