"""
Independent Safety Gate (audit sections 17-18, architecture section 5:
RISK ENGINE -> SAFETY GATE -> EXECUTION SAFETY GATE -> MT5).

THIS MODULE MUST NOT IMPORT OR CALL INTO app.risk.validator,
app.risk.guards, OR ANY OTHER PART OF THE RISK ENGINE. That is not an
accident or an oversight to fix later — it is the entire point.

"Two independent validations" (section 18) means two SEPARATELY
IMPLEMENTED code paths reaching the same conclusion, so a bug in one
implementation is unlikely to also exist in the other. If SafetyGate
were built by calling RiskGuardEngine.check() internally, a bug in
RiskGuardEngine would silently compromise BOTH "independent" layers at
once — that would be independence in name only. Some of the checks
below necessarily look similar to app.risk.guards's checks (the same
underlying safety rules apply), but every check here is computed from
its own inputs, in its own code, with its own logic — never by
delegating to the risk engine's implementation.

AUTHORITY: even when the risk engine (TradeValidator) has already
approved a trade, SafetyGate can still reject it, and that rejection
is final — SafetyGate sits ABOVE the risk engine in the architecture's
authority hierarchy (section 4), not beside it. A trade proceeds only
if BOTH independently approve (section 18). If SafetyGate itself
errors — any exception, for any reason — the result is an automatic
rejection, never a pass-through (section 18: "SafetyGate ERROR: FAIL
CLOSED").

FRESHNESS: SafetyGateInput is meant to be built from data gathered
freshly, immediately before this call — not reused from whatever
context produced the original signal. Time passes between signal
generation and order submission; this gate exists specifically to
catch what may have changed in that window (section 19).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.news.filter import NewsState


@dataclass
class SafetyGateInput:
    # Account state
    account_equity: float
    account_trade_allowed: bool
    account_is_demo: Optional[bool]

    # Symbol / broker state
    symbol_trade_allowed: bool
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int
    tick_size: float

    # Proposed order
    direction: str  # "BUY" | "SELL"
    volume: float
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]

    # Market state
    current_spread_points: float
    max_spread_points: float
    market_data_fresh: bool

    # News state -- the actual NewsState enum value, never a
    # pre-collapsed boolean (see app.news.filter's module docstring for
    # why that collapsing was a live bug).
    news_state: NewsState

    # Safety / risk state
    kill_switch_active: bool
    daily_loss_pct: float
    max_daily_loss_pct: float
    open_positions_count: int
    max_open_positions: int

    checked_at: datetime = field(default_factory=lambda: datetime.now().astimezone())


@dataclass
class SafetyGateResult:
    approved: bool
    is_error: bool
    checks: dict[str, bool]
    reasons: list[str]

    def to_dict(self) -> dict:
        return {"approved": self.approved, "is_error": self.is_error,
                "checks": self.checks, "reasons": self.reasons}


class SafetyGate:
    """Stateless -- every check is a pure function of the
    SafetyGateInput passed to evaluate(). No shared state with the
    risk engine, the strategy layer, or the AI layer."""

    def evaluate(self, data: SafetyGateInput) -> SafetyGateResult:
        try:
            return self._evaluate(data)
        except Exception as exc:  # noqa: BLE001 - deliberate: ANY failure here fails closed
            return SafetyGateResult(
                approved=False,
                is_error=True,
                checks={},
                reasons=[f"SafetyGate encountered an internal error and is failing closed: {exc}"],
            )

    def _evaluate(self, data: SafetyGateInput) -> SafetyGateResult:
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        # -- kill switch ---------------------------------------------------
        checks["kill_switch_ok"] = not data.kill_switch_active
        if not checks["kill_switch_ok"]:
            reasons.append("Kill switch is active.")

        # -- broker / account permissions ------------------------------------
        checks["account_trade_allowed"] = data.account_trade_allowed
        if not checks["account_trade_allowed"]:
            reasons.append("Account does not currently permit trading.")

        checks["symbol_trade_allowed"] = data.symbol_trade_allowed
        if not checks["symbol_trade_allowed"]:
            reasons.append("Symbol trading is not currently permitted by the broker.")

        # -- market data freshness ------------------------------------------------
        checks["market_data_fresh"] = data.market_data_fresh
        if not checks["market_data_fresh"]:
            reasons.append("Market data is not fresh.")

        # -- news state: ONLY CLEAR permits a trade (never a collapsed boolean) ------
        checks["news_clear"] = data.news_state == NewsState.CLEAR
        if not checks["news_clear"]:
            reasons.append(f"News state is {data.news_state.value}, not CLEAR.")

        # -- daily loss ---------------------------------------------------------------
        checks["daily_loss_within_limit"] = data.daily_loss_pct <= data.max_daily_loss_pct
        if not checks["daily_loss_within_limit"]:
            reasons.append(
                f"Daily loss ({data.daily_loss_pct:.2f}%) exceeds the configured maximum "
                f"({data.max_daily_loss_pct:.2f}%)."
            )

        # -- spread ------------------------------------------------------------------------
        checks["spread_within_limit"] = data.current_spread_points <= data.max_spread_points
        if not checks["spread_within_limit"]:
            reasons.append(
                f"Spread ({data.current_spread_points} points) exceeds the maximum "
                f"({data.max_spread_points} points)."
            )

        # -- open position limit -------------------------------------------------------------
        checks["position_limit_ok"] = data.open_positions_count < data.max_open_positions
        if not checks["position_limit_ok"]:
            reasons.append(
                f"Already have {data.open_positions_count} open position(s) "
                f"(max {data.max_open_positions})."
            )

        # -- stop loss mandatory and directionally valid (section 20) -----------------------
        checks["stop_loss_present"] = data.stop_loss is not None
        if not checks["stop_loss_present"]:
            reasons.append("No stop loss on the proposed order; automated entries require one.")
        else:
            if data.direction == "BUY":
                checks["stop_loss_direction_valid"] = data.stop_loss < data.entry_price
            elif data.direction == "SELL":
                checks["stop_loss_direction_valid"] = data.stop_loss > data.entry_price
            else:
                checks["stop_loss_direction_valid"] = False
            if not checks["stop_loss_direction_valid"]:
                reasons.append(
                    f"Stop loss ({data.stop_loss}) is on the wrong side of entry "
                    f"({data.entry_price}) for a {data.direction} order."
                )

            # broker minimum stop distance, in points
            if data.tick_size > 0:
                distance_points = abs(data.entry_price - data.stop_loss) / data.tick_size
                checks["stop_distance_meets_broker_minimum"] = distance_points >= data.stops_level_points
                if not checks["stop_distance_meets_broker_minimum"]:
                    reasons.append(
                        f"Stop distance ({distance_points:.1f} points) is below the broker's "
                        f"minimum stops level ({data.stops_level_points} points)."
                    )
            else:
                checks["stop_distance_meets_broker_minimum"] = False
                reasons.append("Symbol tick_size is not positive; cannot validate stop distance.")

        # -- take profit directional validity, if present -----------------------------------
        if data.take_profit is not None:
            if data.direction == "BUY":
                checks["take_profit_direction_valid"] = data.take_profit > data.entry_price
            elif data.direction == "SELL":
                checks["take_profit_direction_valid"] = data.take_profit < data.entry_price
            else:
                checks["take_profit_direction_valid"] = False
            if not checks["take_profit_direction_valid"]:
                reasons.append(
                    f"Take profit ({data.take_profit}) is on the wrong side of entry "
                    f"({data.entry_price}) for a {data.direction} order."
                )

        # -- volume validity against broker constraints --------------------------------------
        checks["volume_within_broker_limits"] = data.volume_min <= data.volume <= data.volume_max
        if not checks["volume_within_broker_limits"]:
            reasons.append(
                f"Volume ({data.volume}) is outside the broker's allowed range "
                f"[{data.volume_min}, {data.volume_max}]."
            )
        if data.volume_step > 0:
            steps = data.volume / data.volume_step
            checks["volume_matches_broker_step"] = abs(steps - round(steps)) < 1e-6
            if not checks["volume_matches_broker_step"]:
                reasons.append(f"Volume ({data.volume}) is not a multiple of the broker's step ({data.volume_step}).")
        else:
            checks["volume_matches_broker_step"] = False
            reasons.append("Symbol volume_step is not positive; cannot validate volume step.")

        checks["volume_positive"] = data.volume > 0
        if not checks["volume_positive"]:
            reasons.append("Volume must be positive.")

        approved = all(checks.values())
        return SafetyGateResult(approved=approved, is_error=False, checks=checks, reasons=reasons)
