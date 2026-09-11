from datetime import datetime, timedelta, timezone

from app.config.settings import RiskSettings
from app.risk.guards import GuardCheckInput, RiskGuardEngine
from app.risk.history import TradeRecord


def _settings(**overrides) -> RiskSettings:
    return RiskSettings(_env_file=None, **overrides)


def _base_input(**overrides) -> GuardCheckInput:
    base = dict(
        equity=10000.0, day_start_equity=10000.0, week_start_equity=10000.0,
        trades_today_count=0, open_positions_count=0, current_spread_points=20.0,
        mt5_connected=True, broker_trade_allowed=True, market_data_fresh=True,
        kill_switch_active=False, recent_trades=[],
    )
    base.update(overrides)
    return GuardCheckInput(**base)


def test_all_checks_pass_with_healthy_input():
    engine = RiskGuardEngine(_settings())
    result = engine.check(_base_input())
    assert result.passed is True
    assert result.reasons == []
    assert all(result.checks.values())


def test_kill_switch_active_blocks_everything():
    engine = RiskGuardEngine(_settings())
    result = engine.check(_base_input(kill_switch_active=True))
    assert result.passed is False
    assert result.checks["kill_switch_ok"] is False


def test_mt5_disconnected_blocks():
    engine = RiskGuardEngine(_settings())
    result = engine.check(_base_input(mt5_connected=False))
    assert result.passed is False
    assert result.checks["mt5_connected"] is False


def test_broker_trade_disallowed_blocks():
    engine = RiskGuardEngine(_settings())
    result = engine.check(_base_input(broker_trade_allowed=False))
    assert result.passed is False


def test_stale_market_data_blocks():
    engine = RiskGuardEngine(_settings())
    result = engine.check(_base_input(market_data_fresh=False))
    assert result.passed is False


def test_below_minimum_equity_blocks():
    engine = RiskGuardEngine(_settings(min_account_equity=5000.0))
    result = engine.check(_base_input(equity=2000.0, day_start_equity=2000.0, week_start_equity=2000.0))
    assert result.passed is False
    assert result.checks["min_equity_ok"] is False


def test_daily_loss_exceeding_limit_blocks():
    engine = RiskGuardEngine(_settings(max_daily_loss_pct=2.0))
    # Equity dropped from 10000 to 9700 -> 3% daily loss, exceeds 2% cap.
    result = engine.check(_base_input(equity=9700.0, day_start_equity=10000.0))
    assert result.passed is False
    assert result.checks["daily_loss_ok"] is False


def test_daily_gain_does_not_trip_loss_guard():
    engine = RiskGuardEngine(_settings(max_daily_loss_pct=2.0))
    result = engine.check(_base_input(equity=10500.0, day_start_equity=10000.0))
    assert result.checks["daily_loss_ok"] is True


def test_weekly_loss_exceeding_limit_blocks():
    engine = RiskGuardEngine(_settings(max_weekly_loss_pct=5.0))
    result = engine.check(_base_input(equity=9000.0, week_start_equity=10000.0))
    assert result.passed is False
    assert result.checks["weekly_loss_ok"] is False


def test_max_trades_per_day_blocks_at_limit():
    engine = RiskGuardEngine(_settings(max_trades_per_day=3))
    result = engine.check(_base_input(trades_today_count=3))
    assert result.passed is False
    assert result.checks["max_trades_per_day_ok"] is False


def test_max_trades_per_day_allows_below_limit():
    engine = RiskGuardEngine(_settings(max_trades_per_day=3))
    result = engine.check(_base_input(trades_today_count=2))
    assert result.checks["max_trades_per_day_ok"] is True


def test_max_open_positions_blocks_at_limit():
    engine = RiskGuardEngine(_settings(max_open_positions=1))
    result = engine.check(_base_input(open_positions_count=1))
    assert result.passed is False
    assert result.checks["max_open_positions_ok"] is False


def test_max_spread_blocks_when_exceeded():
    engine = RiskGuardEngine(_settings(max_spread_points=30.0))
    result = engine.check(_base_input(current_spread_points=45.0))
    assert result.passed is False
    assert result.checks["max_spread_ok"] is False


def test_consecutive_losses_counted_from_most_recent_backwards():
    now = datetime.now(timezone.utc)
    trades = [
        TradeRecord(closed_at=now - timedelta(hours=5), symbol="XAUUSD", direction="BUY", pnl=50.0),  # win, breaks streak
        TradeRecord(closed_at=now - timedelta(hours=4), symbol="XAUUSD", direction="BUY", pnl=-10.0),
        TradeRecord(closed_at=now - timedelta(hours=3), symbol="XAUUSD", direction="SELL", pnl=-20.0),
        TradeRecord(closed_at=now - timedelta(hours=2), symbol="XAUUSD", direction="BUY", pnl=-15.0),
    ]
    engine = RiskGuardEngine(_settings(max_consecutive_losses=3))
    result = engine.check(_base_input(recent_trades=trades))
    assert result.passed is False
    assert result.checks["max_consecutive_losses_ok"] is False


def test_consecutive_losses_reset_by_a_win():
    now = datetime.now(timezone.utc)
    trades = [
        TradeRecord(closed_at=now - timedelta(hours=3), symbol="XAUUSD", direction="BUY", pnl=-10.0),
        TradeRecord(closed_at=now - timedelta(hours=2), symbol="XAUUSD", direction="SELL", pnl=-20.0),
        TradeRecord(closed_at=now - timedelta(hours=1), symbol="XAUUSD", direction="BUY", pnl=30.0),  # win
    ]
    engine = RiskGuardEngine(_settings(max_consecutive_losses=2))
    result = engine.check(_base_input(recent_trades=trades))
    assert result.checks["max_consecutive_losses_ok"] is True


def test_multiple_failures_all_reported():
    engine = RiskGuardEngine(_settings(max_spread_points=10.0, max_open_positions=1))
    result = engine.check(_base_input(current_spread_points=50.0, open_positions_count=2, kill_switch_active=True))
    assert result.passed is False
    assert len(result.reasons) >= 3
