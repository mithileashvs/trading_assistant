"""
Paper Trading Position Model (Phase 12).

Represents an individual simulated paper position with full excursion tracking
(MAE / MFE), floating P/L calculation, and seamless adaptation to the Phase 8
ManagedPosition interface.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.execution.engine import ManagedPosition
from app.mt5.interface import SymbolSpec, Tick


@dataclass
class PaperPosition:
    """Simulated paper position tracked locally by the PaperExecutionAdapter."""

    ticket: int
    symbol: str
    direction: str  # "BUY" | "SELL"
    volume: float
    price_open: float = 0.0
    current_price: float = 0.0
    client_order_id: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy: str = ""
    regime: str = ""
    open_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_update_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    close_time: Optional[datetime] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    mae: float = 0.0  # Maximum Adverse Excursion
    mfe: float = 0.0  # Maximum Favorable Excursion
    is_paper: bool = True
    is_closed: bool = False
    commission: float = 0.0
    swap: float = 0.0
    spread_cost: float = 0.0
    slippage_cost: float = 0.0
    entry_price_arg: Optional[float] = None

    def __init__(
        self,
        ticket: int,
        symbol: str,
        direction: str,
        volume: float,
        price_open: float = 0.0,
        current_price: float = 0.0,
        client_order_id: str = "",
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        strategy: str = "",
        regime: str = "",
        open_time: Optional[datetime] = None,
        last_update_time: Optional[datetime] = None,
        close_time: Optional[datetime] = None,
        close_price: Optional[float] = None,
        close_reason: Optional[str] = None,
        realized_pnl: float = 0.0,
        unrealized_pnl: float = 0.0,
        mae: float = 0.0,
        mfe: float = 0.0,
        is_paper: bool = True,
        is_closed: bool = False,
        commission: float = 0.0,
        swap: float = 0.0,
        spread_cost: float = 0.0,
        slippage_cost: float = 0.0,
        entry_price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        **kwargs: Any,
    ):
        self.ticket = ticket
        self.symbol = symbol
        self.direction = direction
        self.volume = volume
        p_open = entry_price if entry_price is not None else price_open
        self.price_open = p_open
        self.current_price = current_price if current_price != 0.0 else p_open
        self.client_order_id = client_order_id or f"order_{ticket}"
        self.stop_loss = sl if sl is not None else stop_loss
        self.take_profit = tp if tp is not None else take_profit
        self.strategy = strategy
        self.regime = regime
        self.open_time = open_time or datetime.now(timezone.utc)
        self.last_update_time = last_update_time or datetime.now(timezone.utc)
        self.close_time = close_time
        self.close_price = close_price
        self.close_reason = close_reason
        self.realized_pnl = realized_pnl
        self.unrealized_pnl = unrealized_pnl
        self.mae = mae
        self.mfe = mfe
        self.is_paper = is_paper
        self.is_closed = is_closed
        self.commission = commission
        self.swap = swap
        self.spread_cost = spread_cost
        self.slippage_cost = slippage_cost

    @property
    def entry_price(self) -> float:
        return self.price_open

    @entry_price.setter
    def entry_price(self, val: float) -> None:
        self.price_open = val

    @property
    def sl(self) -> Optional[float]:
        return self.stop_loss

    @sl.setter
    def sl(self, val: Optional[float]) -> None:
        self.stop_loss = val

    @property
    def tp(self) -> Optional[float]:
        return self.take_profit

    @tp.setter
    def tp(self, val: Optional[float]) -> None:
        self.take_profit = val

    @property
    def floating_pnl(self) -> float:
        return self.unrealized_pnl

    @floating_pnl.setter
    def floating_pnl(self, val: float) -> None:
        self.unrealized_pnl = val

    def to_managed_position(self) -> ManagedPosition:
        """Thin, deterministic adapter returning a ManagedPosition for Phase 8 PositionMonitor."""
        return ManagedPosition(
            ticket=self.ticket,
            symbol=self.symbol,
            direction=self.direction,
            volume=self.volume,
            price_open=self.price_open,
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            open_time=self.open_time,
            strategy=self.strategy,
            regime=self.regime,
            monetary_risk=None,
            is_paper=True,
        )

    def update_price(
        self,
        *args: Any,
        high: Optional[float] = None,
        low: Optional[float] = None,
        current: Optional[float] = None,
        update_time: Optional[datetime] = None,
        **kwargs: Any,
    ) -> None:
        """Mark-to-market revaluation against current price, updating unrealized P/L, MAE, and MFE."""
        if self.is_closed:
            return

        self.last_update_time = update_time or datetime.now(timezone.utc)

        # Case 1: called with (high, low, current) or named args
        if high is not None or low is not None or current is not None or len(args) >= 3:
            h = high if high is not None else float(args[0])
            l = low if low is not None else float(args[1])
            c = current if current is not None else float(args[2])
            self.current_price = c

            if self.direction == "BUY":
                price_diff = c - self.price_open
                adverse = max(0.0, self.price_open - l)
                favorable = max(0.0, h - self.price_open)
            else:
                price_diff = self.price_open - c
                adverse = max(0.0, h - self.price_open)
                favorable = max(0.0, self.price_open - l)

            self.unrealized_pnl = round(price_diff * self.volume * 100.0, 2)
            if adverse > self.mae:
                self.mae = adverse
            if favorable > self.mfe:
                self.mfe = favorable
            return

        # Case 2: called with (price, symbol_spec) or (price,)
        if len(args) >= 1:
            price = float(args[0])
            self.current_price = price
            symbol_spec = args[1] if len(args) > 1 and isinstance(args[1], SymbolSpec) else None

            if self.direction == "BUY":
                price_diff = price - self.price_open
            else:
                price_diff = self.price_open - price

            if symbol_spec and symbol_spec.tick_size > 0:
                ticks = price_diff / symbol_spec.tick_size
                self.unrealized_pnl = round(ticks * symbol_spec.tick_value * self.volume, 2)
            else:
                self.unrealized_pnl = round(price_diff * self.volume * 100.0, 2)

            if self.unrealized_pnl > self.mfe:
                self.mfe = self.unrealized_pnl
            if self.unrealized_pnl < -self.mae:
                self.mae = abs(self.unrealized_pnl)

    def to_dict(self) -> dict[str, Any]:
        """Serializes position state to a JSON-compatible dictionary."""
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "direction": self.direction,
            "volume": self.volume,
            "price_open": self.price_open,
            "current_price": self.current_price,
            "client_order_id": self.client_order_id,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "strategy": self.strategy,
            "regime": self.regime,
            "open_time": self.open_time.isoformat() if self.open_time else None,
            "last_update_time": self.last_update_time.isoformat() if self.last_update_time else None,
            "close_time": self.close_time.isoformat() if self.close_time else None,
            "close_price": self.close_price,
            "close_reason": self.close_reason,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "mae": self.mae,
            "mfe": self.mfe,
            "is_paper": self.is_paper,
            "is_closed": self.is_closed,
            "commission": self.commission,
            "swap": self.swap,
            "spread_cost": self.spread_cost,
            "slippage_cost": self.slippage_cost,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperPosition:
        """Reconstructs a PaperPosition from a dictionary."""
        d = dict(data)
        if d.get("open_time") and isinstance(d["open_time"], str):
            d["open_time"] = datetime.fromisoformat(d["open_time"])
        if d.get("last_update_time") and isinstance(d["last_update_time"], str):
            d["last_update_time"] = datetime.fromisoformat(d["last_update_time"])
        if d.get("close_time") and isinstance(d["close_time"], str):
            d["close_time"] = datetime.fromisoformat(d["close_time"])
        return cls(**d)
