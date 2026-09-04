"""Causal month-end Range Score bin evaluation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from data import (
    DIVIDEND_POLICY,
    canonical_to_phase1,
    require_single_provider,
    validate_backtest_price_contract,
)

from .range_detector import RangeDetector
from .range_score import RangeScorer

SCORE_BINS: tuple[float, ...] = (0.0, 40.0, 60.0, 80.0, 100.0000001)
SCORE_LABELS: tuple[str, ...] = ("0-40", "40-60", "60-80", "80-100")
OBSERVATION_COLUMNS: tuple[str, ...] = (
    "provider",
    "symbol",
    "evaluation_date",
    "range_score",
    "score_bin",
    "forward_sessions",
    "forward_return",
    "mean_reversion_target",
    "mean_reversion_target_hit",
    "win",
    "maximum_adverse_excursion",
    "maximum_favorable_excursion",
    "maximum_drawdown",
    "dividend_policy",
    "universe_bias",
)
SUMMARY_COLUMNS: tuple[str, ...] = (
    "score_bin",
    "symbol_count",
    "observation_count",
    "mean_forward_return",
    "median_forward_return",
    "mean_reversion_target_hit_rate",
    "win_rate",
    "mean_maximum_adverse_excursion",
    "mean_maximum_favorable_excursion",
    "maximum_drawdown",
    "profit_factor",
    "forward_return_standard_error",
    "forward_return_ci95_lower",
    "forward_return_ci95_upper",
)


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
    """Evaluate fixed bins without exposing any future row to score generation.

    Forward returns and excursions use raw closes with explicit split-share
    adjustment and exclude cash dividends. The fixed SMA visible on the
    evaluation date is the mean-reversion target.
    """

    if isinstance(forward_sessions, bool) or not isinstance(forward_sessions, int):
        raise TypeError("forward_sessions must be an integer")
    if forward_sessions <= 0:
        raise ValueError("forward_sessions must be positive")
    provider = require_single_provider(bars)
    records: list[dict[str, object]] = []
    for symbol in sorted(set(bars["symbol"].astype(str))):
        symbol_bars = bars.loc[bars["symbol"].astype(str) == symbol].copy()
        phase1 = canonical_to_phase1(symbol_bars, symbol=symbol)
        validate_backtest_price_contract(phase1)
        month_end_positions = (
            phase1.reset_index(drop=True)
            .groupby(phase1["date"].dt.to_period("M").to_numpy(), sort=True)
            .tail(1)
            .index
        )
        for position in month_end_positions:
            forward_position = int(position) + forward_sessions
            if forward_position >= len(phase1):
                continue

            # Recompute on the prefix so no future row can reach either stage.
            available = phase1.iloc[: int(position) + 1].copy()
            row = scorer.transform(detector.transform(available)).iloc[-1]
            if pd.isna(row["range_score"]) or pd.isna(row["sma"]):
                continue
            future = phase1.iloc[int(position) + 1 : forward_position + 1]
            path_returns = _split_adjusted_path_returns(
                current_close=float(phase1.iloc[int(position)]["execution_close"]),
                future=future,
            )
            forward_return = float(path_returns.iloc[-1])
            current_signal_close = float(row["close"])
            target = float(row["sma"])
            target_hit = _target_was_hit(
                current_signal_close,
                target,
                future["signal_close"].astype(float),
            )
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
                    "forward_return": forward_return,
                    "mean_reversion_target": target,
                    "mean_reversion_target_hit": target_hit,
                    "win": forward_return > 0.0,
                    "maximum_adverse_excursion": min(0.0, float(path_returns.min())),
                    "maximum_favorable_excursion": max(0.0, float(path_returns.max())),
                    "maximum_drawdown": _maximum_drawdown(path_returns),
                    "dividend_policy": DIVIDEND_POLICY,
                    "universe_bias": (
                        "current/supplied universe; survivorship bias possible"
                    ),
                }
            )
    observations = pd.DataFrame.from_records(records, columns=OBSERVATION_COLUMNS)
    if observations.empty:
        return ScoreEvaluationResult(
            observations,
            pd.DataFrame(columns=SUMMARY_COLUMNS),
        )
    observations["score_bin"] = pd.Categorical(
        observations["score_bin"], categories=SCORE_LABELS, ordered=True
    )
    return ScoreEvaluationResult(observations, _summarize(observations))


def _split_adjusted_path_returns(
    *, current_close: float, future: pd.DataFrame
) -> pd.Series:
    split_multiplier = future["split_ratio"].astype(float).cumprod()
    future_value = future["execution_close"].astype(float) * split_multiplier
    return future_value / current_close - 1.0


def _target_was_hit(
    current_close: float, target: float, future_closes: pd.Series
) -> bool:
    if current_close < target:
        return bool((future_closes >= target).any())
    if current_close > target:
        return bool((future_closes <= target).any())
    return True


def _maximum_drawdown(path_returns: pd.Series) -> float:
    wealth = np.concatenate(([1.0], 1.0 + path_returns.to_numpy(dtype=float)))
    peaks = np.maximum.accumulate(wealth)
    return float(np.min(wealth / peaks - 1.0))


def _summarize(observations: pd.DataFrame) -> pd.DataFrame:
    grouped = observations.groupby("score_bin", observed=False)
    summary = grouped.agg(
        symbol_count=("symbol", "nunique"),
        observation_count=("forward_return", "count"),
        mean_forward_return=("forward_return", "mean"),
        median_forward_return=("forward_return", "median"),
        mean_reversion_target_hit_rate=("mean_reversion_target_hit", "mean"),
        win_rate=("win", "mean"),
        mean_maximum_adverse_excursion=("maximum_adverse_excursion", "mean"),
        mean_maximum_favorable_excursion=("maximum_favorable_excursion", "mean"),
        maximum_drawdown=("maximum_drawdown", "min"),
    ).reset_index()
    standard_deviation = grouped["forward_return"].std(ddof=1).reset_index(drop=True)
    count = summary["observation_count"].astype(float)
    standard_error = standard_deviation / np.sqrt(count.where(count > 0.0))
    # This CLI evaluates observations, not a trading rule, so PF is N/A.
    summary["profit_factor"] = np.nan
    summary["forward_return_standard_error"] = standard_error
    summary["forward_return_ci95_lower"] = (
        summary["mean_forward_return"] - 1.96 * standard_error
    )
    summary["forward_return_ci95_upper"] = (
        summary["mean_forward_return"] + 1.96 * standard_error
    )
    return summary.loc[:, SUMMARY_COLUMNS]
