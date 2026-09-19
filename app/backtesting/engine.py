"""
Backtesting Engine (Phase 10, sections 24, 41).

Walks forward bar-by-bar through M15 history. At each evaluated bar:
- H1/H4 context is derived using ONLY fully-closed higher-timeframe
  candles up to that point (app.backtesting.resampling).
- A decision is made from that bar's close.
- Any resulting entry is filled at the NEXT bar's open -- never the
  same bar whose close produced the signal.
- Intrabar SL/TP execution is gap-aware (gapping past a stop fills at
  market open, not the optimistic stop level).
- Conservative same-bar resolution: if both SL and TP are touched in
  the same candle, the stop loss is assumed to have hit first.
- Optional Phase 8 position management (breakeven, trailing stop,
  partial exit) can be simulated without future lookahead.
- Pre-simulation data quality is validated and reported.
- Comprehensive metrics, benchmarks, and rejection statistics are
  recorded for full auditability and deterministic reproducibility.

This engine routes exclusively through simulation and NEVER places live orders.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
import numpy as np
import pandas as pd

from app.backtesting.benchmark import compute_benchmark
from app.backtesting.costs import ExecutionCosts, apply_entry_costs, apply_exit_costs, commission_cost, swap_cost
from app.backtesting.data_validator import DataQualityReport, InvalidHistoricalDataError, validate_historical_data
from app.backtesting.metrics import compute_metrics
from app.backtesting.resampling import resample_closed_only
from app.backtesting.results import BacktestResult, BacktestTrade, EquityPoint
from app.config.settings import RiskSettings, TradingMode
from app.features.engine import MIN_BARS_REQUIRED, InsufficientDataError, compute_features
from app.mt5.interface import AccountInfo, SymbolSpec
from app.news.filter import AlwaysClearNewsFilter, NewsFilter
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.positions.monitor import PositionMonitorConfig
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
    enable_position_management: bool = False
    position_monitor_config: Optional[PositionMonitorConfig] = None
    strict_data_validation: bool = False
    random_seed: Optional[int] = None


def compute_reproducibility_hash(
    data: pd.DataFrame,
    spec: SymbolSpec,
    cfg: BacktestConfig,
    costs: ExecutionCosts,
    risk_settings: RiskSettings,
) -> str:
    """Compute a deterministic SHA-256 fingerprint encompassing both the simulation
    configuration parameters and the canonical binary representation of the historical
    OHLCV, timestamp, and spread data used during the backtest.
    """
    hasher = hashlib.sha256()

    # 1. Configuration & risk parameters
    config_payload = (
        f"CONFIG:{spec.name}:{len(data)}:"
        f"{cfg.starting_balance}:{cfg.warmup_bars}:{cfg.step}:{cfg.min_score_label}:{cfg.min_risk_reward}:"
        f"{cfg.enable_position_management}:"
        f"{costs.spread_points}:{costs.slippage_points}:{costs.commission_per_lot}:"
        f"{costs.swap_long_per_lot_per_day}:{costs.swap_short_per_lot_per_day}:"
        f"{risk_settings.risk_per_trade_pct}:{risk_settings.max_daily_loss_pct}:{risk_settings.max_weekly_loss_pct};"
    ).encode("utf-8")
    hasher.update(config_payload)

    # 2. Historical data payload: timestamps and OHLCV/spread columns
    if data is not None and not data.empty:
        # Timestamps
        hasher.update(b"INDEX:")
        if isinstance(data.index, pd.DatetimeIndex):
            hasher.update(str(data.index.tz).encode("utf-8"))
            hasher.update(data.index.astype("<i8").to_numpy().tobytes())
        else:
            for ts in data.index:
                hasher.update(str(ts).encode("utf-8"))

        # Columns in canonical order: OHLC first, then any other columns in alphabetical order
        primary_cols = ["open", "high", "low", "close"]
        additional_cols = sorted([c for c in data.columns if c not in primary_cols])
        ordered_cols = [c for c in primary_cols if c in data.columns] + additional_cols

        for col in ordered_cols:
            hasher.update(f";COL:{col}:".encode("utf-8"))
            series = data[col]
            if np.issubdtype(series.dtype, np.number):
                hasher.update(series.to_numpy(dtype="<f8").tobytes())
            else:
                for val in series:
                    hasher.update(str(val).encode("utf-8"))

    return hasher.hexdigest()


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
        trading_mode: TradingMode = TradingMode.BACKTEST,
    ):
        # Strict Safety Check: BacktestEngine must NEVER execute live orders
        if trading_mode == TradingMode.LIVE or getattr(risk_settings, "trading_mode", None) == TradingMode.LIVE:
            raise RuntimeError("Safety violation: BacktestEngine must never be configured for LIVE trading mode.")

        self.symbol_spec = symbol_spec
        self.risk_settings = risk_settings
        self.costs = costs or ExecutionCosts()
        self.config = config or BacktestConfig()
        self.selector = selector or StrategySelector()
        self.regime_thresholds = regime_thresholds
        self.scoring_weights = scoring_weights
        self.news_filter = news_filter or AlwaysClearNewsFilter()
        self.validator = TradeValidator(
            risk_settings,
            guard_engine=RiskGuardEngine(risk_settings),
            news_filter=self.news_filter,
            min_score_label=self.config.min_score_label,
            min_risk_reward=self.config.min_risk_reward,
        )
        self.rejections: dict[str, int] = {}
        self.rejection_details: list[dict] = []

    def run(self, m15_df: pd.DataFrame) -> BacktestResult:
        cfg = self.config
        costs = self.costs
        spec = self.symbol_spec

        # 1. Historical data quality check
        quality_report = validate_historical_data(m15_df, strict=cfg.strict_data_validation)

        if m15_df is None or m15_df.empty:
            empty_hash = compute_reproducibility_hash(
                pd.DataFrame(), spec, cfg, costs, self.risk_settings
            )
            empty_res = BacktestResult(
                symbol=spec.name,
                starting_balance=cfg.starting_balance,
                ending_balance=cfg.starting_balance,
                bars_evaluated=0,
                config=asdict(cfg),
                data_quality=quality_report.to_dict(),
                reproducibility_hash=empty_hash,
            )
            empty_res.metrics = compute_metrics(empty_res)
            return empty_res

        self.rejections = {}
        self.rejection_details = []

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

            # --- 1. Fill pending entry at THIS bar's open (decided at previous bar's close) ---
            if pending_entry is not None and open_position is None:
                fill = apply_entry_costs(bar["open"], pending_entry["direction"], costs, spec.tick_size)
                initial_risk = abs(fill.price - pending_entry["stop_loss"])
                open_position = {
                    **pending_entry,
                    "entry_price": fill.price,
                    "entry_time": bar_time,
                    "entry_spread_cost": fill.spread_cost,
                    "entry_slippage_cost": fill.slippage_cost,
                    "initial_risk": initial_risk,
                    "initial_stop_loss": pending_entry["stop_loss"],
                    "breakeven_applied": False,
                    "partial_taken": False,
                    "mae": 0.0,
                    "mfe": 0.0,
                }
                pending_entry = None

            # --- 2. Manage open position against THIS bar's high/low ---
            if open_position is not None:
                open_position = self._update_and_maybe_exit(open_position, bar, bar_time, spec, costs, trades, history)
                if open_position is not None and open_position.get("_partial_realized"):
                    equity += open_position.pop("_partial_realized")
                if open_position is not None and open_position.get("_closed"):
                    equity += open_position["_realized"]
                    open_position = None

            # --- 3. Evaluate for a new entry (decision made from this bar's close) ---
            if open_position is None and pending_entry is None and i >= cfg.warmup_bars and i % cfg.step == 0:
                pending_entry = self._evaluate_entry(
                    m15_df, i, bar, bar_time, bar_date, equity, day_start_equity, week_start_equity, history
                )

            equity_curve.append(EquityPoint(time=bar_time, equity=equity + self._floating_pnl(open_position, bar, spec)))

        # Force-close anything still open at the end of the data
        if open_position is not None:
            last_bar = m15_df.iloc[n - 1]
            last_time = m15_df.index[n - 1]
            trade, realized = self._close_position(open_position, last_bar["close"], last_time, "END_OF_DATA", spec, costs)
            trades.append(trade)
            history.record(TradeRecord(
                closed_at=last_time, symbol=spec.name, direction=trade.direction,
                pnl=trade.pnl, r_multiple=trade.r_multiple
            ))
            equity += realized
            if equity_curve:
                equity_curve[-1] = EquityPoint(time=last_time, equity=equity)

        # Benchmark calculation
        benchmark = compute_benchmark(m15_df, cfg.starting_balance, spec, warmup_bars=cfg.warmup_bars)

        # Reproducibility hash
        eval_data = m15_df.iloc[:n] if n > 0 else m15_df
        rep_hash = compute_reproducibility_hash(
            eval_data, spec, cfg, costs, self.risk_settings
        )

        result = BacktestResult(
            symbol=spec.name,
            starting_balance=cfg.starting_balance,
            ending_balance=equity,
            trades=trades,
            equity_curve=equity_curve,
            bars_evaluated=n,
            config=asdict(cfg),
            start_time=m15_df.index[0].to_pydatetime() if len(m15_df) else None,
            end_time=m15_df.index[n - 1].to_pydatetime() if n > 0 else None,
            timeframe="M15",
            rejections=dict(self.rejections),
            rejection_details=list(self.rejection_details),
            data_quality=quality_report.to_dict(),
            benchmark=benchmark,
            risk_settings=self.risk_settings.model_dump() if hasattr(self.risk_settings, "model_dump") else {},
            execution_costs=self.costs.model_dump() if hasattr(self.costs, "model_dump") else {},
            reproducibility_hash=rep_hash,
        )
        result.metrics = compute_metrics(result)
        return result

    # ------------------------------------------------------------------
    def _floating_pnl(self, open_position: Optional[dict], bar, spec: SymbolSpec) -> float:
        if open_position is None:
            return 0.0
        direction = open_position["direction"]
        price = bar["close"]
        ticks = (price - open_position["entry_price"]) / spec.tick_size if direction == "BUY" else (open_position["entry_price"] - price) / spec.tick_size
        return ticks * spec.tick_value * open_position["lots"]

    def _update_and_maybe_exit(
        self,
        pos: dict,
        bar,
        bar_time,
        spec: SymbolSpec,
        costs: ExecutionCosts,
        trades: list,
        history: InMemoryTradeHistory,
    ) -> Optional[dict]:
        direction = pos["direction"]
        if direction == "BUY":
            favorable = bar["high"] - pos["entry_price"]
            adverse = pos["entry_price"] - bar["low"]
        else:
            favorable = pos["entry_price"] - bar["low"]
            adverse = bar["high"] - pos["entry_price"]
        pos["mfe"] = max(pos["mfe"], favorable, 0.0)
        pos["mae"] = max(pos["mae"], adverse, 0.0)

        # 1. Position Management triggers (Phase 8), if enabled
        if self.config.enable_position_management and pos.get("initial_risk", 0.0) > 0:
            pos_mon_cfg = self.config.position_monitor_config or PositionMonitorConfig()
            r_favorable = (favorable / pos["initial_risk"]) if pos["initial_risk"] > 0 else 0.0

            # 1a. Move to Breakeven
            if not pos.get("breakeven_applied") and r_favorable >= pos_mon_cfg.breakeven_trigger_r:
                buffer = pos_mon_cfg.breakeven_buffer_ticks * spec.tick_size
                if direction == "BUY":
                    be_sl = pos["entry_price"] + buffer
                    if be_sl > pos["stop_loss"]:
                        pos["stop_loss"] = be_sl
                        pos["breakeven_applied"] = True
                else:
                    be_sl = pos["entry_price"] - buffer
                    if be_sl < pos["stop_loss"]:
                        pos["stop_loss"] = be_sl
                        pos["breakeven_applied"] = True

            # 1b. Trailing Stop
            if pos_mon_cfg.enable_trailing and r_favorable >= pos_mon_cfg.trailing_trigger_r:
                atr = pos.get("entry_atr", 1.0)
                if direction == "BUY":
                    trail_sl = bar["close"] - (pos_mon_cfg.trailing_atr_multiplier * atr)
                    if trail_sl > pos["stop_loss"]:
                        pos["stop_loss"] = trail_sl
                else:
                    trail_sl = bar["close"] + (pos_mon_cfg.trailing_atr_multiplier * atr)
                    if trail_sl < pos["stop_loss"]:
                        pos["stop_loss"] = trail_sl

            # 1c. Partial Exit
            if (
                pos_mon_cfg.enable_partial_exit
                and not pos.get("partial_taken")
                and r_favorable >= pos_mon_cfg.partial_exit_trigger_r
            ):
                frac = pos_mon_cfg.partial_exit_fraction
                step = spec.volume_step
                partial_lots = round(pos["lots"] * frac / step) * step
                if partial_lots >= spec.volume_min and (pos["lots"] - partial_lots) >= spec.volume_min:
                    if direction == "BUY":
                        trigger_price = pos["entry_price"] + (pos_mon_cfg.partial_exit_trigger_r * pos["initial_risk"])
                    else:
                        trigger_price = pos["entry_price"] - (pos_mon_cfg.partial_exit_trigger_r * pos["initial_risk"])
                    partial_pos = dict(pos)
                    partial_pos["lots"] = partial_lots
                    partial_trade, partial_realized = self._close_position(
                        partial_pos, trigger_price, bar_time, "PARTIAL_EXIT", spec, costs, is_partial=True
                    )
                    trades.append(partial_trade)
                    if hasattr(history, "record"):
                        history.record(TradeRecord(
                            closed_at=bar_time, symbol=spec.name, direction=direction,
                            pnl=partial_trade.pnl, r_multiple=partial_trade.r_multiple
                        ))
                    pos["lots"] -= partial_lots
                    pos["partial_taken"] = True
                    pos["_partial_realized"] = partial_realized

        # 2. Intrabar SL / TP Exits with Gap-Aware Fills and Conservative Same-Bar Resolution
        if direction == "BUY":
            hit_sl = bar["low"] <= pos["stop_loss"]
            hit_tp = bar["high"] >= pos["take_profit"]
        else:
            hit_sl = bar["high"] >= pos["stop_loss"]
            hit_tp = bar["low"] <= pos["take_profit"]

        exit_price, exit_reason = None, None

        # Conservative same-bar resolution rule:
        # If both SL and TP fall inside the same bar's range, assume the stop loss hits first.
        if hit_sl and hit_tp:
            exit_reason = "STOP_LOSS"
            if direction == "BUY":
                exit_price = min(pos["stop_loss"], bar["open"])
            else:
                exit_price = max(pos["stop_loss"], bar["open"])
        elif hit_sl:
            exit_reason = "STOP_LOSS"
            if direction == "BUY":
                exit_price = min(pos["stop_loss"], bar["open"])
            else:
                exit_price = max(pos["stop_loss"], bar["open"])
        elif hit_tp:
            exit_reason = "TAKE_PROFIT"
            if direction == "BUY":
                exit_price = max(pos["take_profit"], bar["open"])
            else:
                exit_price = min(pos["take_profit"], bar["open"])

        if exit_price is None:
            return pos

        trade, realized = self._close_position(pos, exit_price, bar_time, exit_reason, spec, costs, is_partial=False)
        trades.append(trade)
        if hasattr(history, "record"):
            history.record(TradeRecord(
                closed_at=bar_time, symbol=spec.name, direction=trade.direction,
                pnl=trade.pnl, r_multiple=trade.r_multiple
            ))
        pos["_closed"] = True
        pos["_realized"] = realized
        return pos

    def _close_position(
        self,
        pos: dict,
        raw_exit_price: float,
        close_time,
        exit_reason: str,
        spec: SymbolSpec,
        costs: ExecutionCosts,
        is_partial: bool = False,
    ):
        direction = pos["direction"]
        fill = apply_exit_costs(raw_exit_price, direction, costs, spec.tick_size)
        ticks = (fill.price - pos["entry_price"]) / spec.tick_size if direction == "BUY" else (pos["entry_price"] - fill.price) / spec.tick_size
        gross_pnl = ticks * spec.tick_value * pos["lots"]
        comm = commission_cost(pos["lots"], costs)
        days_held = max((close_time.date() - pos["entry_time"].date()).days, 0)
        sw = swap_cost(pos["lots"], direction, days_held, costs)
        net_pnl = gross_pnl - comm + sw
        r_multiple = (net_pnl / pos["monetary_risk"]) if pos.get("monetary_risk") else None

        # Calculate exact spread and slippage cost contributions in dollar terms
        entry_spread_dollar = (pos.get("entry_spread_cost", 0.0) / spec.tick_size) * spec.tick_value * pos["lots"]
        exit_spread_dollar = (fill.spread_cost / spec.tick_size) * spec.tick_value * pos["lots"]
        total_spread_cost = entry_spread_dollar + exit_spread_dollar

        entry_slip_dollar = (pos.get("entry_slippage_cost", 0.0) / spec.tick_size) * spec.tick_value * pos["lots"]
        exit_slip_dollar = (fill.slippage_cost / spec.tick_size) * spec.tick_value * pos["lots"]
        total_slippage_cost = entry_slip_dollar + exit_slip_dollar

        trade = BacktestTrade(
            symbol=spec.name, direction=direction, strategy=pos["strategy"], regime=pos["regime"], score=pos["score"],
            open_time=pos["entry_time"], close_time=close_time, entry_price=pos["entry_price"], exit_price=fill.price,
            stop_loss=pos["stop_loss"], take_profit=pos["take_profit"], lots=pos["lots"], pnl=net_pnl,
            gross_pnl=gross_pnl, commission=comm, swap=sw, r_multiple=r_multiple, mae=pos["mae"], mfe=pos["mfe"],
            exit_reason=exit_reason, session=pos.get("session", "OFF_HOURS"),
            spread_cost=total_spread_cost, slippage_cost=total_slippage_cost, is_partial=is_partial,
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
            self.rejections["DISALLOWED_SESSION"] = self.rejections.get("DISALLOWED_SESSION", 0) + 1
            self.rejection_details.append({
                "timestamp": bar_time.isoformat(),
                "strategy": "ALL",
                "reason": f"Session '{session}' not in allowed_sessions",
            })
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
            for r in validation.rejection_reasons:
                self.rejections[r] = self.rejections.get(r, 0) + 1
                self.rejection_details.append({
                    "timestamp": bar_time.isoformat(),
                    "strategy": best.strategy,
                    "reason": r,
                })
            return None

        monetary_risk = equity * (self.risk_settings.risk_per_trade_pct / 100.0)
        return {
            "direction": best.direction.value, "strategy": best.strategy, "regime": h4_regime.regime.value,
            "score": best.score, "stop_loss": best.stop_loss, "take_profit": best.take_profit,
            "lots": validation.lots, "monetary_risk": monetary_risk, "session": session,
            "entry_atr": m15_features.get("atr_14", 1.0),
        }
