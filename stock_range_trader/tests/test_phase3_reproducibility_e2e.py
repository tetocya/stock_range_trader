"""End-to-end reproducibility and Formal-OOS adversarial tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from phase3_adversarial_helpers import (
    PROJECT_ROOT,
    adversarial_dates,
    adversarial_fold,
    canonical_bars,
    fold_schedule,
    signal_catalog,
    strategy_config,
    universe_frame,
)

from config import load_phase3_config
from data import provider_price_basis
from walkforward import (
    AnalysisMode,
    ExperimentError,
    ExperimentIdentityBuilder,
    SignalCandidateCatalog,
    SignalCandidateDefinition,
    SourceSnapshot,
    SourceState,
    assess_formal_oos,
    assess_universe,
    build_input_artifact_fingerprint,
)


def _source(
    state: SourceState = SourceState.CLEAN, *, tree_hash: str = "b" * 64
) -> SourceSnapshot:
    if state is SourceState.GIT_UNAVAILABLE:
        return SourceSnapshot(
            source_state=state,
            git_root=None,
            git_commit_sha=None,
            git_branch=None,
            worktree_dirty=None,
            source_tree_sha256=None,
            reproducibility_status="degraded",
        )
    dirty = state is SourceState.DIRTY
    return SourceSnapshot(
        source_state=state,
        git_root="/artificial/repository",
        git_commit_sha="a" * 40,
        git_branch="main",
        worktree_dirty=dirty,
        source_tree_sha256=tree_hash,
        reproducibility_status="degraded" if dirty else "reproducible",
    )


def _identity_arguments(
    tmp_path,
    *,
    bars: pd.DataFrame | None = None,
    universe_as_of: str = "2024-01-01",
):
    selected_bars = canonical_bars(provider="yfinance") if bars is None else bars
    price_path = tmp_path / "prices.parquet"
    selected_bars.to_parquet(price_path, index=False)
    universe_data = universe_frame(as_of=universe_as_of)
    universe_path = tmp_path / "universe.csv"
    universe_data.to_csv(universe_path, index=False)
    schedule = fold_schedule(adversarial_fold())
    universe = assess_universe(
        universe_path,
        universe_data,
        selected_bars,
        provider="yfinance",
        schedule=schedule,
    )
    config = load_phase3_config(PROJECT_ROOT / "config" / "phase3.yaml")
    return {
        "phase3_config": config,
        "strategy_config": strategy_config(),
        "candidate_catalog": signal_catalog(),
        "selection_policy": config.signal_selection,
        "schedule": schedule,
        "provider": "yfinance",
        "analysis_mode": AnalysisMode.SIGNAL_VALIDATION,
        "provider_price_basis": provider_price_basis("yfinance"),
        "input_artifact": build_input_artifact_fingerprint(price_path, selected_bars),
        "universe": universe,
        "source": _source(),
    }


def test_experiment_id_ignores_row_order_and_fetched_at(tmp_path) -> None:
    bars = pd.concat(
        [
            canonical_bars("7203.T", provider="yfinance"),
            canonical_bars("1301.T", provider="yfinance"),
        ],
        ignore_index=True,
    )
    original_dir = tmp_path / "original"
    changed_dir = tmp_path / "changed"
    original_dir.mkdir()
    changed_dir.mkdir()
    original = _identity_arguments(original_dir, bars=bars)
    changed_bars = bars.sample(frac=1.0, random_state=9).reset_index(drop=True)
    changed_bars["fetched_at"] = datetime(2035, 1, 1, tzinfo=UTC)
    changed = _identity_arguments(changed_dir, bars=changed_bars)

    first = ExperimentIdentityBuilder().build(**original)
    second = ExperimentIdentityBuilder().build(**changed)

    assert original["input_artifact"].file_sha256 != (
        changed["input_artifact"].file_sha256
    )
    assert original["input_artifact"].canonical_content_sha256 == (
        changed["input_artifact"].canonical_content_sha256
    )
    assert second == first


def test_analysis_affecting_identity_components_each_change_experiment_id(
    tmp_path,
) -> None:
    base = _identity_arguments(tmp_path)
    builder = ExperimentIdentityBuilder()
    baseline = builder.build(**base)
    dates = adversarial_dates()
    changed_catalog = SignalCandidateCatalog(
        candidates=(
            SignalCandidateDefinition("signal_a", 0.6, 60.0, 30.0),
            *signal_catalog().candidates[1:],
        )
    )
    variants = {
        "candidate": {"candidate_catalog": changed_catalog},
        "selection": {
            "selection_policy": replace(
                base["selection_policy"], minimum_observation_count=999
            )
        },
        "fold": {
            "schedule": fold_schedule(
                replace(adversarial_fold(), train_start=dates[1].date())
            )
        },
        "provider": {
            "provider": "jquants",
            "provider_price_basis": provider_price_basis("jquants"),
        },
        "universe": {
            "universe": replace(base["universe"], normalized_universe_sha256="c" * 64)
        },
        "source": {"source": _source(tree_hash="d" * 64)},
    }

    identities = {
        name: builder.build(**{**base, **changes}).experiment_id
        for name, changes in variants.items()
    }

    assert all(value != baseline.experiment_id for value in identities.values())
    assert len(set(identities.values())) == len(identities)


def test_experiment_identity_rejects_analysis_mode_prefix_mismatch(tmp_path) -> None:
    identity = ExperimentIdentityBuilder().build(**_identity_arguments(tmp_path))

    with pytest.raises(ExperimentError, match="experiment_id"):
        replace(identity, analysis_mode=AnalysisMode.EXECUTABLE_VALIDATION)


@pytest.mark.parametrize(
    ("case", "expected_reasons"),
    (
        ("clean", ()),
        ("dirty", ("source_state_dirty",)),
        (
            "git_unavailable",
            ("source_state_git_unavailable", "git_commit_sha_unavailable"),
        ),
        ("future_universe", ("point_in_time_universe_not_satisfied",)),
        ("capability", ("provider_capability_not_allowed",)),
        ("collision", ("experiment_output_already_exists",)),
        ("lineage", ("derived_after_test_review",)),
        (
            "clean_not_required",
            ("clean_worktree_not_required_by_configuration",),
        ),
    ),
)
def test_formal_oos_conditions_have_exact_reason_codes(
    tmp_path, case: str, expected_reasons: tuple[str, ...]
) -> None:
    arguments = _identity_arguments(tmp_path)
    config = arguments["phase3_config"]
    source = arguments["source"]
    universe = arguments["universe"]
    capability_allowed = True
    output_collision = False
    parent = None
    if case == "dirty":
        source = _source(SourceState.DIRTY)
    elif case == "git_unavailable":
        source = _source(SourceState.GIT_UNAVAILABLE)
    elif case == "future_universe":
        future_dir = tmp_path / "future"
        future_dir.mkdir()
        universe = _identity_arguments(future_dir, universe_as_of="2030-01-01")[
            "universe"
        ]
    elif case == "capability":
        capability_allowed = False
    elif case == "collision":
        output_collision = True
    elif case == "lineage":
        parent = "wf3-parent"
    elif case == "clean_not_required":
        # Phase3Config correctly forbids this state at construction. Exercise the
        # defense-in-depth assessment branch without weakening that Schema.
        config = SimpleNamespace(require_clean_worktree_for_formal_oos=False)

    assessment = assess_formal_oos(
        source=source,
        config=config,
        input_artifact=arguments["input_artifact"],
        universe=universe,
        capability_allowed=capability_allowed,
        output_collision=output_collision,
        parent_experiment_id=parent,
    )

    assert assessment.eligible is (case == "clean")
    assert assessment.reasons == expected_reasons
