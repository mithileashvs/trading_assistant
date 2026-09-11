import pytest

from app.mt5.interface import SymbolSpec
from app.risk.position_sizing import PositionSizingError, calculate_position_size


def _spec(**overrides) -> SymbolSpec:
    base = dict(
        name="XAUUSD", contract_size=100.0, volume_min=0.01, volume_max=50.0,
        volume_step=0.01, tick_size=0.01, tick_value=1.0, digits=2,
        stops_level_points=50, freeze_level_points=0, trade_allowed=True, spread_points=20.0,
    )
    base.update(overrides)
    return SymbolSpec(**base)


def test_basic_position_size_matches_hand_calculation():
    # equity=10000, risk=0.5% -> $50 risk. Stop distance $5, tick_size
    # 0.01 -> 500 ticks * $1/tick = $500 loss per lot. 50/500 = 0.1 lots.
    result = calculate_position_size(10000, 0.5, 2650.0, 2645.0, _spec())
    assert result.lots == pytest.approx(0.1)
    assert result.monetary_risk == pytest.approx(50.0)
    assert result.loss_per_lot == pytest.approx(500.0)
    assert not result.clamped


def test_rounds_down_to_volume_step_never_up():
    # Choose numbers that don't divide evenly by volume_step.
    result = calculate_position_size(10000, 0.53, 2650.0, 2645.0, _spec(volume_step=0.05))
    assert result.lots <= result.requested_lots
    # lots must be an exact multiple of volume_step
    assert round(result.lots / 0.05) == pytest.approx(result.lots / 0.05)


def test_clamped_to_minimum_when_risk_too_small():
    result = calculate_position_size(100, 0.01, 2650.0, 2649.99, _spec(volume_min=0.01))
    assert result.lots == 0.0
    assert result.clamped
    assert not result.is_tradable
    assert result.warnings


def test_clamped_to_maximum_when_risk_huge():
    result = calculate_position_size(10_000_000, 5.0, 2650.0, 2649.0, _spec(volume_max=50.0))
    assert result.lots == 50.0
    assert result.clamped


def test_never_hardcodes_lot_size_uses_symbol_spec():
    spec_a = _spec(tick_value=1.0)
    spec_b = _spec(tick_value=2.0)  # different instrument/broker economics
    result_a = calculate_position_size(10000, 0.5, 2650.0, 2645.0, spec_a)
    result_b = calculate_position_size(10000, 0.5, 2650.0, 2645.0, spec_b)
    assert result_a.lots != result_b.lots  # sizing genuinely depends on the spec


def test_raises_on_zero_stop_distance():
    with pytest.raises(PositionSizingError):
        calculate_position_size(10000, 0.5, 2650.0, 2650.0, _spec())


def test_raises_on_nonpositive_equity():
    with pytest.raises(PositionSizingError):
        calculate_position_size(0, 0.5, 2650.0, 2645.0, _spec())


def test_larger_stop_distance_yields_smaller_position():
    tight = calculate_position_size(10000, 0.5, 2650.0, 2648.0, _spec())
    wide = calculate_position_size(10000, 0.5, 2650.0, 2640.0, _spec())
    assert wide.lots < tight.lots
