import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.news.calendar import CalendarEvent, CalendarNewsFilter, build_news_filter, load_calendar_csv, load_calendar_json
from app.news.filter import UnavailableNewsFilter


NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def _events():
    return [
        CalendarEvent(time=NOW + timedelta(hours=2), name="US CPI m/m", impact="HIGH", currency="USD"),
        CalendarEvent(time=NOW + timedelta(hours=5), name="Retail Sales", impact="LOW", currency="USD"),
        CalendarEvent(time=NOW + timedelta(hours=8), name="EUR Trade Balance", impact="HIGH", currency="EUR"),
    ]


def test_no_blackout_far_from_any_event():
    filt = CalendarNewsFilter(_events())
    status = filt.check(NOW)
    assert status.available is True
    assert status.blackout_active is False


def test_blackout_active_within_window_before_event():
    filt = CalendarNewsFilter(_events(), minutes_before=30, minutes_after=30)
    status = filt.check(NOW + timedelta(hours=2) - timedelta(minutes=10))
    assert status.blackout_active is True
    assert "US CPI" in status.reason


def test_blackout_active_within_window_after_event():
    filt = CalendarNewsFilter(_events(), minutes_before=30, minutes_after=30)
    status = filt.check(NOW + timedelta(hours=2) + timedelta(minutes=20))
    assert status.blackout_active is True


def test_no_blackout_just_outside_window():
    filt = CalendarNewsFilter(_events(), minutes_before=30, minutes_after=30)
    status = filt.check(NOW + timedelta(hours=2) - timedelta(minutes=45))
    assert status.blackout_active is False


def test_low_impact_events_never_trigger_blackout():
    filt = CalendarNewsFilter(_events(), minutes_before=60, minutes_after=60)
    status = filt.check(NOW + timedelta(hours=5))
    assert status.blackout_active is False


def test_currency_filter_excludes_irrelevant_events():
    # EUR event should not block a USD/XAU-relevant filter.
    filt = CalendarNewsFilter(_events(), symbol_currencies=("USD", "XAU"))
    status = filt.check(NOW + timedelta(hours=8))
    assert status.blackout_active is False


def test_currency_filter_includes_relevant_events():
    filt = CalendarNewsFilter(_events(), symbol_currencies=("EUR",))
    status = filt.check(NOW + timedelta(hours=8))
    assert status.blackout_active is True


def test_upcoming_high_impact_events_excludes_low_impact_and_past():
    filt = CalendarNewsFilter(_events())
    upcoming = filt.upcoming_high_impact_events(NOW)
    names = [e.name for e in upcoming]
    assert "US CPI m/m" in names
    assert "Retail Sales" not in names  # low impact excluded


def test_load_calendar_csv_round_trip(tmp_path):
    path = tmp_path / "cal.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "name", "impact", "currency"])
        w.writerow([NOW.isoformat(), "FOMC Statement", "HIGH", "USD"])
    events = load_calendar_csv(str(path))
    assert len(events) == 1
    assert events[0].name == "FOMC Statement"
    assert events[0].impact == "HIGH"


def test_load_calendar_json_round_trip(tmp_path):
    path = tmp_path / "cal.json"
    with open(path, "w") as f:
        json.dump([{"time": NOW.isoformat(), "name": "NFP", "impact": "high", "currency": "USD"}], f)
    events = load_calendar_json(str(path))
    assert len(events) == 1
    assert events[0].impact == "HIGH"  # case-insensitive parsing


def test_build_news_filter_falls_back_when_no_path():
    filt = build_news_filter(None)
    assert isinstance(filt, UnavailableNewsFilter)


def test_build_news_filter_falls_back_on_missing_file():
    filt = build_news_filter("/definitely/does/not/exist.csv")
    assert isinstance(filt, UnavailableNewsFilter)


def test_build_news_filter_falls_back_on_malformed_file(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("not,a,valid,calendar\nfile,at,all,here")
    filt = build_news_filter(str(path))
    assert isinstance(filt, UnavailableNewsFilter)


def test_build_news_filter_uses_real_calendar_when_valid(tmp_path):
    path = tmp_path / "cal.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "name", "impact", "currency"])
        w.writerow([NOW.isoformat(), "CPI", "HIGH", "USD"])
    filt = build_news_filter(str(path))
    assert isinstance(filt, CalendarNewsFilter)
