"""CSV loading entry points for daily OHLCV data."""

from __future__ import annotations

from pathlib import Path
from typing import TypeAlias

import pandas as pd

from .validation import (
    NUMERIC_COLUMNS,
    DataValidationError,
    validate_ohlcv,
    validate_required_columns,
)

PathLike: TypeAlias = str | Path


class DataLoadError(RuntimeError):
    """Raised when a CSV cannot be read or converted to the OHLCV schema."""


def load_ohlcv_csv(path: PathLike) -> pd.DataFrame:
    """Load and strictly validate a daily OHLCV CSV file.

    The row order is preserved deliberately.  In particular, descending or
    otherwise unordered dates are rejected instead of being silently sorted.

    Args:
        path: UTF-8 CSV containing date, open, high, low, close, and volume.

    Returns:
        A validated DataFrame whose date column has a datetime dtype and whose
        OHLCV value columns are numeric.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        DataLoadError: If the file cannot be parsed as CSV.
        DataValidationError: If its schema or values are invalid.
    """

    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"OHLCV CSV file not found: {source}")

    try:
        frame = pd.read_csv(source, encoding="utf-8-sig")
    except (OSError, UnicodeError, pd.errors.ParserError) as error:
        raise DataLoadError(f"Failed to read OHLCV CSV {source}: {error}") from error

    validate_required_columns(frame.columns)
    frame = frame.copy()

    try:
        frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    except (TypeError, ValueError) as error:
        raise DataValidationError(f"date contains an invalid value: {error}") from error

    for column in NUMERIC_COLUMNS:
        try:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        except (TypeError, ValueError) as error:
            raise DataValidationError(
                f"{column} contains a non-numeric value: {error}"
            ) from error

    validate_ohlcv(frame)
    return frame
