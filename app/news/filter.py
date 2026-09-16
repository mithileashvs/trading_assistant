"""
News Filter (section 18) — explicit multi-state model.

SAFETY REDESIGN NOTE: this module previously exposed two independent
booleans (`available`, `blackout_active`), and callers derived
"is it safe to trade" as `not blackout_active`. That produced a
dangerous combination: `available=False` (no data) + `blackout_active=False`
(defaults to "no blackout") silently evaluated to "news is fine, trade
away" -- i.e. "I don't know" was silently treated as "safe". That is
exactly backwards, and it is not a hypothetical: it was verified as a
live bug in `TradeValidator.validate()`.

This is now an explicit enum with exactly ONE state that permits a new
trade:

    NewsState.CLEAR         -> new trades may proceed (if every other
                                gate also passes)
    NewsState.BLOCKED       -> a blackout window is active; no new trade
    NewsState.UNAVAILABLE   -> no working data source; no new trade
    NewsState.UNKNOWN       -> a data source exists but cannot speak
                                confidently about this specific time
                                (e.g. outside its known coverage
                                window); no new trade

"Unknown" and "unavailable" are DELIBERATELY distinct: unavailable
means there is no news-data source at all; unknown means a source
exists but this specific query is outside what it can honestly answer
for. Both block trading identically -- the distinction is for
diagnostics/auditability, not because either one is treated as safer
than the other.

`NewsStatus.permits_new_trade` is the ONLY sanctioned way to derive a
trade/no-trade decision from a NewsStatus -- it is a property, not a
value callers recompute, specifically so the old bug class (someone
writing their own boolean expression here that gets it backwards
again) cannot silently recur.
"""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from datetime import datetime


class NewsState(str, enum.Enum):
    CLEAR = "CLEAR"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass
class NewsStatus:
    state: NewsState
    reason: str

    @property
    def permits_new_trade(self) -> bool:
        """The ONLY state that permits a new trade is CLEAR. Every
        other state -- including UNAVAILABLE and UNKNOWN -- blocks,
        because "I don't know" must never be treated as "safe"."""
        return self.state == NewsState.CLEAR


class NewsFilter(abc.ABC):
    @abc.abstractmethod
    def check(self, at: datetime) -> NewsStatus: ...


class UnavailableNewsFilter(NewsFilter):
    """The default until a real news-data provider is integrated.
    Always reports UNAVAILABLE -- which blocks new trades -- rather
    than ever claiming to know whether a blackout is or isn't active."""

    def check(self, at: datetime) -> NewsStatus:
        return NewsStatus(
            state=NewsState.UNAVAILABLE,
            reason="No news-data provider is configured; news filter is unavailable.",
        )


class BlockedNewsFilter(NewsFilter):
    """Always reports an active blackout (BLOCKED). For tests that
    specifically need to exercise the "a blackout is active" branch
    of the news gate, distinct from "no data at all" (UNAVAILABLE)."""

    def check(self, at: datetime) -> NewsStatus:
        return NewsStatus(
            state=NewsState.BLOCKED,
            reason="Blackout window is active (test double: BlockedNewsFilter).",
        )


class AlwaysClearNewsFilter(NewsFilter):
    """BACKTEST-ONLY. Never use this outside `BacktestEngine`.

    Always reports CLEAR, i.e. it unconditionally permits the news
    gate to pass. This does NOT simulate real historical news
    blackout windows -- no historical news data is fabricated or
    consulted. It exists purely so that a backtest run (which by
    definition has no live news-data provider and never will) isn't
    unconditionally blocked at the news gate the same way a live/paper
    deployment correctly is by `UnavailableNewsFilter`.

    Backtest results produced with this filter therefore do NOT
    account for real-world news-driven risk at all -- that's a known,
    accepted limitation of the current backtest engine (section 18
    news data is not wired into backtesting), not a claim that "no
    news event would ever have mattered" for the tested period.

    `TradeValidator`'s default (used by anything OTHER than
    `BacktestEngine`, i.e. every live/paper path) remains
    `UnavailableNewsFilter`, which fails closed. This class is never
    imported or referenced by any live/paper trading code path -- it
    is wired in exactly one place: `BacktestEngine.__init__`'s
    default `news_filter` argument.
    """

    def check(self, at: datetime) -> NewsStatus:
        return NewsStatus(
            state=NewsState.CLEAR,
            reason="Backtest mode: news gate bypassed (AlwaysClearNewsFilter, no historical news data available).",
        )
