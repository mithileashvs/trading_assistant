"""
Trading Loop (Phase 8: Paper Trading; also the shared orchestrator for
Phase 9 MT5 Demo and Phase 10 Live).

Ties together every previous phase into one continuously-runnable
cycle: fetch data -> manage any open position -> if flat, evaluate a
new signal -> validate -> execute -> journal. The SAME code runs for
PAPER and LIVE — only ExecutionEngine's internal fill path differs
(simulated vs a real IMT5Client.submit_order call), which is exactly
the point of Phase 4's operating-mode design (section 4).

TradingLoop.__init__ calls Settings.validate_live_safety() up front —
constructing a loop in an unsafe LIVE configuration fails immediately,
before a single cycle ever runs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from typing import Optional

from app.config.settings import Settings, TradingMode
from app.execution.engine import ExecutionEngine, ManagedPosition
from app.journal.journal import SignalLogEntry, TradeJournal, TradeLogEntry
from app.market_data.engine import MarketDataEngine, StaleMarketDataError
from app.mt5.interface import IMT5Client, SymbolSpec
from app.news.calendar import build_news_filter
from app.positions.monitor import ActionType, PositionMonitor
from app.regimes.thresholds import RegimeThresholds
from app.risk.guards import GuardCheckInput, RiskGuardEngine
from app.risk.kill_switch import KillSwitch
from app.risk.validator import TradeValidator
from app.safety.gate import SafetyGate, SafetyGateInput
from app.signals.models import SignalDirection
from app.strategies.context import build_context
from app.strategies.selector import StrategySelector


@dataclass
class CycleSummary:
    timestamp: datetime
    regime: Optional[str] = None
    signal_direction: Optional[str] = None
    signal_approved: Optional[bool] = None
    safety_gate_approved: Optional[bool] = None
    actions_taken: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def describe_account_safety(account, trading_mode: TradingMode) -> str:
    """Human-readable note about whether the connected account is a
    demo or real-money account (section 38 distinguishes Phase 9 "MT5
    Demo" from Phase 10 "Live" specifically on this point). Not every
    broker/account reports this reliably, so an unknown value is
    surfaced honestly rather than assumed safe."""
    if trading_mode != TradingMode.LIVE:
        return f"Trading mode is {trading_mode.value}; no real orders are submitted regardless of account type."
    if account.is_demo is True:
        return "Connected account is reported as a DEMO account."
    if account.is_demo is False:
        return "Connected account is reported as a REAL-MONEY account. Real orders WILL be submitted."
    return "Connected account's demo/real status could not be determined from the broker. Verify manually before proceeding."


class TradingLoop:
    def __init__(
        self,
        settings: Settings,
        client: IMT5Client,
        symbol_spec: SymbolSpec,
        journal: TradeJournal,
        selector: Optional[StrategySelector] = None,
        regime_thresholds: Optional[RegimeThresholds] = None,
        position_monitor: Optional[PositionMonitor] = None,
        execution_engine: Optional[ExecutionEngine] = None,
        kill_switch: Optional[KillSwitch] = None,
    ):
        settings.validate_live_safety()  # fails fast for an unsafe LIVE config

        self.settings = settings
        self.client = client
        self.symbol_spec = symbol_spec
        self.market_data = MarketDataEngine(client, settings)
        self.selector = selector or StrategySelector()
        self.regime_thresholds = regime_thresholds
        self.journal = journal
        self.position_monitor = position_monitor or PositionMonitor()
        # kill_switch is constructed before execution_engine specifically
        # so the default ExecutionEngine can be given the SAME instance
        # (see ExecutionEngine's _NullKillSwitch docstring for why a
        # freshly-defaulted, unrelated KillSwitch would be unsafe here).
        self.kill_switch = kill_switch or KillSwitch(default_active=False)
        self.execution_engine = execution_engine or ExecutionEngine(
            client, symbol_spec, settings.trading_mode, kill_switch=self.kill_switch
        )
        self.validator = TradeValidator(
            settings.risk,
            guard_engine=RiskGuardEngine(settings.risk),
            news_filter=build_news_filter(
                settings.news_calendar_path,
                minutes_before=settings.news_blackout_minutes_before,
                minutes_after=settings.news_blackout_minutes_after,
            ),
        )
        # Independent second gate (audit sections 17-18): deliberately a
        # SEPARATE code path from self.validator, re-checking freshly
        # gathered state right before order submission. Even when the
        # risk engine has approved, this can still reject -- and that
        # rejection is final. Note: it reads news state via
        # self.validator.news_filter (the single source of truth for
        # "the configured news filter") rather than holding its own
        # separate reference -- two attributes that both need to be
        # kept in sync on override is exactly the kind of footgun that
        # caused a real test bug during this phase's own development
        # (see the README's safety-audit section).
        self.safety_gate = SafetyGate()

        self._journal_trade_ids: dict[int, int] = {}  # execution ticket -> journal row id
        self._current_day = None
        self._day_start_equity: Optional[float] = None
        self._current_week = None
        self._week_start_equity: Optional[float] = None

        try:
            account = client.get_account_info()
            self.account_safety_note = describe_account_safety(account, settings.trading_mode)
        except Exception:  # noqa: BLE001 - best-effort; run_once() will surface real connection errors
            self.account_safety_note = "Could not determine account safety status at startup."

    @property
    def news_filter(self):
        """The single source of truth for the configured news filter is
        self.validator.news_filter -- this property exists so
        `loop.news_filter = X` (a natural thing to write, especially in
        tests) always changes the SAME object SafetyGate reads from,
        instead of silently creating a second, divergent reference.
        (That divergence was a real bug caught while wiring SafetyGate
        into this loop -- see the README's safety-audit section.)"""
        return self.validator.news_filter

    @news_filter.setter
    def news_filter(self, value) -> None:
        self.validator.news_filter = value

    def _refresh_equity_baselines(self, equity: float, now: datetime) -> None:
        day = now.date()
        if self._current_day != day:
            self._current_day = day
            self._day_start_equity = equity
        week = now.isocalendar()[:2]
        if self._current_week != week:
            self._current_week = week
            self._week_start_equity = equity

    def run_once(self) -> CycleSummary:
        now = datetime.now(timezone.utc)
        summary = CycleSummary(timestamp=now)

        if self.settings.kill_switch or self.kill_switch.is_active():
            summary.errors.append("Kill switch is active; skipping this cycle.")
            return summary

        try:
            account = self.client.get_account_info()
        except Exception as exc:  # noqa: BLE001
            summary.errors.append(f"Could not fetch account info: {exc}")
            return summary

        self._refresh_equity_baselines(account.equity, now)

        try:
            tick = self.market_data.get_tick()
        except StaleMarketDataError as exc:
            summary.errors.append(f"Market data stale: {exc}")
            return summary

        # --- 1. Manage any open position -----------------------------------------
        open_positions = self.execution_engine.get_open_positions()
        current_atr = None
        for position in open_positions:
            self._manage_position(position, tick, summary)

        # --- 2. If flat, evaluate for a new entry -----------------------------------
        open_positions = self.execution_engine.get_open_positions()  # re-check after any closes above
        if not open_positions:
            self._evaluate_new_entry(account, now, summary)

        return summary

    def _manage_position(self, position: ManagedPosition, tick, summary: CycleSummary) -> None:
        actions = self.position_monitor.evaluate(position, tick, self.symbol_spec.tick_size)
        for action in actions:
            if action.type == ActionType.MOVE_TO_BREAKEVEN:
                result = self.execution_engine.modify_position(position.ticket, action.new_stop_loss, position.take_profit)
                if result.success:
                    self.position_monitor.mark_breakeven_applied(position.ticket)
                    summary.actions_taken.append(f"Moved ticket {position.ticket} to breakeven.")
            elif action.type == ActionType.TRAIL_STOP:
                result = self.execution_engine.modify_position(position.ticket, action.new_stop_loss, position.take_profit)
                if result.success:
                    summary.actions_taken.append(f"Trailed stop for ticket {position.ticket}.")
            elif action.type == ActionType.PARTIAL_EXIT:
                result = self.execution_engine.close_position_partial(position.ticket, action.partial_volume)
                if result.success:
                    self.position_monitor.mark_partial_taken(position.ticket)
                    summary.actions_taken.append(f"Took partial exit on ticket {position.ticket}.")
            elif action.type == ActionType.FULL_EXIT:
                self._close_and_journal(position, action.reason, summary)

        # Hard SL/TP check against the current tick (in addition to
        # whatever the broker/paper engine enforces natively).
        price = tick.bid if position.direction == "BUY" else tick.ask
        hit_sl = (
            position.stop_loss is not None
            and ((position.direction == "BUY" and price <= position.stop_loss)
                 or (position.direction == "SELL" and price >= position.stop_loss))
        )
        hit_tp = (
            position.take_profit is not None
            and ((position.direction == "BUY" and price >= position.take_profit)
                 or (position.direction == "SELL" and price <= position.take_profit))
        )
        if hit_sl:
            self._close_and_journal(position, "STOP_LOSS", summary)
        elif hit_tp:
            self._close_and_journal(position, "TAKE_PROFIT", summary)

    def _close_and_journal(self, position: ManagedPosition, reason: str, summary: CycleSummary) -> None:
        result = self.execution_engine.close_position(position.ticket, reason=reason)
        if not result.success:
            summary.errors.append(f"Failed to close ticket {position.ticket}: {result.comment}")
            return
        journal_id = self._journal_trade_ids.pop(position.ticket, None)
        if journal_id is not None and result.price is not None:
            ticks = (
                (result.price - position.price_open) / self.symbol_spec.tick_size if position.direction == "BUY"
                else (position.price_open - result.price) / self.symbol_spec.tick_size
            )
            pnl = ticks * self.symbol_spec.tick_value * position.volume
            r_multiple = (pnl / position.monetary_risk) if position.monetary_risk else None
            self.journal.close_trade(
                journal_id, close_time=datetime.now(timezone.utc), exit_price=result.price,
                exit_reason=reason, pnl=pnl, r_multiple=r_multiple,
            )
        self.position_monitor.forget(position.ticket)
        summary.actions_taken.append(f"Closed ticket {position.ticket} ({reason}).")

    def _evaluate_new_entry(self, account, now: datetime, summary: CycleSummary) -> None:
        try:
            context = build_context(self.market_data, self.regime_thresholds, bars=250)
        except Exception as exc:  # noqa: BLE001
            summary.errors.append(f"Could not build strategy context: {exc}")
            return

        summary.regime = context.h4_regime.regime.value
        best = self.selector.best_signal(context)
        summary.signal_direction = best.direction.value

        day_start_dt = datetime.combine(now.date(), dtime.min, tzinfo=timezone.utc)
        trades_today = len(self.journal.trades_since(day_start_dt))
        recent = self.journal.recent_trades(20)

        guard_input = GuardCheckInput(
            equity=account.equity, day_start_equity=self._day_start_equity or account.equity,
            week_start_equity=self._week_start_equity or account.equity,
            trades_today_count=trades_today, open_positions_count=0,
            current_spread_points=(context.current_ask - context.current_bid) / self.symbol_spec.tick_size if context.current_ask and context.current_bid else 0.0,
            mt5_connected=self.client.is_connected(), broker_trade_allowed=account.trade_allowed,
            market_data_fresh=True, kill_switch_active=self.settings.kill_switch or self.kill_switch.is_active(),
            recent_trades=recent,
        )
        validation = self.validator.validate(best, context, self.symbol_spec, account, guard_input)
        summary.signal_approved = validation.approved

        self.journal.log_signal(SignalLogEntry(
            timestamp=now, symbol=self.symbol_spec.name, direction=best.direction.value,
            strategy=best.strategy, regime=summary.regime, score=best.score,
            entry=best.entry, stop_loss=best.stop_loss, take_profit=best.take_profit,
            session=context.m15_features.get("session"), approved=validation.approved,
            rejection_reasons=validation.rejection_reasons,
        ))

        if not validation.approved or best.direction == SignalDirection.NO_SIGNAL:
            return

        # --- Independent Safety Gate (audit sections 17-19) ------------------------
        # A SEPARATE approval, from freshly-gathered state (not reused
        # from `validation`/`guard_input` above) -- time has passed since
        # the risk engine's check, however briefly, and this gate exists
        # specifically to not assume nothing changed in that window.
        safety_result = self._run_safety_gate(best, validation, now)
        summary.safety_gate_approved = safety_result.approved
        if not safety_result.approved:
            summary.errors.append(
                f"SafetyGate rejected the trade (error={safety_result.is_error}): "
                + "; ".join(safety_result.reasons)
            )
            return

        client_order_id = f"{self.symbol_spec.name}-{now.isoformat()}-{best.strategy}"
        monetary_risk = account.equity * (self.settings.risk.risk_per_trade_pct / 100.0)
        result, position = self.execution_engine.submit_market_order(
            direction=best.direction.value, volume=validation.lots, stop_loss=best.stop_loss,
            take_profit=best.take_profit, client_order_id=client_order_id, strategy=best.strategy,
            regime=summary.regime, monetary_risk=monetary_risk,
        )
        if not result.success or position is None:
            summary.errors.append(f"Order submission failed: {result.comment}")
            return

        journal_id = self.journal.open_trade(TradeLogEntry(
            symbol=self.symbol_spec.name, direction=position.direction, strategy=position.strategy,
            regime=position.regime, score=best.score, open_time=position.open_time,
            entry_price=position.price_open, lots=position.volume, stop_loss=position.stop_loss,
            take_profit=position.take_profit, risk_pct=self.settings.risk.risk_per_trade_pct,
            session=context.m15_features.get("session"),
            atr=context.m15_features["volatility"].get("atr"), adx=context.m15_features["trend"].get("adx"),
            rsi=context.m15_features["momentum"].get("rsi"), ema_20=context.m15_features["trend"].get("ema_20"),
            ema_50=context.m15_features["trend"].get("ema_50"), ema_200=context.m15_features["trend"].get("ema_200"),
        ))
        self._journal_trade_ids[position.ticket] = journal_id
        summary.actions_taken.append(f"Opened {position.direction} ticket {position.ticket} ({position.strategy}).")

    def _run_safety_gate(self, signal, validation, now: datetime):
        """Builds a SafetyGateInput from FRESHLY gathered state (not
        reused from the risk-engine validation above) and runs it
        through the independent SafetyGate. Any exception while
        gathering this fresh state is itself treated as a safety
        failure, not allowed to propagate and accidentally skip the
        gate."""
        try:
            fresh_account = self.client.get_account_info()
            fresh_tick = self.market_data.get_tick()
            fresh_news_status = self.news_filter.check(now)
            fresh_open_positions = len(self.execution_engine.get_open_positions())

            day_start_equity = self._day_start_equity or fresh_account.equity
            daily_pnl = fresh_account.equity - day_start_equity
            daily_loss_pct = (-daily_pnl / day_start_equity * 100.0) if daily_pnl < 0 and day_start_equity > 0 else 0.0

            spread_points = (fresh_tick.ask - fresh_tick.bid) / self.symbol_spec.tick_size if self.symbol_spec.tick_size else 0.0

            gate_input = SafetyGateInput(
                account_equity=fresh_account.equity,
                account_trade_allowed=fresh_account.trade_allowed,
                account_is_demo=fresh_account.is_demo,
                symbol_trade_allowed=self.symbol_spec.trade_allowed,
                volume_min=self.symbol_spec.volume_min,
                volume_max=self.symbol_spec.volume_max,
                volume_step=self.symbol_spec.volume_step,
                stops_level_points=self.symbol_spec.stops_level_points,
                tick_size=self.symbol_spec.tick_size,
                direction=signal.direction.value,
                volume=validation.lots,
                entry_price=signal.entry,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                current_spread_points=spread_points,
                max_spread_points=self.settings.risk.max_spread_points,
                market_data_fresh=True,
                news_state=fresh_news_status.state,
                kill_switch_active=self.settings.kill_switch or self.kill_switch.is_active(),
                daily_loss_pct=daily_loss_pct,
                max_daily_loss_pct=self.settings.risk.max_daily_loss_pct,
                open_positions_count=fresh_open_positions,
                max_open_positions=self.settings.risk.max_open_positions,
            )
            return self.safety_gate.evaluate(gate_input)
        except Exception as exc:  # noqa: BLE001 - gathering-failure also fails closed
            from app.safety.gate import SafetyGateResult
            return SafetyGateResult(
                approved=False, is_error=True, checks={},
                reasons=[f"Could not gather fresh state for the safety gate: {exc}"],
            )

    def run(self, iterations: int, sleep_seconds: float = 60.0) -> list[CycleSummary]:
        summaries = []
        for i in range(iterations):
            summaries.append(self.run_once())
            if i < iterations - 1:
                time.sleep(sleep_seconds)
        return summaries
