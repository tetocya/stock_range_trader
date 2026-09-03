"""Range-market detection and scoring."""

from .range_detector import (
    RangeDetector,
    mean_crossing_count,
    normalized_rolling_slope,
)
from .range_score import RangeScorer, RangeScoreWeights

__all__ = [
    "RangeDetector",
    "RangeScorer",
    "RangeScoreWeights",
    "mean_crossing_count",
    "normalized_rolling_slope",
]
