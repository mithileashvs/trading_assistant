from app.mt5.interface import OrderRequest
from app.mt5.mock_client import MockMT5Client


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
