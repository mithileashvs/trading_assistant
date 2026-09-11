"""
Summarization (section 30: "summarizing market conditions",
"summarizing performance", "analyzing backtest results").

All summaries here are built directly from structured data the system
already computes — no LLM call is required for a summary to exist.
`polish()` at the bottom is an OPTIONAL LLM pass to rephrase the same
facts more fluidly; it never adds facts the deterministic text didn't
already state, and callers get the deterministic text either way if no
LLM is configured or the call fails.
"""
from __future__ import annotations

from app.ai.llm_client import LLMClient
from app.regimes.detector import RegimeDecision


def summarize_market_conditions(h4_features: dict, h1_features: dict, m15_features: dict, regime: RegimeDecision) -> str:
    lines = [
        f"H4 regime: {regime.regime.value} ({regime.confidence:.0%} confidence) — {'; '.join(regime.reasons)}",
        f"H4 close {h4_features['close']}, ADX {h4_features['trend'].get('adx')}, "
        f"RSI {h4_features['momentum'].get('rsi')}, ATR% {h4_features['volatility'].get('atr_pct')}",
        f"H1 structure: {h1_features['trend'].get('structure')}, "
        f"EMA20/50: {h1_features['trend'].get('ema_20')}/{h1_features['trend'].get('ema_50')}",
        f"M15 close {m15_features['close']}, session {m15_features.get('session')}, "
        f"RSI {m15_features['momentum'].get('rsi')}",
    ]
    return "\n".join(lines)


def summarize_backtest(metrics: dict) -> str:
    if metrics.get("number_of_trades", 0) == 0:
        return "No trades were taken in this backtest window."

    lines = [
        f"{metrics['number_of_trades']} trades, net profit {metrics['net_profit']:.2f} "
        f"(starting {metrics['starting_balance']:.2f} -> ending {metrics['ending_balance']:.2f}).",
        f"Win rate {metrics['win_rate']:.0%}, profit factor {metrics['profit_factor']:.2f}, "
        f"expectancy {metrics['expectancy']:.2f} per trade.",
        f"Max drawdown {metrics['max_drawdown_pct']:.1%} over {metrics['max_drawdown_duration_days']:.1f} days; "
        f"max {metrics['max_consecutive_losses']} consecutive losses.",
    ]
    if metrics.get("sharpe_ratio") is not None:
        lines.append(f"Sharpe {metrics['sharpe_ratio']:.2f}, Sortino {metrics['sortino_ratio']:.2f}.")
    by_strategy = metrics.get("breakdown_by_strategy") or {}
    if by_strategy:
        parts = [f"{name}: {v['trades']} trades, net {v['net_pnl']:.2f}" for name, v in by_strategy.items()]
        lines.append("By strategy — " + "; ".join(parts))
    return "\n".join(lines)


def summarize_trade_history(trades: list[dict]) -> str:
    """`trades` is a list of dicts as returned by TradeJournal.all_trades()
    (sqlite3.Row supports dict()) or app.backtesting.results.BacktestTrade.to_dict()."""
    closed = [t for t in trades if t.get("close_time")]
    if not closed:
        return "No closed trades in this history."

    total_pnl = sum(t.get("pnl") or 0.0 for t in closed)
    wins = [t for t in closed if (t.get("pnl") or 0.0) > 0]
    win_rate = len(wins) / len(closed)
    return (
        f"{len(closed)} closed trades, total P&L {total_pnl:.2f}, win rate {win_rate:.0%}. "
        f"Most recent: {closed[-1].get('direction')} {closed[-1].get('symbol')} "
        f"({closed[-1].get('exit_reason')}, pnl {closed[-1].get('pnl')})."
    )


def polish(deterministic_text: str, llm_client: LLMClient, context_label: str = "trading summary") -> str:
    """Optionally rephrase already-correct deterministic text more
    fluidly via an LLM. On ANY failure (no client configured, API
    error, etc.) returns the original deterministic text unchanged —
    a summary must never become unavailable just because the optional
    LLM polish step failed."""
    if not llm_client.is_available():
        return deterministic_text
    try:
        return llm_client.complete(
            system_prompt=(
                "You rephrase trading system output for a human reader. "
                "Do not add, remove, or change any fact, number, or conclusion — "
                "only improve phrasing and flow. If you are unsure, keep the original wording."
            ),
            user_prompt=f"Rephrase this {context_label} for readability, changing no facts:\n\n{deterministic_text}",
        )
    except Exception:  # noqa: BLE001 - the deterministic text is always a safe fallback
        return deterministic_text
