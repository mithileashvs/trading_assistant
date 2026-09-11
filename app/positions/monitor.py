"""
Position Monitor (section 22).

Continuously evaluates open positions against the current tick and
decides on stop-management actions: move to breakeven, trail the
stop, take a partial exit, or close entirely. All behavior is
configurable (section 22) and every decision is returned as an
explicit PositionAction rather than being silently applied — the
ExecutionEngine (Phase 9) is what actually carries an action out, so
this module stays pure and easily testable.

This is what the backtest engine (Phase 6) deliberately did NOT
implement (single fixed SL/TP bracket only) — the full breakeven /
trailing / partial-exit behavior belongs here, for live and paper
trading specifically.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.execution.engine import ManagedPosition
from app.mt5.interface import Tick


class ActionType(str, enum.Enum):
    NONE = "NONE"
    MOVE_TO_BREAKEVEN = "MOVE_TO_BREAKEVEN"
    TRAIL_STOP = "TRAIL_STOP"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    FULL_EXIT = "FULL_EXIT"


@dataclass
class PositionSnapshot:
    ticket: int
    direction: str
    entry_price: float
    current_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    volume: float
    unrealized_pnl_ticks: float
    r_multiple: Optional[float]
    spread: float


@dataclass
class PositionAction:
    type: ActionType
    ticket: int
    new_stop_loss: Optional[float] = None
    partial_volume: Optional[float] = None
    reason: str = ""


class PositionMonitorConfig(BaseSettings):
    breakeven_trigger_r: float = Field(1.0, gt=0, description="Move stop to breakeven once price reaches this many R in profit.")
    breakeven_buffer_ticks: float = Field(5.0, ge=0, description="Ticks beyond entry to lock in once breakeven triggers (covers costs).")
    enable_trailing: bool = Field(True)
    trailing_trigger_r: float = Field(1.5, gt=0, description="Start trailing once price reaches this many R in profit.")
    trailing_atr_multiplier: float = Field(1.5, gt=0)
    enable_partial_exit: bool = Field(True)
    partial_exit_trigger_r: float = Field(1.0, gt=0)
    partial_exit_fraction: float = Field(0.5, gt=0, lt=1.0, description="Fraction of volume to close at the partial-exit trigger.")

    model_config = SettingsConfigDict(env_prefix="POSITION_MONITOR_", extra="ignore")


class PositionMonitor:
    def __init__(self, config: PositionMonitorConfig | None = None):
        self.config = config or PositionMonitorConfig()
        self._partial_taken: set[int] = set()
        self._breakeven_moved: set[int] = set()

    def snapshot(self, position: ManagedPosition, tick: Tick, tick_size: float) -> PositionSnapshot:
        current_price = tick.bid if position.direction == "BUY" else tick.ask
        ticks = (
            (current_price - position.price_open) / tick_size if position.direction == "BUY"
            else (position.price_open - current_price) / tick_size
        )
        risk_ticks = None
        if position.stop_loss is not None:
            risk_ticks = abs(position.price_open - position.stop_loss) / tick_size
        r_multiple = (ticks / risk_ticks) if risk_ticks else None
        return PositionSnapshot(
            ticket=position.ticket, direction=position.direction, entry_price=position.price_open,
            current_price=current_price, stop_loss=position.stop_loss, take_profit=position.take_profit,
            volume=position.volume, unrealized_pnl_ticks=ticks, r_multiple=r_multiple, spread=tick.spread,
        )

    def evaluate(
        self, position: ManagedPosition, tick: Tick, tick_size: float, current_atr: Optional[float] = None
    ) -> list[PositionAction]:
        """Returns zero or more actions to take for this position, in
        the order they should be applied. Never mutates the position or
        calls the execution engine itself."""
        cfg = self.config
        snap = self.snapshot(position, tick, tick_size)
        actions: list[PositionAction] = []

        if snap.r_multiple is None:
            return actions  # no stop set -> nothing to manage against

        # --- breakeven -----------------------------------------------------------
        if position.ticket not in self._breakeven_moved and snap.r_multiple >= cfg.breakeven_trigger_r:
            buffer = cfg.breakeven_buffer_ticks * tick_size
            new_stop = (
                position.price_open + buffer if position.direction == "BUY"
                else position.price_open - buffer
            )
            # Only move if it's actually an improvement over the current stop.
            improves = (
                (position.direction == "BUY" and (position.stop_loss is None or new_stop > position.stop_loss))
                or (position.direction == "SELL" and (position.stop_loss is None or new_stop < position.stop_loss))
            )
            if improves:
                actions.append(PositionAction(
                    type=ActionType.MOVE_TO_BREAKEVEN, ticket=position.ticket, new_stop_loss=new_stop,
                    reason=f"Reached {snap.r_multiple:.2f}R, moving stop to breakeven + buffer.",
                ))

        # --- partial exit ---------------------------------------------------------
        if (
            cfg.enable_partial_exit
            and position.ticket not in self._partial_taken
            and snap.r_multiple >= cfg.partial_exit_trigger_r
        ):
            volume = round(position.volume * cfg.partial_exit_fraction, 8)
            if volume > 0:
                actions.append(PositionAction(
                    type=ActionType.PARTIAL_EXIT, ticket=position.ticket, partial_volume=volume,
                    reason=f"Reached {snap.r_multiple:.2f}R, taking partial profit ({cfg.partial_exit_fraction:.0%}).",
                ))

        # --- trailing stop -------------------------------------------------------------
        if cfg.enable_trailing and current_atr and snap.r_multiple >= cfg.trailing_trigger_r:
            trail_distance = current_atr * cfg.trailing_atr_multiplier
            candidate_stop = (
                snap.current_price - trail_distance if position.direction == "BUY"
                else snap.current_price + trail_distance
            )
            improves = (
                (position.direction == "BUY" and (position.stop_loss is None or candidate_stop > position.stop_loss))
                or (position.direction == "SELL" and (position.stop_loss is None or candidate_stop < position.stop_loss))
            )
            if improves:
                actions.append(PositionAction(
                    type=ActionType.TRAIL_STOP, ticket=position.ticket, new_stop_loss=candidate_stop,
                    reason=f"Trailing stop at {cfg.trailing_atr_multiplier}x ATR behind price.",
                ))

        return actions

    def mark_breakeven_applied(self, ticket: int) -> None:
        self._breakeven_moved.add(ticket)

    def mark_partial_taken(self, ticket: int) -> None:
        self._partial_taken.add(ticket)

    def forget(self, ticket: int) -> None:
        """Call when a position closes entirely, so state doesn't leak
        across tickets (especially important since paper-position
        tickets are simple counters)."""
        self._partial_taken.discard(ticket)
        self._breakeven_moved.discard(ticket)
