"""Typed Phase 3 CSV tables, manifest, and atomic report-bundle publishing."""

from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from importlib import metadata as package_metadata
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pandas as pd

from data import (
    CANONICAL_SCHEMA_VERSION,
    price_policy_manifest_fields,
)
from walkforward import (
    AnalysisMode,
    ExecutableCandidateCatalog,
    ExecutableWalkForwardAggregate,
    ExecutableWalkForwardRunResult,
    SignalCandidateCatalog,
    SignalWalkForwardAggregate,
    SignalWalkForwardRunResult,
    resolve_fold_observation_bounds,
)
from walkforward.audit import (
    ExecutableTestEquityRecord,
    ExecutableTestOrderRecord,
    ExecutableTestTradeRecord,
    audit_record_columns,
)
from walkforward.experiment import (
    REPORT_SCHEMA_VERSION,
    SURVIVORSHIP_LIMITATION,
    WalkForwardRunMetadata,
    sha256_file,
)

PROVENANCE_COLUMNS: tuple[str, ...] = (
    "experiment_id",
    "analysis_mode",
    "provider",
    "provider_price_basis",
    "fold_id",
    "candidate_id",
    "train_start",
    "train_end",
    "validation_start",
    "validation_end",
    "test_start",
    "test_end",
    "universe_as_of_date",
    "temporal_oos",
    "point_in_time_universe",
    "survivorship_bias_status",
)

FOLDS_COLUMNS = PROVENANCE_COLUMNS + (
    "embargo_sessions",
    "forward_sessions",
    "purge_rule",
    "selection_status",
    "selected_candidate_id",
    "test_status",
    "validation_input_symbol_count",
    "validation_admitted_symbol_count",
    "test_requested_symbol_count",
    "test_admitted_symbol_count",
    "actual_train_first_date",
    "actual_train_last_date",
    "actual_validation_first_date",
    "actual_validation_last_date",
    "actual_test_first_date",
    "actual_test_last_date",
)
OBSERVATION_BOUNDS_COLUMNS = PROVENANCE_COLUMNS + (
    "symbol",
    "train_first_observation_date",
    "train_last_observation_date",
    "train_observation_count",
    "validation_first_observation_date",
    "validation_last_observation_date",
    "validation_observation_count",
    "test_first_observation_date",
    "test_last_observation_date",
    "test_observation_count",
)
VALIDATION_COHORT_COLUMNS = PROVENANCE_COLUMNS + ("symbol", "cohort_status")
UNIVERSE_COVERAGE_COLUMNS = PROVENANCE_COLUMNS + (
    "scope",
    "symbol",
    "in_universe",
    "price_row_count",
    "first_price_date",
    "last_price_date",
    "coverage_status",
)
SIGNAL_VALIDATION_COLUMNS = PROVENANCE_COLUMNS + (
    "observation_count",
    "mean_reversion_target_hit_rate",
    "median_forward_return",
    "median_mae_magnitude",
    "eligible",
    "rejection_reasons",
    "rank",
    "selected",
)
EXECUTABLE_VALIDATION_COLUMNS = PROVENANCE_COLUMNS + (
    "admitted_symbol_count",
    "traded_symbol_count",
    "trading_symbol_ratio",
    "total_trade_count",
    "finite_sharpe_count",
    "median_symbol_sharpe_ratio",
    "median_symbol_maximum_drawdown_magnitude",
    "worst_symbol_maximum_drawdown_magnitude",
    "median_symbol_net_return",
    "eligible",
    "rejection_reasons",
    "rank",
    "selected",
)
SIGNAL_SELECTED_PARAMETERS_COLUMNS = PROVENANCE_COLUMNS + (
    "selection_status",
    "selected_candidate_id",
    "buy_atr_multiplier",
    "range_score_threshold",
    "adx_entry_max",
)
EXECUTABLE_SELECTED_PARAMETERS_COLUMNS = PROVENANCE_COLUMNS + (
    "selection_status",
    "selected_candidate_id",
    "buy_atr_multiplier",
    "sell_atr_multiplier",
    "range_score_threshold",
    "adx_entry_max",
)
CANDIDATE_FREQUENCY_COLUMNS = PROVENANCE_COLUMNS + (
    "scope",
    "selected_fold_count",
    "selected_fraction_of_all_folds",
    "selected_fraction_of_evaluated_folds",
)
EXCLUSIONS_COLUMNS = PROVENANCE_COLUMNS + (
    "stage",
    "scope",
    "status",
    "reason",
    "symbol",
    "feature_date",
    "label_start_date",
    "label_end_date",
)
SIGNAL_SUMMARY_COLUMNS = PROVENANCE_COLUMNS + (
    "scope",
    "aggregate_status",
    "fold_count",
    "evaluated_fold_count",
    "no_eligible_fold_count",
    "test_observation_count",
    "unique_test_symbol_count",
    "mean_reversion_target_hit_rate",
    "median_forward_return",
    "median_mae_magnitude",
)
EXECUTABLE_SUMMARY_COLUMNS = PROVENANCE_COLUMNS + (
    "scope",
    "aggregate_status",
    "fold_count",
    "evaluated_fold_count",
    "no_eligible_fold_count",
    "requested_symbol_fold_count",
    "admitted_symbol_fold_count",
    "traded_symbol_fold_count",
    "total_trade_count",
    "finite_sharpe_symbol_fold_count",
    "median_symbol_fold_sharpe_ratio",
    "median_symbol_fold_maximum_drawdown_magnitude",
    "worst_symbol_fold_maximum_drawdown_magnitude",
    "median_symbol_fold_net_return",
)
SIGNAL_OBSERVATION_COLUMNS = PROVENANCE_COLUMNS + (
    "symbol",
    "feature_date",
    "label_start_date",
    "label_end_date",
    "signal_close",
    "signal_date_sma",
    "signal_date_atr",
    "buy_threshold",
    "range_score",
    "adx",
    "forward_return",
    "mean_reversion_target_hit",
    "maximum_adverse_excursion",
    "maximum_adverse_excursion_magnitude",
    "maximum_favorable_excursion",
)
SIGNAL_FOLD_SUMMARY_COLUMNS = PROVENANCE_COLUMNS + (
    "selection_status",
    "test_status",
    "observation_count",
    "mean_reversion_target_hit_rate",
    "median_forward_return",
    "median_mae_magnitude",
)
EXECUTABLE_METRICS_COLUMNS = PROVENANCE_COLUMNS + (
    "symbol",
    "test_first_observation_date",
    "test_last_observation_date",
    "initial_capital",
    "final_equity",
    "net_return",
    "maximum_drawdown_magnitude",
    "sharpe_ratio",
    "number_of_trades",
    "filled_order_count",
    "rejected_order_count",
    "canceled_order_count",
    "open_position_at_end",
    "theoretical_buy_and_hold_return",
    "executable_buy_and_hold_return",
    "strategy_vs_executable_buy_and_hold",
)
TRADE_COLUMNS = PROVENANCE_COLUMNS + audit_record_columns(ExecutableTestTradeRecord)[3:]
ORDER_COLUMNS = PROVENANCE_COLUMNS + audit_record_columns(ExecutableTestOrderRecord)[3:]
EQUITY_COLUMNS = (
    PROVENANCE_COLUMNS + audit_record_columns(ExecutableTestEquityRecord)[3:]
)

