"""
Strategy interface (sections 8-10). Every concrete strategy is
independently testable and returns exactly one Signal — never raises
for "no setup found", since NO_SIGNAL is the expected, valid outcome.
"""
from __future__ import annotations

import abc

from app.signals.models import Signal
from app.strategies.context import StrategyContext


class Strategy(abc.ABC):
    name: str = "BASE_STRATEGY"

    @abc.abstractmethod
    def generate(self, context: StrategyContext) -> Signal: ...
