"""Causal month-end Range Score bin evaluation."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from data import canonical_to_phase1, require_single_provider

from .range_detector import RangeDetector
from .range_score import RangeScorer

SCORE_BINS: tuple[float, ...] = (0.0, 40.0, 60.0, 80.0, 100.0000001)
SCORE_LABELS: tuple[str, ...] = ("0-40", "40-60", "60-80", "80-100")


@dataclass(frozen=True, slots=True)
class ScoreEvaluationResult:
    """Point-in-time observations and aggregate forward outcomes."""

    observations: pd.DataFrame
    summary: pd.DataFrame


def evaluate_range_score_history(
    bars: pd.DataFrame,
    detector: RangeDetector,
    scorer: RangeScorer,
    *,
    forward_sessions: int = 20,
) -> ScoreEvaluationResult:
    """Score each observed month-end using only information available then."""

    if forward_sessions <= 0:
        raise ValueError("forward_sessions must be positive")
    provider = require_single_provider(bars)
    records: list[dict[str, object]] = []
    for symbol in sorted(set(bars["symbol"].astype(str))):
        symbol_bars = bars.loc[bars["symbol"].astype(str) == symbol].copy()
        phase1 = canonical_to_phase1(symbol_bars, symbol=symbol)
        scored = scorer.transform(detector.transform(phase1))
        month_end_positions = (
            scored.groupby(scored["date"].dt.to_period("M"), sort=True).tail(1).index
        )
        for position in month_end_positions:
            integer_position = scored.index.get_loc(position)
            if not isinstance(integer_position, int):
                continue
            forward_position = integer_position + forward_sessions
            row = scored.iloc[integer_position]
            if forward_position >= len(scored) or pd.isna(row["range_score"]):
                continue
            current_close = float(row["close"])
            future = scored.iloc[integer_position + 1 : forward_position + 1]
            future_close = float(scored.iloc[forward_position]["close"])
            score_bin = pd.cut(
                pd.Series([float(row["range_score"])]),
                bins=SCORE_BINS,
                labels=SCORE_LABELS,
                right=False,
                include_lowest=True,
            ).iloc[0]
            records.append(
                {
                    "provider": provider,
                    "symbol": symbol,
                    "evaluation_date": row["date"],
                    "range_score": float(row["range_score"]),
                    "score_bin": str(score_bin),
                    "forward_sessions": forward_sessions,
                    "forward_return": future_close / current_close - 1.0,
                    "forward_min_return": (
                        float(future["close"].min()) / current_close - 1.0
                    ),
                    "forward_max_return": (
                        float(future["close"].max()) / current_close - 1.0
                    ),
                    "universe_bias": "current/supplied universe; survivorship bias possible",
                }
            )
    observations = pd.DataFrame.from_records(records)
    if observations.empty:
        return ScoreEvaluationResult(observations, pd.DataFrame())
    observations["score_bin"] = pd.Categorical(
        observations["score_bin"], categories=SCORE_LABELS, ordered=True
    )
    summary = (
        observations.groupby("score_bin", observed=False)
        .agg(
            observation_count=("forward_return", "count"),
            mean_forward_return=("forward_return", "mean"),
            median_forward_return=("forward_return", "median"),
            mean_forward_min_return=("forward_min_return", "mean"),
            mean_forward_max_return=("forward_max_return", "mean"),
        )
        .reset_index()
    )
    return ScoreEvaluationResult(observations, summary)
