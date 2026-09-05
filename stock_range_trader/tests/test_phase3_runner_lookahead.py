"""Look-ahead and fallback guards for STEP 7 walk-forward runners."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from test_phase3_runner import _empty_signal_test, _schedule
from test_phase3_signal_evaluation import (
    _bars,
    _catalog,
    _dates,
    _evaluator,
    _fold,
    _install_pipeline,
)

from config import SignalSelectionPolicy
from data import provider_price_basis
from walkforward import (
    PurgePolicy,
    SignalCandidateSelector,
    SignalOutcomeEvaluationResult,
    SignalOutcomeObservation,
    SignalValidationScore,
    SignalWalkForwardRunner,
    WalkForwardFold,
)


def _selector() -> SignalCandidateSelector:
    return SignalCandidateSelector(
        SignalSelectionPolicy(
            primary_metric="mean_reversion_target_hit_rate",
            mean_reversion_target="signal_date_sma",
            minimum_observation_count=1,
            tie_breakers=(
                "median_forward_return_desc",
                "median_mae_magnitude_asc",
                "candidate_id_asc",
            ),
        )
    )


def test_test_prices_change_only_test_result_not_validation_selection_or_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    _install_pipeline(monkeypatch, {dates[6].date(), dates[19].date()})
    baseline = _bars()
    changed = baseline.copy()
    test_future = changed["date"].dt.date.isin({dates[20].date(), dates[21].date()})
    for prefix in ("raw", "adjusted"):
        for field in ("open", "high", "low", "close"):
            changed.loc[test_future, f"{prefix}_{field}"] *= 2.0
    changed.loc[test_future, "turnover_value"] *= 2.0
    runner = SignalWalkForwardRunner(_evaluator(), _selector(), PurgePolicy(2))
    catalog = _catalog("candidate_a", "candidate_b")
    schedule = _schedule(_fold())

    original = runner.run(baseline, schedule, catalog).fold_results[0]
    modified = runner.run(changed, schedule, catalog).fold_results[0]

    assert modified.validation_result == original.validation_result
    assert modified.selection == original.selection
    assert modified.validation_cohort == original.validation_cohort
    assert modified.selection.selected_candidate_id == "candidate_a"
    assert modified.test_result != original.test_result


def test_rows_after_test_end_leave_complete_fold_result_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    _install_pipeline(monkeypatch, {dates[6].date(), dates[19].date()})
    future_dates = pd.bdate_range(_fold().test_end + timedelta(days=1), periods=4)
    future = _bars(
        provider="unknown",
        dates=future_dates,
        closes=np.linspace(1.0, 10_000.0, 4),
    )
    runner = SignalWalkForwardRunner(_evaluator(), _selector(), PurgePolicy(2))
    catalog = _catalog("candidate_a", "candidate_b")
    schedule = _schedule(_fold())

    baseline = runner.run(_bars(), schedule, catalog)
    extended = runner.run(
        pd.concat([_bars(), future], ignore_index=True), schedule, catalog
    )

    assert extended == baseline


def test_symbol_appearing_only_in_test_cannot_enter_frozen_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    _install_pipeline(monkeypatch, {dates[6].date(), dates[19].date()})
    newcomer = _bars("9999.T", dates=dates[18:])
    bars = pd.concat([_bars(), newcomer], ignore_index=True)

    result = SignalWalkForwardRunner(_evaluator(), _selector(), PurgePolicy(2)).run(
        bars, _schedule(_fold()), _catalog("candidate_a", "candidate_b")
    )
    fold_result = result.fold_results[0]

    assert fold_result.validation_result.input_symbol_count == 1
    assert fold_result.validation_cohort.symbols == ("7203.T",)
    assert fold_result.test_result.requested_symbols == ("7203.T",)


def test_test_failure_is_propagated_without_second_candidate_fallback() -> None:
    fold = _fold()
    catalog = _catalog("candidate_a", "candidate_b")
    test_candidates: list[str] = []

    class Evaluator:
        def evaluate_validation(self, bars, fold, catalog, policy):
            return _validation_with_rates(fold, (0.9, 0.1))

        def evaluate_test(self, bars, fold, candidate, policy, cohort):
            test_candidates.append(candidate.candidate_id)
            raise RuntimeError("Test evaluator failure")

    with pytest.raises(RuntimeError, match="Test evaluator failure"):
        SignalWalkForwardRunner(Evaluator(), _selector(), PurgePolicy(2)).run(
            _bars(), _schedule(fold), catalog
        )

    assert test_candidates == ["candidate_a"]


def test_future_change_preserves_prior_selection_but_may_affect_later_fold() -> None:
    dates = pd.bdate_range("2024-01-02", periods=50)
    first = WalkForwardFold(
        "fold_0001",
        dates[0].date(),
        dates[5].date(),
        dates[5].date(),
        dates[10].date(),
        dates[10].date(),
        dates[15].date(),
        2,
    )
    second = WalkForwardFold(
        "fold_0002",
        dates[10].date(),
        dates[20].date(),
        dates[20].date(),
        dates[25].date(),
        dates[25].date(),
        dates[30].date(),
        2,
    )
    baseline = _bars(dates=dates)
    changed = baseline.copy()
    changed.loc[changed["date"] == dates[12], "adjusted_close"] = 200.0

    class Evaluator:
        def evaluate_validation(self, bars, fold, catalog, policy):
            if fold.fold_id == "fold_0001":
                rates = (0.9, 0.1)
            else:
                warmup_changed = (
                    float(bars.loc[bars["date"] == dates[12], "adjusted_close"].iloc[0])
                    > 150.0
                )
                rates = (0.1, 0.9) if warmup_changed else (0.9, 0.1)
            return _validation_with_rates(fold, rates)

        def evaluate_test(self, bars, fold, candidate, policy, cohort):
            return _empty_signal_test(fold, candidate.candidate_id)

    runner = SignalWalkForwardRunner(Evaluator(), _selector(), PurgePolicy(2))
    schedule = _schedule(first, second)
    catalog = _catalog("candidate_a", "candidate_b")

    original = runner.run(baseline, schedule, catalog)
    modified = runner.run(changed, schedule, catalog)

    assert original.fold_results[0].selection == modified.fold_results[0].selection
    assert original.fold_results[0].selection.selected_candidate_id == "candidate_a"
    assert original.fold_results[1].selection.selected_candidate_id == "candidate_a"
    assert modified.fold_results[1].selection.selected_candidate_id == "candidate_b"


def _validation_with_rates(
    fold: WalkForwardFold,
    rates: tuple[float, float],
) -> SignalOutcomeEvaluationResult:
    candidate_ids = ("candidate_a", "candidate_b")
    observations = tuple(
        SignalOutcomeObservation(
            fold_id=fold.fold_id,
            provider="yfinance",
            candidate_id=candidate_id,
            symbol="7203.T",
            feature_date=fold.validation_start,
            label_start_date=fold.validation_start + timedelta(days=1),
            label_end_date=fold.validation_start + timedelta(days=2),
            signal_close=100.0,
            signal_date_sma=105.0,
            signal_date_atr=5.0,
            buy_threshold=95.0,
            range_score=80.0,
            adx=10.0,
            forward_return=0.01,
            mean_reversion_target_hit=rate >= 0.5,
            maximum_adverse_excursion=-0.01,
            maximum_adverse_excursion_magnitude=0.01,
            maximum_favorable_excursion=0.02,
        )
        for candidate_id, rate in zip(candidate_ids, rates, strict=True)
    )
    scores = tuple(
        SignalValidationScore(candidate_id, 1, rate, 0.01, 0.01)
        for candidate_id, rate in zip(candidate_ids, rates, strict=True)
    )
    return SignalOutcomeEvaluationResult(
        provider="yfinance",
        provider_price_basis=provider_price_basis("yfinance"),
        fold_id=fold.fold_id,
        input_symbol_count=1,
        admitted_symbol_count=1,
        observations=observations,
        observation_exclusions=(),
        symbol_exclusions=(),
        scores=scores,
    )
