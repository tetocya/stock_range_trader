"""Deterministic Phase 3 experiment identity and provenance contracts."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from config import Phase3Config, StrategyConfig
from data import CANONICAL_COLUMNS, CANONICAL_SCHEMA_VERSION
from universe import UNIVERSE_COLUMNS

from .capabilities import AnalysisMode, ProviderCapability
from .folds import FoldSchedule

REPORT_SCHEMA_VERSION = "phase3-report-1.0"
FORMAL_OOS_CLAIM_SCOPE = (
    "code-verifiable preconditions only; it does not prove that a person has "
    "never inspected prior Test results"
)
SURVIVORSHIP_LIMITATION = (
    "not_indicated_by_snapshot_timing only means snapshot timing did not reveal "
    "future-universe use; it does not reconstruct delistings or symbol changes"
)


class ExperimentError(ValueError):
    """Raised when provenance cannot be represented deterministically."""


class SourceState(str, Enum):
    CLEAN = "clean"
    DIRTY = "dirty"
    GIT_UNAVAILABLE = "git_unavailable"


@dataclass(frozen=True, slots=True)
class InputArtifactFingerprint:
    """Byte identity and semantic identity of one Canonical Parquet input."""

    filename: str
    file_sha256: str
    canonical_content_sha256: str
    file_size_bytes: int
    row_count: int
    column_names: tuple[str, ...]
    actual_start: date | None
    actual_end: date | None

    def __post_init__(self) -> None:
        _non_empty("filename", self.filename)
        _sha256("file_sha256", self.file_sha256)
        _sha256("canonical_content_sha256", self.canonical_content_sha256)
        _non_negative_int("file_size_bytes", self.file_size_bytes)
        _non_negative_int("row_count", self.row_count)
        _string_tuple("column_names", self.column_names)
        _date_range(self.actual_start, self.actual_end)


@dataclass(frozen=True, slots=True)
class ConfigArtifactFingerprint:
    """Byte identity of a configuration file without exposing its path."""

    filename: str
    file_sha256: str

    def __post_init__(self) -> None:
        _non_empty("filename", self.filename)
        _sha256("file_sha256", self.file_sha256)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Git source state used by identity and formal-OOS checks."""

    source_state: SourceState
    git_root: str | None
    git_commit_sha: str | None
    git_branch: str | None
    worktree_dirty: bool | None
    source_tree_sha256: str | None
    reproducibility_status: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_state, SourceState):
            raise TypeError("source_state must be SourceState")
        if self.source_state is SourceState.GIT_UNAVAILABLE:
            if any(
                value is not None
                for value in (
                    self.git_root,
                    self.git_commit_sha,
                    self.git_branch,
                    self.worktree_dirty,
                    self.source_tree_sha256,
                )
            ):
                raise ExperimentError("git_unavailable cannot claim Git metadata")
            if self.reproducibility_status != "degraded":
                raise ExperimentError("git_unavailable reproducibility is degraded")
            return
        _non_empty("git_root", self.git_root)
        _sha256("git_commit_sha", self.git_commit_sha, git=True)
        _sha256("source_tree_sha256", self.source_tree_sha256)
        if self.git_branch is not None:
            _non_empty("git_branch", self.git_branch)
        if not isinstance(self.worktree_dirty, bool):
            raise ExperimentError("Git source requires a worktree_dirty boolean")
        expected = self.source_state is SourceState.DIRTY
        if self.worktree_dirty is not expected:
            raise ExperimentError("source_state and worktree_dirty disagree")
        if self.reproducibility_status != (
            "reproducible" if not expected else "degraded"
        ):
            raise ExperimentError("reproducibility_status disagrees with Git state")


