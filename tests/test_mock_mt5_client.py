from app.mt5.interface import OrderRequest
from app.mt5.mock_client import MockMT5Client
import pandas as pd


def test_connect_disconnect():
    c = MockMT5Client()
    assert c.is_connected() is False
    c.connect()
    assert c.is_connected() is True
    c.disconnect()
    assert c.is_connected() is False


def test_symbol_discovery_returns_first_match():
    c = MockMT5Client()
    symbol = c.discover_symbol(["NOTREAL", "XAUUSDm", "XAUUSD"])
    assert symbol == "XAUUSDm"


def test_symbol_discovery_returns_none_if_no_match():
    c = MockMT5Client()
    assert c.discover_symbol(["NOTREAL", "ALSO_FAKE"]) is None


def test_symbol_spec_never_hardcodes_lot_size_zero():
    c = MockMT5Client()
    spec = c.get_symbol_spec("XAUUSD")
    assert spec.volume_min > 0
    assert spec.volume_step > 0
    assert spec.contract_size > 0


def test_ohlcv_shape_and_no_lookahead_ordering():
    c = MockMT5Client()
    df = c.get_ohlcv("XAUUSD", "M15", count=100)
    assert len(df) == 100
    assert list(df.columns) == ["open", "high", "low", "close", "tick_volume", "spread"]
    assert df.index.is_monotonic_increasing  # oldest -> newest, no shuffling


def test_ohlcv_reproducible_for_same_symbol_timeframe():
    c = MockMT5Client()
    df1 = c.get_ohlcv("XAUUSD", "H1", count=20)
    df2 = c.get_ohlcv("XAUUSD", "H1", count=20)
    assert (df1["close"] == df2["close"]).all()


# --- multi-timeframe data consistency (audit section 3.1) ---------------------

def test_h1_bars_are_derived_from_the_same_m15_series_not_independent():
    """H4/H1/M15 must represent the SAME underlying market timeline --
    aggregating the M15 series this client returns must exactly
    reproduce the H1 series it returns for the same symbol, not just
    be statistically similar."""
    c = MockMT5Client()
    m15 = c.get_ohlcv("XAUUSD", "M15", 500)
    h1 = c.get_ohlcv("XAUUSD", "H1", 100)

    agg = {"open": "first", "high": "max", "low": "min", "close": "last",
           "tick_volume": "sum", "spread": "mean"}
    manual_h1 = m15.resample("1h", label="left", closed="left").agg(agg).dropna().round(2)

    overlap = h1.index.intersection(manual_h1.index)
    assert len(overlap) > 0
    for col in ("open", "high", "low", "close"):
        assert (h1.loc[overlap, col] == manual_h1.loc[overlap, col]).all(), (
            f"H1 '{col}' does not match aggregation of the returned M15 series -- "
            "timeframes are not coherent."
        )


def test_h4_bars_are_derived_from_the_same_m15_series_not_independent():
    c = MockMT5Client()
    m15 = c.get_ohlcv("XAUUSD", "M15", 2000)
    h4 = c.get_ohlcv("XAUUSD", "H4", 50)

    agg = {"open": "first", "high": "max", "low": "min", "close": "last",
           "tick_volume": "sum", "spread": "mean"}
    manual_h4 = m15.resample("4h", label="left", closed="left").agg(agg).dropna().round(2)

    overlap = h4.index.intersection(manual_h4.index)
    assert len(overlap) > 0
    for col in ("open", "high", "low", "close"):
        assert (h4.loc[overlap, col] == manual_h4.loc[overlap, col]).all()


def test_h4_bars_are_also_coherent_with_h1_bars():
    """Cross-check the full chain: H4 aggregated from H1 must also
    match, since H1 itself is aggregated from the same M15 base."""
    c = MockMT5Client()
    h1 = c.get_ohlcv("XAUUSD", "H1", 800)
    h4 = c.get_ohlcv("XAUUSD", "H4", 50)

    agg = {"open": "first", "high": "max", "low": "min", "close": "last",
           "tick_volume": "sum", "spread": "mean"}
    manual_h4 = h1.resample("4h", label="left", closed="left").agg(agg).dropna().round(2)

    overlap = h4.index.intersection(manual_h4.index)
    assert len(overlap) > 0
    for col in ("open", "high", "low", "close"):
        assert (h4.loc[overlap, col] == manual_h4.loc[overlap, col]).all()


def test_different_symbols_have_independent_series():
    c = MockMT5Client()
    xau = c.get_ohlcv("XAUUSD", "H1", 50)
    gold = c.get_ohlcv("GOLD", "H1", 50)
    assert not xau["close"].equals(gold["close"])


