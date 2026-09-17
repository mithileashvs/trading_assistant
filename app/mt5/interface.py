"""
Abstract MT5 client interface.

Only code behind this interface may ever talk to a real broker. Every
other layer of the system (features, regimes, strategies, risk) depends
on this interface, never on the `MetaTrader5` package directly — this
is what lets us swap MockMT5Client -> RealMT5Client with a one-line
config change (see app/config/settings.py: MT5_USE_MOCK).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd


@dataclass
class SymbolSpec:
    """Broker-reported specification for a trading symbol. Never assume
    these values — always read them from the broker (section 3)."""

    name: str
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    tick_size: float
    tick_value: float
    digits: int
    stops_level_points: int
    freeze_level_points: int
    trade_allowed: bool
    spread_points: float


@dataclass
class AccountInfo:
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    currency: str
    leverage: int
    trade_allowed: bool
    is_demo: Optional[bool] = None


@dataclass
class Tick:
    symbol: str
    time: datetime
    bid: float
    ask: float
    last: float
    volume: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class OrderRequest:
    symbol: str
    direction: str  # "BUY" | "SELL"
    volume: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    comment: str = ""
    magic: int = 0
    client_order_id: Optional[str] = None  # for duplicate-order protection


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[int]
    deal_id: Optional[int]
    price: Optional[float]
    volume: Optional[float]
    retcode: Optional[int]
    comment: str
    raw: dict = field(default_factory=dict)


@dataclass
class Position:
    ticket: int
    symbol: str
    direction: str
    volume: float
    price_open: float
    price_current: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    profit: float
    open_time: datetime
    magic: int = 0
    comment: str = ""


# Standardized values for OrderResult.raw["status"] (Phase 6). OrderResult.success
# is a plain bool and can only ever express filled-vs-not -- it has no way to
# express "we genuinely don't know what happened" (e.g. the connection dropped
# after a request was sent but before a confirmation came back). Rather than
# change OrderResult's shape (which every existing caller already depends on),
# clients that can distinguish this case set raw["status"] to one of these
# values on top of the existing fields. success is ALWAYS False when status is
# UNKNOWN -- nothing may ever assume success it cannot confirm (fail-closed).
# Absence of this key (the common case for existing code, real or mock) simply
# means "not distinguished" -- callers should keep relying on `success` as
# before; this is purely additive.
EXECUTION_STATUS_FILLED = "FILLED"
EXECUTION_STATUS_REJECTED = "REJECTED"
EXECUTION_STATUS_UNKNOWN = "UNKNOWN"

# Shared "how old can a tick be and still be safe to fill/close against"
# threshold (Phase 6), used by MockMT5Client's own order validation and by
# ExecutionEngine's PAPER-mode simulation (app.execution.engine). Deliberately
# NOT wired into RealMT5Client / LIVE order submission -- that would be a
# change to real execution behavior, out of scope for the mock-only phase
# this was introduced in.
STALE_TICK_SECONDS = 300


class IMT5Client(abc.ABC):
    """Everything the rest of the system is allowed to know about MT5."""

    @abc.abstractmethod
    def connect(self) -> bool: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    @abc.abstractmethod
    def discover_symbol(self, candidates: list[str]) -> Optional[str]:
        """Try each candidate symbol name in order; return the first one
        the broker actually exposes and allows trading on, else None."""
        ...

    @abc.abstractmethod
    def get_symbol_spec(self, symbol: str) -> SymbolSpec: ...

    @abc.abstractmethod
    def get_account_info(self) -> AccountInfo: ...

    @abc.abstractmethod
    def get_tick(self, symbol: str) -> Tick: ...

    @abc.abstractmethod
    def get_ohlcv(
        self, symbol: str, timeframe: str, count: int
    ) -> pd.DataFrame:
        """Return a DataFrame indexed by UTC timestamp with columns:
        open, high, low, close, tick_volume, spread."""
        ...

    @abc.abstractmethod
    def get_open_positions(self, symbol: Optional[str] = None) -> list[Position]: ...

    @abc.abstractmethod
    def submit_order(self, request: OrderRequest) -> OrderResult: ...

    @abc.abstractmethod
    def close_position(self, ticket: int) -> OrderResult: ...

    @abc.abstractmethod
    def close_position_partial(self, ticket: int, volume: float) -> OrderResult:
        """Close only part of an open position's volume (support 15/22).
        Implementations should reject (OrderResult.success=False) if
        volume >= the position's full volume; use close_position for a
        full close instead."""
        ...

    @abc.abstractmethod
    def modify_position(
        self, ticket: int, stop_loss: Optional[float], take_profit: Optional[float]
    ) -> OrderResult: ...