COMMON_FILENAMES = frozenset(
    {
        "walk_forward_folds.csv",
        "fold_observation_bounds.csv",
        "validation_cohort.csv",
        "universe_coverage.csv",
        "candidate_validation_results.csv",
        "selected_parameters.csv",
        "candidate_selection_frequency.csv",
        "walk_forward_exclusions.csv",
        "walk_forward_summary.csv",
    }
)
SIGNAL_FILENAMES = frozenset(
    {"oos_signal_observations.csv", "oos_signal_fold_summary.csv"}
)
EXECUTABLE_FILENAMES = frozenset(
    {
        "oos_executable_metrics.csv",
        "oos_trade_log.csv",
        "oos_order_log.csv",
        "oos_equity_curve.csv",
    }
)
_EXPERIMENT_ID_PATTERN = re.compile(
    r"^wf3-(signal_validation|executable_validation)-[0-9a-f]{64}$"
)


class ReportWriteError(RuntimeError):
    """Raised when a complete report bundle cannot be safely published."""


class ExperimentAlreadyExistsError(ReportWriteError):
    """Raised before analysis instead of overwriting an experiment bundle."""


@dataclass(frozen=True, slots=True)
class WalkForwardReportTable:
    """One immutable, fixed-schema CSV table."""

    filename: str
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]

    def __post_init__(self) -> None:
        if (
            not self.filename.endswith(".csv")
            or Path(self.filename).name != self.filename
        ):
            raise ReportWriteError("report table filename must be a plain CSV name")
        if not self.columns or len(self.columns) != len(set(self.columns)):
            raise ReportWriteError("report table columns must be non-empty and unique")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ReportWriteError("report table row width does not match columns")

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame.from_records(self.rows, columns=self.columns)


@dataclass(frozen=True, slots=True)
class WalkForwardReportBundle:
    """Completed tables and base manifest, with no execution-layer dependency."""

    experiment_id: str
    analysis_mode: AnalysisMode
    provider: str
    provider_price_basis: str
    tables: tuple[WalkForwardReportTable, ...]
    manifest: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.analysis_mode, AnalysisMode):
            raise TypeError("analysis_mode must be AnalysisMode")
        _validate_experiment_id(self.experiment_id, self.analysis_mode)
        for name in ("provider", "provider_price_basis"):
            if (
                not isinstance(getattr(self, name), str)
                or not getattr(self, name).strip()
            ):
                raise ReportWriteError(f"{name} must not be empty")
        if not isinstance(self.tables, tuple) or any(
            not isinstance(item, WalkForwardReportTable) for item in self.tables
        ):
            raise TypeError("tables must be a tuple of WalkForwardReportTable")
        filenames = tuple(item.filename for item in self.tables)
        if filenames != tuple(sorted(filenames)) or len(filenames) != len(
            set(filenames)
        ):
            raise ReportWriteError("report tables must be unique and filename-sorted")
        if not isinstance(self.manifest, Mapping):
            raise TypeError("manifest must be a mapping")


class SignalWalkForwardReportBuilder:
    """Build Signal-only report tables from one completed STEP 7 result."""

    def build(
        self,
        result: SignalWalkForwardRunResult,
        aggregate: SignalWalkForwardAggregate,
        metadata: WalkForwardRunMetadata,
        bars: pd.DataFrame,
        catalog: SignalCandidateCatalog,
    ) -> WalkForwardReportBundle:
        _validate_builder_inputs(result, aggregate, metadata, bars, catalog)
        tables = _common_tables(result, aggregate, metadata, bars, catalog)
        observation_rows: list[dict[str, object]] = []
        fold_summary_rows: list[dict[str, object]] = []
        for fold_result in result.fold_results:
            fold = fold_result.fold
            test = fold_result.test_result
            if test is not None:
                for item in test.observations:
                    observation_rows.append(
                        {
                            **_fold_provenance(metadata, fold, item.candidate_id),
                            **_dataclass_values(
                                item,
                                exclude={"fold_id", "provider", "candidate_id"},
                            ),
                        }
                    )
            summary = test.summary if test is not None else None
            fold_summary_rows.append(
                {
                    **_fold_provenance(
                        metadata,
                        fold,
                        fold_result.selection.selected_candidate_id,
                    ),
                    "selection_status": fold_result.selection.status,
                    "test_status": fold_result.test_status,
                    "observation_count": summary.observation_count if summary else None,
                    "mean_reversion_target_hit_rate": (
                        summary.mean_reversion_target_hit_rate if summary else None
                    ),
                    "median_forward_return": (
                        summary.median_forward_return if summary else None
                    ),
                    "median_mae_magnitude": (
                        summary.median_mae_magnitude if summary else None
                    ),
                }
            )
        tables.extend(
            (
                _table(
                    "oos_signal_observations.csv",
                    SIGNAL_OBSERVATION_COLUMNS,
                    observation_rows,
                    sort_by=("fold_id", "candidate_id", "symbol", "feature_date"),
                ),
                _table(
                    "oos_signal_fold_summary.csv",
                    SIGNAL_FOLD_SUMMARY_COLUMNS,
                    fold_summary_rows,
                    sort_by=("fold_id",),
                ),
            )
        )
        return _bundle(metadata, tables, result, aggregate)


