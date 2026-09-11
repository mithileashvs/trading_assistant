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
