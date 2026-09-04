"""Regression tests preventing look-ahead in Phase 3 Signal Validation."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from test_phase3_signal_evaluation import (
    _bars,
    _catalog,
    _dates,
    _evaluator,
    _fold,
    _install_pipeline,
)

from walkforward import PurgePolicy, SignalOutcomeEvaluator


def _replace_ohlcv(frame: pd.DataFrame, mask: pd.Series, multiplier: float) -> None:
    for prefix in ("raw", "adjusted"):
        for field in ("open", "high", "low", "close"):
            frame.loc[mask, f"{prefix}_{field}"] *= multiplier
    frame.loc[mask, "raw_volume"] *= multiplier
    frame.loc[mask, "adjusted_volume"] *= multiplier
    frame.loc[mask, "turnover_value"] *= multiplier * multiplier


def test_changing_test_prices_does_not_change_validation_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    original = _bars()
    changed = original.copy()
    test_mask = changed["date"].dt.date >= _fold().test_start
    _replace_ohlcv(changed, test_mask, 1_000.0)
    _install_pipeline(
        monkeypatch,
        {dates[6].date(), dates[16].date()},
    )
    evaluator = _evaluator()

    baseline = evaluator.evaluate_validation(
        original, _fold(), _catalog("one", "two"), PurgePolicy(2)
    )
    modified = evaluator.evaluate_validation(
        changed, _fold(), _catalog("one", "two"), PurgePolicy(2)
    )

    assert modified == baseline


def test_pipeline_never_receives_test_price_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[pd.DataFrame] = []
    _install_pipeline(monkeypatch, {_dates()[6].date()}, received=received)
    fold = _fold()

    _evaluator().evaluate_validation(
        _bars(), fold, _catalog("one", "two"), PurgePolicy(2)
    )

    assert len(received) == 2
    assert all(frame["date"].dt.date.max() < fold.test_start for frame in received)
    assert all(frame["date"].dt.date.min() >= fold.train_start for frame in received)


def test_later_validation_price_change_preserves_completed_earlier_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    cutoff = dates[11].date()
    original = _bars()
    changed = original.copy()
    changed_mask = changed["date"].dt.date > cutoff
    _replace_ohlcv(changed, changed_mask, 2.0)

    def causal_pipeline(self, frame, candidate):
        result = frame.copy()
        result["sma"] = result["close"] + 1.0
        result["atr"] = 1.0
        result["adx"] = 10.0
        result["range_score"] = 80.0
        result["buy_threshold"] = result["close"]
        result["entry_condition"] = result["date"].dt.date.isin(
            {dates[6].date(), dates[13].date()}
        )
        return result

    monkeypatch.setattr(SignalOutcomeEvaluator, "_prepare_candidate", causal_pipeline)
    evaluator = _evaluator()
    baseline = evaluator.evaluate_validation(
        original, _fold(), _catalog(), PurgePolicy(2)
    )
    modified = evaluator.evaluate_validation(
        changed, _fold(), _catalog(), PurgePolicy(2)
    )
    early_baseline = tuple(
        item for item in baseline.observations if item.label_end_date <= cutoff
    )
    early_modified = tuple(
        item for item in modified.observations if item.label_end_date <= cutoff
    )

    assert early_modified == early_baseline
    assert len(early_baseline) == 1


def test_target_is_signal_date_sma_not_forward_sma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()

    def changing_sma_pipeline(self, frame, candidate):
        result = frame.copy()
        result["sma"] = 1.0
        result.loc[result["date"].dt.date == dates[6].date(), "sma"] = 105.0
        result.loc[result["date"].dt.date > dates[6].date(), "sma"] = 50.0
        result["atr"] = 1.0
        result["adx"] = 10.0
        result["range_score"] = 80.0
        result["buy_threshold"] = 100.0
        result["entry_condition"] = result["date"].dt.date.eq(dates[6].date())
        return result

    monkeypatch.setattr(
        SignalOutcomeEvaluator, "_prepare_candidate", changing_sma_pipeline
    )
    result = _evaluator().evaluate_validation(
        _bars(), _fold(), _catalog(), PurgePolicy(2)
    )

    assert result.observations[0].signal_date_sma == 105.0
    assert result.observations[0].mean_reversion_target_hit is False


def test_label_end_equal_to_test_start_is_purged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    _install_pipeline(monkeypatch, {dates[16].date()})

    result = _evaluator().evaluate_validation(
        _bars(), _fold(), _catalog(), PurgePolicy(2)
    )

    assert result.observations == ()
    exclusion = result.observation_exclusions[0]
    assert exclusion.feature_date == dates[16].date()
    assert exclusion.reason == PurgePolicy.VALIDATION_OVERLAP_REASON


def test_appending_rows_after_test_end_preserves_complete_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    original = _bars()
    future_dates = pd.bdate_range(_fold().test_end + timedelta(days=1), periods=5)
    future = _bars(dates=future_dates, closes=np.linspace(1.0, 10_000.0, 5))
    _install_pipeline(monkeypatch, {dates[6].date(), dates[9].date()})
    evaluator = _evaluator()

    baseline = evaluator.evaluate_validation(
        original, _fold(), _catalog(), PurgePolicy(2)
    )
    extended = evaluator.evaluate_validation(
        pd.concat([original, future], ignore_index=True),
        _fold(),
        _catalog(),
        PurgePolicy(2),
    )

    assert extended == baseline


def test_test_dates_are_available_only_to_purge_not_outcome_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    received: list[pd.DataFrame] = []
    _install_pipeline(monkeypatch, {dates[16].date()}, received=received)
    bars = _bars()
    test_dates = set(
        bars.loc[bars["date"].dt.date >= _fold().test_start, "date"].dt.date
    )

    result = _evaluator().evaluate_validation(bars, _fold(), _catalog(), PurgePolicy(2))

    assert result.observations == ()
    assert result.observation_exclusions[0].reason == (
        PurgePolicy.VALIDATION_OVERLAP_REASON
    )
    assert test_dates
    assert all(test_dates.isdisjoint(set(frame["date"].dt.date)) for frame in received)


def test_train_rows_are_warmup_only(monkeypatch: pytest.MonkeyPatch) -> None:
    dates = _dates()
    train_signal = dates[3].date()
    validation_signal = dates[6].date()

    def pipeline(self, frame, candidate):
        result = frame.copy()
        result["sma"] = 105.0
        result["atr"] = 5.0
        result["adx"] = 10.0
        result["range_score"] = 80.0
        result["buy_threshold"] = 100.0
        result["entry_condition"] = result["date"].dt.date.isin(
            {train_signal, validation_signal}
        )
        return result

    monkeypatch.setattr(SignalOutcomeEvaluator, "_prepare_candidate", pipeline)
    result = _evaluator().evaluate_validation(
        _bars(), _fold(), _catalog(), PurgePolicy(2)
    )

    assert tuple(item.feature_date for item in result.observations) == (
        validation_signal,
    )
