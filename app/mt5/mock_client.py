"""
Deterministic mock MT5 client.

This exists because the real `MetaTrader5` package requires an actual
MT5 terminal process (Windows, or Wine) connected to a live broker —
something this development sandbox cannot provide. The mock produces
synthetic-but-realistic OHLCV data (seeded random walk) so the rest of
the pipeline (features, regimes, strategies, risk, backtesting) can be
built and unit-tested end-to-end without a broker connection.

MULTI-TIMEFRAME COHERENCE: H4, H1, and M15 for a given symbol are all
derived from ONE underlying M15 series (see _get_m15_base_series /
get_ohlcv) — never independently generated per timeframe. Aggregating
the M15 series this client returns must reproduce the H1/H4 series it
returns for the same symbol, bar-for-bar over any overlapping window;
see tests/test_mock_mt5_client.py for the tests that verify this.

It must NEVER be used for LIVE trading — see
Settings.validate_live_safety(), which refuses to start LIVE while
MT5_USE_MOCK=true.

DETERMINISTIC FAULT INJECTION (Phase 6): by default every method below
behaves as it always has (successful, deterministic-but-synthetic
fills) — none of what follows changes default behavior. Tests that
need to exercise realistic broker failure modes opt in explicitly,
per call or per some persistent condition, via the methods in the
"-- fault injection (Phase 6) --" section below:

  - queue_connection_lost(applies_to): the NEXT call to that method
    ("submit_order" | "get_account_info" | "get_tick" | "close_position"
    | "close_position_partial" | "modify_position" -- the last three
    added in Phase 7 to test close/modify recovery) raises the
    existing app.mt5.real_client.MT5ConnectionError (reused, not
    reinvented) instead of doing anything — simulates a connection
    that dropped BEFORE the call could reach the broker. Safe to
    treat as "nothing happened".
  - queue_unknown_execution_result(): the NEXT submit_order() call
    returns (does not raise) success=False with
    raw["status"] == EXECUTION_STATUS_UNKNOWN — simulates a
    connection dropping AFTER a request was sent but BEFORE a
    confirmation came back. Genuinely uncertain: no position is
    created (we don't know one exists), but see
    ExecutionEngine.submit_market_order for how this is also
    prevented from allowing a blind resubmission under the same
    client_order_id.
  - queue_order_rejection(comment, retcode): the NEXT submit_order()
    call is cleanly rejected with the given reason — for modeling a
    broker/server-side rejection that isn't one of the specific,
    state-driven rejection reasons below.
  - set_reconnect_unavailable(True): connect() itself starts raising
    MT5ConnectionError — simulates a broker/terminal that can't be
    reached again after a connection was lost.
  - set_spread_points / set_slippage_points / set_tick_state /
    set_symbol_known / set_trading_disabled / set_market_closed /
    set_margin_insufficient: persistent (not one-shot) market/account
    conditions. These drive REAL validation inside submit_order (not
    a scripted response) — e.g. an order for a symbol outside
    volume_min/volume_max is rejected because it genuinely fails that
    check, the same way it would against a real broker.

None of this ever talks to a real broker; it only changes what this
in-memory mock returns. See tests/test_phase6_mock_execution.py.
"""
from __future__ import annotations

import itertools
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

from app.mt5.interface import (
    EXECUTION_STATUS_FILLED,
    EXECUTION_STATUS_REJECTED,
    EXECUTION_STATUS_UNKNOWN,
    STALE_TICK_SECONDS,
    AccountInfo,
    IMT5Client,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
)
from app.mt5.real_client import MT5ConnectionError  # reused, not reinvented -- see module docstring


def _stable_seed(*parts: str) -> int:
    """A process-stable seed derived from string parts.

    Python's built-in hash() is intentionally randomized per-process for
    strings (PYTHONHASHSEED, a security feature since Python 3.3) --
    using it to seed a numpy Generator means the "same" seed produces a
    DIFFERENT random sequence every time a fresh Python process runs,
    even though it's stable within one process's lifetime. That's
    invisible for short sequences (limited accumulated drift) but
    becomes a real reproducibility bug for long ones (e.g. the 20,000-bar
    base M15 series below, where drift over that many bars can easily
    swing the resulting "current price" by hundreds of dollars between
    process runs). zlib.crc32 is deterministic across processes, so the
    same symbol always seeds the same price path everywhere.
    """
    joined = "|".join(parts).encode("utf-8")
    return zlib.crc32(joined)


