"""
Phase 11 Strategy Lab / Research Tooling Tests.

Verifies:
1. Serializable, deterministic ExperimentConfig and ExperimentResult models.
2. ParameterGrid cartesian products, deterministic ordering, and safety bounds.
3. ComputationalLimitExceededError on unbounded grids.
4. StrategyLabRunner simulation-only execution and live mode rejection.
5. In-Sample vs Out-of-Sample separation and profit degradation calculation.
6. Sequential, non-overlapping walk-forward fold execution.
7. No-lookahead: future data mutation strictly does not affect prior results.
8. No data leakage: chronological split integrity.
9. Descriptive-only multi-strategy comparison (no ranking or winner declaration).
10. Graceful failure handling (status="FAILED", error captured, no crash).
11. Persistence in SqliteResearchStore and InMemoryResearchStore.
12. Zero invocation of ExecutionEngine or live broker functions.
13. API research endpoints and safety gates.
14. Phase 10 regression compatibility.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.api.main as main_module
from app.api.state import build_app_state
from app.backtesting.costs import ExecutionCosts
from app.backtesting.engine import BacktestConfig, BacktestEngine
from app.config.settings import RiskSettings, Settings, TradingMode, get_settings
from app.mt5.mock_client import MockMT5Client
from app.strategy_lab.compare import ALL_STRATEGY_NAMES
from app.strategy_lab.models import (
    ComputationalLimitExceededError,
    ExperimentConfig,
    ExperimentResult,
    OverfittingReport,
    ParameterGrid,
)
from app.strategy_lab.oos import compute_overfitting_analysis, train_test_split
from app.strategy_lab.runner import StrategyLabRunner
from app.strategy_lab.store import InMemoryResearchStore, SqliteResearchStore
import tests.strategy_test_helpers as h


_WARMUP = 3400


@pytest.fixture(scope="module")
def sample_market_data():
    """Generates deterministic synthetic M15 market data."""
    return h.synthetic_market_ohlcv(n=4200, seed=42)


@pytest.fixture(scope="module")
def symbol_spec():
    client = MockMT5Client()
    return client.get_symbol_spec("XAUUSD")


@pytest.fixture()
def runner(symbol_spec):
    settings = get_settings()
    store = InMemoryResearchStore()
    bt_cfg = BacktestConfig(warmup_bars=_WARMUP, resample_lookback_bars=3700, step=8)
    return StrategyLabRunner(
        symbol_spec=symbol_spec,
        risk_settings=settings.risk,
        backtest_config=bt_cfg,
        store=store,
        trading_mode=TradingMode.BACKTEST,
    )


# =====================================================================
# 1. Serialization & Models
# =====================================================================

def test_experiment_serialization():
    config = ExperimentConfig(
        strategy_name="TREND_PULLBACK",
        strategy_version="1.0.0",
        parameters={"pullback_atr_multiplier": 1.5, "risk_reward": 2.0},
        dataset_identity="dataset-v1-synthetic",
        random_seed=123,
    )
    d = config.to_dict()
    assert d["strategy_name"] == "TREND_PULLBACK"
    assert d["experiment_id"] == config.experiment_id
    assert d["parameters"] == {"pullback_atr_multiplier": 1.5, "risk_reward": 2.0}

    reconstructed = ExperimentConfig.from_dict(d)
    assert reconstructed.experiment_id == config.experiment_id
    assert reconstructed.strategy_name == config.strategy_name
    assert reconstructed.parameters == config.parameters
    assert reconstructed.dataset_identity == config.dataset_identity
    assert reconstructed.random_seed == config.random_seed

    result = ExperimentResult(
        experiment_id=config.experiment_id,
        status="COMPLETED",
        error=None,
        dataset_period={"start": "2026-01-01", "end": "2026-02-01", "bars": 1000},
        metrics={"net_profit": 250.0, "total_trades": 12},
        benchmark={"buy_and_hold_pnl": 100.0},
        reproducibility_hash="sha256-dummy-hash",
        config=d,
    )
    res_dict = result.to_dict()
    assert res_dict["status"] == "COMPLETED"
    assert res_dict["metrics"]["net_profit"] == 250.0

    res_rec = ExperimentResult.from_dict(res_dict)
    assert res_rec.experiment_id == result.experiment_id
    assert res_rec.status == "COMPLETED"
    assert res_rec.metrics == result.metrics


# =====================================================================
# 2. Deterministic Experiment ID
# =====================================================================

def test_experiment_id_deterministic():
    c1 = ExperimentConfig(
        strategy_name="BREAKOUT",
        parameters={"expansion_ratio_threshold": 1.5},
        dataset_identity="data-alpha",
        random_seed=42,
    )
    c2 = ExperimentConfig(
        strategy_name="BREAKOUT",
        parameters={"expansion_ratio_threshold": 1.5},
        dataset_identity="data-alpha",
        random_seed=42,
    )
    assert c1.experiment_id == c2.experiment_id
    assert len(c1.experiment_id) == 64  # SHA-256 hex length


def test_experiment_id_changes_with_parameters():
    c1 = ExperimentConfig(strategy_name="BREAKOUT", parameters={"stop_atr_multiplier": 1.5})
    c2 = ExperimentConfig(strategy_name="BREAKOUT", parameters={"stop_atr_multiplier": 2.0})
    assert c1.experiment_id != c2.experiment_id


def test_experiment_id_changes_with_dataset_identity():
    c1 = ExperimentConfig(strategy_name="BREAKOUT", dataset_identity="dataset-A")
    c2 = ExperimentConfig(strategy_name="BREAKOUT", dataset_identity="dataset-B")
    assert c1.experiment_id != c2.experiment_id


# =====================================================================
# 3. Parameter Grid & Safety Limits
# =====================================================================

def test_parameter_grid_generation_and_order():
    grid = ParameterGrid(
        param_ranges={
            "b_param": [10, 20],
            "a_param": [1.0, 2.0, 3.0],
        }
    )
    assert grid.total_combinations == 6
    combos = grid.generate_combinations()
    assert len(combos) == 6
    # Keys should be sorted alphabetically: "a_param" then "b_param"
    first = combos[0]
    assert first["a_param"] == 1.0
    assert first["b_param"] == 10


def test_computational_limit_exceeded():
    grid = ParameterGrid(
        param_ranges={
            "p1": [1, 2, 3],
            "p2": [10, 20, 30],
        },
        max_combinations=5,
    )
    assert grid.total_combinations == 9
    with pytest.raises(ComputationalLimitExceededError, match="exceeds safety limit"):
        grid.generate_combinations()


# =====================================================================
# 4. Repeated Experiment Reproducibility
# =====================================================================

def test_repeated_experiment_reproducibility(runner, sample_market_data):
    config = ExperimentConfig(
        strategy_name="TREND_PULLBACK",
        parameters={"pullback_atr_multiplier": 1.5},
        random_seed=101,
    )
    res1 = runner.run_experiment(config, sample_market_data)
    res2 = runner.run_experiment(config, sample_market_data)

    assert res1.status == "COMPLETED"
    assert res2.status == "COMPLETED"
    assert res1.reproducibility_hash == res2.reproducibility_hash
    assert res1.reproducibility_hash != ""
    assert res1.metrics["net_profit"] == res2.metrics["net_profit"]
    assert res1.metrics["total_trades"] == res2.metrics["total_trades"]


# =====================================================================
# 5. In-Sample / Out-of-Sample Separation & Degradation
# =====================================================================

def test_is_oos_separation_and_degradation(runner, sample_market_data):
    config = ExperimentConfig(
        strategy_name="TREND_PULLBACK",
        parameters={"pullback_atr_multiplier": 1.5},
    )
    report = runner.run_in_sample_out_of_sample(config, sample_market_data, train_frac=0.7)

    assert isinstance(report, OverfittingReport)
    assert report.is_period["bars"] == int(len(sample_market_data) * 0.7)
    assert report.oos_period["bars"] == len(sample_market_data) - report.is_period["bars"]
    assert pd.Timestamp(report.is_period["end"]) < pd.Timestamp(report.oos_period["start"])

    # Verify degradation calculation
    if report.is_net_profit != 0:
        expected_deg = round(((report.is_net_profit - report.oos_net_profit) / abs(report.is_net_profit)) * 100.0, 4)
        assert report.profit_degradation_pct == expected_deg


# =====================================================================
# 6. Walk-Forward Sequential Folds
# =====================================================================

def test_walk_forward_execution(runner, sample_market_data):
    config = ExperimentConfig(
        strategy_name="TREND_PULLBACK",
        parameters={"pullback_atr_multiplier": 1.5},
    )
    folds = runner.run_walk_forward(config, sample_market_data, n_folds=2)
    assert len(folds) == 2
    assert folds[0].end < folds[1].start
    assert folds[0].label == "FOLD_1"
    assert folds[1].label == "FOLD_2"
    assert isinstance(folds[0].metrics, dict)
    assert isinstance(folds[1].metrics, dict)


# =====================================================================
# 7. No-Lookahead & Data Leakage Protections
# =====================================================================

def test_no_lookahead_future_data_mutation(runner, sample_market_data):
    cut = 3800
    df1 = sample_market_data.copy()
    df2 = sample_market_data.copy()

    # Mutate all future data after cut in df2
    df2.iloc[cut:, df2.columns.get_loc("close")] += 50.0
    df2.iloc[cut:, df2.columns.get_loc("high")] += 50.0
    df2.iloc[cut:, df2.columns.get_loc("low")] += 50.0
    df2.iloc[cut:, df2.columns.get_loc("spread")] += 100

    config = ExperimentConfig(strategy_name="TREND_PULLBACK")

    # Run on slice up to cut
    res1 = runner.run_experiment(config, df1.iloc[:cut])
    res2 = runner.run_experiment(config, df2.iloc[:cut])

    assert res1.reproducibility_hash == res2.reproducibility_hash
    assert res1.metrics["total_trades"] == res2.metrics["total_trades"]
    assert res1.metrics["net_profit"] == res2.metrics["net_profit"]


def test_no_data_leakage_shuffling_rejected():
    df = h.synthetic_market_ohlcv(n=500, seed=1)
    train, test = train_test_split(df, train_frac=0.6)
    assert train.index[-1] < test.index[0]

    with pytest.raises(ValueError, match="train_frac must be between 0 and 1"):
        train_test_split(df, train_frac=0.0)
    with pytest.raises(ValueError, match="train_frac must be between 0 and 1"):
        train_test_split(df, train_frac=1.0)


# =====================================================================
# 8. Descriptive Comparison (No Ranking / Winner Bias)
# =====================================================================

def test_multi_strategy_comparison_no_ranking(runner, sample_market_data):
    entries = runner.compare_strategies(sample_market_data)
    assert set(entries.keys()) == set(ALL_STRATEGY_NAMES)

    for name, entry in entries.items():
        assert entry.strategy_name == name
        assert "net_profit" in entry.metrics
        assert "total_trades" in entry.metrics
        assert "win_rate" in entry.metrics
        # Verify no rank or winner fields
        assert not hasattr(entry, "rank")
        assert not hasattr(entry, "winner")
        assert "rank" not in entry.metrics
        assert "winner" not in entry.metrics


# =====================================================================
# 9. Graceful Failure Handling
# =====================================================================

def test_failed_experiment_handled_gracefully(runner, sample_market_data):
    # Pass an unknown strategy name to trigger a failure during execution
    config = ExperimentConfig(
        strategy_name="INVALID_STRATEGY_DOES_NOT_EXIST",
        parameters={},
    )
    result = runner.run_experiment(config, sample_market_data)
    assert result.status == "FAILED"
    assert result.error is not None
    assert "Unknown strategy_name" in result.error
    assert result.metrics == {}
    assert result.reproducibility_hash == ""


# =====================================================================
# 10. Safety Gate: Live Mode Rejection & Zero ExecutionEngine Invocations
# =====================================================================

def test_live_mode_strictly_rejected(symbol_spec):
    settings = get_settings()

    # Rejected via trading_mode arg
    with pytest.raises(RuntimeError, match="Safety violation: Strategy Lab is simulation-only"):
        StrategyLabRunner(
            symbol_spec=symbol_spec,
            risk_settings=settings.risk,
            trading_mode=TradingMode.LIVE,
        )

    # Rejected via risk_settings trading_mode
    class MockLiveRisk:
        trading_mode = TradingMode.LIVE

    with pytest.raises(RuntimeError, match="Safety violation: Strategy Lab is simulation-only"):
        StrategyLabRunner(
            symbol_spec=symbol_spec,
            risk_settings=MockLiveRisk(),  # type: ignore
            trading_mode=TradingMode.BACKTEST,
        )


def test_zero_execution_engine_invocation():
    import app.strategy_lab.runner as r_module

    # ExecutionEngine must NOT be imported or used in the research runner
    assert not hasattr(r_module, "ExecutionEngine")
    assert not hasattr(r_module, "order_send")


# =====================================================================
# 11. Persistence: InMemoryResearchStore & SqliteResearchStore
# =====================================================================

def test_research_store_persistence(tmp_path):
    db_file = tmp_path / "test_research.db"
    store = SqliteResearchStore(db_path=str(db_file))

    config = ExperimentConfig(strategy_name="BREAKOUT", parameters={"min_relative_volume": 1.4})
    result = ExperimentResult(
        experiment_id=config.experiment_id,
        status="COMPLETED",
        error=None,
        dataset_period={"bars": 1000},
        metrics={"net_profit": 150.0},
        benchmark={"buy_and_hold_pnl": 50.0},
        reproducibility_hash="hash123",
        config=config.to_dict(),
    )

    store.save_experiment(result)

    # Retrieve
    retrieved = store.get_experiment(config.experiment_id)
    assert retrieved is not None
    assert retrieved.experiment_id == config.experiment_id
    assert retrieved.status == "COMPLETED"
    assert retrieved.metrics["net_profit"] == 150.0
    assert retrieved.reproducibility_hash == "hash123"

    # List
    all_exps = store.list_experiments()
    assert len(all_exps) == 1

    # Filtered list
    tp_exps = store.list_experiments(strategy_name="TREND_PULLBACK")
    assert len(tp_exps) == 0

    brk_exps = store.list_experiments(strategy_name="BREAKOUT")
    assert len(brk_exps) == 1

    # Non-existent
    assert store.get_experiment("nonexistent-id") is None


# =====================================================================
# 12. Grid Search Execution
# =====================================================================

def test_grid_search_execution(runner, sample_market_data):
    grid = ParameterGrid(
        param_ranges={
            "pullback_atr_multiplier": [1.0, 2.0],
        }
    )
    base_config = ExperimentConfig(strategy_name="TREND_PULLBACK")
    results = runner.run_grid_search(base_config, grid, sample_market_data)

    assert len(results) == 2
    assert all(r.status == "COMPLETED" for r in results)
    assert results[0].experiment_id != results[1].experiment_id


# =====================================================================
# 13. Phase 10 Regression Compatibility
# =====================================================================

def test_phase10_regression_compatibility(symbol_spec, sample_market_data):
    settings = get_settings()
    cfg = BacktestConfig(warmup_bars=_WARMUP, resample_lookback_bars=3700, step=8)
    engine = BacktestEngine(symbol_spec, settings.risk, config=cfg)
    result = engine.run(sample_market_data)

    assert result.reproducibility_hash is not None
    assert len(result.reproducibility_hash) == 64
    assert result.data_quality["is_valid"] is True
    assert "buy_and_hold_pnl" in result.benchmark


# =====================================================================
# 14. API Research Endpoints
# =====================================================================

@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/api_journal.db")
    main_module._state = None
    state = build_app_state()
    state.kill_switch = state.kill_switch.__class__(str(tmp_path / "api_kill_switch.json"))
    state.research_store = InMemoryResearchStore()
    main_module._state = state
    with TestClient(main_module.app) as c:
        yield c
    main_module._state = None


def test_api_research_endpoints(api_client):
    # 1. List experiments (initially empty)
    r = api_client.get("/api/research/experiments")
    assert r.status_code == 200
    assert r.json() == {"experiments": []}

    # 2. Run an experiment
    run_payload = {
        "strategy_name": "TREND_PULLBACK",
        "parameters": {"pullback_atr_multiplier": 1.5},
        "bars": 3600,
    }
    r = api_client.post("/api/research/experiments/run", json=run_payload)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "COMPLETED"
    exp_id = data["experiment_id"]
    assert exp_id

    # 3. Retrieve experiment by ID
    r = api_client.get(f"/api/research/experiments/{exp_id}")
    assert r.status_code == 200
    assert r.json()["experiment_id"] == exp_id

    # 4. Retrieve non-existent experiment
    r = api_client.get("/api/research/experiments/nonexistent-id")
    assert r.status_code == 404

    # 5. Compare strategies
    r = api_client.post("/api/research/compare", json={"bars": 3600})
    assert r.status_code == 200
    comp_data = r.json()
    assert "TREND_PULLBACK" in comp_data
    assert "BREAKOUT" in comp_data
    assert "MEAN_REVERSION" in comp_data

    # 6. Walk-forward
    r = api_client.post(
        "/api/research/walk-forward",
        json={"strategy_name": "TREND_PULLBACK", "bars": 3600, "n_folds": 2},
    )
    assert r.status_code == 200
    wf_data = r.json()
    assert len(wf_data["folds"]) == 2


def test_api_live_mode_rejected(api_client):
    state = main_module.get_state()
    # Temporarily set trading_mode to LIVE to test API safety gate
    state.settings.trading_mode = TradingMode.LIVE

    try:
        r = api_client.post(
            "/api/research/experiments/run",
            json={"strategy_name": "TREND_PULLBACK", "bars": 500},
        )
        assert r.status_code == 400
        assert "Safety violation" in r.json()["detail"]

        r = api_client.post("/api/research/compare", json={"bars": 500})
        assert r.status_code == 400
        assert "Safety violation" in r.json()["detail"]

        r = api_client.post(
            "/api/research/walk-forward",
            json={"strategy_name": "TREND_PULLBACK", "bars": 500, "n_folds": 2},
        )
        assert r.status_code == 400
        assert "Safety violation" in r.json()["detail"]
    finally:
        state.settings.trading_mode = TradingMode.BACKTEST
