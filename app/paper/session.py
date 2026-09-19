"""
Paper Session Data Models (Phase 12).

Defines deterministic session configuration and tracking models with SHA-256
reproducibility fingerprints for repeatable Paper Trading simulations.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class PaperSessionConfig:
    """Configuration for a Paper Trading session with deterministic fingerprinting."""

    session_id: str = "default_paper_session"
    symbol: str = "XAUUSD"
    timeframe: str = "M15"
    initial_balance: float = 10_000.0
    random_seed: Optional[int] = None
    slippage_points: float = 0.0
    spread_multiplier: float = 1.0
    strategy_config: Optional[dict[str, Any]] = None
    risk_config: Optional[dict[str, Any]] = None
    cost_config: Optional[dict[str, Any]] = None
    trading_mode: Any = None
    reproducibility_hash: str = field(init=False)

    def __post_init__(self):
        self.reproducibility_hash = self.compute_reproducibility_hash()

    def compute_reproducibility_hash(self) -> str:
        """Computes deterministic SHA-256 fingerprint from configuration fields."""
        payload = {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "initial_balance": self.initial_balance,
            "random_seed": self.random_seed,
            "slippage_points": self.slippage_points,
            "spread_multiplier": self.spread_multiplier,
            "strategy_config": self.strategy_config,
            "risk_config": self.risk_config,
            "cost_config": self.cost_config,
        }
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if hasattr(self.trading_mode, "value"):
            d["trading_mode"] = self.trading_mode.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperSessionConfig:
        d = dict(data)
        d.pop("reproducibility_hash", None)
        return cls(**d)


@dataclass
class PaperSession:
    """Maintains active state, lifecycle, and summary metrics for a Paper Trading session."""

    config: PaperSessionConfig
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    status: str = "INITIALIZED"  # "INITIALIZED" | "RUNNING" | "STOPPED" | "FAILED"
    stop_reason: Optional[str] = None
    bars_processed: int = 0
    error: Optional[str] = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        config: Optional[PaperSessionConfig] = None,
        created_at: Optional[datetime] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        status: str = "INITIALIZED",
        stop_reason: Optional[str] = None,
        bars_processed: int = 0,
        error: Optional[str] = None,
        metrics: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
        **kwargs: Any,
    ):
        self.config = config or PaperSessionConfig()
        if session_id is not None:
            self.config.session_id = session_id
        self.created_at = created_at or datetime.now(timezone.utc)
        self.start_time = start_time
        self.end_time = end_time
        self.status = status
        self.stop_reason = stop_reason
        self.bars_processed = bars_processed
        self.error = error
        self.metrics = metrics or {}

    @property
    def session_id(self) -> str:
        return self.config.session_id

    def start(self, timestamp: Optional[datetime] = None) -> None:
        self.start_time = timestamp or datetime.now(timezone.utc)
        self.status = "RUNNING"

    def stop(self, timestamp: Optional[datetime] = None, metrics: Optional[dict[str, Any]] = None, reason: str = "") -> None:
        self.end_time = timestamp or datetime.now(timezone.utc)
        self.status = "STOPPED"
        if reason:
            self.stop_reason = reason
        if metrics:
            self.metrics = metrics

    def fail(self, error: str, timestamp: Optional[datetime] = None) -> None:
        self.end_time = timestamp or datetime.now(timezone.utc)
        self.status = "FAILED"
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "config": self.config.to_dict(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "bars_processed": self.bars_processed,
            "error": self.error,
            "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperSession:
        d = dict(data)
        cfg_raw = d.pop("config", {})
        cfg = PaperSessionConfig.from_dict(cfg_raw)
        sid = d.pop("session_id", None)
        if sid and not cfg.session_id:
            cfg.session_id = sid
        if d.get("created_at") and isinstance(d["created_at"], str):
            d["created_at"] = datetime.fromisoformat(d["created_at"])
        if d.get("start_time") and isinstance(d["start_time"], str):
            d["start_time"] = datetime.fromisoformat(d["start_time"])
        if d.get("end_time") and isinstance(d["end_time"], str):
            d["end_time"] = datetime.fromisoformat(d["end_time"])
        return cls(config=cfg, session_id=sid, **d)
