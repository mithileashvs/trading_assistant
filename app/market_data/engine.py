"""
Market Data Engine (section 3, 35).

Responsibilities in Phase 1:
- Resolve the broker's actual tradable symbol from configured candidates.
- Fetch OHLCV / tick data through the MT5 adapter.
- Enforce a data-freshness check before that data is trusted downstream
  ("market_data_fresh" in the trade validator object, section 20).

Nothing here talks to MT5 directly — everything goes through IMT5Client.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from app.config.settings import Settings
from app.mt5.interface import IMT5Client, SymbolSpec, Tick


class SymbolDiscoveryError(RuntimeError):
    pass


class StaleMarketDataError(RuntimeError):
    pass


@dataclass
class ResolvedSymbol:
    name: str
    spec: SymbolSpec


class MarketDataEngine:
    def __init__(self, client: IMT5Client, settings: Settings):
        self._client = client
        self._settings = settings
        self._resolved: ResolvedSymbol | None = None

    # -- symbol discovery ------------------------------------------------
    def resolve_symbol(self) -> ResolvedSymbol:
        """Discover and cache the broker's actual symbol name for gold,
        trying each candidate in app.config Settings.symbol_candidates
        (section 3). Raises SymbolDiscoveryError if none work."""
        if self._resolved is not None:
            return self._resolved

        candidates = self._settings.symbol_candidate_list()
        name = self._client.discover_symbol(candidates)
        if name is None:
            raise SymbolDiscoveryError(
                f"None of the candidate symbols {candidates} are tradable on "
                "this broker. Update SYMBOL_CANDIDATES in your .env."
            )
        spec = self._client.get_symbol_spec(name)
        if not spec.trade_allowed:
            raise SymbolDiscoveryError(
                f"Symbol '{name}' was discovered but trading is disabled on it."
            )
        self._resolved = ResolvedSymbol(name=name, spec=spec)
        return self._resolved

    # -- data retrieval --------------------------------------------------
    def get_ohlcv(self, timeframe: str, count: int) -> pd.DataFrame:
        symbol = self.resolve_symbol().name
        df = self._client.get_ohlcv(symbol, timeframe, count)
        self._assert_fresh(df)
        return df

    def get_tick(self) -> Tick:
        symbol = self.resolve_symbol().name
        tick = self._client.get_tick(symbol)
        self._assert_tick_fresh(tick)
        return tick

    # -- freshness checks (section 35: "stale market data" is a failure mode) --
    def _assert_fresh(self, df: pd.DataFrame) -> None:
        if df.empty:
            raise StaleMarketDataError("OHLCV frame is empty.")
        last_ts = df.index[-1]
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize(timezone.utc)
        age = (datetime.now(timezone.utc) - last_ts).total_seconds()
        max_age = self._settings.market_data_max_staleness_seconds
        # Note: for lower timeframes this check needs a timeframe-aware
        # threshold in later phases (an H4 candle is legitimately "old"
        # for most of its 4-hour life). Phase 1 uses a single global
        # threshold intended for near-real-time (M15 and below) checks.
        if age > max_age:
            raise StaleMarketDataError(
                f"Last candle is {age:.0f}s old, exceeds max staleness of {max_age}s."
            )

    def _assert_tick_fresh(self, tick: Tick) -> None:
        age = (datetime.now(timezone.utc) - tick.time).total_seconds()
        max_age = self._settings.market_data_max_staleness_seconds
        if age > max_age:
            raise StaleMarketDataError(
                f"Tick is {age:.0f}s old, exceeds max staleness of {max_age}s."
            )
