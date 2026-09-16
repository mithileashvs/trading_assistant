"""
Deterministic mock MT5 client.

This exists because the real `MetaTrader5` package requires an actual
MT5 terminal process (Windows, or Wine) connected to a live broker —
something this development sandbox cannot provide. The mock produces
synthetic-but-realistic OHLCV data (seeded random walk) so the rest of
the pipeline (features, regimes, strategies, risk, backtesting) can be
built and unit-tested end-to-end without a broker connection.

MULTI-TIMEFRAME COHERENCE: H4, H1, and M15 for a given symbol are all
derived from ONE underlying M15 series (see _get_m15_base_series /
get_ohlcv) — never independently generated per timeframe. Aggregating
the M15 series this client returns must reproduce the H1/H4 series it
returns for the same symbol, bar-for-bar over any overlapping window;
see tests/test_mock_mt5_client.py for the tests that verify this.

It must NEVER be used for LIVE trading — see
Settings.validate_live_safety(), which refuses to start LIVE while
MT5_USE_MOCK=true.
"""
from __future__ import annotations

import itertools
import zlib
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

from app.mt5.interface import (
    AccountInfo,
    IMT5Client,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
)


def _stable_seed(*parts: str) -> int:
    """A process-stable seed derived from string parts.

    Python's built-in hash() is intentionally randomized per-process for
    strings (PYTHONHASHSEED, a security feature since Python 3.3) --
    using it to seed a numpy Generator means the "same" seed produces a
    DIFFERENT random sequence every time a fresh Python process runs,
    even though it's stable within one process's lifetime. That's
    invisible for short sequences (limited accumulated drift) but
    becomes a real reproducibility bug for long ones (e.g. the 20,000-bar
    base M15 series below, where drift over that many bars can easily
    swing the resulting "current price" by hundreds of dollars between
    process runs). zlib.crc32 is deterministic across processes, so the
    same symbol always seeds the same price path everywhere.
    """
    joined = "|".join(parts).encode("utf-8")
    return zlib.crc32(joined)


_TF_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