@dataclass(frozen=True, slots=True)
class UniverseCoverageRecord:
    """Price availability for one included or observed symbol."""

    symbol: str
    in_universe: bool
    price_row_count: int
    first_price_date: date | None
    last_price_date: date | None
    coverage_status: str

    def __post_init__(self) -> None:
        _non_empty("symbol", self.symbol)
        if not isinstance(self.in_universe, bool):
            raise ExperimentError("in_universe must be boolean")
        _non_negative_int("price_row_count", self.price_row_count)
        _date_range(self.first_price_date, self.last_price_date)
        if self.coverage_status not in {
            "available",
            "missing_prices",
            "excluded_not_in_universe",
        }:
            raise ExperimentError("unknown universe coverage status")
        expected = (
            "excluded_not_in_universe"
            if not self.in_universe
            else "available"
            if self.price_row_count
            else "missing_prices"
        )
        if self.coverage_status != expected:
            raise ExperimentError("universe coverage status is inconsistent")
        if (self.price_row_count == 0) != (self.first_price_date is None):
            raise ExperimentError("coverage dates disagree with price_row_count")


@dataclass(frozen=True, slots=True)
class FoldUniverseAssessment:
    """Snapshot timing assessment for one Test fold."""

    fold_id: str
    temporal_oos: bool
    point_in_time_universe: bool
    survivorship_bias_status: str

    def __post_init__(self) -> None:
        _non_empty("fold_id", self.fold_id)
        if self.temporal_oos is not True:
            raise ExperimentError("STEP 7 fold results must remain temporal OOS")
        if not isinstance(self.point_in_time_universe, bool):
            raise ExperimentError("point_in_time_universe must be boolean")
        expected = (
            "not_indicated_by_snapshot_timing"
            if self.point_in_time_universe
            else "present"
        )
        if self.survivorship_bias_status != expected:
            raise ExperimentError("survivorship status disagrees with snapshot timing")


@dataclass(frozen=True, slots=True)
class UniverseAssessment:
    """Normalized universe identity, coverage, and fold timing assessments."""

    filename: str
    file_sha256: str
    normalized_universe_sha256: str
    universe_as_of_date: date
    included_symbols: tuple[str, ...]
    included_symbol_count: int
    available_price_symbol_count: int
    missing_price_symbol_count: int
    unexpected_price_symbol_count: int
    coverage: tuple[UniverseCoverageRecord, ...]
    fold_assessments: tuple[FoldUniverseAssessment, ...]
    point_in_time_universe: bool
    survivorship_bias_status: str

    def __post_init__(self) -> None:
        _non_empty("filename", self.filename)
        _sha256("file_sha256", self.file_sha256)
        _sha256("normalized_universe_sha256", self.normalized_universe_sha256)
        _require_date("universe_as_of_date", self.universe_as_of_date)
        _string_tuple("included_symbols", self.included_symbols)
        if self.included_symbols != tuple(sorted(self.included_symbols)) or len(
            self.included_symbols
        ) != len(set(self.included_symbols)):
            raise ExperimentError("included_symbols must be unique and sorted")
        for name in (
            "included_symbol_count",
            "available_price_symbol_count",
            "missing_price_symbol_count",
            "unexpected_price_symbol_count",
        ):
            _non_negative_int(name, getattr(self, name))
        if self.included_symbol_count != len(self.included_symbols):
            raise ExperimentError("included_symbol_count is inconsistent")
        if self.available_price_symbol_count + self.missing_price_symbol_count != (
            self.included_symbol_count
        ):
            raise ExperimentError("universe availability counts are inconsistent")
        _tuple_of("coverage", self.coverage, UniverseCoverageRecord)
        if tuple(item.symbol for item in self.coverage) != tuple(
            sorted(item.symbol for item in self.coverage)
        ):
            raise ExperimentError("universe coverage must be sorted by symbol")
        _tuple_of("fold_assessments", self.fold_assessments, FoldUniverseAssessment)
        if not isinstance(self.point_in_time_universe, bool):
            raise ExperimentError("run point_in_time_universe must be boolean")
        expected_point_in_time = all(
            item.point_in_time_universe for item in self.fold_assessments
        )
        if self.point_in_time_universe != expected_point_in_time:
            raise ExperimentError("run point-in-time state must cover every fold")
        expected_bias = (
            "not_indicated_by_snapshot_timing"
            if self.point_in_time_universe
            else "present"
        )
        if self.survivorship_bias_status != expected_bias:
            raise ExperimentError("run survivorship state is inconsistent")


