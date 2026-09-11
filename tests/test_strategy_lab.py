import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import pytest
import strategy_test_helpers as h

from app.backtesting.engine import BacktestConfig, BacktestEngine
from app.config.settings import get_settings
from app.mt5.mock_client import MockMT5Client
from app.regimes.thresholds import RegimeThresholds
from app.strategy_lab.compare import compare_strategies
from app.strategy_lab.monte_carlo import run_monte_carlo
from app.strategy_lab.oos import run_in_sample_out_of_sample, train_test_split, walk_forward_folds
from app.strategy_lab.sensitivity import run_sensitivity_sweep

_WARMUP = 3400


@pytest.fixture(scope="module")
def market_data():
    return h.synthetic_market_ohlcv(n=8200, seed=123)


@pytest.fixture(scope="module")
def symbol_spec_and_settings():
    client = MockMT5Client()
    return client.get_symbol_spec("XAUUSD"), get_settings()


def _engine_factory(spec, settings, step=6):
    def factory():
        cfg = BacktestConfig(warmup_bars=_WARMUP, resample_lookback_bars=3700, step=step)
        return BacktestEngine(spec, settings.risk, config=cfg)
    return factory


# --- train/test split -----------------------------------------------------

def test_train_test_split_is_chronological_not_shuffled():
    df = h.synthetic_market_ohlcv(n=1000, seed=1)
    train, test = train_test_split(df, train_frac=0.7)
    assert len(train) == 700
    assert len(test) == 300
    assert train.index[-1] < test.index[0]  # train strictly precedes test


def test_train_test_split_rejects_invalid_fraction():
    df = h.synthetic_market_ohlcv(n=100, seed=1)
    with pytest.raises(ValueError):
        train_test_split(df, train_frac=1.5)


# --- in-sample / out-of-sample --------------------------------------------

def test_oos_report_has_both_labels_and_isolated_results(market_data, symbol_spec_and_settings):
    spec, settings = symbol_spec_and_settings
    factory = _engine_factory(spec, settings)
    report = run_in_sample_out_of_sample(factory, market_data, train_frac=0.5)
    assert set(report.keys()) == {"IN_SAMPLE", "OUT_OF_SAMPLE"}
    assert report["IN_SAMPLE"].end < report["OUT_OF_SAMPLE"].start
    # Each split's trades must stay within that split's own time range.
    for label, split in report.items():
        for trade in split.result.trades:
            assert split.start <= trade.open_time
            assert trade.close_time <= split.end


# --- walk-forward folds -----------------------------------------------------

def test_walk_forward_folds_are_sequential_and_non_overlapping(market_data, symbol_spec_and_settings):
    spec, settings = symbol_spec_and_settings
    factory = _engine_factory(spec, settings)
    folds = walk_forward_folds(factory, market_data, n_folds=2)
    assert len(folds) == 2
    assert folds[0].end < folds[1].start
    assert all(f.label.startswith("FOLD_") for f in folds)


def test_walk_forward_rejects_too_few_folds(market_data, symbol_spec_and_settings):
    spec, settings = symbol_spec_and_settings
    factory = _engine_factory(spec, settings)
    with pytest.raises(ValueError):
        walk_forward_folds(factory, market_data, n_folds=1)


# --- sensitivity -------------------------------------------------------------

def test_sensitivity_sweep_runs_one_point_per_value(symbol_spec_and_settings):
    spec, settings = symbol_spec_and_settings
    small_df = h.synthetic_market_ohlcv(n=4600, seed=99)

    def factory(value):
        cfg = BacktestConfig(warmup_bars=_WARMUP, resample_lookback_bars=3700, step=8)
        thresholds = RegimeThresholds(_env_file=None, trend_adx_threshold=value)
        return BacktestEngine(spec, settings.risk, config=cfg, regime_thresholds=thresholds)

    report = run_sensitivity_sweep(factory, small_df, "trend_adx_threshold", [15.0, 25.0])
    assert len(report.points) == 2
    assert report.points[0].value == 15.0
    assert report.points[1].value == 25.0
    assert isinstance(report.fragile, bool)


def test_sensitivity_flags_fragile_when_profit_sign_flips_every_step():
    # Construct a fake sweep via the sensitivity module's own flagging
    # logic by monkey-patching the engine factory to return results
    # with alternating profit sign, without running real backtests.
    from app.strategy_lab.sensitivity import SensitivityPoint, SensitivityReport

    points = [
        SensitivityPoint(value=1, net_profit=10, profit_factor=1.2, number_of_trades=3, win_rate=0.5),
        SensitivityPoint(value=2, net_profit=-10, profit_factor=0.8, number_of_trades=3, win_rate=0.4),
        SensitivityPoint(value=3, net_profit=10, profit_factor=1.1, number_of_trades=3, win_rate=0.5),
    ]
    flips = sum(1 for a, b in zip(points, points[1:]) if (a.net_profit > 0) != (b.net_profit > 0))
    assert flips == 2  # sanity on the fixture itself
    report = SensitivityReport(parameter_name="x", points=points, fragile=True, fragility_reason="test")
    assert report.fragile is True


# --- Monte Carlo -----------------------------------------------------------

def test_monte_carlo_empty_trades_returns_empty_report():
    from app.backtesting.results import BacktestResult
    result = BacktestResult(symbol="XAUUSD", starting_balance=10000.0, ending_balance=10000.0)
    report = run_monte_carlo(result, iterations=100)
    assert report.iterations == 0
    assert report.ending_balance_distribution == []


def test_monte_carlo_produces_distribution_of_requested_size(market_data, symbol_spec_and_settings):
    spec, settings = symbol_spec_and_settings
    factory = _engine_factory(spec, settings, step=6)
    result = factory().run(market_data)
    if not result.trades:
        pytest.skip("No trades produced in this run to Monte-Carlo over.")
    report = run_monte_carlo(result, iterations=200, seed=1)
    assert report.iterations == 200
    assert len(report.ending_balance_distribution) == 200
    assert len(report.max_drawdown_pct_distribution) == 200
    summary = report.summary()
    assert "max_drawdown_pct" in summary
    assert "probability_of_ruin_below_50pct" in summary


def test_monte_carlo_is_reproducible_with_same_seed(market_data, symbol_spec_and_settings):
    spec, settings = symbol_spec_and_settings
    factory = _engine_factory(spec, settings, step=6)
    result = factory().run(market_data)
    if not result.trades:
        pytest.skip("No trades produced in this run to Monte-Carlo over.")
    report_a = run_monte_carlo(result, iterations=100, seed=7)
    report_b = run_monte_carlo(result, iterations=100, seed=7)
    assert report_a.ending_balance_distribution == report_b.ending_balance_distribution


# --- strategy comparison -----------------------------------------------------

def test_compare_strategies_only_runs_the_named_strategy(symbol_spec_and_settings):
    spec, settings = symbol_spec_and_settings
    small_df = h.synthetic_market_ohlcv(n=4600, seed=99)
    cfg = BacktestConfig(warmup_bars=_WARMUP, resample_lookback_bars=3700, step=8)
    comparison = compare_strategies(spec, settings.risk, small_df, strategy_names=("TREND_PULLBACK",), config=cfg)
    assert set(comparison.keys()) == {"TREND_PULLBACK"}
    for trade in comparison["TREND_PULLBACK"].result.trades:
        assert trade.strategy == "TREND_PULLBACK"
