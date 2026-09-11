"""
Minimal trade-history interface the risk guards depend on (section 16:
daily/weekly loss, consecutive losses, trades-today all need recent
closed-trade history). This is intentionally NOT the full Trade
Journal from section 23 — it's the small slice of history the risk
engine needs, behind an interface so the real Journal can implement it
later without the guards changing.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class TradeRecord:
    closed_at: datetime
    symbol: str
    direction: str
    pnl: float
    r_multiple: float | None = None


class TradeHistoryProvider(abc.ABC):
    @abc.abstractmethod
    def trades_since(self, since: datetime) -> list[TradeRecord]: ...

    @abc.abstractmethod
    def recent_trades(self, limit: int) -> list[TradeRecord]:
        """Most recent trades, oldest first, for consecutive-loss counting."""
        ...


@dataclass
class InMemoryTradeHistory(TradeHistoryProvider):
    """Simple in-process implementation for development/testing and for
    any deployment that hasn't wired up the full Journal (Phase 5+) yet."""

    _trades: list[TradeRecord] = field(default_factory=list)

    def record(self, trade: TradeRecord) -> None:
        self._trades.append(trade)
        self._trades.sort(key=lambda t: t.closed_at)

    def trades_since(self, since: datetime) -> list[TradeRecord]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return [t for t in self._trades if t.closed_at >= since]

    def recent_trades(self, limit: int) -> list[TradeRecord]:
        return self._trades[-limit:]
