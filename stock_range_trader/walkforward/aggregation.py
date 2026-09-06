"""Read-only Test-only aggregation for completed walk-forward runs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import numpy as np

from .capabilities import AnalysisMode
from .result import TestEvaluationStatus
from .runner import ExecutableWalkForwardRunResult, SignalWalkForwardRunResult


class WalkForwardAggregationError(ValueError):
    """Raised when an in-memory run result cannot be aggregated safely."""


class WalkForwardAggregateStatus(str, Enum):
    """Stable completion vocabulary for Test coverage across folds."""

    COMPLETED_ALL_FOLDS_EVALUATED = "completed_all_folds_evaluated"
    COMPLETED_PARTIAL_TEST_COVERAGE = "completed_partial_test_coverage"
    COMPLETED_NO_TEST_FOLDS = "completed_no_test_folds"


@dataclass(frozen=True, slots=True)
class CandidateSelectionFrequency:
    """How often one configured candidate was selected across all folds."""

    candidate_id: str
    selected_fold_count: int
    selected_fraction_of_all_folds: float
    selected_fraction_of_evaluated_folds: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise WalkForwardAggregationError("candidate_id must not be empty")
        _non_negative_int("selected_fold_count", self.selected_fold_count)
        _rate(
            "selected_fraction_of_all_folds",
            self.selected_fraction_of_all_folds,
        )
        if self.selected_fraction_of_evaluated_folds is not None:
            _rate(
                "selected_fraction_of_evaluated_folds",
                self.selected_fraction_of_evaluated_folds,
            )


@dataclass(frozen=True, slots=True)
class SignalWalkForwardAggregate:
    """Pooled Test-observation distribution for Signal Validation mode."""

    analysis_mode: AnalysisMode
    fold_count: int
    evaluated_fold_count: int
    no_eligible_fold_count: int
    selected_candidate_counts: tuple[CandidateSelectionFrequency, ...]
    aggregate_status: WalkForwardAggregateStatus
    test_observation_count: int
    unique_test_symbol_count: int
    mean_reversion_target_hit_rate: float | None
    median_forward_return: float | None
    median_mae_magnitude: float | None

    def __post_init__(self) -> None:
        if self.analysis_mode is not AnalysisMode.SIGNAL_VALIDATION:
            raise WalkForwardAggregationError("Signal aggregate mode is invalid")
        _validate_common(self)
        _non_negative_int("test_observation_count", self.test_observation_count)
        _non_negative_int("unique_test_symbol_count", self.unique_test_symbol_count)
        values = (
            self.mean_reversion_target_hit_rate,
            self.median_forward_return,
            self.median_mae_magnitude,
        )
        if self.test_observation_count == 0:
            if self.unique_test_symbol_count != 0 or any(
                value is not None for value in values
            ):
                raise WalkForwardAggregationError(
                    "empty Signal aggregate requires zero symbols and None metrics"
                )
        else:
            if self.unique_test_symbol_count <= 0:
                raise WalkForwardAggregationError(
                    "non-empty Signal aggregate requires Test symbols"
                )
            _rate(
                "mean_reversion_target_hit_rate",
                self.mean_reversion_target_hit_rate,
            )
            _finite("median_forward_return", self.median_forward_return)
            _non_negative_finite("median_mae_magnitude", self.median_mae_magnitude)


@dataclass(frozen=True, slots=True)
class ExecutableWalkForwardAggregate:
    """Distribution of independent Test symbol-fold account outcomes."""

    analysis_mode: AnalysisMode
    fold_count: int
    evaluated_fold_count: int
    no_eligible_fold_count: int
    selected_candidate_counts: tuple[CandidateSelectionFrequency, ...]
    aggregate_status: WalkForwardAggregateStatus
    requested_symbol_fold_count: int
    admitted_symbol_fold_count: int
    traded_symbol_fold_count: int
    total_trade_count: int
    finite_sharpe_symbol_fold_count: int
    median_symbol_fold_sharpe_ratio: float | None
    median_symbol_fold_maximum_drawdown_magnitude: float | None
    worst_symbol_fold_maximum_drawdown_magnitude: float | None
    median_symbol_fold_net_return: float | None

    def __post_init__(self) -> None:
        if self.analysis_mode is not AnalysisMode.EXECUTABLE_VALIDATION:
            raise WalkForwardAggregationError("Executable aggregate mode is invalid")
        _validate_common(self)
        for name in (
            "requested_symbol_fold_count",
            "admitted_symbol_fold_count",
            "traded_symbol_fold_count",
            "total_trade_count",
            "finite_sharpe_symbol_fold_count",
        ):
            _non_negative_int(name, getattr(self, name))
        if not (
            self.finite_sharpe_symbol_fold_count
            <= self.admitted_symbol_fold_count
            <= self.requested_symbol_fold_count
        ):
            raise WalkForwardAggregationError(
                "Executable aggregate symbol-fold counts are inconsistent"
            )
        if self.traded_symbol_fold_count > self.admitted_symbol_fold_count:
            raise WalkForwardAggregationError(
                "traded symbol-fold count cannot exceed admitted count"
            )
        if self.total_trade_count < self.traded_symbol_fold_count:
            raise WalkForwardAggregationError(
                "trade count cannot be below traded symbol-fold count"
            )
        distributions = (
            self.median_symbol_fold_maximum_drawdown_magnitude,
            self.worst_symbol_fold_maximum_drawdown_magnitude,
            self.median_symbol_fold_net_return,
        )
        if self.admitted_symbol_fold_count == 0:
            if any(value is not None for value in distributions):
                raise WalkForwardAggregationError(
                    "zero admitted symbol-folds require None distributions"
                )
        else:
            _non_negative_finite(
                "median_symbol_fold_maximum_drawdown_magnitude",
                self.median_symbol_fold_maximum_drawdown_magnitude,
            )
            _non_negative_finite(
                "worst_symbol_fold_maximum_drawdown_magnitude",
                self.worst_symbol_fold_maximum_drawdown_magnitude,
            )
            _finite(
                "median_symbol_fold_net_return",
                self.median_symbol_fold_net_return,
            )
            if (
                self.worst_symbol_fold_maximum_drawdown_magnitude
                < self.median_symbol_fold_maximum_drawdown_magnitude
            ):
                raise WalkForwardAggregationError(
                    "worst symbol-fold drawdown cannot be below the median"
                )
        if self.finite_sharpe_symbol_fold_count == 0:
            if self.median_symbol_fold_sharpe_ratio is not None:
                raise WalkForwardAggregationError(
                    "zero finite Sharpe outcomes require a None median"
                )
        else:
            _finite(
                "median_symbol_fold_sharpe_ratio",
                self.median_symbol_fold_sharpe_ratio,
            )


class SignalWalkForwardAggregator:
    """Pool only selected-candidate Signal Test observations."""

    def aggregate(
        self,
        result: SignalWalkForwardRunResult,
    ) -> SignalWalkForwardAggregate:
        if not isinstance(result, SignalWalkForwardRunResult):
            raise TypeError("result must be SignalWalkForwardRunResult")
        observations = tuple(
            observation
            for fold_result in result.fold_results
            if fold_result.test_result is not None
            for observation in fold_result.test_result.observations
        )
        keys = tuple((item.symbol, item.feature_date) for item in observations)
        if len(keys) != len(set(keys)):
            raise WalkForwardAggregationError(
                "duplicate symbol/feature_date across Test folds"
            )
        if observations:
            hit_rate = float(
                np.mean([item.mean_reversion_target_hit for item in observations])
            )
            median_return = float(
                np.median([item.forward_return for item in observations])
            )
            median_mae = float(
                np.median(
                    [item.maximum_adverse_excursion_magnitude for item in observations]
                )
            )
        else:
            hit_rate = median_return = median_mae = None
        common = _common_aggregate(result)
        return SignalWalkForwardAggregate(
            analysis_mode=AnalysisMode.SIGNAL_VALIDATION,
            **common,
            test_observation_count=len(observations),
            unique_test_symbol_count=len({item.symbol for item in observations}),
            mean_reversion_target_hit_rate=hit_rate,
            median_forward_return=median_return,
            median_mae_magnitude=median_mae,
        )


class ExecutableWalkForwardAggregator:
    """Aggregate independent symbol-fold outcomes without portfolio synthesis."""

    def aggregate(
        self,
        result: ExecutableWalkForwardRunResult,
    ) -> ExecutableWalkForwardAggregate:
        if not isinstance(result, ExecutableWalkForwardRunResult):
            raise TypeError("result must be ExecutableWalkForwardRunResult")
        test_results = tuple(
            fold_result.test_result
            for fold_result in result.fold_results
            if fold_result.test_result is not None
        )
        outcomes = tuple(
            outcome
            for test_result in test_results
            for outcome in test_result.symbol_outcomes
        )
        keys = tuple((item.fold_id, item.symbol) for item in outcomes)
        if len(keys) != len(set(keys)):
            raise WalkForwardAggregationError(
                "duplicate symbol-fold Executable Test outcome"
            )
        sharpes = [
            item.sharpe_ratio for item in outcomes if item.sharpe_ratio is not None
        ]
        drawdowns = [item.maximum_drawdown_magnitude for item in outcomes]
        returns = [item.net_return for item in outcomes]
        common = _common_aggregate(result)
        return ExecutableWalkForwardAggregate(
            analysis_mode=AnalysisMode.EXECUTABLE_VALIDATION,
            **common,
            requested_symbol_fold_count=sum(
                item.requested_symbol_count for item in test_results
            ),
            admitted_symbol_fold_count=len(outcomes),
            traded_symbol_fold_count=sum(
                item.number_of_trades > 0 for item in outcomes
            ),
            total_trade_count=sum(item.number_of_trades for item in outcomes),
            finite_sharpe_symbol_fold_count=len(sharpes),
            median_symbol_fold_sharpe_ratio=(
                float(np.median(sharpes)) if sharpes else None
            ),
            median_symbol_fold_maximum_drawdown_magnitude=(
                float(np.median(drawdowns)) if drawdowns else None
            ),
            worst_symbol_fold_maximum_drawdown_magnitude=(
                float(np.max(drawdowns)) if drawdowns else None
            ),
            median_symbol_fold_net_return=(
                float(np.median(returns)) if returns else None
            ),
        )


def _common_aggregate(result: object) -> dict[str, object]:
    fold_results = result.fold_results
    fold_count = len(fold_results)
    evaluated = sum(
        item.test_status is TestEvaluationStatus.EVALUATED for item in fold_results
    )
    no_eligible = fold_count - evaluated
    if evaluated == fold_count:
        status = WalkForwardAggregateStatus.COMPLETED_ALL_FOLDS_EVALUATED
    elif evaluated == 0:
        status = WalkForwardAggregateStatus.COMPLETED_NO_TEST_FOLDS
    else:
        status = WalkForwardAggregateStatus.COMPLETED_PARTIAL_TEST_COVERAGE

    catalog_ids = tuple(
        score.candidate_id for score in fold_results[0].validation_result.scores
    )
    for fold_result in fold_results[1:]:
        observed = tuple(
            score.candidate_id for score in fold_result.validation_result.scores
        )
        if observed != catalog_ids:
            raise WalkForwardAggregationError(
                "candidate catalog order must be stable across folds"
            )
    selected_ids = tuple(
        item.selection.selected_candidate_id
        for item in fold_results
        if item.selection.selected_candidate_id is not None
    )
    frequencies = tuple(
        CandidateSelectionFrequency(
            candidate_id=candidate_id,
            selected_fold_count=selected_ids.count(candidate_id),
            selected_fraction_of_all_folds=selected_ids.count(candidate_id)
            / fold_count,
            selected_fraction_of_evaluated_folds=(
                selected_ids.count(candidate_id) / evaluated if evaluated else None
            ),
        )
        for candidate_id in catalog_ids
    )
    return {
        "fold_count": fold_count,
        "evaluated_fold_count": evaluated,
        "no_eligible_fold_count": no_eligible,
        "selected_candidate_counts": frequencies,
        "aggregate_status": status,
    }


def _validate_common(value: object) -> None:
    for name in ("fold_count", "evaluated_fold_count", "no_eligible_fold_count"):
        _non_negative_int(name, getattr(value, name))
    if value.fold_count <= 0:
        raise WalkForwardAggregationError("fold_count must be positive")
    if value.evaluated_fold_count + value.no_eligible_fold_count != value.fold_count:
        raise WalkForwardAggregationError("fold coverage counts are inconsistent")
    if not isinstance(value.aggregate_status, WalkForwardAggregateStatus):
        raise TypeError("aggregate_status must be WalkForwardAggregateStatus")
    if not isinstance(value.selected_candidate_counts, tuple) or any(
        not isinstance(item, CandidateSelectionFrequency)
        for item in value.selected_candidate_counts
    ):
        raise TypeError(
            "selected_candidate_counts must be CandidateSelectionFrequency tuple"
        )
    ids = tuple(item.candidate_id for item in value.selected_candidate_counts)
    if len(ids) != len(set(ids)):
        raise WalkForwardAggregationError("candidate frequency IDs must be unique")
    if sum(item.selected_fold_count for item in value.selected_candidate_counts) != (
        value.evaluated_fold_count
    ):
        raise WalkForwardAggregationError(
            "candidate selection counts must equal evaluated fold count"
        )


def _non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WalkForwardAggregationError(f"{name} must be a non-negative integer")


def _finite(name: str, value: object) -> None:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float, np.number))
        or not math.isfinite(float(value))
    ):
        raise WalkForwardAggregationError(f"{name} must be finite")


def _non_negative_finite(name: str, value: object) -> None:
    _finite(name, value)
    if float(value) < 0.0:
        raise WalkForwardAggregationError(f"{name} must be non-negative")


def _rate(name: str, value: object) -> None:
    _finite(name, value)
    if not 0.0 <= float(value) <= 1.0:
        raise WalkForwardAggregationError(f"{name} must be in [0, 1]")
