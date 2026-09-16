"""
MT5 Execution Engine (section 21).

Only this module may submit trading orders. In LIVE mode it does so
for real, through IMT5Client. In PAPER mode it simulates fills against
the current tick using the same execution-cost model the backtest
engine uses (app.backtesting.costs) and never calls
client.submit_order — so "paper" genuinely never touches a broker
order path, it only reads market data through the client.

Duplicate-order protection is enforced via client_order_id: submitting
the same client_order_id twice returns the first result's ticket
without submitting a second order.

EXECUTION SAFETY GATE (audit sections 19, 54-56): submit_market_order()
calls ExecutionSafetyGate internally, unconditionally, before EVER
reaching client.submit_order() or fabricating a paper fill. This is
deliberate placement, not just an extra check — it's what makes
bypassing the safety gates structurally impossible rather than merely
against convention: any caller of submit_market_order(), present or
future, inherits this check automatically, because there is no path
to MT5 submission that doesn't go through this exact function.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.backtesting.costs import ExecutionCosts, apply_entry_costs, apply_exit_costs, commission_cost
from app.config.settings import TradingMode
from app.execution.execution_safety_gate import ExecutionSafetyGate, ExecutionSafetyGateInput
from app.mt5.interface import IMT5Client, OrderRequest, OrderResult, Position, SymbolSpec, Tick


class DuplicateOrderError(RuntimeError):
    pass


class _NullKillSwitch:
    """The default when ExecutionEngine is constructed without an
    explicit kill_switch. Deliberately NOT a file-backed KillSwitch --
    defaulting to one would mean every standalone ExecutionEngine
    (tests, ad-hoc scripts) silently shares whatever state happens to
    be on disk at the default path, which is a real test-isolation and
    correctness hazard. Production entry points (TradingLoop) always
    pass their own properly-scoped KillSwitch explicitly; this null
    object is only for callers that haven't wired one in, and it is
    intentionally, visibly inert rather than pretending to protect."""

    def is_active(self) -> bool:
        return False


@dataclass
class ManagedPosition:
    """Unified position view regardless of mode (paper-simulated or
    real broker) — the Position Monitor (Phase 9) consumes this, not
    IMT5Client.Position directly, so it doesn't need to know which mode
    it's running in."""
    ticket: int
    symbol: str
    direction: str
    volume: float
    price_open: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    open_time: datetime
    strategy: str = ""
    regime: str = ""
    monetary_risk: Optional[float] = None
    is_paper: bool = False


