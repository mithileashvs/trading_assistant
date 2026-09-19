"""
Central configuration for the XAU/USD trading assistant.

Design rules (see master build prompt, sections 4, 13, 17, 36):
- TRADING_MODE defaults to PAPER. LIVE must be explicitly opted into.
- No secrets are hard-coded; everything sensitive comes from environment
  variables (see .env.example).
- All risk/strategy thresholds are configurable, not sacred constants.
"""
from __future__ import annotations

import enum
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _blank_to_none(v):
    """Treat an empty-string env var (e.g. MT5_LOGIN= in .env) as unset
    rather than a parse error."""
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


class TradingMode(str, enum.Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Timeframe(str, enum.Enum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"


class RiskSettings(BaseSettings):
    """Risk engine configuration. All values are software defaults, not
    performance claims (see section 13)."""

    risk_per_trade_pct: float = Field(0.5, ge=0.01, le=5.0)
    max_daily_loss_pct: float = Field(2.0, ge=0.1, le=20.0)
    max_weekly_loss_pct: float = Field(5.0, ge=0.1, le=40.0)
    max_open_positions: int = Field(1, ge=1, le=20)
    max_trades_per_day: int = Field(5, ge=1, le=100)
    max_consecutive_losses: int = Field(3, ge=1, le=20)
    max_spread_points: float = Field(50.0, gt=0)
    max_slippage_points: float = Field(20.0, gt=0)
    min_account_equity: float = Field(0.0, ge=0.0)

    model_config = SettingsConfigDict(env_prefix="RISK_")


class Settings(BaseSettings):
    """Top-level application settings, populated from environment
    variables / .env file. Never put credentials in code."""

    # --- Mode & safety -----------------------------------------------
    trading_mode: TradingMode = Field(TradingMode.PAPER, alias="TRADING_MODE")
    kill_switch: bool = Field(False, alias="KILL_SWITCH")
    kill_switch_closes_positions: bool = Field(
        False, alias="KILL_SWITCH_CLOSES_POSITIONS"
    )

    # --- MT5 connection (credentials only, never hard-coded) ----------
    mt5_login: Optional[int] = Field(None, alias="MT5_LOGIN")
    mt5_password: Optional[str] = Field(None, alias="MT5_PASSWORD")
    mt5_server: Optional[str] = Field(None, alias="MT5_SERVER")
    mt5_terminal_path: Optional[str] = Field(None, alias="MT5_TERMINAL_PATH")

    @field_validator("mt5_login", "mt5_password", "mt5_server", "mt5_terminal_path", mode="before")
    @classmethod
    def _empty_string_to_none(cls, v):
        return _blank_to_none(v)
    mt5_use_mock: bool = Field(
        True,
        alias="MT5_USE_MOCK",
        description=(
            "When true, use the deterministic MockMT5Client instead of a "
            "real MT5 terminal connection. Must be false for PAPER-live "
            "market data or LIVE trading."
        ),
    )

    # --- Symbol discovery ----------------------------------------------
    symbol_candidates: str = Field(
        "XAUUSD,XAUUSDm,GOLD,GOLDm,XAUUSD.",
        alias="SYMBOL_CANDIDATES",
        description="Comma-separated candidate broker symbol names, tried in order.",
    )

    # --- Timeframe architecture (section 5) -----------------------------
    tf_trend: Timeframe = Field(Timeframe.H4, alias="TF_TREND")
    tf_structure: Timeframe = Field(Timeframe.H1, alias="TF_STRUCTURE")
    tf_entry: Timeframe = Field(Timeframe.M15, alias="TF_ENTRY")

    # --- Strategy toggles ------------------------------------------------
    strategy_trend_pullback_enabled: bool = Field(
        True, alias="STRATEGY_TREND_PULLBACK_ENABLED"
    )
    strategy_breakout_enabled: bool = Field(True, alias="STRATEGY_BREAKOUT_ENABLED")
    strategy_mean_reversion_enabled: bool = Field(
        True, alias="STRATEGY_MEAN_REVERSION_ENABLED"
    )

    # --- Data freshness (section 35) --------------------------------------
    market_data_max_staleness_seconds: int = Field(
        120, alias="MARKET_DATA_MAX_STALENESS_SECONDS", gt=0
    )

    # --- News filter (section 18) ---------------------------------------------
    news_calendar_path: Optional[str] = Field(
        None,
        alias="NEWS_CALENDAR_PATH",
        description=(
            "Path to a CSV/JSON economic-calendar file (see app/news/calendar.py). "
            "When unset or missing, the news filter honestly reports itself unavailable "
            "rather than pretending to filter news it has no data for."
        ),
    )
    news_blackout_minutes_before: int = Field(30, alias="NEWS_BLACKOUT_MINUTES_BEFORE", ge=0)
    news_blackout_minutes_after: int = Field(30, alias="NEWS_BLACKOUT_MINUTES_AFTER", ge=0)

    @field_validator("news_calendar_path", mode="before")
    @classmethod
    def _empty_calendar_path_to_none(cls, v):
        return _blank_to_none(v)

    # --- Journal / storage -------------------------------------------------
    database_url: str = Field(
        "sqlite:///./data/trading_journal.db", alias="DATABASE_URL"
    )

    # --- Execution state / recovery (Phase 7) -------------------------------
    execution_state_db_path: str = Field(
        "./data/execution_state.db",
        alias="EXECUTION_STATE_DB_PATH",
        description=(
            "Path to the sqlite file ExecutionEngine uses to persist "
            "client_order_id outcomes (see app.execution.state_store), so an "
            "UNKNOWN/uncertain execution -- or the idempotency record of an "
            "already-FILLED order -- is never forgotten across a restart."
        ),
    )

    # --- Position monitor state / recovery (Phase 8) -------------------------
    position_monitor_state_db_path: str = Field(
        "./data/position_monitor_state.db",
        alias="POSITION_MONITOR_STATE_DB_PATH",
        description=(
            "Path to the sqlite file PositionMonitor uses to persist per-ticket "
            "management state (see app.positions.state_store) -- breakeven/"
            "partial-exit idempotency and any unresolved UNKNOWN modify/close "
            "outcome -- so it is never forgotten across a restart."
        ),
    )

    # --- Logging -------------------------------------------------------------
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_dir: str = Field("./data/logs", alias="LOG_DIR")

    risk: RiskSettings = Field(default_factory=RiskSettings)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
        extra="ignore",
    )

    @field_validator("mt5_use_mock")
    @classmethod
    def _forbid_live_with_mock(cls, v: bool, info) -> bool:
        # Cross-field validation (mode vs mock) is enforced in
        # `validate_live_safety` below, since pydantic v2 field_validator
        # doesn't reliably see sibling fields during construction order.
        return v

    def symbol_candidate_list(self) -> list[str]:
        return [s.strip() for s in self.symbol_candidates.split(",") if s.strip()]

    def validate_live_safety(self) -> None:
        """Extra safeguards required before LIVE mode may run (section 4, 17).

        Raises RuntimeError if any live-trading precondition is unmet.
        This must be called explicitly at startup and before any order
        submission path is enabled — it is not implicit in __init__ so
        that constructing Settings never itself throws for PAPER/BACKTEST.
        """
        if self.trading_mode != TradingMode.LIVE:
            return
        problems = []
        if self.mt5_use_mock:
            problems.append("MT5_USE_MOCK must be false for LIVE trading.")
        if self.kill_switch:
            problems.append("KILL_SWITCH is currently active; LIVE trading refused.")
        if not (self.mt5_login and self.mt5_password and self.mt5_server):
            problems.append(
                "MT5_LOGIN, MT5_PASSWORD and MT5_SERVER must all be set for LIVE trading."
            )
        if problems:
            raise RuntimeError(
                "Refusing to start in LIVE mode:\n- " + "\n- ".join(problems)
            )


def get_settings() -> Settings:
    """Factory (not a cached singleton) so tests can construct fresh
    Settings from custom env without leaking state between tests."""
    return Settings()
