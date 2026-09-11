"""
Position Sizing (section 13).

Lot size is ALWAYS derived from account equity, risk %, stop distance,
and the broker's actual symbol specification — never a hard-coded
value like "0.1 lot". This is the only module that turns a risk
percentage into a tradable volume.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.mt5.interface import SymbolSpec


@dataclass
class PositionSizeResult:
    lots: float
    monetary_risk: float
    loss_per_lot: float
    requested_lots: float
    clamped: bool
    warnings: list[str]

    @property
    def is_tradable(self) -> bool:
        """False if the sizing collapsed to zero (e.g. risk amount too
        small to afford even the broker's minimum lot at this stop
        distance would exceed the intended risk)."""
        return self.lots > 0


class PositionSizingError(ValueError):
    pass


def calculate_position_size(
    equity: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    symbol_spec: SymbolSpec,
) -> PositionSizeResult:
    """
    lots = (equity * risk_pct / 100) / loss_per_lot
    loss_per_lot = (|entry - stop_loss| / tick_size) * tick_value

    Result is clamped to [volume_min, volume_max] and rounded DOWN to
    the nearest volume_step — rounding up would silently risk more
    than the configured percentage.
    """
    if equity <= 0:
        raise PositionSizingError("Equity must be positive.")
    if entry == stop_loss:
        raise PositionSizingError("Entry and stop_loss cannot be equal (zero stop distance).")
    if symbol_spec.tick_size <= 0 or symbol_spec.volume_step <= 0:
        raise PositionSizingError("Symbol spec has invalid tick_size/volume_step.")

    price_distance = abs(entry - stop_loss)
    ticks = price_distance / symbol_spec.tick_size
    loss_per_lot = ticks * symbol_spec.tick_value
    if loss_per_lot <= 0:
        raise PositionSizingError("Computed loss_per_lot is non-positive; check symbol spec.")

    monetary_risk = equity * (risk_pct / 100.0)
    raw_lots = monetary_risk / loss_per_lot

    # Round DOWN to the nearest volume_step (never round up — that
    # would silently exceed the configured risk).
    steps = math.floor(raw_lots / symbol_spec.volume_step)
    lots = steps * symbol_spec.volume_step
    lots = round(lots, 8)  # clean up float artifacts

    warnings: list[str] = []
    clamped = False

    if lots < symbol_spec.volume_min:
        if raw_lots > 0:
            warnings.append(
                f"Risk-based size ({raw_lots:.4f} lots) is below the broker's minimum "
                f"({symbol_spec.volume_min}); no trade can be taken within the configured risk."
            )
        lots = 0.0
        clamped = True
    elif lots > symbol_spec.volume_max:
        warnings.append(
            f"Risk-based size ({raw_lots:.4f} lots) exceeds the broker's maximum "
            f"({symbol_spec.volume_max}); capping at the maximum."
        )
        lots = symbol_spec.volume_max
        clamped = True

    return PositionSizeResult(
        lots=lots,
        monetary_risk=monetary_risk,
        loss_per_lot=loss_per_lot,
        requested_lots=raw_lots,
        clamped=clamped,
        warnings=warnings,
    )
