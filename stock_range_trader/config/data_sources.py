"""Strict Phase 2 data-source configuration."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import TypeAlias, TypeVar

import yaml

PathLike: TypeAlias = str | Path
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class JQuantsConfig:
    """J-Quants V2 Free-plan request policy."""

    plan: str = "free"
    rate_limit_per_minute: int = 5
    min_request_interval_seconds: float = 13.0
    timeout_seconds: float = 30.0
    max_retries: int = 3

    def __post_init__(self) -> None:
        if self.plan != "free":
            raise ValueError("Phase 2 J-Quants plan must be 'free'")
        if self.rate_limit_per_minute != 5:
            raise ValueError("J-Quants Free rate_limit_per_minute must be 5")
        if self.min_request_interval_seconds < 12.0:
            raise ValueError("J-Quants Free requests must be at least 12 seconds apart")
        _positive("timeout_seconds", self.timeout_seconds)
        _positive_int("max_retries", self.max_retries)


@dataclass(frozen=True, slots=True)
class YFinanceConfig:
    """Explicit yfinance batching and retry policy."""

    batch_size: int = 50
    threads: bool = False
    timeout_seconds: float = 20.0
    max_retries: int = 3

    def __post_init__(self) -> None:
        _positive_int("batch_size", self.batch_size)
        if self.threads is not False:
            raise ValueError("Phase 2 requires yfinance threads: false")
        _positive("timeout_seconds", self.timeout_seconds)
        _positive_int("max_retries", self.max_retries)


@dataclass(frozen=True, slots=True)
class ScreeningConfig:
    """Cross-sectional screening defaults."""

    top_n: int = 30
    minimum_observations: int = 120
    maximum_missing_session_ratio: float = 0.10

    def __post_init__(self) -> None:
        _positive_int("top_n", self.top_n)
        _positive_int("minimum_observations", self.minimum_observations)
        if not 0.0 <= self.maximum_missing_session_ratio < 1.0:
            raise ValueError("maximum_missing_session_ratio must be in [0, 1)")


@dataclass(frozen=True, slots=True)
class ComparisonConfig:
    """Provider reconciliation warning thresholds."""

    price_relative_tolerance: float = 0.01
    volume_relative_tolerance: float = 0.10

    def __post_init__(self) -> None:
        _non_negative("price_relative_tolerance", self.price_relative_tolerance)
        _non_negative("volume_relative_tolerance", self.volume_relative_tolerance)


@dataclass(frozen=True, slots=True)
class DataSourcesConfig:
    """Complete Phase 2 data configuration."""

    cache_root: str
    schema_version: str
    jquants: JQuantsConfig
    yfinance: YFinanceConfig
    screening: ScreeningConfig
    comparison: ComparisonConfig

    @classmethod
    def from_mapping(cls, values: object) -> DataSourcesConfig:
        if not isinstance(values, dict):
            raise ValueError("data source configuration must be a YAML mapping")
        expected = {field.name for field in fields(cls)}
        _reject_key_mismatch("data source configuration", values, expected)
        cache_root = values["cache_root"]
        schema_version = values["schema_version"]
        if not isinstance(cache_root, str) or not cache_root.strip():
            raise ValueError("cache_root must be a non-empty string")
        if not isinstance(schema_version, str) or not schema_version.strip():
            raise ValueError("schema_version must be a non-empty string")
        return cls(
            cache_root=cache_root,
            schema_version=schema_version,
            jquants=_build_section(JQuantsConfig, values["jquants"]),
            yfinance=_build_section(YFinanceConfig, values["yfinance"]),
            screening=_build_section(ScreeningConfig, values["screening"]),
            comparison=_build_section(ComparisonConfig, values["comparison"]),
        )


def load_data_sources_config(path: PathLike) -> DataSourcesConfig:
    """Load one strict UTF-8 Phase 2 YAML configuration."""

    source_path = Path(path).expanduser()
    if not source_path.is_file():
        raise FileNotFoundError(f"Data source configuration not found: {source_path}")
    try:
        with source_path.open(encoding="utf-8") as source:
            values = yaml.safe_load(source)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(
            f"Failed to load data source configuration: {error}"
        ) from error
    return DataSourcesConfig.from_mapping(values)


def _build_section(section_type: type[T], values: object) -> T:
    if not isinstance(values, dict):
        raise ValueError(f"{section_type.__name__} must be a YAML mapping")
    expected = {field.name for field in fields(section_type)}
    _reject_key_mismatch(section_type.__name__, values, expected)
    try:
        return section_type(**values)
    except TypeError as error:
        raise ValueError(f"Invalid {section_type.__name__}: {error}") from error


def _reject_key_mismatch(
    name: str, values: dict[object, object], expected: set[str]
) -> None:
    supplied = set(values)
    missing = sorted(expected - supplied)
    unknown = sorted(supplied - expected)
    if missing:
        raise ValueError(f"Missing {name} keys: " + ", ".join(missing))
    if unknown:
        raise ValueError(f"Unknown {name} keys: " + ", ".join(map(str, unknown)))


def _positive(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _non_negative(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{name} must be non-negative")


def _positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
