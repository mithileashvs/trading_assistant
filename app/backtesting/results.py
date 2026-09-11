"""
Backtest result data models (sections 23-25). Every closed trade
records the full context needed for later performance breakdowns by
year/month/session/regime/strategy/direction (section 25).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class BacktestTrade:
    symbol: str
    direction: str  # "BUY" | "SELL"
    strategy: str
    regime: str
    score: Optional[int]

    open_time: datetime
    close_time: datetime
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    lots: float

    pnl: float  # net of commission and swap
    gross_pnl: float
    commission: float
    swap: float
    r_multiple: Optional[float]

    mae: float  # maximum adverse excursion, in price terms (always >= 0)
    mfe: float  # maximum favorable excursion, in price terms (always >= 0)

    exit_reason: str  # "STOP_LOSS" | "TAKE_PROFIT" | "END_OF_DATA"
    session: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "direction": self.direction, "strategy": self.strategy,
            "regime": self.regime, "score": self.score,
            "open_time": self.open_time.isoformat(), "close_time": self.close_time.isoformat(),
            "entry_price": self.entry_price, "exit_price": self.exit_price,
            "stop_loss": self.stop_loss, "take_profit": self.take_profit, "lots": self.lots,
            "pnl": self.pnl, "gross_pnl": self.gross_pnl, "commission": self.commission,
            "swap": self.swap, "r_multiple": self.r_multiple, "mae": self.mae, "mfe": self.mfe,
            "exit_reason": self.exit_reason, "session": self.session,
        }


@dataclass
class EquityPoint:
    time: datetime
    equity: float


@dataclass
class BacktestResult:
    symbol: str
    starting_balance: float
    ending_balance: float
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    bars_evaluated: int = 0
    config: dict = field(default_factory=dict)

    @property
    def net_profit(self) -> float:
        return self.ending_balance - self.starting_balance