@dataclass(frozen=True, slots=True)
class ExperimentIdentity:
    """Deterministic identifier and its normalized identity payload."""

    experiment_id: str
    analysis_mode: AnalysisMode
    payload_sha256: str
    normalized_payload_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.analysis_mode, AnalysisMode):
            raise TypeError("analysis_mode must be AnalysisMode")
        _sha256("payload_sha256", self.payload_sha256)
        expected = f"wf3-{self.analysis_mode.value}-{self.payload_sha256}"
        if self.experiment_id != expected:
            raise ExperimentError("experiment_id does not match its payload hash")
        try:
            decoded = json.loads(self.normalized_payload_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise ExperimentError(
                "normalized identity payload is invalid JSON"
            ) from error
        if _canonical_json(decoded) != self.normalized_payload_json:
            raise ExperimentError("identity payload is not canonical JSON")
        actual_digest = hashlib.sha256(
            self.normalized_payload_json.encode("ascii")
        ).hexdigest()
        if actual_digest != self.payload_sha256:
            raise ExperimentError("payload_sha256 must match normalized_payload_json")


@dataclass(frozen=True, slots=True)
class FormalOOSAssessment:
    """Machine-checkable formal-OOS eligibility and all failed conditions."""

    eligible: bool
    reasons: tuple[str, ...]
    claim_scope: str = FORMAL_OOS_CLAIM_SCOPE

    def __post_init__(self) -> None:
        if not isinstance(self.eligible, bool):
            raise ExperimentError("formal OOS eligible must be boolean")
        _string_tuple("reasons", self.reasons, allow_empty=True)
        if len(self.reasons) != len(set(self.reasons)):
            raise ExperimentError("formal OOS reasons must be unique")
        if self.eligible == bool(self.reasons):
            raise ExperimentError("formal OOS eligibility and reasons disagree")
        _non_empty("claim_scope", self.claim_scope)


@dataclass(frozen=True, slots=True)
class WalkForwardRunMetadata:
    """Typed context used to build reports without re-running analysis."""

    identity: ExperimentIdentity
    source: SourceSnapshot
    input_artifact: InputArtifactFingerprint
    universe: UniverseAssessment
    provider_capability: ProviderCapability
    phase3_config: Phase3Config
    strategy_config: StrategyConfig
    schedule: FoldSchedule
    phase3_config_artifact: ConfigArtifactFingerprint
    strategy_config_artifact: ConfigArtifactFingerprint
    requested_start: date
    requested_end_exclusive: date
    formal_oos: FormalOOSAssessment
    started_at_utc: datetime
    completed_at_utc: datetime
    parent_experiment_id: str | None = None
    change_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ExperimentIdentity):
            raise TypeError("identity must be ExperimentIdentity")
        for name, value, expected in (
            ("source", self.source, SourceSnapshot),
            ("input_artifact", self.input_artifact, InputArtifactFingerprint),
            ("universe", self.universe, UniverseAssessment),
            ("provider_capability", self.provider_capability, ProviderCapability),
            ("phase3_config", self.phase3_config, Phase3Config),
            ("strategy_config", self.strategy_config, StrategyConfig),
            ("schedule", self.schedule, FoldSchedule),
            (
                "phase3_config_artifact",
                self.phase3_config_artifact,
                ConfigArtifactFingerprint,
            ),
            (
                "strategy_config_artifact",
                self.strategy_config_artifact,
                ConfigArtifactFingerprint,
            ),
            ("formal_oos", self.formal_oos, FormalOOSAssessment),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{name} must be {expected.__name__}")
        _require_date("requested_start", self.requested_start)
        _require_date("requested_end_exclusive", self.requested_end_exclusive)
        if self.requested_start >= self.requested_end_exclusive:
            raise ExperimentError("requested start must precede exclusive end")
        for name in ("started_at_utc", "completed_at_utc"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ExperimentError(f"{name} must be timezone-aware")
        if self.completed_at_utc < self.started_at_utc:
            raise ExperimentError("completed_at_utc precedes started_at_utc")
        if (self.parent_experiment_id is None) != (self.change_reason is None):
            raise ExperimentError(
                "parent_experiment_id and change_reason must be supplied together"
            )
        if self.parent_experiment_id is not None:
            _non_empty("parent_experiment_id", self.parent_experiment_id)
            _non_empty("change_reason", self.change_reason)


class SourceStateResolver:
    """Resolve Git state without treating command failures as a clean tree."""

    def resolve(self, path: str | Path) -> SourceSnapshot:
        location = Path(path).expanduser()
        try:
            root_result = _git(location, "rev-parse", "--show-toplevel")
            if root_result.returncode != 0:
                return _git_unavailable()
            root = Path(root_result.stdout.strip())
            commit_result = _git(root, "rev-parse", "HEAD")
            status_result = _git(
                root, "status", "--porcelain=v1", "--untracked-files=all"
            )
            files_result = _git_bytes(
                root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
            )
            if any(
                item.returncode != 0
                for item in (commit_result, status_result, files_result)
            ):
                return _git_unavailable()
            branch_result = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
            branch = (
                branch_result.stdout.strip() if branch_result.returncode == 0 else None
            )
            dirty = bool(status_result.stdout)
            return SourceSnapshot(
                source_state=SourceState.DIRTY if dirty else SourceState.CLEAN,
                git_root=str(root),
                git_commit_sha=commit_result.stdout.strip(),
                git_branch=branch,
                worktree_dirty=dirty,
                source_tree_sha256=_source_tree_hash(root, files_result.stdout),
                reproducibility_status="degraded" if dirty else "reproducible",
            )
        except (FileNotFoundError, OSError, UnicodeError):
            return _git_unavailable()


class ExperimentIdentityBuilder:
    """Build a deterministic identity from analysis-affecting inputs only."""

    def build(
        self,
        *,
        phase3_config: Phase3Config,
        strategy_config: StrategyConfig,
        candidate_catalog: object,
        selection_policy: object,
        schedule: FoldSchedule,
        provider: str,
        analysis_mode: AnalysisMode,
        provider_price_basis: str,
        input_artifact: InputArtifactFingerprint,
        universe: UniverseAssessment,
        source: SourceSnapshot,
    ) -> ExperimentIdentity:
        if not isinstance(phase3_config, Phase3Config):
            raise TypeError("phase3_config must be Phase3Config")
        if not isinstance(strategy_config, StrategyConfig):
            raise TypeError("strategy_config must be StrategyConfig")
        if not isinstance(schedule, FoldSchedule):
            raise TypeError("schedule must be FoldSchedule")
        if not isinstance(analysis_mode, AnalysisMode):
            raise TypeError("analysis_mode must be AnalysisMode")
        payload = {
            "phase3_config": _normalize(phase3_config),
            "strategy_config": _normalize(strategy_config),
            "candidate_catalog": _normalize(candidate_catalog),
            "selection_policy": _normalize(selection_policy),
            "fold_schedule": _normalize(schedule),
            "provider": provider,
            "analysis_mode": analysis_mode.value,
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "provider_price_basis": provider_price_basis,
            "canonical_content_sha256": input_artifact.canonical_content_sha256,
            "normalized_universe_sha256": universe.normalized_universe_sha256,
            "universe_as_of_date": universe.universe_as_of_date.isoformat(),
            "random_seed": phase3_config.random_seed,
            "git_commit_sha": source.git_commit_sha,
            "source_tree_sha256": source.source_tree_sha256,
            "source_state": source.source_state.value,
        }
        normalized = _canonical_json(payload)
        digest = hashlib.sha256(normalized.encode("ascii")).hexdigest()
        return ExperimentIdentity(
            experiment_id=f"wf3-{analysis_mode.value}-{digest}",
            analysis_mode=analysis_mode,
            payload_sha256=digest,
            normalized_payload_json=normalized,
        )


def build_input_artifact_fingerprint(
    path: str | Path, frame: pd.DataFrame
) -> InputArtifactFingerprint:
    """Fingerprint both the Parquet bytes and normalized Canonical values."""

    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"input artifact not found: {source}")
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    dates = pd.to_datetime(frame["date"], errors="raise") if len(frame) else None
    return InputArtifactFingerprint(
        filename=source.name,
        file_sha256=sha256_file(source),
        canonical_content_sha256=canonical_content_sha256(frame),
        file_size_bytes=source.stat().st_size,
        row_count=len(frame),
        column_names=tuple(str(item) for item in frame.columns),
        actual_start=dates.min().date() if dates is not None else None,
        actual_end=dates.max().date() if dates is not None else None,
    )


def build_config_artifact_fingerprint(path: str | Path) -> ConfigArtifactFingerprint:
    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"configuration artifact not found: {source}")
    return ConfigArtifactFingerprint(source.name, sha256_file(source))


