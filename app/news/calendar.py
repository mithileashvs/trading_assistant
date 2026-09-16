"""
Calendar-based News Filter (section 18) — a REAL implementation.

UnavailableNewsFilter (the original Phase 5 stub) remains the default
because this sandbox has no network access to a live economic-calendar
API (the egress allowlist covers package registries only — see
app/mt5/real_client.py's notes on the same kind of sandbox limit for
MT5). CalendarNewsFilter is what you get once you point it at actual
calendar data: a CSV or JSON export from any economic-calendar
provider (ForexFactory, Investing.com, a broker's calendar API, etc.),
loaded from disk. Once configured, high-impact-event blackout windows
are real, not simulated — the "unavailable" honesty from section 18
was about not pretending an ABSENT filter works, not an argument
against a working one once real data exists.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.news.filter import NewsFilter, NewsState, NewsStatus

_HIGH_IMPACT_KEYWORDS = {
    "CPI", "NFP", "NONFARM", "FOMC", "INTEREST RATE", "RATE DECISION",
    "PCE", "GDP", "UNEMPLOYMENT", "FED", "CENTRAL BANK", "PAYROLLS",
}


@dataclass
class CalendarEvent:
    time: datetime
    name: str
    impact: str  # "HIGH" | "MEDIUM" | "LOW"
    currency: str = ""


def _parse_impact(raw: str) -> str:
    raw = (raw or "").strip().upper()
    if raw in ("HIGH", "H", "3", "RED"):
        return "HIGH"
    if raw in ("MEDIUM", "MED", "M", "2", "ORANGE", "YELLOW"):
        return "MEDIUM"
    return "LOW"


def load_calendar_csv(path: str) -> list[CalendarEvent]:
    """Expects columns: time (ISO 8601), name, impact, currency
    (currency optional). This is a common shape for economic-calendar
    CSV exports; adapt column names here if your source differs."""
    events = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = datetime.fromisoformat(row["time"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            events.append(CalendarEvent(
                time=ts, name=row.get("name", "").strip(),
                impact=_parse_impact(row.get("impact", "")),
                currency=row.get("currency", "").strip(),
            ))
    return events


def load_calendar_json(path: str) -> list[CalendarEvent]:
    """Expects a JSON list of objects with keys: time, name, impact,
    currency (currency optional)."""
    with open(path, encoding="utf-8") as f:
        raw_events = json.load(f)
    events = []
    for e in raw_events:
        ts = datetime.fromisoformat(e["time"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        events.append(CalendarEvent(
            time=ts, name=e.get("name", ""), impact=_parse_impact(e.get("impact", "")),
            currency=e.get("currency", ""),
        ))
    return events


def load_calendar(path: str) -> list[CalendarEvent]:
    ext = Path(path).suffix.lower()
    if ext == ".csv":
        return load_calendar_csv(path)
    if ext == ".json":
        return load_calendar_json(path)
    raise ValueError(f"Unsupported calendar file type '{ext}' (use .csv or .json).")


class CalendarNewsFilter(NewsFilter):
    """Blocks trading in a configurable window around HIGH-impact
    events only (section 18's examples — CPI, NFP, FOMC, rate
    decisions — are all HIGH-impact by convention). MEDIUM/LOW impact
    events never trigger a blackout; that threshold is deliberately
    not configurable per-event here to keep behavior predictable, but
    the whole filter is opt-in per instance.

    COVERAGE AWARENESS: this filter only ever has confident knowledge
    within the time range its loaded events actually span. A query for
    a time outside that range returns NewsState.UNKNOWN, not CLEAR —
    "no events listed near this time" is not the same claim as "the
    calendar confirms there's nothing scheduled near this time"; a
    calendar file that simply hasn't been refreshed far enough into
    the future can't honestly make the second claim.
    """

    def __init__(
        self,
        events: list[CalendarEvent],
        minutes_before: int = 30,
        minutes_after: int = 30,
        symbol_currencies: tuple[str, ...] = ("USD", "XAU"),
        coverage_buffer_hours: float = 6.0,
    ):
        self._events = sorted(events, key=lambda e: e.time)
        self.minutes_before = minutes_before
        self.minutes_after = minutes_after
        self.symbol_currencies = symbol_currencies
        # How far beyond the earliest/latest listed event (of ANY
        # impact level -- even a low-impact entry confirms the
        # calendar creator considered that date) we still trust the
        # calendar's silence as meaning "confirmed clear," not just
        # "not checked that far out."
        self.coverage_buffer_hours = coverage_buffer_hours

    @classmethod
    def from_file(cls, path: str, **kwargs) -> "CalendarNewsFilter":
        return cls(load_calendar(path), **kwargs)

    def _relevant_high_impact_events(self) -> list[CalendarEvent]:
        return [
            e for e in self._events
            if e.impact == "HIGH" and (not e.currency or e.currency.upper() in self.symbol_currencies)
        ]

    def _coverage_range(self) -> tuple[datetime, datetime] | None:
        if not self._events:
            return None
        return self._events[0].time, self._events[-1].time

    def check(self, at: datetime) -> NewsStatus:
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)

        coverage = self._coverage_range()
        if coverage is None:
            return NewsStatus(
                state=NewsState.UNKNOWN,
                reason="Calendar contains no events at all; its coverage window cannot be determined, "
                       "so 'no blocking events' cannot be confirmed.",
            )
        coverage_start, coverage_end = coverage
        buffer = timedelta(hours=self.coverage_buffer_hours)
        if at < coverage_start - buffer or at > coverage_end + buffer:
            return NewsStatus(
                state=NewsState.UNKNOWN,
                reason=f"Requested time {at.isoformat()} is outside the calendar's known coverage window "
                       f"({coverage_start.isoformat()} to {coverage_end.isoformat()}); cannot confirm "
                       "there is no blocking event near this time.",
            )

        for event in self._relevant_high_impact_events():
            window_start = event.time - timedelta(minutes=self.minutes_before)
            window_end = event.time + timedelta(minutes=self.minutes_after)
            if window_start <= at <= window_end:
                return NewsStatus(
                    state=NewsState.BLOCKED,
                    reason=f"High-impact event '{event.name}' at {event.time.isoformat()} "
                           f"(blackout window {self.minutes_before}min before / {self.minutes_after}min after).",
                )
        return NewsStatus(
            state=NewsState.CLEAR,
            reason="No high-impact event within the blackout window; requested time is within calendar coverage.",
        )

    def upcoming_high_impact_events(self, after: datetime, limit: int = 10) -> list[CalendarEvent]:
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        return [e for e in self._relevant_high_impact_events() if e.time >= after][:limit]


def build_news_filter(
    calendar_path: str | None,
    minutes_before: int = 30,
    minutes_after: int = 30,
) -> NewsFilter:
    """Factory used by the trading loop / dashboard: falls back to the
    honest UnavailableNewsFilter when no calendar file is configured or
    the configured path doesn't exist, rather than silently failing."""
    from app.news.filter import UnavailableNewsFilter

    if not calendar_path or not os.path.exists(calendar_path):
        return UnavailableNewsFilter()
    try:
        return CalendarNewsFilter.from_file(calendar_path, minutes_before=minutes_before, minutes_after=minutes_after)
    except Exception:  # noqa: BLE001 - a malformed calendar file must not crash the app
        return UnavailableNewsFilter()
