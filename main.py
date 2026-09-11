"""
Phase 1 entry point.

Verifies: configuration loads, MT5 connection (mock or real), symbol
discovery, account info, and OHLCV retrieval across the H4/H1/M15
timeframes. This is a smoke test you run after setup — it does not
place any trades.
"""
from __future__ import annotations

import os
import sys

from app.config.settings import get_settings
from app.features.engine import InsufficientDataError, compute_features
from app.logging_config import configure_logging, log_event
from app.market_data.engine import MarketDataEngine, SymbolDiscoveryError, StaleMarketDataError
from app.mt5.factory import build_mt5_client
from app.regimes.detector import detect_regime
from app.risk.guards import GuardCheckInput
from app.risk.kill_switch import KillSwitch
from app.risk.validator import TradeValidator
from app.strategies.context import build_context
from app.strategies.selector import StrategySelector


def main() -> int:
    settings = get_settings()
    logger = configure_logging(settings.log_level, settings.log_dir)

    log_event(
        logger, "SYSTEM_ERROR" if False else "MT5_CONNECT",
        "Starting XAU/USD trading assistant (Phase 1 smoke test)",
        {
            "trading_mode": settings.trading_mode.value,
            "mt5_use_mock": settings.mt5_use_mock,
            "kill_switch": settings.kill_switch,
        },
    )

    try:
        settings.validate_live_safety()
    except RuntimeError as exc:
        log_event(logger, "SYSTEM_ERROR", str(exc), level=40)
        return 1

    if settings.kill_switch:
        log_event(logger, "KILL_SWITCH", "Kill switch is active. Halting startup.", level=30)
        return 1

    client = build_mt5_client(settings)

    try:
        client.connect()
        log_event(logger, "MT5_CONNECT", "Connected to MT5 client.", {"mock": settings.mt5_use_mock})
    except Exception as exc:  # noqa: BLE001 - top-level smoke test boundary
        log_event(logger, "SYSTEM_ERROR", f"MT5 connection failed: {exc}", level=40)
        return 1

    engine = MarketDataEngine(client, settings)

    try:
        resolved = engine.resolve_symbol()
        log_event(
            logger, "SYMBOL_DISCOVERY", f"Resolved symbol: {resolved.name}",
            {"symbol": resolved.name, "spec": resolved.spec.__dict__},
        )
    except SymbolDiscoveryError as exc:
        log_event(logger, "SYSTEM_ERROR", str(exc), level=40)
        return 1

    account = client.get_account_info()
    log_event(logger, "MARKET_DATA", "Account info retrieved.", {"account": account.__dict__})

    for tf in (settings.tf_trend.value, settings.tf_structure.value, settings.tf_entry.value):
        try:
            df = engine.get_ohlcv(tf, count=250)
            log_event(
                logger, "MARKET_DATA", f"Fetched {len(df)} {tf} candles.",
                {"timeframe": tf, "last_close": float(df["close"].iloc[-1])},
            )
        except StaleMarketDataError as exc:
            log_event(logger, "SYSTEM_ERROR", f"{tf} data stale: {exc}", level=30)
            continue

        try:
            snapshot = compute_features(df, tf)
            log_event(
                logger, "MARKET_DATA", f"Computed {tf} feature snapshot.",
                {
                    "timeframe": tf,
                    "structure": snapshot["trend"]["structure"],
                    "adx": snapshot["trend"]["adx"],
                    "rsi": snapshot["momentum"]["rsi"],
                    "atr_pct": snapshot["volatility"]["atr_pct"],
                },
            )
            regime_decision = detect_regime(snapshot)
            log_event(
                logger, "REGIME_CHANGE", f"{tf} regime: {regime_decision.regime.value}",
                regime_decision.to_dict(),
            )
        except InsufficientDataError as exc:
            log_event(logger, "SYSTEM_ERROR", f"{tf} feature snapshot skipped: {exc}", level=30)

    tick = engine.get_tick()
    log_event(
        logger, "MARKET_DATA", "Fetched current tick.",
        {"bid": tick.bid, "ask": tick.ask, "spread": tick.spread},
    )

    try:
        context = build_context(engine, bars=250)
        selector = StrategySelector()
        signals = selector.generate_signals(context)
        for sig in signals:
            event = "SIGNAL_GENERATED" if sig.direction.value != "NO_SIGNAL" else "SIGNAL_REJECTED"
            log_event(logger, event, f"{sig.strategy}: {sig.direction.value}", sig.to_dict())
        best = selector.best_signal(context)
        log_event(
            logger,
            "SIGNAL_GENERATED" if best.direction.value != "NO_SIGNAL" else "SIGNAL_REJECTED",
            f"Best signal: {best.strategy} {best.direction.value}",
            best.to_dict(),
        )

        kill_switch = KillSwitch(os.path.join(settings.log_dir, "..", "kill_switch_state.json"))
        symbol_spec = engine.resolve_symbol().spec
        account = client.get_account_info()
        tick = engine.get_tick()
        spread_points = tick.spread / symbol_spec.tick_size if symbol_spec.tick_size else 0.0

        guard_input = GuardCheckInput(
            equity=account.equity,
            day_start_equity=account.equity,  # no journal history yet in this smoke test
            week_start_equity=account.equity,
            trades_today_count=0,
            open_positions_count=len(client.get_open_positions(symbol_spec.name)),
            current_spread_points=spread_points,
            mt5_connected=client.is_connected(),
            broker_trade_allowed=account.trade_allowed,
            market_data_fresh=True,
            kill_switch_active=settings.kill_switch or kill_switch.is_active(),
        )
        validator = TradeValidator(settings.risk)
        validation = validator.validate(best, context, symbol_spec, account, guard_input)
        log_event(
            logger,
            "RISK_CHECK",
            f"Trade validation: approved={validation.approved}",
            validation.to_dict(),
        )
    except InsufficientDataError as exc:
        log_event(logger, "SYSTEM_ERROR", f"Strategy context skipped: {exc}", level=30)

    client.disconnect()
    log_event(logger, "MT5_DISCONNECT", "Disconnected cleanly. Phase 1 smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
