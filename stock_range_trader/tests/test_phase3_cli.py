"""STEP 8 local-only CLI confirmation and preflight boundary tests."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml
from test_phase3_executable_evaluation import _bars as executable_bars
from test_phase3_experiment_identity import _universe
from test_phase3_signal_evaluation import _bars

from data import provider_price_basis
from examples.phase3_common import build_parser, prepare_preflight
from examples.run_walk_forward_executable import main as executable_main
from examples.run_walk_forward_signal import main as signal_main
from reports import ExperimentAlreadyExistsError
from walkforward import (
    AnalysisMode,
    CandidateAssessment,
    CandidateSelection,
    ProviderCapabilityError,
    SelectionStatus,
    SignalFoldRunResult,
    SignalOutcomeEvaluationResult,
    SignalValidationScore,
    SignalWalkForwardRunResult,
    ValidationCohort,
)
from walkforward import TestEvaluationStatus as EvaluationStatus

PROJECT_ROOT = Path(__file__).parents[1]


def _inputs(tmp_path: Path, *, post_end_provider: str | None = None):
    start = date(2024, 1, 1)
    end = date(2024, 4, 1)
    dates = pd.bdate_range(start, end - pd.Timedelta(days=1))
    bars = _bars(dates=dates)
    if post_end_provider is not None:
        future = _bars(
            "9999.T",
            provider=post_end_provider,
            dates=pd.DatetimeIndex([pd.Timestamp(end)]),
        )
        bars = pd.concat([bars, future], ignore_index=True)
    input_path = tmp_path / "prices.parquet"
    bars.to_parquet(input_path, index=False)
    universe_path = tmp_path / "universe.csv"
    _universe().to_csv(universe_path, index=False)
    with (PROJECT_ROOT / "config" / "phase3.yaml").open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    for key in ("signal_fold_schedule", "executable_fold_schedule"):
        config[key].update(
            {
                "train_months": 1,
                "validation_months": 1,
                "test_months": 1,
                "step_months": 1,
                "minimum_folds": 1,
            }
        )
    config["signal_fold_schedule"].update(
        {"forward_sessions": 2, "embargo_sessions": 2}
    )
    config["executable_fold_schedule"].update(
        {"forward_sessions": 0, "embargo_sessions": 0}
    )
    config_path = tmp_path / "phase3.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return input_path, universe_path, config_path, start, end


def _arguments(tmp_path: Path, *, flag: str, post_end_provider: str | None = None):
    input_path, universe_path, config_path, start, end = _inputs(
        tmp_path, post_end_provider=post_end_provider
    )
    return [
        "--input",
        str(input_path),
        "--universe",
        str(universe_path),
        "--config",
        str(config_path),
        "--strategy-config",
        str(PROJECT_ROOT / "config" / "strategy.yaml"),
        "--start",
        start.isoformat(),
        "--end",
        end.isoformat(),
        "--output-dir",
        str(tmp_path / "reports"),
        flag,
    ]


def test_cli_requires_exactly_one_preflight_or_confirmation_flag() -> None:
    parser = build_parser(AnalysisMode.SIGNAL_VALIDATION)
    required = [
        "--input",
        "prices.parquet",
        "--universe",
        "universe.csv",
        "--start",
        "2024-01-01",
        "--end",
        "2024-04-01",
        "--output-dir",
        "reports",
    ]

    with pytest.raises(SystemExit):
        parser.parse_args(required)
    with pytest.raises(SystemExit):
        parser.parse_args([*required, "--preflight-only", "--confirm-test-evaluation"])


def test_parent_and_change_reason_must_be_supplied_together(tmp_path: Path) -> None:
    args = [
        *_arguments(tmp_path, flag="--preflight-only"),
        "--parent-experiment-id",
        "wf3-parent",
    ]

    with pytest.raises(SystemExit):
        signal_main(args)


def test_signal_preflight_does_not_run_validation_or_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def forbidden(*args, **kwargs):
        calls.append("runner")
        raise AssertionError("Runner must not run in preflight")

    monkeypatch.setattr("examples.phase3_common.SignalWalkForwardRunner.run", forbidden)

    assert signal_main(_arguments(tmp_path, flag="--preflight-only")) == 0
    assert calls == []
    output = capsys.readouterr().out
    assert "No Validation or Test evaluation was run" in output
    assert "adjusted-price signals only" in output
    assert not (tmp_path / "reports").exists()


def test_requested_end_is_exclusive_before_provider_gate(tmp_path: Path) -> None:
    args = _arguments(tmp_path, flag="--preflight-only", post_end_provider="unknown")

    assert signal_main(args) == 0


def test_executable_yfinance_is_rejected_during_preflight(tmp_path: Path) -> None:
    with pytest.raises(ProviderCapabilityError, match="does not support"):
        executable_main(_arguments(tmp_path, flag="--preflight-only"))


def test_executable_cli_preflight_and_completed_bundle_with_artificial_jquants(
    tmp_path: Path,
) -> None:
    arguments = _arguments(tmp_path, flag="--preflight-only")
    input_path = Path(arguments[1])
    dates = pd.bdate_range("2024-01-01", "2024-03-29")
    executable_bars("72030", dates=dates).to_parquet(input_path, index=False)

    assert executable_main(arguments) == 0
    confirmed = [
        "--confirm-test-evaluation" if item == "--preflight-only" else item
        for item in arguments
    ]
    assert executable_main(confirmed) == 0

    bundles = list((tmp_path / "reports").glob("wf3-executable_validation-*"))
    assert len(bundles) == 1
    manifest = yaml.safe_load(
        (bundles[0] / "walk_forward_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "completed"
    assert manifest["experiment"]["analysis_mode"] == "executable_validation"


def test_output_collision_is_rejected_before_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _arguments(tmp_path, flag="--confirm-test-evaluation")
    namespace = build_parser(AnalysisMode.SIGNAL_VALIDATION).parse_args(arguments)
    preflight = prepare_preflight(AnalysisMode.SIGNAL_VALIDATION, namespace)
    (namespace.output_dir / preflight.identity.experiment_id).mkdir(parents=True)
    calls: list[str] = []

    def forbidden(*args, **kwargs):
        calls.append("runner")
        raise AssertionError("Runner must not run after a collision")

    monkeypatch.setattr("examples.phase3_common.SignalWalkForwardRunner.run", forbidden)

    with pytest.raises(ExperimentAlreadyExistsError):
        signal_main(arguments)
    assert calls == []


def test_all_fold_no_eligible_is_a_completed_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_eligible_run(self, bars, schedule, catalog):
        fold_results = []
        for fold in schedule.folds:
            scores = tuple(
                SignalValidationScore(item, 0, None, None, None)
                for item in catalog.candidate_ids
            )
            validation = SignalOutcomeEvaluationResult(
                provider="yfinance",
                provider_price_basis=provider_price_basis("yfinance"),
                fold_id=fold.fold_id,
                input_symbol_count=1,
                admitted_symbol_count=1,
                observations=(),
                observation_exclusions=(),
                symbol_exclusions=(),
                scores=scores,
            )
            selection = CandidateSelection(
                analysis_mode=AnalysisMode.SIGNAL_VALIDATION,
                status=SelectionStatus.NO_ELIGIBLE_CANDIDATE,
                selected_candidate_id=None,
                ranked_candidate_ids=(),
                assessments=tuple(
                    CandidateAssessment(
                        candidate_id=item,
                        eligible=False,
                        rejection_reasons=(
                            "insufficient_observation_count",
                            "invalid_primary_metric",
                            "invalid_median_forward_return",
                            "invalid_median_mae_magnitude",
                        ),
                        rank=None,
                    )
                    for item in sorted(catalog.candidate_ids)
                ),
            )
            fold_results.append(
                SignalFoldRunResult(
                    fold=fold,
                    validation_cohort=ValidationCohort(
                        "yfinance",
                        provider_price_basis("yfinance"),
                        ("7203.T",),
                    ),
                    validation_result=validation,
                    selection=selection,
                    test_status=(EvaluationStatus.NOT_RUN_NO_ELIGIBLE_CANDIDATE),
                    test_result=None,
                )
            )
        return SignalWalkForwardRunResult(
            provider="yfinance",
            provider_price_basis=provider_price_basis("yfinance"),
            schedule=schedule,
            fold_results=tuple(fold_results),
        )

    monkeypatch.setattr(
        "examples.phase3_common.SignalWalkForwardRunner.run", no_eligible_run
    )

    assert signal_main(_arguments(tmp_path, flag="--confirm-test-evaluation")) == 0
    bundles = list((tmp_path / "reports").glob("wf3-*"))
    assert len(bundles) == 1
    summary = pd.read_csv(bundles[0] / "walk_forward_summary.csv")
    assert summary.loc[0, "aggregate_status"] == "completed_no_test_folds"
    assert summary.loc[0, "no_eligible_fold_count"] == 1


def test_require_formal_oos_rejects_future_universe_before_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _arguments(tmp_path, flag="--confirm-test-evaluation")
    universe_path = Path(arguments[3])
    universe = pd.read_csv(universe_path)
    universe.loc[:, "as_of_date"] = "2030-01-01"
    universe.to_csv(universe_path, index=False)
    calls: list[str] = []

    def forbidden(*args, **kwargs):
        calls.append("runner")
        raise AssertionError("Runner must not run for future Universe")

    monkeypatch.setattr("examples.phase3_common.SignalWalkForwardRunner.run", forbidden)

    with pytest.raises(ValueError, match="point_in_time_universe_not_satisfied"):
        signal_main([*arguments, "--require-formal-oos"])

    assert calls == []
    assert not (tmp_path / "reports").exists()


def test_test_failure_publishes_only_one_safe_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _arguments(tmp_path, flag="--confirm-test-evaluation")
    config_path = Path(arguments[5])
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["signal_selection"]["minimum_observation_count"] = 1
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    tested_candidates: list[str] = []
    secret = "JQUANTS_API_KEY=must-not-appear"

    def prepare(self, frame, candidate):
        result = frame.copy()
        result["sma"] = 105.0
        result["atr"] = 5.0
        result["adx"] = 10.0
        result["range_score"] = 80.0
        result["buy_threshold"] = 95.0
        result["entry_condition"] = True
        return result

    def fail_test(self, bars, fold, candidate, policy, cohort):
        tested_candidates.append(candidate.candidate_id)
        raise RuntimeError(secret)

    monkeypatch.setattr(
        "walkforward.SignalOutcomeEvaluator._prepare_candidate", prepare
    )
    monkeypatch.setattr("walkforward.SignalOutcomeEvaluator.evaluate_test", fail_test)

    with pytest.raises(RuntimeError):
        signal_main(arguments)

    output = tmp_path / "reports"
    receipts = list((output / "_failed").glob("*.json"))
    completed = list(output.glob("wf3-*"))
    assert len(tested_candidates) == 1
    assert completed == []
    assert len(receipts) == 1
    payload = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["exception_type"] == "RuntimeError"
    assert secret not in receipts[0].read_text(encoding="utf-8")


def test_both_clis_are_offline_for_artificial_local_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def forbidden(*args, **kwargs):
        calls.append("external")
        raise AssertionError("Phase 3 CLI must not call a provider or socket")

    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("data.providers.YFinanceProvider.get_daily_bars", forbidden)
    monkeypatch.setattr("data.providers.JQuantsV2Provider.get_daily_bars", forbidden)
    monkeypatch.setattr("data.providers.JQuantsV2Provider.get_universe", forbidden)

    signal_arguments = _arguments(tmp_path, flag="--preflight-only")
    assert signal_main(signal_arguments) == 0

    executable_root = tmp_path / "executable"
    executable_root.mkdir()
    executable_arguments = _arguments(executable_root, flag="--preflight-only")
    dates = pd.bdate_range("2024-01-01", "2024-03-29")
    executable_bars("72030", dates=dates).to_parquet(
        executable_arguments[1], index=False
    )
    assert executable_main(executable_arguments) == 0
    assert calls == []
