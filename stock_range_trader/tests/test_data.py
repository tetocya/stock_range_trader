"""Tests for strict CSV loading and OHLCV validation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pandas.api.types import is_datetime64_any_dtype

from data.loader import load_ohlcv_csv
from data.validation import DataValidationError, validate_ohlcv


@pytest.fixture
def valid_frame() -> pd.DataFrame:
    """Return a minimal valid daily OHLCV table."""

    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-01-06", "2025-01-07"]),
            "open": [1000.0, 1015.0],
            "high": [1020.0, 1030.0],
            "low": [990.0, 1005.0],
            "close": [1010.0, 1025.0],
            "volume": [150_000, 180_000],
        }
    )


def test_load_valid_csv_converts_types(tmp_path: Path) -> None:
    csv_path = tmp_path / "valid.csv"
    csv_path.write_text(
        "date,open,high,low,close,volume\n"
        "2025-01-06,1000,1020,990,1010,150000\n"
        "2025-01-07,1015,1030,1005,1025,180000\n",
        encoding="utf-8",
    )

    result = load_ohlcv_csv(csv_path)

    assert is_datetime64_any_dtype(result["date"].dtype)
    assert result["close"].tolist() == [1010, 1025]


def test_loader_does_not_silently_sort_dates(tmp_path: Path) -> None:
    csv_path = tmp_path / "descending.csv"
    csv_path.write_text(
        "date,open,high,low,close,volume\n"
        "2025-01-07,1015,1030,1005,1025,180000\n"
        "2025-01-06,1000,1020,990,1010,150000\n",
        encoding="utf-8",
    )

    with pytest.raises(DataValidationError, match="ascending"):
        load_ohlcv_csv(csv_path)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("open", 0, "greater than zero"),
        ("high", -1, "greater than zero"),
        ("low", np.inf, "non-finite"),
        ("close", np.nan, "Missing"),
        ("volume", -1, "non-negative"),
    ],
)
def test_rejects_invalid_values(
    valid_frame: pd.DataFrame, column: str, value: float, message: str
) -> None:
    valid_frame.loc[0, column] = value

    with pytest.raises(DataValidationError, match=message):
        validate_ohlcv(valid_frame)


@pytest.mark.parametrize(
    ("updates", "requirement"),
    [
        ({"high": 980}, "high >= low"),
        ({"high": 995}, "high >= open"),
        ({"high": 1005}, "high >= close"),
        ({"low": 1005}, "low <= open"),
        ({"open": 1020, "close": 1010, "low": 1015}, "low <= close"),
    ],
)
def test_rejects_invalid_ohlc_relationships(
    valid_frame: pd.DataFrame, updates: dict[str, int], requirement: str
) -> None:
    for column, value in updates.items():
        valid_frame.loc[0, column] = value

    with pytest.raises(DataValidationError, match=requirement):
        validate_ohlcv(valid_frame)


def test_rejects_duplicate_dates(valid_frame: pd.DataFrame) -> None:
    valid_frame.loc[1, "date"] = valid_frame.loc[0, "date"]

    with pytest.raises(DataValidationError, match="Duplicate dates"):
        validate_ohlcv(valid_frame)


def test_rejects_missing_column(valid_frame: pd.DataFrame) -> None:
    with pytest.raises(DataValidationError, match="volume"):
        validate_ohlcv(valid_frame.drop(columns="volume"))


def test_rejects_non_numeric_csv_value(tmp_path: Path) -> None:
    csv_path = tmp_path / "bad-number.csv"
    csv_path.write_text(
        "date,open,high,low,close,volume\n"
        "2025-01-06,not-a-price,1020,990,1010,150000\n",
        encoding="utf-8",
    )

    with pytest.raises(DataValidationError, match="open.*non-numeric"):
        load_ohlcv_csv(csv_path)


def test_rejects_invalid_date(tmp_path: Path) -> None:
    csv_path = tmp_path / "bad-date.csv"
    csv_path.write_text(
        "date,open,high,low,close,volume\nnot-a-date,1000,1020,990,1010,150000\n",
        encoding="utf-8",
    )

    with pytest.raises(DataValidationError, match="date.*invalid"):
        load_ohlcv_csv(csv_path)


def test_equal_ohlc_prices_and_zero_volume_are_valid() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-01-06"]),
            "open": [1_000.0],
            "high": [1_000.0],
            "low": [1_000.0],
            "close": [1_000.0],
            "volume": [0],
        }
    )

    validate_ohlcv(frame)


def test_empty_ohlcv_data_is_rejected() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.Series(dtype="datetime64[ns]"),
            "open": pd.Series(dtype=float),
            "high": pd.Series(dtype=float),
            "low": pd.Series(dtype=float),
            "close": pd.Series(dtype=float),
            "volume": pd.Series(dtype=float),
        }
    )

    with pytest.raises(DataValidationError, match="at least one row"):
        validate_ohlcv(frame)
