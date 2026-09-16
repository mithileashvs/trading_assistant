"""
Execution Safety Gate (audit sections 19, 54-56).

Distinct from app.safety.gate.SafetyGate (the independent "is this
order objectively safe" re-validation that TradingLoop runs before
deciding to submit at all). This gate is narrower and lives in a
different PLACE for a specific reason: it is called from INSIDE
ExecutionEngine.submit_market_order() itself, unconditionally, before
that method ever touches client.submit_order() or fabricates a paper
fill. That placement is the point — app.safety.gate.SafetyGate is only
enforced because TradingLoop happens to call it before deciding to
submit; if any OTHER caller (a future "manual trade" UI button, a bug,
a different orchestrator) called ExecutionEngine.submit_market_order()
directly, it would completely bypass both the risk engine and
SafetyGate. Section 56's "NO DIRECT STRATEGY -> MT5 ACCESS" is a rule
about not having a path around the gates, not just a naming
convention — so this gate is baked into the one function that is
allowed to reach MT5 order submission, making bypass structurally
impossible rather than merely against convention.

Being inside ExecutionEngine also means it can check things only
knowable at that literal moment: whether this exact client_order_id
has already been submitted (duplicate protection), and freshly-fetched
broker/account state gathered by ExecutionEngine right before this
call — not whatever was fetched earlier in the pipeline.

FAIL CLOSED: like app.safety.gate.SafetyGate, any internal error here
is itself a rejection, never a pass-through.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ExecutionSafetyGateInput:
    is_duplicate: bool

    kill_switch_active: bool
    account_trade_allowed: bool
    symbol_trade_allowed: bool

    direction: str  # "BUY" | "SELL"
    volume: float
    volume_min: float
    volume_max: float
    volume_step: float
    stop_loss: Optional[float]

    market_data_fresh: bool


@dataclass
class ExecutionSafetyGateResult:
    approved: bool
    is_error: bool
    checks: dict[str, bool]
    reasons: list[str]

    def to_dict(self) -> dict:
        return {"approved": self.approved, "is_error": self.is_error,
                "checks": self.checks, "reasons": self.reasons}


class ExecutionSafetyGate:
    """Stateless -- every check is a pure function of the
    ExecutionSafetyGateInput passed to evaluate()."""

    def evaluate(self, data: ExecutionSafetyGateInput) -> ExecutionSafetyGateResult:
        try:
            return self._evaluate(data)
        except Exception as exc:  # noqa: BLE001 - deliberate: ANY failure here fails closed
            return ExecutionSafetyGateResult(
                approved=False,
                is_error=True,
                checks={},
                reasons=[f"ExecutionSafetyGate encountered an internal error and is failing closed: {exc}"],
            )

    def _evaluate(self, data: ExecutionSafetyGateInput) -> ExecutionSafetyGateResult:
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        checks["not_duplicate"] = not data.is_duplicate
        if not checks["not_duplicate"]:
            reasons.append("This client_order_id has already been submitted.")

        checks["kill_switch_ok"] = not data.kill_switch_active
        if not checks["kill_switch_ok"]:
            reasons.append("Kill switch is active.")

        checks["account_trade_allowed"] = data.account_trade_allowed
        if not checks["account_trade_allowed"]:
            reasons.append("Account does not currently permit trading.")

        checks["symbol_trade_allowed"] = data.symbol_trade_allowed
        if not checks["symbol_trade_allowed"]:
            reasons.append("Symbol trading is not currently permitted by the broker.")

        checks["market_data_fresh"] = data.market_data_fresh
        if not checks["market_data_fresh"]:
            reasons.append("Market data is not fresh.")

        checks["direction_valid"] = data.direction in ("BUY", "SELL")
        if not checks["direction_valid"]:
            reasons.append(f"Order direction '{data.direction}' is not BUY or SELL.")

        checks["stop_loss_present"] = data.stop_loss is not None
        if not checks["stop_loss_present"]:
            reasons.append("No stop loss on the order about to be submitted; automated entries require one.")

        checks["volume_within_broker_limits"] = data.volume_min <= data.volume <= data.volume_max
        if not checks["volume_within_broker_limits"]:
            reasons.append(
                f"Volume ({data.volume}) is outside the broker's allowed range "
                f"[{data.volume_min}, {data.volume_max}]."
            )

        if data.volume_step > 0:
            steps = data.volume / data.volume_step
            checks["volume_matches_broker_step"] = abs(steps - round(steps)) < 1e-6
            if not checks["volume_matches_broker_step"]:
                reasons.append(f"Volume ({data.volume}) is not a multiple of the broker's step ({data.volume_step}).")
        else:
            checks["volume_matches_broker_step"] = False
            reasons.append("Broker volume_step is not positive; cannot validate volume step.")

        checks["volume_positive"] = data.volume > 0
        if not checks["volume_positive"]:
            reasons.append("Volume must be positive.")

        approved = all(checks.values())
        return ExecutionSafetyGateResult(approved=approved, is_error=False, checks=checks, reasons=reasons)
