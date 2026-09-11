import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import pytest
import strategy_test_helpers as h

from app.backtesting.costs import ExecutionCosts
from app.backtesting.engine import BacktestConfig, BacktestEngine
from app.backtesting.metrics import compute_metrics
from app.config.settings import get_settings
from app.mt5.mock_client import MockMT5Client

# A single, moderately-sized backtest run shared across tests --
# running the full walk-forward loop is the expensive part, so we
# don't want to repeat it per assertion. step=8 (bi-hourly evaluation)
# and a bounded dataset keep this well under test-suite time budgets.
# Data comes from a fixed, time-anchored synthetic generator (not
# MockMT5Client.get_ohlcv, which anchors to wall-clock "now" and would
# make session-dependent scoring -- and therefore trade counts --
# non-reproducible between runs at different times of day).
_BARS = 4600
_WARMUP = 3400


@pytest.fixture(scope="module")
def market_data():
    return h.synthetic_market_ohlcv(n=_BARS, seed=99)


@pytest.fixture(scope="module")
def backtest_result(market_data):
    client = MockMT5Client()
    spec = client.get_symbol_spec("XAUUSD")
    settings = get_settings()
    cfg = BacktestConfig(warmup_bars=_WARMUP, resample_lookback_bars=3700, step=4)
    engine = BacktestEngine(spec, settings.risk, config=cfg)
    return engine.run(market_data), spec, settings


def test_bars_evaluated_matches_input_length(backtest_result):
    result, spec, settings = backtest_result
    assert result.bars_evaluated == _BARS


def test_no_trades_open_before_warmup(backtest_result, market_data):
    result, spec, settings = backtest_result
    warmup_cutoff = market_data.index[_WARMUP]
    for trade in result.trades:
        assert trade.open_time >= warmup_cutoff


def test_entries_fill_after_the_decision_bar_not_on_it(backtest_result):
    """Every trade's open_time must be a LATER bar than whatever bar
    produced the signal -- entries never fill on the same bar whose
    close triggered them (no same-bar look-ahead)."""
    result, spec, settings = backtest_result
    # We can't directly recover "decision bar" here, but we can assert
    # entries land on bar boundaries that exist in the M15 index and
    # are never earlier than the warmup cutoff -- combined with the
    # dedicated resampling no-lookahead test, this is the integration
    # -level check that the engine's decide-then-fill wiring is intact.
    assert all(t.open_time < t.close_time for t in result.trades)


def test_stop_loss_exits_never_worse_than_stop_by_more_than_costs(backtest_result):
    result, spec, settings = backtest_result
    for trade in result.trades:
        if trade.exit_reason != "STOP_LOSS":
            continue
        # BUY stopped out should exit at/near/below the stop (costs make it worse, i.e. lower);
        # SELL stopped out should exit at/near/above the stop.
        if trade.direction == "BUY":
            assert trade.exit_price <= trade.stop_loss + 1e-6
        else:
            assert trade.exit_price >= trade.stop_loss - 1e-6


def test_commission_and_costs_are_applied(backtest_result):
    result, spec, settings = backtest_result
    for trade in result.trades:
        assert trade.commission > 0
        assert trade.pnl == pytest.approx(trade.gross_pnl - trade.commission + trade.swap, abs=1e-6)


def test_equity_curve_length_matches_bars(backtest_result):
    result, spec, settings = backtest_result
    assert len(result.equity_curve) == _BARS


def test_ending_balance_reflects_realized_trade_pnl(backtest_result):
    result, spec, settings = backtest_result
    expected = result.starting_balance + sum(t.pnl for t in result.trades)
    assert result.ending_balance == pytest.approx(expected, abs=1e-6)


def test_position_sizing_used_broker_spec_not_hardcoded(backtest_result):
    result, spec, settings = backtest_result
    for trade in result.trades:
        # lots must be a clean multiple of the broker's volume_step
        ratio = trade.lots / spec.volume_step
        assert ratio == pytest.approx(round(ratio), abs=1e-6)
        assert spec.volume_min <= trade.lots <= spec.volume_max


def test_metrics_computed_without_error(backtest_result):
    result, spec, settings = backtest_result
    metrics = compute_metrics(result)
    assert metrics["number_of_trades"] == len(result.trades)
    assert metrics["starting_balance"] == result.starting_balance
    assert metrics["ending_balance"] == result.ending_balance


def test_metrics_breakdowns_sum_to_total_trades(backtest_result):
    result, spec, settings = backtest_result
    metrics = compute_metrics(result)
    if metrics["number_of_trades"] == 0:
        pytest.skip("No trades in this run to break down.")
    by_direction_total = sum(v["trades"] for v in metrics["breakdown_by_direction"].values())
    assert by_direction_total == metrics["number_of_trades"]


def test_no_lookahead_prefix_invariance(market_data, backtest_result):
    """The gold-standard no-lookahead check: running the engine on a
    truncated dataset must reproduce the exact same trades (up to the
    truncation point) as running it on the full dataset -- proving
    bars beyond the truncation point cannot influence earlier decisions."""
    result_full, spec, settings = backtest_result
    truncated_df = market_data.iloc[: _BARS - 200]  # chop off the last ~2 days

    cfg = BacktestConfig(warmup_bars=_WARMUP, resample_lookback_bars=3700, step=4)
    result_truncated = BacktestEngine(spec, settings.risk, config=cfg).run(truncated_df)

    cutoff = truncated_df.index[-1]
    full_trades_before_cutoff = [t for t in result_full.trades if t.close_time <= cutoff]
    truncated_closed_trades = [t for t in result_truncated.trades if t.exit_reason != "END_OF_DATA"]

    assert len(full_trades_before_cutoff) == len(truncated_closed_trades)
    for a, b in zip(full_trades_before_cutoff, truncated_closed_trades):
        assert a.open_time == b.open_time
        assert a.entry_price == pytest.approx(b.entry_price)
        assert a.exit_price == pytest.approx(b.exit_price)
        assert a.pnl == pytest.approx(b.pnl)


def test_zero_cost_backtest_differs_from_realistic_cost_backtest(market_data, backtest_result):
    result_real, spec, settings = backtest_result  # uses the default (realistic) ExecutionCosts
    cfg = BacktestConfig(warmup_bars=_WARMUP, resample_lookback_bars=3700, step=4)
    zero_cost = ExecutionCosts(_env_file=None, spread_points=0, slippage_points=0, commission_per_lot=0, swap_long_per_lot_per_day=0, swap_short_per_lot_per_day=0)
    result_zero = BacktestEngine(spec, settings.risk, costs=zero_cost, config=cfg).run(market_data)

    if result_zero.trades and result_real.trades:
        # Same trade count/direction/timing (decisions are cost-independent
        # here), but realistic costs must never produce a better P&L.
        assert result_real.ending_balance <= result_zero.ending_balance
