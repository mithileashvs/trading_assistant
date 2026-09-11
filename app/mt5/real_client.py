"""
Real MT5 client, wrapping the `MetaTrader5` pip package.

IMPORTANT: `MetaTrader5` only works when an actual MT5 terminal is
installed and running on the host (Windows natively, or via Wine on
Linux/Mac). It cannot connect to a broker from a headless Linux
container with no terminal — attempting to import/use this module in
such an environment will raise a clear ImportError/RuntimeError rather
than silently failing or fabricating data.

Never hard-code credentials here. They come from Settings (env vars).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from app.mt5.interface import (
    AccountInfo,
    IMT5Client,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
)

try:
    import MetaTrader5 as mt5  # type: ignore
    _MT5_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only where package missing
    mt5 = None
    _MT5_AVAILABLE = False

_TF_MAP = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


class MT5ConnectionError(RuntimeError):
    pass


class RealMT5Client(IMT5Client):
    def __init__(
        self,
        login: Optional[int],
        password: Optional[str],
        server: Optional[str],
        terminal_path: Optional[str] = None,
    ):
        if not _MT5_AVAILABLE:
            raise MT5ConnectionError(
                "The 'MetaTrader5' package is not usable in this environment "
                "(no MT5 terminal available). Install it on a Windows host "
                "(or Wine) with the MT5 terminal running, or set "
                "MT5_USE_MOCK=true for development."
            )
        self._login = login
        self._password = password
        self._server = server
        self._terminal_path = terminal_path
        self._connected = False

    def connect(self) -> bool:
        kwargs = {}
        if self._terminal_path:
            kwargs["path"] = self._terminal_path
        ok = mt5.initialize(**kwargs)
        if not ok:
            raise MT5ConnectionError(f"mt5.initialize failed: {mt5.last_error()}")
        if self._login and self._password and self._server:
            ok = mt5.login(
                login=self._login, password=self._password, server=self._server
            )
            if not ok:
                raise MT5ConnectionError(f"mt5.login failed: {mt5.last_error()}")
        self._connected = True
        return True

    def disconnect(self) -> None:
        if _MT5_AVAILABLE:
            mt5.shutdown()
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def discover_symbol(self, candidates: list[str]) -> Optional[str]:
        for candidate in candidates:
            info = mt5.symbol_info(candidate)
            if info is None:
                continue
            if not info.visible:
                mt5.symbol_select(candidate, True)
                info = mt5.symbol_info(candidate)
            if info is not None and info.trade_mode != mt5.SYMBOL_TRADE_MODE_DISABLED:
                return candidate
        return None

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise ValueError(f"Symbol '{symbol}' not found on broker.")
        return SymbolSpec(
            name=symbol,
            contract_size=info.trade_contract_size,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
            tick_size=info.trade_tick_size,
            tick_value=info.trade_tick_value,
            digits=info.digits,
            stops_level_points=info.trade_stops_level,
            freeze_level_points=info.trade_freeze_level,
            trade_allowed=info.trade_mode != mt5.SYMBOL_TRADE_MODE_DISABLED,
            spread_points=info.spread,
        )

    def get_account_info(self) -> AccountInfo:
        info = mt5.account_info()
        if info is None:
            raise MT5ConnectionError(f"account_info() failed: {mt5.last_error()}")
        return AccountInfo(
            login=info.login,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            margin_free=info.margin_free,
            currency=info.currency,
            leverage=info.leverage,
            trade_allowed=info.trade_allowed,
            is_demo=(getattr(info, "trade_mode", None) == getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", -1)),
        )

    def get_tick(self, symbol: str) -> Tick:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5ConnectionError(f"symbol_info_tick failed for {symbol}: {mt5.last_error()}")
        return Tick(
            symbol=symbol,
            time=datetime.fromtimestamp(tick.time, tz=timezone.utc),
            bid=tick.bid,
            ask=tick.ask,
            last=tick.last,
            volume=tick.volume,
        )

    def get_ohlcv(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        if timeframe not in _TF_MAP:
            raise ValueError(f"Unsupported timeframe '{timeframe}'")
        tf_const = getattr(mt5, _TF_MAP[timeframe])
        rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, count)
        if rates is None or len(rates) == 0:
            raise MT5ConnectionError(
                f"copy_rates_from_pos returned no data for {symbol}/{timeframe}: "
                f"{mt5.last_error()}"
            )
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
        df = df.rename(columns={"tick_volume": "tick_volume"})
        return df[["open", "high", "low", "close", "tick_volume", "spread"]]

    def get_open_positions(self, symbol: Optional[str] = None) -> list[Position]:
        raw = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if raw is None:
            return []
        out = []
        for p in raw:
            out.append(
                Position(
                    ticket=p.ticket,
                    symbol=p.symbol,
                    direction="BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                    volume=p.volume,
                    price_open=p.price_open,
                    price_current=p.price_current,
                    stop_loss=p.sl or None,
                    take_profit=p.tp or None,
                    profit=p.profit,
                    open_time=datetime.fromtimestamp(p.time, tz=timezone.utc),
                    magic=p.magic,
                    comment=p.comment,
                )
            )
        return out

    def submit_order(self, request: OrderRequest) -> OrderResult:
        order_type = mt5.ORDER_TYPE_BUY if request.direction == "BUY" else mt5.ORDER_TYPE_SELL
        tick = self.get_tick(request.symbol)
        price = tick.ask if request.direction == "BUY" else tick.bid
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": request.symbol,
            "volume": request.volume,
            "type": order_type,
            "price": price,
            "sl": request.stop_loss or 0.0,
            "tp": request.take_profit or 0.0,
            "magic": request.magic,
            "comment": request.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(req)
        if result is None:
            return OrderResult(
                success=False, order_id=None, deal_id=None, price=None,
                volume=None, retcode=None, comment=f"order_send returned None: {mt5.last_error()}",
            )
        success = result.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(
            success=success,
            order_id=result.order,
            deal_id=result.deal,
            price=result.price,
            volume=result.volume,
            retcode=result.retcode,
            comment=result.comment,
            raw=result._asdict() if hasattr(result, "_asdict") else {},
        )

    def close_position(self, ticket: int) -> OrderResult:
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return OrderResult(
                success=False, order_id=None, deal_id=None, price=None,
                volume=None, retcode=-1, comment="POSITION_NOT_FOUND",
            )
        pos = positions[0]
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = self.get_tick(pos.symbol)
        price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(req)
        success = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(
            success=success,
            order_id=getattr(result, "order", None),
            deal_id=getattr(result, "deal", None),
            price=getattr(result, "price", None),
            volume=getattr(result, "volume", None),
            retcode=getattr(result, "retcode", None),
            comment=getattr(result, "comment", "CLOSE_FAILED"),
        )

    def close_position_partial(self, ticket: int, volume: float) -> OrderResult:
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return OrderResult(
                success=False, order_id=None, deal_id=None, price=None,
                volume=None, retcode=-1, comment="POSITION_NOT_FOUND",
            )
        pos = positions[0]
        if volume <= 0 or volume >= pos.volume:
            return OrderResult(
                success=False, order_id=None, deal_id=None, price=None,
                volume=None, retcode=-1,
                comment="INVALID_PARTIAL_VOLUME (must be > 0 and < full position volume)",
            )
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = self.get_tick(pos.symbol)
        price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(req)
        success = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(
            success=success,
            order_id=getattr(result, "order", None),
            deal_id=getattr(result, "deal", None),
            price=getattr(result, "price", None),
            volume=getattr(result, "volume", None),
            retcode=getattr(result, "retcode", None),
            comment=getattr(result, "comment", "PARTIAL_CLOSE_FAILED"),
        )

    def modify_position(
        self, ticket: int, stop_loss: Optional[float], take_profit: Optional[float]
    ) -> OrderResult:
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return OrderResult(
                success=False, order_id=None, deal_id=None, price=None,
                volume=None, retcode=-1, comment="POSITION_NOT_FOUND",
            )
        pos = positions[0]
        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": ticket,
            "sl": stop_loss if stop_loss is not None else pos.sl,
            "tp": take_profit if take_profit is not None else pos.tp,
        }
        result = mt5.order_send(req)
        success = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(
            success=success,
            order_id=getattr(result, "order", None),
            deal_id=None,
            price=None,
            volume=pos.volume,
            retcode=getattr(result, "retcode", None),
            comment=getattr(result, "comment", "MODIFY_FAILED"),
        )
