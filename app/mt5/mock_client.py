"""
Deterministic mock MT5 client.

This exists because the real `MetaTrader5` package requires an actual
MT5 terminal process (Windows, or Wine) connected to a live broker —
something this development sandbox cannot provide. The mock produces
synthetic-but-realistic OHLCV data (seeded random walk) so the rest of
the pipeline (features, regimes, strategies, risk, backtesting) can be
built and unit-tested end-to-end without a broker connection.

It must NEVER be used for LIVE trading — see
Settings.validate_live_safety(), which refuses to start LIVE while
MT5_USE_MOCK=true.
"""
from __future__ import annotations

import itertools
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
    def __init__(self, seed: int = 42, base_price: float = 2650.0):
        self._connected = False
        self._rng = np.random.default_rng(seed)
        self._base_price = base_price
        self._positions: dict[int, Position] = {}
        self._ticket_counter = itertools.count(start=100000)
        self._known_symbols = {"XAUUSD", "XAUUSDm", "GOLD", "GOLDm"}
        self._current_symbol = "XAUUSD"
        self._client_order_ids: set[str] = set()

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
        price = self._base_price + self._rng.normal(0, 0.5)
        spread = 0.20
        return Tick(
            symbol=symbol,
            time=datetime.now(timezone.utc),
            bid=round(price, 2),
            ask=round(price + spread, 2),
            last=round(price, 2),
            volume=1.0,
        )

    def get_ohlcv(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        if timeframe not in _TF_MINUTES:
            raise ValueError(f"Unsupported timeframe '{timeframe}'")
        minutes = _TF_MINUTES[timeframe]
        # Deterministic-per-symbol/timeframe seeded random walk so tests
        # are reproducible.
        local_rng = np.random.default_rng(abs(hash((symbol, timeframe))) % (2**32))
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

        df = pd.DataFrame(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "tick_volume": tick_volume,
                "spread": spread,
            },
            index=pd.DatetimeIndex(times, name="time"),
        )
        return df.round(2)

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