def canonical_content_sha256(frame: pd.DataFrame) -> str:
    """Hash normalized analysis values, excluding non-semantic fetched_at."""

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    missing = tuple(column for column in CANONICAL_COLUMNS if column not in frame)
    if missing:
        raise ExperimentError(
            "Canonical semantic hash missing columns: " + ", ".join(missing)
        )
    columns = tuple(column for column in CANONICAL_COLUMNS if column != "fetched_at")
    ordered = frame.loc[:, columns].copy()
    ordered["date"] = pd.to_datetime(ordered["date"], errors="raise")
    ordered = ordered.sort_values(["symbol", "date"], kind="stable")
    hasher = hashlib.sha256()
    _hash_fields(hasher, columns)
    for row in ordered.itertuples(index=False, name=None):
        _hash_fields(hasher, tuple(_semantic_scalar(value) for value in row))
    return hasher.hexdigest()


def assess_universe(
    path: str | Path,
    universe: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    provider: str,
    schedule: FoldSchedule,
) -> UniverseAssessment:
    """Validate one snapshot and calculate deterministic coverage and bias flags."""

    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"universe snapshot not found: {source}")
    if not isinstance(universe, pd.DataFrame) or not isinstance(prices, pd.DataFrame):
        raise TypeError("universe and prices must be pandas DataFrames")
    missing = tuple(column for column in UNIVERSE_COLUMNS if column not in universe)
    if missing:
        raise ExperimentError("universe missing columns: " + ", ".join(missing))
    if universe.empty:
        raise ExperimentError("universe snapshot must not be empty")
    normalized = universe.loc[:, UNIVERSE_COLUMNS].copy()
    normalized["as_of_date"] = pd.to_datetime(normalized["as_of_date"], errors="raise")
    if normalized["as_of_date"].isna().any():
        raise ExperimentError("universe as_of_date contains missing values")
    as_of_values = tuple(sorted(set(normalized["as_of_date"].dt.date)))
    if len(as_of_values) != 1:
        raise ExperimentError("universe as_of_date must be identical on every row")
    if (
        not normalized["universe_included"]
        .map(lambda value: isinstance(value, (bool, np.bool_)))
        .all()
    ):
        raise ExperimentError("universe_included must contain booleans")
    symbol_column = {
        "jquants": "jquants_code",
        "yfinance": "yfinance_ticker",
    }.get(provider)
    if symbol_column is None:
        raise ExperimentError(f"unknown provider {provider!r}")
    included = normalized.loc[normalized["universe_included"].astype(bool)].copy()
    raw_symbols = tuple(included[symbol_column])
    if any(pd.isna(value) for value in raw_symbols):
        raise ExperimentError("included universe symbols must not be empty")
    symbols = tuple(str(value).strip() for value in raw_symbols)
    if any(not symbol for symbol in symbols):
        raise ExperimentError("included universe symbols must not be empty")
    if len(symbols) != len(set(symbols)):
        raise ExperimentError("included universe symbols must not contain duplicates")
    included_symbols = tuple(sorted(symbols))
    price_symbols = tuple(sorted(set(prices["symbol"].astype(str))))
    included_set = set(included_symbols)
    price_set = set(price_symbols)
    coverage: list[UniverseCoverageRecord] = []
    for symbol in sorted(included_set | price_set):
        rows = prices.loc[prices["symbol"].astype(str) == symbol]
        dates = pd.to_datetime(rows["date"], errors="raise")
        in_universe = symbol in included_set
        coverage.append(
            UniverseCoverageRecord(
                symbol=symbol,
                in_universe=in_universe,
                price_row_count=len(rows),
                first_price_date=dates.min().date() if len(rows) else None,
                last_price_date=dates.max().date() if len(rows) else None,
                coverage_status=(
                    "excluded_not_in_universe"
                    if not in_universe
                    else "available"
                    if len(rows)
                    else "missing_prices"
                ),
            )
        )
    fold_assessments = tuple(
        FoldUniverseAssessment(
            fold_id=fold.fold_id,
            temporal_oos=True,
            point_in_time_universe=as_of_values[0] <= fold.test_start,
            survivorship_bias_status=(
                "not_indicated_by_snapshot_timing"
                if as_of_values[0] <= fold.test_start
                else "present"
            ),
        )
        for fold in schedule.folds
    )
    point_in_time = all(item.point_in_time_universe for item in fold_assessments)
    return UniverseAssessment(
        filename=source.name,
        file_sha256=sha256_file(source),
        normalized_universe_sha256=normalized_universe_sha256(normalized),
        universe_as_of_date=as_of_values[0],
        included_symbols=included_symbols,
        included_symbol_count=len(included_symbols),
        available_price_symbol_count=len(included_set & price_set),
        missing_price_symbol_count=len(included_set - price_set),
        unexpected_price_symbol_count=len(price_set - included_set),
        coverage=tuple(coverage),
        fold_assessments=fold_assessments,
        point_in_time_universe=point_in_time,
        survivorship_bias_status=(
            "not_indicated_by_snapshot_timing" if point_in_time else "present"
        ),
    )


