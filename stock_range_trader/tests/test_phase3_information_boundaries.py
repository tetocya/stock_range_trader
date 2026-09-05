"""Adversarial information-boundary tests for Phase 3."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from phase3_adversarial_helpers import (
    adversarial_dates,
    adversarial_fold,
    canonical_bars,
    executable_catalog,
    install_signal_pipeline,
    mutate_ohlc_lane,
    result_projection,
    signal_catalog,
    strategy_config,
)

import walkforward.executable_evaluation as executable_module
from walkforward import (
    ExecutableOutcomeEvaluator,
    ProviderCapabilityRegistry,
    PurgePolicy,
    SignalOutcomeEvaluator,
    derive_executable_validation_cohort,
    derive_signal_validation_cohort,
)


def _constant_features(frame, config):
    result = frame.copy()
    result["sma"] = 100.0
    result["atr"] = 10.0
    result["adx"] = 10.0
    result["range_score"] = 90.0
    return result


def test_signal_validation_ignores_test_provider_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = adversarial_dates()
    fold = adversarial_fold()
    bars = canonical_bars(provider="yfinance")
    install_signal_pipeline(monkeypatch, {dates[17].date()})
    evaluator = SignalOutcomeEvaluator(strategy_config(), ProviderCapabilityRegistry())

    baseline = evaluator.evaluate_validation(
        bars, fold, signal_catalog(), PurgePolicy(3)
    )
    changed = bars.copy()
    test_mask = changed["date"].dt.date >= fold.test_start
    assert int(test_mask.sum()) > 0
    changed.loc[test_mask, "provider"] = "unknown_provider"

    modified = evaluator.evaluate_validation(
        changed, fold, signal_catalog(), PurgePolicy(3)
    )

    assert modified == baseline
    assert baseline.observations


def test_signal_validation_ignores_added_test_row_from_another_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = adversarial_dates()
    fold = adversarial_fold()
    bars = canonical_bars(provider="yfinance")
    install_signal_pipeline(monkeypatch, {dates[17].date()})
    evaluator = SignalOutcomeEvaluator(strategy_config(), ProviderCapabilityRegistry())
    missing_test_date = pd.Timestamp("2024-02-08")
    assert fold.test_start <= missing_test_date.date() < fold.test_end
    assert missing_test_date not in set(bars["date"])
    added = canonical_bars(
        provider="jquants", dates=pd.DatetimeIndex([missing_test_date])
    )

    baseline = evaluator.evaluate_validation(
        bars, fold, signal_catalog(), PurgePolicy(3)
    )
    extended = evaluator.evaluate_validation(
        pd.concat([bars, added], ignore_index=True),
        fold,
        signal_catalog(),
        PurgePolicy(3),
    )

    assert extended == baseline


def test_signal_validation_uses_only_adjusted_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = adversarial_dates()
    fold = adversarial_fold()
    bars = canonical_bars(provider="yfinance")
    install_signal_pipeline(monkeypatch, {dates[17].date()})
    evaluator = SignalOutcomeEvaluator(strategy_config(), ProviderCapabilityRegistry())
    before_test = bars["date"].dt.date < fold.test_start
    changed = mutate_ohlc_lane(bars, before_test, lane="raw", multiplier=7.0)
    changed.loc[before_test, "dividend"] = 123.0

    baseline = evaluator.evaluate_validation(
        bars, fold, signal_catalog(), PurgePolicy(3)
    )
    modified = evaluator.evaluate_validation(
        changed, fold, signal_catalog(), PurgePolicy(3)
    )

    assert modified == baseline
    assert not hasattr(modified, "trade_records")
    assert not hasattr(modified, "equity_curve")


def test_gap_price_changes_signal_labels_but_not_executable_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = adversarial_dates()
    fold = adversarial_fold()
    bars = canonical_bars()
    install_signal_pipeline(monkeypatch, {dates[17].date()})
    monkeypatch.setattr(executable_module, "_prepare_features", _constant_features)
    gap_mask = (bars["date"].dt.date >= fold.validation_end) & (
        bars["date"].dt.date < fold.test_start
    )
    assert int(gap_mask.sum()) == 5
    changed = mutate_ohlc_lane(bars, gap_mask, lane="adjusted", multiplier=1.5)
    signal_evaluator = SignalOutcomeEvaluator(
        strategy_config(), ProviderCapabilityRegistry()
    )
    executable_evaluator = ExecutableOutcomeEvaluator(
        strategy_config(), ProviderCapabilityRegistry()
    )

    signal_before = signal_evaluator.evaluate_validation(
        bars, fold, signal_catalog(), PurgePolicy(3)
    )
    signal_after = signal_evaluator.evaluate_validation(
        changed, fold, signal_catalog(), PurgePolicy(3)
    )
    executable_before = executable_evaluator.evaluate_validation(
        bars, fold, executable_catalog()
    )
    executable_after = executable_evaluator.evaluate_validation(
        changed, fold, executable_catalog()
    )

    assert signal_after.observations != signal_before.observations
    assert signal_after.scores != signal_before.scores
    assert result_projection(executable_after) == result_projection(executable_before)


def test_signal_validation_is_invariant_to_input_row_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = adversarial_dates()
    fold = adversarial_fold()
    bars = pd.concat(
        [
            canonical_bars("7203.T", provider="yfinance"),
            canonical_bars("1301.T", provider="yfinance"),
            canonical_bars("9984.T", provider="yfinance"),
        ],
        ignore_index=True,
    )
    install_signal_pipeline(
        monkeypatch,
        {dates[9].date(), dates[10].date(), dates[13].date()},
    )
    evaluator = SignalOutcomeEvaluator(strategy_config(), ProviderCapabilityRegistry())

    ordered = evaluator.evaluate_validation(
        bars, fold, signal_catalog(), PurgePolicy(3)
    )
    shuffled = evaluator.evaluate_validation(
        bars.sample(frac=1.0, random_state=90210).reset_index(drop=True),
        fold,
        signal_catalog(),
        PurgePolicy(3),
    )

    assert shuffled == ordered


def test_executable_validation_execution_lane_mutation_is_nontrivial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fold = adversarial_fold()
    bars = canonical_bars()
    monkeypatch.setattr(executable_module, "_prepare_features", _constant_features)
    validation_mask = (bars["date"].dt.date >= fold.validation_start) & (
        bars["date"].dt.date < fold.validation_end
    )
    changed = mutate_ohlc_lane(bars, validation_mask, lane="raw", multiplier=1.4)
    evaluator = ExecutableOutcomeEvaluator(
        strategy_config(), ProviderCapabilityRegistry()
    )

    baseline = evaluator.evaluate_validation(bars, fold, executable_catalog())
    modified = evaluator.evaluate_validation(changed, fold, executable_catalog())

    assert modified.symbol_outcomes != baseline.symbol_outcomes
    assert np.array_equal(
        changed.loc[validation_mask, "adjusted_close"],
        bars.loc[validation_mask, "adjusted_close"],
    )


def test_signal_test_is_invariant_to_input_row_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = adversarial_dates()
    fold = adversarial_fold()
    bars = canonical_bars(provider="yfinance")
    install_signal_pipeline(monkeypatch, {dates[10].date(), dates[25].date()})
    evaluator = SignalOutcomeEvaluator(strategy_config(), ProviderCapabilityRegistry())
    validation = evaluator.evaluate_validation(
        bars, fold, signal_catalog(), PurgePolicy(3)
    )
    cohort = derive_signal_validation_cohort(bars, fold, validation)

    ordered = evaluator.evaluate_test(
        bars, fold, signal_catalog().candidates[0], PurgePolicy(3), cohort
    )
    shuffled = evaluator.evaluate_test(
        bars.sample(frac=1.0, random_state=19).reset_index(drop=True),
        fold,
        signal_catalog().candidates[0],
        PurgePolicy(3),
        cohort,
    )

    assert shuffled == ordered


def test_executable_validation_and_test_are_invariant_to_input_row_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fold = adversarial_fold()
    bars = pd.concat(
        [canonical_bars("7203.T"), canonical_bars("1301.T")], ignore_index=True
    )
    shuffled_bars = bars.sample(frac=1.0, random_state=27).reset_index(drop=True)
    monkeypatch.setattr(executable_module, "_prepare_features", _constant_features)
    evaluator = ExecutableOutcomeEvaluator(
        strategy_config(), ProviderCapabilityRegistry()
    )

    validation = evaluator.evaluate_validation(bars, fold, executable_catalog())
    shuffled_validation = evaluator.evaluate_validation(
        shuffled_bars, fold, executable_catalog()
    )
    cohort = derive_executable_validation_cohort(bars, fold, validation)
    test = evaluator.evaluate_test(
        bars, fold, executable_catalog().candidates[0], cohort
    )
    shuffled_test = evaluator.evaluate_test(
        shuffled_bars, fold, executable_catalog().candidates[0], cohort
    )

    assert shuffled_validation == validation
    assert shuffled_test == test
