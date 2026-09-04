"""Typed, mode-specific Phase 3 candidate domains and catalogs."""

from __future__ import annotations

from dataclasses import dataclass, replace

from config.phase3 import (
    MAX_CANDIDATES_PER_CATALOG,
    ExecutableCandidateCatalogConfig,
    ExecutableCandidateConfig,
    SignalCandidateCatalogConfig,
    SignalCandidateConfig,
)
from config.settings import StrategyConfig


@dataclass(frozen=True, slots=True)
class SignalCandidateDefinition:
    """The three strategy overrides allowed for Signal Validation."""

    candidate_id: str
    buy_atr_multiplier: float
    range_score_threshold: float
    adx_entry_max: float

    def __post_init__(self) -> None:
        SignalCandidateConfig(
            id=self.candidate_id,
            buy_atr_multiplier=self.buy_atr_multiplier,
            range_score_threshold=self.range_score_threshold,
            adx_entry_max=self.adx_entry_max,
        )

    @classmethod
    def from_config(cls, config: SignalCandidateConfig) -> SignalCandidateDefinition:
        """Convert the matching STEP 2 configuration type."""

        if not isinstance(config, SignalCandidateConfig):
            raise TypeError("config must be SignalCandidateConfig")
        return cls(
            candidate_id=config.id,
            buy_atr_multiplier=config.buy_atr_multiplier,
            range_score_threshold=config.range_score_threshold,
            adx_entry_max=config.adx_entry_max,
        )

    def apply(self, base_config: StrategyConfig) -> StrategyConfig:
        """Return a validated copy with only Signal candidate fields replaced."""

        _require_strategy_config(base_config)
        return replace(
            base_config,
            buy_atr_multiplier=self.buy_atr_multiplier,
            range_score_threshold=self.range_score_threshold,
            adx_entry_max=self.adx_entry_max,
        )


@dataclass(frozen=True, slots=True)
class ExecutableCandidateDefinition:
    """The four strategy overrides allowed for Executable Validation."""

    candidate_id: str
    buy_atr_multiplier: float
    sell_atr_multiplier: float
    range_score_threshold: float
    adx_entry_max: float

    def __post_init__(self) -> None:
        ExecutableCandidateConfig(
            id=self.candidate_id,
            buy_atr_multiplier=self.buy_atr_multiplier,
            sell_atr_multiplier=self.sell_atr_multiplier,
            range_score_threshold=self.range_score_threshold,
            adx_entry_max=self.adx_entry_max,
        )

    @classmethod
    def from_config(
        cls, config: ExecutableCandidateConfig
    ) -> ExecutableCandidateDefinition:
        """Convert the matching STEP 2 configuration type."""

        if not isinstance(config, ExecutableCandidateConfig):
            raise TypeError("config must be ExecutableCandidateConfig")
        return cls(
            candidate_id=config.id,
            buy_atr_multiplier=config.buy_atr_multiplier,
            sell_atr_multiplier=config.sell_atr_multiplier,
            range_score_threshold=config.range_score_threshold,
            adx_entry_max=config.adx_entry_max,
        )

    def apply(self, base_config: StrategyConfig) -> StrategyConfig:
        """Return a validated copy with only strategy candidate fields replaced."""

        _require_strategy_config(base_config)
        return replace(
            base_config,
            buy_atr_multiplier=self.buy_atr_multiplier,
            sell_atr_multiplier=self.sell_atr_multiplier,
            range_score_threshold=self.range_score_threshold,
            adx_entry_max=self.adx_entry_max,
        )


@dataclass(frozen=True, slots=True)
class SignalCandidateCatalog:
    """Ordered immutable runtime catalog for Signal candidates."""

    candidates: tuple[SignalCandidateDefinition, ...]
    maximum_candidates: int = MAX_CANDIDATES_PER_CATALOG

    def __post_init__(self) -> None:
        _validate_catalog(
            "Signal",
            self.candidates,
            self.maximum_candidates,
            SignalCandidateDefinition,
        )

    @classmethod
    def from_config(
        cls, config: SignalCandidateCatalogConfig
    ) -> SignalCandidateCatalog:
        """Preserve YAML order while converting the matching catalog config."""

        if not isinstance(config, SignalCandidateCatalogConfig):
            raise TypeError("config must be SignalCandidateCatalogConfig")
        return cls(
            candidates=tuple(
                SignalCandidateDefinition.from_config(candidate)
                for candidate in config.candidates
            ),
            maximum_candidates=config.maximum_candidates,
        )

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        """Return candidate identifiers in configured order."""

        return tuple(candidate.candidate_id for candidate in self.candidates)


@dataclass(frozen=True, slots=True)
class ExecutableCandidateCatalog:
    """Ordered immutable runtime catalog for Executable candidates."""

    candidates: tuple[ExecutableCandidateDefinition, ...]
    maximum_candidates: int = MAX_CANDIDATES_PER_CATALOG

    def __post_init__(self) -> None:
        _validate_catalog(
            "Executable",
            self.candidates,
            self.maximum_candidates,
            ExecutableCandidateDefinition,
        )

    @classmethod
    def from_config(
        cls, config: ExecutableCandidateCatalogConfig
    ) -> ExecutableCandidateCatalog:
        """Preserve YAML order while converting the matching catalog config."""

        if not isinstance(config, ExecutableCandidateCatalogConfig):
            raise TypeError("config must be ExecutableCandidateCatalogConfig")
        return cls(
            candidates=tuple(
                ExecutableCandidateDefinition.from_config(candidate)
                for candidate in config.candidates
            ),
            maximum_candidates=config.maximum_candidates,
        )

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        """Return candidate identifiers in configured order."""

        return tuple(candidate.candidate_id for candidate in self.candidates)


def _validate_catalog(
    name: str,
    candidates: object,
    maximum_candidates: object,
    candidate_type: type[SignalCandidateDefinition]
    | type[ExecutableCandidateDefinition],
) -> None:
    if (
        isinstance(maximum_candidates, bool)
        or not isinstance(maximum_candidates, int)
        or maximum_candidates <= 0
    ):
        raise ValueError("maximum_candidates must be a positive integer")
    if maximum_candidates > MAX_CANDIDATES_PER_CATALOG:
        raise ValueError(
            f"{name} maximum_candidates cannot exceed {MAX_CANDIDATES_PER_CATALOG}"
        )
    if not isinstance(candidates, tuple) or any(
        not isinstance(candidate, candidate_type) for candidate in candidates
    ):
        raise TypeError(
            f"{name} candidates must be a tuple of {candidate_type.__name__}"
        )
    if not candidates:
        raise ValueError(f"{name} candidate catalog cannot be empty")
    if len(candidates) > maximum_candidates:
        raise ValueError(
            f"{name} candidate count {len(candidates)} exceeds configured maximum "
            f"{maximum_candidates}"
        )
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError(f"{name} candidate IDs must be unique")


def _require_strategy_config(config: object) -> None:
    if not isinstance(config, StrategyConfig):
        raise TypeError("base_config must be StrategyConfig")
