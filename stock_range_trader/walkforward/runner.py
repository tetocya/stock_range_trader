"""Validation-only selection followed by one-time walk-forward Test evaluation."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from pandas.api.types import is_datetime64_any_dtype

from data import CANONICAL_COLUMNS, CanonicalDataError

from .candidates import (
    ExecutableCandidateCatalog,
    ExecutableCandidateDefinition,
    SignalCandidateCatalog,
    SignalCandidateDefinition,
)
from .capabilities import AnalysisMode
from .executable_evaluation import (
    ExecutableOutcomeEvaluationResult,
    ExecutableOutcomeEvaluator,
    ExecutableTestEvaluationResult,
)
from .folds import FoldSchedule, PurgePolicy, WalkForwardFold
from .result import TestEvaluationStatus, ValidationCohort
from .selection import (
    CandidateSelection,
    ExecutableCandidateSelector,
    SelectionStatus,
    SignalCandidateSelector,
)
from .signal_evaluation import (
    SignalOutcomeEvaluationResult,
    SignalOutcomeEvaluator,
    SignalTestEvaluationResult,
)


class WalkForwardRunnerError(ValueError):
    """Raised when a runner or derived cohort violates STEP 7 contracts."""


@dataclass(frozen=True, slots=True)
class SignalFoldRunResult:
    """Validation, selection, frozen cohort, and optional Signal Test result."""

    fold: WalkForwardFold
    validation_cohort: ValidationCohort
    validation_result: SignalOutcomeEvaluationResult
    selection: CandidateSelection
    test_status: TestEvaluationStatus
    test_result: SignalTestEvaluationResult | None

    def __post_init__(self) -> None:
        _validate_fold_result_common(
            self.fold,
            self.validation_cohort,
            self.validation_result,
            self.selection,
            self.test_status,
            self.test_result,
            AnalysisMode.SIGNAL_VALIDATION,
            SignalOutcomeEvaluationResult,
            SignalTestEvaluationResult,
        )


@dataclass(frozen=True, slots=True)
class ExecutableFoldRunResult:
    """Validation, selection, frozen cohort, and Executable Test result."""

    fold: WalkForwardFold
    validation_cohort: ValidationCohort
    validation_result: ExecutableOutcomeEvaluationResult
    selection: CandidateSelection
    test_status: TestEvaluationStatus
    test_result: ExecutableTestEvaluationResult | None

    def __post_init__(self) -> None:
        _validate_fold_result_common(
            self.fold,
            self.validation_cohort,
            self.validation_result,
            self.selection,
            self.test_status,
            self.test_result,
            AnalysisMode.EXECUTABLE_VALIDATION,
            ExecutableOutcomeEvaluationResult,
            ExecutableTestEvaluationResult,
        )


@dataclass(frozen=True, slots=True)
class SignalWalkForwardRunResult:
    """Deterministic in-memory Signal results in schedule order."""

    provider: str
    provider_price_basis: str
    schedule: FoldSchedule
    fold_results: tuple[SignalFoldRunResult, ...]

    def __post_init__(self) -> None:
        _validate_run_result(
            self.provider,
            self.provider_price_basis,
            self.schedule,
            self.fold_results,
            SignalFoldRunResult,
        )


@dataclass(frozen=True, slots=True)
class ExecutableWalkForwardRunResult:
    """Deterministic in-memory Executable results in schedule order."""

    provider: str
    provider_price_basis: str
    schedule: FoldSchedule
    fold_results: tuple[ExecutableFoldRunResult, ...]

    def __post_init__(self) -> None:
        _validate_run_result(
            self.provider,
            self.provider_price_basis,
            self.schedule,
            self.fold_results,
            ExecutableFoldRunResult,
        )


@dataclass(frozen=True, slots=True)
class SignalWalkForwardRunner:
    """Select with Signal Validation scores, then evaluate one Test candidate."""

    evaluator: SignalOutcomeEvaluator
    selector: SignalCandidateSelector
    purge_policy: PurgePolicy

    def run(
        self,
        bars: pd.DataFrame,
        schedule: FoldSchedule,
        catalog: SignalCandidateCatalog,
    ) -> SignalWalkForwardRunResult:
        """Run every fold independently and preserve schedule order."""

        if not isinstance(schedule, FoldSchedule):
            raise TypeError("schedule must be FoldSchedule")
        if not isinstance(catalog, SignalCandidateCatalog):
            raise TypeError("catalog must be SignalCandidateCatalog")
        _validate_runner_bars(bars)
        results: list[SignalFoldRunResult] = []
        for fold in schedule.folds:
            validation = self.evaluator.evaluate_validation(
                bars, fold, catalog, self.purge_policy
            )
            selection = self.selector.select(catalog, validation.scores)
            cohort = derive_signal_validation_cohort(bars, fold, validation)
            if selection.status is SelectionStatus.NO_ELIGIBLE_CANDIDATE:
                test_status = TestEvaluationStatus.NOT_RUN_NO_ELIGIBLE_CANDIDATE
                test_result = None
            else:
                candidate = _selected_signal_candidate(catalog, selection)
                test_result = self.evaluator.evaluate_test(
                    bars,
                    fold,
                    candidate,
                    self.purge_policy,
                    cohort,
                )
                test_status = TestEvaluationStatus.EVALUATED
            results.append(
                SignalFoldRunResult(
                    fold=fold,
                    validation_cohort=cohort,
                    validation_result=validation,
                    selection=selection,
                    test_status=test_status,
                    test_result=test_result,
                )
            )
        return SignalWalkForwardRunResult(
            provider=results[0].validation_result.provider,
            provider_price_basis=results[0].validation_result.provider_price_basis,
            schedule=schedule,
            fold_results=tuple(results),
        )


@dataclass(frozen=True, slots=True)
class ExecutableWalkForwardRunner:
    """Select with Executable Validation scores, then run one Test candidate."""

    evaluator: ExecutableOutcomeEvaluator
    selector: ExecutableCandidateSelector

    def run(
        self,
        bars: pd.DataFrame,
        schedule: FoldSchedule,
        catalog: ExecutableCandidateCatalog,
    ) -> ExecutableWalkForwardRunResult:
        """Run every fold independently and preserve schedule order."""

        if not isinstance(schedule, FoldSchedule):
            raise TypeError("schedule must be FoldSchedule")
        if not isinstance(catalog, ExecutableCandidateCatalog):
            raise TypeError("catalog must be ExecutableCandidateCatalog")
        _validate_runner_bars(bars)
        results: list[ExecutableFoldRunResult] = []
        for fold in schedule.folds:
            validation = self.evaluator.evaluate_validation(bars, fold, catalog)
            selection = self.selector.select(catalog, validation.scores)
            cohort = derive_executable_validation_cohort(bars, fold, validation)
            if selection.status is SelectionStatus.NO_ELIGIBLE_CANDIDATE:
                test_status = TestEvaluationStatus.NOT_RUN_NO_ELIGIBLE_CANDIDATE
                test_result = None
            else:
                candidate = _selected_executable_candidate(catalog, selection)
                test_result = self.evaluator.evaluate_test(
                    bars, fold, candidate, cohort
                )
                test_status = TestEvaluationStatus.EVALUATED
            results.append(
                ExecutableFoldRunResult(
                    fold=fold,
                    validation_cohort=cohort,
                    validation_result=validation,
                    selection=selection,
                    test_status=test_status,
                    test_result=test_result,
                )
            )
        return ExecutableWalkForwardRunResult(
            provider=results[0].validation_result.provider,
            provider_price_basis=results[0].validation_result.provider_price_basis,
            schedule=schedule,
            fold_results=tuple(results),
        )


def derive_signal_validation_cohort(
    bars: pd.DataFrame,
    fold: WalkForwardFold,
    validation_result: SignalOutcomeEvaluationResult,
) -> ValidationCohort:
    """Freeze Signal symbols using the exact STEP 5 validation input scope."""

    if not isinstance(fold, WalkForwardFold):
        raise TypeError("fold must be WalkForwardFold")
    if not isinstance(validation_result, SignalOutcomeEvaluationResult):
        raise TypeError("validation_result must be SignalOutcomeEvaluationResult")
    _validate_runner_bars(bars)
    scope = bars.loc[bars["date"].dt.date < fold.test_end]
    return _derive_cohort(scope, validation_result)


def derive_executable_validation_cohort(
    bars: pd.DataFrame,
    fold: WalkForwardFold,
    validation_result: ExecutableOutcomeEvaluationResult,
) -> ValidationCohort:
    """Freeze Executable symbols using the exact STEP 6 validation scope."""

    if not isinstance(fold, WalkForwardFold):
        raise TypeError("fold must be WalkForwardFold")
    if not isinstance(validation_result, ExecutableOutcomeEvaluationResult):
        raise TypeError("validation_result must be ExecutableOutcomeEvaluationResult")
    _validate_runner_bars(bars)
    scope = bars.loc[
        (bars["date"].dt.date >= fold.train_start)
        & (bars["date"].dt.date < fold.validation_end)
    ]
    return _derive_cohort(scope, validation_result)


def _derive_cohort(scope: pd.DataFrame, validation_result: object) -> ValidationCohort:
    input_symbols = tuple(sorted(set(scope["symbol"].astype(str))))
    if len(input_symbols) != validation_result.input_symbol_count:
        raise WalkForwardRunnerError(
            "derived input symbols do not match Validation input_symbol_count"
        )
    excluded = {item.symbol for item in validation_result.symbol_exclusions}
    if not excluded.issubset(input_symbols):
        raise WalkForwardRunnerError(
            "Validation exclusions contain symbols outside the input scope"
        )
    admitted = tuple(symbol for symbol in input_symbols if symbol not in excluded)
    if len(admitted) != validation_result.admitted_symbol_count:
        raise WalkForwardRunnerError(
            "derived cohort does not match Validation admitted_symbol_count"
        )
    return ValidationCohort(
        provider=validation_result.provider,
        provider_price_basis=validation_result.provider_price_basis,
        symbols=admitted,
    )


def _selected_signal_candidate(
    catalog: SignalCandidateCatalog,
    selection: CandidateSelection,
) -> SignalCandidateDefinition:
    selected_id = selection.selected_candidate_id
    matches = tuple(
        candidate
        for candidate in catalog.candidates
        if candidate.candidate_id == selected_id
    )
    if len(matches) != 1:
        raise WalkForwardRunnerError(
            "selected Signal candidate must match exactly one catalog entry"
        )
    return matches[0]


def _selected_executable_candidate(
    catalog: ExecutableCandidateCatalog,
    selection: CandidateSelection,
) -> ExecutableCandidateDefinition:
    selected_id = selection.selected_candidate_id
    matches = tuple(
        candidate
        for candidate in catalog.candidates
        if candidate.candidate_id == selected_id
    )
    if len(matches) != 1:
        raise WalkForwardRunnerError(
            "selected Executable candidate must match exactly one catalog entry"
        )
    return matches[0]


def _validate_fold_result_common(
    fold: object,
    cohort: object,
    validation: object,
    selection: object,
    test_status: object,
    test_result: object,
    analysis_mode: AnalysisMode,
    validation_type: type,
    test_type: type,
) -> None:
    if not isinstance(fold, WalkForwardFold):
        raise TypeError("fold must be WalkForwardFold")
    if not isinstance(cohort, ValidationCohort):
        raise TypeError("validation_cohort must be ValidationCohort")
    if not isinstance(validation, validation_type):
        raise TypeError(f"validation_result must be {validation_type.__name__}")
    if not isinstance(selection, CandidateSelection):
        raise TypeError("selection must be CandidateSelection")
    if not isinstance(test_status, TestEvaluationStatus):
        raise TypeError("test_status must be TestEvaluationStatus")
    if test_result is not None and not isinstance(test_result, test_type):
        raise TypeError(f"test_result must be {test_type.__name__} or None")
    if validation.fold_id != fold.fold_id:
        raise WalkForwardRunnerError("Validation result fold_id must match fold")
    if (
        validation.provider != cohort.provider
        or validation.provider_price_basis != cohort.provider_price_basis
    ):
        raise WalkForwardRunnerError(
            "Validation provider contract must match the frozen cohort"
        )
    if selection.analysis_mode is not analysis_mode:
        raise WalkForwardRunnerError("selection analysis mode does not match runner")
    if len(cohort.symbols) != validation.admitted_symbol_count:
        raise WalkForwardRunnerError(
            "cohort size must match Validation admitted_symbol_count"
        )
    if selection.status is SelectionStatus.SELECTED:
        if test_status is not TestEvaluationStatus.EVALUATED or test_result is None:
            raise WalkForwardRunnerError(
                "selected folds require an evaluated Test result"
            )
        if test_result.fold_id != fold.fold_id:
            raise WalkForwardRunnerError("Test result fold_id must match fold")
        if (
            test_result.provider != cohort.provider
            or test_result.provider_price_basis != cohort.provider_price_basis
        ):
            raise WalkForwardRunnerError(
                "Test provider contract must match the frozen cohort"
            )
        if test_result.candidate_id != selection.selected_candidate_id:
            raise WalkForwardRunnerError(
                "Test candidate must match the selected candidate"
            )
        if test_result.requested_symbols != cohort.symbols:
            raise WalkForwardRunnerError(
                "Test requested symbols must match the frozen cohort"
            )
    elif selection.status is SelectionStatus.NO_ELIGIBLE_CANDIDATE:
        if (
            test_status is not TestEvaluationStatus.NOT_RUN_NO_ELIGIBLE_CANDIDATE
            or test_result is not None
        ):
            raise WalkForwardRunnerError(
                "no-eligible folds must not contain a Test result"
            )
    else:
        raise WalkForwardRunnerError("unknown Candidate selection status")


def _validate_run_result(
    provider: object,
    provider_price_basis: object,
    schedule: object,
    fold_results: object,
    fold_result_type: type,
) -> None:
    for name, value in (
        ("provider", provider),
        ("provider_price_basis", provider_price_basis),
    ):
        if not isinstance(value, str) or not value.strip():
            raise WalkForwardRunnerError(f"{name} must be a non-empty string")
    if not isinstance(schedule, FoldSchedule):
        raise TypeError("schedule must be FoldSchedule")
    if not isinstance(fold_results, tuple) or any(
        not isinstance(item, fold_result_type) for item in fold_results
    ):
        raise TypeError(f"fold_results must be a tuple of {fold_result_type.__name__}")
    if len(fold_results) != len(schedule.folds):
        raise WalkForwardRunnerError(
            "fold result count must match the schedule fold count"
        )
    if tuple(item.fold for item in fold_results) != schedule.folds:
        raise WalkForwardRunnerError(
            "fold result IDs and order must match the schedule"
        )
    for item in fold_results:
        if (
            item.validation_result.provider != provider
            or item.validation_result.provider_price_basis != provider_price_basis
        ):
            raise WalkForwardRunnerError(
                "all fold results must share the run provider contract"
            )


def _validate_runner_bars(bars: object) -> None:
    if not isinstance(bars, pd.DataFrame):
        raise TypeError("bars must be a pandas DataFrame")
    missing = sorted(set(CANONICAL_COLUMNS).difference(bars.columns))
    if missing:
        raise CanonicalDataError("Missing canonical columns: " + ", ".join(missing))
    if not is_datetime64_any_dtype(bars["date"].dtype):
        raise CanonicalDataError("canonical date must have a pandas datetime dtype")
    if bars["date"].isna().any():
        raise CanonicalDataError("canonical date contains invalid values")
