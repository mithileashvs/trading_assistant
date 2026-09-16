"""
Trade Validator / Guard (section 20).

Generates the complete validation object logged before every order
submission. This is where signal scoring, position sizing, risk
guards, and the news filter all converge into a single approve/reject
decision — the deterministic risk/validation layer has final say, not
the strategy or any AI layer (sections 1, 30).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.config.settings import RiskSettings
from app.mt5.interface import AccountInfo, SymbolSpec
from app.news.filter import NewsFilter, UnavailableNewsFilter
from app.risk.guards import GuardCheckInput, GuardResult, RiskGuardEngine
from app.risk.position_sizing import PositionSizeResult, calculate_position_size
from app.signals.models import Signal, SignalDirection
from app.strategies.context import StrategyContext


@dataclass
class TradeValidation:
    symbol: str
    direction: str
    strategy: str
    regime: str
    score: Optional[int]
    entry: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    risk_reward: Optional[float]
    risk_percent: float
    lots: float
    spread_ok: bool
    news_ok: bool
    news_state: str
    daily_loss_limit_ok: bool
    weekly_loss_limit_ok: bool
    position_limit_ok: bool
    market_data_fresh: bool
    approved: bool
    rejection_reasons: list[str] = field(default_factory=list)
    guard_checks: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "strategy": self.strategy,
            "regime": self.regime,
            "score": self.score,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "risk_reward": self.risk_reward,
            "risk_percent": self.risk_percent,
            "lots": self.lots,
            "spread_ok": self.spread_ok,
            "news_ok": self.news_ok,
            "news_state": self.news_state,
            "daily_loss_limit_ok": self.daily_loss_limit_ok,
            "weekly_loss_limit_ok": self.weekly_loss_limit_ok,
            "position_limit_ok": self.position_limit_ok,
            "market_data_fresh": self.market_data_fresh,
            "approved": self.approved,
            "rejection_reasons": self.rejection_reasons,
            "guard_checks": self.guard_checks,
        }


class TradeValidator:
    def __init__(
        self,
        risk_settings: RiskSettings,
        guard_engine: RiskGuardEngine | None = None,
        news_filter: NewsFilter | None = None,
        min_score_label: str = "VALID",
        min_risk_reward: float = 1.2,
    ):
        self.risk_settings = risk_settings
        self.guard_engine = guard_engine or RiskGuardEngine(risk_settings)
        self.news_filter = news_filter or UnavailableNewsFilter()
        # Score labels are ordered NO_TRADE < WEAK < VALID < STRONG; a
        # signal must meet at least min_score_label to be approvable.
        self._label_rank = {"NO_TRADE": 0, "WEAK": 1, "VALID": 2, "STRONG": 3}
        self.min_score_label = min_score_label
        self.min_risk_reward = min_risk_reward

    def validate(
        self,
        signal: Signal,
        context: StrategyContext,
        symbol_spec: SymbolSpec,
        account_info: AccountInfo,
        guard_input: GuardCheckInput,
    ) -> TradeValidation:
        reasons: list[str] = []

        guard_result = self.guard_engine.check(guard_input)
        news_status = self.news_filter.check(datetime.now(timezone.utc))
        # SAFETY-CRITICAL: news_ok comes ONLY from NewsStatus.permits_new_trade,
        # which is True for exactly one state (CLEAR). This replaces a prior
        # bug where `news_ok = not news_status.blackout_active` silently
        # evaluated to True when the filter was unavailable (available=False,
        # blackout_active defaulting to False) -- i.e. "I don't know" was
        # being treated as "safe". UNAVAILABLE and UNKNOWN now block
        # identically to BLOCKED; only CLEAR permits a new trade.
        news_ok = news_status.permits_new_trade
        if not news_ok:
            reasons.append(news_status.reason)

        if signal.direction == SignalDirection.NO_SIGNAL:
            reasons.append("No actionable signal (NO_SIGNAL) — nothing to validate.")
            return TradeValidation(
                symbol=context.symbol,
                direction=signal.direction.value,
                strategy=signal.strategy,
                regime=context.h4_regime.regime.value,
                score=signal.score,
                entry=None,
                stop_loss=None,
                take_profit=None,
                risk_reward=None,
                risk_percent=self.risk_settings.risk_per_trade_pct,
                lots=0.0,
                spread_ok=guard_result.checks.get("max_spread_ok", False),
                news_ok=news_ok,
                news_state=news_status.state.value,
                daily_loss_limit_ok=guard_result.checks.get("daily_loss_ok", False),
                weekly_loss_limit_ok=guard_result.checks.get("weekly_loss_ok", False),
                position_limit_ok=guard_result.checks.get("max_open_positions_ok", False),
                market_data_fresh=guard_result.checks.get("market_data_fresh", False),
                approved=False,
                rejection_reasons=reasons,
                guard_checks=guard_result.checks,
            )

        if not guard_result.passed:
            reasons.extend(guard_result.reasons)

        score_label = signal.score_label or "NO_TRADE"
        score_ok = self._label_rank.get(score_label, 0) >= self._label_rank.get(self.min_score_label, 2)
        if not score_ok:
            reasons.append(f"Signal score label ({score_label}) is below the minimum required ({self.min_score_label}).")

        risk_reward = signal.risk_reward
        rr_ok = risk_reward is not None and risk_reward >= self.min_risk_reward
        if not rr_ok:
            reasons.append(
                f"Risk:Reward ({risk_reward}) is below the minimum required ({self.min_risk_reward})."
            )

        sizing: Optional[PositionSizeResult] = None
        try:
            sizing = calculate_position_size(
                equity=account_info.equity,
                risk_pct=self.risk_settings.risk_per_trade_pct,
                entry=signal.entry,
                stop_loss=signal.stop_loss,
                symbol_spec=symbol_spec,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a rejection reason, not a crash
            reasons.append(f"Position sizing failed: {exc}")

        size_ok = sizing is not None and sizing.is_tradable
        if sizing is not None:
            reasons.extend(sizing.warnings)
        if sizing is not None and not size_ok:
            reasons.append("Computed position size is zero — cannot take this trade within the configured risk.")

        approved = guard_result.passed and news_ok and score_ok and rr_ok and size_ok

        return TradeValidation(
            symbol=context.symbol,
            direction=signal.direction.value,
            strategy=signal.strategy,
            regime=context.h4_regime.regime.value,
            score=signal.score,
            entry=signal.entry,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            risk_reward=risk_reward,
            risk_percent=self.risk_settings.risk_per_trade_pct,
            lots=sizing.lots if sizing else 0.0,
            spread_ok=guard_result.checks.get("max_spread_ok", False),
            news_ok=news_ok,
            news_state=news_status.state.value,
            daily_loss_limit_ok=guard_result.checks.get("daily_loss_ok", False),
            weekly_loss_limit_ok=guard_result.checks.get("weekly_loss_ok", False),
            position_limit_ok=guard_result.checks.get("max_open_positions_ok", False),
            market_data_fresh=guard_result.checks.get("market_data_fresh", False),
            approved=approved,
            rejection_reasons=reasons,
            guard_checks=guard_result.checks,
        )
