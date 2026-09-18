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
without submitting a second order. This also covers the UNCERTAIN case
(Phase 6): if a LIVE submission's outcome could not be determined (the
connection dropped mid-flight, or the client explicitly reports
raw["status"] == EXECUTION_STATUS_UNKNOWN — see app.mt5.interface),
the client_order_id is still recorded, with no ticket, specifically so
a second attempt under the SAME client_order_id is rejected rather
than silently creating a second order at the broker. Resolving that
uncertainty is a manual/operational step (compare against
self.reconcile(), which is exactly what it exists for), not something
this engine ever guesses at or retries automatically — it never has,
and Phase 6 didn't add any retry logic.

EXECUTION SAFETY GATE (audit sections 19, 54-56): submit_market_order()
calls ExecutionSafetyGate internally, unconditionally, before EVER
reaching client.submit_order() or fabricating a paper fill. This is
deliberate placement, not just an extra check — it's what makes
bypassing the safety gates structurally impossible rather than merely
against convention: any caller of submit_market_order(), present or
future, inherits this check automatically, because there is no path
to MT5 submission that doesn't go through this exact function.

EXECUTION & RECOVERY (Phase 7): the client_order_id -> outcome
bookkeeping described above is no longer a plain in-memory dict — it
is delegated to an ExecutionStateStore (app.execution.state_store),
so an UNKNOWN/uncertain outcome (and the idempotency record of an
already-FILLED order) is never silently forgotten if the process
restarts before it's been reconciled. The default store
(InMemoryExecutionStateStore) preserves the exact pre-Phase-7
behavior — state lives only as long as the process does — for the
same reason ExecutionEngine defaults to a _NullKillSwitch rather than
a file-backed one (see below): a persistent default would mean every
standalone ExecutionEngine, tests included, silently shares whatever
happens to be on disk. Production entry points (TradingLoop) pass an
explicit, persistent SqliteExecutionStateStore instead.