def filter_prices_to_universe(
    prices: pd.DataFrame, universe: UniverseAssessment
) -> pd.DataFrame:
    """Return only snapshot-included symbols in deterministic Canonical order."""

    if not isinstance(prices, pd.DataFrame):
        raise TypeError("prices must be a pandas DataFrame")
    if not isinstance(universe, UniverseAssessment):
        raise TypeError("universe must be UniverseAssessment")
    return (
        prices.loc[prices["symbol"].astype(str).isin(universe.included_symbols)]
        .sort_values(["symbol", "date"], kind="stable")
        .reset_index(drop=True)
    )


def normalized_universe_sha256(universe: pd.DataFrame) -> str:
    """Hash the complete normalized snapshot independent of input row order."""

    missing = tuple(column for column in UNIVERSE_COLUMNS if column not in universe)
    if missing:
        raise ExperimentError("universe hash missing columns: " + ", ".join(missing))
    frame = universe.loc[:, UNIVERSE_COLUMNS].copy()
    frame["as_of_date"] = pd.to_datetime(frame["as_of_date"], errors="raise")
    frame = frame.sort_values(
        ["jquants_code", "yfinance_ticker", "company_name"], kind="stable"
    )
    hasher = hashlib.sha256()
    _hash_fields(hasher, UNIVERSE_COLUMNS)
    for row in frame.itertuples(index=False, name=None):
        _hash_fields(hasher, tuple(_semantic_scalar(value) for value in row))
    return hasher.hexdigest()


