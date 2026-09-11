"""
Signal model shared by all strategies (sections 8-11).

Every strategy returns exactly one Signal. NO_SIGNAL is a first-class,
expected outcome — not an error or an empty/null return — matching
"NO TRADE is a valid and important signal" (section 11).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class SignalDirection(str, enum.Enum):
    NO_SIGNAL = "NO_SIGNAL"
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Signal:
    direction: SignalDirection
    strategy: str
    reasons: list[str] = field(default_factory=list)

    confidence: Optional[float] = None  # strategy's own 0-1 confidence
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    invalidation: Optional[float] = None

    # Populated by strategies to let the (generic, strategy-agnostic)
    # scorer award "entry confirmation" / "breakout-or-pullback quality"
    # points (section 12) without the scorer needing to know each
    # strategy's internal logic.
    meta: dict = field(default_factory=dict)

    # Filled in by the scoring layer (app.signals.scoring), not by the
    # strategy itself.
    score: Optional[int] = None
    score_breakdown: Optional[dict] = None
    score_label: Optional[str] = None

    @property
    def risk_reward(self) -> Optional[float]:
        if self.entry is None or self.stop_loss is None or self.take_profit is None:
            return None
        risk = abs(self.entry - self.stop_loss)
        if risk == 0:
            return None
        reward = abs(self.take_profit - self.entry)
        return round(reward / risk, 2)

    def to_dict(self) -> dict:
        return {
            "direction": self.direction.value,
            "strategy": self.strategy,
            "reasons": self.reasons,
            "confidence": self.confidence,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "risk_reward": self.risk_reward,
            "invalidation": self.invalidation,
            "score": self.score,
            "score_breakdown": self.score_breakdown,
            "score_label": self.score_label,
        }


def no_signal(strategy: str, reason: str) -> Signal:
    """Convenience constructor for the (very common) NO_SIGNAL case."""
    return Signal(direction=SignalDirection.NO_SIGNAL, strategy=strategy, reasons=[reason])
