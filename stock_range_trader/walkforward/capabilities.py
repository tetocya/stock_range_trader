"""Provider capability contracts for Phase 3 validation modes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from data.price_policy import provider_price_basis


class AnalysisMode(str, Enum):
    """The two deliberately separate Phase 3 analysis modes."""

    SIGNAL_VALIDATION = "signal_validation"
    EXECUTABLE_VALIDATION = "executable_validation"

    @classmethod
    def parse(cls, value: AnalysisMode | str) -> AnalysisMode:
        """Return a validated mode without accepting ambiguous aliases."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValueError("analysis mode must be a string or AnalysisMode")
        normalized = value.strip().lower()
        try:
            return cls(normalized)
        except ValueError as error:
            allowed = ", ".join(mode.value for mode in cls)
            raise ValueError(f"analysis mode must be one of: {allowed}") from error


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    """Immutable declaration of one provider's Phase 3 capabilities."""

    provider: str
    signal_validation_supported: bool
    executable_validation_supported: bool
    benchmark_supported: bool
    provider_price_basis: str
    maximum_expected_history: str
    availability_lag: str
    notes: tuple[str, ...]

    def __post_init__(self) -> None:
        _canonical_provider(self.provider)
        for name in (
            "signal_validation_supported",
            "executable_validation_supported",
            "benchmark_supported",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        for name in (
            "provider_price_basis",
            "maximum_expected_history",
            "availability_lag",
        ):
            _non_empty_string(name, getattr(self, name))
        if not isinstance(self.notes, tuple) or any(
            not isinstance(note, str) or not note.strip() for note in self.notes
        ):
            raise ValueError("notes must be a tuple of non-empty strings")

    def supports(self, mode: AnalysisMode | str) -> bool:
        """Return whether the provider supports the requested analysis mode."""

        parsed = AnalysisMode.parse(mode)
        if parsed is AnalysisMode.SIGNAL_VALIDATION:
            return self.signal_validation_supported
        return self.executable_validation_supported


class ProviderCapabilityError(ValueError):
    """Raised before computation for unsupported provider/mode requests."""


class ProviderCapabilityRegistry:
    """Read-only provider capability lookup and pre-computation gate."""

    __slots__ = ("_capabilities",)

    def __init__(
        self, capabilities: Iterable[ProviderCapability] | None = None
    ) -> None:
        selected = (
            DEFAULT_PROVIDER_CAPABILITIES
            if capabilities is None
            else tuple(capabilities)
        )
        indexed: dict[str, ProviderCapability] = {}
        for capability in selected:
            if not isinstance(capability, ProviderCapability):
                raise TypeError("capabilities must contain ProviderCapability values")
            provider = _canonical_provider(capability.provider)
            if provider in indexed:
                raise ValueError(f"duplicate provider capability: {provider}")
            indexed[provider] = capability
        if not indexed:
            raise ValueError("at least one provider capability is required")
        self._capabilities = MappingProxyType(indexed)

    @property
    def providers(self) -> tuple[str, ...]:
        """Return registered provider names in deterministic order."""

        return tuple(sorted(self._capabilities))

    def get(self, provider: str) -> ProviderCapability:
        """Return a capability or raise for an unknown provider."""

        normalized = _lookup_provider(provider)
        try:
            return self._capabilities[normalized]
        except KeyError as error:
            known = ", ".join(self.providers)
            raise ProviderCapabilityError(
                f"unknown provider {normalized!r}; registered providers: {known}"
            ) from error

    def require(
        self,
        provider: str,
        mode: AnalysisMode | str,
        *,
        require_benchmark: bool = False,
    ) -> ProviderCapability:
        """Require support and return the declaration before any computation."""

        capability = self.get(provider)
        parsed_mode = AnalysisMode.parse(mode)
        if not capability.supports(parsed_mode):
            raise ProviderCapabilityError(
                f"provider {capability.provider!r} does not support {parsed_mode.value}"
            )
        if require_benchmark and not capability.benchmark_supported:
            raise ProviderCapabilityError(
                f"provider {capability.provider!r} does not support an executable "
                "benchmark"
            )
        return capability


def _canonical_provider(provider: str) -> str:
    _non_empty_string("provider", provider)
    if provider != provider.strip().lower():
        raise ValueError("ProviderCapability.provider must be canonical lowercase")
    return provider


def _lookup_provider(provider: str) -> str:
    _non_empty_string("provider", provider)
    return provider.strip().lower()


def _non_empty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


DEFAULT_PROVIDER_CAPABILITIES: tuple[ProviderCapability, ...] = (
    ProviderCapability(
        provider="yfinance",
        signal_validation_supported=True,
        executable_validation_supported=False,
        benchmark_supported=False,
        provider_price_basis=provider_price_basis("yfinance"),
        maximum_expected_history="target_up_to_5_years",
        availability_lag="provider_dependent",
        notes=(
            "signal_validation_only",
            "executable_results_and_benchmarks_are_unsupported",
            "adjusted_signal_prices_may_reflect_provider_distribution_adjustments",
        ),
    ),
    ProviderCapability(
        provider="jquants",
        signal_validation_supported=True,
        executable_validation_supported=True,
        benchmark_supported=True,
        provider_price_basis=provider_price_basis("jquants"),
        maximum_expected_history="free_plan_2_years_excluding_availability_lag",
        availability_lag="free_plan_12_weeks",
        notes=(
            "executable_use_requires_validate_backtest_price_contract",
            "corporate_action_failures_are_symbol_level_unsupported",
            "free_plan_limits_must_be_recorded_at_run_time",
        ),
    ),
)
