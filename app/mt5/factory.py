"""
Factory that picks Mock vs Real MT5 client based on Settings.

This is the single switch point in the whole codebase — nothing else
should ever instantiate MockMT5Client or RealMT5Client directly.
"""
from __future__ import annotations

from app.config.settings import Settings
from app.mt5.interface import IMT5Client
from app.mt5.mock_client import MockMT5Client


def build_mt5_client(settings: Settings) -> IMT5Client:
    if settings.mt5_use_mock:
        return MockMT5Client()

    # Imported lazily so that environments without the MetaTrader5
    # package (like this sandbox) can still import the rest of the app.
    from app.mt5.real_client import RealMT5Client

    return RealMT5Client(
        login=settings.mt5_login,
        password=settings.mt5_password,
        server=settings.mt5_server,
        terminal_path=settings.mt5_terminal_path,
    )
