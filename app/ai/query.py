"""
Trade history querying (section 30: "querying trade history").

Structured (not natural-language) query functions over TradeJournal —
these are what an AI layer would call as tools if a future LLM-driven
interface is added, and are directly usable on their own today.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.journal.journal import TradeJournal


def trades_in_range(journal: TradeJournal, start: datetime, end: datetime) -> list[dict]:
    rows = journal.all_trades()
    out = []
    for row in rows:
        if row["close_time"] is None:
            continue
        closed = datetime.fromisoformat(row["close_time"])
        if start <= closed <= end:
            out.append(dict(row))
    return out


def trades_today(journal: TradeJournal, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return trades_in_range(journal, start, now)


def trades_this_week(journal: TradeJournal, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=now.weekday())
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return trades_in_range(journal, start, now)


def win_rate_for_strategy(journal: TradeJournal, strategy: str) -> float | None:
    rows = [dict(r) for r in journal.all_trades() if r["strategy"] == strategy and r["close_time"] is not None]
    if not rows:
        return None
    wins = [r for r in rows if (r["pnl"] or 0.0) > 0]
    return len(wins) / len(rows)


def net_pnl_by_strategy(journal: TradeJournal) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in journal.all_trades():
        if row["close_time"] is None:
            continue
        totals[row["strategy"]] = totals.get(row["strategy"], 0.0) + (row["pnl"] or 0.0)
    return totals


def open_positions_summary(journal: TradeJournal) -> list[dict]:
    return [dict(r) for r in journal.open_trades()]
