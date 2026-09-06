"""Unit tests for the Phase 3 provider capability gate."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from data import provider_price_basis
from walkforward import (
    AnalysisMode,
    ProviderCapability,
    ProviderCapabilityError,
    ProviderCapabilityRegistry,
)


def test_analysis_mode_accepts_only_explicit_phase3_values() -> None:
    assert AnalysisMode.parse(" signal_validation ") is AnalysisMode.SIGNAL_VALIDATION
    assert (
        AnalysisMode.parse(AnalysisMode.EXECUTABLE_VALIDATION)
        is AnalysisMode.EXECUTABLE_VALIDATION
    )

    with pytest.raises(ValueError, match="analysis mode must be one of"):
        AnalysisMode.parse("backtest")

    with pytest.raises(ValueError, match="analysis mode must be one of"):
        ProviderCapabilityRegistry().require("jquants", "unknown_mode")


def test_default_registry_matches_phase2_price_contract() -> None:
    registry = ProviderCapabilityRegistry()
    yahoo = registry.require("YFINANCE", AnalysisMode.SIGNAL_VALIDATION)
    jquants = registry.require(
        "jquants",
        AnalysisMode.EXECUTABLE_VALIDATION,
        require_benchmark=True,
    )

    assert registry.providers == ("jquants", "yfinance")
    assert yahoo.provider_price_basis == provider_price_basis("yfinance")
    assert yahoo.executable_validation_supported is False
    assert yahoo.benchmark_supported is False
    assert jquants.provider_price_basis == provider_price_basis("jquants")
    assert jquants.executable_validation_supported is True
    assert jquants.maximum_expected_history == (
        "free_plan_2_years_excluding_availability_lag"
    )
    assert jquants.availability_lag == "free_plan_12_weeks"
    assert "validate_backtest_price_contract" in " ".join(jquants.notes)


def test_yfinance_executable_mode_fails_before_computation() -> None:
    registry = ProviderCapabilityRegistry()

    with pytest.raises(
        ProviderCapabilityError,
        match="yfinance.*does not support executable_validation",
    ):
        registry.require("yfinance", AnalysisMode.EXECUTABLE_VALIDATION)


def test_registry_rejects_unknown_and_duplicate_providers() -> None:
    registry = ProviderCapabilityRegistry()
    with pytest.raises(ProviderCapabilityError, match="unknown provider"):
        registry.get("unknown")

    capability = registry.get("jquants")
    with pytest.raises(ValueError, match="duplicate provider capability"):
        ProviderCapabilityRegistry((capability, capability))


def test_provider_capability_is_immutable_and_requires_canonical_name() -> None:
    capability = ProviderCapabilityRegistry().get("jquants")
    with pytest.raises(FrozenInstanceError):
        capability.provider = "other"  # type: ignore[misc]

    with pytest.raises(ValueError, match="canonical lowercase"):
        ProviderCapability(
            provider="JQuants",
            signal_validation_supported=True,
            executable_validation_supported=True,
            benchmark_supported=True,
            provider_price_basis="basis",
            maximum_expected_history="history",
            availability_lag="lag",
            notes=(),
        )