def test_repeated_calls_return_consistent_cached_data():
    """Calling get_ohlcv multiple times within one client instance must
    not silently regenerate a different series each time -- the base
    M15 series is cached per symbol precisely so H4/H1/M15 stay
    coherent with each other across repeated calls too."""
    c = MockMT5Client()
    first = c.get_ohlcv("XAUUSD", "M15", 500)
    second = c.get_ohlcv("XAUUSD", "M15", 500)
    assert first["close"].equals(second["close"])

    # A larger subsequent request must still agree with the earlier,
    # smaller request over their overlapping tail.
    larger = c.get_ohlcv("XAUUSD", "M15", 800)
    assert larger.tail(500)["close"].reset_index(drop=True).equals(first["close"].reset_index(drop=True))


def test_no_future_leakage_in_ohlcv_timestamps():
    from datetime import datetime, timezone
    c = MockMT5Client()
    now = pd.Timestamp(datetime.now(timezone.utc))
    for tf in ("M15", "H1", "H4"):
        df = c.get_ohlcv("XAUUSD", tf, 50)
        assert df.index[-1] <= now, f"{tf} returned a bar timestamped in the future."


def test_m15_h1_h4_timestamps_are_all_utc_aware_and_comparable():
    c = MockMT5Client()
    for tf in ("M15", "H1", "H4"):
        df = c.get_ohlcv("XAUUSD", tf, 20)
        assert df.index.tz is not None, f"{tf} index is not timezone-aware."


def test_get_tick_price_is_consistent_with_latest_m15_close():
    """The current quote should not float independently of the most
    recent OHLCV data once history has been generated for the symbol."""
    c = MockMT5Client()
    m15 = c.get_ohlcv("XAUUSD", "M15", 100)
    last_close = float(m15["close"].iloc[-1])
    tick = c.get_tick("XAUUSD")
    # Tick price is anchored to the latest M15 close plus small noise
    # (std 0.5) and a fixed spread -- should stay in a tight band, not
    # be anchored to a completely unrelated static base price.
    assert abs(tick.bid - last_close) < 5.0


def test_price_path_is_stable_across_fresh_processes():
    """Regression test for a real reproducibility bug: seeding via
    Python's built-in hash() is randomized per-process for strings
    (PYTHONHASHSEED), so the "same" seed silently produced a DIFFERENT
    price path every time a fresh process ran, even though results were
    internally consistent within one process's lifetime. This was
    invisible for short walks but caused real, intermittent test
    failures once the base series grew long enough (20,000 bars) to
    accumulate meaningfully different drift per process. Spawns a
    genuinely separate Python process (not just a new MockMT5Client
    instance) to prove the fix holds across process boundaries, not
    just within one."""
    import os
    import subprocess
    import sys

    script = (
        "from app.mt5.mock_client import MockMT5Client\n"
        "c = MockMT5Client()\n"
        "print(c.get_ohlcv('XAUUSD', 'M15', 100)['close'].iloc[-1])\n"
    )
    env = {**os.environ, "PYTHONPATH": "."}
    results = set()
    for _ in range(3):
        out = subprocess.run(
            [sys.executable, "-c", script], cwd=".", capture_output=True, text=True, timeout=30, env=env,
        )
        assert out.returncode == 0, out.stderr
        results.add(out.stdout.strip())
    assert len(results) == 1, f"Price path differs across fresh processes: {results}"


def test_submit_order_creates_position():
    c = MockMT5Client()
    result = c.submit_order(OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1))
    assert result.success
    positions = c.get_open_positions("XAUUSD")
    assert len(positions) == 1
    assert positions[0].direction == "BUY"


def test_duplicate_client_order_id_rejected():
    c = MockMT5Client()
    req = OrderRequest(symbol="XAUUSD", direction="BUY", volume=0.1, client_order_id="abc123")
    first = c.submit_order(req)
    second = c.submit_order(req)
    assert first.success is True
    assert second.success is False
    assert second.comment == "DUPLICATE_ORDER_REJECTED"


def test_close_position():
    c = MockMT5Client()
    result = c.submit_order(OrderRequest(symbol="XAUUSD", direction="SELL", volume=0.1))
    close_result = c.close_position(result.order_id)
    assert close_result.success
    assert c.get_open_positions("XAUUSD") == []


def test_close_nonexistent_position_fails_safely():
    c = MockMT5Client()
    result = c.close_position(999999)
    assert result.success is False
    assert result.comment == "POSITION_NOT_FOUND"
