"""
Explainability (section 32).

Every trade decision must answer WHY / WHY NOW / WHY THIS STRATEGY /
WHY THIS ENTRY / WHY THIS STOP / WHY THIS TARGET / HOW MUCH RISK / WHAT
WOULD INVALIDATE IT. This is built ENTIRELY from structured objects the
rest of the system already produces (Signal, RegimeDecision,
TradeValidation) — no LLM call, no interpretation beyond formatting.
An LLM can optionally rephrase the output (see llm_client.py) but
never originates any of these facts.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.regimes.detector import RegimeDecision
from app.risk.validator import TradeValidation
from app.signals.models import Signal, SignalDirection


@dataclass
class TradeExplanation:
    why: str
    why_now: str
    why_this_strategy: str
    why_this_entry: str
    why_this_stop: str
    why_this_target: str
    how_much_risk: str
    what_would_invalidate_it: str
    decision: str
    decision_detail: str

    def to_text(self) -> str:
        lines = [
            f"{self.decision} {self.decision_detail}".strip(),
            "",
            "WHY?",
            self.why,
            "",
            "WHY NOW?",
            self.why_now,
            "",
            "WHY THIS STRATEGY?",
            self.why_this_strategy,
            "",
            "WHY THIS ENTRY?",
            self.why_this_entry,
            "",
            "WHY THIS STOP?",
            self.why_this_stop,
            "",
            "WHY THIS TARGET?",
            self.why_this_target,
            "",
            "HOW MUCH RISK?",
            self.how_much_risk,
            "",
            "WHAT WOULD INVALIDATE IT?",
            self.what_would_invalidate_it,
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision, "decision_detail": self.decision_detail,
            "why": self.why, "why_now": self.why_now, "why_this_strategy": self.why_this_strategy,
            "why_this_entry": self.why_this_entry, "why_this_stop": self.why_this_stop,
            "why_this_target": self.why_this_target, "how_much_risk": self.how_much_risk,
            "what_would_invalidate_it": self.what_would_invalidate_it,
        }


def explain_trade(signal: Signal, regime: RegimeDecision, validation: TradeValidation) -> TradeExplanation:
    if signal.direction == SignalDirection.NO_SIGNAL:
        return TradeExplanation(
            why=f"H4 regime is {regime.regime.value} ({regime.confidence:.0%} confidence): "
                + "; ".join(regime.reasons),
            why_now="No strategy produced an actionable setup on this bar.",
            why_this_strategy="N/A — no strategy triggered.",
            why_this_entry="N/A",
            why_this_stop="N/A",
            why_this_target="N/A",
            how_much_risk="N/A — no trade to size.",
            what_would_invalidate_it="N/A",
            decision="NO TRADE",
            decision_detail="(no actionable signal)",
        )

    reasons = signal.reasons or []
    # Reasons are ordered roughly higher-timeframe-first by convention
    # in every strategy (section 8-10), so the first ones tend to
    # answer WHY (broad context) and later ones answer WHY NOW (the
    # specific trigger) -- but we don't over-claim precision here,
    # just present the full reasoning trail under both headers so
    # nothing is hidden.
    why = f"H4 regime: {regime.regime.value} ({regime.confidence:.0%} confidence). " + "; ".join(regime.reasons)
    why_now = "; ".join(reasons) if reasons else "Strategy conditions were met on this bar."

    risk_pct = validation.risk_percent
    lots = validation.lots
    how_much_risk = f"{risk_pct}% of equity (~{lots} lots at current sizing)."

    invalidation = signal.invalidation if signal.invalidation is not None else signal.stop_loss
    what_would_invalidate_it = (
        f"Price reaching the stop/invalidation level ({invalidation}), or the H4 regime changing away from "
        f"{regime.regime.value} before the trade triggers."
    )

    decision = "APPROVED" if validation.approved else "REJECTED"
    decision_detail = f"{signal.direction.value} {validation.symbol}"
    if not validation.approved and validation.rejection_reasons:
        decision_detail += " — " + "; ".join(validation.rejection_reasons)

    return TradeExplanation(
        why=why,
        why_now=why_now,
        why_this_strategy=f"{signal.strategy} was the highest-scoring strategy active for the {regime.regime.value} regime "
                           f"(score {signal.score}, {signal.score_label}).",
        why_this_entry=f"Entry at {signal.entry}, as determined by {signal.strategy}'s own entry rule.",
        why_this_stop=f"Stop-loss at {signal.stop_loss}, as determined by {signal.strategy}'s configured stop method.",
        why_this_target=f"Take-profit at {signal.take_profit} "
                         f"(risk:reward {signal.risk_reward if signal.risk_reward is not None else 'N/A'}).",
        how_much_risk=how_much_risk,
        what_would_invalidate_it=what_would_invalidate_it,
        decision=decision,
        decision_detail=decision_detail,
    )