def assess_formal_oos(
    *,
    source: SourceSnapshot,
    config: Phase3Config,
    input_artifact: InputArtifactFingerprint,
    universe: UniverseAssessment,
    capability_allowed: bool,
    output_collision: bool,
    parent_experiment_id: str | None,
) -> FormalOOSAssessment:
    """Evaluate every machine-checkable formal-OOS precondition."""

    reasons: list[str] = []
    if source.source_state is not SourceState.CLEAN:
        reasons.append(f"source_state_{source.source_state.value}")
    if not config.require_clean_worktree_for_formal_oos:
        reasons.append("clean_worktree_not_required_by_configuration")
    if source.git_commit_sha is None:
        reasons.append("git_commit_sha_unavailable")
    if not input_artifact.file_sha256 or not input_artifact.canonical_content_sha256:
        reasons.append("input_hash_unavailable")
    if not universe.file_sha256 or not universe.normalized_universe_sha256:
        reasons.append("universe_hash_unavailable")
    if not capability_allowed:
        reasons.append("provider_capability_not_allowed")
    if not all(item.temporal_oos for item in universe.fold_assessments):
        reasons.append("temporal_oos_not_satisfied")
    if not universe.point_in_time_universe:
        reasons.append("point_in_time_universe_not_satisfied")
    if output_collision:
        reasons.append("experiment_output_already_exists")
    if parent_experiment_id is not None:
        reasons.append("derived_after_test_review")
    return FormalOOSAssessment(eligible=not reasons, reasons=tuple(reasons))


