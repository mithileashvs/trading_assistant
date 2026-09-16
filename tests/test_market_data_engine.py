from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.config.settings import Settings
from app.market_data.engine import (
    MarketDataEngine,
    StaleMarketDataError,
    SymbolDiscoveryError,
)
from app.mt5.mock_client import MockMT5Client


def _settings(**overrides):
    return Settings(_env_file=None, **overrides)


def test_resolve_symbol_success():
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings())
    resolved = engine.resolve_symbol()
    assert resolved.name in {"XAUUSD", "XAUUSDm", "GOLD", "GOLDm"}
    assert resolved.spec.trade_allowed


def test_resolve_symbol_is_cached():
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings())
    first = engine.resolve_symbol()
    second = engine.resolve_symbol()
    assert first is second


def test_resolve_symbol_failure_raises():
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings(SYMBOL_CANDIDATES="NOPE,ALSO_NOPE"))
    with pytest.raises(SymbolDiscoveryError):
        engine.resolve_symbol()


def test_get_ohlcv_returns_data():
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings())
    df = engine.get_ohlcv("H4", count=30)
    assert len(df) == 30


def test_get_tick_returns_recent_tick():
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings())
    tick = engine.get_tick()
    assert tick.ask >= tick.bid


def test_stale_ohlcv_detected():
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings(MARKET_DATA_MAX_STALENESS_SECONDS=1))

    stale_index = pd.DatetimeIndex(
        [datetime.now(timezone.utc) - timedelta(hours=5)], name="time"
    )
    stale_df = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "tick_volume": [1], "spread": [1]},
        index=stale_index,
    )

    with pytest.raises(StaleMarketDataError):
        engine._assert_fresh(stale_df)


def test_empty_ohlcv_detected_as_stale():
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings())
    empty_df = pd.DataFrame(columns=["open", "high", "low", "close", "tick_volume", "spread"])
    with pytest.raises(StaleMarketDataError):
        engine._assert_fresh(empty_df)


# --- timeframe-aware freshness (audit section 3.1 fix follow-on) --------------

def test_h1_candle_up_to_one_bar_period_old_is_not_stale():
    """An H1 candle's timestamp is its OPEN time -- a bar that started
    50 minutes ago is still the CURRENT H1 bar, not stale data."""
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings())
    recent_index = pd.DatetimeIndex(
        [datetime.now(timezone.utc) - timedelta(minutes=50)], name="time"
    )
    df = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "tick_volume": [1], "spread": [1]},
        index=recent_index,
    )
    engine._assert_fresh(df, timeframe="H1")  # must not raise


def test_h4_candle_up_to_one_bar_period_old_is_not_stale():
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings())
    recent_index = pd.DatetimeIndex(
        [datetime.now(timezone.utc) - timedelta(hours=3, minutes=50)], name="time"
    )
    df = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "tick_volume": [1], "spread": [1]},
        index=recent_index,
    )
    engine._assert_fresh(df, timeframe="H4")  # must not raise


def test_h1_candle_older_than_one_bar_period_plus_buffer_is_stale():
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings(MARKET_DATA_MAX_STALENESS_SECONDS=60))
    old_index = pd.DatetimeIndex(
        [datetime.now(timezone.utc) - timedelta(hours=3)], name="time"
    )
    df = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "tick_volume": [1], "spread": [1]},
        index=old_index,
    )
    with pytest.raises(StaleMarketDataError):
        engine._assert_fresh(df, timeframe="H1")


def test_h4_candle_older_than_one_bar_period_plus_buffer_is_stale():
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings(MARKET_DATA_MAX_STALENESS_SECONDS=60))
    old_index = pd.DatetimeIndex(
        [datetime.now(timezone.utc) - timedelta(hours=10)], name="time"
    )
    df = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "tick_volume": [1], "spread": [1]},
        index=old_index,
    )
    with pytest.raises(StaleMarketDataError):
        engine._assert_fresh(df, timeframe="H4")


def test_unknown_timeframe_defaults_to_strictest_known_bar_period():
    """An unrecognized timeframe must fail closed (use the SMALLEST
    known bar duration, so it's more likely to reject as stale) rather
    than assume a large, lenient duration that could let genuinely
    stale data through unnoticed."""
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings(MARKET_DATA_MAX_STALENESS_SECONDS=60))
    old_index = pd.DatetimeIndex(
        [datetime.now(timezone.utc) - timedelta(hours=1)], name="time"
    )
    df = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "tick_volume": [1], "spread": [1]},
        index=old_index,
    )
    with pytest.raises(StaleMarketDataError):
        engine._assert_fresh(df, timeframe="SOME_UNKNOWN_TIMEFRAME")


def test_live_h4_h1_m15_all_pass_freshness_via_full_engine():
    """Integration check: fetching real (mock, coherent) H4/H1/M15 data
    through the full engine must not spuriously raise stale errors now
    that bars have realistic (non-artificially-recent) timestamps."""
    client = MockMT5Client()
    engine = MarketDataEngine(client, _settings())
    for tf in ("M15", "H1", "H4"):
        df = engine.get_ohlcv(tf, count=250)  # must not raise
        assert len(df) == 250
