"""Simple moving-average calculations."""

from __future__ import annotations

import pandas as pd

from ._validation import as_float_series, validate_period


def sma(close: pd.Series, period: int = 20) -> pd.Series:
    """Return the trailing simple moving average of closing prices.

    Only the current and preceding observations are included.  The first
    ``period - 1`` values are undefined rather than being calculated from a
    partial window.
    """

    validate_period(period)
    values = as_float_series(close, "close")
    result = values.rolling(window=period, min_periods=period).mean()
    return result.rename("sma")
