from datetime import datetime, timezone

from app.news.filter import UnavailableNewsFilter


def test_unavailable_filter_reports_itself_honestly():
    filt = UnavailableNewsFilter()
    status = filt.check(datetime.now(timezone.utc))
    assert status.available is False
    assert status.blackout_active is False
    assert "unavailable" in status.reason.lower()


def test_unavailable_filter_never_blocks_trades():
    filt = UnavailableNewsFilter()
    for _ in range(5):
        status = filt.check(datetime.now(timezone.utc))
        assert status.blackout_active is False
