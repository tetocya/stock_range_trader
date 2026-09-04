"""Strict Phase 3 walk-forward configuration schema."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import ClassVar, TypeAlias, TypeVar

import yaml

PathLike: TypeAlias = str | Path
T = TypeVar("T")

MAX_CANDIDATES_PER_CATALOG = 12
PHASE3_SCHEMA_VERSION = "3.0"
GIT_UNAVAILABLE_SOURCE_STATE = "git_unavailable"

_CANDIDATE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class FoldScheduleConfig:
    """Validated calendar schedule and session-based purge settings."""

    EXPECTED_PURGE_RULE: ClassVar[str] = "label_end_date_lt_test_start"

    train_months: int
    validation_months: int
    test_months: int
    step_months: int
    forward_sessions: int
    embargo_sessions: int
    minimum_folds: int
    purge_rule: str

    def __post_init__(self) -> None:
        for name in (
            "train_months",
            "validation_months",
            "test_months",
            "step_months",
            "minimum_folds",
        ):
            _positive_int(name, getattr(self, name))
        for name in ("forward_sessions", "embargo_sessions"):
            _non_negative_int(name, getattr(self, name))
        if self.step_months < self.test_months:
            raise ValueError("step_months must be greater than or equal to test_months")
        if self.embargo_sessions < self.forward_sessions:
            raise ValueError(
                "embargo_sessions must be greater than or equal to forward_sessions"
            )
        if self.purge_rule != self.EXPECTED_PURGE_RULE:
            raise ValueError(
                "purge_rule must compare each observation's actual label_end_date "
                "with test_start"
            )

    @classmethod
    def from_mapping(cls, values: object) -> FoldScheduleConfig:
        return _build_strict_dataclass(cls, values, "fold schedule")


@dataclass(frozen=True, slots=True)
class SignalSelectionPolicy:
    """Selection contract for adjusted-price Signal outcomes."""

    EXPECTED_PRIMARY_METRIC: ClassVar[str] = "mean_reversion_target_hit_rate"
    EXPECTED_TARGET: ClassVar[str] = "signal_date_sma"
    EXPECTED_TIE_BREAKERS: ClassVar[tuple[str, ...]] = (
        "median_forward_return_desc",
        "median_mae_magnitude_asc",
        "candidate_id_asc",
    )

    primary_metric: str
    mean_reversion_target: str
    minimum_observation_count: int
    tie_breakers: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.primary_metric != self.EXPECTED_PRIMARY_METRIC:
            raise ValueError(
                f"Signal primary_metric must be {self.EXPECTED_PRIMARY_METRIC!r}"
            )
        if self.mean_reversion_target != self.EXPECTED_TARGET:
            raise ValueError(
                "Signal mean_reversion_target must be the SMA observed on the "
                "signal date"
            )
        _positive_int("minimum_observation_count", self.minimum_observation_count)
        _require_exact_tie_breakers(
            "Signal", self.tie_breakers, self.EXPECTED_TIE_BREAKERS
        )

    @classmethod
    def from_mapping(cls, values: object) -> SignalSelectionPolicy:
        converted = _selection_mapping(values, cls, "signal selection policy")
        return cls(**converted)


@dataclass(frozen=True, slots=True)
class ExecutableSelectionPolicy:
    """Selection contract for independent-capital executable results."""

    EXPECTED_PRIMARY_METRIC: ClassVar[str] = "median_symbol_sharpe_ratio"
    EXPECTED_TIE_BREAKERS: ClassVar[tuple[str, ...]] = (
        "median_symbol_maximum_drawdown_magnitude_asc",
        "median_symbol_net_return_desc",
        "candidate_id_asc",
    )

    primary_metric: str
    minimum_traded_symbol_count: int
    minimum_trading_symbol_ratio: float
    minimum_total_trade_count: int
    minimum_finite_sharpe_count: int
    maximum_drawdown_limit: float
    tie_breakers: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.primary_metric != self.EXPECTED_PRIMARY_METRIC:
            raise ValueError(
                f"Executable primary_metric must be {self.EXPECTED_PRIMARY_METRIC!r}"
            )
        for name in (
            "minimum_traded_symbol_count",
            "minimum_total_trade_count",
            "minimum_finite_sharpe_count",
        ):
            _positive_int(name, getattr(self, name))
        _closed_unit_interval(
            "minimum_trading_symbol_ratio",
            self.minimum_trading_symbol_ratio,
        )
        _drawdown_loss_magnitude(
            "maximum_drawdown_limit",
            self.maximum_drawdown_limit,
        )
        _require_exact_tie_breakers(
            "Executable", self.tie_breakers, self.EXPECTED_TIE_BREAKERS
        )

    @classmethod
    def from_mapping(cls, values: object) -> ExecutableSelectionPolicy:
        converted = _selection_mapping(values, cls, "executable selection policy")
        return cls(**converted)


@dataclass(frozen=True, slots=True)
class SignalCandidateConfig:
    """Entry-only candidate overrides for Signal Validation."""

    id: str
    buy_atr_multiplier: float
    range_score_threshold: float
    adx_entry_max: float

    def __post_init__(self) -> None:
        _candidate_id(self.id)
        _finite_non_negative("buy_atr_multiplier", self.buy_atr_multiplier)
        _percentage("range_score_threshold", self.range_score_threshold)
        _finite_non_negative("adx_entry_max", self.adx_entry_max)

    @classmethod
    def from_mapping(cls, values: object) -> SignalCandidateConfig:
        return _build_strict_dataclass(cls, values, "signal candidate")


@dataclass(frozen=True, slots=True)
class ExecutableCandidateConfig:
    """Entry and exit candidate overrides for Executable Validation."""

    id: str
    buy_atr_multiplier: float
    sell_atr_multiplier: float
    range_score_threshold: float
    adx_entry_max: float

    def __post_init__(self) -> None:
        _candidate_id(self.id)
        _finite_non_negative("buy_atr_multiplier", self.buy_atr_multiplier)
        _finite_non_negative("sell_atr_multiplier", self.sell_atr_multiplier)
        _percentage("range_score_threshold", self.range_score_threshold)
        _finite_non_negative("adx_entry_max", self.adx_entry_max)

    @classmethod
    def from_mapping(cls, values: object) -> ExecutableCandidateConfig:
        return _build_strict_dataclass(cls, values, "executable candidate")


@dataclass(frozen=True, slots=True)
class SignalCandidateCatalogConfig:
    """Bounded, immutable Signal candidate catalog."""

    maximum_candidates: int
    candidates: tuple[SignalCandidateConfig, ...]

    def __post_init__(self) -> None:
        _validate_catalog(
            "signal", self.maximum_candidates, self.candidates, SignalCandidateConfig
        )

    @classmethod
    def from_mapping(cls, values: object) -> SignalCandidateCatalogConfig:
        mapping = _strict_mapping(
            "signal candidate catalog",
            values,
            {"maximum_candidates", "candidates"},
        )
        candidates = _build_candidates(
            "signal", mapping["candidates"], SignalCandidateConfig
        )
        return cls(
            maximum_candidates=mapping["maximum_candidates"],
            candidates=candidates,
        )


@dataclass(frozen=True, slots=True)
class ExecutableCandidateCatalogConfig:
    """Bounded, immutable Executable candidate catalog."""

    maximum_candidates: int
    candidates: tuple[ExecutableCandidateConfig, ...]

    def __post_init__(self) -> None:
        _validate_catalog(
            "executable",
            self.maximum_candidates,
            self.candidates,
            ExecutableCandidateConfig,
        )

    @classmethod
    def from_mapping(cls, values: object) -> ExecutableCandidateCatalogConfig:
        mapping = _strict_mapping(
            "executable candidate catalog",
            values,
            {"maximum_candidates", "candidates"},
        )
        candidates = _build_candidates(
            "executable", mapping["candidates"], ExecutableCandidateConfig
        )
        return cls(
            maximum_candidates=mapping["maximum_candidates"],
            candidates=candidates,
        )


@dataclass(frozen=True, slots=True)
class Phase3Config:
    """Complete strict schema for the Phase 3 validation configuration."""

    schema_version: str
    random_seed: int
    require_clean_worktree_for_formal_oos: bool
    git_unavailable_source_state: str
    git_unavailable_reproducibility_status: str
    git_unavailable_formal_oos_eligible: bool
    signal_fold_schedule: FoldScheduleConfig
    executable_fold_schedule: FoldScheduleConfig
    signal_selection: SignalSelectionPolicy
    executable_selection: ExecutableSelectionPolicy
    signal_candidate_catalog: SignalCandidateCatalogConfig
    executable_candidate_catalog: ExecutableCandidateCatalogConfig

    def __post_init__(self) -> None:
        if self.schema_version != PHASE3_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {PHASE3_SCHEMA_VERSION!r} for Phase 3"
            )
        _non_negative_int("random_seed", self.random_seed)
        if self.require_clean_worktree_for_formal_oos is not True:
            raise ValueError("formal OOS runs must require a clean Git worktree")
        if self.git_unavailable_source_state != GIT_UNAVAILABLE_SOURCE_STATE:
            raise ValueError(
                f"git_unavailable_source_state must be {GIT_UNAVAILABLE_SOURCE_STATE!r}"
            )
        if self.git_unavailable_reproducibility_status != "degraded":
            raise ValueError(
                "git_unavailable_reproducibility_status must be 'degraded'"
            )
        if self.git_unavailable_formal_oos_eligible is not False:
            raise ValueError(
                "git_unavailable source state must not be eligible for formal OOS"
            )
        for name, expected_type in (
            ("signal_fold_schedule", FoldScheduleConfig),
            ("executable_fold_schedule", FoldScheduleConfig),
            ("signal_selection", SignalSelectionPolicy),
            ("executable_selection", ExecutableSelectionPolicy),
            ("signal_candidate_catalog", SignalCandidateCatalogConfig),
            ("executable_candidate_catalog", ExecutableCandidateCatalogConfig),
        ):
            if not isinstance(getattr(self, name), expected_type):
                raise TypeError(f"{name} must be {expected_type.__name__}")
        if self.signal_fold_schedule.forward_sessions <= 0:
            raise ValueError("Signal forward_sessions must be a positive integer")

    @classmethod
    def from_mapping(cls, values: object) -> Phase3Config:
        mapping = _strict_mapping(
            "Phase 3 configuration",
            values,
            {field.name for field in fields(cls)},
        )
        return cls(
            schema_version=mapping["schema_version"],
            random_seed=mapping["random_seed"],
            require_clean_worktree_for_formal_oos=mapping[
                "require_clean_worktree_for_formal_oos"
            ],
            git_unavailable_source_state=mapping["git_unavailable_source_state"],
            git_unavailable_reproducibility_status=mapping[
                "git_unavailable_reproducibility_status"
            ],
            git_unavailable_formal_oos_eligible=mapping[
                "git_unavailable_formal_oos_eligible"
            ],
            signal_fold_schedule=FoldScheduleConfig.from_mapping(
                mapping["signal_fold_schedule"]
            ),
            executable_fold_schedule=FoldScheduleConfig.from_mapping(
                mapping["executable_fold_schedule"]
            ),
            signal_selection=SignalSelectionPolicy.from_mapping(
                mapping["signal_selection"]
            ),
            executable_selection=ExecutableSelectionPolicy.from_mapping(
                mapping["executable_selection"]
            ),
            signal_candidate_catalog=SignalCandidateCatalogConfig.from_mapping(
                mapping["signal_candidate_catalog"]
            ),
            executable_candidate_catalog=(
                ExecutableCandidateCatalogConfig.from_mapping(
                    mapping["executable_candidate_catalog"]
                )
            ),
        )


def load_phase3_config(path: PathLike) -> Phase3Config:
    """Load one strict UTF-8 Phase 3 YAML configuration."""

    source_path = Path(path).expanduser()
    if not source_path.is_file():
        raise FileNotFoundError(f"Phase 3 configuration file not found: {source_path}")
    try:
        with source_path.open(encoding="utf-8") as source:
            values = yaml.safe_load(source)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"Failed to load Phase 3 configuration: {error}") from error
    return Phase3Config.from_mapping(values)


def _selection_mapping(
    values: object, section_type: type[T], name: str
) -> dict[str, object]:
    mapping = _strict_mapping(
        name, values, {field.name for field in fields(section_type)}
    )
    converted = dict(mapping)
    converted["tie_breakers"] = _string_tuple(
        f"{name} tie_breakers", converted["tie_breakers"]
    )
    return converted


def _build_strict_dataclass(section_type: type[T], values: object, name: str) -> T:
    mapping = _strict_mapping(
        name, values, {field.name for field in fields(section_type)}
    )
    try:
        return section_type(**mapping)
    except TypeError as error:
        raise ValueError(f"Invalid {name}: {error}") from error


def _strict_mapping(name: str, values: object, expected: set[str]) -> dict[str, object]:
    if not isinstance(values, dict):
        raise ValueError(f"{name} must be a YAML mapping")
    supplied = set(values)
    missing = sorted(expected - supplied)
    unknown = sorted(supplied - expected, key=str)
    if missing:
        raise ValueError(f"Missing {name} keys: " + ", ".join(missing))
    if unknown:
        raise ValueError(f"Unknown {name} keys: " + ", ".join(map(str, unknown)))
    return values


def _build_candidates(
    name: str,
    values: object,
    candidate_type: type[T],
) -> tuple[T, ...]:
    if not isinstance(values, list):
        raise ValueError(f"{name} candidates must be a YAML sequence")
    return tuple(candidate_type.from_mapping(value) for value in values)


def _validate_catalog(
    name: str,
    maximum_candidates: int,
    candidates: tuple[T, ...],
    candidate_type: type[T],
) -> None:
    _positive_int("maximum_candidates", maximum_candidates)
    if maximum_candidates > MAX_CANDIDATES_PER_CATALOG:
        raise ValueError(
            f"{name} maximum_candidates cannot exceed {MAX_CANDIDATES_PER_CATALOG}"
        )
    if not isinstance(candidates, tuple) or any(
        not isinstance(candidate, candidate_type) for candidate in candidates
    ):
        raise TypeError(
            f"{name} candidates must be a tuple of {candidate_type.__name__}"
        )
    if not candidates:
        raise ValueError(f"{name} candidate catalog cannot be empty")
    if len(candidates) > maximum_candidates:
        raise ValueError(
            f"{name} candidate count {len(candidates)} exceeds configured maximum "
            f"{maximum_candidates}"
        )
    ids = [candidate.id for candidate in candidates]
    duplicates = sorted(
        {candidate_id for candidate_id in ids if ids.count(candidate_id) > 1}
    )
    if duplicates:
        raise ValueError(f"duplicate {name} candidate ids: " + ", ".join(duplicates))


def _candidate_id(value: object) -> None:
    if not isinstance(value, str) or not _CANDIDATE_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "candidate id must use lowercase letters, digits, underscores, or hyphens"
        )


def _require_exact_tie_breakers(
    name: str, actual: object, expected: tuple[str, ...]
) -> None:
    if not isinstance(actual, tuple) or actual != expected:
        raise ValueError(f"{name} tie_breakers must be exactly: " + ", ".join(expected))


def _string_tuple(name: str, values: object) -> tuple[str, ...]:
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise ValueError(f"{name} must be a YAML sequence of non-empty strings")
    return tuple(values)


def _positive_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _finite_non_negative(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0.0
    ):
        raise ValueError(f"{name} must be finite and non-negative")


def _percentage(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 100.0
    ):
        raise ValueError(f"{name} must be between 0 and 100")


def _closed_unit_interval(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise ValueError(f"{name} must be in [0, 1]")


def _drawdown_loss_magnitude(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value < 1.0
    ):
        raise ValueError(f"{name} must be a non-negative loss magnitude in [0, 1)")
