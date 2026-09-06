"""Tests for STEP 7 Validation-selection-Test runner orchestration."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pandas as pd
import pytest
from test_phase3_executable_evaluation import (
    _catalog as executable_catalog,
)
from test_phase3_executable_evaluation import (
    _controlled_prices,
    _install_constant_features,
)
from test_phase3_executable_evaluation import (
    _evaluator as executable_evaluator,
)
from test_phase3_executable_evaluation import (
    _fold as executable_fold,
)
from test_phase3_signal_evaluation import _bars as signal_bars
from test_phase3_signal_evaluation import _catalog as signal_catalog
from test_phase3_signal_evaluation import _fold as signal_fold

from config import ExecutableSelectionPolicy, FoldScheduleConfig
from data import provider_price_basis
from walkforward import (
    AnalysisMode,
    CandidateAssessment,
    CandidateSelection,
    ExecutableCandidateSelector,
    ExecutableTestSummary,
    ExecutableValidationScore,
    ExecutableWalkForwardRunner,
    FoldSchedule,
    PurgePolicy,
    SelectionInputError,
    SelectionStatus,
    SignalCandidateSelector,
    SignalFoldRunResult,
    SignalOutcomeEvaluationResult,
    SignalTestEvaluationResult,
    SignalTestSummary,
    SignalValidationScore,
    SignalWalkForwardRunner,
    ValidationCohort,
    WalkForwardFold,
    WalkForwardResultError,
    WalkForwardRunnerError,
    derive_executable_validation_cohort,
    derive_signal_validation_cohort,
)
from walkforward import (
    TestEvaluationStatus as EvaluationStatus,
)


def _schedule(*folds: WalkForwardFold) -> FoldSchedule:
    return FoldSchedule(
        config=FoldScheduleConfig(
            train_months=1,
            validation_months=1,
            test_months=1,
            step_months=1,
            forward_sessions=2,
            embargo_sessions=2,
            minimum_folds=len(folds),
            purge_rule="label_end_date_lt_test_start",
        ),
        configured_start=min(fold.train_start for fold in folds),
        configured_end=max(fold.test_end for fold in folds),
        folds=tuple(folds),
    )


def _signal_selection(
    candidate_ids: tuple[str, ...], selected: str
) -> CandidateSelection:
    ranked = (selected, *(item for item in candidate_ids if item != selected))
    return CandidateSelection(
        analysis_mode=AnalysisMode.SIGNAL_VALIDATION,
        status=SelectionStatus.SELECTED,
        selected_candidate_id=selected,
        ranked_candidate_ids=ranked,
        assessments=tuple(
            CandidateAssessment(
                candidate_id=candidate_id,
                eligible=True,
                rejection_reasons=(),
                rank=ranked.index(candidate_id) + 1,
            )
            for candidate_id in sorted(candidate_ids)
        ),
    )


def _empty_signal_validation(
    fold: WalkForwardFold,
    candidate_ids: tuple[str, ...],
) -> SignalOutcomeEvaluationResult:
    return SignalOutcomeEvaluationResult(
        provider="yfinance",
        provider_price_basis=provider_price_basis("yfinance"),
        fold_id=fold.fold_id,
        input_symbol_count=1,
        admitted_symbol_count=1,
        observations=(),
        observation_exclusions=(),
        symbol_exclusions=(),
        scores=tuple(
            SignalValidationScore(candidate_id, 0, None, None, None)
            for candidate_id in candidate_ids
        ),
    )


def _empty_signal_test(
    fold: WalkForwardFold,
    candidate_id: str,
) -> SignalTestEvaluationResult:
    return SignalTestEvaluationResult(
        provider="yfinance",
        provider_price_basis=provider_price_basis("yfinance"),
        fold_id=fold.fold_id,
        candidate_id=candidate_id,
        requested_symbols=("7203.T",),
        requested_symbol_count=1,
        admitted_symbol_count=1,
        observations=(),
        observation_exclusions=(),
        symbol_exclusions=(),
        summary=SignalTestSummary(candidate_id, 0, None, None, None),
    )


def test_test_summaries_are_separate_types_and_selectors_reject_them() -> None:
    signal_summary = SignalTestSummary("baseline", 0, None, None, None)
    executable_summary = ExecutableTestSummary(
        "baseline", 0, 0, 0, 0, 0, None, None, None, None
    )

    assert not isinstance(signal_summary, SignalValidationScore)
    assert not isinstance(executable_summary, ExecutableValidationScore)
    with pytest.raises(SelectionInputError):
        SignalCandidateSelector.__new__(SignalCandidateSelector).select(
            signal_catalog(), (signal_summary,)
        )
    with pytest.raises(SelectionInputError):
        ExecutableCandidateSelector.__new__(ExecutableCandidateSelector).select(
            executable_catalog(), (executable_summary,)
        )


def test_signal_runner_calls_validation_selection_and_one_test_in_order() -> None:
    events: list[str] = []
    catalog = signal_catalog("candidate_a", "candidate_b")
    fold = signal_fold()

    class Evaluator:
        def evaluate_validation(self, bars, received_fold, received_catalog, policy):
            events.append("evaluate_validation")
            return _empty_signal_validation(
                received_fold, received_catalog.candidate_ids
            )

        def evaluate_test(self, bars, received_fold, candidate, policy, cohort):
            events.append(f"evaluate_test:{candidate.candidate_id}")
            return _empty_signal_test(received_fold, candidate.candidate_id)

    class Selector:
        def select(self, received_catalog, scores):
            events.append("selector.select")
            assert all(isinstance(score, SignalValidationScore) for score in scores)
            return _signal_selection(received_catalog.candidate_ids, "candidate_a")

    result = SignalWalkForwardRunner(Evaluator(), Selector(), PurgePolicy(2)).run(
        signal_bars(), _schedule(fold), catalog
    )

    assert events == (
        ["evaluate_validation", "selector.select", "evaluate_test:candidate_a"]
    )
    assert result.fold_results[0].selection.selected_candidate_id == "candidate_a"
    assert result.fold_results[0].test_result.candidate_id == "candidate_a"
    assert "candidate_b" not in events


def test_no_eligible_skips_test_and_continues_next_fold() -> None:
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
        dates[16].date(),
        dates[20].date(),
        dates[20].date(),
        dates[25].date(),
        dates[25].date(),
        dates[30].date(),
        2,
    )
    catalog = signal_catalog("candidate_a", "candidate_b")
    validation_calls: list[str] = []
    test_calls: list[str] = []

    class Evaluator:
        def evaluate_validation(self, bars, fold, catalog, policy):
            validation_calls.append(fold.fold_id)
            return _empty_signal_validation(fold, catalog.candidate_ids)

        def evaluate_test(self, *args):
            test_calls.append("called")
            raise AssertionError("Test must not run")

    class Selector:
        def select(self, received_catalog, scores):
            return CandidateSelection(
                analysis_mode=AnalysisMode.SIGNAL_VALIDATION,
                status=SelectionStatus.NO_ELIGIBLE_CANDIDATE,
                selected_candidate_id=None,
                ranked_candidate_ids=(),
                assessments=tuple(
                    CandidateAssessment(
                        candidate_id=item,
                        eligible=False,
                        rejection_reasons=("insufficient_observation_count",),
                        rank=None,
                    )
                    for item in sorted(received_catalog.candidate_ids)
                ),
            )

    result = SignalWalkForwardRunner(Evaluator(), Selector(), PurgePolicy(2)).run(
        signal_bars(dates=dates), _schedule(first, second), catalog
    )

    assert validation_calls == ["fold_0001", "fold_0002"]
    assert test_calls == []
    assert all(
        item.test_status is EvaluationStatus.NOT_RUN_NO_ELIGIBLE_CANDIDATE
        for item in result.fold_results
    )
    assert all(item.test_result is None for item in result.fold_results)


def test_executable_runner_uses_validation_scores_then_one_selected_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    events: list[str] = []
    real = executable_evaluator()

    class Evaluator:
        def evaluate_validation(self, bars, fold, catalog):
            events.append("evaluate_validation")
            return real.evaluate_validation(bars, fold, catalog)

        def evaluate_test(self, bars, fold, candidate, cohort):
            events.append(f"evaluate_test:{candidate.candidate_id}")
            return real.evaluate_test(bars, fold, candidate, cohort)

    policy = ExecutableSelectionPolicy(
        primary_metric="median_symbol_sharpe_ratio",
        minimum_traded_symbol_count=1,
        minimum_trading_symbol_ratio=1.0,
        minimum_total_trade_count=1,
        minimum_finite_sharpe_count=1,
        maximum_drawdown_limit=0.9,
        tie_breakers=(
            "median_symbol_maximum_drawdown_magnitude_asc",
            "median_symbol_net_return_desc",
            "candidate_id_asc",
        ),
    )

    class Selector:
        def select(self, catalog, scores):
            events.append("selector.select")
            assert all(isinstance(score, ExecutableValidationScore) for score in scores)
            return ExecutableCandidateSelector(policy).select(catalog, scores)

    result = ExecutableWalkForwardRunner(Evaluator(), Selector()).run(
        _controlled_prices(),
        _schedule(executable_fold()),
        executable_catalog("candidate_a", "candidate_b"),
    )

    assert events == [
        "evaluate_validation",
        "selector.select",
        "evaluate_test:candidate_a",
    ]
    assert result.fold_results[0].test_result.candidate_id == "candidate_a"


def test_cohort_derivation_freezes_validation_admitted_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    fold = executable_fold()
    catalog = executable_catalog()
    bars = _controlled_prices()
    validation = executable_evaluator().evaluate_validation(bars, fold, catalog)

    cohort = derive_executable_validation_cohort(bars, fold, validation)

    assert cohort.symbols == ("7203.T",)
    assert cohort.provider == validation.provider
    assert cohort.provider_price_basis == validation.provider_price_basis


def test_signal_cohort_rejects_input_count_drift() -> None:
    fold = signal_fold()
    validation = _empty_signal_validation(fold, ("baseline",))
    extra = signal_bars("1301.T")

    with pytest.raises(WalkForwardRunnerError, match="input symbols"):
        derive_signal_validation_cohort(
            pd.concat([signal_bars(), extra], ignore_index=True), fold, validation
        )


def test_fold_result_rejects_cohort_and_test_requested_symbol_mismatch() -> None:
    fold = signal_fold()
    validation = _empty_signal_validation(fold, ("baseline",))
    selection = _signal_selection(("baseline",), "baseline")
    test = _empty_signal_test(fold, "baseline")

    with pytest.raises(WalkForwardRunnerError, match="requested symbols"):
        SignalFoldRunResult(
            fold=fold,
            validation_cohort=ValidationCohort(
                "yfinance", provider_price_basis("yfinance"), ("1301.T",)
            ),
            validation_result=validation,
            selection=selection,
            test_status=EvaluationStatus.EVALUATED,
            test_result=test,
        )


def test_validation_cohort_and_run_results_are_immutable() -> None:
    cohort = ValidationCohort(
        "yfinance", provider_price_basis("yfinance"), ("1301.T", "7203.T")
    )

    with pytest.raises(FrozenInstanceError):
        cohort.provider = "jquants"  # type: ignore[misc]
    with pytest.raises(WalkForwardResultError):
        ValidationCohort(
            "yfinance", provider_price_basis("yfinance"), ("7203.T", "1301.T")
        )
