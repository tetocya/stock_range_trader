"""Phase 3 walk-forward validation contracts."""

from .candidates import (
    ExecutableCandidateCatalog,
    ExecutableCandidateDefinition,
    SignalCandidateCatalog,
    SignalCandidateDefinition,
)
from .capabilities import (
    AnalysisMode,
    ProviderCapability,
    ProviderCapabilityError,
    ProviderCapabilityRegistry,
)
from .executable_evaluation import (
    INSUFFICIENT_FEATURE_HISTORY_REASON as EXECUTABLE_INSUFFICIENT_FEATURE_HISTORY_REASON,
)
from .executable_evaluation import (
    NO_VALIDATION_OBSERVATIONS_REASON,
    ExecutableEvaluationError,
    ExecutableOutcomeEvaluationResult,
    ExecutableOutcomeEvaluator,
    ExecutableSymbolExclusion,
    ExecutableSymbolOutcome,
)
from .executable_evaluation import (
    UNSUPPORTED_CORPORATE_ACTION_REASON as EXECUTABLE_UNSUPPORTED_CORPORATE_ACTION_REASON,
)
from .folds import (
    FoldObservationBounds,
    FoldSchedule,
    FoldValidationError,
    ForwardObservation,
    InsufficientFoldsError,
    PurgePolicy,
    WalkForwardFold,
    generate_fold_schedule,
    resolve_fold_observation_bounds,
)
from .selection import (
    CandidateAssessment,
    CandidateSelection,
    ExecutableCandidateSelector,
    ExecutableValidationScore,
    SelectionInputError,
    SelectionStatus,
    SignalCandidateSelector,
    SignalValidationScore,
)
from .signal_evaluation import (
    INSUFFICIENT_FEATURE_HISTORY_REASON,
    OVERLAPPING_FORWARD_WINDOW_REASON,
    SIGNAL_OUTCOME_DIVIDEND_POLICY,
    SIGNAL_OUTCOME_FORWARD_RETURN_MODE,
    UNSUPPORTED_CORPORATE_ACTION_REASON,
    SignalEvaluationError,
    SignalObservationExclusion,
    SignalOutcomeEvaluationResult,
    SignalOutcomeEvaluator,
    SignalOutcomeObservation,
    SignalSymbolExclusion,
)

__all__ = [
    "AnalysisMode",
    "CandidateAssessment",
    "CandidateSelection",
    "ExecutableCandidateCatalog",
    "ExecutableCandidateDefinition",
    "ExecutableCandidateSelector",
    "ExecutableEvaluationError",
    "EXECUTABLE_INSUFFICIENT_FEATURE_HISTORY_REASON",
    "EXECUTABLE_UNSUPPORTED_CORPORATE_ACTION_REASON",
    "ExecutableOutcomeEvaluationResult",
    "ExecutableOutcomeEvaluator",
    "ExecutableSymbolExclusion",
    "ExecutableSymbolOutcome",
    "ExecutableValidationScore",
    "FoldObservationBounds",
    "FoldSchedule",
    "FoldValidationError",
    "ForwardObservation",
    "InsufficientFoldsError",
    "INSUFFICIENT_FEATURE_HISTORY_REASON",
    "OVERLAPPING_FORWARD_WINDOW_REASON",
    "NO_VALIDATION_OBSERVATIONS_REASON",
    "ProviderCapability",
    "ProviderCapabilityError",
    "ProviderCapabilityRegistry",
    "PurgePolicy",
    "SelectionInputError",
    "SelectionStatus",
    "SIGNAL_OUTCOME_DIVIDEND_POLICY",
    "SIGNAL_OUTCOME_FORWARD_RETURN_MODE",
    "SignalCandidateCatalog",
    "SignalCandidateDefinition",
    "SignalCandidateSelector",
    "SignalEvaluationError",
    "SignalObservationExclusion",
    "SignalOutcomeEvaluationResult",
    "SignalOutcomeEvaluator",
    "SignalOutcomeObservation",
    "SignalSymbolExclusion",
    "SignalValidationScore",
    "UNSUPPORTED_CORPORATE_ACTION_REASON",
    "WalkForwardFold",
    "generate_fold_schedule",
    "resolve_fold_observation_bounds",
]
