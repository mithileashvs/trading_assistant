from datetime import datetime, timedelta, timezone

from app.backtesting.metrics import compute_metrics
from app.backtesting.results import BacktestResult, BacktestTrade, EquityPoint


def _trade(pnl, days_ago=1, direction="BUY", strategy="TEST", regime="TREND_BULLISH", session="LONDON", exit_reason="TAKE_PROFIT"):
    now = datetime.now(timezone.utc)
    open_time = now - timedelta(days=days_ago, hours=1)
    close_time = now - timedelta(days=days_ago)
    return BacktestTrade(
        symbol="XAUUSD", direction=direction, strategy=strategy, regime=regime, score=8,
        open_time=open_time, close_time=close_time, entry_price=2650.0,
        exit_price=2650.0 + (pnl / 10), stop_loss=2645.0, take_profit=2660.0, lots=0.1,
        pnl=pnl, gross_pnl=pnl, commission=0.5, swap=0.0, r_multiple=pnl / 50.0,
        mae=2.0, mfe=5.0, exit_reason=exit_reason, session=session,
    )


def test_empty_result_reports_zero_trades_without_error():
    result = BacktestResult(symbol="XAUUSD", starting_balance=10000.0, ending_balance=10000.0)
    metrics = compute_metrics(result)
    assert metrics["number_of_trades"] == 0
    assert "note" in metrics


def test_all_winning_trades_profit_factor_is_infinite():
    now = datetime.now(timezone.utc)
    trades = [_trade(50.0, days_ago=3), _trade(30.0, days_ago=2), _trade(20.0, days_ago=1)]
    equity_curve = [EquityPoint(time=now - timedelta(days=3), equity=10000.0),
                     EquityPoint(time=now, equity=10100.0)]
    result = BacktestResult(symbol="XAUUSD", starting_balance=10000.0, ending_balance=10100.0, trades=trades, equity_curve=equity_curve)
    metrics = compute_metrics(result)
    assert metrics["win_rate"] == 1.0
    assert metrics["loss_rate"] == 0.0
    assert metrics["profit_factor"] == float("inf")


def test_win_rate_and_expectancy_computed_correctly():
    trades = [_trade(100.0, days_ago=3), _trade(-40.0, days_ago=2), _trade(-40.0, days_ago=1)]
    now = datetime.now(timezone.utc)
    equity_curve = [EquityPoint(time=now - timedelta(days=3), equity=10000.0),
                     EquityPoint(time=now, equity=10020.0)]
    result = BacktestResult(symbol="XAUUSD", starting_balance=10000.0, ending_balance=10020.0, trades=trades, equity_curve=equity_curve)
    metrics = compute_metrics(result)
    assert metrics["number_of_trades"] == 3
    assert metrics["win_rate"] == 1 / 3
    assert metrics["expectancy"] == (100.0 - 40.0 - 40.0) / 3


def test_max_consecutive_losses_counts_correctly():
    trades = [_trade(10.0, days_ago=5), _trade(-5.0, days_ago=4), _trade(-5.0, days_ago=3),
              _trade(-5.0, days_ago=2), _trade(10.0, days_ago=1)]
    now = datetime.now(timezone.utc)
    equity_curve = [EquityPoint(time=now - timedelta(days=5), equity=10000.0), EquityPoint(time=now, equity=10005.0)]
    result = BacktestResult(symbol="XAUUSD", starting_balance=10000.0, ending_balance=10005.0, trades=trades, equity_curve=equity_curve)
    metrics = compute_metrics(result)
    assert metrics["max_consecutive_losses"] == 3


def test_breakdowns_group_by_strategy_and_direction():
    trades = [
        _trade(50.0, days_ago=3, direction="BUY", strategy="TREND_PULLBACK"),
        _trade(-20.0, days_ago=2, direction="SELL", strategy="BREAKOUT"),
        _trade(30.0, days_ago=1, direction="BUY", strategy="TREND_PULLBACK"),
    ]
    now = datetime.now(timezone.utc)
    equity_curve = [EquityPoint(time=now - timedelta(days=3), equity=10000.0), EquityPoint(time=now, equity=10060.0)]
    result = BacktestResult(symbol="XAUUSD", starting_balance=10000.0, ending_balance=10060.0, trades=trades, equity_curve=equity_curve)
    metrics = compute_metrics(result)
    assert metrics["breakdown_by_strategy"]["TREND_PULLBACK"]["trades"] == 2
    assert metrics["breakdown_by_strategy"]["BREAKOUT"]["trades"] == 1
    assert metrics["breakdown_by_direction"]["BUY"]["trades"] == 2
    assert metrics["breakdown_by_direction"]["SELL"]["trades"] == 1


def test_max_drawdown_reflects_equity_curve_dip():
    now = datetime.now(timezone.utc)
    equity_curve = [
        EquityPoint(time=now - timedelta(days=4), equity=10000.0),
        EquityPoint(time=now - timedelta(days=3), equity=10500.0),
        EquityPoint(time=now - timedelta(days=2), equity=9800.0),  # drawdown from peak 10500
        EquityPoint(time=now - timedelta(days=1), equity=10200.0),
        EquityPoint(time=now, equity=10600.0),
    ]
    trades = [_trade(600.0, days_ago=1)]
    result = BacktestResult(symbol="XAUUSD", starting_balance=10000.0, ending_balance=10600.0, trades=trades, equity_curve=equity_curve)
    metrics = compute_metrics(result)
    assert metrics["max_drawdown_abs"] == -700.0  # 10500 -> 9800