class ExecutableWalkForwardReportBuilder:
    """Build independent symbol-fold reports from one completed STEP 7 result."""

    def build(
        self,
        result: ExecutableWalkForwardRunResult,
        aggregate: ExecutableWalkForwardAggregate,
        metadata: WalkForwardRunMetadata,
        bars: pd.DataFrame,
        catalog: ExecutableCandidateCatalog,
    ) -> WalkForwardReportBundle:
        _validate_builder_inputs(result, aggregate, metadata, bars, catalog)
        tables = _common_tables(result, aggregate, metadata, bars, catalog)
        metrics_rows: list[dict[str, object]] = []
        trade_rows: list[dict[str, object]] = []
        order_rows: list[dict[str, object]] = []
        equity_rows: list[dict[str, object]] = []
        for fold_result in result.fold_results:
            test = fold_result.test_result
            if test is None:
                continue
            fold = fold_result.fold
            for item in test.symbol_outcomes:
                metrics_rows.append(
                    {
                        **_fold_provenance(metadata, fold, item.candidate_id),
                        **_dataclass_values(
                            item,
                            exclude={"fold_id", "provider", "candidate_id"},
                        ),
                    }
                )
            for records, destination in (
                (test.trade_records, trade_rows),
                (test.order_records, order_rows),
                (test.equity_records, equity_rows),
            ):
                for item in records:
                    destination.append(
                        {
                            **_fold_provenance(metadata, fold, item.candidate_id),
                            **_dataclass_values(
                                item,
                                exclude={"fold_id", "provider", "candidate_id"},
                            ),
                        }
                    )
        tables.extend(
            (
                _table(
                    "oos_executable_metrics.csv",
                    EXECUTABLE_METRICS_COLUMNS,
                    metrics_rows,
                    sort_by=("fold_id", "symbol"),
                ),
                _table(
                    "oos_trade_log.csv",
                    TRADE_COLUMNS,
                    trade_rows,
                    sort_by=("fold_id", "symbol", "sequence"),
                ),
                _table(
                    "oos_order_log.csv",
                    ORDER_COLUMNS,
                    order_rows,
                    sort_by=("fold_id", "symbol", "sequence"),
                ),
                _table(
                    "oos_equity_curve.csv",
                    EQUITY_COLUMNS,
                    equity_rows,
                    sort_by=("fold_id", "symbol", "sequence"),
                ),
            )
        )
        return _bundle(metadata, tables, result, aggregate)


