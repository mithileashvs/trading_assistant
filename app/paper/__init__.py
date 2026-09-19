"""
Paper Trading Module (Phase 12).

Provides simulation-only paper trading runtime, isolated execution adapter,
simulated account, excursion tracking positions, and persistent session state.
"""
from app.paper.account import PaperAccount
from app.paper.broker import PaperBroker, PaperExecutionAdapter
from app.paper.metrics import compute_paper_metrics
from app.paper.positions import PaperPosition
from app.paper.runtime import PaperTradingRuntime
from app.paper.session import PaperSession, PaperSessionConfig
from app.paper.store import (
    InMemoryPaperStateStore,
    PaperStateStore,
    SqlitePaperStateStore,
    StateCorruptedError,
)

__all__ = [
    "PaperAccount",
    "PaperPosition",
    "PaperExecutionAdapter",
    "PaperBroker",
    "PaperSession",
    "PaperSessionConfig",
    "PaperTradingRuntime",
    "PaperStateStore",
    "InMemoryPaperStateStore",
    "SqlitePaperStateStore",
    "StateCorruptedError",
    "compute_paper_metrics",
]
