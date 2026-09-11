"""
Strategy Selector (section 11).

Maps the H4 regime to the strategies allowed to run in it, runs each,
scores actionable signals (section 12), and picks the best-scoring
one. NO_SIGNAL propagates as a first-class, expected result — the
selector never forces a trade just because it ran a strategy.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.regimes.detector import Regime
from app.signals.models import Signal, SignalDirection
from app.signals.scoring import ScoringWeights, score_signal
from app.strategies.base import Strategy
from app.strategies.breakout import BreakoutStrategy
from app.strategies.context import StrategyContext
from app.strategies.mean_reversion import MeanReversionStrategy
from app.strategies.trend_pullback import TrendPullbackStrategy

# Section 11's mapping. HIGH_VOLATILITY intentionally maps to a single,
# reduced-risk-flagged strategy rather than "usually no trade" being a
# hardcoded skip — the breakout strategy itself still has to clear its
# own confirmation bar, so most HIGH_VOLATILITY bars still end in
# NO_SIGNAL in practice.
_REGIME_STRATEGY_MAP: dict[Regime, list[str]] = {
    Regime.TREND_BULLISH: ["TREND_PULLBACK", "BREAKOUT"],
    Regime.TREND_BEARISH: ["TREND_PULLBACK", "BREAKOUT"],
    Regime.RANGE: ["MEAN_REVERSION", "BREAKOUT"],
    Regime.HIGH_VOLATILITY: ["BREAKOUT"],
    Regime.UNCERTAIN: [],  # NO TRADE, by design (section 11)
}


@dataclass
class StrategySelector:
    strategies: dict[str, Strategy] = field(default_factory=dict)
    enabled: dict[str, bool] = field(default_factory=dict)
    scoring_weights: ScoringWeights | None = None

    def __post_init__(self):
        if not self.strategies:
            self.strategies = {
                "TREND_PULLBACK": TrendPullbackStrategy(),
                "BREAKOUT": BreakoutStrategy(),
                "MEAN_REVERSION": MeanReversionStrategy(),
            }
        if not self.enabled:
            self.enabled = {name: True for name in self.strategies}

    def active_strategy_names(self, regime: Regime) -> list[str]:
        candidates = _REGIME_STRATEGY_MAP.get(regime, [])
        return [
            name for name in candidates
            if name in self.strategies and self.enabled.get(name, True)
        ]

    def generate_signals(self, context: StrategyContext) -> list[Signal]:
        """Run every strategy active for the current H4 regime, scoring
        actionable ones. Always returns at least one Signal (possibly
        NO_SIGNAL if the regime allows no strategies or none trigger)."""
        regime = context.h4_regime.regime
        names = self.active_strategy_names(regime)

        if not names:
            return [
                Signal(
                    direction=SignalDirection.NO_SIGNAL,
                    strategy="STRATEGY_SELECTOR",
                    reasons=[f"H4 regime is {regime.value}; no strategies are enabled for this regime (NO TRADE)."],
                )
            ]

        signals = []
        for name in names:
            strategy = self.strategies[name]
            signal = strategy.generate(context)
            if signal.direction != SignalDirection.NO_SIGNAL:
                score_signal(signal, context, self.scoring_weights)
            signals.append(signal)
        return signals

    def best_signal(self, context: StrategyContext) -> Signal:
        """Pick the highest-scoring actionable signal, or a NO_SIGNAL
        summarizing why nothing triggered. A high score alone does not
        mean this trade should execute — the risk engine (Phase 5)
        still has final say."""
        signals = self.generate_signals(context)
        actionable = [s for s in signals if s.direction != SignalDirection.NO_SIGNAL]

        if not actionable:
            reasons = [s.reasons[0] for s in signals if s.reasons]
            return Signal(
                direction=SignalDirection.NO_SIGNAL,
                strategy="STRATEGY_SELECTOR",
                reasons=reasons or ["No strategy produced an actionable signal."],
            )

        actionable.sort(key=lambda s: (s.score or 0), reverse=True)
        return actionable[0]
