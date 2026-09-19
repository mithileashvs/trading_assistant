"""
Paper Trading Runtime (Phase 12).

Orchestrates deterministic, simulation-only Paper Trading using controlled chronological
market data (bars) or read-only quotes.

STRICT SAFETY GUARANTEES:
- TradingMode.LIVE is strictly rejected at startup AND at every execution boundary.
- Structurally isolated from real broker execution (zero calls to MT5 or ExecutionEngine).
- Integrates the full production pipeline: Features -> Regimes -> Strategies ->
  Scoring -> Risk Validation -> Safety Gate -> Paper Execution -> Phase 8 Position
  Management -> Journal / Audit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import pandas as pd

from app.backtesting.costs import ExecutionCosts
from app.config.settings import RiskSettings, Settings, TradingMode, get_settings
from app.features.engine import compute_features
from app.journal.journal import AuditCategory, AuditEvent, TradeJournal
from app.mt5.interface import AccountInfo, SymbolSpec, Tick
from app.paper.account import PaperAccount
from app.paper.broker import PaperExecutionAdapter
from app.paper.metrics import compute_paper_metrics
from app.paper.positions import PaperPosition
from app.paper.session import PaperSession, PaperSessionConfig
from app.paper.store import InMemoryPaperStateStore, PaperStateStore, SqlitePaperStateStore
from app.positions.monitor import ActionType, PositionMonitor
from app.positions.state_store import InMemoryPositionMonitorStateStore
from app.regimes.detector import RegimeThresholds
from app.risk.guards import GuardCheckInput, RiskGuardEngine
from app.risk.kill_switch import KillSwitch
from app.risk.validator import TradeValidator
from app.safety.gate import SafetyGate, SafetyGateInput
from app.signals.models import SignalDirection
from app.strategies.context import build_context
from app.strategies.selector import StrategySelector

logger = logging.getLogger("xau_trader.paper_runtime")


class PaperTradingRuntime:
    """Simulation-only Paper Trading runtime coordinator."""

    def __init__(
        self,
        symbol_spec: SymbolSpec,
        settings: Optional[Settings] = None,
        risk_settings: Optional[RiskSettings] = None,
        costs: Optional[ExecutionCosts] = None,
        session_id: str = "paper_default_session",
        initial_balance: float = 10_000.0,
        store: Optional[PaperStateStore] = None,
        state_store: Optional[PaperStateStore] = None,
        journal: Optional[TradeJournal] = None,
        kill_switch: Optional[KillSwitch] = None,
        position_monitor: Optional[PositionMonitor] = None,
        trading_mode: TradingMode = TradingMode.PAPER,
        paper_broker: Optional[PaperExecutionAdapter] = None,
    ):
        # 1. Startup Safety: Hard rejection of LIVE mode
        if trading_mode == TradingMode.LIVE or (settings and settings.trading_mode == TradingMode.LIVE):
            raise RuntimeError(
                "Safety violation: TradingMode.LIVE cannot be used with PaperTradingRuntime"
            )

        self.trading_mode = trading_mode
        self.settings = settings or get_settings()
        self.symbol_spec = symbol_spec
        self.risk_settings = risk_settings or self.settings.risk
        self.costs = costs or ExecutionCosts()
        self.store = state_store or store or InMemoryPaperStateStore()
        self.journal = journal or TradeJournal(self.settings.database_url.replace("sqlite:///", ""))
        self.kill_switch = kill_switch or KillSwitch(default_active=False, journal=self.journal)

        # Re-check settings trading_mode
        if self.settings.trading_mode == TradingMode.LIVE:
            raise RuntimeError(
                "Safety violation: TradingMode.LIVE cannot be used with PaperTradingRuntime"
            )

        # Initialize or restore Session
        self.config = PaperSessionConfig(
            session_id=session_id,
            symbol=self.symbol_spec.name,
            initial_balance=initial_balance,
        )
        existing_session = self.store.get_session(session_id)
        if existing_session:
            self.session = existing_session
            self.account = self.store.load_account(session_id) or PaperAccount(
                balance=initial_balance,
                current_balance=initial_balance,
                equity=initial_balance,
                free_margin=initial_balance,
            )
        else:
            self.session = PaperSession(config=self.config)
            self.account = PaperAccount(
                balance=initial_balance,
                current_balance=initial_balance,
                equity=initial_balance,
                free_margin=initial_balance,
            )
            self.store.save_session(self.session)
            self.store.save_account(session_id, self.account)

        # Isolated Paper Execution Adapter
        if paper_broker is not None:
            self.adapter = paper_broker
            self.account = paper_broker.account
        else:
            self.adapter = PaperExecutionAdapter(
                symbol_spec=self.symbol_spec,
                account=self.account,
                costs=self.costs,
                trading_mode=TradingMode.PAPER,
                kill_switch=self.kill_switch,
                journal=self.journal,
                initial_balance=initial_balance,
            )
        self.broker = self.adapter

        # Phase 8 Position Monitor
        self.position_monitor = position_monitor or PositionMonitor(
            state_store=InMemoryPositionMonitorStateStore()
        )

        # Production Strategy Pipeline
        self.selector = StrategySelector()
        self.validator = TradeValidator(self.risk_settings, guard_engine=RiskGuardEngine(self.risk_settings))
        self.safety_gate = SafetyGate()
        self.regime_thresholds = RegimeThresholds()

        self.rejected_signals_count = 0
        self.unknown_actions_count = 0
        self._bars_processed_counter = 0

    @property
    def is_running(self) -> bool:
        return self.session is not None and self.session.status == "RUNNING"

    @property
    def bars_processed(self) -> int:
        if self.session is not None:
            return self.session.bars_processed
        return self._bars_processed_counter

    def start_session(self, config: Optional[PaperSessionConfig] = None) -> PaperSession:
        """Starts or resets a paper trading session."""
        self._assert_paper_mode()
        if config is not None:
            self.config = config
            self.session = PaperSession(config=self.config)
            if self.account is None:
                self.account = PaperAccount(
                    balance=config.initial_balance,
                    current_balance=config.initial_balance,
                    equity=config.initial_balance,
                    free_margin=config.initial_balance,
                )
                self.adapter.account = self.account
        self.session.start()
        self.store.save_session(self.session)
        self.store.save_account(self.session.session_id, self.account)
        return self.session

    def stop_session(self, reason: str = "") -> PaperSession:
        """Stops the current paper trading session and records final metrics."""
        metrics = compute_paper_metrics(self.account, self.rejected_signals_count, self.unknown_actions_count)
        self.session.stop(metrics=metrics, reason=reason)
        self.store.save_session(self.session)
        self.store.save_account(self.session.session_id, self.account)
        self.store.save_positions(self.session.session_id, self.account.closed_positions)
        return self.session

    def _assert_paper_mode(self) -> None:
        """Enforces that trading_mode is PAPER at every security-sensitive execution boundary."""
        if self.trading_mode != TradingMode.PAPER or (self.settings and self.settings.trading_mode != TradingMode.PAPER):
            raise RuntimeError(
                "Safety violation: Paper Trading is simulation-only and cannot run in LIVE mode."
            )

    def step_bar(
        self,
        bar: pd.Series,
        bar_time: Optional[datetime] = None,
        check_stale: bool = True,
    ) -> None:
        """Evaluates a single chronological bar against risk guards, SL/TP triggers, and PositionMonitor."""
        self._assert_paper_mode()

        if not self.is_running:
            self.start_session()

        # Check Kill Switch
        if self.kill_switch.is_active():
            self.stop_session("Kill switch active")
            return

        # Check consecutive loss threshold
        max_losses = getattr(self.risk_settings, "max_consecutive_losses", 5)
        if self.account.consecutive_losses >= max_losses:
            self.stop_session("Max consecutive losses reached")
            return

        # Determine bar timestamp
        if bar_time is None:
            if hasattr(bar, "name") and isinstance(bar.name, (datetime, pd.Timestamp)):
                bar_time = pd.Timestamp(bar.name).to_pydatetime()
            else:
                bar_time = datetime.now(timezone.utc)
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=timezone.utc)

        # Check stale data: if timestamp older than 7 days from now
        if check_stale:
            now_utc = datetime.now(timezone.utc)
            if (now_utc - bar_time).total_seconds() > (7 * 86400):
                # Stale bar detected: reject / skip processing without crashing
                return

        spec = self.symbol_spec
        bar_open = float(bar["open"])
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])

        # 1. Check open positions against bar price action (SL/TP, same-bar conservative, gap open)
        open_tickets = list(self.account.open_positions.keys())
        for ticket in open_tickets:
            pos = self.account.open_positions.get(ticket)
            if pos is None:
                continue

            pos.update_price(bar_high, bar_low, bar_close)

            sl_hit = False
            tp_hit = False
            exit_price = None

            if pos.direction == "BUY":
                if pos.stop_loss is not None and bar_low <= pos.stop_loss:
                    sl_hit = True
                if pos.take_profit is not None and bar_high >= pos.take_profit:
                    tp_hit = True

                if sl_hit and tp_hit:
                    # Conservative Phase 10 same-bar rule: SL triggers first
                    exit_price = bar_open if bar_open < pos.stop_loss else pos.stop_loss
                    self.adapter.close_position(ticket, reason="SL", exit_price=exit_price)
                elif sl_hit:
                    exit_price = bar_open if bar_open < pos.stop_loss else pos.stop_loss
                    self.adapter.close_position(ticket, reason="SL", exit_price=exit_price)
                elif tp_hit:
                    exit_price = bar_open if bar_open > pos.take_profit else pos.take_profit
                    self.adapter.close_position(ticket, reason="TP", exit_price=exit_price)

            else:  # SELL
                if pos.stop_loss is not None and bar_high >= pos.stop_loss:
                    sl_hit = True
                if pos.take_profit is not None and bar_low <= pos.take_profit:
                    tp_hit = True

                if sl_hit and tp_hit:
                    exit_price = bar_open if bar_open > pos.stop_loss else pos.stop_loss
                    self.adapter.close_position(ticket, reason="SL", exit_price=exit_price)
                elif sl_hit:
                    exit_price = bar_open if bar_open > pos.stop_loss else pos.stop_loss
                    self.adapter.close_position(ticket, reason="SL", exit_price=exit_price)
                elif tp_hit:
                    exit_price = bar_open if bar_open < pos.take_profit else pos.take_profit
                    self.adapter.close_position(ticket, reason="TP", exit_price=exit_price)

        # 2. Update tick at bar close on adapter
        spread_pts = float(bar.get("spread", 20.0))
        simulated_tick = Tick(
            symbol=spec.name,
            time=bar_time,
            bid=bar_close,
            ask=round(bar_close + (spread_pts * spec.tick_size), 4),
            last=bar_close,
            volume=float(bar.get("tick_volume", 100)),
        )
        self.adapter.set_current_tick(simulated_tick)

        # 3. Position Management via Phase 8 PositionMonitor (breakeven / trailing stop)
        open_tickets = list(self.account.open_positions.keys())
        for ticket in open_tickets:
            pos = self.account.open_positions.get(ticket)
            if pos is None:
                continue
            actions = self.position_monitor.evaluate(pos.to_managed_position(), simulated_tick, spec.tick_size)
            for action in actions:
                if action.type == ActionType.MOVE_TO_BREAKEVEN:
                    self.adapter.modify_position(ticket, stop_loss=action.new_stop_loss, take_profit=pos.take_profit)
                elif action.type == ActionType.TRAIL_STOP:
                    self.adapter.modify_position(ticket, stop_loss=action.new_stop_loss, take_profit=pos.take_profit)
                elif action.type == ActionType.PARTIAL_EXIT and action.partial_volume is not None:
                    self.adapter.close_position_partial(ticket, action.partial_volume)
                elif action.type == ActionType.FULL_EXIT:
                    self.adapter.close_position(ticket, reason="FULL_EXIT")

        self.session.bars_processed += 1
        self._bars_processed_counter += 1

    def process_chronological_bars(
        self,
        m15_df: pd.DataFrame,
        warmup_bars: int = 100,
        step: int = 4,
    ) -> dict[str, Any]:
        """Executes a deterministic simulation over controlled chronological market data bars."""
        self._assert_paper_mode()
        if not self.is_running:
            self.start_session()

        n = len(m15_df)
        warmup = min(warmup_bars, max(0, n - 10))

        for i in range(warmup, n):
            self._assert_paper_mode()
            bar = m15_df.iloc[i]
            bar_time = m15_df.index[i]
            if not isinstance(bar_time, datetime):
                bar_time = pd.Timestamp(bar_time).to_pydatetime()
            if bar_time.tzinfo is None:
                bar_time = bar_time.replace(tzinfo=timezone.utc)

            self.step_bar(bar, bar_time=bar_time, check_stale=False)

            # Signal generation when flat
            if len(self.account.open_positions) == 0 and (i % step == 0) and not self.kill_switch.is_active():
                historical_slice = m15_df.iloc[: i + 1]
                self._evaluate_bar_signal(historical_slice, bar_time, i)

        metrics = compute_paper_metrics(self.account, self.rejected_signals_count, self.unknown_actions_count)
        self.stop_session("Chronological bars complete")
        return metrics

    def _evaluate_bar_signal(self, historical_slice: pd.DataFrame, bar_time: datetime, bar_index: int) -> None:
        """Evaluates features, regimes, strategies, risk validation, and safety gates for a potential new trade."""
        spec = self.symbol_spec
        account_info = self.account.as_account_info()

        try:
            snapshot = compute_features(historical_slice)
            ctx = build_context(historical_slice, snapshot, self.regime_thresholds)
        except Exception:
            return

        signal = self.selector.generate_best(ctx)
        if signal.direction == SignalDirection.NO_SIGNAL:
            return

        direction_str = "BUY" if signal.direction == SignalDirection.BUY else "SELL"

        # TradeValidator check
        validation = self.validator.validate(
            signal=signal,
            snapshot=snapshot,
            account=account_info,
            symbol_spec=spec,
            open_positions=[p.to_managed_position() for p in self.account.open_positions.values()],
        )

        if not validation.approved:
            self.rejected_signals_count += 1
            return

        # SafetyGate check
        gate_input = SafetyGateInput(
            trading_mode=TradingMode.PAPER,
            kill_switch_active=self.kill_switch.is_active(),
            market_data_fresh=True,
            news_blackout=False,
            account=account_info,
            daily_loss_pct=self.risk_settings.max_daily_loss_pct,
            weekly_loss_pct=self.risk_settings.max_weekly_loss_pct,
            symbol_spec=spec,
            direction=direction_str,
            lots=validation.position_size_lots,
            entry_price=signal.entry_price or float(historical_slice.iloc[-1]["close"]),
            stop_loss=signal.stop_loss or 0.0,
            take_profit=signal.take_profit,
            signal_score=signal.score,
            open_positions=len(self.account.open_positions),
            consecutive_losses=self.account.consecutive_losses,
        )

        gate_res = self.safety_gate.evaluate(gate_input)
        if not gate_res.approved:
            self.rejected_signals_count += 1
            return

        # Submit to isolated PaperExecutionAdapter
        client_order_id = f"paper_{bar_index}_{self.config.session_id}_{signal.strategy_name}"
        self.adapter.submit_market_order(
            direction=direction_str,
            volume=validation.position_size_lots,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            client_order_id=client_order_id,
            strategy=signal.strategy_name,
            regime=ctx.h4_regime.regime.value if hasattr(ctx.h4_regime.regime, "value") else str(ctx.h4_regime.regime),
        )
