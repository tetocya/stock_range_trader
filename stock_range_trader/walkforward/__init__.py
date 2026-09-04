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

__all__ = [
    "AnalysisMode",
    "CandidateAssessment",
    "CandidateSelection",
    "ExecutableCandidateCatalog",
    "ExecutableCandidateDefinition",
    "ExecutableCandidateSelector",
    "ExecutableValidationScore",
    "FoldObservationBounds",
    "FoldSchedule",
    "FoldValidationError",
    "ForwardObservation",
    "InsufficientFoldsError",
    "ProviderCapability",
    "ProviderCapabilityError",
    "ProviderCapabilityRegistry",
    "PurgePolicy",
    "SelectionInputError",
    "SelectionStatus",
    "SignalCandidateCatalog",
    "SignalCandidateDefinition",
    "SignalCandidateSelector",
    "SignalValidationScore",
    "WalkForwardFold",
    "generate_fold_schedule",
    "resolve_fold_observation_bounds",
]