def sha256_file(path: str | Path) -> str:
    source = Path(path)
    hasher = hashlib.sha256()
    with source.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def utc_now() -> datetime:
    """Injectable clock default for CLI orchestration."""

    return datetime.now(UTC)


def _git(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *arguments],
        capture_output=True,
        check=False,
        text=True,
    )


def _git_bytes(path: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(path), *arguments],
        capture_output=True,
        check=False,
    )


def _git_unavailable() -> SourceSnapshot:
    return SourceSnapshot(
        source_state=SourceState.GIT_UNAVAILABLE,
        git_root=None,
        git_commit_sha=None,
        git_branch=None,
        worktree_dirty=None,
        source_tree_sha256=None,
        reproducibility_status="degraded",
    )


def _source_tree_hash(root: Path, raw_paths: bytes) -> str:
    paths = sorted(item.decode("utf-8") for item in raw_paths.split(b"\0") if item)
    hasher = hashlib.sha256()
    for relative in paths:
        path = root / relative
        _hash_fields(hasher, (relative,))
        if path.is_file():
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    hasher.update(chunk)
        else:
            hasher.update(b"<deleted-or-non-file>")
        hasher.update(b"\n")
    return hasher.hexdigest()


def _hash_fields(hasher: Any, values: tuple[Any, ...]) -> None:
    for value in values:
        encoded = str(value).encode("utf-8")
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)
    hasher.update(b"\xff")


def _semantic_scalar(value: object) -> str:
    if value is None or pd.isna(value):
        return "<null>"
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bool, np.bool_)):
        return "true" if bool(value) else "false"
    if isinstance(value, (int, float, np.integer, np.floating)):
        converted = float(value)
        if not math.isfinite(converted):
            raise ExperimentError("semantic hashes reject NaN and Infinity")
        return converted.hex()
    if isinstance(value, str):
        return value
    raise ExperimentError(f"unsupported semantic hash value: {type(value).__name__}")


def _normalize(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normalize(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExperimentError("identity payload rejects NaN and Infinity")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ExperimentError(f"unsupported identity value: {type(value).__name__}")


def _canonical_json(payload: object) -> str:
    try:
        return json.dumps(
            _normalize(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ExperimentError("identity payload is not canonicalizable") from error


def _non_empty(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentError(f"{name} must be a non-empty string")


def _sha256(name: str, value: object, *, git: bool = False) -> None:
    _non_empty(name, value)
    expected_length = 40 if git else 64
    if len(value) != expected_length or any(
        character not in "0123456789abcdef" for character in value.lower()
    ):
        raise ExperimentError(f"{name} must be {expected_length} hexadecimal chars")


def _non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExperimentError(f"{name} must be a non-negative integer")


def _string_tuple(name: str, value: object, *, allow_empty: bool = False) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ExperimentError(f"{name} must be a tuple of non-empty strings")
    if not value and not allow_empty:
        raise ExperimentError(f"{name} must not be empty")


def _tuple_of(name: str, value: object, expected: type) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(item, expected) for item in value
    ):
        raise TypeError(f"{name} must be a tuple of {expected.__name__}")


def _require_date(name: str, value: object) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ExperimentError(f"{name} must be a date")


def _date_range(first: date | None, last: date | None) -> None:
    if (first is None) != (last is None):
        raise ExperimentError("actual date bounds must both be present or absent")
    if first is not None:
        _require_date("first date", first)
        _require_date("last date", last)
        if first > last:
            raise ExperimentError("actual date bounds are reversed")