class WalkForwardReportWriter:
    """Atomically publish a complete immutable experiment directory."""

    def ensure_available(self, output_dir: str | Path, experiment_id: str) -> Path:
        _validate_experiment_id(experiment_id)
        root = Path(output_dir).expanduser()
        destination = root / experiment_id
        if destination.exists():
            raise ExperimentAlreadyExistsError(
                f"experiment output already exists: {destination}"
            )
        return destination

    def write(self, bundle: WalkForwardReportBundle, output_dir: str | Path) -> Path:
        if not isinstance(bundle, WalkForwardReportBundle):
            raise TypeError("bundle must be WalkForwardReportBundle")
        destination = self.ensure_available(output_dir, bundle.experiment_id)
        root = destination.parent
        root.mkdir(parents=True, exist_ok=True)
        self._validate_bundle(bundle)
        temporary = Path(tempfile.mkdtemp(prefix=".wf3-", dir=root))
        try:
            artifacts: list[dict[str, object]] = []
            for table in bundle.tables:
                output = temporary / table.filename
                self._write_csv(table, output)
                frame = table.to_frame()
                artifacts.append(
                    {
                        "filename": table.filename,
                        "sha256": sha256_file(output),
                        "size_bytes": output.stat().st_size,
                        "row_count": len(frame),
                        "columns": list(table.columns),
                    }
                )
            manifest = dict(bundle.manifest)
            manifest["artifacts"] = sorted(
                artifacts, key=lambda item: str(item["filename"])
            )
            manifest_path = temporary / "walk_forward_manifest.json"
            self._write_manifest(manifest, manifest_path)
            self._validate_written_files(bundle, temporary, manifest)
            if destination.exists():
                raise ExperimentAlreadyExistsError(
                    f"experiment output already exists: {destination}"
                )
            os.rename(temporary, destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return destination

    def write_failure_receipt(
        self,
        *,
        output_dir: str | Path,
        experiment_id: str,
        analysis_mode: AnalysisMode,
        failed_stage: str,
        exception: Exception,
        started_at_utc: datetime,
        failed_at_utc: datetime,
        source_state: str,
        git_commit_sha: str | None,
        fold_id: str | None = None,
    ) -> Path:
        if not isinstance(analysis_mode, AnalysisMode):
            raise TypeError("analysis_mode must be AnalysisMode")
        _validate_experiment_id(experiment_id, analysis_mode)
        failed_root = Path(output_dir).expanduser() / "_failed"
        failed_root.mkdir(parents=True, exist_ok=True)
        timestamp = failed_at_utc.strftime("%Y%m%dT%H%M%S%fZ")
        destination = failed_root / f"{experiment_id}-{timestamp}.json"
        receipt = {
            "status": "failed",
            "experiment_id": experiment_id,
            "analysis_mode": analysis_mode.value,
            "failed_stage": failed_stage,
            "fold_id": fold_id,
            "exception_type": type(exception).__name__,
            "safe_message": "walk-forward execution or reporting failed",
            "started_at_utc": started_at_utc.isoformat(),
            "failed_at_utc": failed_at_utc.isoformat(),
            "source_state": source_state,
            "git_commit_sha": git_commit_sha,
        }
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=failed_root
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            self._write_manifest(receipt, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    @staticmethod
    def _write_csv(table: WalkForwardReportTable, path: Path) -> None:
        table.to_frame().to_csv(path, index=False, lineterminator="\n")

    @staticmethod
    def _write_manifest(manifest: Mapping[str, object], path: Path) -> None:
        payload = _json_value(manifest)
        path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _validate_bundle(self, bundle: WalkForwardReportBundle) -> None:
        allowed_fold_ids, allowed_candidate_ids = _validate_manifest_contract(bundle)
        expected = COMMON_FILENAMES | (
            SIGNAL_FILENAMES
            if bundle.analysis_mode is AnalysisMode.SIGNAL_VALIDATION
            else EXECUTABLE_FILENAMES
        )
        actual = {table.filename for table in bundle.tables}
        if actual != expected:
            raise ReportWriteError(
                "report bundle files do not match the mode-specific schema"
            )
        forbidden = (
            EXECUTABLE_FILENAMES
            if bundle.analysis_mode is AnalysisMode.SIGNAL_VALIDATION
            else SIGNAL_FILENAMES
        )
        if actual & forbidden:
            raise ReportWriteError("report bundle contains files from another mode")
        expected_columns = _expected_columns(bundle.analysis_mode)
        for table in bundle.tables:
            if table.columns != expected_columns[table.filename]:
                raise ReportWriteError(f"{table.filename} column schema mismatch")
            frame = table.to_frame()
            _validate_table_provenance(frame, bundle)
            _validate_table_contract_ids(
                frame,
                allowed_fold_ids=allowed_fold_ids,
                allowed_candidate_ids=allowed_candidate_ids,
            )
            _validate_finite_table_values(table)
        if bundle.analysis_mode is AnalysisMode.SIGNAL_VALIDATION:
            forbidden_fragments = (
                "profit",
                "commission",
                "slippage",
                "fill",
                "shares",
                "benchmark",
                "final_equity",
            )
            signal_columns = {
                column for table in bundle.tables for column in table.columns
            }
            if any(
                fragment in column
                for fragment in forbidden_fragments
                for column in signal_columns
            ):
                raise ReportWriteError("Signal report contains executable P&L fields")

    @staticmethod
    def _validate_written_files(
        bundle: WalkForwardReportBundle,
        directory: Path,
        manifest: Mapping[str, object],
    ) -> None:
        expected = {table.filename for table in bundle.tables} | {
            "walk_forward_manifest.json"
        }
        actual = {item.name for item in directory.iterdir() if item.is_file()}
        if actual != expected:
            raise ReportWriteError("temporary report bundle is incomplete")
        for artifact in manifest["artifacts"]:
            path = directory / artifact["filename"]
            if sha256_file(path) != artifact["sha256"]:
                raise ReportWriteError("artifact hash mismatch before publication")


def _common_tables(result, aggregate, metadata, bars, catalog):
    fold_rows: list[dict[str, object]] = []
    bounds_rows: list[dict[str, object]] = []
    cohort_rows: list[dict[str, object]] = []
    validation_rows: list[dict[str, object]] = []
    selected_rows: list[dict[str, object]] = []
    exclusion_rows: list[dict[str, object]] = []
    candidate_by_id = {item.candidate_id: item for item in catalog.candidates}

    for fold_result in result.fold_results:
        fold = fold_result.fold
        fold_bounds = []
        for symbol in fold_result.validation_cohort.symbols:
            dates = bars.loc[bars["symbol"].astype(str) == symbol, "date"].dt.date
            bounds = resolve_fold_observation_bounds(fold, symbol, dates)
            fold_bounds.append(bounds)
            bounds_rows.append(
                {
                    **_fold_provenance(metadata, fold, None),
                    "symbol": symbol,
                    **_dataclass_values(bounds, exclude={"fold", "symbol"}),
                }
            )
            cohort_rows.append(
                {
                    **_fold_provenance(metadata, fold, None),
                    "symbol": symbol,
                    "cohort_status": "validation_admitted",
                }
            )
        test = fold_result.test_result
        fold_rows.append(
            {
                **_fold_provenance(
                    metadata, fold, fold_result.selection.selected_candidate_id
                ),
                "embargo_sessions": fold.embargo_sessions,
                "forward_sessions": result.schedule.config.forward_sessions,
                "purge_rule": result.schedule.config.purge_rule,
                "selection_status": fold_result.selection.status,
                "selected_candidate_id": fold_result.selection.selected_candidate_id,
                "test_status": fold_result.test_status,
                "validation_input_symbol_count": (
                    fold_result.validation_result.input_symbol_count
                ),
                "validation_admitted_symbol_count": (
                    fold_result.validation_result.admitted_symbol_count
                ),
                "test_requested_symbol_count": (
                    test.requested_symbol_count if test is not None else 0
                ),
                "test_admitted_symbol_count": (
                    test.admitted_symbol_count if test is not None else 0
                ),
                **_aggregate_actual_bounds(fold_bounds),
            }
        )
        assessment_by_id = {
            item.candidate_id: item for item in fold_result.selection.assessments
        }
        for score in fold_result.validation_result.scores:
            assessment = assessment_by_id[score.candidate_id]
            row = {
                **_fold_provenance(metadata, fold, score.candidate_id),
                **_dataclass_values(score, exclude={"candidate_id"}),
            }
            if hasattr(score, "trading_symbol_ratio"):
                row["trading_symbol_ratio"] = score.trading_symbol_ratio
            row.update(
                {
                    "eligible": assessment.eligible,
                    "rejection_reasons": json.dumps(
                        list(assessment.rejection_reasons),
                        separators=(",", ":"),
                    ),
                    "rank": assessment.rank,
                    "selected": (
                        fold_result.selection.selected_candidate_id
                        == score.candidate_id
                    ),
                }
            )
            validation_rows.append(row)
        selected = fold_result.selection.selected_candidate_id
        candidate = candidate_by_id.get(selected)
        parameters = (
            _dataclass_values(candidate, exclude={"candidate_id"})
            if candidate is not None
            else {}
        )
        selected_rows.append(
            {
                **_fold_provenance(metadata, fold, selected),
                "selection_status": fold_result.selection.status,
                "selected_candidate_id": selected,
                **parameters,
            }
        )
        exclusion_rows.extend(_fold_exclusion_rows(metadata, fold_result))

    coverage_rows = [
        {
            **_run_provenance(metadata, candidate_id=None),
            "scope": "universe_snapshot",
            **_dataclass_values(item),
        }
        for item in metadata.universe.coverage
    ]
    frequency_rows = [
        {
            **_run_provenance(metadata, candidate_id=item.candidate_id),
            "scope": "adaptive_walk_forward_procedure",
            **_dataclass_values(item, exclude={"candidate_id"}),
        }
        for item in aggregate.selected_candidate_counts
    ]
    summary_row = {
        **_run_provenance(metadata, candidate_id=None),
        "scope": "adaptive_walk_forward_procedure",
        **_dataclass_values(
            aggregate, exclude={"analysis_mode", "selected_candidate_counts"}
        ),
    }
    signal_mode = metadata.identity.analysis_mode is AnalysisMode.SIGNAL_VALIDATION
    return [
        _table("walk_forward_folds.csv", FOLDS_COLUMNS, fold_rows, ("fold_id",)),
        _table(
            "fold_observation_bounds.csv",
            OBSERVATION_BOUNDS_COLUMNS,
            bounds_rows,
            ("fold_id", "symbol"),
        ),
        _table(
            "validation_cohort.csv",
            VALIDATION_COHORT_COLUMNS,
            cohort_rows,
            ("fold_id", "symbol"),
        ),
        _table(
            "universe_coverage.csv",
            UNIVERSE_COVERAGE_COLUMNS,
            coverage_rows,
            ("symbol",),
        ),
        _table(
            "candidate_validation_results.csv",
            SIGNAL_VALIDATION_COLUMNS if signal_mode else EXECUTABLE_VALIDATION_COLUMNS,
            validation_rows,
            (),
        ),
        _table(
            "selected_parameters.csv",
            SIGNAL_SELECTED_PARAMETERS_COLUMNS
            if signal_mode
            else EXECUTABLE_SELECTED_PARAMETERS_COLUMNS,
            selected_rows,
            ("fold_id",),
        ),
        _table(
            "candidate_selection_frequency.csv",
            CANDIDATE_FREQUENCY_COLUMNS,
            frequency_rows,
            (),
        ),
        _table(
            "walk_forward_exclusions.csv",
            EXCLUSIONS_COLUMNS,
            exclusion_rows,
            ("fold_id", "stage", "scope", "symbol", "candidate_id", "feature_date"),
        ),
        _table(
            "walk_forward_summary.csv",
            SIGNAL_SUMMARY_COLUMNS if signal_mode else EXECUTABLE_SUMMARY_COLUMNS,
            [summary_row],
            ("scope",),
        ),
    ]


def _fold_exclusion_rows(metadata, fold_result):
    rows: list[dict[str, object]] = []
    validation = fold_result.validation_result
    fold = fold_result.fold
    for item in validation.symbol_exclusions:
        rows.append(
            {
                **_fold_provenance(metadata, fold, None),
                "stage": "validation",
                "scope": "symbol",
                "status": item.status,
                "reason": item.reason,
                "symbol": item.symbol,
                "feature_date": None,
                "label_start_date": None,
                "label_end_date": None,
            }
        )
    for item in getattr(validation, "observation_exclusions", ()):
        rows.append(
            {
                **_fold_provenance(metadata, fold, item.candidate_id),
                "stage": "validation",
                "scope": "observation",
                "status": "excluded",
                "reason": item.reason,
                "symbol": item.symbol,
                "feature_date": item.feature_date,
                "label_start_date": None,
                "label_end_date": None,
            }
        )
    test = fold_result.test_result
    if test is None:
        return rows
    for item in test.symbol_exclusions:
        rows.append(
            {
                **_fold_provenance(metadata, fold, test.candidate_id),
                "stage": "test",
                "scope": "symbol",
                "status": item.status,
                "reason": item.reason,
                "symbol": item.symbol,
                "feature_date": None,
                "label_start_date": None,
                "label_end_date": None,
            }
        )
    for item in getattr(test, "observation_exclusions", ()):
        rows.append(
            {
                **_fold_provenance(metadata, fold, item.candidate_id),
                "stage": "test",
                "scope": "observation",
                "status": "excluded",
                "reason": item.reason,
                "symbol": item.symbol,
                "feature_date": item.feature_date,
                "label_start_date": None,
                "label_end_date": None,
            }
        )
    return rows


def _aggregate_actual_bounds(bounds):
    result: dict[str, object] = {}
    for prefix in ("train", "validation", "test"):
        first_values = [
            getattr(item, f"{prefix}_first_observation_date")
            for item in bounds
            if getattr(item, f"{prefix}_first_observation_date") is not None
        ]
        last_values = [
            getattr(item, f"{prefix}_last_observation_date")
            for item in bounds
            if getattr(item, f"{prefix}_last_observation_date") is not None
        ]
        result[f"actual_{prefix}_first_date"] = (
            min(first_values) if first_values else None
        )
        result[f"actual_{prefix}_last_date"] = max(last_values) if last_values else None
    return result


def _bundle(metadata, tables, result, aggregate):
    ordered = tuple(sorted(tables, key=lambda item: item.filename))
    manifest = MappingProxyType(_manifest(metadata, result, aggregate, ordered))
    return WalkForwardReportBundle(
        experiment_id=metadata.identity.experiment_id,
        analysis_mode=metadata.identity.analysis_mode,
        provider=result.provider,
        provider_price_basis=result.provider_price_basis,
        tables=ordered,
        manifest=manifest,
    )


def _manifest(metadata, result, aggregate, tables):
    exclusion_table = next(
        table for table in tables if table.filename == "walk_forward_exclusions.csv"
    ).to_frame()
    exclusion_counts: list[dict[str, object]] = []
    if not exclusion_table.empty:
        grouped = exclusion_table.groupby(
            ["stage", "scope", "status", "reason"], dropna=False, sort=True
        ).size()
        exclusion_counts = [
            {
                "stage": key[0],
                "scope": key[1],
                "status": key[2],
                "reason": key[3],
                "count": int(count),
            }
            for key, count in grouped.items()
        ]
    capability = _json_value(metadata.provider_capability)
    mode = metadata.identity.analysis_mode
    provider_package = (
        "yfinance" if result.provider == "yfinance" else "jquants-api-client"
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "completed",
        "experiment": {
            "experiment_id": metadata.identity.experiment_id,
            "analysis_mode": mode.value,
            "temporal_oos": True,
            "point_in_time_universe": metadata.universe.point_in_time_universe,
            "survivorship_bias_status": metadata.universe.survivorship_bias_status,
            "formal_oos_eligible": metadata.formal_oos.eligible,
            "formal_oos_ineligibility_reasons": list(metadata.formal_oos.reasons),
            "formal_oos_claim_scope": metadata.formal_oos.claim_scope,
        },
        "source": {
            "source_state": metadata.source.source_state.value,
            "git_root": metadata.source.git_root,
            "git_commit_sha": metadata.source.git_commit_sha,
            "git_branch": metadata.source.git_branch,
            "worktree_dirty": metadata.source.worktree_dirty,
            "source_tree_sha256": metadata.source.source_tree_sha256,
            "reproducibility_status": metadata.source.reproducibility_status,
        },
        "runtime": {
            "started_at_utc": metadata.started_at_utc.isoformat(),
            "completed_at_utc": metadata.completed_at_utc.isoformat(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "stock_range_trader_version": _package_version("stock-range-trader"),
            "pandas_version": pd.__version__,
            "numpy_version": np.__version__,
            "pyarrow_version": _package_version("pyarrow"),
            "pyyaml_version": _package_version("PyYAML"),
            "provider_library_version": _package_version(provider_package),
        },
        "inputs": {
            "input_filename": metadata.input_artifact.filename,
            "requested_start": metadata.requested_start.isoformat(),
            "requested_end_exclusive": metadata.requested_end_exclusive.isoformat(),
            "actual_start": _csv_value(metadata.input_artifact.actual_start),
            "actual_end": _csv_value(metadata.input_artifact.actual_end),
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "input_file_sha256": metadata.input_artifact.file_sha256,
            "canonical_content_sha256": (
                metadata.input_artifact.canonical_content_sha256
            ),
            "row_count": metadata.input_artifact.row_count,
            "column_names": list(metadata.input_artifact.column_names),
        },
        "universe": {
            "universe_filename": metadata.universe.filename,
            "file_sha256": metadata.universe.file_sha256,
            "normalized_universe_sha256": (
                metadata.universe.normalized_universe_sha256
            ),
            "universe_as_of_date": metadata.universe.universe_as_of_date.isoformat(),
            "included_symbol_count": metadata.universe.included_symbol_count,
            "available_price_symbol_count": (
                metadata.universe.available_price_symbol_count
            ),
            "missing_price_symbol_count": metadata.universe.missing_price_symbol_count,
            "unexpected_price_symbol_count": (
                metadata.universe.unexpected_price_symbol_count
            ),
            "point_in_time_universe": metadata.universe.point_in_time_universe,
            "survivorship_bias_status": metadata.universe.survivorship_bias_status,
            "snapshot_timing_claim_limit": SURVIVORSHIP_LIMITATION,
            "fold_assessments": [
                _json_value(item) for item in metadata.universe.fold_assessments
            ],
            "coverage_file": "universe_coverage.csv",
        },
        "provider_capability": {
            **capability,
            "observed_input_actual_start": _csv_value(
                metadata.input_artifact.actual_start
            ),
            "observed_input_actual_end": _csv_value(metadata.input_artifact.actual_end),
        },
        "price_policy": dict(price_policy_manifest_fields(result.provider)),
        "configuration": {
            "phase3": _json_value(metadata.phase3_config),
            "base_strategy": _json_value(metadata.strategy_config),
            "candidates": _json_value(
                metadata.phase3_config.signal_candidate_catalog
                if mode is AnalysisMode.SIGNAL_VALIDATION
                else metadata.phase3_config.executable_candidate_catalog
            ),
            "random_seed": metadata.phase3_config.random_seed,
            "phase3_config_file": _dataclass_values(metadata.phase3_config_artifact),
            "strategy_config_file": _dataclass_values(
                metadata.strategy_config_artifact
            ),
        },
        "fold_schedule": _json_value(result.schedule),
        "selection_policy": _json_value(
            metadata.phase3_config.signal_selection
            if mode is AnalysisMode.SIGNAL_VALIDATION
            else metadata.phase3_config.executable_selection
        ),
        "test_policy": {
            "candidate_selection_information_set": "validation_only",
            "test_candidate_count_per_fold": "0_or_1",
            "fallback_after_test_failure": "forbidden",
            "test_result_used_for_reselection": False,
            "test_state_reset": True,
            "test_end_rule": "exclusive",
            "test_reexecution_for_reporting": False,
        },
        "results": {
            **_dataclass_values(
                aggregate, exclude={"analysis_mode", "selected_candidate_counts"}
            ),
            "candidate_selection_counts": [
                _dataclass_values(item) for item in aggregate.selected_candidate_counts
            ],
        },
        "exclusions": {
            "counts": exclusion_counts,
            "excluded_symbol_count": int(
                exclusion_table.loc[
                    exclusion_table["scope"] == "symbol", "symbol"
                ].nunique()
            ),
            "details_file": "walk_forward_exclusions.csv",
        },
        "artifacts": [],
        "lineage": {
            "parent_experiment_id": metadata.parent_experiment_id,
            "change_reason": metadata.change_reason,
        },
        "limitations": [
            "delistings_symbol_changes_and_trading_halts_not_fully_reconstructed",
            "future_universe_folds_have_survivorship_bias",
            "yfinance_is_signal_only",
            "yfinance_adjusted_values_may_include_distribution_effects",
            "jquants_free_plan_history_and_availability_lag_apply",
            "unsupported_corporate_action_symbols_are_excluded",
            "executable_results_are_independent_capital_symbol_fold_distributions",
            "no_shared_portfolio_is_reported",
            "taxes_are_not_modelled",
            "test_performance_does_not_guarantee_future_profit",
        ],
        "warnings": _warnings(metadata),
    }


def _warnings(metadata):
    warnings = [metadata.formal_oos.claim_scope, SURVIVORSHIP_LIMITATION]
    if not metadata.formal_oos.eligible:
        warnings.append("formal_oos_eligibility_not_satisfied")
    if not metadata.universe.point_in_time_universe:
        warnings.append("one_or_more_folds_use_a_future_universe_snapshot")
    return warnings


def _validate_builder_inputs(result, aggregate, metadata, bars, catalog):
    if not isinstance(metadata, WalkForwardRunMetadata):
        raise TypeError("metadata must be WalkForwardRunMetadata")
    if not isinstance(bars, pd.DataFrame):
        raise TypeError("bars must be a pandas DataFrame")
    mode = metadata.identity.analysis_mode
    if mode is AnalysisMode.SIGNAL_VALIDATION:
        expected = (
            SignalWalkForwardRunResult,
            SignalWalkForwardAggregate,
            SignalCandidateCatalog,
        )
    else:
        expected = (
            ExecutableWalkForwardRunResult,
            ExecutableWalkForwardAggregate,
            ExecutableCandidateCatalog,
        )
    for name, value, expected_type in zip(
        ("result", "aggregate", "catalog"),
        (result, aggregate, catalog),
        expected,
        strict=True,
    ):
        if not isinstance(value, expected_type):
            raise TypeError(f"{name} must be {expected_type.__name__}")
    if result.provider != metadata.provider_capability.provider:
        raise ReportWriteError("run provider differs from metadata capability")
    if result.provider_price_basis != metadata.provider_capability.provider_price_basis:
        raise ReportWriteError("run price basis differs from metadata capability")
    if result.schedule != metadata.schedule:
        raise ReportWriteError("run schedule differs from metadata")
    if (
        tuple(
            score.candidate_id
            for score in result.fold_results[0].validation_result.scores
        )
        != catalog.candidate_ids
    ):
        raise ReportWriteError("run candidate order differs from catalog")


def _expected_columns(mode: AnalysisMode) -> dict[str, tuple[str, ...]]:
    common = {
        "walk_forward_folds.csv": FOLDS_COLUMNS,
        "fold_observation_bounds.csv": OBSERVATION_BOUNDS_COLUMNS,
        "validation_cohort.csv": VALIDATION_COHORT_COLUMNS,
        "universe_coverage.csv": UNIVERSE_COVERAGE_COLUMNS,
        "candidate_selection_frequency.csv": CANDIDATE_FREQUENCY_COLUMNS,
        "walk_forward_exclusions.csv": EXCLUSIONS_COLUMNS,
    }
    if mode is AnalysisMode.SIGNAL_VALIDATION:
        return {
            **common,
            "candidate_validation_results.csv": SIGNAL_VALIDATION_COLUMNS,
            "selected_parameters.csv": SIGNAL_SELECTED_PARAMETERS_COLUMNS,
            "walk_forward_summary.csv": SIGNAL_SUMMARY_COLUMNS,
            "oos_signal_observations.csv": SIGNAL_OBSERVATION_COLUMNS,
            "oos_signal_fold_summary.csv": SIGNAL_FOLD_SUMMARY_COLUMNS,
        }
    return {
        **common,
        "candidate_validation_results.csv": EXECUTABLE_VALIDATION_COLUMNS,
        "selected_parameters.csv": EXECUTABLE_SELECTED_PARAMETERS_COLUMNS,
        "walk_forward_summary.csv": EXECUTABLE_SUMMARY_COLUMNS,
        "oos_executable_metrics.csv": EXECUTABLE_METRICS_COLUMNS,
        "oos_trade_log.csv": TRADE_COLUMNS,
        "oos_order_log.csv": ORDER_COLUMNS,
        "oos_equity_curve.csv": EQUITY_COLUMNS,
    }


def _validate_manifest_contract(
    bundle: WalkForwardReportBundle,
) -> tuple[frozenset[str], frozenset[str]]:
    required_sections = {
        "schema_version",
        "status",
        "experiment",
        "source",
        "runtime",
        "inputs",
        "universe",
        "provider_capability",
        "price_policy",
        "configuration",
        "fold_schedule",
        "selection_policy",
        "test_policy",
        "results",
        "exclusions",
        "artifacts",
        "lineage",
        "limitations",
        "warnings",
    }
    missing = sorted(required_sections - set(bundle.manifest))
    if missing:
        raise ReportWriteError(
            "walk-forward manifest missing sections: " + ", ".join(missing)
        )
    if bundle.manifest["schema_version"] != REPORT_SCHEMA_VERSION:
        raise ReportWriteError("walk-forward manifest schema version mismatch")
    if bundle.manifest["status"] != "completed":
        raise ReportWriteError("walk-forward manifest status must be completed")

    experiment = _require_manifest_mapping(bundle.manifest, "experiment")
    capability = _require_manifest_mapping(bundle.manifest, "provider_capability")
    price_policy = _require_manifest_mapping(bundle.manifest, "price_policy")
    if (
        experiment.get("experiment_id") != bundle.experiment_id
        or experiment.get("analysis_mode") != bundle.analysis_mode.value
        or capability.get("provider") != bundle.provider
        or capability.get("provider_price_basis") != bundle.provider_price_basis
        or price_policy.get("provider_price_basis") != bundle.provider_price_basis
    ):
        raise ReportWriteError("manifest provenance differs from report bundle")

    schedule = _require_manifest_mapping(bundle.manifest, "fold_schedule")
    raw_folds = schedule.get("folds")
    if not isinstance(raw_folds, list) or not raw_folds:
        raise ReportWriteError("manifest fold schedule must contain folds")
    fold_ids = tuple(
        item.get("fold_id") if isinstance(item, Mapping) else None for item in raw_folds
    )
    if any(not isinstance(value, str) or not value for value in fold_ids) or len(
        fold_ids
    ) != len(set(fold_ids)):
        raise ReportWriteError("manifest fold IDs must be non-empty and unique")

    configuration = _require_manifest_mapping(bundle.manifest, "configuration")
    catalog = configuration.get("candidates")
    if not isinstance(catalog, Mapping):
        raise ReportWriteError("manifest candidate catalog is invalid")
    raw_candidates = catalog.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ReportWriteError("manifest candidate catalog must contain candidates")
    candidate_ids = tuple(
        item.get("id") if isinstance(item, Mapping) else None for item in raw_candidates
    )
    if any(not isinstance(value, str) or not value for value in candidate_ids) or len(
        candidate_ids
    ) != len(set(candidate_ids)):
        raise ReportWriteError("manifest candidate IDs must be non-empty and unique")
    return frozenset(fold_ids), frozenset(candidate_ids)


def _validate_experiment_id(
    experiment_id: object, analysis_mode: AnalysisMode | None = None
) -> None:
    if not isinstance(experiment_id, str):
        raise ReportWriteError("experiment_id must be a string")
    match = _EXPERIMENT_ID_PATTERN.fullmatch(experiment_id)
    if match is None:
        raise ReportWriteError("experiment_id does not match the Phase 3 format")
    if analysis_mode is not None and match.group(1) != analysis_mode.value:
        raise ReportWriteError("experiment_id analysis mode does not match the bundle")


def _require_manifest_mapping(
    manifest: Mapping[str, object], section: str
) -> Mapping[str, object]:
    value = manifest.get(section)
    if not isinstance(value, Mapping):
        raise ReportWriteError(f"manifest {section} section must be a mapping")
    return value


def _validate_table_contract_ids(
    frame: pd.DataFrame,
    *,
    allowed_fold_ids: frozenset[str],
    allowed_candidate_ids: frozenset[str],
) -> None:
    fold_ids = set(frame["fold_id"].dropna().astype(str))
    if not fold_ids.issubset(allowed_fold_ids):
        raise ReportWriteError("report table contains a fold outside the schedule")
    for column in ("candidate_id", "selected_candidate_id"):
        if column not in frame:
            continue
        candidate_ids = set(frame[column].dropna().astype(str))
        if not candidate_ids.issubset(allowed_candidate_ids):
            raise ReportWriteError(
                "report table contains a candidate outside the catalog"
            )


def _validate_finite_table_values(table: WalkForwardReportTable) -> None:
    for row in table.rows:
        for value in row:
            if isinstance(value, (float, np.floating)) and not math.isfinite(
                float(value)
            ):
                raise ReportWriteError("report table values reject NaN and Infinity")


def _validate_table_provenance(
    frame: pd.DataFrame, bundle: WalkForwardReportBundle
) -> None:
    for column, expected in (
        ("experiment_id", bundle.experiment_id),
        ("analysis_mode", bundle.analysis_mode.value),
        ("provider", bundle.provider),
        ("provider_price_basis", bundle.provider_price_basis),
    ):
        observed = set(frame[column].dropna().astype(str))
        if observed and observed != {expected}:
            raise ReportWriteError(f"table {column} provenance mismatch")


def _fold_provenance(metadata, fold, candidate_id):
    assessment = next(
        item
        for item in metadata.universe.fold_assessments
        if item.fold_id == fold.fold_id
    )
    return {
        "experiment_id": metadata.identity.experiment_id,
        "analysis_mode": metadata.identity.analysis_mode.value,
        "provider": metadata.provider_capability.provider,
        "provider_price_basis": metadata.provider_capability.provider_price_basis,
        "fold_id": fold.fold_id,
        "candidate_id": candidate_id,
        "train_start": fold.train_start,
        "train_end": fold.train_end,
        "validation_start": fold.validation_start,
        "validation_end": fold.validation_end,
        "test_start": fold.test_start,
        "test_end": fold.test_end,
        "universe_as_of_date": metadata.universe.universe_as_of_date,
        "temporal_oos": assessment.temporal_oos,
        "point_in_time_universe": assessment.point_in_time_universe,
        "survivorship_bias_status": assessment.survivorship_bias_status,
    }


def _run_provenance(metadata, candidate_id):
    return {
        "experiment_id": metadata.identity.experiment_id,
        "analysis_mode": metadata.identity.analysis_mode.value,
        "provider": metadata.provider_capability.provider,
        "provider_price_basis": metadata.provider_capability.provider_price_basis,
        "fold_id": None,
        "candidate_id": candidate_id,
        "train_start": None,
        "train_end": None,
        "validation_start": None,
        "validation_end": None,
        "test_start": None,
        "test_end": None,
        "universe_as_of_date": metadata.universe.universe_as_of_date,
        "temporal_oos": True,
        "point_in_time_universe": metadata.universe.point_in_time_universe,
        "survivorship_bias_status": metadata.universe.survivorship_bias_status,
    }


def _table(filename, columns, rows, sort_by):
    normalized = [
        {column: _csv_value(row.get(column)) for column in columns} for row in rows
    ]
    normalized.sort(
        key=lambda row: tuple(
            (row.get(column) is None, str(row.get(column) or "")) for column in sort_by
        )
    )
    return WalkForwardReportTable(
        filename=filename,
        columns=columns,
        rows=tuple(tuple(row[column] for column in columns) for row in normalized),
    )


def _dataclass_values(value, *, exclude=frozenset()):
    return {
        field.name: _csv_value(getattr(value, field.name))
        for field in fields(value)
        if field.name not in exclude
    }


def _csv_value(value):
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, (bool, np.bool_)):
        return "true" if bool(value) else "false"
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ReportWriteError("report values reject NaN and Infinity")
    return value


def _json_value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    converted = _csv_value(value)
    if converted in {"true", "false"} and isinstance(value, (bool, np.bool_)):
        return bool(value)
    return converted


def _package_version(name: str) -> str | None:
    try:
        return package_metadata.version(name)
    except package_metadata.PackageNotFoundError:
        return None
