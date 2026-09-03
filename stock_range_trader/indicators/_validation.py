"""Shared input checks for indicator functions."""

from __future__ import annotations

import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype


def validate_period(period: int) -> None:
    """Require a strictly positive integer rolling period."""

    if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        raise ValueError("period must be a positive integer")


def as_float_series(values: pd.Series, name: str) -> pd.Series:
    """Validate a numeric Series and return a floating-point copy."""

    if not isinstance(values, pd.Series):
        raise TypeError(f"{name} must be a pandas Series")
    if is_bool_dtype(values.dtype) or not is_numeric_dtype(values.dtype):
        raise TypeError(f"{name} must contain numeric values")
    return values.astype(float)


def validate_aligned(*series: pd.Series) -> None:
    """Require all price Series to use exactly the same index."""

    if not series:
        return
    expected = series[0].index
    if any(not values.index.equals(expected) for values in series[1:]):
        raise ValueError("indicator inputs must have identical indexes")
