"""Unit tests for the mode-specific Phase 3 candidate domains."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from config import StrategyConfig, load_phase3_config, load_strategy_config
from walkforward.candidates import (
    ExecutableCandidateCatalog,
    ExecutableCandidateDefinition,
    SignalCandidateCatalog,
    SignalCandidateDefinition,
)

PROJECT_ROOT = Path(__file__).parents[1]


def _base_config() -> StrategyConfig:
    return load_strategy_config(PROJECT_ROOT / "config" / "strategy.yaml")


def _phase3_config():
    return load_phase3_config(PROJECT_ROOT / "config" / "phase3.yaml")


def _signal_candidate(candidate_id: str = "signal") -> SignalCandidateDefinition:
    return SignalCandidateDefinition(
        candidate_id=candidate_id,
        buy_atr_multiplier=2.0,
        range_score_threshold=80.0,
        adx_entry_max=20.0,
    )


def _executable_candidate(
    candidate_id: str = "executable",
) -> ExecutableCandidateDefinition:
    return ExecutableCandidateDefinition(
        candidate_id=candidate_id,
        buy_atr_multiplier=2.0,
        sell_atr_multiplier=1.0,
        range_score_threshold=80.0,
        adx_entry_max=20.0,
    )


def test_signal_candidate_changes_only_three_allowed_fields() -> None:
    base = _base_config()
    unchanged_snapshot = _base_config()

    applied = _signal_candidate().apply(base)

    changed = {
        field.name
        for field in fields(StrategyConfig)
        if getattr(base, field.name) != getattr(applied, field.name)
    }
    assert changed == {
        "buy_atr_multiplier",
        "range_score_threshold",
        "adx_entry_max",
    }
    assert applied.sell_atr_multiplier == base.sell_atr_multiplier
    assert base == unchanged_snapshot
    assert applied is not base
    assert not hasattr(applied, "candidate_id")


def test_executable_candidate_changes_only_four_strategy_fields() -> None:
    base = _base_config()
    unchanged_snapshot = _base_config()

    applied = _executable_candidate().apply(base)

    changed = {
        field.name
        for field in fields(StrategyConfig)
        if getattr(base, field.name) != getattr(applied, field.name)
    }
    assert changed == {
        "buy_atr_multiplier",
        "sell_atr_multiplier",
        "range_score_threshold",
        "adx_entry_max",
    }
    for fixed_field in (
        "initial_capital",
        "commission_rate",
        "slippage_pct",
        "lot_size",
        "max_position_pct",
        "max_positions",
        "max_drawdown_stop",
        "annual_trading_days",
        "risk_free_rate",
    ):
        assert getattr(applied, fixed_field) == getattr(base, fixed_field)
    assert base == unchanged_snapshot
    assert applied is not base


def test_candidate_apply_reruns_strategy_config_validation(monkeypatch) -> None:
    base = _base_config()
    original_post_init = StrategyConfig.__post_init__
    validated: list[StrategyConfig] = []

    def recording_post_init(config: StrategyConfig) -> None:
        validated.append(config)
        original_post_init(config)

    monkeypatch.setattr(StrategyConfig, "__post_init__", recording_post_init)

    applied = _signal_candidate().apply(base)

    assert validated == [applied]


def test_catalog_conversion_preserves_yaml_order_and_configured_limit() -> None:
    phase3 = _phase3_config()

    signal = SignalCandidateCatalog.from_config(phase3.signal_candidate_catalog)
    executable = ExecutableCandidateCatalog.from_config(
        phase3.executable_candidate_catalog
    )

    assert signal.candidate_ids == ("baseline", "conservative", "moderate")
    assert executable.candidate_ids == ("baseline", "conservative", "moderate")
    assert signal.maximum_candidates == 12
    assert executable.maximum_candidates == 12
    assert signal.candidates[0].buy_atr_multiplier == 1.5
    assert executable.candidates[2].sell_atr_multiplier == 1.25


@pytest.mark.parametrize(
    "catalog_type",
    (SignalCandidateCatalog, ExecutableCandidateCatalog),
)
def test_runtime_catalog_rejects_empty_candidates(catalog_type: type) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        catalog_type(candidates=())


def test_runtime_catalog_rejects_duplicate_ids_and_configured_limit() -> None:
    candidate = _signal_candidate("duplicate")
    with pytest.raises(ValueError, match="IDs must be unique"):
        SignalCandidateCatalog(candidates=(candidate, candidate))

    with pytest.raises(ValueError, match="exceeds configured maximum"):
        SignalCandidateCatalog(
            candidates=(_signal_candidate("one"), _signal_candidate("two")),
            maximum_candidates=1,
        )


def test_runtime_catalog_rejects_phase3_hard_limit() -> None:
    candidates = tuple(_signal_candidate(f"candidate_{index}") for index in range(13))

    with pytest.raises(ValueError, match="cannot exceed 12"):
        SignalCandidateCatalog(candidates=candidates, maximum_candidates=13)


def test_runtime_catalog_and_conversion_reject_other_mode_types() -> None:
    phase3 = _phase3_config()

    with pytest.raises(TypeError, match="SignalCandidateCatalogConfig"):
        SignalCandidateCatalog.from_config(phase3.executable_candidate_catalog)
    with pytest.raises(TypeError, match="ExecutableCandidateCatalogConfig"):
        ExecutableCandidateCatalog.from_config(phase3.signal_candidate_catalog)
    with pytest.raises(TypeError, match="SignalCandidateDefinition"):
        SignalCandidateCatalog(candidates=(_executable_candidate(),))
    with pytest.raises(TypeError, match="ExecutableCandidateDefinition"):
        ExecutableCandidateCatalog(candidates=(_signal_candidate(),))


def test_candidate_definition_conversion_rejects_other_mode_config() -> None:
    phase3 = _phase3_config()

    with pytest.raises(TypeError, match="SignalCandidateConfig"):
        SignalCandidateDefinition.from_config(
            phase3.executable_candidate_catalog.candidates[0]
        )
    with pytest.raises(TypeError, match="ExecutableCandidateConfig"):
        ExecutableCandidateDefinition.from_config(
            phase3.signal_candidate_catalog.candidates[0]
        )


def test_candidate_application_requires_strategy_config() -> None:
    with pytest.raises(TypeError, match="base_config must be StrategyConfig"):
        _signal_candidate().apply(object())  # type: ignore[arg-type]
