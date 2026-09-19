"""
Strategy Lab Runner (Phase 11).

Orchestrates strategy experiments, parameter sweeps, walk-forward testing,
and multi-strategy comparisons.

STRICT SAFETY ENFORCEMENT:
- SIMULATION ONLY: TradingMode.LIVE is strictly rejected.
- REUSE ONLY: BacktestEngine is the ONLY simulation engine used.
- NO BROKER CONNECTIONS: Never calls ExecutionEngine or MT5 order placement.
- DESCRIPTIVE ONLY: Never outputs rankings or declares "winning" strategies.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

from app.backtesting.costs import ExecutionCosts
from app.backtesting.engine import BacktestConfig, BacktestEngine
from app.backtesting.metrics import compute_metrics
from app.config.settings import RiskSettings, TradingMode
from app.mt5.interface import SymbolSpec
from app.regimes.detector import RegimeThresholds
from app.signals.scoring import ScoringWeights
from app.strategies.breakout import BreakoutConfig, BreakoutStrategy
from app.strategies.mean_reversion import MeanReversionConfig, MeanReversionStrategy
from app.strategies.selector import StrategySelector
from app.strategies.trend_pullback import TrendPullbackConfig, TrendPullbackStrategy
from app.strategy_lab.compare import ALL_STRATEGY_NAMES, StrategyComparisonEntry, compare_strategies
from app.strategy_lab.models import (
    ComputationalLimitExceededError,
    ExperimentConfig,
    ExperimentResult,
    OverfittingReport,
    ParameterGrid,
)
from app.strategy_lab.oos import SplitResult, compute_overfitting_analysis, run_in_sample_out_of_sample, walk_forward_folds
from app.strategy_lab.store import ResearchStore

logger = logging.getLogger("xau_trader.strategy_lab")


class StrategyLabRunner:
    """Simulation-only runner for Strategy Lab research experiments."""

    def __init__(
        self,
        symbol_spec: SymbolSpec,
        risk_settings: RiskSettings,
        costs: Optional[ExecutionCosts] = None,
        backtest_config: Optional[BacktestConfig] = None,
        store: Optional[ResearchStore] = None,
        trading_mode: TradingMode = TradingMode.BACKTEST,
    ):
        # Strict Safety Gate: Strategy Lab must NEVER execute in LIVE mode
        if trading_mode == TradingMode.LIVE or getattr(risk_settings, "trading_mode", None) == TradingMode.LIVE:
            raise RuntimeError("Safety violation: Strategy Lab is simulation-only and cannot run in LIVE mode.")

        self.symbol_spec = symbol_spec
        self.risk_settings = risk_settings
        self.costs = costs or ExecutionCosts()
        self.default_backtest_config = backtest_config or BacktestConfig()
        self.store = store
        self.trading_mode = trading_mode

    def build_selector(self, config: ExperimentConfig) -> StrategySelector:
        """Constructs a StrategySelector configured with the experiment's strategy and parameters."""
        s_name = config.strategy_name.upper()
        params = config.parameters or {}

        if s_name == "TREND_PULLBACK":
            cfg = TrendPullbackConfig(**params) if params else TrendPullbackConfig()
            strategy = TrendPullbackStrategy(config=cfg)
            return StrategySelector(
                strategies={strategy.name: strategy},
                enabled={strategy.name: config.enabled},
            )
        elif s_name == "BREAKOUT":
            cfg = BreakoutConfig(**params) if params else BreakoutConfig()
            strategy = BreakoutStrategy(config=cfg)
            return StrategySelector(
                strategies={strategy.name: strategy},
                enabled={strategy.name: config.enabled},
            )
        elif s_name == "MEAN_REVERSION":
            cfg = MeanReversionConfig(**params) if params else MeanReversionConfig()
            strategy = MeanReversionStrategy(config=cfg)
            return StrategySelector(
                strategies={strategy.name: strategy},
                enabled={strategy.name: config.enabled},
            )
        elif s_name == "ALL":
            tp_cfg = TrendPullbackConfig()
            brk_cfg = BreakoutConfig()
            mr_cfg = MeanReversionConfig()
            strategies = {
                "TREND_PULLBACK": TrendPullbackStrategy(config=tp_cfg),
                "BREAKOUT": BreakoutStrategy(config=brk_cfg),
                "MEAN_REVERSION": MeanReversionStrategy(config=mr_cfg),
            }
            return StrategySelector(
                strategies=strategies,
                enabled={k: config.enabled for k in strategies},
            )
        else:
            raise ValueError(f"Unknown strategy_name: {config.strategy_name}")

    def _build_engine_for_config(self, config: ExperimentConfig) -> BacktestEngine:
        """Helper to create a configured BacktestEngine for an experiment."""
        selector = self.build_selector(config)

        # Merge or override BacktestConfig
        bt_kwargs = dict(self.default_backtest_config.__dict__)
        if config.backtest_config:
            bt_kwargs.update(config.backtest_config)
        if config.random_seed is not None:
            bt_kwargs["random_seed"] = config.random_seed
        effective_bt_cfg = BacktestConfig(**bt_kwargs)

        # Merge or override ExecutionCosts
        effective_costs = ExecutionCosts(**config.execution_costs) if config.execution_costs else self.costs

        # Merge or override RiskSettings
        if config.risk_settings:
            effective_risk = RiskSettings(**config.risk_settings)
            if effective_risk.trading_mode == TradingMode.LIVE:
                raise RuntimeError("Safety violation: Strategy Lab cannot run with LIVE risk settings.")
        else:
            effective_risk = self.risk_settings

        # Optional regime thresholds and scoring weights
        regime_thresholds = RegimeThresholds(**config.regime_thresholds) if config.regime_thresholds else None
        scoring_weights = ScoringWeights(**config.scoring_weights) if config.scoring_weights else None

        return BacktestEngine(
            symbol_spec=self.symbol_spec,
            risk_settings=effective_risk,
            costs=effective_costs,
            config=effective_bt_cfg,
            selector=selector,
            regime_thresholds=regime_thresholds,
            scoring_weights=scoring_weights,
            trading_mode=self.trading_mode,
        )

    def run_experiment(self, config: ExperimentConfig, m15_df: pd.DataFrame) -> ExperimentResult:
        """Executes a single research experiment deterministically via BacktestEngine."""
        if self.trading_mode == TradingMode.LIVE:
            raise RuntimeError("Safety violation: Strategy Lab cannot run in LIVE mode.")

        dataset_period = {
            "start": str(m15_df.index[0]) if m15_df is not None and len(m15_df) > 0 else "",
            "end": str(m15_df.index[-1]) if m15_df is not None and len(m15_df) > 0 else "",
            "bars": len(m15_df) if m15_df is not None else 0,
        }

        try:
            engine = self._build_engine_for_config(config)
            bt_res = engine.run(m15_df)
            result = ExperimentResult(
                experiment_id=config.experiment_id,
                status="COMPLETED",
                error=None,
                dataset_period=dataset_period,
                metrics=bt_res.metrics or {},
                benchmark=bt_res.benchmark or {},
                reproducibility_hash=bt_res.reproducibility_hash or "",
                config=config.to_dict(),
            )
        except RuntimeError as e:
            if "Safety violation" in str(e):
                raise
            result = ExperimentResult(
                experiment_id=config.experiment_id,
                status="FAILED",
                error=str(e),
                dataset_period=dataset_period,
                metrics={},
                benchmark={},
                reproducibility_hash="",
                config=config.to_dict(),
            )
        except Exception as exc:
            logger.exception("Experiment %s failed during execution", config.experiment_id)
            result = ExperimentResult(
                experiment_id=config.experiment_id,
                status="FAILED",
                error=str(exc),
                dataset_period=dataset_period,
                metrics={},
                benchmark={},
                reproducibility_hash="",
                config=config.to_dict(),
            )

        if self.store is not None:
            self.store.save_experiment(result)

        return result

    def run_grid_search(
        self,
        base_config: ExperimentConfig,
        grid: ParameterGrid,
        m15_df: pd.DataFrame,
    ) -> list[ExperimentResult]:
        """Runs a deterministic parameter grid search, bounded by safety limits."""
        # Will raise ComputationalLimitExceededError if combinations > max_combinations
        combinations = grid.generate_combinations()

        results: list[ExperimentResult] = []
        for combo in combinations:
            # Build child config with merged parameters
            merged_params = dict(base_config.parameters or {})
            merged_params.update(combo)

            child_config = ExperimentConfig(
                strategy_name=base_config.strategy_name,
                strategy_version=base_config.strategy_version,
                enabled=base_config.enabled,
                parameters=merged_params,
                timeframe=base_config.timeframe,
                regime_thresholds=base_config.regime_thresholds,
                scoring_weights=base_config.scoring_weights,
                risk_settings=base_config.risk_settings,
                execution_costs=base_config.execution_costs,
                backtest_config=base_config.backtest_config,
                dataset_identity=base_config.dataset_identity,
                random_seed=base_config.random_seed,
            )
            res = self.run_experiment(child_config, m15_df)
            results.append(res)

        return results

    def run_in_sample_out_of_sample(
        self,
        config: ExperimentConfig,
        m15_df: pd.DataFrame,
        train_frac: float = 0.7,
    ) -> OverfittingReport:
        """Executes chronological In-Sample vs Out-of-Sample analysis without lookahead or shuffling."""
        def factory():
            return self._build_engine_for_config(config)

        splits = run_in_sample_out_of_sample(factory, m15_df, train_frac=train_frac)
        return compute_overfitting_analysis(splits["IN_SAMPLE"], splits["OUT_OF_SAMPLE"])

    def run_walk_forward(
        self,
        config: ExperimentConfig,
        m15_df: pd.DataFrame,
        n_folds: int = 3,
    ) -> list[SplitResult]:
        """Executes sequential, non-overlapping walk-forward folds without lookahead or shuffling."""
        def factory():
            return self._build_engine_for_config(config)

        return walk_forward_folds(factory, m15_df, n_folds=n_folds)

    def compare_strategies(
        self,
        m15_df: pd.DataFrame,
        strategy_params: Optional[dict[str, dict]] = None,
        strategy_names: tuple[str, ...] = ALL_STRATEGY_NAMES,
    ) -> dict[str, StrategyComparisonEntry]:
        """Runs side-by-side strategy comparison over the identical dataset."""
        return compare_strategies(
            symbol_spec=self.symbol_spec,
            risk_settings=self.risk_settings,
            m15_df=m15_df,
            strategy_names=strategy_names,
            config=self.default_backtest_config,
            strategy_params=strategy_params,
        )
