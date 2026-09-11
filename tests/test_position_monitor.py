from datetime import datetime, timezone

from app.execution.engine import ManagedPosition
from app.mt5.interface import Tick
from app.positions.monitor import ActionType, PositionMonitor, PositionMonitorConfig


def _position(direction="BUY", entry=2650.0, stop=2645.0, volume=0.2):
    return ManagedPosition(
        ticket=1, symbol="XAUUSD", direction=direction, volume=volume, price_open=entry,
        stop_loss=stop, take_profit=entry + 20 if direction == "BUY" else entry - 20,
        open_time=datetime.now(timezone.utc),
    )


def _tick(bid, ask=None):
    ask = ask if ask is not None else bid + 0.2
    return Tick(symbol="XAUUSD", time=datetime.now(timezone.utc), bid=bid, ask=ask, last=bid, volume=1.0)


def test_no_actions_near_entry():
    pos = _position()
    monitor = PositionMonitor()
    actions = monitor.evaluate(pos, _tick(2650.5), tick_size=0.01)
    assert actions == []


def test_no_actions_without_a_stop_loss():
    pos = _position()
    pos.stop_loss = None
    monitor = PositionMonitor()
    actions = monitor.evaluate(pos, _tick(2680.0), tick_size=0.01)
    assert actions == []


def test_breakeven_triggers_at_configured_r():
    pos = _position(entry=2650.0, stop=2645.0)  # risk = 5.0 -> 1R = 2655.0
    monitor = PositionMonitor(PositionMonitorConfig(_env_file=None, breakeven_trigger_r=1.0, enable_partial_exit=False, enable_trailing=False))
    actions = monitor.evaluate(pos, _tick(2655.5), tick_size=0.01)
    assert len(actions) == 1
    assert actions[0].type == ActionType.MOVE_TO_BREAKEVEN
    assert actions[0].new_stop_loss > pos.price_open  # locks in profit beyond entry


def test_breakeven_only_fires_once():
    pos = _position()
    monitor = PositionMonitor(PositionMonitorConfig(_env_file=None, breakeven_trigger_r=1.0, enable_partial_exit=False, enable_trailing=False))
    monitor.evaluate(pos, _tick(2656.0), tick_size=0.01)
    monitor.mark_breakeven_applied(pos.ticket)
    actions = monitor.evaluate(pos, _tick(2657.0), tick_size=0.01)
    assert not any(a.type == ActionType.MOVE_TO_BREAKEVEN for a in actions)


def test_partial_exit_triggers_and_uses_configured_fraction():
    pos = _position(volume=0.2)
    monitor = PositionMonitor(PositionMonitorConfig(_env_file=None, partial_exit_trigger_r=1.0, partial_exit_fraction=0.5, enable_trailing=False, breakeven_trigger_r=100.0))
    actions = monitor.evaluate(pos, _tick(2656.0), tick_size=0.01)
    partials = [a for a in actions if a.type == ActionType.PARTIAL_EXIT]
    assert len(partials) == 1
    assert partials[0].partial_volume == 0.1


def test_partial_exit_only_fires_once():
    pos = _position()
    monitor = PositionMonitor(PositionMonitorConfig(_env_file=None, partial_exit_trigger_r=1.0, enable_trailing=False, breakeven_trigger_r=100.0))
    monitor.evaluate(pos, _tick(2656.0), tick_size=0.01)
    monitor.mark_partial_taken(pos.ticket)
    actions = monitor.evaluate(pos, _tick(2657.0), tick_size=0.01)
    assert not any(a.type == ActionType.PARTIAL_EXIT for a in actions)


def test_trailing_stop_requires_atr_value():
    pos = _position(stop=2645.0)
    monitor = PositionMonitor(PositionMonitorConfig(_env_file=None, trailing_trigger_r=1.0, enable_partial_exit=False, breakeven_trigger_r=100.0))
    # No ATR supplied -> trailing must not fire even though R threshold is met.
    actions = monitor.evaluate(pos, _tick(2660.0), tick_size=0.01, current_atr=None)
    assert not any(a.type == ActionType.TRAIL_STOP for a in actions)


def test_trailing_stop_improves_position_only():
    pos = _position(entry=2650.0, stop=2645.0)
    monitor = PositionMonitor(PositionMonitorConfig(_env_file=None, trailing_trigger_r=1.0, enable_partial_exit=False, breakeven_trigger_r=100.0, trailing_atr_multiplier=1.0))
    actions = monitor.evaluate(pos, _tick(2660.0), tick_size=0.01, current_atr=2.0)
    trails = [a for a in actions if a.type == ActionType.TRAIL_STOP]
    assert len(trails) == 1
    assert trails[0].new_stop_loss > pos.stop_loss  # tightens, never loosens


def test_sell_position_directions_mirror_buy():
    pos = _position(direction="SELL", entry=2650.0, stop=2655.0)  # 1R = 2645.0
    monitor = PositionMonitor(PositionMonitorConfig(_env_file=None, breakeven_trigger_r=1.0, enable_partial_exit=False, enable_trailing=False))
    actions = monitor.evaluate(pos, _tick(2644.0, ask=2644.2), tick_size=0.01)
    assert len(actions) == 1
    assert actions[0].new_stop_loss < pos.price_open


def test_forget_clears_state_for_ticket():
    pos = _position()
    monitor = PositionMonitor()
    monitor.mark_breakeven_applied(pos.ticket)
    monitor.mark_partial_taken(pos.ticket)
    monitor.forget(pos.ticket)
    assert pos.ticket not in monitor._breakeven_moved
    assert pos.ticket not in monitor._partial_taken
