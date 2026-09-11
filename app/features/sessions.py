"""
Trading session awareness (section 19).

Sessions are defined in UTC using commonly-used approximate hours.
These boundaries are configurable-by-editing here deliberately (not
yet wired to Settings) since they rarely need to change per-deployment;
promote to Settings if that changes.
"""
from __future__ import annotations

from datetime import datetime, timezone

_SESSIONS_UTC = {
    "ASIAN": (0, 9),
    "LONDON": (7, 16),
    "NEW_YORK": (12, 21),
}


def active_sessions(ts: datetime) -> list[str]:
    """Return all sessions active at the given UTC timestamp (can be
    more than one during an overlap window)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts_utc = ts.astimezone(timezone.utc)
    hour = ts_utc.hour
    active = [name for name, (start, end) in _SESSIONS_UTC.items() if start <= hour < end]
    return active


def session_label(ts: datetime) -> str:
    """Single human-readable label, including overlaps."""
    sessions = active_sessions(ts)
    if not sessions:
        return "OFF_HOURS"
    if "LONDON" in sessions and "NEW_YORK" in sessions:
        return "LONDON_NY_OVERLAP"
    return "_".join(sorted(sessions))
