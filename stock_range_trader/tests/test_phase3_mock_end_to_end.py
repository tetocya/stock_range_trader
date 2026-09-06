"""STEP 10 confirmed, deterministic, network-free Phase 3 acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from phase3_mock_e2e_helpers import (
    CANDIDATE_IDS,
    FIXED_UTC,
    confirmed_arguments,
    expected_schemas,
    install_deterministic_offline_runtime,
    single_bundle,
    write_mock_phase3_inputs,
)

from backtest import BacktestEngine
from data import provider_price_basis
from examples.run_walk_forward_executable import main as executable_main
from examples.run_walk_forward_signal import main as signal_main
from reports import EXECUTABLE_FILENAMES, SIGNAL_FILENAMES
from screening import RangeDetector, RangeScorer
from walkforward import (
    AnalysisMode,
    ExecutableCandidateSelector,
    ExecutableOutcomeEvaluator,
    ExecutableWalkForwardRunner,
    ProviderCapabilityError,
    sha256_file,
)


def test_confirmed_signal_mock_e2e_is_complete_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = write_mock_phase3_inputs(tmp_path, AnalysisMode.SIGNAL_VALIDATION)
    network_calls = install_deterministic_offline_runtime(monkeypatch)
    input_bars = pd.read_parquet(inputs.input_path)
    assert not input_bars["raw_close"].equals(input_bars["adjusted_close"])

    first = _run_confirmed(inputs.mode, inputs, tmp_path / "signal_first")
    second = _run_confirmed(inputs.mode, inputs, tmp_path / "signal_second")
    manifest, tables = _assert_bundle_contract(first, inputs.mode, tmp_path)

    folds = tables["walk_forward_folds.csv"]
    validation = tables["candidate_validation_results.csv"]
    selected = tables["selected_parameters.csv"]
    observations = tables["oos_signal_observations.csv"]
    fold_summary = tables["oos_signal_fold_summary.csv"]

    assert len(folds) == folds["fold_id"].nunique() == 2
    _assert_test_folds_do_not_overlap(folds)
    _assert_every_candidate_was_validated(validation)
    assert (validation["observation_count"] > 0).all()
    assert len(selected) == 2
    assert set(selected["selection_status"]) == {"selected"}
    assert selected["selected_candidate_id"].notna().all()
    assert not observations.empty
    assert len(fold_summary) == 2
    assert (fold_summary["observation_count"] > 0).all()
    _assert_only_selected_candidate_reaches_test(selected, observations)
    _assert_signal_observations_stay_in_test_windows(folds, observations)

    summary = tables["walk_forward_summary.csv"].iloc[0]
    assert summary["aggregate_status"] == "completed_all_folds_evaluated"
    assert int(summary["fold_count"]) == 2
    assert int(summary["evaluated_fold_count"]) == 2
    assert int(summary["test_observation_count"]) == len(observations)
    assert int(summary["unique_test_symbol_count"]) == 3
    assert manifest["results"]["test_observation_count"] == len(observations)
    assert "total_trade_count" not in manifest["results"]
    assert manifest["test_policy"]["test_candidate_count_per_fold"] == "0_or_1"

    signal_columns = {column for frame in tables.values() for column in frame.columns}
    for forbidden in (
        "profit",
        "commission",
        "slippage",
        "fill",
        "shares",
        "benchmark",
        "final_equity",
    ):
        assert not any(forbidden in column for column in signal_columns)
    assert (
        "sell_atr_multiplier"
        not in manifest["configuration"]["candidates"]["candidates"][0]
    )
    assert not ({item.name for item in first.iterdir()} & EXECUTABLE_FILENAMES)
    _assert_byte_deterministic(first, second)
    assert network_calls == []


def test_confirmed_executable_mock_e2e_has_coherent_trades_and_is_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = write_mock_phase3_inputs(tmp_path, AnalysisMode.EXECUTABLE_VALIDATION)
    network_calls = install_deterministic_offline_runtime(monkeypatch)
    input_bars = pd.read_parquet(inputs.input_path)
    for name in ("open", "high", "low", "close", "volume"):
        assert input_bars[f"raw_{name}"].equals(input_bars[f"adjusted_{name}"])

    first = _run_confirmed(inputs.mode, inputs, tmp_path / "executable_first")
    second = _run_confirmed(inputs.mode, inputs, tmp_path / "executable_second")
    manifest, tables = _assert_bundle_contract(first, inputs.mode, tmp_path)

    folds = tables["walk_forward_folds.csv"]
    validation = tables["candidate_validation_results.csv"]
    selected = tables["selected_parameters.csv"]
    metrics = tables["oos_executable_metrics.csv"]
    trades = tables["oos_trade_log.csv"]
    orders = tables["oos_order_log.csv"]
    equity = tables["oos_equity_curve.csv"]

    assert len(folds) == folds["fold_id"].nunique() == 2
    _assert_test_folds_do_not_overlap(folds)
    _assert_every_candidate_was_validated(validation)
    assert len(selected) == 2
    assert set(selected["selection_status"]) == {"selected"}
    assert len(metrics) == 6
    assert set(metrics["initial_capital"]) == {1_000_000.0}
    assert metrics.groupby("fold_id")["candidate_id"].nunique().eq(1).all()
    _assert_only_selected_candidate_reaches_test(selected, metrics)
    assert not trades.empty
    assert (metrics["number_of_trades"] > 0).any()
    assert int(metrics["filled_order_count"].sum()) >= 2
    assert (orders["status"] == "filled").sum() >= 2
    assert {"buy", "sell"}.issubset(
        set(orders.loc[orders["status"] == "filled", "side"])
    )
    assert not equity.empty
    _assert_executable_audit_consistency(metrics, trades, orders, equity)

    summary = tables["walk_forward_summary.csv"].iloc[0]
    assert summary["aggregate_status"] == "completed_all_folds_evaluated"
    assert int(summary["fold_count"]) == 2
    assert int(summary["evaluated_fold_count"]) == 2
    assert int(summary["total_trade_count"]) == len(trades)
    assert manifest["results"]["total_trade_count"] == len(trades)
    assert "test_observation_count" not in manifest["results"]
    assert manifest["test_policy"]["test_candidate_count_per_fold"] == "0_or_1"

    assert {
        "theoretical_buy_and_hold_return",
        "executable_buy_and_hold_return",
        "strategy_vs_executable_buy_and_hold",
    }.issubset(metrics.columns)
    assert "portfolio_return" not in tables["walk_forward_summary.csv"].columns
    assert not ({item.name for item in first.iterdir()} & SIGNAL_FILENAMES)
    _assert_byte_deterministic(first, second)
    assert network_calls == []


def test_actual_no_eligible_path_publishes_completed_fixed_schema_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = write_mock_phase3_inputs(
        tmp_path,
        AnalysisMode.SIGNAL_VALIDATION,
        no_eligible_signal_candidate=True,
    )
    network_calls = install_deterministic_offline_runtime(monkeypatch)

    bundle = _run_confirmed(inputs.mode, inputs, tmp_path / "no_eligible")
    manifest, tables = _assert_bundle_contract(bundle, inputs.mode, tmp_path)

    validation = tables["candidate_validation_results.csv"]
    selected = tables["selected_parameters.csv"]
    observations = tables["oos_signal_observations.csv"]
    fold_summary = tables["oos_signal_fold_summary.csv"]
    folds = tables["walk_forward_folds.csv"]
    summary = tables["walk_forward_summary.csv"].iloc[0]

    _assert_every_candidate_was_validated(validation)
    assert (validation["observation_count"] > 0).all()
    assert not validation["eligible"].astype(bool).any()
    assert (
        validation["rejection_reasons"]
        .str.contains("insufficient_observation_count")
        .all()
    )
    assert set(selected["selection_status"]) == {"no_eligible_candidate"}
    assert selected["selected_candidate_id"].isna().all()
    assert observations.empty
    assert len(fold_summary) == 2
    assert set(folds["test_status"]) == {"not_run_no_eligible_candidate"}
    assert summary["aggregate_status"] == "completed_no_test_folds"
    assert int(summary["no_eligible_fold_count"]) == 2
    assert int(summary["test_observation_count"]) == 0
    assert manifest["results"]["no_eligible_fold_count"] == 2
    assert not (bundle.parent / "_failed").exists()
    assert network_calls == []


def test_confirmed_yfinance_executable_is_fail_closed_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = write_mock_phase3_inputs(tmp_path, AnalysisMode.SIGNAL_VALIDATION)
    network_calls = install_deterministic_offline_runtime(monkeypatch)
    output_dir = tmp_path / "forbidden_yfinance_executable"
    computation_calls: list[str] = []

    def forbidden_computation(*args: object, **kwargs: object) -> None:
        computation_calls.append("called")
        raise AssertionError("Executable computation started before capability gate")

    for target, method in (
        (RangeDetector, "transform"),
        (RangeScorer, "transform"),
        (ExecutableOutcomeEvaluator, "evaluate_validation"),
        (ExecutableCandidateSelector, "select"),
        (ExecutableWalkForwardRunner, "run"),
        (BacktestEngine, "run"),
    ):
        monkeypatch.setattr(target, method, forbidden_computation)

    with pytest.raises(ProviderCapabilityError, match="does not support"):
        executable_main(confirmed_arguments(inputs, output_dir))

    assert not output_dir.exists()
    assert computation_calls == []
    assert network_calls == []


def _run_confirmed(mode, inputs, output_dir: Path) -> Path:
    main = signal_main if mode is AnalysisMode.SIGNAL_VALIDATION else executable_main
    assert main(confirmed_arguments(inputs, output_dir)) == 0
    return single_bundle(output_dir)


def _assert_bundle_contract(
    bundle: Path,
    mode: AnalysisMode,
    temporary_root: Path,
) -> tuple[dict[str, object], dict[str, pd.DataFrame]]:
    schemas = expected_schemas(mode)
    expected_files = set(schemas) | {"walk_forward_manifest.json"}
    assert {item.name for item in bundle.iterdir() if item.is_file()} == expected_files
    assert len(expected_files) == (12 if mode is AnalysisMode.SIGNAL_VALIDATION else 14)

    manifest_path = bundle / "walk_forward_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    provider = "yfinance" if mode is AnalysisMode.SIGNAL_VALIDATION else "jquants"
    experiment = manifest["experiment"]
    capability = manifest["provider_capability"]
    assert manifest["status"] == "completed"
    assert experiment["experiment_id"] == bundle.name
    assert experiment["analysis_mode"] == mode.value
    assert experiment["formal_oos_eligible"] is True
    assert capability["provider"] == provider
    assert capability["provider_price_basis"] == provider_price_basis(provider)
    assert manifest["price_policy"]["provider_price_basis"] == (
        provider_price_basis(provider)
    )
    assert manifest["source"]["source_state"] == "clean"
    assert manifest["source"]["git_commit_sha"] == "a" * 40
    assert manifest["runtime"]["started_at_utc"] == FIXED_UTC.isoformat()
    assert manifest["runtime"]["completed_at_utc"] == FIXED_UTC.isoformat()

    tables = {name: pd.read_csv(bundle / name) for name in schemas}
    artifacts = {item["filename"]: item for item in manifest["artifacts"]}
    assert set(artifacts) == set(schemas)
    for filename, columns in schemas.items():
        frame = tables[filename]
        artifact = artifacts[filename]
        path = bundle / filename
        assert tuple(frame.columns) == columns
        assert artifact["columns"] == list(columns)
        assert artifact["row_count"] == len(frame)
        assert artifact["size_bytes"] == path.stat().st_size
        assert artifact["sha256"] == sha256_file(path)
        if not frame.empty:
            assert set(frame["experiment_id"]) == {bundle.name}
            assert set(frame["analysis_mode"]) == {mode.value}
            assert set(frame["provider"]) == {provider}
            assert set(frame["provider_price_basis"]) == {
                provider_price_basis(provider)
            }

    serialized = "".join(
        (bundle / filename).read_text(encoding="utf-8")
        for filename in sorted(expected_files)
    )
    assert str(temporary_root) not in serialized
    assert str(Path.home()) not in serialized
    for forbidden in (
        "JQUANTS_API_KEY",
        "api_key",
        "access_token",
        "exception_type",
        "failed_stage",
        "safe_message",
    ):
        assert forbidden not in serialized
    assert not (bundle.parent / "_failed").exists()
    assert {item.name for item in bundle.parent.iterdir()} == {bundle.name}
    return manifest, tables


def _assert_every_candidate_was_validated(validation: pd.DataFrame) -> None:
    assert set(validation["candidate_id"]) == set(CANDIDATE_IDS)
    assert validation.groupby("fold_id").size().eq(len(CANDIDATE_IDS)).all()
    assert (
        validation.groupby("fold_id")["candidate_id"]
        .apply(lambda values: set(values) == set(CANDIDATE_IDS))
        .all()
    )


def _assert_only_selected_candidate_reaches_test(
    selected: pd.DataFrame,
    test_rows: pd.DataFrame,
) -> None:
    selected_by_fold = dict(
        zip(
            selected["fold_id"],
            selected["selected_candidate_id"],
            strict=True,
        )
    )
    assert set(test_rows["fold_id"]) == set(selected_by_fold)
    for fold_id, rows in test_rows.groupby("fold_id", sort=True):
        assert set(rows["candidate_id"]) == {selected_by_fold[fold_id]}


def _assert_signal_observations_stay_in_test_windows(
    folds: pd.DataFrame,
    observations: pd.DataFrame,
) -> None:
    for fold in folds.itertuples(index=False):
        dates = pd.to_datetime(
            observations.loc[
                observations["fold_id"] == fold.fold_id,
                "feature_date",
            ]
        )
        assert (dates >= pd.Timestamp(fold.test_start)).all()
        assert (dates < pd.Timestamp(fold.test_end)).all()


def _assert_test_folds_do_not_overlap(folds: pd.DataFrame) -> None:
    ordered = folds.sort_values("test_start")
    previous_end = pd.to_datetime(ordered["test_end"]).iloc[:-1].reset_index(drop=True)
    next_start = pd.to_datetime(ordered["test_start"]).iloc[1:].reset_index(drop=True)
    assert (previous_end <= next_start).all()


def _assert_executable_audit_consistency(
    metrics: pd.DataFrame,
    trades: pd.DataFrame,
    orders: pd.DataFrame,
    equity: pd.DataFrame,
) -> None:
    key_columns = ["fold_id", "candidate_id", "symbol"]
    for metric in metrics.itertuples(index=False):
        mask = (
            (trades["fold_id"] == metric.fold_id)
            & (trades["candidate_id"] == metric.candidate_id)
            & (trades["symbol"].astype(str) == str(metric.symbol))
        )
        symbol_trades = trades.loc[mask]
        mask = (
            (orders["fold_id"] == metric.fold_id)
            & (orders["candidate_id"] == metric.candidate_id)
            & (orders["symbol"].astype(str) == str(metric.symbol))
        )
        symbol_orders = orders.loc[mask]
        mask = (
            (equity["fold_id"] == metric.fold_id)
            & (equity["candidate_id"] == metric.candidate_id)
            & (equity["symbol"].astype(str) == str(metric.symbol))
        )
        symbol_equity = equity.loc[mask].sort_values("sequence")

        assert len(symbol_trades) == int(metric.number_of_trades)
        assert (symbol_orders["status"] == "filled").sum() == int(
            metric.filled_order_count
        )
        assert (symbol_orders["status"] == "rejected").sum() == int(
            metric.rejected_order_count
        )
        assert (symbol_orders["status"] == "canceled").sum() == int(
            metric.canceled_order_count
        )
        assert list(symbol_trades["sequence"]) == list(range(len(symbol_trades)))
        assert list(symbol_orders["sequence"]) == list(range(len(symbol_orders)))
        assert list(symbol_equity["sequence"]) == list(range(len(symbol_equity)))
        assert symbol_equity.iloc[-1]["total_equity"] == pytest.approx(
            metric.final_equity
        )
    assert not metrics.duplicated(key_columns).any()


def _assert_byte_deterministic(first: Path, second: Path) -> None:
    first_names = {item.name for item in first.iterdir() if item.is_file()}
    second_names = {item.name for item in second.iterdir() if item.is_file()}
    assert first.name == second.name
    assert second_names == first_names
    for filename in sorted(first_names):
        assert (first / filename).read_bytes() == (second / filename).read_bytes()
