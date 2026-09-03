"""Average Directional Index calculations."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ._validation import as_float_series, validate_aligned, validate_period


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Return Wilder's Average Directional Index.

    True Range and directional movements are smoothed with Wilder's recurrence.
    The first bar has no preceding observation and is excluded from True Range
    and directional-movement smoothing.  This follows TA-Lib's standard ADX
    lookback convention: the first ADX appears at zero-based position
    ``2 * period - 1`` (position 27 for period 14).  A bar sequence with no
    directional movement is assigned DX/ADX zero after that warm-up.
    """

    validate_period(period)
    if period < 2:
        raise ValueError("ADX period must be at least 2 for TA-Lib compatibility")
    high_values = as_float_series(high, "high")
    low_values = as_float_series(low, "low")
    close_values = as_float_series(close, "close")
    validate_aligned(high_values, low_values, close_values)

    highs = high_values.to_numpy()
    lows = low_values.to_numpy()
    closes = close_values.to_numpy()
    result = np.full(len(highs), np.nan, dtype=float)
    first_adx_position = 2 * period - 1
    if len(highs) <= first_adx_position:
        return pd.Series(result, index=high_values.index, name="adx")

    smoothed_plus_dm = 0.0
    smoothed_minus_dm = 0.0
    smoothed_tr = 0.0

    # TA-Lib initializes the period aggregate with period-1 observations,
    # because index 0 has no preceding bar. Index ``period`` is then the first
    # observation incorporated through Wilder's recurrence.
    for position in range(1, period):
        plus_dm, minus_dm, true_range = _directional_values(
            highs, lows, closes, position
        )
        smoothed_plus_dm += plus_dm
        smoothed_minus_dm += minus_dm
        smoothed_tr += true_range

    initial_dx_sum = 0.0
    for position in range(period, first_adx_position + 1):
        plus_dm, minus_dm, true_range = _directional_values(
            highs, lows, closes, position
        )
        smoothed_plus_dm = smoothed_plus_dm - smoothed_plus_dm / period + plus_dm
        smoothed_minus_dm = smoothed_minus_dm - smoothed_minus_dm / period + minus_dm
        smoothed_tr = smoothed_tr - smoothed_tr / period + true_range
        initial_dx_sum += _dx(smoothed_plus_dm, smoothed_minus_dm, smoothed_tr)

    previous_adx = initial_dx_sum / period
    result[first_adx_position] = previous_adx

    for position in range(first_adx_position + 1, len(highs)):
        plus_dm, minus_dm, true_range = _directional_values(
            highs, lows, closes, position
        )
        smoothed_plus_dm = smoothed_plus_dm - smoothed_plus_dm / period + plus_dm
        smoothed_minus_dm = smoothed_minus_dm - smoothed_minus_dm / period + minus_dm
        smoothed_tr = smoothed_tr - smoothed_tr / period + true_range
        directional_sum = smoothed_plus_dm + smoothed_minus_dm
        if smoothed_tr > 0.0 and not np.isclose(directional_sum, 0.0):
            current_dx = (
                100.0 * abs(smoothed_plus_dm - smoothed_minus_dm) / directional_sum
            )
            previous_adx = (previous_adx * (period - 1) + current_dx) / period
        result[position] = previous_adx

    return pd.Series(result, index=high_values.index, name="adx")


def _directional_values(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, position: int
) -> tuple[float, float, float]:
    up_move = float(highs[position] - highs[position - 1])
    down_move = float(lows[position - 1] - lows[position])
    plus_dm = up_move if up_move > 0.0 and up_move > down_move else 0.0
    minus_dm = down_move if down_move > 0.0 and down_move > up_move else 0.0
    true_range = max(
        float(highs[position] - lows[position]),
        abs(float(highs[position] - closes[position - 1])),
        abs(float(lows[position] - closes[position - 1])),
    )
    return plus_dm, minus_dm, true_range


def _dx(plus_dm: float, minus_dm: float, true_range: float) -> float:
    directional_sum = plus_dm + minus_dm
    if true_range <= 0.0 or np.isclose(directional_sum, 0.0):
        return 0.0
    return 100.0 * abs(plus_dm - minus_dm) / directional_sum
