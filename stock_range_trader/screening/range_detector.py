"""Causal feature calculations for range-market detection."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from data.validation import validate_ohlcv
from indicators import adx, atr, sma


@dataclass(frozen=True, slots=True)
class RangeDetector:
    """Calculate indicators and trailing range-market features."""

    sma_period: int = 20
    atr_period: int = 20
    atr_method: str = "wilder"
    adx_period: int = 14
    range_window: int = 60
    slope_lookback: int = 20

    def __post_init__(self) -> None:
        """Validate detector parameters when the object is constructed."""

        for name in (
            "sma_period",
            "atr_period",
            "adx_period",
            "range_window",
            "slope_lookback",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.atr_method not in {"wilder", "simple"}:
            raise ValueError("atr_method must be either 'wilder' or 'simple'")

    def transform(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of ``frame`` enriched with range-detection features.

        Every feature at row *t* uses only rows at or before *t*.  The source
        frame is validated and never mutated.
        """

        validate_ohlcv(frame)
        result = frame.copy()
        result["sma"] = sma(result["close"], self.sma_period)
        result["atr"] = atr(
            result["high"],
            result["low"],
            result["close"],
            period=self.atr_period,
            method=self.atr_method,  # type: ignore[arg-type]
        )
        result["adx"] = adx(
            result["high"], result["low"], result["close"], self.adx_period
        )
        result["normalized_slope"] = normalized_rolling_slope(
            result["sma"], self.slope_lookback
        )
        result["rolling_high"] = result["high"].rolling(
            window=self.range_window, min_periods=self.range_window
        ).max()
        result["rolling_low"] = result["low"].rolling(
            window=self.range_window, min_periods=self.range_window
        ).min()
        result["rolling_mid"] = (
            result["rolling_high"] + result["rolling_low"]
        ) / 2.0
        result["range_width"] = (
            result["rolling_high"] - result["rolling_low"]
        ) / result["rolling_mid"].replace(0.0, np.nan)
        result["ma_crossings"] = mean_crossing_count(
            result["close"], result["sma"], self.range_window
        )
        return result


def normalized_rolling_slope(values: pd.Series, lookback: int) -> pd.Series:
    """Return trailing least-squares slope divided by the current value."""

    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback <= 0:
        raise ValueError("lookback must be a positive integer")
    if not isinstance(values, pd.Series):
        raise TypeError("values must be a pandas Series")

    x_values = np.arange(lookback, dtype=float)
    centered_x = x_values - x_values.mean()
    denominator = float(np.dot(centered_x, centered_x))

    if denominator == 0.0:
        slopes = values.astype(float).rolling(window=lookback, min_periods=lookback).apply(
            lambda _: 0.0, raw=True
        )
    else:
        slopes = values.astype(float).rolling(
            window=lookback, min_periods=lookback
        ).apply(
            lambda window: float(
                np.dot(centered_x, window - window.mean()) / denominator
            ),
            raw=True,
        )

    current = values.astype(float).replace(0.0, np.nan)
    return (slopes / current).rename("normalized_slope")


def mean_crossing_count(
    close: pd.Series, center: pd.Series, window: int
) -> pd.Series:
    """Count close/center crossings in each trailing window.

    Exact equality retains the last non-zero side, preventing a price that
    merely touches the center from being counted twice.  Forward filling is
    causal; leading undefined center values remain undefined.
    """

    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("window must be a positive integer")
    if not isinstance(close, pd.Series) or not isinstance(center, pd.Series):
        raise TypeError("close and center must be pandas Series")
    if not close.index.equals(center.index):
        raise ValueError("close and center must have identical indexes")

    side = np.sign(close.astype(float) - center.astype(float))
    side = side.mask(side == 0.0).ffill()
    crossings = (
        side.ne(side.shift(1))
        & side.notna()
        & side.shift(1).notna()
    ).astype(float)
    crossings = crossings.where(side.notna())
    return crossings.rolling(window=window, min_periods=window).sum().rename(
        "ma_crossings"
    )
