"""
Execution cost model (section 24: realistic spread, commission,
slippage, swap/overnight costs).
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutionCosts(BaseSettings):
    spread_points: float = Field(20.0, ge=0, description="Fixed spread in points, applied at entry and exit.")
    slippage_points: float = Field(5.0, ge=0, description="Adverse slippage in points, applied at entry and exit.")
    commission_per_lot: float = Field(7.0, ge=0, description="Round-turn commission per standard lot, in account currency.")
    swap_long_per_lot_per_day: float = Field(-6.5, description="Overnight swap for long positions, per lot per day held.")
    swap_short_per_lot_per_day: float = Field(-3.0, description="Overnight swap for short positions, per lot per day held.")

    model_config = SettingsConfigDict(env_prefix="BACKTEST_COST_", extra="ignore")


@dataclass
class FillResult:
    price: float
    spread_cost: float  # in price terms, one-way
    slippage_cost: float  # in price terms, one-way


def apply_entry_costs(raw_price: float, direction: str, costs: ExecutionCosts, tick_size: float) -> FillResult:
    """BUY fills worse (higher) at the ask; SELL fills worse (lower) at
    the bid. Slippage always works against the trader."""
    half_spread = (costs.spread_points * tick_size) / 2.0
    slip = costs.slippage_points * tick_size
    if direction == "BUY":
        price = raw_price + half_spread + slip
    else:
        price = raw_price - half_spread - slip
    return FillResult(price=price, spread_cost=half_spread, slippage_cost=slip)


def apply_exit_costs(raw_price: float, direction: str, costs: ExecutionCosts, tick_size: float) -> FillResult:
    """Closing a BUY means selling (fills at the worse/lower bid side);
    closing a SELL means buying (fills at the worse/higher ask side)."""
    half_spread = (costs.spread_points * tick_size) / 2.0
    slip = costs.slippage_points * tick_size
    if direction == "BUY":
        price = raw_price - half_spread - slip
    else:
        price = raw_price + half_spread + slip
    return FillResult(price=price, spread_cost=half_spread, slippage_cost=slip)


def commission_cost(lots: float, costs: ExecutionCosts) -> float:
    return lots * costs.commission_per_lot


def swap_cost(lots: float, direction: str, days_held: int, costs: ExecutionCosts) -> float:
    if days_held <= 0:
        return 0.0
    per_day = costs.swap_long_per_lot_per_day if direction == "BUY" else costs.swap_short_per_lot_per_day
    return lots * per_day * days_held
