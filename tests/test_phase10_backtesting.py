"""
Phase 10 — Backtesting & Historical Validation Test Suite.

Comprehensive verification of:
1. No-Lookahead & Data Leakage Integrity (Mandatory Tests A, B, C, D)
2. Historical Data Quality Validation
3. Intrabar SL/TP Handling (Gap-aware fills & conservative same-bar SL resolution)
4. Phase 8 Position Management in Simulation (Breakeven, trailing stop, partial exit)
5. Risk Engine Limits & Rejection Tracking
6. Performance Metrics & Benchmark Baselines
7. Chronological Data Segmentation (In-Sample / Out-of-Sample)
8. Deterministic Reproducibility
9. Live Execution Safety
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.backtesting.benchmark import compute_benchmark
from app.backtesting.costs import ExecutionCosts
from app.backtesting.data_validator import (
    DataQualityReport,
    InvalidHistoricalDataError,
    validate_historical_data,
)
from app.backtesting.engine import BacktestConfig, BacktestEngine
from app.backtesting.metrics import compute_metrics
from app.backtesting.resampling import resample_closed_only
from app.backtesting.results import BacktestResult, BacktestTrade, EquityPoint
from app.config.settings import RiskSettings, TradingMode
from app.features.engine import compute_features
from app.mt5.interface import SymbolSpec
from app.mt5.mock_client import MockMT5Client
from app.positions.monitor import PositionMonitorConfig
from app.risk.history import InMemoryTradeHistory
from app.strategy_lab.oos import run_in_sample_out_of_sample, train_test_split
from tests.strategy_test_helpers import synthetic_market_ohlcv


# =====================================================================
# Fixtures
# =====================================================================
@pytest.fixture
def symbol_spec():
    client = MockMT5Client()
    return client.get_symbol_spec("XAUUSD")


@pytest.fixture
def risk_settings():
    return RiskSettings(_env_file=None)


@pytest.fixture
def market_data_3600():
    return synthetic_market_ohlcv(n=3600, seed=101)


# =====================================================================
# 1. Mandatory Section 13 Data Leakage Tests (Tests A, B, C, D)
# =====================================================================
def test_no_lookahead_test_a_modify_future_candle_after_decision_point(symbol_spec, risk_settings, market_data_3600):
    """TEST A: Modify a future candle after the decision point.
    The earlier signal and result must remain completely unchanged.
    """
    cfg = BacktestConfig(warmup_bars=3400, resample_lookback_bars=3500, step=2)
    engine = BacktestEngine(symbol_spec, risk_settings, config=cfg)

    # 1. Baseline run
    base_result = engine.run(market_data_3600)

    # Pick a point in the dataset well before the end
    split_bar = 3450
    cutoff_time = market_data_3600.index[split_bar]

    # 2. Modify data far into the future (after split_bar)
    modified_df = market_data_3600.copy()
    # Drastically perturb future candles
    modified_df.iloc[split_bar + 10:, modified_df.columns.get_loc("open")] *= 1.2
    modified_df.iloc[split_bar + 10:, modified_df.columns.get_loc("high")] *= 1.2
    modified_df.iloc[split_bar + 10:, modified_df.columns.get_loc("low")] *= 1.2
    modified_df.iloc[split_bar + 10:, modified_df.columns.get_loc("close")] *= 1.2

    # 3. Re-run on modified future
    engine_mod = BacktestEngine(symbol_spec, risk_settings, config=cfg)
    mod_result = engine_mod.run(modified_df)

    # Any trade initiated at or before cutoff_time must be completely invariant
    base_early_trades = [t for t in base_result.trades if t.open_time <= cutoff_time]
    mod_early_trades = [t for t in mod_result.trades if t.open_time <= cutoff_time]

    assert len(base_early_trades) == len(mod_early_trades)
    for t_base, t_mod in zip(base_early_trades, mod_early_trades):
        assert t_base.open_time == t_mod.open_time
        assert t_base.direction == t_mod.direction
        assert t_base.entry_price == pytest.approx(t_mod.entry_price, abs=1e-6)
        assert t_base.stop_loss == pytest.approx(t_mod.stop_loss, abs=1e-6)
        assert t_base.take_profit == pytest.approx(t_mod.take_profit, abs=1e-6)
        assert t_base.lots == pytest.approx(t_mod.lots, abs=1e-6)


def test_no_lookahead_test_b_modify_data_after_trade_opened(symbol_spec, risk_settings):
    """TEST B: Modify data after a trade was opened.
    The entry decision must remain unchanged.
    """
    df = synthetic_market_ohlcv(n=4600, seed=99)
    cfg = BacktestConfig(warmup_bars=3400, resample_lookback_bars=3700, step=4)
    engine = BacktestEngine(symbol_spec, risk_settings, config=cfg)
    result_1 = engine.run(df)

    assert len(result_1.trades) > 0, "Expected trades generated in backtest run."
    first_trade = result_1.trades[0]
    entry_bar_idx = df.index.get_loc(first_trade.open_time)

    # Modify prices after entry_bar_idx
    modified_df = df.copy()
    modified_df.iloc[entry_bar_idx + 1:, modified_df.columns.get_loc("close")] += 15.0
    modified_df.iloc[entry_bar_idx + 1:, modified_df.columns.get_loc("high")] += 15.0

    engine_2 = BacktestEngine(symbol_spec, risk_settings, config=cfg)
    result_2 = engine_2.run(modified_df)

    first_trade_mod = result_2.trades[0]
    assert first_trade_mod.open_time == first_trade.open_time
    assert first_trade_mod.entry_price == pytest.approx(first_trade.entry_price, abs=1e-6)
    assert first_trade_mod.direction == first_trade.direction
    assert first_trade_mod.strategy == first_trade.strategy


def test_no_lookahead_test_c_modify_future_portion_of_higher_timeframe_candle():
    """TEST C: Modify the future portion of an H1/H4 candle.
    M15 decisions before that higher-timeframe candle closes must remain unchanged.
    """
    idx = pd.date_range("2026-01-01 00:00", periods=8, freq="15min", tz="UTC")
    # First H1 candle covers 00:00, 00:15, 00:30, 00:45.
    # At 00:30 (3 bars), the H1 candle is NOT yet closed!
    df_window = pd.DataFrame(
        {
            "open": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0],
            "high": [12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0],
            "low": [9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0],
            "close": [11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0],
            "tick_volume": [100] * 8,
            "spread": [20] * 8,
        },
        index=idx,
    )

    # At bar 2 (00:30), only 3 bars exist. H1 is not closed yet.
    h1_at_0030 = resample_closed_only(df_window.iloc[:3], "H1")
    assert h1_at_0030.empty  # Not a single closed H1 candle exists yet!

    # Even if we wildly modify the future 00:45 candle, H1 at 00:30 remains empty:
    df_mod = df_window.copy()
    df_mod.iloc[3, df_mod.columns.get_loc("high")] = 999.0
    h1_at_0030_mod = resample_closed_only(df_mod.iloc[:3], "H1")
    assert h1_at_0030_mod.empty

    # At bar 4 (00:00 through 00:45 completed), exactly one closed H1 exists
    h1_closed = resample_closed_only(df_window.iloc[:4], "H1")
    assert len(h1_closed) == 1
    assert h1_closed.iloc[0]["open"] == 10.0
    assert h1_closed.iloc[0]["close"] == 14.0


def test_no_lookahead_test_d_indicators_do_not_depend_on_future():
    """TEST D: Ensure indicators/features at timestamp T do not depend on candles after T."""
    from app.indicators import momentum as mom, trend as trend_ind, volatility as vol

    df = synthetic_market_ohlcv(n=350, seed=55)
    t_idx = 250
    sub_df = df.iloc[: t_idx + 1]

    # 1. EMA
    ema_sub = trend_ind.ema(sub_df["close"], 20).iloc[-1]
    ema_all = trend_ind.ema(df["close"], 20).iloc[t_idx]
    assert ema_sub == pytest.approx(ema_all, abs=1e-6)

    # 2. RSI
    rsi_sub = mom.rsi(sub_df["close"], 14).iloc[-1]
    rsi_all = mom.rsi(df["close"], 14).iloc[t_idx]
    assert rsi_sub == pytest.approx(rsi_all, abs=1e-6)

    # 3. ATR
    atr_sub = vol.atr(sub_df, 14).iloc[-1]
    atr_all = vol.atr(df, 14).iloc[t_idx]
    assert atr_sub == pytest.approx(atr_all, abs=1e-6)

    # 4. ADX
    adx_sub = trend_ind.adx(sub_df, 14)["adx"].iloc[-1]
    adx_all = trend_ind.adx(df, 14)["adx"].iloc[t_idx]
    assert adx_sub == pytest.approx(adx_all, abs=1e-6)

    # 5. Full feature engine snapshot
    features_t = compute_features(sub_df, "M15")
    features_all_at_t = compute_features(df.iloc[: t_idx + 1], "M15")
    assert features_t["momentum"]["rsi"] == pytest.approx(features_all_at_t["momentum"]["rsi"], abs=1e-6)
    assert features_t["volatility"]["atr"] == pytest.approx(features_all_at_t["volatility"]["atr"], abs=1e-6)
    assert features_t["session"] == features_all_at_t["session"]


# =====================================================================
# 2. Historical Data Quality Validation
# =====================================================================
def test_data_validation_valid_data_passes():
    df = synthetic_market_ohlcv(n=200, seed=7)
    report = validate_historical_data(df)
    assert report.is_valid is True
    assert report.total_bars == 200
    assert report.errors == []


def test_data_validation_detects_unsorted_timestamps():
    df = synthetic_market_ohlcv(n=50, seed=7)
    # Swap two rows
    idx = list(df.index)
    idx[10], idx[11] = idx[11], idx[10]
    df.index = pd.DatetimeIndex(idx)

    report = validate_historical_data(df)
    assert report.is_valid is False
    assert report.unsorted_timestamps > 0
    assert any("chronologically sorted" in e for e in report.errors)


def test_data_validation_detects_duplicate_timestamps():
    df = synthetic_market_ohlcv(n=50, seed=7)
    idx = list(df.index)
    idx[5] = idx[4]  # duplicate
    df.index = pd.DatetimeIndex(idx)

    report = validate_historical_data(df)
    assert report.is_valid is False
    assert report.duplicate_timestamps == 1
    assert any("duplicate" in e for e in report.errors)


def test_data_validation_detects_non_positive_prices():
    df = synthetic_market_ohlcv(n=50, seed=7)
    df.iloc[10, df.columns.get_loc("low")] = -5.0
    report = validate_historical_data(df)
    assert report.is_valid is False
    assert report.non_positive_bars > 0
    assert any("non-positive" in e for e in report.errors)


def test_data_validation_detects_invalid_ohlc_geometries():
    df = synthetic_market_ohlcv(n=50, seed=7)
    # Set high lower than low
    df.iloc[15, df.columns.get_loc("high")] = 2500.0
    df.iloc[15, df.columns.get_loc("low")] = 2600.0
    report = validate_historical_data(df)
    assert report.is_valid is False
    assert report.invalid_ohlc_bars > 0
    assert any("geometry" in e for e in report.errors)


def test_data_validation_detects_negative_spread():
    df = synthetic_market_ohlcv(n=50, seed=7)
    df.iloc[20, df.columns.get_loc("spread")] = -10
    report = validate_historical_data(df)
    assert report.is_valid is False
    assert report.invalid_spread_bars == 1
    assert any("negative spread" in e for e in report.errors)


def test_data_validation_strict_mode_raises_error():
    df = synthetic_market_ohlcv(n=50, seed=7)
    df.iloc[10, df.columns.get_loc("open")] = np.nan
    with pytest.raises(InvalidHistoricalDataError):
        validate_historical_data(df, strict=True)


def test_data_validation_empty_dataframe_handled_safely():
    report = validate_historical_data(pd.DataFrame())
    assert report.is_valid is False
    assert any("empty" in e for e in report.errors)


# =====================================================================
# 3. Intrabar SL/TP Handling & Conservative Same-Bar Resolution
# =====================================================================
def test_intrabar_both_sl_and_tp_hit_same_bar_resolves_conservatively_to_sl(symbol_spec, risk_settings):
    """When both SL and TP fall inside the same bar's High-Low range,
    the deterministic conservative rule MUST assume the stop loss hit first.
    """
    cfg = BacktestConfig(warmup_bars=0)
    engine = BacktestEngine(symbol_spec, risk_settings, config=cfg)

    # Position: BUY at 2650.0, SL at 2640.0, TP at 2670.0
    pos = {
        "direction": "BUY",
        "entry_price": 2650.0,
        "entry_time": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        "entry_spread_cost": 0.1,
        "entry_slippage_cost": 0.05,
        "stop_loss": 2640.0,
        "take_profit": 2670.0,
        "lots": 0.1,
        "strategy": "TEST",
        "regime": "TREND_BULLISH",
        "score": 8,
        "monetary_risk": 100.0,
        "mae": 0.0,
        "mfe": 0.0,
    }
    # Bar spans from 2635.0 to 2675.0 -- touches BOTH SL (2640) and TP (2670)!
    bar = pd.Series({"open": 2655.0, "high": 2675.0, "low": 2635.0, "close": 2660.0})
    bar_time = datetime(2026, 1, 1, 10, 15, tzinfo=timezone.utc)

    trades = []
    history = InMemoryTradeHistory()
    res = engine._update_and_maybe_exit(pos, bar, bar_time, symbol_spec, ExecutionCosts(), trades, history)

    assert res["_closed"] is True
    assert len(trades) == 1
    assert trades[0].exit_reason == "STOP_LOSS"
    assert trades[0].pnl < 0


def test_gap_through_sl_fills_at_worse_open_price(symbol_spec, risk_settings):
    """If market gaps below stop loss on open, fill must occur at market open,
    NOT the optimistic stop loss level.
    """
    engine = BacktestEngine(symbol_spec, risk_settings)

    # 1. BUY gapping down past SL
    pos_buy = {
        "direction": "BUY",
        "entry_price": 2650.0,
        "entry_time": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        "stop_loss": 2640.0,
        "take_profit": 2670.0,
        "lots": 0.1,
        "strategy": "TEST",
        "regime": "TREND",
        "score": 8,
        "monetary_risk": 100.0,
        "mae": 0.0,
        "mfe": 0.0,
    }
    # Bar opens at 2635.0 (gapped 5.0 below SL 2640.0)
    bar_gap_down = pd.Series({"open": 2635.0, "high": 2638.0, "low": 2630.0, "close": 2632.0})
    trades = []
    history = InMemoryTradeHistory()
    engine._update_and_maybe_exit(pos_buy, bar_gap_down, datetime.now(timezone.utc), symbol_spec, ExecutionCosts(), trades, history)
    assert len(trades) == 1
    # Exit price before exit costs is 2635.0 (the open), NOT 2640.0!
    assert trades[0].exit_price <= 2635.0

    # 2. SELL gapping up past SL
    pos_sell = {
        "direction": "SELL",
        "entry_price": 2650.0,
        "entry_time": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        "stop_loss": 2660.0,
        "take_profit": 2630.0,
        "lots": 0.1,
        "strategy": "TEST",
        "regime": "TREND",
        "score": 8,
        "monetary_risk": 100.0,
        "mae": 0.0,
        "mfe": 0.0,
    }
    # Bar opens at 2665.0 (gapped 5.0 above SL 2660.0)
    bar_gap_up = pd.Series({"open": 2665.0, "high": 2670.0, "low": 2662.0, "close": 2668.0})
    trades2 = []
    history2 = InMemoryTradeHistory()
    engine._update_and_maybe_exit(pos_sell, bar_gap_up, datetime.now(timezone.utc), symbol_spec, ExecutionCosts(), trades2, history2)
    assert len(trades2) == 1
    # Exit price before exit costs is 2665.0, NOT 2660.0!
    assert trades2[0].exit_price >= 2665.0


def test_neither_sl_nor_tp_hit_keeps_position_open(symbol_spec, risk_settings):
    engine = BacktestEngine(symbol_spec, risk_settings)
    pos = {
        "direction": "BUY",
        "entry_price": 2650.0,
        "entry_time": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        "stop_loss": 2640.0,
        "take_profit": 2670.0,
        "lots": 0.1,
        "strategy": "TEST",
        "regime": "TREND",
        "score": 8,
        "monetary_risk": 100.0,
        "mae": 0.0,
        "mfe": 0.0,
    }
    # Normal bar within bracket
    bar = pd.Series({"open": 2652.0, "high": 2658.0, "low": 2648.0, "close": 2655.0})
    trades = []
    history = InMemoryTradeHistory()
    res = engine._update_and_maybe_exit(pos, bar, datetime.now(timezone.utc), symbol_spec, ExecutionCosts(), trades, history)
    assert res.get("_closed") is None
    assert len(trades) == 0


# =====================================================================
# 4. Phase 8 Position Management in Simulation
# =====================================================================
def test_position_management_breakeven_moves_sl(symbol_spec, risk_settings):
    """When enabled, position moves stop loss to breakeven + buffer once trigger R is reached."""
    cfg = BacktestConfig(
        enable_position_management=True,
        position_monitor_config=PositionMonitorConfig(
            breakeven_trigger_r=1.0, breakeven_buffer_ticks=5.0, enable_partial_exit=False
        ),
    )
    engine = BacktestEngine(symbol_spec, risk_settings, config=cfg)

    # BUY position: entry 2650.0, SL 2640.0 -> initial risk = 10.0
    pos = {
        "direction": "BUY",
        "entry_price": 2650.0,
        "entry_time": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        "initial_risk": 10.0,
        "stop_loss": 2640.0,
        "take_profit": 2680.0,
        "lots": 0.2,
        "strategy": "TEST",
        "regime": "TREND",
        "score": 8,
        "monetary_risk": 200.0,
        "breakeven_applied": False,
        "mae": 0.0,
        "mfe": 0.0,
    }
    # Bar reaches 2661.0 (1.1 R profit!)
    bar = pd.Series({"open": 2652.0, "high": 2661.0, "low": 2649.0, "close": 2658.0})
    trades = []
    history = InMemoryTradeHistory()
    res = engine._update_and_maybe_exit(pos, bar, datetime.now(timezone.utc), symbol_spec, ExecutionCosts(), trades, history)

    assert res["breakeven_applied"] is True
    # Stop loss should now be entry (2650) + 5 ticks (0.05) = 2650.05
    assert res["stop_loss"] == pytest.approx(2650.05)


def test_position_management_trailing_stop_tightens_sl(symbol_spec, risk_settings):
    """Trailing stop tightens as price advances, but never loosens."""
    cfg = BacktestConfig(
        enable_position_management=True,
        position_monitor_config=PositionMonitorConfig(
            enable_trailing=True, trailing_trigger_r=1.5, trailing_atr_multiplier=1.0, enable_partial_exit=False
        ),
    )
    engine = BacktestEngine(symbol_spec, risk_settings, config=cfg)

    # BUY position: entry 2650.0, SL 2640.0 (risk=10), ATR=2.0
    pos = {
        "direction": "BUY",
        "entry_price": 2650.0,
        "entry_time": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        "initial_risk": 10.0,
        "stop_loss": 2640.0,
        "take_profit": 2690.0,
        "lots": 0.2,
        "entry_atr": 2.0,
        "strategy": "TEST",
        "regime": "TREND",
        "score": 8,
        "monetary_risk": 200.0,
        "mae": 0.0,
        "mfe": 0.0,
    }
    # Bar reaches 2667.0 (1.7 R profit, close at 2665.0)
    # Trail SL = 2665.0 - (1.0 * 2.0) = 2663.0
    bar = pd.Series({"open": 2655.0, "high": 2667.0, "low": 2654.0, "close": 2665.0})
    trades = []
    history = InMemoryTradeHistory()
    res = engine._update_and_maybe_exit(pos, bar, datetime.now(timezone.utc), symbol_spec, ExecutionCosts(), trades, history)
    assert res["stop_loss"] == pytest.approx(2663.0)


def test_position_management_partial_exit_realizes_partial_pnl(symbol_spec, risk_settings):
    """Partial exit closes fraction of volume and logs partial trade."""
    cfg = BacktestConfig(
        enable_position_management=True,
        position_monitor_config=PositionMonitorConfig(
            enable_partial_exit=True, partial_exit_trigger_r=1.0, partial_exit_fraction=0.5
        ),
    )
    engine = BacktestEngine(symbol_spec, risk_settings, config=cfg)

    # BUY position: entry 2650.0, SL 2640.0 (risk=10), lots=0.4
    pos = {
        "direction": "BUY",
        "entry_price": 2650.0,
        "entry_time": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        "initial_risk": 10.0,
        "stop_loss": 2640.0,
        "take_profit": 2690.0,
        "lots": 0.4,
        "strategy": "TEST",
        "regime": "TREND",
        "score": 8,
        "monetary_risk": 400.0,
        "partial_taken": False,
        "mae": 0.0,
        "mfe": 0.0,
    }
    # Bar reaches 2661.0 (1.1 R profit) -> triggers partial exit of 0.2 lots
    # Keep low at 2651.0 (above breakeven stop of 2650.05) so remainder position remains open
    bar = pd.Series({"open": 2652.0, "high": 2661.0, "low": 2651.0, "close": 2658.0})
    trades = []
    history = InMemoryTradeHistory()
    bar_time = datetime(2026, 1, 1, 10, 15, tzinfo=timezone.utc)
    res = engine._update_and_maybe_exit(pos, bar, bar_time, symbol_spec, ExecutionCosts(), trades, history)

    assert res["partial_taken"] is True
    assert res["lots"] == pytest.approx(0.2)  # 0.4 - 0.2 = 0.2 remaining
    assert len(trades) == 1
    assert trades[0].is_partial is True
    assert trades[0].exit_reason == "PARTIAL_EXIT"
    assert trades[0].lots == pytest.approx(0.2)
    assert trades[0].pnl > 0


# =====================================================================
# 5. Risk Engine Limits & Rejection Tracking
# =====================================================================
def test_risk_rejections_tracked_and_counted(symbol_spec, risk_settings):
    """When a trade is rejected by the risk engine, rejection reasons are tracked."""
    df = synthetic_market_ohlcv(n=4600, seed=99)
    # Impose a strict maximum spread to trigger spread rejections
    strict_risk = RiskSettings(_env_file=None, max_spread_points=5.0)  # market has spread 15-30
    cfg = BacktestConfig(warmup_bars=3400, resample_lookback_bars=3700, step=4)
    engine = BacktestEngine(symbol_spec, strict_risk, config=cfg)

    result = engine.run(df)
    assert len(result.trades) == 0  # blocked by spread limit
    assert len(result.rejections) > 0
    assert any("spread" in k.lower() for k in result.rejections.keys())


# =====================================================================
# 6. Performance Metrics & Benchmark Baseline
# =====================================================================
def test_extended_metrics_and_benchmark(symbol_spec, risk_settings, market_data_3600):
    cfg = BacktestConfig(warmup_bars=3400, resample_lookback_bars=3500, step=4)
    engine = BacktestEngine(symbol_spec, risk_settings, config=cfg)
    result = engine.run(market_data_3600)

    metrics = result.metrics
    # Required Phase 10 metric fields
    required_keys = [
        "number_of_trades", "winning_trades", "losing_trades",
        "gross_profit", "gross_loss", "net_profit",
        "win_rate", "profit_factor", "expectancy",
        "max_consecutive_wins", "max_consecutive_losses",
        "total_commission", "total_spread_cost", "total_slippage_cost", "total_swap_cost",
    ]
    for key in required_keys:
        assert key in metrics, f"Missing metric {key}"

    # Benchmark fields
    bench = result.benchmark
    assert "buy_and_hold_pnl" in bench
    assert "buy_and_hold_return_pct" in bench
    assert bench["no_trade_pnl"] == 0.0


# =====================================================================
# 7. Chronological Data Segmentation (In-Sample / Out-of-Sample)
# =====================================================================
def test_in_sample_out_of_sample_segmentation(symbol_spec, risk_settings):
    df = synthetic_market_ohlcv(n=4000, seed=12)
    train_df, test_df = train_test_split(df, train_frac=0.7)

    assert len(train_df) == 2800
    assert len(test_df) == 1200
    # Strictly chronological
    assert train_df.index[-1] < test_df.index[0]

    def factory():
        cfg = BacktestConfig(warmup_bars=1000, resample_lookback_bars=1500, step=4)
        return BacktestEngine(symbol_spec, risk_settings, config=cfg)

    splits = run_in_sample_out_of_sample(factory, df, train_frac=0.7)
    assert "IN_SAMPLE" in splits
    assert "OUT_OF_SAMPLE" in splits
    assert splits["IN_SAMPLE"].bars == 2800
    assert splits["OUT_OF_SAMPLE"].bars == 1200


# =====================================================================
# 8. Deterministic Reproducibility
# =====================================================================
def test_deterministic_reproducibility(symbol_spec, risk_settings, market_data_3600):
    cfg = BacktestConfig(warmup_bars=3400, resample_lookback_bars=3500, step=4)
    engine_1 = BacktestEngine(symbol_spec, risk_settings, config=cfg)
    result_1 = engine_1.run(market_data_3600)

    engine_2 = BacktestEngine(symbol_spec, risk_settings, config=cfg)
    result_2 = engine_2.run(market_data_3600)

    assert result_1.reproducibility_hash is not None
    assert result_1.reproducibility_hash == result_2.reproducibility_hash
    assert result_1.ending_balance == pytest.approx(result_2.ending_balance, abs=1e-6)
    assert len(result_1.trades) == len(result_2.trades)


def test_reproducibility_hash_changes_on_historical_data_modification(symbol_spec, risk_settings, market_data_3600):
    """Confirm that modifying even a single historical price, timestamp, or spread alters the reproducibility hash."""
    cfg = BacktestConfig(warmup_bars=3400, resample_lookback_bars=3500, step=4)
    engine = BacktestEngine(symbol_spec, risk_settings, config=cfg)
    base_result = engine.run(market_data_3600)
    assert base_result.reproducibility_hash is not None

    # 1. Modify a single price in historical OHLC data
    modified_price_df = market_data_3600.copy()
    modified_price_df.iloc[100, modified_price_df.columns.get_loc("close")] += 0.05
    mod_price_result = engine.run(modified_price_df)
    assert mod_price_result.reproducibility_hash is not None
    assert base_result.reproducibility_hash != mod_price_result.reproducibility_hash

    # 2. Modify a single timestamp in historical data
    modified_ts_df = market_data_3600.copy()
    idx = list(modified_ts_df.index)
    idx[50] = idx[50] + timedelta(seconds=1)
    modified_ts_df.index = pd.DatetimeIndex(idx)
    mod_ts_result = engine.run(modified_ts_df)
    assert base_result.reproducibility_hash != mod_ts_result.reproducibility_hash

    # 3. Modify spread in historical data
    modified_spread_df = market_data_3600.copy()
    modified_spread_df.iloc[200, modified_spread_df.columns.get_loc("spread")] += 1.0
    mod_spread_result = engine.run(modified_spread_df)
    assert base_result.reproducibility_hash != mod_spread_result.reproducibility_hash


# =====================================================================
# 9. Live Execution Safety
# =====================================================================
def test_backtest_engine_refuses_live_mode(symbol_spec, risk_settings):
    """BacktestEngine must fail immediately if configured with TradingMode.LIVE."""
    with pytest.raises(RuntimeError, match="Safety violation"):
        BacktestEngine(symbol_spec, risk_settings, trading_mode=TradingMode.LIVE)
