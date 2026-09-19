"""
Backtest Metrics (section 25).

All metrics are computed from a BacktestResult. Nothing here claims a
strategy "will make money" — these are descriptive statistics of what
already happened in the simulation, for the person to interpret
(section 41).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from app.backtesting.results import BacktestResult


def _trade_frame(result: BacktestResult) -> pd.DataFrame:
    if not result.trades:
        return pd.DataFrame()
    rows = [t.to_dict() for t in result.trades]
    df = pd.DataFrame(rows)
    df["open_time"] = pd.to_datetime(df["open_time"])
    df["close_time"] = pd.to_datetime(df["close_time"])
    return df


def _equity_series(result: BacktestResult) -> pd.Series:
    if not result.equity_curve:
        return pd.Series(dtype=float)
    idx = pd.to_datetime([p.time for p in result.equity_curve])
    return pd.Series([p.equity for p in result.equity_curve], index=idx)


def _max_drawdown(equity: pd.Series) -> tuple[float, float, pd.Timedelta]:
    """Returns (max_drawdown_abs, max_drawdown_pct, max_drawdown_duration)."""
    if equity.empty:
        return 0.0, 0.0, pd.Timedelta(0)
    running_max = equity.cummax()
    drawdown = equity - running_max
    drawdown_pct = drawdown / running_max.replace(0, np.nan)

    max_dd_abs = float(drawdown.min())
    max_dd_pct = float(drawdown_pct.min()) if not drawdown_pct.isna().all() else 0.0

    # Duration: longest stretch where equity stays below its prior peak.
    in_drawdown = drawdown < 0
    longest = pd.Timedelta(0)
    if in_drawdown.any():
        start = None
        prev_time = None
        for t, flag in in_drawdown.items():
            if flag and start is None:
                start = t
            if not flag and start is not None:
                longest = max(longest, prev_time - start)
                start = None
            prev_time = t
        if start is not None:
            longest = max(longest, equity.index[-1] - start)
    return max_dd_abs, max_dd_pct, longest


def _max_consecutive_losses(pnls: list[float]) -> int:
    longest = current = 0
    for pnl in pnls:
        if pnl < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _max_consecutive_wins(pnls: list[float]) -> int:
    longest = current = 0
    for pnl in pnls:
        if pnl > 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _sharpe_sortino(daily_returns: pd.Series, periods_per_year: int = 252) -> tuple[float, float]:
    if daily_returns.empty or daily_returns.std(ddof=0) == 0:
        return 0.0, 0.0
    mean = daily_returns.mean()
    std = daily_returns.std(ddof=0)
    sharpe = (mean / std) * math.sqrt(periods_per_year) if std > 0 else 0.0

    downside = daily_returns[daily_returns < 0]
    downside_std = downside.std(ddof=0) if len(downside) > 1 else 0.0
    sortino = (mean / downside_std) * math.sqrt(periods_per_year) if downside_std > 0 else 0.0
    return float(sharpe), float(sortino)


def compute_metrics(result: BacktestResult) -> dict:
    trades_df = _trade_frame(result)
    equity = _equity_series(result)

    n = len(trades_df)
    if n == 0:
        return {
            "number_of_trades": 0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "net_profit": result.net_profit,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "starting_balance": result.starting_balance,
            "ending_balance": result.ending_balance,
            "win_rate": 0.0,
            "loss_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "average_trade": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
            "total_commissions": 0.0,
            "total_commission": 0.0,
            "total_spread_cost": 0.0,
            "total_slippage_cost": 0.0,
            "total_swap_cost": 0.0,
            "note": "No trades were taken during this backtest window.",
        }

    wins = trades_df[trades_df["pnl"] > 0]
    losses = trades_df[trades_df["pnl"] <= 0]
    gross_profit = float(wins["pnl"].sum())
    gross_loss = float(losses["pnl"].sum())  # negative or zero

    win_rate = len(wins) / n
    loss_rate = len(losses) / n
    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss < 0 else (float("inf") if gross_profit > 0 else 0.0)
    expectancy = float(trades_df["pnl"].mean())
    avg_win = float(wins["pnl"].mean()) if len(wins) else 0.0
    avg_loss = float(losses["pnl"].mean()) if len(losses) else 0.0

    max_dd_abs, max_dd_pct, max_dd_duration = _max_drawdown(equity)
    pnl_list = trades_df["pnl"].tolist()
    max_consecutive_losses = _max_consecutive_losses(pnl_list)
    max_consecutive_wins = _max_consecutive_wins(pnl_list)

    # Cost aggregations
    tot_commission = float(trades_df["commission"].sum()) if "commission" in trades_df else 0.0
    tot_swap = float(trades_df["swap"].sum()) if "swap" in trades_df else 0.0
    tot_spread = float(trades_df["spread_cost"].sum()) if "spread_cost" in trades_df else 0.0
    tot_slippage = float(trades_df["slippage_cost"].sum()) if "slippage_cost" in trades_df else 0.0

    # Daily returns from the equity curve, for Sharpe/Sortino/Calmar.
    daily_equity = equity.resample("D").last().ffill()
    daily_returns = daily_equity.pct_change().dropna()
    sharpe, sortino = _sharpe_sortino(daily_returns)

    date_span_days = (equity.index[-1] - equity.index[0]).days if len(equity) > 1 else 0
    years = date_span_days / 365.25 if date_span_days > 0 else None
    cagr = None
    if years and years > 0 and result.starting_balance > 0:
        cagr = (result.ending_balance / result.starting_balance) ** (1 / years) - 1

    calmar = None
    if cagr is not None and max_dd_pct != 0:
        calmar = cagr / abs(max_dd_pct)

    total_notional = float((trades_df["lots"] * trades_df["entry_price"]).sum())
    avg_equity = float(equity.mean()) if not equity.empty else result.starting_balance
    turnover = total_notional / avg_equity if avg_equity else 0.0

    holding_time = (trades_df["close_time"] - trades_df["open_time"]).sum()
    total_time = (equity.index[-1] - equity.index[0]) if len(equity) > 1 else pd.Timedelta(0)
    exposure = (holding_time.total_seconds() / total_time.total_seconds()) if total_time.total_seconds() > 0 else 0.0

    metrics = {
        "number_of_trades": n,
        "total_trades": n,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_profit": result.net_profit,
        "starting_balance": result.starting_balance,
        "ending_balance": result.ending_balance,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "average_trade": expectancy,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "cagr": cagr,
        "max_drawdown_abs": max_dd_abs,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_duration_days": max_dd_duration.total_seconds() / 86400,
        "max_consecutive_wins": max_consecutive_wins,
        "max_consecutive_losses": max_consecutive_losses,
        "total_commission": tot_commission,
        "total_commissions": tot_commission,
        "total_spread_cost": tot_spread,
        "total_slippage_cost": tot_slippage,
        "total_swap_cost": tot_swap,
        "turnover": turnover,
        "exposure": exposure,
        "avg_mae": float(trades_df["mae"].mean()),
        "avg_mfe": float(trades_df["mfe"].mean()),
        "breakdown_by_year": _breakdown(trades_df, trades_df["open_time"].dt.year),
        "breakdown_by_month": _breakdown(trades_df, trades_df["open_time"].dt.tz_localize(None).dt.to_period("M").astype(str)),
        "breakdown_by_session": _breakdown(trades_df, trades_df["session"]),
        "breakdown_by_regime": _breakdown(trades_df, trades_df["regime"]),
        "breakdown_by_strategy": _breakdown(trades_df, trades_df["strategy"]),
        "breakdown_by_direction": _breakdown(trades_df, trades_df["direction"]),
    }
    return metrics


def _breakdown(trades_df: pd.DataFrame, group_key: pd.Series) -> dict:
    out = {}
    for key, group in trades_df.groupby(group_key):
        wins = group[group["pnl"] > 0]
        out[str(key)] = {
            "trades": len(group),
            "net_pnl": float(group["pnl"].sum()),
            "win_rate": len(wins) / len(group) if len(group) else 0.0,
        }
    return out
