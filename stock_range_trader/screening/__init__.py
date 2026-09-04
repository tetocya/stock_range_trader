"""Range-market detection and scoring."""

from .batch_screener import BatchScreener, ScreeningResult
from .range_detector import (
    RangeDetector,
    mean_crossing_count,
    normalized_rolling_slope,
)
from .range_score import RangeScorer, RangeScoreWeights
from .score_evaluation import (
    EVALUATION_EXCLUSION_COLUMNS,
    RANGE_SCORE_DIVIDEND_POLICY,
    RANGE_SCORE_FORWARD_RETURN_MODE,
    SCORE_LABELS,
    ScoreEvaluationResult,
    evaluate_range_score_history,
)

__all__ = [
    "BatchScreener",
    "EVALUATION_EXCLUSION_COLUMNS",
    "RANGE_SCORE_DIVIDEND_POLICY",
    "RANGE_SCORE_FORWARD_RETURN_MODE",
    "RangeDetector",
    "RangeScorer",
    "RangeScoreWeights",
    "ScreeningResult",
    "ScoreEvaluationResult",
    "SCORE_LABELS",
    "evaluate_range_score_history",
    "mean_crossing_count",
    "normalized_rolling_slope",
]
