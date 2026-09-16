from datetime import datetime, timezone

from app.news.filter import NewsState, UnavailableNewsFilter


def test_unavailable_filter_reports_state_unavailable():
    filt = UnavailableNewsFilter()
    status = filt.check(datetime.now(timezone.utc))
    assert status.state == NewsState.UNAVAILABLE
    assert "unavailable" in status.reason.lower()


def test_unavailable_filter_never_permits_a_new_trade():
    """This is the core safety fix: UNAVAILABLE must block trading,
    not silently be treated as 'no blackout, so it's fine'."""
    filt = UnavailableNewsFilter()
    for _ in range(5):
        status = filt.check(datetime.now(timezone.utc))
        assert status.permits_new_trade is False


def test_only_clear_state_permits_a_new_trade():
    """Exhaustively confirm the safety invariant: of all four states,
    exactly one (CLEAR) permits a new trade."""
    from app.news.filter import NewsStatus

    results = {}
    for state in NewsState:
        results[state] = NewsStatus(state=state, reason="test").permits_new_trade

    assert results[NewsState.CLEAR] is True
    assert results[NewsState.BLOCKED] is False
    assert results[NewsState.UNAVAILABLE] is False
    assert results[NewsState.UNKNOWN] is False
