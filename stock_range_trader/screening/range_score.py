"""Composite, interpretable Range Score calculations."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class RangeScoreWeights:
    """Weights for the four Range Score components."""

    trend: float = 0.30
    mean_reversion: float = 0.30
    stability: float = 0.20
    liquidity: float = 0.20

    def __post_init__(self) -> None:
        values = (self.trend, self.mean_reversion, self.stability, self.liquidity)
        if any(not np.isfinite(value) or value < 0.0 for value in values):
            raise ValueError("Range Score weights must be finite and non-negative")
        if not np.isclose(sum(values), 1.0, rtol=0.0, atol=1e-12):
            raise ValueError("Range Score weights must sum to 1.0")


@dataclass(frozen=True, slots=True)
class RangeScorer:
    """Convert causal range features into component scores from 0 to 100."""

    weights: RangeScoreWeights = field(default_factory=RangeScoreWeights)
    normalized_slope_limit: float = 0.001
    adx_score_limit: float = 50.0
    trend_slope_weight: float = 0.50
    trend_adx_weight: float = 0.50
    crossing_target: int = 6
    stability_window: int = 20
    stability_cv_limit: float = 0.50
    liquidity_window: int = 60
    average_volume_target: float = 100_000.0
    range_threshold: float = 70.0
    adx_max: float = 25.0

    def __post_init__(self) -> None:
        positive_floats = {
            "normalized_slope_limit": self.normalized_slope_limit,
            "adx_score_limit": self.adx_score_limit,
            "stability_cv_limit": self.stability_cv_limit,
            "average_volume_target": self.average_volume_target,
        }
        for name, value in positive_floats.items():
            if isinstance(value, bool) or not np.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and greater than zero")

        positive_integers = {
            "crossing_target": self.crossing_target,
            "stability_window": self.stability_window,
            "liquidity_window": self.liquidity_window,
        }
        for name, value in positive_integers.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        trend_weights = (self.trend_slope_weight, self.trend_adx_weight)
        if any(not np.isfinite(value) or value < 0.0 for value in trend_weights):
            raise ValueError("trend component weights must be finite and non-negative")
        if not np.isclose(sum(trend_weights), 1.0, rtol=0.0, atol=1e-12):
            raise ValueError("trend component weights must sum to 1.0")
        if not 0.0 <= self.range_threshold <= 100.0:
            raise ValueError("range_threshold must be between 0 and 100")
        if not np.isfinite(self.adx_max) or self.adx_max < 0.0:
            raise ValueError("adx_max must be finite and non-negative")

    def transform(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Return range component scores, total score, and range flag.

        The formulas use linear capped scales so every component remains easy
        to audit.  Missing warm-up features propagate to ``range_score`` and
        produce ``False`` for ``is_range_market``.
        """

        required = {
            "normalized_slope",
            "adx",
            "ma_crossings",
            "atr",
            "sma",
            "range_width",
            "volume",
        }
        missing = sorted(required.difference(frame.columns))
        if missing:
            raise ValueError("Missing Range Score features: " + ", ".join(missing))

        result = frame.copy()

        # Near-zero normalized slope and low ADX are independent evidence that
        # persistent directional movement is weak.
        result["slope_score"] = _inverse_linear_score(
            result["normalized_slope"].abs(), self.normalized_slope_limit
        )
        result["adx_score"] = _inverse_linear_score(
            result["adx"], self.adx_score_limit
        )
        result["trend_score"] = (
            self.trend_slope_weight * result["slope_score"]
            + self.trend_adx_weight * result["adx_score"]
        )

        # Repeated center crossings are direct, bounded evidence of mean
        # reversion; additional crossings beyond the target do not inflate it.
        result["mean_reversion_score"] = (
            100.0 * result["ma_crossings"] / self.crossing_target
        ).clip(lower=0.0, upper=100.0)

        # Coefficients of variation compare changes proportionally across
        # different price levels.  Stable ATR% and range width each contribute
        # half of the stability component.
        result["atr_pct"] = result["atr"] / result["sma"].replace(0.0, np.nan)
        result["atr_cv"] = _rolling_coefficient_of_variation(
            result["atr_pct"], self.stability_window
        )
        result["range_width_cv"] = _rolling_coefficient_of_variation(
            result["range_width"], self.stability_window
        )
        atr_stability = _inverse_linear_score(
            result["atr_cv"], self.stability_cv_limit
        )
        width_stability = _inverse_linear_score(
            result["range_width_cv"], self.stability_cv_limit
        )
        result["stability_score"] = (atr_stability + width_stability) / 2.0

        # Average volume receives full credit at the configured target.  This
        # is a transparent Phase 1 proxy, not a market-impact model.
        result["average_volume"] = result["volume"].rolling(
            window=self.liquidity_window, min_periods=self.liquidity_window
        ).mean()
        result["liquidity_score"] = (
            100.0 * result["average_volume"] / self.average_volume_target
        ).clip(lower=0.0, upper=100.0)

        result["range_score"] = (
            self.weights.trend * result["trend_score"]
            + self.weights.mean_reversion * result["mean_reversion_score"]
            + self.weights.stability * result["stability_score"]
            + self.weights.liquidity * result["liquidity_score"]
        ).clip(lower=0.0, upper=100.0)
        result["is_range_market"] = (
            (result["range_score"] >= self.range_threshold)
            & (result["adx"] <= self.adx_max)
        )
        return result


def _inverse_linear_score(values: pd.Series, limit: float) -> pd.Series:
    """Map zero to 100 and the configured limit (or greater) to zero."""

    return (100.0 * (1.0 - values / limit)).clip(lower=0.0, upper=100.0)


def _rolling_coefficient_of_variation(
    values: pd.Series, window: int
) -> pd.Series:
    """Return trailing population standard deviation divided by absolute mean."""

    rolling = values.rolling(window=window, min_periods=window)
    mean = rolling.mean().abs()
    standard_deviation = rolling.std(ddof=0)
    coefficient = standard_deviation / mean.replace(0.0, np.nan)
    zero_mean = pd.Series(
        np.where(standard_deviation == 0.0, 0.0, np.inf),
        index=values.index,
        dtype=float,
    )
    return coefficient.where(mean != 0.0, zero_mean)
