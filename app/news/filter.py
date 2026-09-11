"""
News Filter (section 18).

No real news-data provider is wired up yet. Per the spec: "If no
reliable news data is available, the system must clearly indicate that
the news filter is unavailable rather than pretending it is working."
This stub does exactly that — it never claims a blackout is or isn't
active from data it doesn't have; it reports itself as unavailable so
callers (the trade validator) can decide how to treat that honestly.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime


@dataclass
class NewsStatus:
    available: bool
    blackout_active: bool
    reason: str


class NewsFilter(abc.ABC):
    @abc.abstractmethod
    def check(self, at: datetime) -> NewsStatus: ...


class UnavailableNewsFilter(NewsFilter):
    """The only implementation until a real news-data provider is
    integrated. Always reports itself as unavailable and never blocks
    trades on the strength of data it doesn't actually have."""

    def check(self, at: datetime) -> NewsStatus:
        return NewsStatus(
            available=False,
            blackout_active=False,
            reason="No news-data provider is configured; news filter is unavailable.",
        )
