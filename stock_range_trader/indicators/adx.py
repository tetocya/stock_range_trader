"""Average Directional Index calculations."""

from __future__ import annotations

import pandas as pd

from ._validation import as_float_series, validate_aligned, validate_period
from .atr import true_range, wilder_average


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Return Wilder's Average Directional Index.

    True Range and directional movements are smoothed with Wilder's recurrence.
    The first bar contributes zero directional movement.  Consequently, the
    first DX is available at position ``period - 1`` and the first ADX at
    position ``2 * period - 2``.  A bar sequence with no directional movement
    is assigned DX/ADX zero once the warm-up period has completed.
    """

    validate_period(period)
    high_values = as_float_series(high, "high")
    low_values = as_float_series(low, "low")
    close_values = as_float_series(close, "close")
    validate_aligned(high_values, low_values, close_values)

    up_move = high_values.diff()
    down_move = -low_values.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    smoothed_tr = wilder_average(
        true_range(high_values, low_values, close_values), period
    )
    smoothed_plus_dm = wilder_average(plus_dm, period)
    smoothed_minus_dm = wilder_average(minus_dm, period)

    plus_di = (100.0 * smoothed_plus_dm / smoothed_tr).where(
        smoothed_tr != 0, 0.0
    )
    minus_di = (100.0 * smoothed_minus_dm / smoothed_tr).where(
        smoothed_tr != 0, 0.0
    )
    directional_sum = plus_di + minus_di
    dx = (100.0 * (plus_di - minus_di).abs() / directional_sum).where(
        directional_sum != 0, 0.0
    )

    return wilder_average(dx, period).rename("adx")
