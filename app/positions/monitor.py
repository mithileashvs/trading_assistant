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

PHASE 8 -- POSITION MONITORING & MANAGEMENT: this module remains pure
(evaluate() still only returns decisions; it never touches the
execution engine directly), but its idempotency state now lives behind
a PositionMonitorStateStore (app.positions.state_store) rather than
purely in-memory sets, so breakeven-only-once / partial-exit-only-once
and "never re-submit an unchanged trailing stop" all survive a process
restart. The in-memory sets (_breakeven_moved, _partial_taken) are kept
as a fast local mirror of the store -- reads never touch the store,
only mark_*/forget writes do -- so this stays cheap to call every tick
while still being restart-safe. The Position Monitor still never
decides FINAL success/failure of an action -- see
PositionMonitor.record_pending / resolve_pending, and
app.positions.reconciliation, for how an UNKNOWN outcome is tracked
without ever being silently upgraded to SUCCESS or REJECTED.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.execution.engine import ManagedPosition
from app.mt5.interface import Tick
from app.positions.state_store import (
    InMemoryPositionMonitorStateStore,
    PendingAction,
    PositionMonitorRecord,
    PositionMonitorStateStore,
)


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
    def __init__(
        self,
        config: PositionMonitorConfig | None = None,
        state_store: Optional[PositionMonitorStateStore] = None,
    ):
        self.config = config or PositionMonitorConfig()
        # Phase 8: source of truth is self._store (restart-safe); these
        # sets are a cheap in-memory mirror populated from it at
        # construction, kept in sync on every mark_*/forget call, so
        # per-tick evaluate() calls never need to hit the store.
        self._store = state_store or InMemoryPositionMonitorStateStore()
        self._partial_taken: set[int] = set()
        self._breakeven_moved: set[int] = set()
        for record in self._store.all():
            if record.closed:
                continue
            if record.breakeven_applied:
                self._breakeven_moved.add(record.ticket)
            if record.partial_exit_taken:
                self._partial_taken.add(record.ticket)

    def _get_or_create_record(self, position: ManagedPosition) -> PositionMonitorRecord:
        record = self._store.get(position.ticket)
        if record is None:
            record = PositionMonitorRecord(
                ticket=position.ticket, symbol=position.symbol, direction=position.direction,
                initial_volume=position.volume, last_confirmed_stop_loss=position.stop_loss,
            )
            self._store.put(record)
        return record

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

        record = self._get_or_create_record(position)

        # Phase 8 section 9: never generate a new action for a ticket
        # that already has one in flight and unresolved -- that's
        # exactly what would turn a single uncertain modify/close into
        # a blind, potentially duplicate retry. Reconciliation is the
        # only thing that clears this.
        if record.pending_action is not None:
            return actions

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
            # Fraction of the ORIGINAL volume (section 4), not whatever
            # the position's current volume happens to be -- these
            # normally coincide (partial exit only fires once), but the
            # persisted record's initial_volume is the authoritative
            # basis so a restart or an unrelated external volume change
            # can never silently change the fraction being closed.
            basis_volume = record.initial_volume if record.initial_volume is not None else position.volume
            volume = round(basis_volume * cfg.partial_exit_fraction, 8)
            # Never exceed the position's ACTUAL current volume, and
            # never accidentally take the whole thing unless that's
            # genuinely what the configured fraction computes to.
            volume = min(volume, position.volume)
            if volume > 0 and volume < position.volume:
                actions.append(PositionAction(
                    type=ActionType.PARTIAL_EXIT, ticket=position.ticket, partial_volume=volume,
                    reason=f"Reached {snap.r_multiple:.2f}R, taking partial profit ({cfg.partial_exit_fraction:.0%}).",
                ))

        # --- trailing stop -------------------------------------------------------------
        if cfg.enable_trailing and current_atr is not None and current_atr > 0 and snap.r_multiple >= cfg.trailing_trigger_r:
            trail_distance = current_atr * cfg.trailing_atr_multiplier
            candidate_stop = (
                snap.current_price - trail_distance if position.direction == "BUY"
                else snap.current_price + trail_distance
            )
            improves = (
                (position.direction == "BUY" and (position.stop_loss is None or candidate_stop > position.stop_loss))
                or (position.direction == "SELL" and (position.stop_loss is None or candidate_stop < position.stop_loss))
            )
            # Idempotency (section 13): never resubmit a stop we've
            # already successfully pushed and that hasn't changed --
            # comparing against the persisted last-applied value (not
            # just `position.stop_loss`) protects against re-submitting
            # while a broker/paper update is still settling.
            unchanged = (
                record.last_trailing_stop_applied is not None
                and abs(candidate_stop - record.last_trailing_stop_applied) < (tick_size / 2)
            )
            if improves and not unchanged:
                actions.append(PositionAction(
                    type=ActionType.TRAIL_STOP, ticket=position.ticket, new_stop_loss=candidate_stop,
                    reason=f"Trailing stop at {cfg.trailing_atr_multiplier}x ATR behind price.",
                ))

        return actions

    # -- confirmed-outcome bookkeeping (only ever called after SUCCESS) -------

    def mark_breakeven_applied(self, ticket: int, confirmed_stop_loss: Optional[float] = None) -> None:
        """Call ONLY after a breakeven modify is CONFIRMED successful.
        Never call this for an UNKNOWN or REJECTED result (section 9:
        "Never convert UNKNOWN -> SUCCESS without evidence")."""
        self._breakeven_moved.add(ticket)
        record = self._store.get(ticket) or PositionMonitorRecord(ticket=ticket)
        record.breakeven_applied = True
        if confirmed_stop_loss is not None:
            record.last_confirmed_stop_loss = confirmed_stop_loss
        record.pending_action = None
        self._store.put(record)

    def mark_trailing_applied(self, ticket: int, confirmed_stop_loss: float) -> None:
        """Call ONLY after a trailing-stop modify is CONFIRMED successful."""
        record = self._store.get(ticket) or PositionMonitorRecord(ticket=ticket)
        record.last_trailing_stop_applied = confirmed_stop_loss
        record.last_confirmed_stop_loss = confirmed_stop_loss
        record.pending_action = None
        self._store.put(record)

    def mark_partial_taken(self, ticket: int, confirmed_volume: Optional[float] = None) -> None:
        """Call ONLY after a partial exit is CONFIRMED successful.
        `confirmed_volume` is the volume actually closed (used by
        reconciliation to compute expected remaining volume) -- pass it
        whenever the caller has it."""
        self._partial_taken.add(ticket)
        record = self._store.get(ticket) or PositionMonitorRecord(ticket=ticket)
        record.partial_exit_taken = True
        if confirmed_volume is not None:
            record.partial_exit_volume = round((record.partial_exit_volume or 0.0) + confirmed_volume, 8)
        record.pending_action = None
        self._store.put(record)

    # -- uncertain-outcome bookkeeping (section 9) -----------------------------

    def record_pending(
        self, ticket: int, action_type: "ActionType | str",
        requested_stop_loss: Optional[float] = None,
        requested_partial_volume: Optional[float] = None,
        note: str = "",
    ) -> None:
        """An action was submitted and the result was UNKNOWN/uncertain
        (connection loss, ambiguous broker response, etc.). Record it
        as pending so evaluate() stops generating new actions for this
        ticket until it is resolved -- never blindly retried, never
        assumed to have succeeded or failed."""
        record = self._store.get(ticket) or PositionMonitorRecord(ticket=ticket)
        action_value = action_type.value if isinstance(action_type, ActionType) else str(action_type)
        record.pending_action = PendingAction(
            action_type=action_value, requested_stop_loss=requested_stop_loss,
            requested_partial_volume=requested_partial_volume, note=note,
            submitted_at=datetime.now(timezone.utc).isoformat(),
        )
        self._store.put(record)

    def has_pending(self, ticket: int) -> bool:
        record = self._store.get(ticket)
        return record is not None and record.pending_action is not None

    def clear_pending(self, ticket: int) -> None:
        """Explicitly resolve a pending action without asserting
        success (e.g. reconciliation determined it was REJECTED /
        never happened) -- state otherwise stays exactly as it was."""
        record = self._store.get(ticket)
        if record is not None and record.pending_action is not None:
            record.pending_action = None
            self._store.put(record)

    def get_record(self, ticket: int) -> Optional[PositionMonitorRecord]:
        return self._store.get(ticket)

    def forget(self, ticket: int) -> None:
        """Call when a position closes entirely, so state doesn't leak
        across tickets (especially important since paper-position
        tickets are simple counters). The persisted record is kept,
        marked closed=True, for audit continuity (section 7) rather
        than deleted -- only the fast in-memory mirror is cleared."""
        self._partial_taken.discard(ticket)
        self._breakeven_moved.discard(ticket)
        record = self._store.get(ticket)
        if record is not None:
            record.closed = True
            record.pending_action = None
            self._store.put(record)
