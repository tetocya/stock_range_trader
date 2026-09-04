"""Pure validation-only candidate selection for Phase 3."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum

from config.phase3 import ExecutableSelectionPolicy, SignalSelectionPolicy

from .candidates import ExecutableCandidateCatalog, SignalCandidateCatalog
from .capabilities import AnalysisMode

SIGNAL_REJECTION_REASONS: tuple[str, ...] = (
    "insufficient_observation_count",
    "invalid_primary_metric",
    "invalid_median_forward_return",
    "invalid_median_mae_magnitude",
)

EXECUTABLE_REJECTION_REASONS: tuple[str, ...] = (
    "insufficient_traded_symbol_count",
    "insufficient_trading_symbol_ratio",
    "insufficient_total_trade_count",
    "insufficient_finite_sharpe_count",
    "maximum_drawdown_limit_exceeded",
    "invalid_primary_metric",
    "invalid_median_drawdown_magnitude",
    "invalid_median_net_return",
)

_CANDIDATE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class SelectionInputError(ValueError):
    """Raised when score inputs are inconsistent or structurally incomplete."""


@dataclass(frozen=True, slots=True)
class SignalValidationScore:
    """Validation-only aggregate metrics for one Signal candidate."""

    candidate_id: str
    observation_count: int
    mean_reversion_target_hit_rate: float | None
    median_forward_return: float | None
    median_mae_magnitude: float | None

    def __post_init__(self) -> None:
        _require_candidate_id(self.candidate_id)
        _require_non_negative_int("observation_count", self.observation_count)
        if self.observation_count == 0:
            if any(
                value is not None
                for value in (
                    self.mean_reversion_target_hit_rate,
                    self.median_forward_return,
                    self.median_mae_magnitude,
                )
            ):
                raise SelectionInputError(
                    "zero-observation Signal scores must use None aggregate metrics"
                )
            return
        _require_rate(
            "mean_reversion_target_hit_rate",
            self.mean_reversion_target_hit_rate,
        )
        _require_finite(
            "median_forward_return",
            self.median_forward_return,
        )
        _require_non_negative_finite(
            "median_mae_magnitude",
            self.median_mae_magnitude,
        )


@dataclass(frozen=True, slots=True)
class ExecutableValidationScore:
    """Validation-only aggregate metrics for one Executable candidate."""

    candidate_id: str
    admitted_symbol_count: int
    traded_symbol_count: int
    total_trade_count: int
    finite_sharpe_count: int
    median_symbol_sharpe_ratio: float | None
    median_symbol_maximum_drawdown_magnitude: float | None
    worst_symbol_maximum_drawdown_magnitude: float | None
    median_symbol_net_return: float | None

    def __post_init__(self) -> None:
        _require_candidate_id(self.candidate_id)
        for name in (
            "admitted_symbol_count",
            "traded_symbol_count",
            "total_trade_count",
            "finite_sharpe_count",
        ):
            _require_non_negative_int(name, getattr(self, name))
        if self.traded_symbol_count > self.admitted_symbol_count:
            raise SelectionInputError(
                "traded_symbol_count cannot exceed admitted_symbol_count"
            )
        if self.finite_sharpe_count > self.admitted_symbol_count:
            raise SelectionInputError(
                "finite_sharpe_count cannot exceed admitted_symbol_count"
            )
        if self.total_trade_count < self.traded_symbol_count:
            raise SelectionInputError(
                "total_trade_count cannot be less than traded_symbol_count"
            )

        if self.finite_sharpe_count == 0:
            if self.median_symbol_sharpe_ratio is not None:
                raise SelectionInputError(
                    "median_symbol_sharpe_ratio must be None when "
                    "finite_sharpe_count is zero"
                )
        else:
            _require_finite(
                "median_symbol_sharpe_ratio",
                self.median_symbol_sharpe_ratio,
            )

        distribution_metrics = (
            self.median_symbol_maximum_drawdown_magnitude,
            self.worst_symbol_maximum_drawdown_magnitude,
            self.median_symbol_net_return,
        )
        if self.admitted_symbol_count == 0:
            if any(value is not None for value in distribution_metrics):
                raise SelectionInputError(
                    "zero-admission Executable scores must use None return and "
                    "drawdown metrics"
                )
            return

        _require_non_negative_finite(
            "median_symbol_maximum_drawdown_magnitude",
            self.median_symbol_maximum_drawdown_magnitude,
        )
        _require_non_negative_finite(
            "worst_symbol_maximum_drawdown_magnitude",
            self.worst_symbol_maximum_drawdown_magnitude,
        )
        _require_finite(
            "median_symbol_net_return",
            self.median_symbol_net_return,
        )
        if (
            self.worst_symbol_maximum_drawdown_magnitude
            < self.median_symbol_maximum_drawdown_magnitude
        ):
            raise SelectionInputError(
                "worst drawdown magnitude cannot be less than median drawdown magnitude"
            )

    @property
    def trading_symbol_ratio(self) -> float:
        """Use the candidate-independent admitted population as denominator."""

        if self.admitted_symbol_count == 0:
            return 0.0
        return self.traded_symbol_count / self.admitted_symbol_count


class SelectionStatus(str, Enum):
    """Stable machine-readable selection status."""

    SELECTED = "selected"
    NO_ELIGIBLE_CANDIDATE = "no_eligible_candidate"


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    """Eligibility reasons and optional rank for one candidate."""

    candidate_id: str
    eligible: bool
    rejection_reasons: tuple[str, ...]
    rank: int | None

    def __post_init__(self) -> None:
        _require_candidate_id(self.candidate_id)
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be a boolean")
        if not isinstance(self.rejection_reasons, tuple) or any(
            not isinstance(reason, str) or not reason
            for reason in self.rejection_reasons
        ):
            raise ValueError("rejection_reasons must be a tuple of non-empty strings")
        if len(self.rejection_reasons) != len(set(self.rejection_reasons)):
            raise ValueError("rejection_reasons must not contain duplicates")
        if self.eligible:
            if self.rejection_reasons:
                raise ValueError("eligible candidates cannot have rejection reasons")
            _require_positive_int("rank", self.rank)
        else:
            if not self.rejection_reasons:
                raise ValueError("ineligible candidates require rejection reasons")
            if self.rank is not None:
                raise ValueError("ineligible candidates cannot have a rank")


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    """Deterministic, auditable result of one mode-specific selection."""

    analysis_mode: AnalysisMode
    status: SelectionStatus
    selected_candidate_id: str | None
    ranked_candidate_ids: tuple[str, ...]
    assessments: tuple[CandidateAssessment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.analysis_mode, AnalysisMode):
            raise TypeError("analysis_mode must be AnalysisMode")
        if not isinstance(self.status, SelectionStatus):
            raise TypeError("status must be SelectionStatus")
        if not isinstance(self.ranked_candidate_ids, tuple):
            raise TypeError("ranked_candidate_ids must be a tuple")
        for candidate_id in self.ranked_candidate_ids:
            _require_candidate_id(candidate_id)
        if len(self.ranked_candidate_ids) != len(set(self.ranked_candidate_ids)):
            raise ValueError("ranked_candidate_ids must be unique")
        if not isinstance(self.assessments, tuple) or any(
            not isinstance(assessment, CandidateAssessment)
            for assessment in self.assessments
        ):
            raise TypeError("assessments must be a tuple of CandidateAssessment")
        assessment_ids = tuple(
            assessment.candidate_id for assessment in self.assessments
        )
        if assessment_ids != tuple(sorted(assessment_ids)):
            raise ValueError("assessments must be sorted by candidate_id")
        if len(assessment_ids) != len(set(assessment_ids)):
            raise ValueError("assessment candidate IDs must be unique")

        reason_order = (
            SIGNAL_REJECTION_REASONS
            if self.analysis_mode is AnalysisMode.SIGNAL_VALIDATION
            else EXECUTABLE_REJECTION_REASONS
        )
        for assessment in self.assessments:
            expected_reasons = tuple(
                reason
                for reason in reason_order
                if reason in assessment.rejection_reasons
            )
            if assessment.rejection_reasons != expected_reasons:
                raise ValueError(
                    "rejection reasons must use the stable mode-specific order"
                )
            unknown_reasons = set(assessment.rejection_reasons) - set(reason_order)
            if unknown_reasons:
                raise ValueError("unknown rejection reason code")

        eligible_by_rank = tuple(
            assessment.candidate_id
            for assessment in sorted(
                (item for item in self.assessments if item.eligible),
                key=lambda item: item.rank,
            )
        )
        if eligible_by_rank != self.ranked_candidate_ids:
            raise ValueError("assessment ranks must match ranked_candidate_ids")
        expected_ranks = tuple(range(1, len(eligible_by_rank) + 1))
        actual_ranks = tuple(
            assessment.rank
            for assessment in sorted(
                (item for item in self.assessments if item.eligible),
                key=lambda item: item.rank,
            )
        )
        if actual_ranks != expected_ranks:
            raise ValueError("eligible candidate ranks must be consecutive from one")

        if self.status is SelectionStatus.SELECTED:
            if not self.ranked_candidate_ids:
                raise ValueError("selected status requires an eligible candidate")
            if self.selected_candidate_id != self.ranked_candidate_ids[0]:
                raise ValueError("selected_candidate_id must equal rank one")
        else:
            if self.selected_candidate_id is not None:
                raise ValueError(
                    "no_eligible_candidate status requires selected_candidate_id None"
                )
            if self.ranked_candidate_ids:
                raise ValueError(
                    "no_eligible_candidate status cannot contain ranked candidates"
                )


@dataclass(frozen=True, slots=True)
class SignalCandidateSelector:
    """Select one Signal candidate using Validation aggregates only."""

    policy: SignalSelectionPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.policy, SignalSelectionPolicy):
            raise SelectionInputError("policy must be SignalSelectionPolicy")

    def select(
        self,
        catalog: SignalCandidateCatalog,
        scores: tuple[SignalValidationScore, ...],
    ) -> CandidateSelection:
        """Apply fixed Signal eligibility and deterministic ranking rules."""

        if not isinstance(catalog, SignalCandidateCatalog):
            raise SelectionInputError("catalog must be SignalCandidateCatalog")
        score_by_id = _validate_scores(
            catalog.candidate_ids,
            scores,
            SignalValidationScore,
        )
        reasons_by_id: dict[str, tuple[str, ...]] = {}
        eligible_scores: list[SignalValidationScore] = []
        for candidate_id in catalog.candidate_ids:
            score = score_by_id[candidate_id]
            reasons: list[str] = []
            if score.observation_count < self.policy.minimum_observation_count:
                reasons.append("insufficient_observation_count")
            if score.mean_reversion_target_hit_rate is None:
                reasons.append("invalid_primary_metric")
            if score.median_forward_return is None:
                reasons.append("invalid_median_forward_return")
            if score.median_mae_magnitude is None:
                reasons.append("invalid_median_mae_magnitude")
            reasons_by_id[candidate_id] = tuple(reasons)
            if not reasons:
                eligible_scores.append(score)

        ranked = sorted(
            eligible_scores,
            key=lambda score: (
                -_present(score.mean_reversion_target_hit_rate),
                -_present(score.median_forward_return),
                _present(score.median_mae_magnitude),
                score.candidate_id,
            ),
        )
        return _build_selection(
            AnalysisMode.SIGNAL_VALIDATION,
            catalog.candidate_ids,
            reasons_by_id,
            tuple(score.candidate_id for score in ranked),
        )


@dataclass(frozen=True, slots=True)
class ExecutableCandidateSelector:
    """Select one Executable candidate using Validation aggregates only."""

    policy: ExecutableSelectionPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ExecutableSelectionPolicy):
            raise SelectionInputError("policy must be ExecutableSelectionPolicy")

    def select(
        self,
        catalog: ExecutableCandidateCatalog,
        scores: tuple[ExecutableValidationScore, ...],
    ) -> CandidateSelection:
        """Apply fixed Executable eligibility and deterministic ranking rules."""

        if not isinstance(catalog, ExecutableCandidateCatalog):
            raise SelectionInputError("catalog must be ExecutableCandidateCatalog")
        score_by_id = _validate_scores(
            catalog.candidate_ids,
            scores,
            ExecutableValidationScore,
        )
        admitted_counts = {
            score.admitted_symbol_count for score in score_by_id.values()
        }
        if len(admitted_counts) != 1:
            raise SelectionInputError(
                "admitted_symbol_count must be identical for every candidate"
            )

        reasons_by_id: dict[str, tuple[str, ...]] = {}
        eligible_scores: list[ExecutableValidationScore] = []
        for candidate_id in catalog.candidate_ids:
            score = score_by_id[candidate_id]
            reasons: list[str] = []
            if score.traded_symbol_count < self.policy.minimum_traded_symbol_count:
                reasons.append("insufficient_traded_symbol_count")
            if score.trading_symbol_ratio < self.policy.minimum_trading_symbol_ratio:
                reasons.append("insufficient_trading_symbol_ratio")
            if score.total_trade_count < self.policy.minimum_total_trade_count:
                reasons.append("insufficient_total_trade_count")
            if score.finite_sharpe_count < self.policy.minimum_finite_sharpe_count:
                reasons.append("insufficient_finite_sharpe_count")
            if (
                score.worst_symbol_maximum_drawdown_magnitude is not None
                and score.worst_symbol_maximum_drawdown_magnitude
                > self.policy.maximum_drawdown_limit
            ):
                reasons.append("maximum_drawdown_limit_exceeded")
            if score.median_symbol_sharpe_ratio is None:
                reasons.append("invalid_primary_metric")
            if score.median_symbol_maximum_drawdown_magnitude is None:
                reasons.append("invalid_median_drawdown_magnitude")
            if score.median_symbol_net_return is None:
                reasons.append("invalid_median_net_return")
            reasons_by_id[candidate_id] = tuple(reasons)
            if not reasons:
                eligible_scores.append(score)

        ranked = sorted(
            eligible_scores,
            key=lambda score: (
                -_present(score.median_symbol_sharpe_ratio),
                _present(score.median_symbol_maximum_drawdown_magnitude),
                -_present(score.median_symbol_net_return),
                score.candidate_id,
            ),
        )
        return _build_selection(
            AnalysisMode.EXECUTABLE_VALIDATION,
            catalog.candidate_ids,
            reasons_by_id,
            tuple(score.candidate_id for score in ranked),
        )


def _validate_scores(
    catalog_ids: tuple[str, ...],
    scores: object,
    score_type: type[SignalValidationScore] | type[ExecutableValidationScore],
) -> dict[str, SignalValidationScore] | dict[str, ExecutableValidationScore]:
    if not isinstance(scores, tuple) or any(
        not isinstance(score, score_type) for score in scores
    ):
        raise SelectionInputError(
            f"scores must be a tuple of {score_type.__name__} values"
        )
    score_ids = tuple(score.candidate_id for score in scores)
    if len(score_ids) != len(set(score_ids)):
        raise SelectionInputError("score candidate IDs must be unique")
    catalog_set = set(catalog_ids)
    score_set = set(score_ids)
    missing = tuple(sorted(catalog_set - score_set))
    unexpected = tuple(sorted(score_set - catalog_set))
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing scores: " + ", ".join(missing))
        if unexpected:
            details.append("undefined scores: " + ", ".join(unexpected))
        raise SelectionInputError("; ".join(details))
    return {score.candidate_id: score for score in scores}


def _build_selection(
    analysis_mode: AnalysisMode,
    catalog_ids: tuple[str, ...],
    reasons_by_id: dict[str, tuple[str, ...]],
    ranked_candidate_ids: tuple[str, ...],
) -> CandidateSelection:
    rank_by_id = {
        candidate_id: rank
        for rank, candidate_id in enumerate(ranked_candidate_ids, start=1)
    }
    assessments = tuple(
        CandidateAssessment(
            candidate_id=candidate_id,
            eligible=candidate_id in rank_by_id,
            rejection_reasons=reasons_by_id[candidate_id],
            rank=rank_by_id.get(candidate_id),
        )
        for candidate_id in sorted(catalog_ids)
    )
    if ranked_candidate_ids:
        status = SelectionStatus.SELECTED
        selected_candidate_id = ranked_candidate_ids[0]
    else:
        status = SelectionStatus.NO_ELIGIBLE_CANDIDATE
        selected_candidate_id = None
    return CandidateSelection(
        analysis_mode=analysis_mode,
        status=status,
        selected_candidate_id=selected_candidate_id,
        ranked_candidate_ids=ranked_candidate_ids,
        assessments=assessments,
    )


def _require_candidate_id(candidate_id: object) -> None:
    if not isinstance(candidate_id, str) or not _CANDIDATE_ID_PATTERN.fullmatch(
        candidate_id
    ):
        raise SelectionInputError("candidate_id has an invalid format")


def _require_non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SelectionInputError(f"{name} must be a non-negative integer")


def _require_positive_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_finite(name: str, value: object) -> None:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise SelectionInputError(f"{name} must be a finite number")


def _require_rate(name: str, value: object) -> None:
    _require_finite(name, value)
    if not 0.0 <= value <= 1.0:
        raise SelectionInputError(f"{name} must be in [0, 1]")


def _require_non_negative_finite(name: str, value: object) -> None:
    _require_finite(name, value)
    if value < 0.0:
        raise SelectionInputError(f"{name} must be non-negative")


def _present(value: float | None) -> float:
    if value is None:
        raise AssertionError("eligible candidate metric unexpectedly missing")
    return value
