"""Tests for range-market detection and scoring."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from screening import (
    RangeDetector,
    RangeScorer,
    RangeScoreWeights,
    mean_crossing_count,
    normalized_rolling_slope,
)


def _ohlcv(close: np.ndarray) -> pd.DataFrame:
    """Build valid OHLCV bars around an arbitrary close series."""

    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=len(close), freq="D"),
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(len(close), 100_000),
        }
    )


def _score_features(close: list[float], volume: list[float]) -> pd.DataFrame:
    size = len(close)
    return pd.DataFrame(
        {
            "normalized_slope": np.zeros(size),
            "adx": np.zeros(size),
            "ma_crossings": np.zeros(size),
            "atr": np.ones(size),
            "sma": np.full(size, 100.0),
            "range_width": np.full(size, 0.1),
            "close": close,
            "volume": volume,
        }
    )


def test_normalized_rolling_slope_matches_linear_series() -> None:
    values = pd.Series([10.0, 11.0, 12.0, 13.0])

    result = normalized_rolling_slope(values, lookback=3)

    expected = [np.nan, np.nan, 1.0 / 12.0, 1.0 / 13.0]
    np.testing.assert_allclose(result, expected, equal_nan=True)


def test_mean_crossing_count_ignores_center_touches() -> None:
    close = pd.Series([9.0, 11.0, 9.0, 10.0, 11.0, 9.0])
    center = pd.Series([10.0] * len(close))

    result = mean_crossing_count(close, center, window=4)

    np.testing.assert_allclose(
        result, [np.nan, np.nan, np.nan, 2.0, 3.0, 3.0], equal_nan=True
    )


def test_detector_calculates_trailing_range_features_without_mutation() -> None:
    frame = _ohlcv(np.arange(10.0, 18.0))
    original = frame.copy(deep=True)
    detector = RangeDetector(
        sma_period=2,
        atr_period=2,
        adx_period=2,
        range_window=4,
        slope_lookback=3,
    )

    result = detector.transform(frame)

    pd.testing.assert_frame_equal(frame, original)
    assert result.loc[3, "rolling_high"] == pytest.approx(14.0)
    assert result.loc[3, "rolling_low"] == pytest.approx(9.0)
    assert result.loc[3, "rolling_mid"] == pytest.approx(11.5)
    assert result.loc[3, "range_width"] == pytest.approx(5.0 / 11.5)
    assert result.loc[3, "normalized_slope"] == pytest.approx(1.0 / 12.5)


def test_range_score_weighted_formula_matches_known_components() -> None:
    features = pd.DataFrame(
        {
            "normalized_slope": [0.0, 0.0],
            "adx": [0.0, 0.0],
            "ma_crossings": [2.0, 2.0],
            "atr": [10.0, 10.0],
            "sma": [100.0, 100.0],
            "range_width": [0.2, 0.2],
            "close": [1.0, 1.0],
            "volume": [50.0, 50.0],
        }
    )
    scorer = RangeScorer(
        crossing_target=4,
        stability_window=2,
        liquidity_window=2,
        median_trading_value_target=100.0,
        range_threshold=70.0,
    )

    result = scorer.transform(features)

    assert result.loc[1, "trend_score"] == pytest.approx(100.0)
    assert result.loc[1, "mean_reversion_score"] == pytest.approx(50.0)
    assert result.loc[1, "stability_score"] == pytest.approx(100.0)
    assert result.loc[1, "liquidity_score"] == pytest.approx(50.0)
    assert result.loc[1, "range_score"] == pytest.approx(75.0)
    assert bool(result.loc[1, "is_range_market"])


def test_adx_filter_can_reject_otherwise_eligible_range_score() -> None:
    features = pd.DataFrame(
        {
            "normalized_slope": [0.0],
            "adx": [30.0],
            "ma_crossings": [6.0],
            "atr": [10.0],
            "sma": [100.0],
            "range_width": [0.2],
            "close": [1.0],
            "volume": [100.0],
        }
    )
    scorer = RangeScorer(
        stability_window=1,
        liquidity_window=1,
        median_trading_value_target=100.0,
        range_threshold=0.0,
        adx_max=25.0,
    )

    result = scorer.transform(features)

    assert result.loc[0, "range_score"] >= 0.0
    assert not bool(result.loc[0, "is_range_market"])


def test_score_components_are_clipped_to_zero_and_one_hundred() -> None:
    features = pd.DataFrame(
        {
            "normalized_slope": [1.0],
            "adx": [1_000.0],
            "ma_crossings": [1_000.0],
            "atr": [1.0],
            "sma": [100.0],
            "range_width": [0.1],
            "close": [100.0],
            "volume": [1_000_000.0],
        }
    )
    result = RangeScorer(stability_window=1, liquidity_window=1).transform(features)

    for column in (
        "trend_score",
        "mean_reversion_score",
        "stability_score",
        "liquidity_score",
        "range_score",
    ):
        assert 0.0 <= result.loc[0, column] <= 100.0


def test_liquidity_uses_close_times_volume_and_rolling_median() -> None:
    features = _score_features(
        close=[100.0, 100.0, 100.0],
        volume=[1_000.0, 1_000_000.0, 1_000.0],
    )
    scorer = RangeScorer(
        stability_window=1,
        liquidity_window=3,
        median_trading_value_target=200_000.0,
    )

    result = scorer.transform(features)

    assert result.loc[0, "trading_value"] == 100_000.0
    assert result.loc[1, "trading_value"] == 100_000_000.0
    assert result.loc[2, "median_trading_value"] == 100_000.0
    assert result.loc[2, "liquidity_score"] == pytest.approx(50.0)
    assert result["trading_value"].mean() > 30_000_000.0


def test_higher_price_scores_higher_at_the_same_volume() -> None:
    result = RangeScorer(
        stability_window=1,
        liquidity_window=1,
        median_trading_value_target=1_000.0,
    ).transform(_score_features(close=[1.0, 10.0], volume=[100.0, 100.0]))

    assert result.loc[0, "liquidity_score"] == pytest.approx(10.0)
    assert result.loc[1, "liquidity_score"] == pytest.approx(100.0)


def test_liquidity_score_is_clipped_to_zero_and_one_hundred() -> None:
    result = RangeScorer(
        stability_window=1,
        liquidity_window=1,
        median_trading_value_target=100.0,
    ).transform(_score_features(close=[100.0, 100.0], volume=[0.0, 10.0]))

    assert result.loc[0, "liquidity_score"] == 0.0
    assert result.loc[1, "liquidity_score"] == 100.0


def test_invalid_score_weights_are_rejected_instead_of_normalized() -> None:
    with pytest.raises(ValueError, match="sum to 1.0"):
        RangeScoreWeights(trend=0.4)


def test_negative_score_weight_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        RangeScoreWeights(
            trend=-0.1,
            mean_reversion=0.5,
            stability=0.3,
            liquidity=0.3,
        )


def test_custom_score_weights_change_the_composite_score() -> None:
    features = pd.DataFrame(
        {
            "normalized_slope": [0.0],
            "adx": [0.0],
            "ma_crossings": [0.0],
            "atr": [10.0],
            "sma": [100.0],
            "range_width": [0.2],
            "close": [1.0],
            "volume": [100.0],
        }
    )
    scorer = RangeScorer(
        weights=RangeScoreWeights(
            trend=1.0, mean_reversion=0.0, stability=0.0, liquidity=0.0
        ),
        stability_window=1,
        liquidity_window=1,
        median_trading_value_target=100.0,
    )

    result = scorer.transform(features)

    assert result.loc[0, "range_score"] == pytest.approx(result.loc[0, "trend_score"])


def test_range_series_scores_above_directional_trend() -> None:
    size = 240
    ranging_close = 100.0 + 4.0 * np.sin(np.arange(size) * 2.0 * np.pi / 10.0)
    trending_close = 100.0 + 0.5 * np.arange(size)
    detector = RangeDetector()
    scorer = RangeScorer()

    ranging = scorer.transform(detector.transform(_ohlcv(ranging_close)))
    trending = scorer.transform(detector.transform(_ohlcv(trending_close)))

    assert ranging.iloc[-1]["range_score"] > trending.iloc[-1]["range_score"]
    assert bool(ranging.iloc[-1]["is_range_market"])
    assert not bool(trending.iloc[-1]["is_range_market"])


def test_future_changes_do_not_alter_past_range_results() -> None:
    close = 100.0 + 3.0 * np.sin(np.arange(100) / 2.0)
    frame = _ohlcv(close)
    detector = RangeDetector(
        sma_period=5,
        atr_period=5,
        adx_period=5,
        range_window=10,
        slope_lookback=5,
    )
    scorer = RangeScorer(
        stability_window=5,
        liquidity_window=10,
        median_trading_value_target=10_000_000.0,
    )
    original = scorer.transform(detector.transform(frame))

    changed_frame = frame.copy()
    for column in ("open", "high", "low", "close"):
        changed_frame.loc[80:, column] += 1_000.0
    changed = scorer.transform(detector.transform(changed_frame))

    pd.testing.assert_frame_equal(original.iloc[:80], changed.iloc[:80])
