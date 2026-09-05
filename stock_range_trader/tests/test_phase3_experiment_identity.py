"""STEP 8 deterministic identity, source state, and universe tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from test_phase3_executable_evaluation import _bars, _fold
from test_phase3_runner import _schedule

import walkforward.experiment as experiment_module
from config import load_phase3_config, load_strategy_config
from data import provider_price_basis
from universe import UNIVERSE_COLUMNS
from walkforward import (
    AnalysisMode,
    ExecutableCandidateCatalog,
    ExperimentError,
    ExperimentIdentity,
    ExperimentIdentityBuilder,
    SourceSnapshot,
    SourceState,
    SourceStateResolver,
    assess_formal_oos,
    assess_universe,
    build_input_artifact_fingerprint,
    canonical_content_sha256,
    normalized_universe_sha256,
)

PROJECT_ROOT = Path(__file__).parents[1]


def _universe(as_of: str = "2024-01-01") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "as_of_date": as_of,
                "jquants_code": "72030",
                "company_name": "Toyota",
                "market_segment_code": "0111",
                "market_segment_name": "Prime",
                "sector17_code": "6",
                "sector17_name": "Auto",
                "sector33_code": "3700",
                "sector33_name": "Transport",
                "product_category": "011",
                "yfinance_ticker": "7203.T",
                "universe_included": True,
                "exclusion_reason": "",
            },
            {
                "as_of_date": as_of,
                "jquants_code": "13010",
                "company_name": "Excluded",
                "market_segment_code": "0109",
                "market_segment_name": "Other",
                "sector17_code": "1",
                "sector17_name": "Other",
                "sector33_code": "1000",
                "sector33_name": "Other",
                "product_category": "011",
                "yfinance_ticker": "1301.T",
                "universe_included": False,
                "exclusion_reason": "outside_prime_standard_growth:0109",
            },
        ],
        columns=UNIVERSE_COLUMNS,
    )


def _clean_source(tree_hash: str = "b" * 64) -> SourceSnapshot:
    return SourceSnapshot(
        source_state=SourceState.CLEAN,
        git_root="/repo",
        git_commit_sha="a" * 40,
        git_branch="main",
        worktree_dirty=False,
        source_tree_sha256=tree_hash,
        reproducibility_status="reproducible",
    )


def _identity_inputs(tmp_path: Path):
    config = load_phase3_config(PROJECT_ROOT / "config" / "phase3.yaml")
    strategy = load_strategy_config(PROJECT_ROOT / "config" / "strategy.yaml")
    prices = _bars()
    input_path = tmp_path / "prices.parquet"
    prices.to_parquet(input_path, index=False)
    universe = _universe()
    universe_path = tmp_path / "universe.csv"
    universe.to_csv(universe_path, index=False)
    schedule = _schedule(_fold())
    assessment = assess_universe(
        universe_path,
        universe,
        prices,
        provider="jquants",
        schedule=schedule,
    )
    artifact = build_input_artifact_fingerprint(input_path, prices)
    catalog = ExecutableCandidateCatalog.from_config(
        config.executable_candidate_catalog
    )
    return config, strategy, schedule, assessment, artifact, catalog


def test_canonical_hash_ignores_row_order_and_fetched_at() -> None:
    original = pd.concat([_bars("7203.T"), _bars("1301.T")], ignore_index=True)
    changed = original.sample(frac=1.0, random_state=7).reset_index(drop=True)
    changed["fetched_at"] = datetime(2030, 1, 1, tzinfo=UTC)

    assert canonical_content_sha256(changed) == canonical_content_sha256(original)


def test_canonical_hash_changes_when_a_price_changes() -> None:
    original = _bars()
    changed = original.copy()
    changed.loc[0, "adjusted_close"] += 0.5

    assert canonical_content_sha256(changed) != canonical_content_sha256(original)


def test_experiment_identity_is_deterministic_and_source_sensitive(
    tmp_path: Path,
) -> None:
    config, strategy, schedule, universe, artifact, catalog = _identity_inputs(tmp_path)
    builder = ExperimentIdentityBuilder()
    arguments = {
        "phase3_config": config,
        "strategy_config": strategy,
        "candidate_catalog": catalog,
        "selection_policy": config.executable_selection,
        "schedule": schedule,
        "provider": "jquants",
        "analysis_mode": AnalysisMode.EXECUTABLE_VALIDATION,
        "provider_price_basis": provider_price_basis("jquants"),
        "input_artifact": artifact,
        "universe": universe,
        "source": _clean_source(),
    }

    first = builder.build(**arguments)
    second = builder.build(**arguments)
    changed = builder.build(**{**arguments, "source": _clean_source("c" * 64)})

    assert first == second
    assert first.experiment_id.startswith("wf3-executable_validation-")
    assert changed.experiment_id != first.experiment_id
    assert "output" not in first.normalized_payload_json


def test_identity_rejects_nonfinite_payload(tmp_path: Path) -> None:
    config, strategy, schedule, universe, artifact, catalog = _identity_inputs(tmp_path)
    with pytest.raises(ExperimentError, match="canonicalizable"):
        ExperimentIdentityBuilder().build(
            phase3_config=config,
            strategy_config=strategy,
            candidate_catalog=catalog,
            selection_policy=config.executable_selection,
            schedule=schedule,
            provider=float("nan"),  # type: ignore[arg-type]
            analysis_mode=AnalysisMode.EXECUTABLE_VALIDATION,
            provider_price_basis=provider_price_basis("jquants"),
            input_artifact=artifact,
            universe=universe,
            source=_clean_source(),
        )


def test_identity_rejects_modified_payload_with_original_digest(
    tmp_path: Path,
) -> None:
    config, strategy, schedule, universe, artifact, catalog = _identity_inputs(tmp_path)
    identity = ExperimentIdentityBuilder().build(
        phase3_config=config,
        strategy_config=strategy,
        candidate_catalog=catalog,
        selection_policy=config.executable_selection,
        schedule=schedule,
        provider="jquants",
        analysis_mode=AnalysisMode.EXECUTABLE_VALIDATION,
        provider_price_basis=provider_price_basis("jquants"),
        input_artifact=artifact,
        universe=universe,
        source=_clean_source(),
    )
    payload = identity.normalized_payload_json.replace(
        '"provider":"jquants"', '"provider":"yfinance"'
    )
    assert payload != identity.normalized_payload_json

    with pytest.raises(ExperimentError, match="payload_sha256"):
        ExperimentIdentity(
            experiment_id=identity.experiment_id,
            analysis_mode=identity.analysis_mode,
            payload_sha256=identity.payload_sha256,
            normalized_payload_json=payload,
        )


def test_identity_rejects_forged_digest_even_when_id_matches_it(
    tmp_path: Path,
) -> None:
    config, strategy, schedule, universe, artifact, catalog = _identity_inputs(tmp_path)
    identity = ExperimentIdentityBuilder().build(
        phase3_config=config,
        strategy_config=strategy,
        candidate_catalog=catalog,
        selection_policy=config.executable_selection,
        schedule=schedule,
        provider="jquants",
        analysis_mode=AnalysisMode.EXECUTABLE_VALIDATION,
        provider_price_basis=provider_price_basis("jquants"),
        input_artifact=artifact,
        universe=universe,
        source=_clean_source(),
    )
    forged_digest = "0" * 64

    with pytest.raises(ExperimentError, match="payload_sha256"):
        ExperimentIdentity(
            experiment_id=f"wf3-{identity.analysis_mode.value}-{forged_digest}",
            analysis_mode=identity.analysis_mode,
            payload_sha256=forged_digest,
            normalized_payload_json=identity.normalized_payload_json,
        )


def test_universe_hash_is_row_order_independent() -> None:
    universe = _universe()
    reversed_rows = universe.iloc[::-1].reset_index(drop=True)

    assert normalized_universe_sha256(universe) == normalized_universe_sha256(
        reversed_rows
    )


def test_universe_coverage_records_missing_and_unexpected_prices(
    tmp_path: Path,
) -> None:
    universe = _universe()
    universe.loc[1, "universe_included"] = True
    universe_path = tmp_path / "universe.csv"
    universe.to_csv(universe_path, index=False)
    prices = pd.concat([_bars("72030"), _bars("99990")], ignore_index=True)

    result = assess_universe(
        universe_path,
        universe,
        prices,
        provider="jquants",
        schedule=_schedule(_fold()),
    )

    assert result.available_price_symbol_count == 1
    assert result.missing_price_symbol_count == 1
    assert result.unexpected_price_symbol_count == 1
    statuses = {item.symbol: item.coverage_status for item in result.coverage}
    assert statuses == {
        "13010": "missing_prices",
        "72030": "available",
        "99990": "excluded_not_in_universe",
    }


def test_future_universe_snapshot_marks_every_earlier_fold_non_point_in_time(
    tmp_path: Path,
) -> None:
    universe = _universe("2025-01-01")
    path = tmp_path / "universe.csv"
    universe.to_csv(path, index=False)

    result = assess_universe(
        path,
        universe,
        _bars("72030"),
        provider="jquants",
        schedule=_schedule(_fold()),
    )

    assert result.point_in_time_universe is False
    assert result.survivorship_bias_status == "present"
    assert result.fold_assessments[0].point_in_time_universe is False


def test_multiple_universe_snapshot_dates_are_rejected(tmp_path: Path) -> None:
    universe = _universe()
    universe.loc[1, "as_of_date"] = "2024-01-02"
    path = tmp_path / "universe.csv"
    universe.to_csv(path, index=False)

    with pytest.raises(ExperimentError, match="identical"):
        assess_universe(
            path,
            universe,
            _bars("72030"),
            provider="jquants",
            schedule=_schedule(_fold()),
        )


def test_missing_included_provider_symbol_is_rejected(tmp_path: Path) -> None:
    universe = _universe()
    universe.loc[0, "jquants_code"] = pd.NA
    path = tmp_path / "universe.csv"
    universe.to_csv(path, index=False)

    with pytest.raises(ExperimentError, match="must not be empty"):
        assess_universe(
            path,
            universe,
            _bars("72030"),
            provider="jquants",
            schedule=_schedule(_fold()),
        )


def test_git_command_failure_is_degraded_not_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(experiment_module, "_git", unavailable)

    result = SourceStateResolver().resolve(PROJECT_ROOT)

    assert result.source_state is SourceState.GIT_UNAVAILABLE
    assert result.reproducibility_status == "degraded"
    assert result.git_commit_sha is None


def test_formal_oos_rejects_dirty_git_future_universe_and_lineage(
    tmp_path: Path,
) -> None:
    config, _, _, universe, artifact, _ = _identity_inputs(tmp_path)
    dirty = SourceSnapshot(
        source_state=SourceState.DIRTY,
        git_root="/repo",
        git_commit_sha="a" * 40,
        git_branch=None,
        worktree_dirty=True,
        source_tree_sha256="b" * 64,
        reproducibility_status="degraded",
    )
    future = replace(
        universe,
        fold_assessments=tuple(
            replace(
                item,
                point_in_time_universe=False,
                survivorship_bias_status="present",
            )
            for item in universe.fold_assessments
        ),
        point_in_time_universe=False,
        survivorship_bias_status="present",
    )

    result = assess_formal_oos(
        source=dirty,
        config=config,
        input_artifact=artifact,
        universe=future,
        capability_allowed=True,
        output_collision=False,
        parent_experiment_id="wf3-parent",
    )

    assert result.eligible is False
    assert result.reasons == (
        "source_state_dirty",
        "point_in_time_universe_not_satisfied",
        "derived_after_test_review",
    )
