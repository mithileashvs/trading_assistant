from datetime import datetime, timezone

from app.features.sessions import active_sessions, session_label


def test_asian_session_only():
    ts = datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)
    assert active_sessions(ts) == ["ASIAN"]
    assert session_label(ts) == "ASIAN"


def test_london_ny_overlap():
    ts = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)
    sessions = active_sessions(ts)
    assert "LONDON" in sessions and "NEW_YORK" in sessions
    assert session_label(ts) == "LONDON_NY_OVERLAP"


def test_off_hours():
    ts = datetime(2026, 1, 1, 22, 30, tzinfo=timezone.utc)
    assert active_sessions(ts) == []
    assert session_label(ts) == "OFF_HOURS"


def test_naive_datetime_treated_as_utc():
    ts = datetime(2026, 1, 1, 2, 0)  # no tzinfo
    assert active_sessions(ts) == ["ASIAN"]