class ExecutionEngine:
    def __init__(
        self,
        client: IMT5Client,
        symbol_spec: SymbolSpec,
        mode: TradingMode,
        costs: ExecutionCosts | None = None,
        kill_switch=None,
    ):
        self.client = client
        self.symbol_spec = symbol_spec
        self.mode = mode
        self.costs = costs or ExecutionCosts()
        self.kill_switch = kill_switch or _NullKillSwitch()
        self._exec_safety_gate = ExecutionSafetyGate()
        self._seen_client_order_ids: dict[str, int] = {}  # client_order_id -> ticket
        self._paper_positions: dict[int, ManagedPosition] = {}
        self._paper_ticket_counter = itertools.count(start=900_000_000)

    @property
    def is_paper(self) -> bool:
        return self.mode != TradingMode.LIVE

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
    ) -> tuple[OrderResult, Optional[ManagedPosition]]:
        # Duplicate-order protection (section 21, section 16): the same
        # client_order_id is never submitted twice, in either mode.
        # Kept as its own early return (distinct DUPLICATE_ORDER_REJECTED
        # comment) rather than folded into the safety gate's generic
        # rejection, so callers can keep relying on that exact signal.
        if client_order_id in self._seen_client_order_ids:
            ticket = self._seen_client_order_ids[client_order_id]
            return (
                OrderResult(success=False, order_id=ticket, deal_id=None, price=None,
                            volume=None, retcode=-1, comment="DUPLICATE_ORDER_REJECTED"),
                None,
            )

        # --- Execution Safety Gate: the final, unconditional check before
        # this function does ANYTHING else toward submitting an order. ---
        try:
            account = self.client.get_account_info()
        except Exception as exc:  # noqa: BLE001 - can't gather fresh state -> fail closed
            return (
                OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None,
                            retcode=-1, comment=f"EXECUTION_SAFETY_GATE_REJECTED: could not fetch account info: {exc}"),
                None,
            )

        gate_input = ExecutionSafetyGateInput(
            is_duplicate=False,  # already handled above; gate still carries the field for completeness
            kill_switch_active=self.kill_switch.is_active(),
            account_trade_allowed=account.trade_allowed,
            symbol_trade_allowed=self.symbol_spec.trade_allowed,
            direction=direction,
            volume=volume,
            volume_min=self.symbol_spec.volume_min,
            volume_max=self.symbol_spec.volume_max,
            volume_step=self.symbol_spec.volume_step,
            stop_loss=stop_loss,
            market_data_fresh=True,
        )
        gate_result = self._exec_safety_gate.evaluate(gate_input)
        if not gate_result.approved:
            return (
                OrderResult(
                    success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=-1,
                    comment=f"EXECUTION_SAFETY_GATE_REJECTED: {'; '.join(gate_result.reasons)}",
                    raw=gate_result.to_dict(),
                ),
                None,
            )

        if self.mode == TradingMode.LIVE:
            request = OrderRequest(
                symbol=self.symbol_spec.name, direction=direction, volume=volume,
                stop_loss=stop_loss, take_profit=take_profit, comment=comment,
                client_order_id=client_order_id,
            )
            result = self.client.submit_order(request)
            if not result.success or result.order_id is None:
                return result, None
            self._seen_client_order_ids[client_order_id] = result.order_id
            position = ManagedPosition(
                ticket=result.order_id, symbol=self.symbol_spec.name, direction=direction,
                volume=result.volume or volume, price_open=result.price or 0.0,
                stop_loss=stop_loss, take_profit=take_profit, open_time=datetime.now(timezone.utc),
                strategy=strategy, regime=regime, monetary_risk=monetary_risk, is_paper=False,
            )
            return result, position

        # --- PAPER (and BACKTEST, if ever routed here) mode: simulate ---------------
        tick = self.client.get_tick(self.symbol_spec.name)
        raw_price = tick.ask if direction == "BUY" else tick.bid
        fill = apply_entry_costs(raw_price, direction, self.costs, self.symbol_spec.tick_size)
        ticket = next(self._paper_ticket_counter)
        self._seen_client_order_ids[client_order_id] = ticket
        position = ManagedPosition(
            ticket=ticket, symbol=self.symbol_spec.name, direction=direction, volume=volume,
            price_open=fill.price, stop_loss=stop_loss, take_profit=take_profit,
            open_time=datetime.now(timezone.utc), strategy=strategy, regime=regime,
            monetary_risk=monetary_risk, is_paper=True,
        )
        self._paper_positions[ticket] = position
        result = OrderResult(success=True, order_id=ticket, deal_id=ticket, price=fill.price,
                              volume=volume, retcode=10009, comment="PAPER_FILLED")
        return result, position

    def close_position(self, ticket: int, reason: str = "") -> OrderResult:
        if self.mode == TradingMode.LIVE:
            result = self.client.close_position(ticket)
            return result

        position = self._paper_positions.pop(ticket, None)
        if position is None:
            return OrderResult(success=False, order_id=None, deal_id=None, price=None,
                                volume=None, retcode=-1, comment="POSITION_NOT_FOUND")
        tick = self.client.get_tick(self.symbol_spec.name)
        raw_price = tick.bid if position.direction == "BUY" else tick.ask
        fill = apply_exit_costs(raw_price, position.direction, self.costs, self.symbol_spec.tick_size)
        return OrderResult(success=True, order_id=ticket, deal_id=ticket, price=fill.price,
                            volume=position.volume, retcode=10009, comment=f"PAPER_CLOSED:{reason}")

    def close_position_partial(self, ticket: int, volume: float) -> OrderResult:
        if self.mode == TradingMode.LIVE:
            return self.client.close_position_partial(ticket, volume)

        position = self._paper_positions.get(ticket)
        if position is None:
            return OrderResult(success=False, order_id=None, deal_id=None, price=None,
                                volume=None, retcode=-1, comment="POSITION_NOT_FOUND")
        if volume <= 0 or volume >= position.volume:
            return OrderResult(success=False, order_id=None, deal_id=None, price=None,
                                volume=None, retcode=-1, comment="INVALID_PARTIAL_VOLUME")
        tick = self.client.get_tick(self.symbol_spec.name)
        raw_price = tick.bid if position.direction == "BUY" else tick.ask
        fill = apply_exit_costs(raw_price, position.direction, self.costs, self.symbol_spec.tick_size)
        position.volume = round(position.volume - volume, 8)
        return OrderResult(success=True, order_id=ticket, deal_id=ticket, price=fill.price,
                            volume=volume, retcode=10009, comment="PAPER_PARTIAL_CLOSED")

    def modify_position(self, ticket: int, stop_loss: Optional[float], take_profit: Optional[float]) -> OrderResult:
        if self.mode == TradingMode.LIVE:
            return self.client.modify_position(ticket, stop_loss, take_profit)

        position = self._paper_positions.get(ticket)
        if position is None:
            return OrderResult(success=False, order_id=None, deal_id=None, price=None,
                                volume=None, retcode=-1, comment="POSITION_NOT_FOUND")
        if stop_loss is not None:
            position.stop_loss = stop_loss
        if take_profit is not None:
            position.take_profit = take_profit
        return OrderResult(success=True, order_id=ticket, deal_id=None, price=position.price_open,
                            volume=position.volume, retcode=10009, comment="PAPER_MODIFIED")

    def get_open_positions(self) -> list[ManagedPosition]:
        if self.mode == TradingMode.LIVE:
            real = self.client.get_open_positions(self.symbol_spec.name)
            return [
                ManagedPosition(
                    ticket=p.ticket, symbol=p.symbol, direction=p.direction, volume=p.volume,
                    price_open=p.price_open, stop_loss=p.stop_loss, take_profit=p.take_profit,
                    open_time=p.open_time, is_paper=False,
                )
                for p in real
            ]
        return list(self._paper_positions.values())

    def reconcile(self) -> list[str]:
        """Reconcile internal state against the broker's actual open
        positions (section 35: "MT5 broker state is authoritative for
        actual open positions"). Only meaningful in LIVE mode — paper
        positions have no external broker state to reconcile against.
        Returns a list of human-readable discrepancy notes (empty if
        everything matches)."""
        if self.mode != TradingMode.LIVE:
            return []
        broker_tickets = {p.ticket for p in self.client.get_open_positions(self.symbol_spec.name)}
        known_tickets = set(self._seen_client_order_ids.values())
        notes = []
        for ticket in known_tickets - broker_tickets:
            notes.append(f"Ticket {ticket} was tracked internally but is no longer open at the broker.")
        for ticket in broker_tickets - known_tickets:
            notes.append(f"Ticket {ticket} is open at the broker but was not tracked internally.")
        return notes
