"""
Strategy Lab Data Models (Phase 11).

Defines serializable, deterministic configuration and result data models for
strategy research, parameter sweeps, and overfitting analysis.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


class ComputationalLimitExceededError(ValueError):
    """Raised when a parameter search grid or experiment batch exceeds safety limits."""


@dataclass
class ExperimentConfig:
    """Deterministic configuration for a strategy research experiment."""

    strategy_name: str
    strategy_version: str = "1.0.0"
    enabled: bool = True
    parameters: dict[str, Any] = field(default_factory=dict)
    timeframe: str = "M15"
    regime_thresholds: Optional[dict[str, Any]] = None
    scoring_weights: Optional[dict[str, Any]] = None
    risk_settings: Optional[dict[str, Any]] = None
    execution_costs: Optional[dict[str, Any]] = None
    backtest_config: Optional[dict[str, Any]] = None
    dataset_identity: str = ""
    random_seed: Optional[int] = None
    experiment_id: str = field(init=False)

    def __post_init__(self):
        self.experiment_id = self.compute_experiment_id()

    def compute_experiment_id(self) -> str:
        """Compute a canonical SHA-256 fingerprint from all configuration values."""
        payload = {
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "enabled": self.enabled,
            "parameters": self.parameters,
            "timeframe": self.timeframe,
            "regime_thresholds": self.regime_thresholds,
            "scoring_weights": self.scoring_weights,
            "risk_settings": self.risk_settings,
            "execution_costs": self.execution_costs,
            "backtest_config": self.backtest_config,
            "dataset_identity": self.dataset_identity,
            "random_seed": self.random_seed,
        }
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "enabled": self.enabled,
            "parameters": dict(self.parameters),
            "timeframe": self.timeframe,
            "regime_thresholds": dict(self.regime_thresholds) if self.regime_thresholds else None,
            "scoring_weights": dict(self.scoring_weights) if self.scoring_weights else None,
            "risk_settings": dict(self.risk_settings) if self.risk_settings else None,
            "execution_costs": dict(self.execution_costs) if self.execution_costs else None,
            "backtest_config": dict(self.backtest_config) if self.backtest_config else None,
            "dataset_identity": self.dataset_identity,
            "random_seed": self.random_seed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExperimentConfig:
        return cls(
            strategy_name=data.get("strategy_name", "UNKNOWN"),
            strategy_version=data.get("strategy_version", "1.0.0"),
            enabled=data.get("enabled", True),
            parameters=data.get("parameters", {}),
            timeframe=data.get("timeframe", "M15"),
            regime_thresholds=data.get("regime_thresholds"),
            scoring_weights=data.get("scoring_weights"),
            risk_settings=data.get("risk_settings"),
            execution_costs=data.get("execution_costs"),
            backtest_config=data.get("backtest_config"),
            dataset_identity=data.get("dataset_identity", ""),
            random_seed=data.get("random_seed"),
        )


@dataclass
class ExperimentResult:
    """Descriptive outcome of an evaluated strategy experiment."""

    experiment_id: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "COMPLETED"  # "COMPLETED" | "FAILED" | "REJECTED"
    error: Optional[str] = None
    dataset_period: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    benchmark: dict[str, Any] = field(default_factory=dict)
    reproducibility_hash: Optional[str] = None
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "created_at": self.created_at,
            "status": self.status,
            "error": self.error,
            "dataset_period": dict(self.dataset_period),
            "metrics": dict(self.metrics),
            "benchmark": dict(self.benchmark),
            "reproducibility_hash": self.reproducibility_hash,
            "config": dict(self.config),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExperimentResult:
        return cls(
            experiment_id=data.get("experiment_id", ""),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            status=data.get("status", "COMPLETED"),
            error=data.get("error"),
            dataset_period=data.get("dataset_period", {}),
            metrics=data.get("metrics", {}),
            benchmark=data.get("benchmark", {}),
            reproducibility_hash=data.get("reproducibility_hash"),
            config=data.get("config", {}),
        )


@dataclass
class ParameterGrid:
    """Bounded, deterministic grid search space for parameter sweeps."""

    param_ranges: dict[str, list[Any]] = field(default_factory=dict)
    max_combinations: int = 100

    @property
    def total_combinations(self) -> int:
        if not self.param_ranges:
            return 0
        total = 1
        for vals in self.param_ranges.values():
            total *= len(vals)
        return total

    def generate_combinations(self) -> list[dict[str, Any]]:
        """Deterministically enumerate parameter combinations.
        
        Raises ComputationalLimitExceededError if combinations exceed max_combinations.
        """
        if not self.param_ranges:
            return [{}]

        count = self.total_combinations
        if count > self.max_combinations:
            raise ComputationalLimitExceededError(
                f"Requested parameter grid of {count} combinations exceeds safety limit "
                f"of {self.max_combinations}. Narrow your parameter ranges."
            )

        # Sort keys for deterministic enumeration order across runs
        keys = sorted(self.param_ranges.keys())
        value_lists = [self.param_ranges[k] for k in keys]

        combinations = []
        for combo in itertools.product(*value_lists):
            combinations.append(dict(zip(keys, combo)))
        return combinations


@dataclass
class OverfittingReport:
    """Descriptive comparison of In-Sample vs Out-of-Sample performance."""

    is_net_profit: float
    oos_net_profit: float
    is_trades: int
    oos_trades: int
    is_win_rate: float
    oos_win_rate: float
    is_max_drawdown: float
    oos_max_drawdown: float
    profit_degradation_pct: Optional[float]
    is_period: dict[str, Any]
    oos_period: dict[str, Any]
    in_sample_metrics: dict[str, Any]
    out_of_sample_metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_net_profit": self.is_net_profit,
            "oos_net_profit": self.oos_net_profit,
            "is_trades": self.is_trades,
            "oos_trades": self.oos_trades,
            "is_win_rate": self.is_win_rate,
            "oos_win_rate": self.oos_win_rate,
            "is_max_drawdown": self.is_max_drawdown,
            "oos_max_drawdown": self.oos_max_drawdown,
            "profit_degradation_pct": self.profit_degradation_pct,
            "is_period": self.is_period,
            "oos_period": self.oos_period,
            "in_sample_metrics": self.in_sample_metrics,
            "out_of_sample_metrics": self.out_of_sample_metrics,
        }
