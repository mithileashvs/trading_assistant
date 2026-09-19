"""
Paper Account Model (Phase 12).

Maintains a deterministic, mark-to-market simulated account state with margin tracking,
realized and unrealized P/L, cost accounting (spread, slippage, commission, swap),
and seamless adaptation to AccountInfo for risk and safety validation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.mt5.interface import AccountInfo, SymbolSpec, Tick
from app.paper.positions import PaperPosition


@dataclass
class PaperAccount:
    """Deterministic simulated trading account for Paper Trading."""

    starting_balance: float = 10_000.0
    current_balance: float = 10_000.0
    equity: float = 10_000.0
    used_margin: float = 0.0
    free_margin: float = 10_000.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    spread_cost: float = 0.0
    slippage_cost: float = 0.0
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    number_of_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    open_positions: dict[int, PaperPosition] = field(default_factory=dict)
    closed_positions: list[PaperPosition] = field(default_factory=list)
    currency: str = "USD"
    leverage: float = 100.0
    stopout_level: float = 50.0
    trade_allowed: bool = True
    is_demo: bool = True
    current_day: Optional[str] = None
    current_week: Optional[str] = None

    def __init__(
        self,
        starting_balance: float = 10_000.0,
        current_balance: Optional[float] = None,
        equity: Optional[float] = None,
        used_margin: float = 0.0,
        free_margin: Optional[float] = None,
        realized_pnl: float = 0.0,
        unrealized_pnl: float = 0.0,
        commission: float = 0.0,
        swap: float = 0.0,
        spread_cost: float = 0.0,
        slippage_cost: float = 0.0,
        daily_pnl: float = 0.0,
        weekly_pnl: float = 0.0,
        consecutive_losses: int = 0,
        consecutive_wins: int = 0,
        number_of_trades: int = 0,
        winning_trades: int = 0,
        losing_trades: int = 0,
        open_positions: Optional[dict[int, PaperPosition]] = None,
        closed_positions: Optional[list[PaperPosition]] = None,
        currency: str = "USD",
        leverage: float = 100.0,
        stopout_level: float = 50.0,
        trade_allowed: bool = True,
        is_demo: bool = True,
        current_day: Optional[str] = None,
        current_week: Optional[str] = None,
        balance: Optional[float] = None,
        **kwargs: Any,
    ):
        bal = balance if balance is not None else (current_balance if current_balance is not None else starting_balance)
        self.starting_balance = starting_balance if balance is None else balance
        self.current_balance = bal
        self.equity = equity if equity is not None else bal
        self.used_margin = used_margin
        self.free_margin = free_margin if free_margin is not None else (self.equity - self.used_margin)
        self.realized_pnl = realized_pnl
        self.unrealized_pnl = unrealized_pnl
        self.commission = commission
        self.swap = swap
        self.spread_cost = spread_cost
        self.slippage_cost = slippage_cost
        self.daily_pnl = daily_pnl
        self.weekly_pnl = weekly_pnl
        self.consecutive_losses = consecutive_losses
        self.consecutive_wins = consecutive_wins
        self.number_of_trades = number_of_trades
        self.winning_trades = winning_trades
        self.losing_trades = losing_trades
        self.open_positions = open_positions if open_positions is not None else {}
        self.closed_positions = closed_positions if closed_positions is not None else []
        self.currency = currency
        self.leverage = leverage
        self.stopout_level = stopout_level
        self.trade_allowed = trade_allowed
        self.is_demo = is_demo
        self.current_day = current_day
        self.current_week = current_week

    @property
    def balance(self) -> float:
        return self.current_balance

    @balance.setter
    def balance(self, val: float) -> None:
        self.current_balance = val

    @property
    def floating_pnl(self) -> float:
        return self.unrealized_pnl

    @floating_pnl.setter
    def floating_pnl(self, val: float) -> None:
        self.unrealized_pnl = val

    @property
    def margin(self) -> float:
        return self.used_margin

    @margin.setter
    def margin(self, val: float) -> None:
        self.used_margin = val

    @property
    def daily_realized_pnl(self) -> float:
        return self.daily_pnl

    @daily_realized_pnl.setter
    def daily_realized_pnl(self, val: float) -> None:
        self.daily_pnl = val

    @property
    def margin_level(self) -> float:
        if self.used_margin <= 0:
            return float("inf")
        return (self.equity / self.used_margin) * 100.0

    def check_margin_available(self, required_margin: float) -> bool:
        """Returns True if sufficient free margin is available."""
        return self.free_margin >= required_margin

    def is_stopout_breached(self) -> bool:
        """Returns True if margin level has fallen below the stopout threshold."""
        if self.used_margin <= 0:
            return False
        return self.margin_level < self.stopout_level

    def update_floating_pnl(self, positions: list[PaperPosition]) -> None:
        """Recalculates floating P/L, margin, equity, and free margin across a list of positions."""
        tot_unrealized = sum(p.floating_pnl for p in positions)
        lev = max(1.0, float(self.leverage))
        tot_margin = sum((p.volume * 100.0 * getattr(p, "entry_price", 2650.0)) / lev for p in positions)
        self.unrealized_pnl = round(tot_unrealized, 2)
        self.used_margin = round(tot_margin, 2)
        self.equity = round(self.current_balance + self.unrealized_pnl, 2)
        self.free_margin = max(0.0, round(self.equity - self.used_margin, 2))

    def as_account_info(self) -> AccountInfo:
        """Adapts simulated account state to standard AccountInfo."""
        return AccountInfo(
            login=999_999_999,
            balance=self.current_balance,
            equity=self.equity,
            margin=self.used_margin,
            margin_free=self.free_margin,
            currency=self.currency,
            leverage=int(self.leverage),
            trade_allowed=self.trade_allowed,
            is_demo=self.is_demo,
        )

    def _refresh_baselines(self, timestamp: datetime) -> None:
        """Resets daily and weekly realized P/L baselines on date/week transitions."""
        day_str = timestamp.strftime("%Y-%m-%d")
        week_str = f"{timestamp.isocalendar().year}-W{timestamp.isocalendar().week:02d}"

        if self.current_day != day_str:
            self.current_day = day_str
            self.daily_pnl = 0.0

        if self.current_week != week_str:
            self.current_week = week_str
            self.weekly_pnl = 0.0

    def calculate_margin(self, volume: float, price: float, symbol_spec: SymbolSpec) -> float:
        """Calculates margin requirement for a given volume and price."""
        if self.leverage <= 0:
            return 0.0
        contract_size = symbol_spec.contract_size if symbol_spec.contract_size > 0 else 100.0
        return round((volume * contract_size * price) / self.leverage, 2)

    def update_floating(self, symbol_spec: SymbolSpec, tick_or_price: Tick | float, timestamp: Optional[datetime] = None) -> None:
        """Updates open positions, unrealized P/L, used/free margin, and equity."""
        now = timestamp or datetime.now(timezone.utc)
        self._refresh_baselines(now)

        tot_unrealized = 0.0
        tot_margin = 0.0

        for pos in self.open_positions.values():
            if isinstance(tick_or_price, Tick):
                price = tick_or_price.bid if pos.direction == "BUY" else tick_or_price.ask
            else:
                price = float(tick_or_price)

            pos.update_price(price, symbol_spec, update_time=now)
            tot_unrealized += pos.unrealized_pnl
            tot_margin += self.calculate_margin(pos.volume, pos.price_open, symbol_spec)

        self.unrealized_pnl = round(tot_unrealized, 2)
        self.used_margin = round(tot_margin, 2)
        self.equity = round(self.current_balance + self.unrealized_pnl, 2)
        self.free_margin = max(0.0, round(self.equity - self.used_margin, 2))

    def apply_entry(
        self,
        pos: PaperPosition,
        symbol_spec: SymbolSpec,
        commission: float = 0.0,
        spread_cost: float = 0.0,
        slippage_cost: float = 0.0,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Registers a new simulated open position."""
        now = timestamp or datetime.now(timezone.utc)
        self._refresh_baselines(now)

        self.open_positions[pos.ticket] = pos
        self.spread_cost = round(self.spread_cost + spread_cost, 2)
        self.slippage_cost = round(self.slippage_cost + slippage_cost, 2)

        self.update_floating(symbol_spec, pos.price_open, timestamp=now)

    def apply_close(
        self,
        ticket: int,
        exit_price: float,
        symbol_spec: SymbolSpec,
        close_reason: str = "",
        timestamp: Optional[datetime] = None,
        commission: float = 0.0,
        spread_cost: float = 0.0,
        slippage_cost: float = 0.0,
        swap: float = 0.0,
    ) -> Optional[PaperPosition]:
        """Closes an existing position, realizes P/L, and updates metrics and margins."""
        pos = self.open_positions.pop(ticket, None)
        if pos is None:
            return None

        now = timestamp or datetime.now(timezone.utc)
        self._refresh_baselines(now)

        # Calculate gross price pnl
        if pos.direction == "BUY":
            diff = exit_price - pos.price_open
        else:
            diff = pos.price_open - exit_price

        if symbol_spec.tick_size > 0:
            ticks = diff / symbol_spec.tick_size
            gross_pnl = round(ticks * symbol_spec.tick_value * pos.volume, 2)
        else:
            gross_pnl = round(diff * pos.volume * symbol_spec.contract_size, 2)

        # Total round-trip commission for this position (entry comm + exit comm)
        total_position_comm = round(pos.commission + commission, 2)
        net_pnl = round(gross_pnl - total_position_comm - swap, 2)

        pos.is_closed = True
        pos.close_time = now
        pos.close_price = exit_price
        pos.close_reason = close_reason
        pos.realized_pnl = net_pnl
        pos.unrealized_pnl = 0.0
        pos.commission = total_position_comm
        pos.spread_cost = round(pos.spread_cost + spread_cost, 2)
        pos.slippage_cost = round(pos.slippage_cost + slippage_cost, 2)
        pos.swap = round(pos.swap + swap, 2)

        # Update account balances
        self.current_balance = round(self.current_balance + net_pnl, 2)
        self.realized_pnl = round(self.realized_pnl + net_pnl, 2)
        self.commission = round(self.commission + total_position_comm, 2)
        self.spread_cost = round(self.spread_cost + spread_cost, 2)
        self.slippage_cost = round(self.slippage_cost + slippage_cost, 2)
        self.swap = round(self.swap + swap, 2)
        self.daily_pnl = round(self.daily_pnl + net_pnl, 2)
        self.weekly_pnl = round(self.weekly_pnl + net_pnl, 2)
        self.number_of_trades += 1

        if net_pnl > 0:
            self.winning_trades += 1
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.losing_trades += 1
            self.consecutive_losses += 1
            self.consecutive_wins = 0

        self.closed_positions.append(pos)
        self.update_floating(symbol_spec, exit_price, timestamp=now)
        return pos

    def to_dict(self) -> dict[str, Any]:
        """Serializes account state to dictionary."""
        return {
            "starting_balance": self.starting_balance,
            "current_balance": self.current_balance,
            "equity": self.equity,
            "used_margin": self.used_margin,
            "free_margin": self.free_margin,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "commission": self.commission,
            "swap": self.swap,
            "spread_cost": self.spread_cost,
            "slippage_cost": self.slippage_cost,
            "daily_pnl": self.daily_pnl,
            "weekly_pnl": self.weekly_pnl,
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "number_of_trades": self.number_of_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "currency": self.currency,
            "leverage": self.leverage,
            "stopout_level": self.stopout_level,
            "open_positions": [p.to_dict() for p in self.open_positions.values()],
            "closed_positions": [p.to_dict() for p in self.closed_positions],
            "current_day": self.current_day,
            "current_week": self.current_week,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperAccount:
        """Restores account state from dictionary."""
        d = dict(data)
        open_pos_raw = d.pop("open_positions", [])
        closed_pos_raw = d.pop("closed_positions", [])

        account = cls(**d)
        account.open_positions = {
            p_dict["ticket"]: PaperPosition.from_dict(p_dict) for p_dict in open_pos_raw
        }
        account.closed_positions = [PaperPosition.from_dict(p_dict) for p_dict in closed_pos_raw]
        return account
