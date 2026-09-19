"""
Position Reconciliation (Phase 8, sections 7-8).

Compares LOCAL position-monitor state against the current broker/
execution-engine state (app.execution.engine.ExecutionEngine.
get_open_positions -- broker state in LIVE mode, section 35's "MT5
broker state is authoritative"). Reports discrepancies; never silently
"fixes" one, and never fabricates data to paper over a gap. This is
deliberately the same posture as ExecutionEngine.reconcile() (Phase 7)
extended to cover position-management state (SL/TP/volume), not just
open-vs-tracked tickets.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from app.execution.engine import ManagedPosition
from app.positions.state_store import PositionMonitorRecord, PositionMonitorStateStore


class DiscrepancyType(str, enum.Enum):
    MISSING_AT_BROKER = "MISSING_AT_BROKER"  # tracked locally, no longer open at broker/execution engine
    UNTRACKED_LOCALLY = "UNTRACKED_LOCALLY"  # open at broker, no local monitor record
    STOP_LOSS_CHANGED_EXTERNALLY = "STOP_LOSS_CHANGED_EXTERNALLY"
    TAKE_PROFIT_CHANGED_EXTERNALLY = "TAKE_PROFIT_CHANGED_EXTERNALLY"
    VOLUME_CHANGED_EXTERNALLY = "VOLUME_CHANGED_EXTERNALLY"
    UNRESOLVED_PENDING_ACTION = "UNRESOLVED_PENDING_ACTION"


@dataclass
class Discrepancy:
    type: DiscrepancyType
    ticket: int
    detail: str


def reconcile_positions(
    records: list[PositionMonitorRecord],
    broker_positions: list[ManagedPosition],
    stop_tolerance: float = 1e-6,
    volume_tolerance: float = 1e-6,
) -> list[Discrepancy]:
    """Pure comparison -- never mutates `records` or the store. Callers
    decide what (if anything) to do with the result; this function only
    reports (section 7: "do not silently overwrite evidence", "do not
    automatically 'fix' ambiguous discrepancies")."""
    by_ticket = {p.ticket: p for p in broker_positions}
    tracked = {r.ticket: r for r in records if not r.closed}
    discrepancies: list[Discrepancy] = []

    for ticket, record in tracked.items():
        if record.pending_action is not None:
            discrepancies.append(Discrepancy(
                DiscrepancyType.UNRESOLVED_PENDING_ACTION, ticket,
                f"Ticket {ticket} has an unresolved {record.pending_action.action_type} action "
                f"pending since it was submitted; reconcile against the broker before assuming an "
                f"outcome.",
            ))
        broker_pos = by_ticket.get(ticket)
        if broker_pos is None:
            discrepancies.append(Discrepancy(
                DiscrepancyType.MISSING_AT_BROKER, ticket,
                f"Ticket {ticket} is tracked locally but no longer open at the broker/execution "
                f"engine -- it may have closed (stop, take-profit, manual close) or disappeared "
                f"unexpectedly. Do not fabricate an exit price or reopen it automatically.",
            ))
            continue
        if (
            record.last_confirmed_stop_loss is not None
            and broker_pos.stop_loss is not None
            and abs(record.last_confirmed_stop_loss - broker_pos.stop_loss) > stop_tolerance
        ):
            discrepancies.append(Discrepancy(
                DiscrepancyType.STOP_LOSS_CHANGED_EXTERNALLY, ticket,
                f"Ticket {ticket}: locally-confirmed SL {record.last_confirmed_stop_loss} differs "
                f"from broker-reported SL {broker_pos.stop_loss}; it was likely changed outside the "
                f"application (e.g. manually on the broker platform).",
            ))
        if (
            record.initial_volume is not None
            and record.partial_exit_volume is not None
        ):
            expected_volume = round(record.initial_volume - record.partial_exit_volume, 8)
            if abs(expected_volume - broker_pos.volume) > volume_tolerance:
                discrepancies.append(Discrepancy(
                    DiscrepancyType.VOLUME_CHANGED_EXTERNALLY, ticket,
                    f"Ticket {ticket}: expected remaining volume {expected_volume} (initial "
                    f"{record.initial_volume} minus confirmed partial exits {record.partial_exit_volume}) "
                    f"does not match broker-reported volume {broker_pos.volume}.",
                ))

    for ticket in by_ticket:
        if ticket not in tracked:
            discrepancies.append(Discrepancy(
                DiscrepancyType.UNTRACKED_LOCALLY, ticket,
                f"Ticket {ticket} is open at the broker/execution engine but has no local "
                f"position-monitor record (opened outside this process, or state was lost).",
            ))

    return discrepancies


def recover_state(
    store: Optional[PositionMonitorStateStore],
    broker_positions: list[ManagedPosition],
) -> tuple[list[PositionMonitorRecord], list[Discrepancy]]:
    """Restart recovery (Phase 8 section 8). Loads persisted state and
    compares it to current broker/execution-engine positions.

    FAILS CLOSED: if the store cannot be read at all, returns an empty
    record list and a single discrepancy noting the failure --
    upstream (PositionMonitor/TradingLoop) must treat an empty,
    unreadable state as "do not automatically manage anything yet",
    never as "there is nothing to manage" (section 8: "If state is
    unreadable: FAIL CLOSED for automated position-management
    actions.").
    """
    if store is None:
        return [], []
    try:
        records = store.all()
    except Exception as exc:  # noqa: BLE001 - deliberate: any read failure fails closed
        return [], [Discrepancy(
            DiscrepancyType.UNRESOLVED_PENDING_ACTION, ticket=-1,
            detail=f"Position-monitor state could not be read on restart ({exc}); failing closed -- "
                   f"no automated position-management action will be taken until this is resolved.",
        )]
    discrepancies = reconcile_positions(records, broker_positions)
    return records, discrepancies
