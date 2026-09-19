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
    spread_cost: float = 0.0
    slippage_cost: float = 0.0
    is_partial: bool = False

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "direction": self.direction, "strategy": self.strategy,
            "regime": self.regime, "score": self.score,
            "open_time": self.open_time.isoformat(), "close_time": self.close_time.isoformat(),
            "entry_price": self.entry_price, "exit_price": self.exit_price,
            "stop_loss": self.stop_loss, "take_profit": self.take_profit, "lots": self.lots,
            "pnl": self.pnl, "gross_pnl": self.gross_pnl, "commission": self.commission,
            "swap": self.swap, "spread_cost": self.spread_cost, "slippage_cost": self.slippage_cost,
            "is_partial": self.is_partial, "r_multiple": self.r_multiple, "mae": self.mae, "mfe": self.mfe,
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
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    timeframe: str = "M15"
    rejections: dict[str, int] = field(default_factory=dict)
    rejection_details: list[dict] = field(default_factory=list)
    data_quality: dict = field(default_factory=dict)
    benchmark: dict = field(default_factory=dict)
    risk_settings: dict = field(default_factory=dict)
    execution_costs: dict = field(default_factory=dict)
    reproducibility_hash: Optional[str] = None

    @property
    def net_profit(self) -> float:
        return self.ending_balance - self.starting_balance

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "starting_balance": self.starting_balance,
            "ending_balance": self.ending_balance,
            "net_profit": self.net_profit,
            "bars_evaluated": self.bars_evaluated,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "timeframe": self.timeframe,
            "config": self.config,
            "risk_settings": self.risk_settings,
            "execution_costs": self.execution_costs,
            "rejections": self.rejections,
            "data_quality": self.data_quality,
            "benchmark": self.benchmark,
            "reproducibility_hash": self.reproducibility_hash,
            "total_trades": len(self.trades),
        }
