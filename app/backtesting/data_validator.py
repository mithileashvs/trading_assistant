"""
Historical Data Validation (Phase 10).

Detects and reports historical data anomalies prior to backtesting:
- Chronological sorting and duplicate timestamps
- Missing or NaN values
- Invalid OHLC geometric relationships (e.g. High < Low, High < Open/Close, Low > Open/Close)
- Non-positive prices (<= 0)
- Negative spreads (< 0)

Never silently alters or repairs historical data in a way that would
produce untracked changes in backtest outcomes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd


class InvalidHistoricalDataError(ValueError):
    """Raised when historical data fails validation in strict mode."""
    pass


@dataclass
class DataQualityReport:
    is_valid: bool = True
    total_bars: int = 0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    unsorted_timestamps: int = 0
    duplicate_timestamps: int = 0
    nan_bars: int = 0
    invalid_ohlc_bars: int = 0
    non_positive_bars: int = 0
    invalid_spread_bars: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "is_valid": self.is_valid,
            "total_bars": self.total_bars,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "unsorted_timestamps": self.unsorted_timestamps,
            "duplicate_timestamps": self.duplicate_timestamps,
            "nan_bars": self.nan_bars,
            "invalid_ohlc_bars": self.invalid_ohlc_bars,
            "non_positive_bars": self.non_positive_bars,
            "invalid_spread_bars": self.invalid_spread_bars,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def validate_historical_data(
    df: pd.DataFrame,
    strict: bool = False,
    timeframe: str = "M15",
) -> DataQualityReport:
    """Validates historical market data DataFrame.

    Parameters:
        df: DataFrame with DatetimeIndex and at least open, high, low, close columns.
        strict: If True, raises InvalidHistoricalDataError upon any fatal error.
        timeframe: Label for contextual logging.

    Returns:
        DataQualityReport containing validation statistics and errors/warnings.
    """
    report = DataQualityReport()

    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        report.is_valid = False
        report.errors.append("Historical data is empty or not a DataFrame.")
        if strict:
            raise InvalidHistoricalDataError("; ".join(report.errors))
        return report

    report.total_bars = len(df)

    # 1. Index validation
    if not isinstance(df.index, pd.DatetimeIndex):
        report.is_valid = False
        report.errors.append("Index must be a pandas DatetimeIndex.")
    else:
        report.start_time = df.index[0].to_pydatetime() if len(df) else None
        report.end_time = df.index[-1].to_pydatetime() if len(df) else None

        # Check sorting
        if not df.index.is_monotonic_increasing:
            # Count out-of-order steps
            diffs = df.index.to_series().diff()
            unsorted_count = int((diffs < pd.Timedelta(0)).sum())
            report.unsorted_timestamps = max(unsorted_count, 1)
            report.is_valid = False
            report.errors.append(f"Timestamps are not chronologically sorted ({report.unsorted_timestamps} inversions).")

        # Check duplicates
        dups = int(df.index.duplicated().sum())
        if dups > 0:
            report.duplicate_timestamps = dups
            report.is_valid = False
            report.errors.append(f"Found {dups} duplicate timestamps in index.")

    # 2. Required columns
    required_cols = {"open", "high", "low", "close"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        report.is_valid = False
        report.errors.append(f"Missing required price columns: {sorted(missing_cols)}")
        if strict:
            raise InvalidHistoricalDataError("; ".join(report.errors))
        return report

    # 3. NaN values
    price_cols = ["open", "high", "low", "close"]
    nan_mask = df[price_cols].isna().any(axis=1)
    nan_count = int(nan_mask.sum())
    if nan_count > 0:
        report.nan_bars = nan_count
        report.is_valid = False
        report.errors.append(f"Found {nan_count} bars with NaN in OHLC prices.")

    # 4. Non-positive prices
    clean_df = df[~nan_mask] if nan_count > 0 else df
    non_pos_mask = (clean_df["open"] <= 0) | (clean_df["high"] <= 0) | (clean_df["low"] <= 0) | (clean_df["close"] <= 0)
    non_pos_count = int(non_pos_mask.sum())
    if non_pos_count > 0:
        report.non_positive_bars = non_pos_count
        report.is_valid = False
        report.errors.append(f"Found {non_pos_count} bars with non-positive prices (<= 0).")

    # 5. OHLC geometric relationships
    # high must be >= open, high >= close, high >= low
    # low must be <= open, low <= close, low <= high
    invalid_geom_mask = (
        (clean_df["high"] < clean_df["low"]) |
        (clean_df["high"] < clean_df["open"]) |
        (clean_df["high"] < clean_df["close"]) |
        (clean_df["low"] > clean_df["open"]) |
        (clean_df["low"] > clean_df["close"])
    )
    invalid_geom_count = int(invalid_geom_mask.sum())
    if invalid_geom_count > 0:
        report.invalid_ohlc_bars = invalid_geom_count
        report.is_valid = False
        report.errors.append(f"Found {invalid_geom_count} bars with invalid OHLC geometry (e.g. High < Low or Low > Open/Close).")

    # 6. Negative spread check
    if "spread" in df.columns:
        neg_spread_count = int((df["spread"] < 0).sum())
        if neg_spread_count > 0:
            report.invalid_spread_bars = neg_spread_count
            report.is_valid = False
            report.errors.append(f"Found {neg_spread_count} bars with negative spread (< 0).")

    # Final decision
    if report.errors:
        report.is_valid = False
        if strict:
            raise InvalidHistoricalDataError("; ".join(report.errors))

    return report
