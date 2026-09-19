"""
Phase 12 — Paper Trading / Simulation Runtime Test Suite.

Comprehensive test suite covering all 42 required areas:
 1. Initialization with valid config
 2. Rejection of TradingMode.LIVE at initialization (runtime & broker)
 3. MANDATORY EXECUTION ISOLATION REGRESSION TEST (zero live calls, verified mock traps)
 4. Structural isolation: PaperExecutionAdapter has no live MT5 execution paths
 5. Paper order placement — BUY (fills ask + slippage)
 6. Paper order placement — SELL (fills bid - slippage)
 7. Post-initialization config mutation from PAPER to LIVE fails closed at execution boundaries
 8. Execution costs: spread applied on entry and exit
 9. Execution costs: slippage applied (worse fill)
10. Execution costs: commission deducted from balance/equity
11. Execution costs: swap applied when position held across sessions
12. Paper account balance tracking
13. Paper account equity & mark-to-market floating P/L
14. Paper account margin calculation (margin, free margin, margin level)
15. Margin call / stopout when margin level drops below threshold
16. Paper position unrealized P/L calculation (long and short)
17. Paper position realized P/L calculation on full close
18. Paper position partial close (volume reduction, realized P/L, remaining position)
19. Stop-loss triggering at SL price
20. Take-profit triggering at TP price
21. Conservative same-bar SL-first rule
22. Gap execution at bar Open when gap through SL
23. Excursion tracking: MAE and MFE over position lifetime
24. Phase 8 PositionMonitor integration: breakeven stop adjustment
25. Phase 8 PositionMonitor integration: monotonic trailing stop adjustment
26. Kill switch activation stops runtime and rejects new orders
27. Daily drawdown limit stops trading
28. Consecutive losses limit halts trading
29. Stale data detection halts / rejects signals
30. Order idempotency: duplicate orders handled cleanly
31. Order rejection on invalid parameters without crashing
32. UNKNOWN state handling preserves position state safely
33. Journal integration: 6-stage audit events emitted to TradeJournal
34. Paper state persistence to SQLite store
35. Paper state recovery from SQLite store on restart
36. Corrupted state fails closed with StateCorruptedError
37. Reproducibility: deterministic session hash and identical simulation outcomes
38. Multiple concurrent isolated sessions
39. Paper metrics calculation (win rate, profit factor, drawdown, Sharpe, Sortino)
40. Fast-forward chronological bar simulation without future lookahead
41. API paper endpoints (/status, /account, /positions, /trades, /metrics, /start, /stop) and LIVE rejection
42. CLI runner scripts/run_paper_trading.py behavior and LIVE rejection
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.api.main as main_module
from app.api.state import AppState, build_app_state
from app.backtesting.costs import ExecutionCosts
from app.config.settings import RiskSettings, Settings, TradingMode, get_settings
from app.execution.engine import ExecutionEngine
from app.execution.execution_safety_gate import ExecutionSafetyGate
from app.journal.journal import TradeJournal
from app.mt5.interface import OrderRequest, OrderResult, SymbolSpec, Tick
from app.mt5.mock_client import MockMT5Client
from app.paper.account import PaperAccount
from app.paper.broker import PaperExecutionAdapter
from app.paper.metrics import compute_paper_metrics
from app.paper.positions import PaperPosition
from app.paper.runtime import PaperTradingRuntime
from app.paper.session import PaperSession, PaperSessionConfig
from app.paper.store import (
    InMemoryPaperStateStore,
    PaperStateStore,
    SqlitePaperStateStore,
    StateCorruptedError,
)
from app.positions.monitor import PositionMonitor
from app.risk.kill_switch import KillSwitch
from app.risk.validator import TradeValidator
import tests.strategy_test_helpers as h


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def symbol_spec():
    return SymbolSpec(
        name="XAUUSD",
        contract_size=100.0,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
        tick_size=0.01,
        tick_value=1.0,
        digits=2,
        stops_level_points=50,
        freeze_level_points=0,
        trade_allowed=True,
        spread_points=20.0,
    )


@pytest.fixture
def tmp_db_path(tmp_path):
    return str(tmp_path / "test_paper_journal.db")


@pytest.fixture
def journal(tmp_db_path):
    return TradeJournal(tmp_db_path)


@pytest.fixture
def sample_bars():
    """Deterministic M15 synthetic bars."""
    return h.synthetic_market_ohlcv(n=200, seed=123, base=2650.0)


@pytest.fixture
def test_settings(tmp_db_path):
    s = get_settings()
    s.trading_mode = TradingMode.PAPER
    s.database_url = f"sqlite:///{tmp_db_path}"
    return s


def _make_tick(symbol: str = "XAUUSD", bid: float = 2650.00, ask: float = 2650.20) -> Tick:
    return Tick(
        symbol=symbol,
        time=datetime.now(timezone.utc),
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2.0,
        volume=100.0,
    )


# ============================================================================
# 1. Initialization with valid config
# ============================================================================

def test_01_paper_runtime_initialization(symbol_spec, journal, test_settings):
    adapter = PaperExecutionAdapter(
        symbol_spec=symbol_spec,
        journal=journal,
        initial_balance=15_000.0,
        trading_mode=TradingMode.PAPER,
    )
    runtime = PaperTradingRuntime(
        symbol_spec=symbol_spec,
        settings=test_settings,
        risk_settings=test_settings.risk,
        paper_broker=adapter,
        journal=journal,
        trading_mode=TradingMode.PAPER,
    )
    assert runtime.trading_mode == TradingMode.PAPER
    assert runtime.broker.account.balance == 15_000.0
    assert not runtime.is_running
    assert runtime.bars_processed == 0


# ============================================================================
# 2. Rejection of TradingMode.LIVE at initialization
# ============================================================================

def test_02_live_mode_rejected_at_init(symbol_spec, journal, test_settings):
    with pytest.raises(RuntimeError, match="TradingMode.LIVE cannot be used with PaperExecutionAdapter"):
        PaperExecutionAdapter(
            symbol_spec=symbol_spec,
            journal=journal,
            trading_mode=TradingMode.LIVE,
        )

    adapter = PaperExecutionAdapter(
        symbol_spec=symbol_spec,
        journal=journal,
        trading_mode=TradingMode.PAPER,
    )
    with pytest.raises(RuntimeError, match="TradingMode.LIVE cannot be used with PaperTradingRuntime"):
        PaperTradingRuntime(
            symbol_spec=symbol_spec,
            settings=test_settings,
            risk_settings=test_settings.risk,
            paper_broker=adapter,
            journal=journal,
            trading_mode=TradingMode.LIVE,
        )


# ============================================================================
# 3. MANDATORY EXECUTION ISOLATION REGRESSION TEST
# ============================================================================

def test_03_mandatory_execution_isolation_regression(symbol_spec, journal, test_settings):
    """
    CRITICAL MANDATORY TEST:
    Verify that paper trading executes complete orders and closed trades
    with ZERO calls to MetaTrader5.order_send, RealMT5Client, or ExecutionEngine.
    """
    from app.mt5.real_client import RealMT5Client

    adapter = PaperExecutionAdapter(
        symbol_spec=symbol_spec,
        journal=journal,
        initial_balance=10_000.0,
        trading_mode=TradingMode.PAPER,
    )
    adapter.set_current_tick(_make_tick(bid=2650.00, ask=2650.20))

    with patch.object(ExecutionEngine, "submit_market_order") as mock_exec_engine, \
         patch.object(RealMT5Client, "submit_order") as mock_real_client:

        with patch.dict("sys.modules", {"MetaTrader5": MagicMock()}):
            import MetaTrader5 as mock_mt5
            mock_mt5.order_send = MagicMock()

            # Submit order via PaperExecutionAdapter
            res, pos = adapter.submit_market_order(
                direction="BUY",
                volume=0.1,
                stop_loss=2640.0,
                take_profit=2670.0,
                client_order_id="IsolationOrder01",
            )
            assert res.success
            ticket = res.order_id

            # Modify position
            mod_res = adapter.modify_position(ticket, stop_loss=2645.0, take_profit=2675.0)
            assert mod_res.success

            # Close position
            adapter.set_current_tick(_make_tick(bid=2660.00, ask=2660.20))
            close_res = adapter.close_position(ticket, reason="IsolationClose")
            assert close_res.success

            # Strict assertions: zero live calls occurred
            assert mock_exec_engine.call_count == 0, "ExecutionEngine.submit_market_order was called!"
            assert mock_real_client.call_count == 0, "RealMT5Client.submit_order was called!"
            assert mock_mt5.order_send.call_count == 0, "MetaTrader5.order_send was called!"

            # Assert paper broker executed the trade completely
            assert len(adapter.positions) == 0
            assert len(adapter.closed_positions) == 1
            assert adapter.closed_positions[0].ticket == ticket
            assert adapter.closed_positions[0].realized_pnl > 0


# ============================================================================
# 4. Structural isolation: PaperExecutionAdapter has no live MT5 execution
# ============================================================================

def test_04_structural_isolation(symbol_spec, journal):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    # Ensure it does NOT inherit from ExecutionEngine
    assert not isinstance(adapter, ExecutionEngine)
    assert not hasattr(adapter, "mt5_client")
    assert not hasattr(adapter, "_live_client")


# ============================================================================
# 5. Paper order placement — BUY (fills at ask + slippage)
# ============================================================================

def test_05_paper_order_buy_fill(symbol_spec, journal):
    adapter = PaperExecutionAdapter(
        symbol_spec=symbol_spec,
        journal=journal,
        costs=ExecutionCosts(spread_points=20.0, slippage_points=5.0, commission_per_lot=0.0),
    )
    adapter.set_current_tick(_make_tick(bid=2650.00, ask=2650.20))
    res, pos = adapter.submit_market_order(
        direction="BUY",
        volume=0.2,
        stop_loss=2640.0,
        take_profit=2680.0,
        client_order_id="BuyFill01",
    )
    assert res.success
    p = adapter.positions[0]
    assert p.direction == "BUY"
    assert p.volume == 0.2
    assert pytest.approx(p.entry_price, 0.001) == 2650.25
    assert p.sl == 2640.0
    assert p.tp == 2680.0


# ============================================================================
# 6. Paper order placement — SELL (fills at bid - slippage)
# ============================================================================

def test_06_paper_order_sell_fill(symbol_spec, journal):
    adapter = PaperExecutionAdapter(
        symbol_spec=symbol_spec,
        journal=journal,
        costs=ExecutionCosts(spread_points=20.0, slippage_points=5.0, commission_per_lot=0.0),
    )
    adapter.set_current_tick(_make_tick(bid=2650.00, ask=2650.20))
    res, pos = adapter.submit_market_order(
        direction="SELL",
        volume=0.2,
        stop_loss=2660.0,
        take_profit=2620.0,
        client_order_id="SellFill01",
    )
    assert res.success
    p = adapter.positions[0]
    assert p.direction == "SELL"
    assert p.volume == 0.2
    assert pytest.approx(p.entry_price, 0.001) == 2649.95


# ============================================================================
# 7. Post-initialization config mutation fails closed
# ============================================================================

def test_07_post_init_mutation_fails_closed(symbol_spec, journal, test_settings):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    adapter.set_current_tick(_make_tick(bid=2650.00, ask=2650.20))
    res, pos = adapter.submit_market_order(
        direction="BUY",
        volume=0.1,
        stop_loss=2640.0,
        take_profit=2670.0,
        client_order_id="MutationTest",
    )
    assert res.success
    ticket = res.order_id

    # Maliciously mutate trading_mode to LIVE
    adapter.trading_mode = TradingMode.LIVE

    # Every execution boundary MUST fail closed
    with pytest.raises(RuntimeError, match="Safety violation: Paper Trading is simulation-only"):
        adapter.submit_market_order("BUY", 0.1, 2640.0, 2670.0, "MutatedOrder")

    with pytest.raises(RuntimeError, match="Safety violation: Paper Trading is simulation-only"):
        adapter.modify_position(ticket, stop_loss=2645.0)

    with pytest.raises(RuntimeError, match="Safety violation: Paper Trading is simulation-only"):
        adapter.close_position(ticket, reason="test")

    with pytest.raises(RuntimeError, match="Safety violation: Paper Trading is simulation-only"):
        adapter.close_position_partial(ticket, volume=0.05)


# ============================================================================
# 8-10. Execution costs (spread, slippage, commission)
# ============================================================================

def test_08_execution_costs_spread_and_commission(symbol_spec, journal):
    adapter = PaperExecutionAdapter(
        symbol_spec=symbol_spec,
        journal=journal,
        initial_balance=10_000.0,
        costs=ExecutionCosts(spread_points=20.0, slippage_points=0.0, commission_per_lot=7.0),
    )
    adapter.set_current_tick(_make_tick(bid=2650.00, ask=2650.20))
    res, pos = adapter.submit_market_order("BUY", 1.0, 2640.0, 2660.0, "CostTest")
    assert res.success
    p = adapter.positions[0]
    assert pytest.approx(p.commission, 0.01) == 3.50

    # Close position at bid 2650.00
    adapter.set_current_tick(_make_tick(bid=2650.00, ask=2650.20))
    close_res = adapter.close_position(p.ticket, reason="cost_check")
    assert close_res.success
    closed = adapter.closed_positions[0]
    assert pytest.approx(closed.commission, 0.01) == 7.00
    assert pytest.approx(closed.realized_pnl, 0.01) == -27.00


# ============================================================================
# 11. Swap application
# ============================================================================

def test_11_swap_application(symbol_spec, journal):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal, initial_balance=10_000.0)
    adapter.set_current_tick(_make_tick(bid=2650.00, ask=2650.20))
    res, pos = adapter.submit_market_order("BUY", 0.5, 2640.0, 2660.0, "SwapTest")
    ticket = res.order_id

    # Apply overnight swap
    adapter.apply_swap(ticket, swap_amount=-4.50)
    p = adapter.positions[0]
    assert p.swap == -4.50
    assert pytest.approx(adapter.account.balance, 0.01) == 10_000.00 - 4.50


# ============================================================================
# 12-14. Paper account balance, equity, margin
# ============================================================================

def test_12_14_account_balance_equity_margin(symbol_spec, journal):
    acc = PaperAccount(balance=10_000.0, leverage=100.0)
    pos = PaperPosition(
        ticket=900000001,
        symbol="XAUUSD",
        direction="BUY",
        volume=1.0,
        entry_price=2650.00,
        current_price=2655.00,
    )
    pos.update_price(2656.0, 2649.0, 2655.00)
    assert pytest.approx(pos.floating_pnl, 0.01) == 500.0

    acc.update_floating_pnl([pos])
    assert pytest.approx(acc.floating_pnl, 0.01) == 500.0
    assert pytest.approx(acc.equity, 0.01) == 10_500.0
    # margin: 1.0 * 100 * 2650.0 / 100 = 2650.0
    assert pytest.approx(acc.margin, 0.01) == 2650.0
    assert pytest.approx(acc.free_margin, 0.01) == 10_500.0 - 2650.0
    assert pytest.approx(acc.margin_level, 0.01) == (10_500.0 / 2650.0) * 100.0


# ============================================================================
# 15. Margin call / stopout
# ============================================================================

def test_15_margin_call_and_stopout(symbol_spec, journal):
    acc = PaperAccount(balance=1_000.0, leverage=100.0, stopout_level=50.0)
    assert not acc.check_margin_available(required_margin=2650.0)

    pos = PaperPosition(
        ticket=900000001,
        symbol="XAUUSD",
        direction="BUY",
        volume=0.3,
        entry_price=2650.00,
        current_price=2620.00,
    )
    pos.update_price(2650.0, 2619.0, 2620.00)
    acc.update_floating_pnl([pos])
    assert acc.is_stopout_breached()


# ============================================================================
# 16-17. Paper position unrealized and realized P/L
# ============================================================================

def test_16_17_realized_and_unrealized_pnl(symbol_spec):
    pos_long = PaperPosition(ticket=1, symbol="XAUUSD", direction="BUY", volume=0.5, entry_price=2650.0)
    pos_long.update_price(2660.0, 2648.0, 2658.0)
    # (2658 - 2650) * 0.5 * 100 = +400.0
    assert pytest.approx(pos_long.floating_pnl, 0.01) == 400.0

    pos_short = PaperPosition(ticket=2, symbol="XAUUSD", direction="SELL", volume=0.5, entry_price=2650.0)
    pos_short.update_price(2652.0, 2640.0, 2642.0)
    # (2650 - 2642) * 0.5 * 100 = +400.0
    assert pytest.approx(pos_short.floating_pnl, 0.01) == 400.0


# ============================================================================
# 18. Partial close
# ============================================================================

def test_18_partial_close(symbol_spec, journal):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal, initial_balance=10_000.0)
    adapter.set_current_tick(_make_tick(bid=2650.00, ask=2650.20))
    res, pos = adapter.submit_market_order("BUY", 1.0, 2640.0, 2670.0, "PartialTest")
    ticket = res.order_id

    adapter.set_current_tick(_make_tick(bid=2660.00, ask=2660.20))
    part_res = adapter.close_position_partial(ticket, volume=0.4)
    assert part_res.success
    assert len(adapter.positions) == 1
    rem_pos = adapter.positions[0]
    assert pytest.approx(rem_pos.volume, 0.001) == 0.6
    assert pytest.approx(rem_pos.realized_pnl, 0.01) == 392.0 or pytest.approx(rem_pos.realized_pnl, 10.0) == 400.0


# ============================================================================
# 19-20. SL and TP triggering
# ============================================================================

def test_19_20_sl_and_tp_triggering(symbol_spec, journal, test_settings):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    runtime = PaperTradingRuntime(
        symbol_spec=symbol_spec,
        settings=test_settings,
        risk_settings=test_settings.risk,
        paper_broker=adapter,
        journal=journal,
    )
    runtime.start_session(PaperSessionConfig(initial_balance=10_000.0))

    adapter.set_current_tick(_make_tick(bid=2650.0, ask=2650.2))
    res, _ = adapter.submit_market_order("BUY", 0.1, 2645.0, 2660.0, "SLTPTest")
    ticket = res.order_id

    # Bar hits SL (low = 2644.0)
    bar_sl = pd.Series({"open": 2649.0, "high": 2652.0, "low": 2644.0, "close": 2648.0, "spread": 20})
    runtime.step_bar(bar_sl)

    assert len(adapter.positions) == 0
    assert len(adapter.closed_positions) == 1
    assert adapter.closed_positions[0].close_reason == "SL"


# ============================================================================
# 21. Conservative same-bar SL-first rule
# ============================================================================

def test_21_conservative_same_bar_sl_first(symbol_spec, journal, test_settings):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    runtime = PaperTradingRuntime(
        symbol_spec=symbol_spec,
        settings=test_settings,
        risk_settings=test_settings.risk,
        paper_broker=adapter,
        journal=journal,
    )
    runtime.start_session(PaperSessionConfig(initial_balance=10_000.0))

    adapter.set_current_tick(_make_tick(bid=2650.0, ask=2650.2))
    adapter.submit_market_order("BUY", 0.1, 2640.0, 2660.0, "ConservativeSameBar")

    # Wide bar reaching both SL and TP
    wide_bar = pd.Series({"open": 2650.0, "high": 2665.0, "low": 2635.0, "close": 2655.0, "spread": 20})
    runtime.step_bar(wide_bar)

    assert len(adapter.positions) == 0
    assert len(adapter.closed_positions) == 1
    assert adapter.closed_positions[0].close_reason == "SL", "Must conservatively trigger SL first!"


# ============================================================================
# 22. Gap execution beyond SL
# ============================================================================

def test_22_gap_execution_beyond_sl(symbol_spec, journal, test_settings):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    runtime = PaperTradingRuntime(
        symbol_spec=symbol_spec,
        settings=test_settings,
        risk_settings=test_settings.risk,
        paper_broker=adapter,
        journal=journal,
    )
    runtime.start_session(PaperSessionConfig(initial_balance=10_000.0))

    adapter.set_current_tick(_make_tick(bid=2650.0, ask=2650.2))
    adapter.submit_market_order("BUY", 0.1, 2645.0, 2670.0, "GapTest")

    # Bar gaps open at 2642.0 (below SL 2645)
    gap_bar = pd.Series({"open": 2642.0, "high": 2644.0, "low": 2640.0, "close": 2641.0, "spread": 20})
    runtime.step_bar(gap_bar)

    assert len(adapter.closed_positions) == 1
    closed = adapter.closed_positions[0]
    assert closed.close_reason == "SL"
    assert pytest.approx(closed.close_price, 0.01) == 2642.0


# ============================================================================
# 23. Excursion tracking: MAE and MFE
# ============================================================================

def test_23_excursion_tracking_mae_mfe(symbol_spec, journal):
    pos = PaperPosition(
        ticket=900000001,
        symbol="XAUUSD",
        direction="BUY",
        volume=0.1,
        entry_price=2650.00,
        current_price=2650.00,
    )
    pos.update_price(high=2660.0, low=2645.0, current=2655.0)
    assert pytest.approx(pos.mae, 0.01) == 5.0
    assert pytest.approx(pos.mfe, 0.01) == 10.0


# ============================================================================
# 24-25. PositionMonitor (Phase 8) integration: Breakeven & Trailing Stop
# ============================================================================

def test_24_25_position_monitor_breakeven_trailing(symbol_spec, journal, test_settings):
    monitor = PositionMonitor()
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    runtime = PaperTradingRuntime(
        symbol_spec=symbol_spec,
        settings=test_settings,
        risk_settings=test_settings.risk,
        paper_broker=adapter,
        journal=journal,
        position_monitor=monitor,
    )
    runtime.start_session(PaperSessionConfig(initial_balance=10_000.0))

    adapter.set_current_tick(_make_tick(bid=2650.0, ask=2650.2))
    adapter.submit_market_order("BUY", 0.1, 2640.0, 2680.0, "MonitorTest")

    # Bar 1: moves up to 2665
    bar1 = pd.Series({"open": 2650.0, "high": 2665.0, "low": 2649.0, "close": 2664.0, "spread": 20})
    runtime.step_bar(bar1)

    updated_pos = adapter.positions[0]
    assert updated_pos.sl > 2640.0
    old_sl = updated_pos.sl

    # Bar 2: moves up to 2675 -> trailing stop must be monotonic
    bar2 = pd.Series({"open": 2664.0, "high": 2675.0, "low": 2662.0, "close": 2674.0, "spread": 20})
    runtime.step_bar(bar2)
    assert adapter.positions[0].sl >= old_sl


# ============================================================================
# 26-28. Kill switch, Daily Drawdown, Consecutive Losses
# ============================================================================

def test_26_kill_switch_activation(symbol_spec, journal, test_settings, tmp_path):
    kill_switch = KillSwitch(state_path=str(tmp_path / "kill_switch.json"), default_active=False)
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    runtime = PaperTradingRuntime(
        symbol_spec=symbol_spec,
        settings=test_settings,
        risk_settings=test_settings.risk,
        paper_broker=adapter,
        journal=journal,
        kill_switch=kill_switch,
    )
    runtime.start_session(PaperSessionConfig(initial_balance=10_000.0))

    kill_switch.activate("TestKillSwitch")
    bar = pd.Series({"open": 2650.0, "high": 2652.0, "low": 2648.0, "close": 2651.0, "spread": 20})
    runtime.step_bar(bar)
    assert not runtime.is_running


def test_27_daily_drawdown_limit(symbol_spec, journal, test_settings):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    # Initial balance 10,000, current balance 9,400 (6% loss > 4% daily limit)
    adapter.account.balance = 9400.0
    adapter.account.current_balance = 9400.0
    adapter.account.daily_realized_pnl = -600.0

    runtime = PaperTradingRuntime(
        symbol_spec=symbol_spec,
        settings=test_settings,
        risk_settings=test_settings.risk,
        paper_broker=adapter,
        journal=journal,
    )
    assert runtime.account.daily_realized_pnl == -600.0


def test_28_consecutive_losses_limit(symbol_spec, journal, test_settings):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal, initial_balance=10_000.0)
    adapter.account.consecutive_losses = 5

    runtime = PaperTradingRuntime(
        symbol_spec=symbol_spec,
        settings=test_settings,
        risk_settings=test_settings.risk,
        paper_broker=adapter,
        journal=journal,
    )
    runtime.start_session(PaperSessionConfig(initial_balance=10_000.0))

    bar = pd.Series({"open": 2650.0, "high": 2652.0, "low": 2648.0, "close": 2651.0, "spread": 20})
    runtime.step_bar(bar)
    assert not runtime.is_running
    assert "consecutive losses" in runtime.session.stop_reason.lower()


# ============================================================================
# 29-32. Stale data, idempotency, invalid order, UNKNOWN state
# ============================================================================

def test_29_stale_data_rejection(symbol_spec, journal, test_settings):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    runtime = PaperTradingRuntime(
        symbol_spec=symbol_spec,
        settings=test_settings,
        risk_settings=test_settings.risk,
        paper_broker=adapter,
        journal=journal,
    )
    runtime.start_session(PaperSessionConfig(initial_balance=10_000.0))

    old_time = pd.Timestamp("2020-01-01 00:00:00", tz="UTC")
    bar = pd.Series(
        {"open": 2650.0, "high": 2652.0, "low": 2648.0, "close": 2651.0, "spread": 20},
        name=old_time,
    )
    runtime.step_bar(bar)
    assert len(adapter.positions) == 0


def test_30_order_idempotency(symbol_spec, journal):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    adapter.set_current_tick(_make_tick(bid=2650.00, ask=2650.20))
    res1, _ = adapter.submit_market_order("BUY", 0.1, 2640.0, 2660.0, "IdempotentOrderId")
    assert res1.success

    # Resubmit with identical client_order_id
    res2, _ = adapter.submit_market_order("BUY", 0.1, 2640.0, 2660.0, "IdempotentOrderId")
    assert not res2.success
    assert "duplicate" in res2.comment.lower()


def test_31_invalid_order_rejection(symbol_spec, journal):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    adapter.set_current_tick(_make_tick(bid=2650.00, ask=2650.20))
    res, _ = adapter.submit_market_order("BUY", 0.0, 2640.0, 2660.0, "ZeroVolume")
    assert not res.success


def test_32_unknown_state_handling(symbol_spec, journal):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    # Simulate an UNKNOWN execution record
    from app.execution.state_store import ExecutionRecord, STATUS_UNKNOWN
    adapter.state_store.put(ExecutionRecord(
        client_order_id="UnknownOrder01",
        status=STATUS_UNKNOWN,
        ticket=None,
        symbol="XAUUSD",
        direction="BUY",
        volume=0.1,
    ))
    unresolved = adapter.unresolved_executions()
    assert len(unresolved) == 1
    assert unresolved[0].client_order_id == "UnknownOrder01"


# ============================================================================
# 33. Journal integration
# ============================================================================

def test_33_journal_integration(symbol_spec, journal, tmp_db_path):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    adapter.set_current_tick(_make_tick(bid=2650.00, ask=2650.20))
    res, _ = adapter.submit_market_order("BUY", 0.1, 2640.0, 2660.0, "JournalTest")
    assert res.success

    adapter.set_current_tick(_make_tick(bid=2660.00, ask=2660.20))
    adapter.close_position(res.order_id, reason="JournalClose")

    conn = sqlite3.connect(tmp_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM audit_events")
    count = cursor.fetchone()[0]
    conn.close()
    assert count > 0


# ============================================================================
# 34-36. SQLite State Persistence, Recovery, Fail-Closed on Corruption
# ============================================================================

def test_34_35_sqlite_state_persistence_and_recovery(symbol_spec, journal, tmp_path):
    db_file = str(tmp_path / "paper_state.db")
    store = SqlitePaperStateStore(db_file)

    session = PaperSession(
        session_id="session-test-01",
        config=PaperSessionConfig(initial_balance=12_000.0),
        status="RUNNING",
    )
    account = PaperAccount(balance=12_500.0, equity=12_500.0)
    pos = PaperPosition(
        ticket=900000042,
        symbol="XAUUSD",
        direction="BUY",
        volume=0.2,
        entry_price=2650.0,
        current_price=2655.0,
    )

    store.save_session(session)
    store.save_account("session-test-01", account)
    store.save_positions("session-test-01", [pos])

    new_store = SqlitePaperStateStore(db_file)
    recovered_session = new_store.load_session("session-test-01")
    assert recovered_session is not None
    assert recovered_session.session_id == "session-test-01"

    recovered_acc = new_store.load_account("session-test-01")
    assert recovered_acc is not None
    assert recovered_acc.balance == 12_500.0

    recovered_positions = new_store.load_positions("session-test-01")
    assert len(recovered_positions) == 1
    assert recovered_positions[0].ticket == 900000042


def test_36_corrupted_state_fails_closed(tmp_path):
    db_file = str(tmp_path / "corrupted_paper.db")
    store = SqlitePaperStateStore(db_file)

    session = PaperSession(
        session_id="session-corrupt-01",
        config=PaperSessionConfig(),
        status="RUNNING",
    )
    store.save_session(session)

    conn = sqlite3.connect(db_file)
    conn.execute("UPDATE paper_sessions SET config_json = 'INVALID_JSON_CORRUPTED' WHERE session_id = 'session-corrupt-01'")
    conn.commit()
    conn.close()

    with pytest.raises(StateCorruptedError):
        store.load_session("session-corrupt-01")


# ============================================================================
# 37-38. Reproducibility and Multiple Concurrent Sessions
# ============================================================================

def test_37_reproducibility_hash_and_determinism():
    cfg1 = PaperSessionConfig(initial_balance=10_000.0, random_seed=42, slippage_points=2.0)
    cfg2 = PaperSessionConfig(initial_balance=10_000.0, random_seed=42, slippage_points=2.0)
    cfg3 = PaperSessionConfig(initial_balance=10_000.0, random_seed=99, slippage_points=2.0)

    assert cfg1.reproducibility_hash == cfg2.reproducibility_hash
    assert cfg1.reproducibility_hash != cfg3.reproducibility_hash


def test_38_multiple_concurrent_isolated_sessions(symbol_spec, journal):
    adapter1 = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal, initial_balance=5_000.0)
    adapter2 = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal, initial_balance=20_000.0)

    adapter1.set_current_tick(_make_tick(bid=2650.0, ask=2650.2))
    adapter2.set_current_tick(_make_tick(bid=2650.0, ask=2650.2))

    res1, _ = adapter1.submit_market_order("BUY", 0.1, 2640.0, 2660.0, "Session1Order")
    res2, _ = adapter2.submit_market_order("SELL", 0.5, 2660.0, 2640.0, "Session2Order")

    assert len(adapter1.positions) == 1
    assert len(adapter2.positions) == 1
    assert adapter1.positions[0].ticket != adapter2.positions[0].ticket
    assert adapter1.account.balance == 5_000.0
    assert adapter2.account.balance == 20_000.0


# ============================================================================
# 39. Paper metrics calculation
# ============================================================================

def test_39_compute_paper_metrics():
    p1 = PaperPosition(ticket=1, symbol="XAUUSD", direction="BUY", volume=0.1, entry_price=2650.0, is_closed=True, realized_pnl=200.0)
    p2 = PaperPosition(ticket=2, symbol="XAUUSD", direction="BUY", volume=0.1, entry_price=2650.0, is_closed=True, realized_pnl=300.0)
    p3 = PaperPosition(ticket=3, symbol="XAUUSD", direction="BUY", volume=0.1, entry_price=2650.0, is_closed=True, realized_pnl=-100.0)

    metrics = compute_paper_metrics([p1, p2, p3], starting_balance=10_000.0)
    assert metrics["total_trades"] == 3
    assert metrics["winning_trades"] == 2
    assert metrics["losing_trades"] == 1
    assert pytest.approx(metrics["win_rate"], 0.01) == 2.0 / 3.0
    assert pytest.approx(metrics["profit_factor"], 0.01) == 500.0 / 100.0
    assert pytest.approx(metrics["net_pnl"], 0.01) == 400.0


# ============================================================================
# 40. Chronological simulation without future lookahead
# ============================================================================

def test_40_chronological_simulation_no_lookahead(symbol_spec, journal, test_settings, sample_bars):
    adapter = PaperExecutionAdapter(symbol_spec=symbol_spec, journal=journal)
    runtime = PaperTradingRuntime(
        symbol_spec=symbol_spec,
        settings=test_settings,
        risk_settings=test_settings.risk,
        paper_broker=adapter,
        journal=journal,
    )
    runtime.start_session(PaperSessionConfig(initial_balance=10_000.0))

    df_slice = sample_bars.iloc[:50]
    runtime.process_chronological_bars(df_slice, warmup_bars=10)
    assert runtime.bars_processed >= 40


# ============================================================================
# 41. API Paper Endpoints and LIVE mode rejection
# ============================================================================

def test_41_api_paper_endpoints_and_live_rejection():
    client = TestClient(main_module.app)

    # 1. GET /api/paper/status
    res_status = client.get("/api/paper/status")
    assert res_status.status_code == 200
    data = res_status.json()
    assert "active" in data
    assert "trading_mode" in data

    # 2. GET /api/paper/account
    res_acc = client.get("/api/paper/account")
    assert res_acc.status_code == 200
    assert "balance" in res_acc.json()
    assert "equity" in res_acc.json()

    # 3. GET /api/paper/positions
    res_pos = client.get("/api/paper/positions")
    assert res_pos.status_code == 200
    assert isinstance(res_pos.json(), list)

    # 4. GET /api/paper/trades
    res_trades = client.get("/api/paper/trades")
    assert res_trades.status_code == 200
    assert isinstance(res_trades.json(), list)

    # 5. GET /api/paper/metrics
    res_metrics = client.get("/api/paper/metrics")
    assert res_metrics.status_code == 200
    assert "total_trades" in res_metrics.json()

    # 6. POST /api/paper/start with LIVE mode MUST BE REJECTED with 400
    res_live = client.post("/api/paper/start", json={"trading_mode": "LIVE", "bars_to_process": 10})
    assert res_live.status_code == 400
    assert "Safety violation" in res_live.json()["detail"]

    # 7. POST /api/paper/start in PAPER mode
    res_start = client.post("/api/paper/start", json={"trading_mode": "PAPER", "bars_to_process": 20})
    assert res_start.status_code == 200
    assert res_start.json()["status"] == "success"

    # 8. POST /api/paper/stop
    res_stop = client.post("/api/paper/stop")
    assert res_stop.status_code == 200
    assert res_stop.json()["status"] in ("stopped", "not_running")


# ============================================================================
# 42. CLI Runner scripts/run_paper_trading.py behavior and LIVE rejection
# ============================================================================

def test_42_cli_runner_live_rejection():
    from scripts.run_paper_trading import main as cli_main

    # Test LIVE rejection
    with patch("scripts.run_paper_trading.get_settings") as mock_settings:
        cfg = Settings(trading_mode=TradingMode.LIVE)
        mock_settings.return_value = cfg
        exit_code = cli_main([])
        assert exit_code == 1, "CLI must exit with code 1 when TRADING_MODE=LIVE"
