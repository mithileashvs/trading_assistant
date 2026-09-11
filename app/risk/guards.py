"""
Risk Guards (section 16).

If ANY critical condition fails, the result is NO NEW TRADES. This
module only reads state (account info, spread, connection status,
trade history) — it never talks to MT5 or places orders itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config.settings import RiskSettings
from app.risk.history import TradeRecord


@dataclass
class GuardCheckInput:
    equity: float
    day_start_equity: float
    week_start_equity: float
    trades_today_count: int
    open_positions_count: int
    current_spread_points: float
    mt5_connected: bool
    broker_trade_allowed: bool
    market_data_fresh: bool
    kill_switch_active: bool
    recent_trades: list[TradeRecord] = field(default_factory=list)  # oldest -> newest


@dataclass
class GuardResult:
    passed: bool
    checks: dict[str, bool]
    reasons: list[str]

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks, "reasons": self.reasons}


def _consecutive_losses(recent_trades: list[TradeRecord]) -> int:
    """Trailing losing streak, counted from the most recent trade
    backwards until a non-loss (or the history runs out)."""
    count = 0
    for trade in reversed(recent_trades):
        if trade.pnl < 0:
            count += 1
        else:
            break
    return count


class RiskGuardEngine:
    def __init__(self, risk_settings: RiskSettings):
        self.settings = risk_settings

    def check(self, data: GuardCheckInput) -> GuardResult:
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        checks["kill_switch_ok"] = not data.kill_switch_active
        if not checks["kill_switch_ok"]:
            reasons.append("Kill switch is active — no new trades.")

        checks["mt5_connected"] = data.mt5_connected
        if not checks["mt5_connected"]:
            reasons.append("MT5 client is not connected.")

        checks["broker_trade_allowed"] = data.broker_trade_allowed
        if not checks["broker_trade_allowed"]:
            reasons.append("Broker/account does not currently permit trading.")

        checks["market_data_fresh"] = data.market_data_fresh
        if not checks["market_data_fresh"]:
            reasons.append("Market data is stale.")

        checks["min_equity_ok"] = data.equity >= self.settings.min_account_equity
        if not checks["min_equity_ok"]:
            reasons.append(
                f"Equity ({data.equity:.2f}) is below the configured minimum "
                f"({self.settings.min_account_equity})."
            )

        daily_pnl = data.equity - data.day_start_equity
        daily_loss_pct = (-daily_pnl / data.day_start_equity * 100.0) if daily_pnl < 0 and data.day_start_equity > 0 else 0.0
        checks["daily_loss_ok"] = daily_loss_pct <= self.settings.max_daily_loss_pct
        if not checks["daily_loss_ok"]:
            reasons.append(
                f"Daily loss ({daily_loss_pct:.2f}%) exceeds the configured maximum "
                f"({self.settings.max_daily_loss_pct}%)."
            )

        weekly_pnl = data.equity - data.week_start_equity
        weekly_loss_pct = (-weekly_pnl / data.week_start_equity * 100.0) if weekly_pnl < 0 and data.week_start_equity > 0 else 0.0
        checks["weekly_loss_ok"] = weekly_loss_pct <= self.settings.max_weekly_loss_pct
        if not checks["weekly_loss_ok"]:
            reasons.append(
                f"Weekly loss ({weekly_loss_pct:.2f}%) exceeds the configured maximum "
                f"({self.settings.max_weekly_loss_pct}%)."
            )

        checks["max_trades_per_day_ok"] = data.trades_today_count < self.settings.max_trades_per_day
        if not checks["max_trades_per_day_ok"]:
            reasons.append(
                f"Already placed {data.trades_today_count} trades today "
                f"(max {self.settings.max_trades_per_day})."
            )

        checks["max_open_positions_ok"] = data.open_positions_count < self.settings.max_open_positions
        if not checks["max_open_positions_ok"]:
            reasons.append(
                f"Already have {data.open_positions_count} open position(s) "
                f"(max {self.settings.max_open_positions})."
            )

        consecutive_losses = _consecutive_losses(data.recent_trades)
        checks["max_consecutive_losses_ok"] = consecutive_losses < self.settings.max_consecutive_losses
        if not checks["max_consecutive_losses_ok"]:
            reasons.append(
                f"{consecutive_losses} consecutive losses (max {self.settings.max_consecutive_losses})."
            )

        checks["max_spread_ok"] = data.current_spread_points <= self.settings.max_spread_points
        if not checks["max_spread_ok"]:
            reasons.append(
                f"Current spread ({data.current_spread_points} points) exceeds the maximum "
                f"({self.settings.max_spread_points} points)."
            )

        passed = all(checks.values())
        return GuardResult(passed=passed, checks=checks, reasons=reasons)
