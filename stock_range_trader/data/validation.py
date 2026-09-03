"""Strict validation for daily OHLCV data.

Validation never sorts, fills, clips, or otherwise repairs the input.  A caller
must make an explicit decision about every data-quality problem.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_datetime64_any_dtype, is_numeric_dtype

REQUIRED_COLUMNS: tuple[str, ...] = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
NUMERIC_COLUMNS: tuple[str, ...] = (*PRICE_COLUMNS, "volume")


class DataValidationError(ValueError):
    """Raised when an OHLCV table violates the required data contract."""


def validate_required_columns(columns: Iterable[object]) -> None:
    """Raise when one or more required, case-sensitive columns are absent."""

    available = set(columns)
    missing = [column for column in REQUIRED_COLUMNS if column not in available]
    if missing:
        raise DataValidationError(
            "Missing required OHLCV columns: " + ", ".join(missing)
        )


def validate_ohlcv(frame: pd.DataFrame) -> None:
    """Validate a daily OHLCV DataFrame without modifying it.

    Args:
        frame: Table containing at least the required OHLCV columns.

    Raises:
        TypeError: If ``frame`` is not a pandas DataFrame.
        DataValidationError: If its schema, types, ordering, or values are
            invalid.
    """

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("OHLCV data must be provided as a pandas DataFrame")

    validate_required_columns(frame.columns)
    if frame.empty:
        raise DataValidationError("OHLCV data must contain at least one row")

    required = frame.loc[:, REQUIRED_COLUMNS]
    missing_mask = required.isna()
    if missing_mask.to_numpy().any():
        details = _format_bad_locations(missing_mask)
        raise DataValidationError(f"Missing OHLCV values detected at {details}")

    if not is_datetime64_any_dtype(frame["date"].dtype):
        raise DataValidationError("date must have a pandas datetime dtype")
    if frame["date"].duplicated(keep=False).any():
        duplicates = frame.loc[
            frame["date"].duplicated(keep=False), "date"
        ].dt.strftime("%Y-%m-%d")
        raise DataValidationError(
            "Duplicate dates detected: " + ", ".join(duplicates.unique()[:5])
        )
    if not frame["date"].is_monotonic_increasing:
        raise DataValidationError("date must be in ascending order")

    for column in NUMERIC_COLUMNS:
        if is_bool_dtype(frame[column].dtype) or not is_numeric_dtype(
            frame[column].dtype
        ):
            raise DataValidationError(f"{column} must be numeric")
        finite_mask = np.isfinite(frame[column].to_numpy(dtype=float))
        if not finite_mask.all():
            rows = frame.index[~finite_mask].tolist()[:5]
            raise DataValidationError(
                f"{column} contains non-finite values at rows {rows}"
            )

    non_positive_prices = frame.loc[:, PRICE_COLUMNS].le(0)
    if non_positive_prices.to_numpy().any():
        details = _format_bad_locations(non_positive_prices)
        raise DataValidationError(f"Prices must be greater than zero at {details}")

    negative_volume = frame["volume"].lt(0)
    if negative_volume.any():
        rows = frame.index[negative_volume].tolist()[:5]
        raise DataValidationError(f"volume must be non-negative at rows {rows}")

    _validate_price_relationship(frame, frame["high"] < frame["low"], "high >= low")
    _validate_price_relationship(
        frame, frame["high"] < frame["open"], "high >= open"
    )
    _validate_price_relationship(
        frame, frame["high"] < frame["close"], "high >= close"
    )
    _validate_price_relationship(frame, frame["low"] > frame["open"], "low <= open")
    _validate_price_relationship(
        frame, frame["low"] > frame["close"], "low <= close"
    )


def _validate_price_relationship(
    frame: pd.DataFrame, invalid: pd.Series, requirement: str
) -> None:
    """Report rows that violate one OHLC relationship."""

    if invalid.any():
        rows = frame.index[invalid].tolist()[:5]
        raise DataValidationError(
            f"Invalid OHLC relationship ({requirement}) at rows {rows}"
        )


def _format_bad_locations(mask: pd.DataFrame) -> str:
    """Return a compact list of row/column locations for an error message."""

    locations = [
        f"row {row!r}, column {column!r}"
        for row, column in zip(*np.where(mask.to_numpy()))
    ]
    return "; ".join(locations[:5])