class MockMT5Client(IMT5Client):
    # Generous default buffer of M15 bars kept per symbol (~208 days).
    # H1/H4/D1 requests are aggregated from this SAME series rather than
    # generated independently, so all timeframes for a symbol always
    # represent one coherent underlying timeline. If a caller ever
    # requests more M15 history than this buffer holds, the buffer is
    # regenerated larger (still seeded deterministically by symbol) --
    # see _get_m15_base_series().
    _DEFAULT_BASE_M15_BARS = 20_000

    _AGG = {"open": "first", "high": "max", "low": "min", "close": "last",
            "tick_volume": "sum", "spread": "mean"}
    _HIGHER_TF_RULE = {"H1": "1h", "H4": "4h", "D1": "1D"}

    def __init__(self, seed: int = 42, base_price: float = 2650.0):
        self._connected = False
        self._rng = np.random.default_rng(seed)
        self._base_price = base_price
        self._positions: dict[int, Position] = {}
        self._ticket_counter = itertools.count(start=100000)
        self._known_symbols = {"XAUUSD", "XAUUSDm", "GOLD", "GOLDm"}
        self._current_symbol = "XAUUSD"
        self._client_order_ids: set[str] = set()
        self._m15_base: dict[str, pd.DataFrame] = {}  # symbol -> cached coherent M15 series

    # -- connection ---------------------------------------------------
    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # -- symbol discovery (section 3) ----------------------------------
    def discover_symbol(self, candidates: list[str]) -> Optional[str]:
        for candidate in candidates:
            if candidate in self._known_symbols:
                self._current_symbol = candidate
                return candidate
        return None

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self._known_symbols:
            raise ValueError(f"Unknown symbol '{symbol}' in mock broker.")
        return SymbolSpec(
            name=symbol,
            contract_size=100.0,
            volume_min=0.01,
            volume_max=50.0,
            volume_step=0.01,
            tick_size=0.01,
            tick_value=1.0,
            digits=2,
            stops_level_points=50,
            freeze_level_points=0,
            trade_allowed=True,
            spread_points=20.0,
        )

    # -- account --------------------------------------------------------
    def get_account_info(self) -> AccountInfo:
        equity = 10_000.0 + sum(p.profit for p in self._positions.values())
        return AccountInfo(
            login=999999,
            balance=10_000.0,
            equity=equity,
            margin=0.0,
            margin_free=equity,
            currency="USD",
            leverage=100,
            trade_allowed=True,
            is_demo=True,  # the mock client is inherently a simulation
        )

    # -- market data ------------------------------------------------------
    def get_tick(self, symbol: str) -> Tick:
        # Anchored around the latest known M15 close (if any history has
        # been generated yet for this symbol) rather than the static
        # base_price, so the "current" quote stays consistent with the
        # most recent OHLCV data instead of floating independently.
        base = self._m15_base.get(symbol)
        anchor = float(base["close"].iloc[-1]) if base is not None and len(base) else self._base_price
        price = anchor + self._rng.normal(0, 0.5)
        spread = 0.20
        return Tick(
            symbol=symbol,
            time=datetime.now(timezone.utc),
            bid=round(price, 2),
            ask=round(price + spread, 2),
            last=round(price, 2),
            volume=1.0,
        )

    def _generate_m15_series(self, symbol: str, num_bars: int) -> pd.DataFrame:
        """The single source-of-truth M15 series for `symbol`. Seeded by
        the SYMBOL ONLY (never the timeframe) -- this is what makes
        H1/H4/D1 (aggregated from this same series, see get_ohlcv)
        represent the same underlying timeline as the M15 data, instead
        of each timeframe being an independently-generated random walk."""
        local_rng = np.random.default_rng(_stable_seed(symbol))
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        now -= timedelta(minutes=now.minute % 15)  # align to a real M15 boundary
        times = [now - timedelta(minutes=15 * i) for i in range(num_bars)][::-1]

        returns = local_rng.normal(0, 0.0009, size=num_bars)
        close = self._base_price * np.cumprod(1 + returns)
        open_ = np.roll(close, 1)
        open_[0] = self._base_price
        high = np.maximum(open_, close) * (1 + np.abs(local_rng.normal(0, 0.0006, num_bars)))
        low = np.minimum(open_, close) * (1 - np.abs(local_rng.normal(0, 0.0006, num_bars)))
        tick_volume = local_rng.integers(50, 500, size=num_bars)
        spread = local_rng.integers(10, 40, size=num_bars)

        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close,
             "tick_volume": tick_volume, "spread": spread},
            index=pd.DatetimeIndex(times, name="time"),
        ).round(2)

    def _get_m15_base_series(self, symbol: str, min_bars: int) -> pd.DataFrame:
        needed = max(min_bars, self._DEFAULT_BASE_M15_BARS)
        cached = self._m15_base.get(symbol)
        if cached is None or len(cached) < needed:
            cached = self._generate_m15_series(symbol, needed)
            self._m15_base[symbol] = cached
        return cached

    def _legacy_independent_ohlcv(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """M1/M5 are FINER than the M15 base series and cannot be
        derived from it (aggregation only works coarser, never finer).
        Neither timeframe is used anywhere in this codebase's live
        pipeline (only H4/H1/M15 are) -- this independently-seeded
        fallback is kept only so the two enum values remain callable,
        clearly documented rather than silently implying a coherence
        guarantee that isn't actually possible here."""
        minutes = _TF_MINUTES[timeframe]
        local_rng = np.random.default_rng(_stable_seed(symbol, timeframe))
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        times = [now - timedelta(minutes=minutes * i) for i in range(count)][::-1]

        returns = local_rng.normal(0, 0.0009, size=count)
        close = self._base_price * np.cumprod(1 + returns)
        open_ = np.roll(close, 1)
        open_[0] = self._base_price
        high = np.maximum(open_, close) * (1 + np.abs(local_rng.normal(0, 0.0006, count)))
        low = np.minimum(open_, close) * (1 - np.abs(local_rng.normal(0, 0.0006, count)))
        tick_volume = local_rng.integers(50, 500, size=count)
        spread = local_rng.integers(10, 40, size=count)

        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close,
             "tick_volume": tick_volume, "spread": spread},
            index=pd.DatetimeIndex(times, name="time"),
        ).round(2)

    def get_ohlcv(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        if timeframe not in _TF_MINUTES:
            raise ValueError(f"Unsupported timeframe '{timeframe}'")

        if timeframe == "M15":
            base = self._get_m15_base_series(symbol, count)
            return base.tail(count).round(2)

        if timeframe in self._HIGHER_TF_RULE:
            # Enough M15 bars to produce `count` COMPLETE higher-timeframe
            # candles, plus a small buffer for boundary alignment.
            m15_bars_needed = (_TF_MINUTES[timeframe] * count) // 15 + 32
            base = self._get_m15_base_series(symbol, m15_bars_needed)
            rule = self._HIGHER_TF_RULE[timeframe]
            resampled = base.resample(rule, label="left", closed="left").agg(self._AGG).dropna()
            return resampled.tail(count).round(2)

        # M1 / M5 -- see _legacy_independent_ohlcv's docstring.
        return self._legacy_independent_ohlcv(symbol, timeframe, count)

    # -- positions / trading ------------------------------------------------
    def get_open_positions(self, symbol: Optional[str] = None) -> list[Position]:
        positions = list(self._positions.values())
        if symbol:
            positions = [p for p in positions if p.symbol == symbol]
        return positions

    def submit_order(self, request: OrderRequest) -> OrderResult:
        if request.client_order_id and request.client_order_id in self._client_order_ids:
            return OrderResult(
                success=False,
                order_id=None,
                deal_id=None,
                price=None,
                volume=None,
                retcode=-1,
                comment="DUPLICATE_ORDER_REJECTED",
            )
        if request.client_order_id:
            self._client_order_ids.add(request.client_order_id)

        tick = self.get_tick(request.symbol)
        fill_price = tick.ask if request.direction == "BUY" else tick.bid
        ticket = next(self._ticket_counter)
        self._positions[ticket] = Position(
            ticket=ticket,
            symbol=request.symbol,
            direction=request.direction,
            volume=request.volume,
            price_open=fill_price,
            price_current=fill_price,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            profit=0.0,
            open_time=datetime.now(timezone.utc),
            magic=request.magic,
            comment=request.comment,
        )
        return OrderResult(
            success=True,
            order_id=ticket,
            deal_id=ticket,
            price=fill_price,
            volume=request.volume,
            retcode=10009,  # TRADE_RETCODE_DONE, mirroring real MT5
            comment="FILLED",
        )

    def close_position(self, ticket: int) -> OrderResult:
        pos = self._positions.pop(ticket, None)
        if pos is None:
            return OrderResult(
                success=False,
                order_id=None,
                deal_id=None,
                price=None,
                volume=None,
                retcode=-1,
                comment="POSITION_NOT_FOUND",
            )
        tick = self.get_tick(pos.symbol)
        close_price = tick.bid if pos.direction == "BUY" else tick.ask
        return OrderResult(
            success=True,
            order_id=pos.ticket,
            deal_id=pos.ticket,
            price=close_price,
            volume=pos.volume,
            retcode=10009,
            comment="CLOSED",
        )

    def close_position_partial(self, ticket: int, volume: float) -> OrderResult:
        pos = self._positions.get(ticket)
        if pos is None:
            return OrderResult(
                success=False, order_id=None, deal_id=None, price=None,
                volume=None, retcode=-1, comment="POSITION_NOT_FOUND",
            )
        if volume <= 0 or volume >= pos.volume:
            return OrderResult(
                success=False, order_id=None, deal_id=None, price=None,
                volume=None, retcode=-1,
                comment="INVALID_PARTIAL_VOLUME (must be > 0 and < full position volume)",
            )
        tick = self.get_tick(pos.symbol)
        close_price = tick.bid if pos.direction == "BUY" else tick.ask
        pos.volume = round(pos.volume - volume, 8)
        return OrderResult(
            success=True,
            order_id=pos.ticket,
            deal_id=pos.ticket,
            price=close_price,
            volume=volume,
            retcode=10009,
            comment="PARTIALLY_CLOSED",
        )

    def modify_position(
        self, ticket: int, stop_loss: Optional[float], take_profit: Optional[float]
    ) -> OrderResult:
        pos = self._positions.get(ticket)
        if pos is None:
            return OrderResult(
                success=False,
                order_id=None,
                deal_id=None,
                price=None,
                volume=None,
                retcode=-1,
                comment="POSITION_NOT_FOUND",
            )
        if stop_loss is not None:
            pos.stop_loss = stop_loss
        if take_profit is not None:
            pos.take_profit = take_profit
        return OrderResult(
            success=True,
            order_id=pos.ticket,
            deal_id=None,
            price=pos.price_current,
            volume=pos.volume,
            retcode=10009,
            comment="MODIFIED",
        )
