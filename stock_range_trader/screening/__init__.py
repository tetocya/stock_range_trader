"""Range-market detection and scoring."""

from .batch_screener import BatchScreener, ScreeningResult
from .range_detector import (
    RangeDetector,
    mean_crossing_count,
    normalized_rolling_slope,
)
from .range_score import RangeScorer, RangeScoreWeights
from .score_evaluation import (
    SCORE_LABELS,
    ScoreEvaluationResult,
    evaluate_range_score_history,
)

__all__ = [
    "BatchScreener",
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
