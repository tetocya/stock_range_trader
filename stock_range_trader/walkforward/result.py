"""Typed contracts shared by one-time walk-forward Test evaluation."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum

import numpy as np

_CANDIDATE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class WalkForwardResultError(ValueError):
    """Raised when a Test or walk-forward result violates its contract."""


class TestEvaluationStatus(str, Enum):
    """Stable status for the one permitted Test evaluation per fold."""

    EVALUATED = "evaluated"
    NOT_RUN_NO_ELIGIBLE_CANDIDATE = "not_run_no_eligible_candidate"


@dataclass(frozen=True, slots=True)
class ValidationCohort:
    """Provider contract and sorted Validation-admitted symbols frozen for Test."""

    provider: str
    provider_price_basis: str
    symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty_string("provider", self.provider)
        _require_non_empty_string("provider_price_basis", self.provider_price_basis)
        if not isinstance(self.symbols, tuple):
            raise TypeError("symbols must be a tuple")
        if any(
            not isinstance(symbol, str) or not symbol.strip() for symbol in self.symbols
        ):
            raise WalkForwardResultError("symbols must contain non-empty strings")
        if self.symbols != tuple(sorted(self.symbols)):
            raise WalkForwardResultError("symbols must be sorted")
        if len(self.symbols) != len(set(self.symbols)):
            raise WalkForwardResultError("symbols must not contain duplicates")


@dataclass(frozen=True, slots=True)
class SignalTestSummary:
    """Test-only aggregate for one already-selected Signal candidate."""

    candidate_id: str
    observation_count: int
    mean_reversion_target_hit_rate: float | None
    median_forward_return: float | None
    median_mae_magnitude: float | None

    def __post_init__(self) -> None:
        _require_candidate_id(self.candidate_id)
        _require_non_negative_int("observation_count", self.observation_count)
        aggregates = (
            self.mean_reversion_target_hit_rate,
            self.median_forward_return,
            self.median_mae_magnitude,
        )
        if self.observation_count == 0:
            if any(value is not None for value in aggregates):
                raise WalkForwardResultError(
                    "zero-observation Signal Test summaries require None aggregates"
                )
            return
        _require_rate(
            "mean_reversion_target_hit_rate",
            self.mean_reversion_target_hit_rate,
        )
        _require_finite("median_forward_return", self.median_forward_return)
        _require_non_negative_finite("median_mae_magnitude", self.median_mae_magnitude)


@dataclass(frozen=True, slots=True)
class ExecutableTestSummary:
    """Test-only symbol distribution for one selected Executable candidate."""

    candidate_id: str
    requested_symbol_count: int
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
            "requested_symbol_count",
            "admitted_symbol_count",
            "traded_symbol_count",
            "total_trade_count",
            "finite_sharpe_count",
        ):
            _require_non_negative_int(name, getattr(self, name))
        if self.admitted_symbol_count > self.requested_symbol_count:
            raise WalkForwardResultError(
                "admitted_symbol_count cannot exceed requested_symbol_count"
            )
        if self.traded_symbol_count > self.admitted_symbol_count:
            raise WalkForwardResultError(
                "traded_symbol_count cannot exceed admitted_symbol_count"
            )
        if self.finite_sharpe_count > self.admitted_symbol_count:
            raise WalkForwardResultError(
                "finite_sharpe_count cannot exceed admitted_symbol_count"
            )
        if self.total_trade_count < self.traded_symbol_count:
            raise WalkForwardResultError(
                "total_trade_count cannot be less than traded_symbol_count"
            )
        if self.finite_sharpe_count == 0:
            if self.median_symbol_sharpe_ratio is not None:
                raise WalkForwardResultError(
                    "median_symbol_sharpe_ratio must be None without finite Sharpe"
                )
        else:
            _require_finite(
                "median_symbol_sharpe_ratio", self.median_symbol_sharpe_ratio
            )
        distribution = (
            self.median_symbol_maximum_drawdown_magnitude,
            self.worst_symbol_maximum_drawdown_magnitude,
            self.median_symbol_net_return,
        )
        if self.admitted_symbol_count == 0:
            if any(value is not None for value in distribution):
                raise WalkForwardResultError(
                    "zero-admission Executable Test summaries require None "
                    "distribution metrics"
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
        _require_finite("median_symbol_net_return", self.median_symbol_net_return)
        if (
            self.worst_symbol_maximum_drawdown_magnitude
            < self.median_symbol_maximum_drawdown_magnitude
        ):
            raise WalkForwardResultError(
                "worst drawdown magnitude cannot be below median drawdown magnitude"
            )


def _require_candidate_id(value: object) -> None:
    if not isinstance(value, str) or not _CANDIDATE_ID_PATTERN.fullmatch(value):
        raise WalkForwardResultError("candidate_id has an invalid format")


def _require_non_empty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WalkForwardResultError(f"{name} must be a non-empty string")


def _require_non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WalkForwardResultError(f"{name} must be a non-negative integer")


def _require_finite(name: str, value: object) -> None:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float, np.number))
        or not math.isfinite(float(value))
    ):
        raise WalkForwardResultError(f"{name} must be a finite number")


def _require_rate(name: str, value: object) -> None:
    _require_finite(name, value)
    if not 0.0 <= float(value) <= 1.0:
        raise WalkForwardResultError(f"{name} must be in [0, 1]")


def _require_non_negative_finite(name: str, value: object) -> None:
    _require_finite(name, value)
    if float(value) < 0.0:
        raise WalkForwardResultError(f"{name} must be non-negative")
