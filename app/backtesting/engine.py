"""
Backtesting Engine (section 24).

Walks forward bar-by-bar through M15 history. At each evaluated bar,
H1/H4 context is derived using ONLY fully-closed higher-timeframe
candles up to that point (app.backtesting.resampling), a decision is
made from that bar's close, and any resulting entry is filled at the
NEXT bar's open — never the same bar whose close produced the signal.
This two-step decide-then-fill pattern, plus resample_closed_only(),
is what keeps the whole engine free of look-ahead bias.

Reuses the exact same feature engine, regime detector, strategy
selector, scoring, position sizing, and trade validator as live/paper
trading — a backtest and a live run differ only in where market data
and fills come from.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from typing import Optional

import pandas as pd

from app.backtesting.costs import ExecutionCosts, apply_entry_costs, apply_exit_costs, commission_cost, swap_cost
from app.backtesting.resampling import resample_closed_only
from app.backtesting.results import BacktestResult, BacktestTrade, EquityPoint
from app.config.settings import RiskSettings
from app.features.engine import MIN_BARS_REQUIRED, InsufficientDataError, compute_features
from app.mt5.interface import AccountInfo, SymbolSpec
from app.news.filter import AlwaysClearNewsFilter, NewsFilter
from app.regimes.detector import detect_regime
from app.regimes.thresholds import RegimeThresholds
from app.risk.guards import GuardCheckInput, RiskGuardEngine
from app.risk.history import InMemoryTradeHistory, TradeRecord
from app.risk.validator import TradeValidator
from app.signals.models import SignalDirection
from app.signals.scoring import ScoringWeights
from app.strategies.context import StrategyContext
from app.strategies.selector import StrategySelector


@dataclass
class BacktestConfig:
    starting_balance: float = 10_000.0
    warmup_bars: int = MIN_BARS_REQUIRED * 16  # enough M15 bars for a full 210-bar H4 history
    resample_lookback_bars: int = 4200  # bound resample cost regardless of total dataset length
    step: int = 4  # evaluate every Nth M15 bar for new entries (perf knob); 4 = hourly
    max_bars: Optional[int] = None
    allowed_sessions: Optional[list[str]] = None  # None = no restriction
    min_score_label: str = "VALID"
    min_risk_reward: float = 1.2


class BacktestEngine:
    def __init__(
        self,
        symbol_spec: SymbolSpec,
        risk_settings: RiskSettings,
        costs: ExecutionCosts | None = None,
        config: BacktestConfig | None = None,
        selector: StrategySelector | None = None,
        regime_thresholds: RegimeThresholds | None = None,
        scoring_weights: ScoringWeights | None = None,
        news_filter: NewsFilter | None = None,
    ):
        self.symbol_spec = symbol_spec
        self.risk_settings = risk_settings
        self.costs = costs or ExecutionCosts()
        self.config = config or BacktestConfig()
        self.selector = selector or StrategySelector()
        self.regime_thresholds = regime_thresholds
        self.scoring_weights = scoring_weights
        # BACKTEST-ONLY DEFAULT: unlike TradeValidator's own default
        # (UnavailableNewsFilter, which correctly fails closed for
        # every live/paper path), a backtest has no live news-data
        # provider and never will, so defaulting to UnavailableNewsFilter
        # here would silently block 100% of backtest entries at the
        # news gate regardless of strategy quality. AlwaysClearNewsFilter
        # is wired ONLY here, is named/documented as backtest-only, and
        # does not fabricate any historical news data -- it simply lets
        # the news gate pass so the rest of the validation pipeline
        # (score, risk:reward, sizing, risk guards) can be exercised.
        # Callers that want to specifically test the news gate itself
        # (e.g. verifying a backtest run stays flat under a simulated
        # blackout) can still pass UnavailableNewsFilter() or
        # BlockedNewsFilter() explicitly via this parameter.
        self.news_filter = news_filter or AlwaysClearNewsFilter()
        self.validator = TradeValidator(
            risk_settings,
            guard_engine=RiskGuardEngine(risk_settings),
            news_filter=self.news_filter,
            min_score_label=self.config.min_score_label,
            min_risk_reward=self.config.min_risk_reward,
        )

    def run(self, m15_df: pd.DataFrame) -> BacktestResult:
        cfg = self.config
        costs = self.costs
        spec = self.symbol_spec

        n = len(m15_df) if cfg.max_bars is None else min(len(m15_df), cfg.max_bars)

        equity = cfg.starting_balance
        history = InMemoryTradeHistory()
        trades: list[BacktestTrade] = []
        equity_curve: list[EquityPoint] = []

        open_position: Optional[dict] = None
        pending_entry: Optional[dict] = None

        day_start_equity = equity
        current_day = None
        week_start_equity = equity
        current_week = None

        for i in range(n):
            bar = m15_df.iloc[i]
            bar_time = m15_df.index[i]

            bar_date = bar_time.date()
            if current_day != bar_date:
                current_day = bar_date
                day_start_equity = equity
            iso_week = bar_time.isocalendar()[:2]
            if current_week != iso_week:
                current_week = iso_week
                week_start_equity = equity

            # --- 1. Fill any pending entry at THIS bar's open (decided at the previous bar's close) ---
            if pending_entry is not None and open_position is None:
                fill = apply_entry_costs(bar["open"], pending_entry["direction"], costs, spec.tick_size)
                open_position = {**pending_entry, "entry_price": fill.price, "entry_time": bar_time, "mae": 0.0, "mfe": 0.0}
                pending_entry = None

            # --- 2. Manage an open position against THIS bar's high/low ---
            if open_position is not None:
                open_position = self._update_and_maybe_exit(open_position, bar, bar_time, spec, costs, trades, history)
                if open_position is not None and open_position.get("_closed"):
                    equity += open_position["_realized"]
                    open_position = None

            # --- 3. Evaluate for a new entry (decision made from this bar's close) ---
            if open_position is None and pending_entry is None and i >= cfg.warmup_bars and i % cfg.step == 0:
                pending_entry = self._evaluate_entry(
                    m15_df, i, bar, bar_time, bar_date, equity, day_start_equity, week_start_equity, history
                )

            equity_curve.append(EquityPoint(time=bar_time, equity=equity + self._floating_pnl(open_position, bar, spec)))

        # Force-close anything still open at the end of the data.
        if open_position is not None:
            last_bar = m15_df.iloc[n - 1]
            last_time = m15_df.index[n - 1]
            trade, realized = self._close_position(open_position, last_bar["close"], last_time, "END_OF_DATA", spec, costs)
            trades.append(trade)
            history.record(TradeRecord(closed_at=last_time, symbol=spec.name, direction=trade.direction, pnl=trade.pnl, r_multiple=trade.r_multiple))
            equity += realized
            if equity_curve:
                equity_curve[-1] = EquityPoint(time=last_time, equity=equity)

        return BacktestResult(
            symbol=spec.name,
            starting_balance=cfg.starting_balance,
            ending_balance=equity,
            trades=trades,
            equity_curve=equity_curve,
            bars_evaluated=n,
            config=asdict(cfg),
        )

    # ------------------------------------------------------------------
    def _floating_pnl(self, open_position: Optional[dict], bar, spec: SymbolSpec) -> float:
        if open_position is None:
            return 0.0
        direction = open_position["direction"]
        price = bar["close"]
        ticks = (price - open_position["entry_price"]) / spec.tick_size if direction == "BUY" else (open_position["entry_price"] - price) / spec.tick_size
        return ticks * spec.tick_value * open_position["lots"]

    def _update_and_maybe_exit(self, pos: dict, bar, bar_time, spec: SymbolSpec, costs: ExecutionCosts, trades: list, history: InMemoryTradeHistory) -> Optional[dict]:
        direction = pos["direction"]
        if direction == "BUY":
            favorable = bar["high"] - pos["entry_price"]
            adverse = pos["entry_price"] - bar["low"]
        else:
            favorable = pos["entry_price"] - bar["low"]
            adverse = bar["high"] - pos["entry_price"]
        pos["mfe"] = max(pos["mfe"], favorable, 0.0)
        pos["mae"] = max(pos["mae"], adverse, 0.0)

        if direction == "BUY":
            hit_sl = bar["low"] <= pos["stop_loss"]
            hit_tp = bar["high"] >= pos["take_profit"]
        else:
            hit_sl = bar["high"] >= pos["stop_loss"]
            hit_tp = bar["low"] <= pos["take_profit"]

        exit_price, exit_reason = None, None
        if hit_sl:  # conservative: if both SL and TP fall inside the same bar's range, assume the stop hits first
            exit_price, exit_reason = pos["stop_loss"], "STOP_LOSS"
        elif hit_tp:
            exit_price, exit_reason = pos["take_profit"], "TAKE_PROFIT"

        if exit_price is None:
            return pos

        trade, realized = self._close_position(pos, exit_price, bar_time, exit_reason, spec, costs)
        trades.append(trade)
        history.record(TradeRecord(closed_at=bar_time, symbol=spec.name, direction=trade.direction, pnl=trade.pnl, r_multiple=trade.r_multiple))
        pos["_closed"] = True
        pos["_realized"] = realized
        return pos

    def _close_position(self, pos: dict, raw_exit_price: float, close_time, exit_reason: str, spec: SymbolSpec, costs: ExecutionCosts):
        direction = pos["direction"]
        fill = apply_exit_costs(raw_exit_price, direction, costs, spec.tick_size)
        ticks = (fill.price - pos["entry_price"]) / spec.tick_size if direction == "BUY" else (pos["entry_price"] - fill.price) / spec.tick_size
        gross_pnl = ticks * spec.tick_value * pos["lots"]
        comm = commission_cost(pos["lots"], costs)
        days_held = max((close_time.date() - pos["entry_time"].date()).days, 0)
        sw = swap_cost(pos["lots"], direction, days_held, costs)
        net_pnl = gross_pnl - comm + sw
        r_multiple = (net_pnl / pos["monetary_risk"]) if pos.get("monetary_risk") else None

        trade = BacktestTrade(
            symbol=spec.name, direction=direction, strategy=pos["strategy"], regime=pos["regime"], score=pos["score"],
            open_time=pos["entry_time"], close_time=close_time, entry_price=pos["entry_price"], exit_price=fill.price,
            stop_loss=pos["stop_loss"], take_profit=pos["take_profit"], lots=pos["lots"], pnl=net_pnl,
            gross_pnl=gross_pnl, commission=comm, swap=sw, r_multiple=r_multiple, mae=pos["mae"], mfe=pos["mfe"],
            exit_reason=exit_reason, session=pos.get("session", "OFF_HOURS"),
        )
        return trade, net_pnl

    def _evaluate_entry(self, m15_df, i, bar, bar_time, bar_date, equity, day_start_equity, week_start_equity, history) -> Optional[dict]:
        cfg = self.config
        spec = self.symbol_spec

        window_start = max(0, i + 1 - cfg.resample_lookback_bars)
        window = m15_df.iloc[window_start: i + 1]

        try:
            h1_df = resample_closed_only(window, "H1")
            h4_df = resample_closed_only(window, "H4")
            if len(h1_df) < MIN_BARS_REQUIRED or len(h4_df) < MIN_BARS_REQUIRED:
                return None

            m15_tail = window.tail(max(MIN_BARS_REQUIRED, 250))
            h4_features = compute_features(h4_df.tail(max(MIN_BARS_REQUIRED, 250)), "H4")
            h1_features = compute_features(h1_df.tail(max(MIN_BARS_REQUIRED, 250)), "H1")
            m15_features = compute_features(m15_tail, "M15")
        except InsufficientDataError:
            return None

        session = m15_features["session"]
        if cfg.allowed_sessions is not None and session not in cfg.allowed_sessions:
            return None

        h4_regime = detect_regime(h4_features, self.regime_thresholds)
        context = StrategyContext(
            symbol=spec.name, h4_df=h4_df, h1_df=h1_df, m15_df=m15_tail,
            h4_features=h4_features, h1_features=h1_features, m15_features=m15_features,
            h4_regime=h4_regime, current_bid=bar["close"] - spec.tick_size, current_ask=bar["close"] + spec.tick_size,
        )

        best = self.selector.best_signal(context)
        if best.direction == SignalDirection.NO_SIGNAL:
            return None

        day_start_dt = datetime.combine(bar_date, time.min, tzinfo=timezone.utc)
        trades_today = len(history.trades_since(day_start_dt))
        recent = history.recent_trades(20)

        guard_input = GuardCheckInput(
            equity=equity, day_start_equity=day_start_equity, week_start_equity=week_start_equity,
            trades_today_count=trades_today, open_positions_count=0, current_spread_points=self.costs.spread_points,
            mt5_connected=True, broker_trade_allowed=True, market_data_fresh=True, kill_switch_active=False,
            recent_trades=recent,
        )
        account = AccountInfo(login=0, balance=equity, equity=equity, margin=0.0, margin_free=equity, currency="USD", leverage=100, trade_allowed=True)
        validation = self.validator.validate(best, context, spec, account, guard_input)
        if not validation.approved:
            return None

        monetary_risk = equity * (self.risk_settings.risk_per_trade_pct / 100.0)
        return {
            "direction": best.direction.value, "strategy": best.strategy, "regime": h4_regime.regime.value,
            "score": best.score, "stop_loss": best.stop_loss, "take_profit": best.take_profit,
            "lots": validation.lots, "monetary_risk": monetary_risk, "session": session,
        }
