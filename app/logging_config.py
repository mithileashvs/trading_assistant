"""
Structured JSON logging for the trading assistant (section 34).

Every major pipeline event should be logged using `log_event(...)` with
one of the EVENT_TYPES below, so log lines are machine-parseable and
never accidentally contain secrets.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Mapping

EVENT_TYPES = {
    "MARKET_DATA",
    "REGIME_CHANGE",
    "SIGNAL_GENERATED",
    "SIGNAL_REJECTED",
    "RISK_CHECK",
    "RISK_REJECT",
    "SAFETY_REJECT",
    "ORDER_SUBMITTED",
    "ORDER_FILLED",
    "ORDER_REJECTED",
    "POSITION_UPDATED",
    "POSITION_CLOSED",
    "KILL_SWITCH",
    "SYSTEM_ERROR",
    "MT5_CONNECT",
    "MT5_DISCONNECT",
    "SYMBOL_DISCOVERY",
    "STARTUP_SAFETY",
}

_SECRET_KEYS = {"password", "mt5_password", "api_key", "token", "secret"}


def _scrub(payload: Mapping[str, Any]) -> dict:
    clean = {}
    for k, v in payload.items():
        if k.lower() in _SECRET_KEYS:
            clean[k] = "***REDACTED***"
        else:
            clean[k] = v
    return clean


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event_type = getattr(record, "event_type", None)
        if event_type:
            payload["event_type"] = event_type
        extra_data = getattr(record, "data", None)
        if extra_data:
            payload["data"] = _scrub(extra_data)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(log_level: str = "INFO", log_dir: str = "./data/logs") -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("xau_assistant")
    logger.setLevel(log_level.upper())
    logger.handlers.clear()

    formatter = JsonFormatter()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(os.path.join(log_dir, "app.log"))
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def log_event(
    logger: logging.Logger,
    event_type: str,
    message: str,
    data: Mapping[str, Any] | None = None,
    level: int = logging.INFO,
) -> None:
    if event_type not in EVENT_TYPES:
        raise ValueError(
            f"Unknown event_type '{event_type}'. Add it to EVENT_TYPES if this "
            "is a genuinely new pipeline event."
        )
    logger.log(level, message, extra={"event_type": event_type, "data": dict(data or {})})