Resolving an UNKNOWN outcome is still never automatic in the sense of
"guess and proceed": reconcile_unknown() only ever adopts a
resolution when the broker provides UNAMBIGUOUS evidence (a currently
open position tagged with this exact order's client_order_id — see
_broker_comment_tag), and resolve_unknown_execution() is the explicit,
operator-driven path for every other case. Nothing in this module
ever converts an unresolved UNKNOWN into permission to submit a second
order for the same logical trade.
"""
from __future__ import annotations

import itertools
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

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
from app.mt5.interface import EXECUTION_STATUS_UNKNOWN, STALE_TICK_SECONDS, IMT5Client, OrderRequest, OrderResult, Position, SymbolSpec, Tick


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
class UnknownReconciliationResult:
    """One record's outcome from reconcile_unknown() (Phase 7)."""
    client_order_id: str
    outcome: str  # "RESOLVED_FILLED" | "STILL_UNKNOWN" | "ERROR"
    ticket: Optional[int]
    detail: str


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
        state_store: Optional[ExecutionStateStore] = None,
    ):
        self.client = client
        self.symbol_spec = symbol_spec
        self.mode = mode
        self.costs = costs or ExecutionCosts()
        self.kill_switch = kill_switch or _NullKillSwitch()
        self._exec_safety_gate = ExecutionSafetyGate()
        # client_order_id -> outcome, persisted via an ExecutionStateStore
        # (Phase 7) -- see module docstring for why the default is
        # in-memory-only, matching pre-Phase-7 behavior.
        self._state_store = state_store or InMemoryExecutionStateStore()
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
        #
        # Phase 7: an entry only blocks resubmission while its status is
        # in BLOCKING_STATUSES (FILLED / UNKNOWN / RESOLVED_FILLED).
        # RESOLVED_NOT_FOUND is deliberately NOT blocking -- once an
        # operator has explicitly confirmed (via resolve_unknown_execution)
        # that this logical order never reached the broker, the same
        # client_order_id becomes usable again, exactly so a retry never
        # needs to invent a new id to bypass this protection. A plain
        # REJECTED outcome was never recorded here at all (unchanged
        # since Phase 6), so it never blocked resubmission either.
        existing = self._state_store.get(client_order_id)
        if existing is not None and existing.status in BLOCKING_STATUSES:
            ticket = existing.ticket
            if existing.status == STATUS_UNKNOWN:
                comment = (
                    "DUPLICATE_ORDER_REJECTED: a previous submission for this client_order_id had an "
                    "UNKNOWN/uncertain outcome; reconcile against the broker (see reconcile() / "
                    "reconcile_unknown() / resolve_unknown_execution()) before resubmitting -- never "
                    "automatically retried"
                )
            else:
                comment = "DUPLICATE_ORDER_REJECTED"
            return (
                OrderResult(success=False, order_id=ticket, deal_id=None, price=None,
                            volume=None, retcode=-1, comment=comment),
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
            # Phase 7: the broker-side comment carries a deterministic
            # tag derived from client_order_id (see _broker_comment_tag)
            # rather than the caller-supplied `comment` -- this is what
            # lets reconcile_unknown() find an UNAMBIGUOUS match for a
            # given logical order among the broker's open positions
            # later. No existing caller passes a non-empty `comment`
            # today (see app.runtime.loop.TradingLoop), so nothing
            # user-facing is lost; this is purely additive recovery
            # infrastructure, not a change to trading behavior.
            #
            # Deliberately NOT forwarding client_order_id itself on the
            # broker-level OrderRequest (Phase 7 fix): real MT5 has no
            # such field at all -- app.mt5.real_client.RealMT5Client
            # never reads request.client_order_id when building the
            # request it sends to mt5.order_send(). The ONLY consumer
            # of that field is MockMT5Client's own supplementary,
            # PERMANENT duplicate-id set (self._client_order_ids) --
            # a mock-only realism/defense-in-depth check with no
            # broker-side equivalent, and no notion of "resolved".
            # ExecutionEngine's own state store (self._state_store) is
            # -- and was already, since Phase 6 -- the sole authoritative
            # duplicate-order safeguard: it is checked before this
            # method ever reaches the client (see the early-return
            # duplicate check above), so nothing is lost by not also
            # tagging the broker-level request with it. Forwarding it
            # would instead actively break Phase 7's required "resolve
            # UNKNOWN as NOT_FOUND, then the SAME client_order_id may be
            # reused" flow: the mock's own set never forgets an id, so
            # a legitimate, explicitly-authorized resubmission would be
            # rejected a second time by the MOCK layer even after
            # ExecutionEngine's own check had correctly permitted it.
            request = OrderRequest(
                symbol=self.symbol_spec.name, direction=direction, volume=volume,
                stop_loss=stop_loss, take_profit=take_profit,
                comment=self._broker_comment_tag(client_order_id),
            )
            # Phase 6: submit_order() itself can raise (e.g. a connection
            # drop) as well as return success=False. ANY exception here
            # means we genuinely cannot tell whether the broker accepted
            # the order -- there is no way to distinguish "failed before
            # send" from "failed after send" from outside the client, so
            # the ONLY safe assumption is uncertainty, never success and
            # never a clean failure either. Recorded as "seen, ticket
            # unknown" so a resubmission under the same client_order_id
            # is blocked above rather than silently creating a second
            # order — and nothing here retries automatically.
            try:
                result = self.client.submit_order(request)
            except Exception as exc:  # noqa: BLE001 - deliberate: fail closed to UNKNOWN, not success or silent failure
                self._record_unknown(client_order_id, direction, volume)
                return (
                    OrderResult(
                        success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=None,
                        comment=f"UNKNOWN_EXECUTION_RESULT: submit_order raised {type(exc).__name__}: {exc}; "
                                "broker acceptance could not be confirmed",
                        raw={"status": EXECUTION_STATUS_UNKNOWN},
                    ),
                    None,
                )

            if result.raw.get("status") == EXECUTION_STATUS_UNKNOWN:
                self._record_unknown(client_order_id, direction, volume)
                if result.success:
                    # Defense in depth: an UNKNOWN-tagged result must
                    # NEVER be reported to the caller as a success, no
                    # matter what the client itself claims -- normalize
                    # rather than trust a single field on its own. A
                    # well-behaved client should never set both, but
                    # this is exactly the kind of thing an "adversarial"
                    # audit exists to catch: without this, a buggy or
                    # malicious client claiming success=True alongside
                    # UNKNOWN would have its success flag passed through
                    # untouched, even though no position is tracked here
                    # -- turning an uncertain response into what LOOKS
                    # like a successful trade from the caller's side.
                    result = OrderResult(
                        success=False, order_id=result.order_id, deal_id=result.deal_id,
                        price=result.price, volume=result.volume, retcode=result.retcode,
                        comment=f"UNKNOWN_EXECUTION_RESULT: client reported success=True alongside an "
                                f"UNKNOWN status, which is never trusted -- original comment: {result.comment!r}",
                        raw=result.raw,
                    )
                return result, None

            if not result.success or result.order_id is None:
                return result, None
            self._state_store.put(ExecutionRecord(
                client_order_id=client_order_id, status=STATUS_FILLED, ticket=result.order_id,
                symbol=self.symbol_spec.name, direction=direction, volume=result.volume or volume,
            ))
            position = ManagedPosition(
                ticket=result.order_id, symbol=self.symbol_spec.name, direction=direction,
                volume=result.volume or volume, price_open=result.price or 0.0,
                stop_loss=stop_loss, take_profit=take_profit, open_time=datetime.now(timezone.utc),
                strategy=strategy, regime=regime, monetary_risk=monetary_risk, is_paper=False,
            )
            return result, position

        # --- PAPER (and BACKTEST, if ever routed here) mode: simulate ---------------
        tick, rejection = self._fetch_valid_tick("simulate a fill")
        if rejection is not None:
            return rejection, None
        raw_price = tick.ask if direction == "BUY" else tick.bid
        fill = apply_entry_costs(raw_price, direction, self.costs, self.symbol_spec.tick_size)
        ticket = next(self._paper_ticket_counter)
        self._state_store.put(ExecutionRecord(
            client_order_id=client_order_id, status=STATUS_FILLED, ticket=ticket,
            symbol=self.symbol_spec.name, direction=direction, volume=volume,
        ))
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

    def _record_unknown(self, client_order_id: str, direction: str, volume: float) -> None:
        """Persists an UNKNOWN/uncertain outcome (Phase 6/7) via the
        state store, so it survives a restart and continues to block a
        blind resubmission under the same client_order_id until
        explicitly reconciled."""
        self._state_store.put(ExecutionRecord(
            client_order_id=client_order_id, status=STATUS_UNKNOWN, ticket=None,
            symbol=self.symbol_spec.name, direction=direction, volume=volume,
        ))

    @staticmethod
    def _broker_comment_tag(client_order_id: str) -> str:
        """Deterministic tag embedded in the broker-side order comment
        (Phase 7) so a LIVE order's open position can later be matched
        back to the client_order_id that created it -- see
        reconcile_unknown(). Real MT5 (and this mock) comment fields
        are conservatively assumed to be capped around 31 characters;
        when the raw id doesn't fit, a stable short hash is used
        instead so the SAME client_order_id always produces the SAME
        tag (required for matching to work at all), even though the
        hash form isn't reversible on its own -- reconcile_unknown()
        only ever needs to recompute and compare the tag, never decode
        it back to the original id."""
        prefix = "coid:"
        max_len = 31
        if len(prefix) + len(client_order_id) <= max_len:
            return prefix + client_order_id
        digest = format(zlib.crc32(client_order_id.encode("utf-8")) & 0xFFFFFFFF, "08x")
        return prefix + "h" + digest

    def unresolved_executions(self) -> list[ExecutionRecord]:
        """Every client_order_id whose outcome is still UNKNOWN and
        awaiting reconciliation (Phase 7). Used by startup recovery
        (app.safety.startup_check) to surface these instead of silently
        forgetting them across a restart."""
        return self._state_store.unresolved()

    def reconcile_unknown(self) -> list[UnknownReconciliationResult]:
        """Attempts to resolve every still-UNKNOWN execution against
        the broker's CURRENT open positions (LIVE mode only).

        This only ever adopts an automatic resolution when the
        evidence is unambiguous: an open position tagged (via
        _broker_comment_tag) with this exact client_order_id. That is
        reading a definite fact from the broker, not "fixing" an
        ambiguous discrepancy, so it's safe to apply without an
        operator in the loop.

        When no such position exists the outcome is left UNKNOWN --
        deliberately -- because absence of an open position is
        genuinely ambiguous: the order may never have reached the
        broker, OR it may have filled and already been closed before
        this reconciliation ran, and nothing observable here can tell
        those two apart. Only an operator, using broker records this
        engine doesn't have access to (e.g. trade history), can
        resolve that case -- via resolve_unknown_execution().
        """
        if self.mode != TradingMode.LIVE:
            return []
        try:
            positions = self.client.get_open_positions(self.symbol_spec.name)
        except Exception as exc:  # noqa: BLE001 - can't fetch broker state -> report, resolve nothing
            return [UnknownReconciliationResult(
                client_order_id="*", outcome="ERROR", ticket=None,
                detail=f"Could not fetch broker positions to reconcile against: {type(exc).__name__}: {exc}",
            )]
        by_tag = {p.comment: p for p in positions}

        results: list[UnknownReconciliationResult] = []
        for record in self._state_store.all():
            if record.status != STATUS_UNKNOWN:
                continue
            tag = self._broker_comment_tag(record.client_order_id)
            match = by_tag.get(tag)
            if match is not None:
                self._state_store.put(ExecutionRecord(
                    client_order_id=record.client_order_id, status=STATUS_RESOLVED_FILLED,
                    ticket=match.ticket, symbol=record.symbol, direction=record.direction,
                    volume=record.volume, created_at=record.created_at,
                    note="auto-resolved by reconcile_unknown(): a matching open position was found at the broker.",
                ))
                results.append(UnknownReconciliationResult(
                    client_order_id=record.client_order_id, outcome="RESOLVED_FILLED", ticket=match.ticket,
                    detail="A matching open position was found at the broker; treated as FILLED.",
                ))
            else:
                results.append(UnknownReconciliationResult(
                    client_order_id=record.client_order_id, outcome="STILL_UNKNOWN", ticket=None,
                    detail=(
                        "No matching open position found at the broker. This does NOT necessarily mean the "
                        "order never happened -- it may have filled and already closed. Remains UNKNOWN and "
                        "still blocks resubmission until resolved explicitly via resolve_unknown_execution()."
                    ),
                ))
        return results

    def resolve_unknown_execution(self, client_order_id: str, ticket: Optional[int], note: str) -> None:
        """Explicit, operator-driven resolution of an UNKNOWN execution
        (Phase 7) -- the only way an unresolved record ever stops
        blocking a resubmission under the same client_order_id other
        than the automatic, unambiguous path in reconcile_unknown().
        Nothing in this engine ever infers or guesses this on its own.

        Pass the real broker ticket if the order is now confirmed
        (e.g. from the broker's own trade history) to have filled, or
        ticket=None if it's confirmed to have never reached/executed
        at the broker. `note` should record how this was confirmed,
        for the audit trail.
        """
        record = self._state_store.get(client_order_id)
        if record is None or record.status != STATUS_UNKNOWN:
            raise ValueError(
                f"client_order_id {client_order_id!r} has no unresolved UNKNOWN execution to resolve."
            )
        status = STATUS_RESOLVED_FILLED if ticket is not None else STATUS_RESOLVED_NOT_FOUND
        self._state_store.put(ExecutionRecord(
            client_order_id=client_order_id, status=status, ticket=ticket,
            symbol=record.symbol, direction=record.direction, volume=record.volume,
            created_at=record.created_at, note=note,
        ))

    def _fetch_valid_tick(self, purpose: str) -> tuple[Optional[Tick], Optional[OrderResult]]:
        """Fetches a tick for a PAPER-mode fill/close/partial-close and
        validates it's usable, fail-closed (Phase 6): a missing,
        crossed/non-positive, or too-old tick must never be used just
        to make a simulated fill or close succeed. Returns (tick, None)
        when the tick is fine to use, or (None, rejection) when it
        isn't -- callers return `rejection` immediately in that case.
        """
        try:
            tick = self.client.get_tick(self.symbol_spec.name)
        except Exception as exc:  # noqa: BLE001 - no quote available -> fail closed, no simulated action
            return None, OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None,
                                      retcode=-1, comment=f"NO_QUOTE: could not fetch a tick to {purpose}: {exc}")
        if tick.ask <= tick.bid or tick.bid <= 0:
            return None, OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None,
                                      retcode=-1, comment=f"INVALID_QUOTE: current tick is crossed or "
                                                            f"non-positive; refusing to {purpose} on invalid "
                                                            "market data")
        if (datetime.now(timezone.utc) - tick.time).total_seconds() > STALE_TICK_SECONDS:
            return None, OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None,
                                      retcode=-1, comment=f"STALE_QUOTE: current tick is too old to {purpose} "
                                                            "against")
        return tick, None

    @staticmethod
    def _safe_client_call(fn, *args, action: str) -> OrderResult:
        """Calls a LIVE-mode client method (close/partial-close/modify)
        and converts ANY exception into a safe OrderResult(success=False)
        instead of letting it propagate uncaught (Phase 6) -- mirroring
        submit_market_order's own fail-closed handling. Unlike a new
        order, there is no duplicate-order risk to guard against here:
        a failed close/modify simply leaves the position open/unchanged
        at the broker, which is the safe default, so no extra
        client_order_id-style bookkeeping is needed."""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - fail closed: nothing here is assumed to have succeeded
            return OrderResult(
                success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=-1,
                comment=f"{action}_FAILED: {type(exc).__name__}: {exc}; outcome could not be confirmed, "
                        "nothing was assumed to have succeeded",
            )

    def close_position(self, ticket: int, reason: str = "") -> OrderResult:
        if self.mode == TradingMode.LIVE:
            return self._safe_client_call(self.client.close_position, ticket, action="CLOSE")

        position = self._paper_positions.get(ticket)
        if position is None:
            return OrderResult(success=False, order_id=None, deal_id=None, price=None,
                                volume=None, retcode=-1, comment="POSITION_NOT_FOUND")
        tick, rejection = self._fetch_valid_tick("close the position")
        if rejection is not None:
            return rejection
        raw_price = tick.bid if position.direction == "BUY" else tick.ask
        fill = apply_exit_costs(raw_price, position.direction, self.costs, self.symbol_spec.tick_size)
        # Only remove from tracking once the close is fully computed --
        # never before (mirrors the same fix in MockMT5Client.close_position).
        del self._paper_positions[ticket]
        return OrderResult(success=True, order_id=ticket, deal_id=ticket, price=fill.price,
                            volume=position.volume, retcode=10009, comment=f"PAPER_CLOSED:{reason}")

    def close_position_partial(self, ticket: int, volume: float) -> OrderResult:
        if self.mode == TradingMode.LIVE:
            return self._safe_client_call(self.client.close_position_partial, ticket, volume, action="PARTIAL_CLOSE")

        position = self._paper_positions.get(ticket)
        if position is None:
            return OrderResult(success=False, order_id=None, deal_id=None, price=None,
                                volume=None, retcode=-1, comment="POSITION_NOT_FOUND")
        if volume <= 0 or volume >= position.volume:
            return OrderResult(success=False, order_id=None, deal_id=None, price=None,
                                volume=None, retcode=-1, comment="INVALID_PARTIAL_VOLUME")
        tick, rejection = self._fetch_valid_tick("partially close the position")
        if rejection is not None:
            return rejection
        raw_price = tick.bid if position.direction == "BUY" else tick.ask
        fill = apply_exit_costs(raw_price, position.direction, self.costs, self.symbol_spec.tick_size)
        position.volume = round(position.volume - volume, 8)
        return OrderResult(success=True, order_id=ticket, deal_id=ticket, price=fill.price,
                            volume=volume, retcode=10009, comment="PAPER_PARTIAL_CLOSED")

    def modify_position(self, ticket: int, stop_loss: Optional[float], take_profit: Optional[float]) -> OrderResult:
        if self.mode == TradingMode.LIVE:
            return self._safe_client_call(self.client.modify_position, ticket, stop_loss, take_profit, action="MODIFY")

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
        records = self._state_store.all()
        # Exclude UNKNOWN-outcome entries (ticket=None, Phase 6) -- there
        # is no ticket to compare against broker state for those; that's
        # exactly the point of reconcile() existing, but a bare `None`
        # must never be treated as a phantom "tracked ticket".
        known_tickets = {r.ticket for r in records if r.ticket is not None}
        unresolved = sum(1 for r in records if r.status == STATUS_UNKNOWN)
        notes = []
        if unresolved:
            notes.append(
                f"{unresolved} client_order_id(s) have an UNKNOWN/uncertain outcome and need manual "
                "reconciliation against the broker's own trade history (not just open positions)."
            )
        for ticket in known_tickets - broker_tickets:
            notes.append(f"Ticket {ticket} was tracked internally but is no longer open at the broker.")
        for ticket in broker_tickets - known_tickets:
            notes.append(f"Ticket {ticket} is open at the broker but was not tracked internally.")
        return notes