_TF_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


@dataclass
class _Fault:
    """A one-shot fault, consumed the first time the matching method is
    called (see MockMT5Client._pop_fault)."""
    applies_to: str  # "submit_order" | "get_account_info" | "get_tick" |
                      # "close_position" | "close_position_partial" | "modify_position" (Phase 7)
    kind: str  # "connection_lost" | "unknown_result" | "rejected"
    retcode: Optional[int] = None
    comment: str = ""
    # Phase 7: only meaningful for kind == "unknown_result". See
    # queue_unknown_execution_result().
    broker_filled: bool = False


class MockMT5Client(IMT5Client):
    # Generous default buffer of M15 bars kept per symbol (~208 days).
    # H1/H4/D1 requests are aggregated from this SAME series rather than
    # generated independently, so all timeframes for a symbol always
    # represent one coherent underlying timeline. If a caller ever
    # requests more M15 history than this buffer holds, the buffer is
    # regenerated larger (still seeded deterministically by symbol) --
    # see _get_m15_base_series().
    _DEFAULT_BASE_M15_BARS = 20_000

    _AGG = {"open": "first", "high": "max", "low": "min", "close": "last",
            "tick_volume": "sum", "spread": "mean"}
    _HIGHER_TF_RULE = {"H1": "1h", "H4": "4h", "D1": "1D"}

    def __init__(self, seed: int = 42, base_price: float = 2650.0):
        self._connected = False
        self._rng = np.random.default_rng(seed)
        self._base_price = base_price
        self._positions: dict[int, Position] = {}
        self._ticket_counter = itertools.count(start=100000)
        self._known_symbols = {"XAUUSD", "XAUUSDm", "GOLD", "GOLDm"}
        self._current_symbol = "XAUUSD"
        self._client_order_ids: set[str] = set()
        self._m15_base: dict[str, pd.DataFrame] = {}  # symbol -> cached coherent M15 series

        # -- Phase 6: deterministic fault injection state. All default
        # to "no fault" -- existing/default behavior is unchanged. --
        self._pending_faults: list[_Fault] = []
        self._reconnect_unavailable: bool = False
        self._spread_points_override: Optional[float] = None
        self._slippage_points: float = 0.0
        self._tick_state: Optional[str] = None  # None | "stale" | "missing" | "invalid"
        self._trading_disabled_symbols: set[str] = set()
        self._market_closed: bool = False
        self._margin_insufficient: bool = False

    # -- fault injection (Phase 6) --------------------------------------
    def _pop_fault(self, applies_to: str) -> Optional[_Fault]:
        for i, f in enumerate(self._pending_faults):
            if f.applies_to == applies_to:
                return self._pending_faults.pop(i)
        return None

    def queue_connection_lost(self, applies_to: str = "submit_order") -> None:
        """The NEXT call to `applies_to` raises MT5ConnectionError,
        simulating a connection that dropped BEFORE that call could do
        anything at the broker. `applies_to` must be one of
        "submit_order", "get_account_info", "get_tick", "close_position",
        "close_position_partial", "modify_position" (the last three
        added in Phase 7 to test close/modify recovery)."""
        if applies_to not in (
            "submit_order", "get_account_info", "get_tick",
            "close_position", "close_position_partial", "modify_position",
        ):
            raise ValueError(f"Unsupported applies_to={applies_to!r}")
        self._pending_faults.append(_Fault(applies_to, "connection_lost"))

    def queue_unknown_execution_result(self, broker_filled: bool = False) -> None:
        """The NEXT submit_order() call simulates a connection that
        dropped AFTER the order was sent but BEFORE a confirmation came
        back -- broker acceptance is genuinely undetermined. Returns
        (does not raise) an OrderResult with success=False and
        raw["status"] == EXECUTION_STATUS_UNKNOWN.

        broker_filled=False (default, Phase 6 behavior): no position is
        created, since we cannot know one exists -- models "the order
        never actually reached the broker".

        broker_filled=True (Phase 7): models the OTHER real-world half
        of this same fault -- the broker actually accepted and filled
        the order, but the CONFIRMATION back to the caller was what got
        lost. A real position is created on this mock's broker-side
        state (tagged with the same request.comment the caller sent),
        even though the caller still receives UNKNOWN, exactly so
        ExecutionEngine.reconcile_unknown() has something deterministic
        to find. Whichever variant is used, the caller-visible result is
        identical (UNKNOWN) -- only the broker's ground truth differs,
        which is the whole point: from the caller's side, the two are
        indistinguishable without reconciliation."""
        self._pending_faults.append(_Fault("submit_order", "unknown_result", broker_filled=broker_filled))

    def queue_order_rejection(self, comment: str, retcode: Optional[int] = None) -> None:
        """The NEXT submit_order() call is cleanly rejected with the
        given reason (for generic/broker-server rejections that aren't
        one of the specific state-driven checks below)."""
        self._pending_faults.append(_Fault("submit_order", "rejected", retcode=retcode, comment=comment))

    def set_reconnect_unavailable(self, unavailable: bool = True) -> None:
        """Makes subsequent connect() calls raise MT5ConnectionError --
        simulates a broker/terminal that cannot be reached again after
        a connection was lost."""
        self._reconnect_unavailable = unavailable

    def set_spread_points(self, points: Optional[float]) -> None:
        """Overrides the bid/ask spread used by get_tick (in the same
        price units as the symbol, e.g. USD for XAUUSD). None restores
        the default simulated spread (0.20)."""
        self._spread_points_override = points

    def set_slippage_points(self, points: float) -> None:
        """Deterministic price slippage applied to every fill (open,
        close, partial close) until changed back. Positive always moves
        the fill price AGAINST the trader (a higher price on a BUY, a
        lower price on a SELL); negative is the mirror -- a
        better-than-quoted fill. 0.0 (the default) applies none."""
        self._slippage_points = points

    def set_tick_state(self, state: Optional[str]) -> None:
        """None (default/fresh) | "stale" | "missing" | "invalid".
        Persistent until changed. Affects get_tick's returned Tick and,
        via submit_order's own pre-fill validation, causes orders to be
        cleanly rejected rather than filled against bad market data."""
        if state not in (None, "stale", "missing", "invalid"):
            raise ValueError(f"Unsupported tick state={state!r}")
        self._tick_state = state

    def set_symbol_known(self, symbol: str, known: bool = True) -> None:
        """Add/remove `symbol` from the set of symbols this mock broker
        recognizes -- simulates an unknown/unavailable symbol."""
        if known:
            self._known_symbols.add(symbol)
        else:
            self._known_symbols.discard(symbol)

    def set_trading_disabled(self, symbol: str, disabled: bool = True) -> None:
        """Symbol exists and is visible but trading is disabled on it
        (broker-side), independent of whatever SymbolSpec.trade_allowed
        was snapshotted earlier by a caller."""
        if disabled:
            self._trading_disabled_symbols.add(symbol)
        else:
            self._trading_disabled_symbols.discard(symbol)

    def set_market_closed(self, closed: bool = True) -> None:
        self._market_closed = closed

    def set_margin_insufficient(self, insufficient: bool = True) -> None:
        self._margin_insufficient = insufficient

    def reset_fault_state(self) -> None:
        """Convenience for tests: clears every Phase 6 fault-injection
        setting back to its default (no faults, normal market)."""
        self._pending_faults.clear()
        self._reconnect_unavailable = False
        self._spread_points_override = None
        self._slippage_points = 0.0
        self._tick_state = None
        self._trading_disabled_symbols.clear()
        self._market_closed = False
        self._margin_insufficient = False

    def _apply_slippage(self, price: float, direction: str) -> float:
        if self._slippage_points == 0:
            return price
        sign = 1 if direction == "BUY" else -1
        return round(price + sign * self._slippage_points, 2)

    def _rejected(self, retcode: Optional[int], comment: str) -> OrderResult:
        return OrderResult(success=False, order_id=None, deal_id=None, price=None, volume=None,
                            retcode=retcode, comment=comment, raw={"status": EXECUTION_STATUS_REJECTED})

    # -- connection ---------------------------------------------------
    def connect(self) -> bool:
        if self._reconnect_unavailable:
            raise MT5ConnectionError("Mock broker: reconnect unavailable (simulated).")
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # -- symbol discovery (section 3) ----------------------------------
    def discover_symbol(self, candidates: list[str]) -> Optional[str]:
        for candidate in candidates:
            if candidate in self._known_symbols:
                self._current_symbol = candidate
                return candidate
        return None

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self._known_symbols:
            raise ValueError(f"Unknown symbol '{symbol}' in mock broker.")
        return SymbolSpec(
            name=symbol,
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

    # -- account --------------------------------------------------------
    def get_account_info(self) -> AccountInfo:
        fault = self._pop_fault("get_account_info")
        if fault is not None and fault.kind == "connection_lost":
            raise MT5ConnectionError("Mock broker: connection lost while fetching account info.")
        equity = 10_000.0 + sum(p.profit for p in self._positions.values())
        margin_free = 0.0 if self._margin_insufficient else equity
        return AccountInfo(
            login=999999,
            balance=10_000.0,
            equity=equity,
            margin=0.0,
            margin_free=margin_free,
            currency="USD",
            leverage=100,
            trade_allowed=True,
            is_demo=True,  # the mock client is inherently a simulation
        )

    # -- market data ------------------------------------------------------
    def get_tick(self, symbol: str) -> Tick:
        fault = self._pop_fault("get_tick")
        if fault is not None and fault.kind == "connection_lost":
            raise MT5ConnectionError(f"Mock broker: connection lost while fetching the tick for {symbol}.")
        if self._tick_state == "missing":
            # Mirrors RealMT5Client.get_tick's own behavior when
            # symbol_info_tick() returns None -- "no quote available"
            # is a real MT5 failure mode, not a mock-only concept, so
            # it's surfaced the same way (raise, don't return a
            # fabricated Tick) regardless of which caller reaches this
            # (submit_order short-circuits before ever getting here for
            # this exact case -- see below -- but ANY other caller,
            # e.g. ExecutionEngine's PAPER-mode fill simulation or
            # MarketDataEngine, must see the same failure).
            raise MT5ConnectionError(f"Mock broker: no current tick available for {symbol} (simulated).")

        # Anchored around the latest known M15 close (if any history has
        # been generated yet for this symbol) rather than the static
        # base_price, so the "current" quote stays consistent with the
        # most recent OHLCV data instead of floating independently.
        base = self._m15_base.get(symbol)
        anchor = float(base["close"].iloc[-1]) if base is not None and len(base) else self._base_price
        price = anchor + self._rng.normal(0, 0.5)
        spread = self._spread_points_override if self._spread_points_override is not None else 0.20

        tick_time = datetime.now(timezone.utc)
        if self._tick_state == "stale":
            # Comfortably past STALE_TICK_SECONDS so any freshness check
            # against it fails unambiguously, not by a hair.
            tick_time = tick_time - timedelta(seconds=STALE_TICK_SECONDS + 60)

        bid = round(price, 2)
        ask = round(price + spread, 2)
        if self._tick_state == "invalid":
            # A structurally invalid quote (crossed market: ask < bid) --
            # never a legitimate broker quote, used to verify callers
            # refuse to fill against it rather than silently using it.
            bid, ask = round(price, 2), round(price - abs(spread) - 0.01, 2)

        return Tick(
            symbol=symbol,
            time=tick_time,
            bid=bid,
            ask=ask,
            last=bid,
            volume=1.0,
        )

    def _generate_m15_series(self, symbol: str, num_bars: int) -> pd.DataFrame:
        """The single source-of-truth M15 series for `symbol`. Seeded by
        the SYMBOL ONLY (never the timeframe) -- this is what makes
        H1/H4/D1 (aggregated from this same series, see get_ohlcv)
        represent the same underlying timeline as the M15 data, instead
        of each timeframe being an independently-generated random walk."""
        local_rng = np.random.default_rng(_stable_seed(symbol))
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        now -= timedelta(minutes=now.minute % 15)  # align to a real M15 boundary
        times = [now - timedelta(minutes=15 * i) for i in range(num_bars)][::-1]

        returns = local_rng.normal(0, 0.0009, size=num_bars)
        close = self._base_price * np.cumprod(1 + returns)
        open_ = np.roll(close, 1)
        open_[0] = self._base_price
        high = np.maximum(open_, close) * (1 + np.abs(local_rng.normal(0, 0.0006, num_bars)))
        low = np.minimum(open_, close) * (1 - np.abs(local_rng.normal(0, 0.0006, num_bars)))
        tick_volume = local_rng.integers(50, 500, size=num_bars)
        spread = local_rng.integers(10, 40, size=num_bars)

        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close,
             "tick_volume": tick_volume, "spread": spread},
            index=pd.DatetimeIndex(times, name="time"),
        ).round(2)

    def _get_m15_base_series(self, symbol: str, min_bars: int) -> pd.DataFrame:
        needed = max(min_bars, self._DEFAULT_BASE_M15_BARS)
        cached = self._m15_base.get(symbol)
        if cached is None or len(cached) < needed:
            cached = self._generate_m15_series(symbol, needed)
            self._m15_base[symbol] = cached
        return cached

    def _legacy_independent_ohlcv(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """M1/M5 are FINER than the M15 base series and cannot be
        derived from it (aggregation only works coarser, never finer).
        Neither timeframe is used anywhere in this codebase's live
        pipeline (only H4/H1/M15 are) -- this independently-seeded
        fallback is kept only so the two enum values remain callable,
        clearly documented rather than silently implying a coherence
        guarantee that isn't actually possible here."""
        minutes = _TF_MINUTES[timeframe]
        local_rng = np.random.default_rng(_stable_seed(symbol, timeframe))
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        times = [now - timedelta(minutes=minutes * i) for i in range(count)][::-1]

        returns = local_rng.normal(0, 0.0009, size=count)
        close = self._base_price * np.cumprod(1 + returns)
        open_ = np.roll(close, 1)
        open_[0] = self._base_price
        high = np.maximum(open_, close) * (1 + np.abs(local_rng.normal(0, 0.0006, count)))
        low = np.minimum(open_, close) * (1 - np.abs(local_rng.normal(0, 0.0006, count)))
        tick_volume = local_rng.integers(50, 500, size=count)
        spread = local_rng.integers(10, 40, size=count)

        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close,
             "tick_volume": tick_volume, "spread": spread},
            index=pd.DatetimeIndex(times, name="time"),
        ).round(2)

    def get_ohlcv(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        if timeframe not in _TF_MINUTES:
            raise ValueError(f"Unsupported timeframe '{timeframe}'")

        if timeframe == "M15":
            base = self._get_m15_base_series(symbol, count)
            return base.tail(count).round(2)

        if timeframe in self._HIGHER_TF_RULE:
            # Enough M15 bars to produce `count` COMPLETE higher-timeframe
            # candles, plus a small buffer for boundary alignment.
            m15_bars_needed = (_TF_MINUTES[timeframe] * count) // 15 + 32
            base = self._get_m15_base_series(symbol, m15_bars_needed)
            rule = self._HIGHER_TF_RULE[timeframe]
            resampled = base.resample(rule, label="left", closed="left").agg(self._AGG).dropna()
            return resampled.tail(count).round(2)

        # M1 / M5 -- see _legacy_independent_ohlcv's docstring.
        return self._legacy_independent_ohlcv(symbol, timeframe, count)

    # -- positions / trading ------------------------------------------------
    def get_open_positions(self, symbol: Optional[str] = None) -> list[Position]:
        positions = list(self._positions.values())
        if symbol:
            positions = [p for p in positions if p.symbol == symbol]
        return positions

    def submit_order(self, request: OrderRequest) -> OrderResult:
        # --- one-shot faults (Phase 6) ------------------------------------
        fault = self._pop_fault("submit_order")
        if fault is not None:
            if fault.kind == "connection_lost":
                raise MT5ConnectionError("Mock broker: connection lost before the order could be sent.")
            if fault.kind == "unknown_result":
                # Duplicate protection must hold here too, not just at
                # ExecutionEngine's layer: record client_order_id as
                # "seen" even though the outcome is uncertain, so a
                # SECOND direct call to submit_order() with the same id
                # is also rejected (as DUPLICATE_ORDER_REJECTED below)
                # rather than being free to create a real fill. Without
                # this, the mock's own duplicate protection would have
                # a blind spot specifically for the UNKNOWN case, while
                # covering every other case -- an asymmetry with no
                # good reason to exist.
                if request.client_order_id:
                    self._client_order_ids.add(request.client_order_id)
                # Phase 7: optionally simulate that the broker actually
                # filled the order despite the lost confirmation -- see
                # queue_unknown_execution_result()'s docstring. Best-effort:
                # if no usable tick is available, the broker-side fill is
                # simply skipped rather than raising, since that's just as
                # valid a ground truth for testing the "genuinely never
                # happened" reconciliation branch.
                if fault.broker_filled:
                    try:
                        tick = self.get_tick(request.symbol)
                        if tick.ask > tick.bid > 0:
                            fill_price = tick.ask if request.direction == "BUY" else tick.bid
                            fill_price = self._apply_slippage(fill_price, request.direction)
                            ticket = next(self._ticket_counter)
                            self._positions[ticket] = Position(
                                ticket=ticket,
                                symbol=request.symbol,
                                direction=request.direction,
                                volume=request.volume,
                                price_open=fill_price,
                                price_current=fill_price,
                                stop_loss=request.stop_loss,
                                take_profit=request.take_profit,
                                profit=0.0,
                                open_time=datetime.now(timezone.utc),
                                magic=request.magic,
                                comment=request.comment,
                            )
                    except Exception:  # noqa: BLE001 - best-effort broker-side simulation only
                        pass
                return OrderResult(
                    success=False, order_id=None, deal_id=None, price=None, volume=None, retcode=None,
                    comment="UNKNOWN_EXECUTION_RESULT: connection lost after the order was sent; "
                            "broker acceptance could not be confirmed",
                    raw={"status": EXECUTION_STATUS_UNKNOWN},
                )
            if fault.kind == "rejected":
                return self._rejected(fault.retcode, fault.comment or "ORDER_REJECTED")

        # --- client-level duplicate protection (existing, unchanged) -----
        if request.client_order_id and request.client_order_id in self._client_order_ids:
            return OrderResult(
                success=False,
                order_id=None,
                deal_id=None,
                price=None,
                volume=None,
                retcode=-1,
                comment="DUPLICATE_ORDER_REJECTED",
                raw={"status": EXECUTION_STATUS_REJECTED},
            )

        # --- realistic broker-side pre-trade validation (Phase 6) ---------
        if request.symbol not in self._known_symbols:
            return self._rejected(-1, f"INVALID_SYMBOL: '{request.symbol}' is not known to the mock broker")
        if request.symbol in self._trading_disabled_symbols:
            return self._rejected(-1, f"TRADING_DISABLED: trading is disabled for '{request.symbol}'")
        if self._market_closed:
            return self._rejected(-1, "MARKET_CLOSED: the market is currently closed")

        spec = self.get_symbol_spec(request.symbol)
        if not (spec.volume_min <= request.volume <= spec.volume_max):
            return self._rejected(
                -1, f"INVALID_VOLUME: {request.volume} is outside the broker's allowed range "
                    f"[{spec.volume_min}, {spec.volume_max}]"
            )
        steps = round((request.volume - spec.volume_min) / spec.volume_step)
        aligned = round(spec.volume_min + steps * spec.volume_step, 8)
        if abs(aligned - request.volume) > 1e-8:
            return self._rejected(
                -1, f"INVALID_VOLUME: {request.volume} does not align to the broker's volume step "
                    f"({spec.volume_step})"
            )

        if self._margin_insufficient:
            return self._rejected(-1, "INSUFFICIENT_MARGIN: not enough free margin for this order")

        if self._tick_state == "missing":
            # Knowable BEFORE anything would be sent to the broker --
            # a clean rejection, not an uncertain outcome (contrast
            # with queue_connection_lost, which deliberately raises to
            # simulate genuine mid-flight uncertainty).
            return self._rejected(-1, "NO_QUOTE: no current tick available for this symbol; order not sent")

        tick = self.get_tick(request.symbol)
        if tick.ask <= tick.bid or tick.bid <= 0:
            return self._rejected(
                -1, "INVALID_QUOTE: current tick is crossed or non-positive; refusing to fill on invalid "
                    "market data"
            )
        if (datetime.now(timezone.utc) - tick.time).total_seconds() > STALE_TICK_SECONDS:
            return self._rejected(-1, "STALE_QUOTE: current tick is too old to fill against")

        if request.stop_loss is not None or request.take_profit is not None:
            min_distance = spec.stops_level_points * spec.tick_size
            ref_price = tick.ask if request.direction == "BUY" else tick.bid
            for label, level in (("stop_loss", request.stop_loss), ("take_profit", request.take_profit)):
                if level is not None and abs(ref_price - level) < min_distance:
                    return self._rejected(
                        -1, f"INVALID_STOPS: {label}={level} is closer than the broker's minimum stop "
                            f"distance ({min_distance}) from the current price ({ref_price})"
                    )

        if request.client_order_id:
            self._client_order_ids.add(request.client_order_id)

        fill_price = tick.ask if request.direction == "BUY" else tick.bid
        fill_price = self._apply_slippage(fill_price, request.direction)
        ticket = next(self._ticket_counter)
        self._positions[ticket] = Position(
            ticket=ticket,
            symbol=request.symbol,
            direction=request.direction,
            volume=request.volume,
            price_open=fill_price,
            price_current=fill_price,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            profit=0.0,
            open_time=datetime.now(timezone.utc),
            magic=request.magic,
            comment=request.comment,
        )
        return OrderResult(
            success=True,
            order_id=ticket,
            deal_id=ticket,
            price=fill_price,
            volume=request.volume,
            retcode=10009,  # TRADE_RETCODE_DONE, mirroring real MT5
            comment="FILLED",
            raw={"status": EXECUTION_STATUS_FILLED},
        )

    def close_position(self, ticket: int) -> OrderResult:
        # Look up (don't remove yet) -- see the module-level note below
        # this method for why fetching the tick BEFORE mutating state
        # matters: a failure fetching the tick must never cause the
        # position to vanish from tracking while its close was never
        # actually confirmed.
        fault = self._pop_fault("close_position")
        if fault is not None and fault.kind == "connection_lost":
            raise MT5ConnectionError("Mock broker: connection lost while closing the position.")
        pos = self._positions.get(ticket)
        if pos is None:
            return OrderResult(
                success=False,
                order_id=None,
                deal_id=None,
                price=None,
                volume=None,
                retcode=-1,
                comment="POSITION_NOT_FOUND",
            )
        tick = self.get_tick(pos.symbol)  # may raise -- pos is still tracked if it does (see above)
        close_price = tick.bid if pos.direction == "BUY" else tick.ask
        # Closing is the opposite side of opening, so slippage's
        # "always against the trader" sign flips too: a worse CLOSE
        # price on a BUY position means a LOWER price, which is the
        # SELL-side application of the same helper.
        closing_direction = "SELL" if pos.direction == "BUY" else "BUY"
        close_price = self._apply_slippage(close_price, closing_direction)
        # Only remove the position from tracking once we have
        # everything needed to report a definite, successful close --
        # never before.
        del self._positions[ticket]
        return OrderResult(
            success=True,
            order_id=pos.ticket,
            deal_id=pos.ticket,
            price=close_price,
            volume=pos.volume,
            retcode=10009,
            comment="CLOSED",
            raw={"status": EXECUTION_STATUS_FILLED},
        )

    def close_position_partial(self, ticket: int, volume: float) -> OrderResult:
        fault = self._pop_fault("close_position_partial")
        if fault is not None and fault.kind == "connection_lost":
            raise MT5ConnectionError("Mock broker: connection lost while partially closing the position.")
        pos = self._positions.get(ticket)
        if pos is None:
            return OrderResult(
                success=False, order_id=None, deal_id=None, price=None,
                volume=None, retcode=-1, comment="POSITION_NOT_FOUND",
            )
        if volume <= 0 or volume >= pos.volume:
            return OrderResult(
                success=False, order_id=None, deal_id=None, price=None,
                volume=None, retcode=-1,
                comment="INVALID_PARTIAL_VOLUME (must be > 0 and < full position volume)",
            )
        tick = self.get_tick(pos.symbol)
        close_price = tick.bid if pos.direction == "BUY" else tick.ask
        closing_direction = "SELL" if pos.direction == "BUY" else "BUY"
        close_price = self._apply_slippage(close_price, closing_direction)
        pos.volume = round(pos.volume - volume, 8)
        return OrderResult(
            success=True,
            order_id=pos.ticket,
            deal_id=pos.ticket,
            price=close_price,
            volume=volume,
            retcode=10009,
            comment="PARTIALLY_CLOSED",
            raw={"status": EXECUTION_STATUS_FILLED},
        )

    def modify_position(
        self, ticket: int, stop_loss: Optional[float], take_profit: Optional[float]
    ) -> OrderResult:
        fault = self._pop_fault("modify_position")
        if fault is not None and fault.kind == "connection_lost":
            raise MT5ConnectionError("Mock broker: connection lost while modifying the position.")
        pos = self._positions.get(ticket)
        if pos is None:
            return OrderResult(
                success=False,
                order_id=None,
                deal_id=None,
                price=None,
                volume=None,
                retcode=-1,
                comment="POSITION_NOT_FOUND",
            )
        if stop_loss is not None:
            pos.stop_loss = stop_loss
        if take_profit is not None:
            pos.take_profit = take_profit
        return OrderResult(
            success=True,
            order_id=pos.ticket,
            deal_id=None,
            price=pos.price_current,
            volume=pos.volume,
            retcode=10009,
            comment="MODIFIED",
        )
