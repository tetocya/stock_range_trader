"""STEP 8 fixed report schema and atomic bundle tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from test_phase3_aggregation import _evaluated_fold, _fold, _observation, _run
from test_phase3_executable_evaluation import (
    _bars as executable_bars,
)
from test_phase3_executable_evaluation import (
    _base_config as executable_base_config,
)
from test_phase3_executable_evaluation import (
    _catalog as executable_catalog,
)
from test_phase3_executable_evaluation import (
    _evaluator as executable_evaluator,
)
from test_phase3_executable_evaluation import (
    _fold as executable_fold,
)
from test_phase3_executable_evaluation import (
    _install_constant_features,
)
from test_phase3_executable_test_evaluation import _test_trade_bars
from test_phase3_experiment_identity import _clean_source, _universe
from test_phase3_signal_evaluation import _bars

import reports.walk_forward_report as report_module
from config import (
    ExecutableCandidateCatalogConfig,
    ExecutableCandidateConfig,
    SignalCandidateCatalogConfig,
    SignalCandidateConfig,
    load_phase3_config,
    load_strategy_config,
)
from data import provider_price_basis
from reports import (
    COMMON_FILENAMES,
    EQUITY_COLUMNS,
    EXECUTABLE_FILENAMES,
    ORDER_COLUMNS,
    SIGNAL_FILENAMES,
    TRADE_COLUMNS,
    ExecutableWalkForwardReportBuilder,
    ExperimentAlreadyExistsError,
    SignalWalkForwardReportBuilder,
    WalkForwardReportWriter,
)
from walkforward import (
    AnalysisMode,
    CandidateAssessment,
    CandidateSelection,
    ExecutableFoldRunResult,
    ExecutableWalkForwardAggregator,
    ExecutableWalkForwardRunResult,
    ExperimentIdentityBuilder,
    ProviderCapabilityRegistry,
    SelectionStatus,
    SignalCandidateCatalog,
    SignalWalkForwardAggregator,
    WalkForwardRunMetadata,
    assess_formal_oos,
    assess_universe,
    build_config_artifact_fingerprint,
    build_input_artifact_fingerprint,
    sha256_file,
)
from walkforward import TestEvaluationStatus as EvaluationStatus

PROJECT_ROOT = Path(__file__).parents[1]


def _fixture(tmp_path: Path):
    fold = _fold("fold_0001", 1)
    run = _run(_evaluated_fold(fold, (_observation(fold, 2, 0.1, hit=True),)))
    aggregate = SignalWalkForwardAggregator().aggregate(run)
    phase3 = load_phase3_config(PROJECT_ROOT / "config" / "phase3.yaml")
    candidate_config = SignalCandidateCatalogConfig(
        maximum_candidates=12,
        candidates=(
            SignalCandidateConfig("candidate_b", 1.0, 70.0, 25.0),
            SignalCandidateConfig("candidate_a", 1.5, 75.0, 22.0),
            SignalCandidateConfig("never_selected", 2.0, 80.0, 20.0),
        ),
    )
    phase3 = replace(phase3, signal_candidate_catalog=candidate_config)
    catalog = SignalCandidateCatalog.from_config(candidate_config)
    strategy = load_strategy_config(PROJECT_ROOT / "config" / "strategy.yaml")
    dates = pd.bdate_range("2024-01-01", "2024-03-29")
    bars = _bars(dates=dates)
    input_path = tmp_path / "prices.parquet"
    bars.to_parquet(input_path, index=False)
    universe_frame = _universe()
    universe_path = tmp_path / "universe.csv"
    universe_frame.to_csv(universe_path, index=False)
    universe = assess_universe(
        universe_path,
        universe_frame,
        bars,
        provider="yfinance",
        schedule=run.schedule,
    )
    input_artifact = build_input_artifact_fingerprint(input_path, bars)
    source = _clean_source()
    identity = ExperimentIdentityBuilder().build(
        phase3_config=phase3,
        strategy_config=strategy,
        candidate_catalog=catalog,
        selection_policy=phase3.signal_selection,
        schedule=run.schedule,
        provider="yfinance",
        analysis_mode=AnalysisMode.SIGNAL_VALIDATION,
        provider_price_basis=provider_price_basis("yfinance"),
        input_artifact=input_artifact,
        universe=universe,
        source=source,
    )
    formal = assess_formal_oos(
        source=source,
        config=phase3,
        input_artifact=input_artifact,
        universe=universe,
        capability_allowed=True,
        output_collision=False,
        parent_experiment_id=None,
    )
    fixed_time = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    metadata = WalkForwardRunMetadata(
        identity=identity,
        source=source,
        input_artifact=input_artifact,
        universe=universe,
        provider_capability=ProviderCapabilityRegistry().require(
            "yfinance", AnalysisMode.SIGNAL_VALIDATION
        ),
        phase3_config=phase3,
        strategy_config=strategy,
        schedule=run.schedule,
        phase3_config_artifact=build_config_artifact_fingerprint(
            PROJECT_ROOT / "config" / "phase3.yaml"
        ),
        strategy_config_artifact=build_config_artifact_fingerprint(
            PROJECT_ROOT / "config" / "strategy.yaml"
        ),
        requested_start=run.schedule.configured_start,
        requested_end_exclusive=run.schedule.configured_end,
        formal_oos=formal,
        started_at_utc=fixed_time,
        completed_at_utc=fixed_time,
    )
    bundle = SignalWalkForwardReportBuilder().build(
        run, aggregate, metadata, bars, catalog
    )
    return bundle, run, aggregate, metadata, bars, catalog


def _executable_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_constant_features(monkeypatch)
    fold = executable_fold()
    catalog = executable_catalog()
    traded = _test_trade_bars("72030")
    idle = executable_bars("13010", signal_close=np.full(len(traded), 100.0))
    bars = pd.concat([idle, traded], ignore_index=True)
    evaluator = executable_evaluator()
    validation = evaluator.evaluate_validation(bars, fold, catalog)
    cohort_symbols = tuple(
        sorted(
            set(bars["symbol"].astype(str))
            - {item.symbol for item in validation.symbol_exclusions}
        )
    )
    from walkforward import ValidationCohort

    cohort = ValidationCohort(
        "jquants", provider_price_basis("jquants"), cohort_symbols
    )
    test = evaluator.evaluate_test(bars, fold, catalog.candidates[0], cohort)
    selection = CandidateSelection(
        analysis_mode=AnalysisMode.EXECUTABLE_VALIDATION,
        status=SelectionStatus.SELECTED,
        selected_candidate_id="baseline",
        ranked_candidate_ids=("baseline",),
        assessments=(CandidateAssessment("baseline", True, (), 1),),
    )
    fold_result = ExecutableFoldRunResult(
        fold=fold,
        validation_cohort=cohort,
        validation_result=validation,
        selection=selection,
        test_status=EvaluationStatus.EVALUATED,
        test_result=test,
    )
    from test_phase3_runner import _schedule

    schedule = _schedule(fold)
    run = ExecutableWalkForwardRunResult(
        provider="jquants",
        provider_price_basis=provider_price_basis("jquants"),
        schedule=schedule,
        fold_results=(fold_result,),
    )
    aggregate = ExecutableWalkForwardAggregator().aggregate(run)
    phase3 = load_phase3_config(PROJECT_ROOT / "config" / "phase3.yaml")
    candidate_config = ExecutableCandidateCatalogConfig(
        maximum_candidates=12,
        candidates=(ExecutableCandidateConfig("baseline", 0.5, 0.5, 70.0, 25.0),),
    )
    phase3 = replace(
        phase3,
        executable_candidate_catalog=candidate_config,
        executable_fold_schedule=schedule.config,
    )
    strategy = executable_base_config()
    universe_frame = _universe()
    universe_frame.loc[:, "universe_included"] = True
    universe_path = tmp_path / "executable_universe.csv"
    universe_frame.to_csv(universe_path, index=False)
    input_path = tmp_path / "executable_prices.parquet"
    bars.to_parquet(input_path, index=False)
    universe = assess_universe(
        universe_path,
        universe_frame,
        bars,
        provider="jquants",
        schedule=schedule,
    )
    input_artifact = build_input_artifact_fingerprint(input_path, bars)
    source = _clean_source()
    identity = ExperimentIdentityBuilder().build(
        phase3_config=phase3,
        strategy_config=strategy,
        candidate_catalog=catalog,
        selection_policy=phase3.executable_selection,
        schedule=schedule,
        provider="jquants",
        analysis_mode=AnalysisMode.EXECUTABLE_VALIDATION,
        provider_price_basis=provider_price_basis("jquants"),
        input_artifact=input_artifact,
        universe=universe,
        source=source,
    )
    formal = assess_formal_oos(
        source=source,
        config=phase3,
        input_artifact=input_artifact,
        universe=universe,
        capability_allowed=True,
        output_collision=False,
        parent_experiment_id=None,
    )
    fixed_time = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    metadata = WalkForwardRunMetadata(
        identity=identity,
        source=source,
        input_artifact=input_artifact,
        universe=universe,
        provider_capability=ProviderCapabilityRegistry().require(
            "jquants", AnalysisMode.EXECUTABLE_VALIDATION, require_benchmark=True
        ),
        phase3_config=phase3,
        strategy_config=strategy,
        schedule=schedule,
        phase3_config_artifact=build_config_artifact_fingerprint(
            PROJECT_ROOT / "config" / "phase3.yaml"
        ),
        strategy_config_artifact=build_config_artifact_fingerprint(
            PROJECT_ROOT / "config" / "strategy.yaml"
        ),
        requested_start=schedule.configured_start,
        requested_end_exclusive=schedule.configured_end,
        formal_oos=formal,
        started_at_utc=fixed_time,
        completed_at_utc=fixed_time,
    )
    bundle = ExecutableWalkForwardReportBuilder().build(
        run, aggregate, metadata, bars, catalog
    )
    return bundle, aggregate


def test_signal_builder_has_all_required_files_and_no_executable_files(
    tmp_path: Path,
) -> None:
    bundle, *_ = _fixture(tmp_path)
    filenames = {item.filename for item in bundle.tables}

    assert filenames == COMMON_FILENAMES | SIGNAL_FILENAMES
    assert not any(name.startswith("oos_executable") for name in filenames)
    signal_columns = {column for table in bundle.tables for column in table.columns}
    assert "commission" not in signal_columns
    assert "final_equity" not in signal_columns
    assert "shares" not in signal_columns


def test_empty_exclusion_table_retains_fixed_header(tmp_path: Path) -> None:
    bundle, *_ = _fixture(tmp_path)
    table = next(
        item for item in bundle.tables if item.filename == "walk_forward_exclusions.csv"
    )

    assert table.rows == ()
    assert table.columns[:4] == (
        "experiment_id",
        "analysis_mode",
        "provider",
        "provider_price_basis",
    )
    assert {"stage", "scope", "status", "reason"}.issubset(table.columns)


def test_candidate_frequency_preserves_catalog_order(tmp_path: Path) -> None:
    bundle, *_ = _fixture(tmp_path)
    table = next(
        item
        for item in bundle.tables
        if item.filename == "candidate_selection_frequency.csv"
    ).to_frame()

    assert list(table["candidate_id"]) == [
        "candidate_b",
        "candidate_a",
        "never_selected",
    ]
    assert list(table["selected_fold_count"]) == [1, 0, 0]


def test_test_aggregate_change_does_not_change_selected_parameters(
    tmp_path: Path,
) -> None:
    bundle, run, aggregate, metadata, bars, catalog = _fixture(tmp_path)
    changed = replace(aggregate, median_forward_return=99.0)
    changed_bundle = SignalWalkForwardReportBuilder().build(
        run, changed, metadata, bars, catalog
    )

    original = next(
        item for item in bundle.tables if item.filename == "selected_parameters.csv"
    )
    modified = next(
        item
        for item in changed_bundle.tables
        if item.filename == "selected_parameters.csv"
    )
    assert modified == original


def test_atomic_writer_records_artifact_hashes_without_self_hash(
    tmp_path: Path,
) -> None:
    bundle, *_ = _fixture(tmp_path)
    destination = WalkForwardReportWriter().write(bundle, tmp_path / "reports")
    manifest_path = destination / "walk_forward_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "phase3-report-1.0"
    assert manifest["status"] == "completed"
    assert "walk_forward_manifest.json" not in {
        item["filename"] for item in manifest["artifacts"]
    }
    for artifact in manifest["artifacts"]:
        assert sha256_file(destination / artifact["filename"]) == artifact["sha256"]
    assert "NaN" not in manifest_path.read_text(encoding="utf-8")
    assert "Infinity" not in manifest_path.read_text(encoding="utf-8")
    assert "git_root" not in manifest["source"]
    assert "/repo" not in manifest_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("filename", "columns"),
    (
        ("oos_trade_log.csv", TRADE_COLUMNS),
        ("oos_order_log.csv", ORDER_COLUMNS),
        ("oos_equity_curve.csv", EQUITY_COLUMNS),
    ),
)
def test_audit_csv_sequence_uses_numeric_order(
    filename: str, columns: tuple[str, ...]
) -> None:
    rows = [
        {"fold_id": "fold_0001", "symbol": "72030", "sequence": sequence}
        for sequence in reversed(range(12))
    ]

    table = report_module._table(
        filename,
        columns,
        rows,
        sort_by=("fold_id", "symbol", "sequence"),
    )

    assert list(table.to_frame()["sequence"]) == list(range(12))


def test_fixed_clock_manifest_is_byte_identical_across_output_roots(
    tmp_path: Path,
) -> None:
    bundle, *_ = _fixture(tmp_path)
    writer = WalkForwardReportWriter()
    first = writer.write(bundle, tmp_path / "first")
    second = writer.write(bundle, tmp_path / "second")

    assert (first / "walk_forward_manifest.json").read_bytes() == (
        second / "walk_forward_manifest.json"
    ).read_bytes()


def test_existing_experiment_is_never_overwritten(tmp_path: Path) -> None:
    bundle, *_ = _fixture(tmp_path)
    writer = WalkForwardReportWriter()
    destination = writer.write(bundle, tmp_path / "reports")
    sentinel = destination / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ExperimentAlreadyExistsError):
        writer.write(bundle, tmp_path / "reports")

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_mid_write_failure_leaves_no_completed_or_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, *_ = _fixture(tmp_path)
    output = tmp_path / "reports"
    calls = 0
    original = WalkForwardReportWriter._write_csv

    def fail_second(table, path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        return original(table, path)

    monkeypatch.setattr(
        WalkForwardReportWriter, "_write_csv", staticmethod(fail_second)
    )

    with pytest.raises(OSError, match="simulated"):
        WalkForwardReportWriter().write(bundle, output)

    assert not (output / bundle.experiment_id).exists()
    assert list(output.glob(".wf3-*")) == []


def test_manifest_write_failure_is_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, *_ = _fixture(tmp_path)
    output = tmp_path / "reports"

    def fail_manifest(manifest, path):
        raise OSError("simulated manifest failure")

    monkeypatch.setattr(
        WalkForwardReportWriter, "_write_manifest", staticmethod(fail_manifest)
    )

    with pytest.raises(OSError, match="manifest"):
        WalkForwardReportWriter().write(bundle, output)

    assert not (output / bundle.experiment_id).exists()
    assert list(output.glob(".wf3-*")) == []


def test_failure_receipt_is_atomic_and_does_not_store_exception_message(
    tmp_path: Path,
) -> None:
    writer = WalkForwardReportWriter()
    timestamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    secret = "JQUANTS_API_KEY=do-not-store"

    destination = writer.write_failure_receipt(
        output_dir=tmp_path,
        experiment_id="wf3-signal_validation-" + "a" * 64,
        analysis_mode=AnalysisMode.SIGNAL_VALIDATION,
        failed_stage="walk_forward_or_reporting",
        exception=RuntimeError(secret),
        started_at_utc=timestamp,
        failed_at_utc=timestamp,
        source_state="dirty",
        git_commit_sha="b" * 40,
    )

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["exception_type"] == "RuntimeError"
    assert secret not in destination.read_text(encoding="utf-8")
    assert not any(destination.parent.glob("*.tmp"))
    assert not any("return" in key or "profit" in key for key in payload)


def test_executable_aggregate_is_symbol_fold_distribution_and_keeps_zero_trade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, aggregate = _executable_fixture(tmp_path, monkeypatch)

    assert aggregate.requested_symbol_fold_count == 2
    assert aggregate.admitted_symbol_fold_count == 2
    assert aggregate.traded_symbol_fold_count == 1
    assert aggregate.total_trade_count == 1
    assert aggregate.median_symbol_fold_net_return == pytest.approx(0.1)
    assert not hasattr(aggregate, "final_equity")
    assert not hasattr(aggregate, "portfolio_sharpe_ratio")


def test_executable_builder_writes_audit_files_without_signal_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _ = _executable_fixture(tmp_path, monkeypatch)
    filenames = {item.filename for item in bundle.tables}

    assert filenames == COMMON_FILENAMES | EXECUTABLE_FILENAMES
    assert not filenames & SIGNAL_FILENAMES
    metrics = next(
        item for item in bundle.tables if item.filename == "oos_executable_metrics.csv"
    ).to_frame()
    trades = next(
        item for item in bundle.tables if item.filename == "oos_trade_log.csv"
    ).to_frame()
    orders = next(
        item for item in bundle.tables if item.filename == "oos_order_log.csv"
    ).to_frame()
    equity = next(
        item for item in bundle.tables if item.filename == "oos_equity_curve.csv"
    ).to_frame()

    assert len(metrics) == 2
    assert len(trades) == 1
    assert len(orders) == 2
    assert set(equity["symbol"]) == {"13010", "72030"}
    assert "sequence" in trades.columns
    assert "sequence" in orders.columns
    assert "sequence" in equity.columns
