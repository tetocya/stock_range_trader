"""STEP 8 Test-only OOS aggregation tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from test_phase3_runner import (
    _empty_signal_validation,
    _schedule,
    _signal_selection,
)

from data import provider_price_basis
from walkforward import (
    AnalysisMode,
    CandidateAssessment,
    CandidateSelection,
    SelectionStatus,
    SignalFoldRunResult,
    SignalOutcomeObservation,
    SignalTestEvaluationResult,
    SignalTestSummary,
    SignalWalkForwardAggregator,
    SignalWalkForwardRunResult,
    ValidationCohort,
    WalkForwardAggregateStatus,
    WalkForwardAggregationError,
    WalkForwardFold,
)
from walkforward import TestEvaluationStatus as EvaluationStatus


def _fold(identifier: str, month: int) -> WalkForwardFold:
    return WalkForwardFold(
        identifier,
        date(2024, month, 1),
        date(2024, month + 1, 1),
        date(2024, month + 1, 1),
        date(2024, month + 2, 1),
        date(2024, month + 2, 1),
        date(2024, month + 3, 1),
        2,
    )


def _observation(
    fold: WalkForwardFold, feature_day: int, forward_return: float, *, hit: bool
) -> SignalOutcomeObservation:
    feature = date(2024, fold.test_start.month, feature_day)
    return SignalOutcomeObservation(
        fold_id=fold.fold_id,
        provider="yfinance",
        candidate_id="candidate_b",
        symbol="7203.T",
        feature_date=feature,
        label_start_date=date(2024, fold.test_start.month, feature_day + 1),
        label_end_date=date(2024, fold.test_start.month, feature_day + 2),
        signal_close=100.0,
        signal_date_sma=105.0,
        signal_date_atr=5.0,
        buy_threshold=97.5,
        range_score=80.0,
        adx=20.0,
        forward_return=forward_return,
        mean_reversion_target_hit=hit,
        maximum_adverse_excursion=-0.1,
        maximum_adverse_excursion_magnitude=0.1,
        maximum_favorable_excursion=0.2,
    )


def _evaluated_fold(
    fold: WalkForwardFold, observations: tuple[SignalOutcomeObservation, ...]
) -> SignalFoldRunResult:
    hits = sum(item.mean_reversion_target_hit for item in observations)
    ordered_returns = sorted(item.forward_return for item in observations)
    midpoint = len(ordered_returns) // 2
    median = (
        ordered_returns[midpoint]
        if len(ordered_returns) % 2
        else (ordered_returns[midpoint - 1] + ordered_returns[midpoint]) / 2
    )
    test = SignalTestEvaluationResult(
        provider="yfinance",
        provider_price_basis=provider_price_basis("yfinance"),
        fold_id=fold.fold_id,
        candidate_id="candidate_b",
        requested_symbols=("7203.T",),
        requested_symbol_count=1,
        admitted_symbol_count=1,
        observations=observations,
        observation_exclusions=(),
        symbol_exclusions=(),
        summary=SignalTestSummary(
            "candidate_b", len(observations), hits / len(observations), median, 0.1
        ),
    )
    candidate_ids = ("candidate_b", "candidate_a", "never_selected")
    return SignalFoldRunResult(
        fold=fold,
        validation_cohort=ValidationCohort(
            "yfinance", provider_price_basis("yfinance"), ("7203.T",)
        ),
        validation_result=_empty_signal_validation(fold, candidate_ids),
        selection=_signal_selection(candidate_ids, "candidate_b"),
        test_status=EvaluationStatus.EVALUATED,
        test_result=test,
    )


def _run(*fold_results: SignalFoldRunResult) -> SignalWalkForwardRunResult:
    return SignalWalkForwardRunResult(
        provider="yfinance",
        provider_price_basis=provider_price_basis("yfinance"),
        schedule=_schedule(*(item.fold for item in fold_results)),
        fold_results=fold_results,
    )


def test_signal_aggregate_pools_test_observations_instead_of_fold_means() -> None:
    first = _fold("fold_0001", 1)
    second = _fold("fold_0002", 4)
    run = _run(
        _evaluated_fold(first, (_observation(first, 2, 0.0, hit=False),)),
        _evaluated_fold(
            second,
            tuple(_observation(second, day, 1.0, hit=True) for day in (2, 5, 8)),
        ),
    )

    aggregate = SignalWalkForwardAggregator().aggregate(run)

    assert aggregate.test_observation_count == 4
    assert aggregate.mean_reversion_target_hit_rate == pytest.approx(0.75)
    assert aggregate.median_forward_return == pytest.approx(1.0)
    assert aggregate.median_forward_return != pytest.approx(0.5)
    assert aggregate.aggregate_status is (
        WalkForwardAggregateStatus.COMPLETED_ALL_FOLDS_EVALUATED
    )


def test_candidate_frequency_keeps_catalog_order_and_unselected_zero() -> None:
    fold = _fold("fold_0001", 1)
    aggregate = SignalWalkForwardAggregator().aggregate(
        _run(_evaluated_fold(fold, (_observation(fold, 2, 0.1, hit=True),)))
    )

    assert tuple(
        (item.candidate_id, item.selected_fold_count)
        for item in aggregate.selected_candidate_counts
    ) == (("candidate_b", 1), ("candidate_a", 0), ("never_selected", 0))


def test_all_no_eligible_folds_are_completed_with_none_oos_metrics() -> None:
    fold = _fold("fold_0001", 1)
    candidate_ids = ("candidate_b", "candidate_a")
    selection = CandidateSelection(
        analysis_mode=AnalysisMode.SIGNAL_VALIDATION,
        status=SelectionStatus.NO_ELIGIBLE_CANDIDATE,
        selected_candidate_id=None,
        ranked_candidate_ids=(),
        assessments=tuple(
            CandidateAssessment(
                candidate_id=item,
                eligible=False,
                rejection_reasons=(
                    "insufficient_observation_count",
                    "invalid_primary_metric",
                    "invalid_median_forward_return",
                    "invalid_median_mae_magnitude",
                ),
                rank=None,
            )
            for item in sorted(candidate_ids)
        ),
    )
    fold_result = SignalFoldRunResult(
        fold=fold,
        validation_cohort=ValidationCohort(
            "yfinance", provider_price_basis("yfinance"), ("7203.T",)
        ),
        validation_result=_empty_signal_validation(fold, candidate_ids),
        selection=selection,
        test_status=EvaluationStatus.NOT_RUN_NO_ELIGIBLE_CANDIDATE,
        test_result=None,
    )

    aggregate = SignalWalkForwardAggregator().aggregate(_run(fold_result))

    assert aggregate.aggregate_status is (
        WalkForwardAggregateStatus.COMPLETED_NO_TEST_FOLDS
    )
    assert aggregate.evaluated_fold_count == 0
    assert aggregate.no_eligible_fold_count == 1
    assert aggregate.mean_reversion_target_hit_rate is None
    assert aggregate.median_forward_return is None


def test_duplicate_symbol_feature_date_across_folds_is_rejected() -> None:
    first = _fold("fold_0001", 1)
    second = _fold("fold_0002", 4)
    first_item = _observation(first, 2, 0.1, hit=True)
    duplicate = replace(first_item, fold_id=second.fold_id)
    run = _run(
        _evaluated_fold(first, (first_item,)),
        _evaluated_fold(second, (duplicate,)),
    )

    with pytest.raises(WalkForwardAggregationError, match="duplicate"):
        SignalWalkForwardAggregator().aggregate(run)
