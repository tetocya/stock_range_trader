"""True Range and Average True Range calculations."""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from ._validation import as_float_series, validate_aligned, validate_period

AtrMethod = Literal["wilder", "simple"]


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Return Wilder's True Range for each daily bar.

    For the first row there is no previous close, so its True Range is the
    current high-low range.  All later rows use the greatest of high-low,
    high-to-previous-close, and low-to-previous-close.
    """

    high_values = as_float_series(high, "high")
    low_values = as_float_series(low, "low")
    close_values = as_float_series(close, "close")
    validate_aligned(high_values, low_values, close_values)

    previous_close = close_values.shift(1)
    components = pd.concat(
        (
            high_values - low_values,
            (high_values - previous_close).abs(),
            (low_values - previous_close).abs(),
        ),
        axis=1,
    )
    return components.max(axis=1, skipna=True).rename("true_range")


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
    method: AtrMethod = "wilder",
) -> pd.Series:
    """Return trailing Average True Range.

    ``wilder`` initializes ATR with the simple mean of the first ``period``
    True Range values, then applies Wilder's recursive smoothing.  ``simple``
    uses a trailing simple mean throughout.  Both methods are causal.
    """

    validate_period(period)
    ranges = true_range(high, low, close)

    if method == "wilder":
        result = wilder_average(ranges, period)
    elif method == "simple":
        result = ranges.rolling(window=period, min_periods=period).mean()
    else:
        raise ValueError("method must be either 'wilder' or 'simple'")

    return result.rename("atr")


def wilder_average(values: pd.Series, period: int) -> pd.Series:
    """Smooth a Series using Wilder's explicitly initialized recurrence.

    Leading missing observations are skipped.  Once the first ``period``
    consecutive observations are available, their simple mean is used as the
    initial value.  This behavior is also used to smooth DX into ADX.
    """

    validate_period(period)
    numeric = as_float_series(values, "values")
    result = pd.Series(np.nan, index=numeric.index, dtype=float)
    if numeric.empty:
        return result

    valid_positions = np.flatnonzero(numeric.notna().to_numpy())
    if len(valid_positions) == 0:
        return result

    start = int(valid_positions[0])
    initial_window = numeric.iloc[start : start + period]
    if len(initial_window) < period or initial_window.isna().any():
        return result

    initial_position = start + period - 1
    result.iloc[initial_position] = float(initial_window.mean())

    for position in range(initial_position + 1, len(numeric)):
        current = numeric.iloc[position]
        previous = result.iloc[position - 1]
        if pd.isna(current) or pd.isna(previous):
            continue
        result.iloc[position] = (
            previous * (period - 1) + float(current)
        ) / period

    return result
