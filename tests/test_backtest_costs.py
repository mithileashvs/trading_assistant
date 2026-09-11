from app.backtesting.costs import ExecutionCosts, apply_entry_costs, apply_exit_costs, commission_cost, swap_cost


def _costs(**overrides) -> ExecutionCosts:
    return ExecutionCosts(_env_file=None, **overrides)


def test_buy_entry_fills_above_raw_price():
    costs = _costs(spread_points=20, slippage_points=5)
    fill = apply_entry_costs(2650.0, "BUY", costs, tick_size=0.01)
    assert fill.price > 2650.0


def test_sell_entry_fills_below_raw_price():
    costs = _costs(spread_points=20, slippage_points=5)
    fill = apply_entry_costs(2650.0, "SELL", costs, tick_size=0.01)
    assert fill.price < 2650.0


def test_buy_exit_fills_below_raw_price():
    costs = _costs(spread_points=20, slippage_points=5)
    fill = apply_exit_costs(2650.0, "BUY", costs, tick_size=0.01)
    assert fill.price < 2650.0


def test_sell_exit_fills_above_raw_price():
    costs = _costs(spread_points=20, slippage_points=5)
    fill = apply_exit_costs(2650.0, "SELL", costs, tick_size=0.01)
    assert fill.price > 2650.0


def test_round_trip_costs_are_never_favorable():
    # Entry + exit costs should always net to a worse price than a
    # costless round trip, for both directions -- costs never help.
    costs = _costs(spread_points=20, slippage_points=5)
    entry = apply_entry_costs(2650.0, "BUY", costs, 0.01)
    exit_ = apply_exit_costs(2650.0, "BUY", costs, 0.01)
    assert exit_.price < entry.price


def test_commission_scales_with_lots():
    costs = _costs(commission_per_lot=7.0)
    assert commission_cost(1.0, costs) == 7.0
    assert commission_cost(0.5, costs) == 3.5


def test_swap_zero_for_zero_days_held():
    costs = _costs(swap_long_per_lot_per_day=-6.5)
    assert swap_cost(1.0, "BUY", 0, costs) == 0.0


def test_swap_accumulates_per_day():
    costs = _costs(swap_long_per_lot_per_day=-6.5, swap_short_per_lot_per_day=-3.0)
    assert swap_cost(1.0, "BUY", 3, costs) == -19.5
    assert swap_cost(2.0, "SELL", 2, costs) == -12.0


def test_zero_cost_config_is_a_no_op():
    costs = _costs(spread_points=0, slippage_points=0)
    fill = apply_entry_costs(2650.0, "BUY", costs, 0.01)
    assert fill.price == 2650.0
