"""
Paper Execution Adapter (Phase 12).

Provides a structurally isolated, simulation-only execution engine for Paper Trading.
Contains ZERO code paths to real MT5 broker order placement.

STRICT SAFETY ENFORCEMENT:
- Rejects TradingMode.LIVE at initialization and at EVERY execution boundary.
- Never imports or invokes MetaTrader5.order_send() or broker execution functions.
- Manages simulated PaperAccount and PaperPositions locally with realistic cost modeling.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from app.journal.journal import TradeJournal

from app.backtesting.costs import ExecutionCosts, apply_entry_costs, apply_exit_costs, commission_cost
from app.config.settings import TradingMode
from app.execution.execution_safety_gate import ExecutionSafetyGate, ExecutionSafetyGateInput
from app.execution.state_store import (
    BLOCKING_STATUSES,
    STATUS_FILLED,
    STATUS_RESOLVED_FILLED,
    STATUS_RESOLVED_NOT_FOUND,
    STATUS_UNKNOWN,
    ExecutionRecord,
    ExecutionStateStore,
    InMemoryExecutionStateStore,
)
from app.mt5.interface import (
    AccountInfo,
    OrderResult,
    SymbolSpec,
    Tick,
)
from app.paper.account import PaperAccount
from app.paper.positions import PaperPosition


class _NullKillSwitch:
    def is_active(self) -> bool:
        return False


_GLOBAL_PAPER_TICKET_COUNTER = itertools.count(start=900_000_001)


class PaperExecutionAdapter:
    """Structurally isolated execution adapter for simulated Paper Trading."""

    def __init__(
        self,
        symbol_spec: SymbolSpec,
        account: Optional[PaperAccount] = None,
        costs: Optional[ExecutionCosts] = None,
        trading_mode: TradingMode = TradingMode.PAPER,
        kill_switch=None,
        state_store: Optional[ExecutionStateStore] = None,
        journal: Optional[TradeJournal] = None,
        client=None,
        initial_balance: float = 10_000.0,
        execution_safety_gate: Optional[ExecutionSafetyGate] = None,
    ):
        # Strict Safety Gate: Paper Trading is simulation-only
        if trading_mode == TradingMode.LIVE:
            raise RuntimeError(
                "Safety violation: TradingMode.LIVE cannot be used with PaperExecutionAdapter"
            )

        self.trading_mode = trading_mode
        self.symbol_spec = symbol_spec
        if account is not None:
            self.account = account
        else:
            self.account = PaperAccount(
                balance=initial_balance,
                current_balance=initial_balance,
                equity=initial_balance,
                free_margin=initial_balance,
            )
        self.costs = costs or ExecutionCosts()
        self.kill_switch = kill_switch or _NullKillSwitch()
        self.state_store = state_store or InMemoryExecutionStateStore()
        self.journal = journal
        self.client = client  # Used strictly for read-only market data (ticks/quotes) if supplied
        self._ticket_counter = itertools.count(start=900_000_001)
        self._exec_safety_gate = execution_safety_gate or ExecutionSafetyGate()
        self.latest_tick: Optional[Tick] = None

    @property
    def positions(self) -> list[PaperPosition]:
        """All currently open paper positions."""
        return list(self.account.open_positions.values())

    @property
    def closed_positions(self) -> list[PaperPosition]:
        """All closed paper positions."""
        return list(self.account.closed_positions)

    def _assert_paper_mode(self) -> None:
        """Enforces that trading_mode is PAPER at every security-sensitive execution boundary."""
        if self.trading_mode != TradingMode.PAPER:
            raise RuntimeError(
                "Safety violation: Paper Trading is simulation-only and cannot run in LIVE mode."
            )

    def set_current_tick(self, tick: Tick) -> None:
        """Updates the current simulated market tick and revalues open positions."""
        self.latest_tick = tick
        self.account.update_floating(self.symbol_spec, tick)

    def get_account_info(self) -> AccountInfo:
        """Returns the current simulated account state adapted to AccountInfo."""
        return self.account.as_account_info()

    def get_open_positions(self) -> list[Any]:
        """Returns all currently open simulated paper positions as ManagedPositions."""
        return [p.to_managed_position() for p in self.account.open_positions.values()]

    def _audit(
        self,
        event_type: str,
        category: str,
        result_status: Optional[str] = None,
        client_order_id: Optional[str] = None,
        ticket: Optional[int] = None,
        side: Optional[str] = None,
        volume: Optional[float] = None,
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        strategy: Optional[str] = None,
        regime: Optional[str] = None,
        reason: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[dict] = None,
        critical: bool = False,
    ) -> None:
        if self.journal is None:
            return
        from app.journal.journal import AuditEvent
        now = datetime.now(timezone.utc)
        evt = AuditEvent(
            event_id=f"paper_{event_type.lower()}_{uuid.uuid4().hex}",
            timestamp=now,
            event_type=event_type,
            category=category,
            symbol=self.symbol_spec.name if self.symbol_spec else None,
            ticket=ticket,
            client_order_id=client_order_id,
            side=side,
            volume=volume,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy=strategy,
            regime=regime,
            result_status=result_status,
            reason=reason,
            error=error,
            metadata=metadata,
        )
        self.journal.log_audit_event(evt, critical=critical)

    def submit_market_order(
        self,
        direction: str,
        volume: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        client_order_id: str,
        strategy: str = "",
        regime: str = "",
        monetary_risk: Optional[float] = None,
        comment: str = "",
    ) -> tuple[OrderResult, Optional[Any]]:
        """Simulates market order submission with strict safety checks, deduplication, and realistic fills."""
        # 1. Check safety invariant at execution boundary
        self._assert_paper_mode()

        self._audit(
            event_type="ORDER_INTENT",
            category="order",
            client_order_id=client_order_id,
            side=direction,
            volume=volume,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy=strategy,
            regime=regime,
            reason="Simulated paper order intent received",
        )

        # 2. Duplicate order check via state store
        existing = self.state_store.get(client_order_id)
        if existing is not None and existing.status in BLOCKING_STATUSES:
            reason = "DUPLICATE_ORDER_REJECTED"
            self._audit(
                event_type="ORDER_REJECTED",
                category="order",
                result_status="REJECTED",
                client_order_id=client_order_id,
                ticket=existing.ticket,
                side=direction,
                volume=volume,
                reason=reason,
            )
            return (
                OrderResult(success=False, order_id=existing.ticket, deal_id=None, price=None, volume=None, retcode=-1, comment=reason),
                None,
            )

        # 3. Execution Safety Gate validation
        account_info = self.get_account_info()
        gate_input = ExecutionSafetyGateInput(
            is_duplicate=False,
            kill_switch_active=self.kill_switch.is_active(),
            account_trade_allowed=account_info.trade_allowed,
            symbol_trade_allowed=self.symbol_spec.trade_allowed,
            direction=direction,
            volume=volume,
            volume_min=self.symbol_spec.volume_min,
            volume_max=self.symbol_spec.volume_max,
            volume_step=self.symbol_spec.volume_step,
            stop_loss=stop_loss,
            market_data_fresh=True,
        )
        gate_res = self._exec_safety_gate.evaluate(gate_input)
        if not gate_res.approved:
            reason = f"EXECUTION_SAFETY_GATE_REJECTED: {'; '.join(gate_res.reasons)}"
            self._audit(
                event_type="EXECUTION_SAFETY_GATE_DECISION",
                category="risk",
                result_status="REJECTED",
                client_order_id=client_order_id,
                side=direction,
                volume=volume,
                reason=reason,
            )
            return (
                OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=-1, comment=reason, raw=gate_res.to_dict()),
                None,
            )

        # 4. Fetch valid tick for fill simulation
        tick = self.latest_tick
        if tick is None and self.client is not None:
            try:
                tick = self.client.get_tick(self.symbol_spec.name)
            except Exception as exc:
                err_msg = f"NO_QUOTE: could not fetch tick for simulated fill: {exc}"
                self._audit(event_type="ORDER_REJECTED", category="order", result_status="REJECTED", client_order_id=client_order_id, reason=err_msg)
                return OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=-1, comment=err_msg), None

        if tick is None or tick.ask <= tick.bid or tick.bid <= 0:
            err_msg = "INVALID_QUOTE: tick is missing, crossed, or non-positive"
            self._audit(event_type="ORDER_REJECTED", category="order", result_status="REJECTED", client_order_id=client_order_id, reason=err_msg)
            return OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=-1, comment=err_msg), None

        # 5. Margin Requirement Check
        raw_price = tick.ask if direction == "BUY" else tick.bid
        required_margin = self.account.calculate_margin(volume, raw_price, self.symbol_spec)
        if self.account.free_margin < required_margin:
            err_msg = f"INSUFFICIENT_MARGIN: required {required_margin:.2f} > free {self.account.free_margin:.2f}"
            self._audit(event_type="ORDER_REJECTED", category="risk", result_status="REJECTED", client_order_id=client_order_id, reason=err_msg)
            return OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=-1, comment=err_msg), None

        # 6. Apply Execution Price Model (Phase 10 costs)
        slip = round(self.costs.slippage_points * self.symbol_spec.tick_size, self.symbol_spec.digits)
        if direction == "BUY":
            fill_price = round(tick.ask + slip, self.symbol_spec.digits)
        else:
            fill_price = round(tick.bid - slip, self.symbol_spec.digits)

        spread_cost = round((tick.ask - tick.bid) / 2.0, self.symbol_spec.digits)
        comm = round(commission_cost(volume, self.costs) / 2.0, 2)
        ticket = next(_GLOBAL_PAPER_TICKET_COUNTER)

        # 7. Record and create simulated PaperPosition
        now = datetime.now(timezone.utc)
        self.state_store.put(ExecutionRecord(
            client_order_id=client_order_id,
            status=STATUS_FILLED,
            ticket=ticket,
            symbol=self.symbol_spec.name,
            direction=direction,
            volume=volume,
        ))

        pos = PaperPosition(
            ticket=ticket,
            client_order_id=client_order_id,
            symbol=self.symbol_spec.name,
            direction=direction,
            volume=volume,
            price_open=fill_price,
            current_price=fill_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy=strategy,
            regime=regime,
            open_time=now,
            last_update_time=now,
            commission=comm,
            spread_cost=spread_cost,
            slippage_cost=slip,
            is_paper=True,
        )

        self.account.apply_entry(pos, self.symbol_spec, commission=comm, spread_cost=spread_cost, slippage_cost=slip, timestamp=now)

        order_res = OrderResult(
            success=True,
            order_id=ticket,
            deal_id=ticket,
            price=fill_price,
            volume=volume,
            retcode=10009,
            comment="PAPER_FILLED",
        )

        self._audit(
            event_type="ORDER_RESULT",
            category="order",
            result_status="CONFIRMED",
            ticket=ticket,
            client_order_id=client_order_id,
            side=direction,
            volume=volume,
            price=fill_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy=strategy,
            regime=regime,
            reason="PAPER_FILLED",
            metadata={"is_paper": True, "fill_price": fill_price, "commission": comm},
            critical=True,
        )

        return order_res, pos.to_managed_position()

    def modify_position(self, ticket: int, stop_loss: Optional[float] = None, take_profit: Optional[float] = None) -> OrderResult:
        """Modifies SL/TP on an existing open simulated position."""
        self._assert_paper_mode()

        pos = self.account.open_positions.get(ticket)
        if pos is None:
            return OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=-1, comment="POSITION_NOT_FOUND")

        old_sl = pos.stop_loss
        old_tp = pos.take_profit

        if stop_loss is not None:
            pos.stop_loss = stop_loss
        if take_profit is not None:
            pos.take_profit = take_profit
        pos.last_update_time = datetime.now(timezone.utc)

        self._audit(
            event_type="POSITION_MODIFIED",
            category="position",
            result_status="CONFIRMED",
            ticket=ticket,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            reason="PAPER_MODIFIED",
            metadata={"old_sl": old_sl, "new_sl": pos.stop_loss, "old_tp": old_tp, "new_tp": pos.take_profit},
        )

        return OrderResult(
            success=True,
            order_id=ticket,
            deal_id=None,
            price=pos.price_open,
            volume=pos.volume,
            retcode=10009,
            comment="PAPER_MODIFIED",
        )

    def close_position_partial(self, ticket: int, volume: float) -> OrderResult:
        """Partially closes an open simulated position, realizing partial P/L and reducing volume."""
        self._assert_paper_mode()

        pos = self.account.open_positions.get(ticket)
        if pos is None:
            return OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=-1, comment="POSITION_NOT_FOUND")

        if volume <= 0 or volume >= pos.volume:
            return OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=-1, comment="INVALID_PARTIAL_VOLUME")

        tick = self.latest_tick
        if tick is None and self.client is not None:
            try:
                tick = self.client.get_tick(self.symbol_spec.name)
            except Exception:
                pass

        if tick is None:
            return OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=-1, comment="NO_QUOTE")

        slip = round(self.costs.slippage_points * self.symbol_spec.tick_size, self.symbol_spec.digits)
        if pos.direction == "BUY":
            fill_price = round(tick.bid - slip, self.symbol_spec.digits)
        else:
            fill_price = round(tick.ask + slip, self.symbol_spec.digits)
        spread_cost = round((tick.ask - tick.bid) / 2.0, self.symbol_spec.digits)
        comm = commission_cost(volume, self.costs)

        # Realize partial P/L
        diff = (fill_price - pos.price_open) if pos.direction == "BUY" else (pos.price_open - fill_price)
        if self.symbol_spec.tick_size > 0:
            ticks = diff / self.symbol_spec.tick_size
            partial_pnl = round(ticks * self.symbol_spec.tick_value * volume - comm, 2)
        else:
            partial_pnl = round(diff * volume * self.symbol_spec.contract_size - comm, 2)

        pos.volume = round(pos.volume - volume, 8)
        pos.realized_pnl = round(pos.realized_pnl + partial_pnl, 2)
        pos.last_update_time = datetime.now(timezone.utc)

        self.account.current_balance = round(self.account.current_balance + partial_pnl, 2)
        self.account.realized_pnl = round(self.account.realized_pnl + partial_pnl, 2)
        self.account.commission = round(self.account.commission + comm, 2)
        self.account.update_floating(self.symbol_spec, tick)

        self._audit(
            event_type="POSITION_PARTIAL_CLOSED",
            category="position",
            result_status="CONFIRMED",
            ticket=ticket,
            volume=volume,
            price=fill_price,
            reason="PAPER_PARTIAL_CLOSED",
            metadata={"closed_volume": volume, "remaining_volume": pos.volume, "partial_pnl": partial_pnl},
        )

        return OrderResult(
            success=True,
            order_id=ticket,
            deal_id=ticket,
            price=fill_price,
            volume=volume,
            retcode=10009,
            comment="PAPER_PARTIAL_CLOSED",
        )

    def close_position(self, ticket: int, reason: str = "", exit_price: Optional[float] = None) -> OrderResult:
        """Fully closes a simulated position, realizes net P/L, and updates the account."""
        self._assert_paper_mode()

        pos = self.account.open_positions.get(ticket)
        if pos is None:
            return OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=-1, comment="POSITION_NOT_FOUND")

        if exit_price is not None:
            fill_price = exit_price
            spread_cost = 0.0
            slip = 0.0
        else:
            tick = self.latest_tick
            if tick is None and self.client is not None:
                try:
                    tick = self.client.get_tick(self.symbol_spec.name)
                except Exception:
                    pass

            if tick is None:
                return OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=-1, comment="NO_QUOTE")

            slip = round(self.costs.slippage_points * self.symbol_spec.tick_size, self.symbol_spec.digits)
            if pos.direction == "BUY":
                fill_price = round(tick.bid - slip, self.symbol_spec.digits)
            else:
                fill_price = round(tick.ask + slip, self.symbol_spec.digits)
            spread_cost = round((tick.ask - tick.bid) / 2.0, self.symbol_spec.digits)

        comm = round(commission_cost(pos.volume, self.costs) / 2.0, 2)

        closed_pos = self.account.apply_close(
            ticket=ticket,
            exit_price=fill_price,
            symbol_spec=self.symbol_spec,
            close_reason=reason or "PAPER_CLOSED",
            commission=comm,
            spread_cost=spread_cost,
            slippage_cost=slip,
        )

        self._audit(
            event_type="POSITION_CLOSED",
            category="position",
            result_status="CONFIRMED",
            ticket=ticket,
            price=fill_price,
            volume=closed_pos.volume if closed_pos else pos.volume,
            reason=reason or "PAPER_CLOSED",
            metadata={"realized_pnl": closed_pos.realized_pnl if closed_pos else 0.0},
            critical=True,
        )

        return OrderResult(
            success=True,
            order_id=ticket,
            deal_id=ticket,
            price=fill_price,
            volume=pos.volume,
            retcode=10009,
            comment=f"PAPER_CLOSED:{reason}",
        )

    def apply_swap(self, ticket: int, swap_amount: float) -> None:
        """Applies overnight rollover swap to a position and account balance."""
        pos = self.account.open_positions.get(ticket)
        if pos is not None:
            pos.swap = round(pos.swap + swap_amount, 2)
            self.account.swap = round(self.account.swap + swap_amount, 2)
            self.account.current_balance = round(self.account.current_balance + swap_amount, 2)
            self.account.equity = round(self.account.current_balance + self.account.unrealized_pnl, 2)

    def reconcile(self) -> list[str]:
        """Paper trading reconciliation: internal state is authoritative, returns empty notes."""
        return []

    def unresolved_executions(self) -> list[ExecutionRecord]:
        """Returns any executions in state_store with STATUS_UNKNOWN."""
        return self.state_store.unresolved()

    def reconcile_unknown(self) -> list[Any]:
        """Reconciles UNKNOWN records against local open paper positions."""
        from app.execution.engine import UnknownReconciliationResult
        results = []
        for record in self.state_store.all():
            if record.status == STATUS_UNKNOWN:
                match = next((p for p in self.account.open_positions.values() if p.client_order_id == record.client_order_id), None)
                if match is not None:
                    self.state_store.put(ExecutionRecord(
                        client_order_id=record.client_order_id,
                        status=STATUS_RESOLVED_FILLED,
                        ticket=match.ticket,
                        symbol=record.symbol,
                        direction=record.direction,
                        volume=record.volume,
                        created_at=record.created_at,
                        note="auto-resolved against simulated paper position",
                    ))
                    results.append(UnknownReconciliationResult(
                        client_order_id=record.client_order_id,
                        outcome="RESOLVED_FILLED",
                        ticket=match.ticket,
                        detail="Simulated paper position found.",
                    ))
                else:
                    results.append(UnknownReconciliationResult(
                        client_order_id=record.client_order_id,
                        outcome="STILL_UNKNOWN",
                        ticket=None,
                        detail="No simulated position found. Remains UNKNOWN.",
                    ))
        return results

    def resolve_unknown_execution(
        self,
        client_order_id: str,
        ticket: Optional[int] = None,
        note: str = "",
        resolved_status: Optional[str] = None,
        confirmed_non_execution: bool = False,
        **kwargs: Any,
    ) -> None:
        """Operator-driven resolution of UNKNOWN execution."""
        record = self.state_store.get(client_order_id)
        if record is None or record.status != STATUS_UNKNOWN:
            raise ValueError(f"client_order_id {client_order_id!r} has no unresolved UNKNOWN execution.")

        status_str = (resolved_status or "").upper()
        if status_str in ("CONFIRMED", "FILLED", "RESOLVED_FILLED") or (ticket is not None and ticket > 0):
            new_status = STATUS_RESOLVED_FILLED
        elif status_str in ("REJECTED", "NOT_FOUND", "RESOLVED_NOT_FOUND") or confirmed_non_execution:
            new_status = STATUS_RESOLVED_NOT_FOUND
        else:
            new_status = STATUS_UNKNOWN

        self.state_store.put(ExecutionRecord(
            client_order_id=client_order_id,
            status=new_status,
            ticket=ticket,
            symbol=record.symbol,
            direction=record.direction,
            volume=record.volume,
            created_at=record.created_at,
            note=note,
        ))


# Clean alias
PaperBroker = PaperExecutionAdapter
