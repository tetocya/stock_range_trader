"""Unit tests for the strict Phase 3 configuration schema."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from config import (
    ExecutableCandidateConfig,
    ExecutableSelectionPolicy,
    FoldScheduleConfig,
    Phase3Config,
    SignalCandidateConfig,
    SignalSelectionPolicy,
    load_phase3_config,
)

CONFIG_PATH = Path(__file__).parents[1] / "config" / "phase3.yaml"


def _values() -> dict[str, object]:
    with CONFIG_PATH.open(encoding="utf-8") as source:
        return yaml.safe_load(source)


def test_phase3_yaml_loads_confirmed_mode_specific_contracts() -> None:
    config = load_phase3_config(CONFIG_PATH)

    assert isinstance(config, Phase3Config)
    assert config.signal_fold_schedule == FoldScheduleConfig(
        train_months=24,
        validation_months=6,
        test_months=6,
        step_months=6,
        forward_sessions=20,
        embargo_sessions=20,
        minimum_folds=3,
        purge_rule="label_end_date_lt_test_start",
    )
    assert config.executable_fold_schedule.minimum_folds == 2
    assert config.signal_selection.primary_metric == ("mean_reversion_target_hit_rate")
    assert config.signal_selection.mean_reversion_target == "signal_date_sma"
    assert config.require_clean_worktree_for_formal_oos is True
    assert config.git_unavailable_source_state == "git_unavailable"
    assert config.git_unavailable_reproducibility_status == "degraded"
    assert config.git_unavailable_formal_oos_eligible is False
    assert config.signal_fold_schedule.purge_rule == ("label_end_date_lt_test_start")


def test_signal_and_executable_candidate_catalogs_are_separate() -> None:
    config = load_phase3_config(CONFIG_PATH)
    signal_baseline = config.signal_candidate_catalog.candidates[0]
    executable_baseline = config.executable_candidate_catalog.candidates[0]

    assert signal_baseline.id == executable_baseline.id == "baseline"
    assert not hasattr(signal_baseline, "sell_atr_multiplier")
    assert executable_baseline.sell_atr_multiplier == 1.5
    assert config.signal_candidate_catalog is not config.executable_candidate_catalog


def test_candidate_schemas_expose_only_mode_appropriate_strategy_parameters() -> None:
    assert {field.name for field in fields(SignalCandidateConfig)} == {
        "id",
        "buy_atr_multiplier",
        "range_score_threshold",
        "adx_entry_max",
    }
    assert {field.name for field in fields(ExecutableCandidateConfig)} == {
        "id",
        "buy_atr_multiplier",
        "sell_atr_multiplier",
        "range_score_threshold",
        "adx_entry_max",
    }


@pytest.mark.parametrize(
    "forbidden_key",
    ("initial_capital", "commission_rate", "slippage_pct", "lot_size"),
)
def test_executable_candidates_reject_execution_condition_overrides(
    forbidden_key: str,
) -> None:
    values = _values()
    catalog = values["executable_candidate_catalog"]
    assert isinstance(catalog, dict)
    candidates = catalog["candidates"]
    assert isinstance(candidates, list)
    assert isinstance(candidates[0], dict)
    candidates[0][forbidden_key] = 1

    with pytest.raises(ValueError, match="Unknown executable candidate keys"):
        Phase3Config.from_mapping(values)


def test_executable_policy_requires_all_four_sample_sufficiency_constraints() -> None:
    config = load_phase3_config(CONFIG_PATH)
    policy = config.executable_selection

    assert policy.minimum_traded_symbol_count == 3
    assert policy.minimum_trading_symbol_ratio == 0.10
    assert policy.minimum_total_trade_count == 10
    assert policy.minimum_finite_sharpe_count == 3
    assert policy.maximum_drawdown_limit == 0.30

    values = _values()["executable_selection"]
    assert isinstance(values, dict)
    values["minimum_finite_sharpe_count"] = 0
    with pytest.raises(ValueError, match="minimum_finite_sharpe_count"):
        ExecutableSelectionPolicy.from_mapping(values)


@pytest.mark.parametrize("ratio", (0.0, 1.0))
def test_minimum_trading_symbol_ratio_includes_both_boundaries(ratio: float) -> None:
    values = _values()["executable_selection"]
    assert isinstance(values, dict)
    values["minimum_trading_symbol_ratio"] = ratio

    policy = ExecutableSelectionPolicy.from_mapping(values)

    assert policy.minimum_trading_symbol_ratio == ratio


@pytest.mark.parametrize("ratio", (-0.01, 1.01))
def test_minimum_trading_symbol_ratio_rejects_values_outside_unit_interval(
    ratio: float,
) -> None:
    values = _values()["executable_selection"]
    assert isinstance(values, dict)
    values["minimum_trading_symbol_ratio"] = ratio

    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        ExecutableSelectionPolicy.from_mapping(values)


@pytest.mark.parametrize("limit", (0.0, 0.999))
def test_maximum_drawdown_limit_accepts_positive_loss_magnitudes(
    limit: float,
) -> None:
    values = _values()["executable_selection"]
    assert isinstance(values, dict)
    values["maximum_drawdown_limit"] = limit

    policy = ExecutableSelectionPolicy.from_mapping(values)

    assert policy.maximum_drawdown_limit == limit


@pytest.mark.parametrize("limit", (-0.01, 1.0))
def test_maximum_drawdown_limit_rejects_values_outside_half_open_interval(
    limit: float,
) -> None:
    values = _values()["executable_selection"]
    assert isinstance(values, dict)
    values["maximum_drawdown_limit"] = limit

    with pytest.raises(ValueError, match=r"\[0, 1\)"):
        ExecutableSelectionPolicy.from_mapping(values)


def test_signal_primary_target_cannot_be_changed_to_candidate_sell_threshold() -> None:
    values = _values()["signal_selection"]
    assert isinstance(values, dict)
    values["mean_reversion_target"] = "candidate_sell_threshold"

    with pytest.raises(ValueError, match="SMA observed on the signal date"):
        SignalSelectionPolicy.from_mapping(values)


def test_fold_schedule_rejects_short_embargo_and_overlapping_step() -> None:
    values = _values()["signal_fold_schedule"]
    assert isinstance(values, dict)
    values["embargo_sessions"] = 19
    with pytest.raises(ValueError, match="embargo_sessions"):
        FoldScheduleConfig.from_mapping(values)

    values = _values()["signal_fold_schedule"]
    assert isinstance(values, dict)
    values["step_months"] = 5
    with pytest.raises(ValueError, match="step_months"):
        FoldScheduleConfig.from_mapping(values)

    values = _values()["signal_fold_schedule"]
    assert isinstance(values, dict)
    values["purge_rule"] = "position_distance_only"
    with pytest.raises(ValueError, match="actual label_end_date"):
        FoldScheduleConfig.from_mapping(values)


def test_phase3_config_rejects_unknown_keys_at_every_schema_boundary() -> None:
    values = _values()
    values["typo"] = True
    with pytest.raises(ValueError, match="Unknown Phase 3 configuration"):
        Phase3Config.from_mapping(values)

    values = _values()
    catalog = values["signal_candidate_catalog"]
    assert isinstance(catalog, dict)
    candidates = catalog["candidates"]
    assert isinstance(candidates, list)
    assert isinstance(candidates[0], dict)
    candidates[0]["sell_atr_multiplier"] = 1.5
    with pytest.raises(ValueError, match="Unknown signal candidate keys"):
        Phase3Config.from_mapping(values)


def test_candidate_catalog_rejects_duplicate_ids_and_hard_limit() -> None:
    values = _values()
    catalog = values["signal_candidate_catalog"]
    assert isinstance(catalog, dict)
    candidates = catalog["candidates"]
    assert isinstance(candidates, list)
    candidates.append(dict(candidates[0]))
    with pytest.raises(ValueError, match="duplicate signal candidate ids"):
        Phase3Config.from_mapping(values)

    values = _values()
    catalog = values["executable_candidate_catalog"]
    assert isinstance(catalog, dict)
    catalog["maximum_candidates"] = 13
    with pytest.raises(ValueError, match="cannot exceed 12"):
        Phase3Config.from_mapping(values)


def test_formal_oos_clean_worktree_policy_cannot_be_disabled() -> None:
    values = _values()
    values["require_clean_worktree_for_formal_oos"] = False

    with pytest.raises(ValueError, match="clean Git worktree"):
        Phase3Config.from_mapping(values)


def test_git_unavailable_cannot_be_promoted_to_formal_oos() -> None:
    values = _values()
    values["git_unavailable_formal_oos_eligible"] = True

    with pytest.raises(ValueError, match="must not be eligible for formal OOS"):
        Phase3Config.from_mapping(values)
