"""Causal Signal Validation outcome evaluation for Phase 3."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd

from config.settings import StrategyConfig
from data import (
    CANONICAL_COLUMNS,
    CanonicalDataError,
    UnsupportedCorporateActionError,
    canonical_to_phase1,
    provider_price_basis,
    require_single_provider,
    validate_canonical_bars,
    validate_signal_price_contract,
)
from screening import RANGE_SCORE_DIVIDEND_POLICY, RANGE_SCORE_FORWARD_RETURN_MODE

from .candidates import SignalCandidateCatalog, SignalCandidateDefinition
from .capabilities import (
    AnalysisMode,
    ProviderCapabilityRegistry,
)
from .folds import (
    FoldValidationError,
    ForwardObservation,
    PurgePolicy,
    WalkForwardFold,
)
from .result import SignalTestSummary, ValidationCohort
from .selection import SignalValidationScore

SIGNAL_OUTCOME_FORWARD_RETURN_MODE = RANGE_SCORE_FORWARD_RETURN_MODE
SIGNAL_OUTCOME_DIVIDEND_POLICY = RANGE_SCORE_DIVIDEND_POLICY
UNSUPPORTED_CORPORATE_ACTION_REASON = "unsupported_corporate_action"
INSUFFICIENT_FEATURE_HISTORY_REASON = "insufficient_feature_history"
OVERLAPPING_FORWARD_WINDOW_REASON = "overlapping_forward_window"
NO_TEST_OBSERVATIONS_REASON = "no_test_observations"


class SignalEvaluationError(ValueError):
    """Raised when Signal Validation inputs or derived outcomes are invalid."""


@dataclass(frozen=True, slots=True)
class SignalOutcomeObservation:
    """One retained, non-overlapping forward outcome for a BUY condition."""

    fold_id: str
    provider: str
    candidate_id: str
    symbol: str
    feature_date: date
    label_start_date: date
    label_end_date: date
    signal_close: float
    signal_date_sma: float
    signal_date_atr: float
    buy_threshold: float
    range_score: float
    adx: float
    forward_return: float
    mean_reversion_target_hit: bool
    maximum_adverse_excursion: float
    maximum_adverse_excursion_magnitude: float
    maximum_favorable_excursion: float

    def __post_init__(self) -> None:
        for name in ("fold_id", "provider", "candidate_id", "symbol"):
            _require_non_empty_string(name, getattr(self, name))
        for name in ("feature_date", "label_start_date", "label_end_date"):
            _require_date(name, getattr(self, name))
        if not self.feature_date < self.label_start_date <= self.label_end_date:
            raise SignalEvaluationError(
                "outcome dates must satisfy feature_date < label_start_date "
                "<= label_end_date"
            )
        for name in ("signal_close", "signal_date_sma", "buy_threshold"):
            _require_positive_finite(name, getattr(self, name))
        for name in ("signal_date_atr", "range_score", "adx"):
            _require_non_negative_finite(name, getattr(self, name))
        if self.range_score > 100.0:
            raise SignalEvaluationError("range_score must be between 0 and 100")
        if not isinstance(self.mean_reversion_target_hit, bool):
            raise SignalEvaluationError("mean_reversion_target_hit must be a boolean")
        _require_finite("forward_return", self.forward_return)
        if self.forward_return <= -1.0:
            raise SignalEvaluationError("forward_return must be greater than -1")
        _require_finite("maximum_adverse_excursion", self.maximum_adverse_excursion)
        if not -1.0 < self.maximum_adverse_excursion <= 0.0:
            raise SignalEvaluationError(
                "maximum_adverse_excursion must be greater than -1 and at most 0"
            )
        _require_non_negative_finite(
            "maximum_adverse_excursion_magnitude",
            self.maximum_adverse_excursion_magnitude,
        )
        if not math.isclose(
            self.maximum_adverse_excursion_magnitude,
            -self.maximum_adverse_excursion,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise SignalEvaluationError(
                "maximum_adverse_excursion_magnitude must equal the negative signed MAE"
            )
        _require_non_negative_finite(
            "maximum_favorable_excursion", self.maximum_favorable_excursion
        )


@dataclass(frozen=True, slots=True)
class SignalObservationExclusion:
    """One candidate/symbol Signal excluded by purge or overlap."""

    fold_id: str
    provider: str
    candidate_id: str
    symbol: str
    feature_date: date
    reason: str

    def __post_init__(self) -> None:
        for name in (
            "fold_id",
            "provider",
            "candidate_id",
            "symbol",
            "reason",
        ):
            _require_non_empty_string(name, getattr(self, name))
        _require_date("feature_date", self.feature_date)


@dataclass(frozen=True, slots=True)
class SignalSymbolExclusion:
    """A candidate-independent symbol exclusion for one fold."""

    fold_id: str
    provider: str
    symbol: str
    status: str
    reason: str

    def __post_init__(self) -> None:
        for name in ("fold_id", "provider", "symbol", "status", "reason"):
            _require_non_empty_string(name, getattr(self, name))
        if self.status not in {"unsupported", "excluded"}:
            raise SignalEvaluationError(
                "symbol exclusion status must be 'unsupported' or 'excluded'"
            )
        allowed_reasons = {
            UNSUPPORTED_CORPORATE_ACTION_REASON,
            INSUFFICIENT_FEATURE_HISTORY_REASON,
        }
        if self.reason not in allowed_reasons:
            raise SignalEvaluationError(
                "unknown Signal Validation symbol exclusion reason"
            )


@dataclass(frozen=True, slots=True)
class SignalOutcomeEvaluationResult:
    """All candidate observations, exclusions, and scores for one fold."""

    provider: str
    provider_price_basis: str
    fold_id: str
    input_symbol_count: int
    admitted_symbol_count: int
    observations: tuple[SignalOutcomeObservation, ...]
    observation_exclusions: tuple[SignalObservationExclusion, ...]
    symbol_exclusions: tuple[SignalSymbolExclusion, ...]
    scores: tuple[SignalValidationScore, ...]

    def __post_init__(self) -> None:
        for name in ("provider", "provider_price_basis", "fold_id"):
            _require_non_empty_string(name, getattr(self, name))
        _require_non_negative_int("input_symbol_count", self.input_symbol_count)
        _require_non_negative_int("admitted_symbol_count", self.admitted_symbol_count)
        if self.admitted_symbol_count > self.input_symbol_count:
            raise SignalEvaluationError(
                "admitted_symbol_count cannot exceed input_symbol_count"
            )
        _require_tuple_of("observations", self.observations, SignalOutcomeObservation)
        _require_tuple_of(
            "observation_exclusions",
            self.observation_exclusions,
            SignalObservationExclusion,
        )
        _require_tuple_of(
            "symbol_exclusions", self.symbol_exclusions, SignalSymbolExclusion
        )
        _require_tuple_of("scores", self.scores, SignalValidationScore)
        if self.input_symbol_count != (
            self.admitted_symbol_count + len(self.symbol_exclusions)
        ):
            raise SignalEvaluationError(
                "input_symbol_count must equal admitted symbols plus symbol exclusions"
            )
        excluded_symbols = tuple(item.symbol for item in self.symbol_exclusions)
        if len(excluded_symbols) != len(set(excluded_symbols)):
            raise SignalEvaluationError("symbol exclusions must be unique by symbol")
        if excluded_symbols != tuple(sorted(excluded_symbols)):
            raise SignalEvaluationError("symbol exclusions must be sorted by symbol")
        score_ids = tuple(score.candidate_id for score in self.scores)
        if len(score_ids) != len(set(score_ids)):
            raise SignalEvaluationError("scores must be unique by candidate_id")
        candidate_order = {
            candidate_id: index for index, candidate_id in enumerate(score_ids)
        }
        self._validate_children(candidate_order)

    def _validate_children(self, candidate_order: dict[str, int]) -> None:
        expected_prefix = (self.fold_id, self.provider)
        for item in (*self.observations, *self.observation_exclusions):
            if (item.fold_id, item.provider) != expected_prefix:
                raise SignalEvaluationError(
                    "observation fold/provider must match its evaluation result"
                )
            if item.candidate_id not in candidate_order:
                raise SignalEvaluationError(
                    "observation candidate_id must have a matching score"
                )
        for item in self.symbol_exclusions:
            if (item.fold_id, item.provider) != expected_prefix:
                raise SignalEvaluationError(
                    "symbol exclusion fold/provider must match its evaluation result"
                )

        observation_keys = tuple(
            (candidate_order[item.candidate_id], item.symbol, item.feature_date)
            for item in self.observations
        )
        if len(observation_keys) != len(set(observation_keys)):
            raise SignalEvaluationError(
                "observations must be unique by candidate, symbol, and feature date"
            )
        if observation_keys != tuple(sorted(observation_keys)):
            raise SignalEvaluationError(
                "observations must follow catalog, symbol, and feature-date order"
            )
        exclusion_keys = tuple(
            (
                candidate_order[item.candidate_id],
                item.symbol,
                item.feature_date,
                item.reason,
            )
            for item in self.observation_exclusions
        )
        if exclusion_keys != tuple(sorted(exclusion_keys)):
            raise SignalEvaluationError(
                "observation exclusions must follow deterministic order"
            )
        excluded_symbols = {item.symbol for item in self.symbol_exclusions}
        if any(
            item.symbol in excluded_symbols
            for item in (*self.observations, *self.observation_exclusions)
        ):
            raise SignalEvaluationError(
                "symbol-level exclusions cannot have candidate observations"
            )
        for score in self.scores:
            actual_count = sum(
                item.candidate_id == score.candidate_id for item in self.observations
            )
            if score.observation_count != actual_count:
                raise SignalEvaluationError(
                    "score observation_count must match retained observations"
                )


@dataclass(frozen=True, slots=True)
class SignalTestSymbolExclusion:
    """One Validation-cohort symbol excluded from Signal Test evaluation."""

    fold_id: str
    provider: str
    symbol: str
    status: str
    reason: str

    def __post_init__(self) -> None:
        for name in ("fold_id", "provider", "symbol", "status", "reason"):
            _require_non_empty_string(name, getattr(self, name))
        allowed = {
            UNSUPPORTED_CORPORATE_ACTION_REASON: "unsupported",
            NO_TEST_OBSERVATIONS_REASON: "excluded",
            INSUFFICIENT_FEATURE_HISTORY_REASON: "excluded",
        }
        if self.reason not in allowed:
            raise SignalEvaluationError("unknown Signal Test symbol exclusion reason")
        if self.status != allowed[self.reason]:
            raise SignalEvaluationError(
                "Signal Test symbol exclusion status does not match its reason"
            )


@dataclass(frozen=True, slots=True)
class SignalTestEvaluationResult:
    """One selected Signal candidate's observations on a Test interval."""

    provider: str
    provider_price_basis: str
    fold_id: str
    candidate_id: str
    requested_symbols: tuple[str, ...]
    requested_symbol_count: int
    admitted_symbol_count: int
    observations: tuple[SignalOutcomeObservation, ...]
    observation_exclusions: tuple[SignalObservationExclusion, ...]
    symbol_exclusions: tuple[SignalTestSymbolExclusion, ...]
    summary: SignalTestSummary

    def __post_init__(self) -> None:
        for name in ("provider", "provider_price_basis", "fold_id", "candidate_id"):
            _require_non_empty_string(name, getattr(self, name))
        if not isinstance(self.requested_symbols, tuple) or any(
            not isinstance(symbol, str) or not symbol.strip()
            for symbol in self.requested_symbols
        ):
            raise TypeError("requested_symbols must be a tuple of non-empty strings")
        if self.requested_symbols != tuple(sorted(self.requested_symbols)):
            raise SignalEvaluationError("requested_symbols must be sorted")
        if len(self.requested_symbols) != len(set(self.requested_symbols)):
            raise SignalEvaluationError("requested_symbols must be unique")
        _require_non_negative_int("requested_symbol_count", self.requested_symbol_count)
        _require_non_negative_int("admitted_symbol_count", self.admitted_symbol_count)
        if self.requested_symbol_count != len(self.requested_symbols):
            raise SignalEvaluationError(
                "requested_symbol_count must match requested_symbols"
            )
        _require_tuple_of("observations", self.observations, SignalOutcomeObservation)
        _require_tuple_of(
            "observation_exclusions",
            self.observation_exclusions,
            SignalObservationExclusion,
        )
        _require_tuple_of(
            "symbol_exclusions", self.symbol_exclusions, SignalTestSymbolExclusion
        )
        if not isinstance(self.summary, SignalTestSummary):
            raise TypeError("summary must be SignalTestSummary")
        excluded_symbols = tuple(item.symbol for item in self.symbol_exclusions)
        if excluded_symbols != tuple(sorted(excluded_symbols)):
            raise SignalEvaluationError("Signal Test symbol exclusions must be sorted")
        if len(excluded_symbols) != len(set(excluded_symbols)):
            raise SignalEvaluationError("Signal Test symbol exclusions must be unique")
        if not set(excluded_symbols).issubset(self.requested_symbols):
            raise SignalEvaluationError(
                "Signal Test exclusions must belong to requested_symbols"
            )
        if self.requested_symbol_count != (
            self.admitted_symbol_count + len(self.symbol_exclusions)
        ):
            raise SignalEvaluationError(
                "requested_symbol_count must equal admitted symbols plus exclusions"
            )
        expected_prefix = (self.fold_id, self.provider, self.candidate_id)
        for item in (*self.observations, *self.observation_exclusions):
            if (item.fold_id, item.provider, item.candidate_id) != expected_prefix:
                raise SignalEvaluationError(
                    "Signal Test observation fold/provider/candidate must match result"
                )
            if item.symbol not in self.requested_symbols:
                raise SignalEvaluationError(
                    "Signal Test observations must belong to requested_symbols"
                )
        for item in self.symbol_exclusions:
            if (item.fold_id, item.provider) != expected_prefix[:2]:
                raise SignalEvaluationError(
                    "Signal Test exclusion fold/provider must match result"
                )
        observation_keys = tuple(
            (item.symbol, item.feature_date) for item in self.observations
        )
        if observation_keys != tuple(sorted(observation_keys)) or len(
            observation_keys
        ) != len(set(observation_keys)):
            raise SignalEvaluationError(
                "Signal Test observations must be unique and sorted"
            )
        exclusion_keys = tuple(
            (item.symbol, item.feature_date, item.reason)
            for item in self.observation_exclusions
        )
        if exclusion_keys != tuple(sorted(exclusion_keys)):
            raise SignalEvaluationError(
                "Signal Test observation exclusions must be sorted"
            )
        if any(
            item.symbol in set(excluded_symbols)
            for item in (*self.observations, *self.observation_exclusions)
        ):
            raise SignalEvaluationError(
                "symbol-level Test exclusions cannot have observations"
            )
        if self.summary.candidate_id != self.candidate_id:
            raise SignalEvaluationError(
                "Signal Test summary candidate must match result"
            )
        if self.summary.observation_count != len(self.observations):
            raise SignalEvaluationError(
                "Signal Test summary observation_count must match observations"
            )


@dataclass(frozen=True, slots=True)
class SignalOutcomeEvaluator:
    """Evaluate every Signal candidate on Validation data only."""

    base_config: StrategyConfig
    capability_registry: ProviderCapabilityRegistry

    def __post_init__(self) -> None:
        if not isinstance(self.base_config, StrategyConfig):
            raise TypeError("base_config must be StrategyConfig")
        if not isinstance(self.capability_registry, ProviderCapabilityRegistry):
            raise TypeError("capability_registry must be ProviderCapabilityRegistry")

    def evaluate_validation(
        self,
        bars: pd.DataFrame,
        fold: WalkForwardFold,
        catalog: SignalCandidateCatalog,
        purge_policy: PurgePolicy,
    ) -> SignalOutcomeEvaluationResult:
        """Return causal Validation outcomes without selecting a candidate."""

        if not isinstance(bars, pd.DataFrame):
            raise TypeError("bars must be a pandas DataFrame")
        if not isinstance(fold, WalkForwardFold):
            raise TypeError("fold must be WalkForwardFold")
        if not isinstance(catalog, SignalCandidateCatalog):
            raise TypeError("catalog must be SignalCandidateCatalog")
        if not isinstance(purge_policy, PurgePolicy):
            raise TypeError("purge_policy must be PurgePolicy")

        missing = sorted(set(CANONICAL_COLUMNS).difference(bars.columns))
        if missing:
            raise CanonicalDataError("Missing canonical columns: " + ", ".join(missing))
        _validate_date_column(bars)
        in_scope_bars = bars.loc[bars["date"].dt.date < fold.test_end].copy()
        if in_scope_bars.empty:
            raise SignalEvaluationError("no observations exist before fold.test_end")

        in_scope_bars = in_scope_bars.sort_values(
            ["symbol", "date"], kind="mergesort"
        ).reset_index(drop=True)
        before_test_start = in_scope_bars.loc[
            in_scope_bars["date"].dt.date < fold.test_start
        ].copy()
        if before_test_start.empty:
            raise SignalEvaluationError("no observations exist before fold.test_start")

        # Test rows supply observed session dates to PurgePolicy only. Freeze the
        # evaluation provider and symbol population before Test metadata exists.
        raw_provider = require_single_provider(before_test_start)
        capability = self.capability_registry.require(
            raw_provider, AnalysisMode.SIGNAL_VALIDATION
        )
        expected_basis = provider_price_basis(raw_provider)
        if capability.provider_price_basis != expected_basis:
            raise SignalEvaluationError(
                "provider_price_basis does not match the provider capability "
                "declaration"
            )
        if fold.embargo_sessions < purge_policy.forward_sessions:
            raise FoldValidationError(
                f"{fold.fold_id} embargo_sessions ({fold.embargo_sessions}) must "
                "be greater than or equal to PurgePolicy.forward_sessions "
                f"({purge_policy.forward_sessions})"
            )
        provider = capability.provider
        validate_canonical_bars(before_test_start, expected_provider=raw_provider)
        symbols = tuple(sorted(set(before_test_start["symbol"].astype(str))))
        input_symbol_count = len(symbols)
        session_scope = (
            in_scope_bars.loc[
                (in_scope_bars["date"].dt.date >= fold.train_start)
                & in_scope_bars["symbol"].astype(str).isin(symbols),
                ["symbol", "date"],
            ]
            .drop_duplicates(["symbol", "date"], keep="first")
            .reset_index(drop=True)
        )
        _validate_session_structure(session_scope)

        signal_frames: dict[str, pd.DataFrame] = {}
        session_dates: dict[str, tuple[date, ...]] = {}
        symbol_exclusions: list[SignalSymbolExclusion] = []
        first_candidate_frames: dict[str, pd.DataFrame] = {}
        first_candidate = catalog.candidates[0]

        for symbol in symbols:
            symbol_bars = before_test_start.loc[
                before_test_start["symbol"].astype(str) == symbol
            ].copy()
            evaluation_bars = symbol_bars.loc[
                (symbol_bars["date"].dt.date >= fold.train_start)
                & (symbol_bars["date"].dt.date < fold.test_start)
            ].copy()
            if evaluation_bars.empty:
                symbol_exclusions.append(
                    _symbol_exclusion(
                        fold,
                        provider,
                        symbol,
                        "excluded",
                        INSUFFICIENT_FEATURE_HISTORY_REASON,
                    )
                )
                continue
            try:
                _reject_unverified_corporate_action(evaluation_bars, provider)
            except UnsupportedCorporateActionError:
                symbol_exclusions.append(
                    _symbol_exclusion(
                        fold,
                        provider,
                        symbol,
                        "unsupported",
                        UNSUPPORTED_CORPORATE_ACTION_REASON,
                    )
                )
                continue

            adapted = canonical_to_phase1(evaluation_bars, symbol=symbol)
            validate_signal_price_contract(adapted)
            signal_frame = adapted.loc[
                :, ["date", "open", "high", "low", "close", "volume"]
            ].copy()
            signal_frames[symbol] = signal_frame
            session_dates[symbol] = tuple(
                session_scope.loc[
                    session_scope["symbol"].astype(str) == symbol, "date"
                ].dt.date
            )

            prepared = self._prepare_candidate(signal_frame, first_candidate)
            usable = _usable_validation_features(prepared, fold)
            if usable.empty:
                symbol_exclusions.append(
                    _symbol_exclusion(
                        fold,
                        provider,
                        symbol,
                        "excluded",
                        INSUFFICIENT_FEATURE_HISTORY_REASON,
                    )
                )
                del signal_frames[symbol]
                del session_dates[symbol]
                continue
            first_candidate_frames[symbol] = prepared

        admitted_symbols = tuple(sorted(signal_frames))
        observations: list[SignalOutcomeObservation] = []
        observation_exclusions: list[SignalObservationExclusion] = []

        for candidate_index, candidate in enumerate(catalog.candidates):
            for symbol in admitted_symbols:
                prepared = (
                    first_candidate_frames[symbol]
                    if candidate_index == 0
                    else self._prepare_candidate(signal_frames[symbol], candidate)
                )
                validation = prepared.loc[
                    prepared["date"].dt.date.map(fold.contains_validation_date)
                ]
                signal_rows = validation.loc[
                    validation["entry_condition"].fillna(False).astype(bool)
                ].copy()
                feature_dates = tuple(signal_rows["date"].dt.date)
                assessed = purge_policy.assess_validation(
                    fold,
                    symbol,
                    feature_dates,
                    session_dates[symbol],
                )
                retained: list[ForwardObservation] = []
                for assessment in assessed:
                    if not assessment.retained:
                        observation_exclusions.append(
                            SignalObservationExclusion(
                                fold_id=fold.fold_id,
                                provider=provider,
                                candidate_id=candidate.candidate_id,
                                symbol=symbol,
                                feature_date=assessment.feature_date,
                                reason=assessment.purge_reason or "",
                            )
                        )
                        continue
                    if (
                        retained
                        and assessment.feature_date <= retained[-1].label_end_date
                    ):
                        observation_exclusions.append(
                            SignalObservationExclusion(
                                fold_id=fold.fold_id,
                                provider=provider,
                                candidate_id=candidate.candidate_id,
                                symbol=symbol,
                                feature_date=assessment.feature_date,
                                reason=OVERLAPPING_FORWARD_WINDOW_REASON,
                            )
                        )
                        continue
                    retained.append(assessment)

                row_by_date = {
                    value.date(): row
                    for value, row in zip(
                        signal_rows["date"],
                        (row for _, row in signal_rows.iterrows()),
                        strict=True,
                    )
                }
                close_by_date = dict(
                    zip(
                        signal_frames[symbol]["date"].dt.date,
                        signal_frames[symbol]["close"].astype(float),
                        strict=True,
                    )
                )
                for assessment in retained:
                    observations.append(
                        _build_observation(
                            fold=fold,
                            provider=provider,
                            candidate=candidate,
                            assessment=assessment,
                            signal_row=row_by_date[assessment.feature_date],
                            observed_sessions=session_dates[symbol],
                            close_by_date=close_by_date,
                            forward_sessions=purge_policy.forward_sessions,
                        )
                    )

        scores = _aggregate_scores(catalog, observations)
        return SignalOutcomeEvaluationResult(
            provider=provider,
            provider_price_basis=expected_basis,
            fold_id=fold.fold_id,
            input_symbol_count=input_symbol_count,
            admitted_symbol_count=len(admitted_symbols),
            observations=tuple(observations),
            observation_exclusions=tuple(observation_exclusions),
            symbol_exclusions=tuple(
                sorted(symbol_exclusions, key=lambda item: item.symbol)
            ),
            scores=scores,
        )

    def evaluate_test(
        self,
        bars: pd.DataFrame,
        fold: WalkForwardFold,
        candidate: SignalCandidateDefinition,
        purge_policy: PurgePolicy,
        cohort: ValidationCohort,
    ) -> SignalTestEvaluationResult:
        """Evaluate one selected Signal candidate exactly on the Test interval."""

        if not isinstance(bars, pd.DataFrame):
            raise TypeError("bars must be a pandas DataFrame")
        if not isinstance(fold, WalkForwardFold):
            raise TypeError("fold must be WalkForwardFold")
        if not isinstance(candidate, SignalCandidateDefinition):
            raise TypeError("candidate must be SignalCandidateDefinition")
        if not isinstance(purge_policy, PurgePolicy):
            raise TypeError("purge_policy must be PurgePolicy")
        if not isinstance(cohort, ValidationCohort):
            raise TypeError("cohort must be ValidationCohort")
        if not cohort.symbols:
            raise SignalEvaluationError("Signal Test cohort must not be empty")
        missing = sorted(set(CANONICAL_COLUMNS).difference(bars.columns))
        if missing:
            raise CanonicalDataError("Missing canonical columns: " + ", ".join(missing))
        _validate_date_column(bars)
        if fold.embargo_sessions < purge_policy.forward_sessions:
            raise FoldValidationError(
                f"{fold.fold_id} embargo_sessions ({fold.embargo_sessions}) must "
                "be greater than or equal to PurgePolicy.forward_sessions "
                f"({purge_policy.forward_sessions})"
            )

        capability = self.capability_registry.require(
            cohort.provider, AnalysisMode.SIGNAL_VALIDATION
        )
        expected_basis = provider_price_basis(cohort.provider)
        if (
            capability.provider_price_basis != expected_basis
            or cohort.provider_price_basis != expected_basis
        ):
            raise SignalEvaluationError(
                "provider_price_basis does not match the frozen Validation cohort"
            )
        evaluation_bars = bars.loc[
            (bars["date"].dt.date >= fold.train_start)
            & (bars["date"].dt.date < fold.test_end)
            & bars["symbol"].astype(str).isin(cohort.symbols)
        ].copy()
        evaluation_bars = evaluation_bars.sort_values(
            ["symbol", "date"], kind="mergesort"
        ).reset_index(drop=True)
        if not evaluation_bars.empty:
            raw_provider = require_single_provider(evaluation_bars)
            if raw_provider != cohort.provider:
                raise SignalEvaluationError(
                    "Test provider does not match the frozen Validation cohort"
                )
            _validate_session_structure(evaluation_bars)
            validate_canonical_bars(evaluation_bars, expected_provider=cohort.provider)

        provider = capability.provider
        signal_frames: dict[str, pd.DataFrame] = {}
        session_dates: dict[str, tuple[date, ...]] = {}
        prepared_frames: dict[str, pd.DataFrame] = {}
        symbol_exclusions: list[SignalTestSymbolExclusion] = []
        for symbol in cohort.symbols:
            symbol_bars = evaluation_bars.loc[
                evaluation_bars["symbol"].astype(str) == symbol
            ].copy()
            test_bars = symbol_bars.loc[symbol_bars["date"].dt.date >= fold.test_start]
            if test_bars.empty:
                symbol_exclusions.append(
                    _test_symbol_exclusion(
                        fold,
                        provider,
                        symbol,
                        "excluded",
                        NO_TEST_OBSERVATIONS_REASON,
                    )
                )
                continue
            try:
                _reject_unverified_corporate_action(symbol_bars, provider)
            except UnsupportedCorporateActionError:
                symbol_exclusions.append(
                    _test_symbol_exclusion(
                        fold,
                        provider,
                        symbol,
                        "unsupported",
                        UNSUPPORTED_CORPORATE_ACTION_REASON,
                    )
                )
                continue
            adapted = canonical_to_phase1(symbol_bars, symbol=symbol)
            validate_signal_price_contract(adapted)
            signal_frame = adapted.loc[
                :, ["date", "open", "high", "low", "close", "volume"]
            ].copy()
            prepared = self._prepare_candidate(signal_frame, candidate)
            if _usable_test_features(prepared, fold).empty:
                symbol_exclusions.append(
                    _test_symbol_exclusion(
                        fold,
                        provider,
                        symbol,
                        "excluded",
                        INSUFFICIENT_FEATURE_HISTORY_REASON,
                    )
                )
                continue
            signal_frames[symbol] = signal_frame
            prepared_frames[symbol] = prepared
            session_dates[symbol] = tuple(symbol_bars["date"].dt.date)

        observations: list[SignalOutcomeObservation] = []
        observation_exclusions: list[SignalObservationExclusion] = []
        for symbol in sorted(signal_frames):
            prepared = prepared_frames[symbol]
            test = prepared.loc[prepared["date"].dt.date.map(fold.contains_test_date)]
            signal_rows = test.loc[
                test["entry_condition"].fillna(False).astype(bool)
            ].copy()
            assessed = purge_policy.assess_test(
                fold,
                symbol,
                tuple(signal_rows["date"].dt.date),
                session_dates[symbol],
            )
            retained: list[ForwardObservation] = []
            for assessment in assessed:
                if not assessment.retained:
                    observation_exclusions.append(
                        SignalObservationExclusion(
                            fold_id=fold.fold_id,
                            provider=provider,
                            candidate_id=candidate.candidate_id,
                            symbol=symbol,
                            feature_date=assessment.feature_date,
                            reason=assessment.purge_reason or "",
                        )
                    )
                    continue
                if retained and assessment.feature_date <= retained[-1].label_end_date:
                    observation_exclusions.append(
                        SignalObservationExclusion(
                            fold_id=fold.fold_id,
                            provider=provider,
                            candidate_id=candidate.candidate_id,
                            symbol=symbol,
                            feature_date=assessment.feature_date,
                            reason=OVERLAPPING_FORWARD_WINDOW_REASON,
                        )
                    )
                    continue
                retained.append(assessment)

            row_by_date = {
                value.date(): row
                for value, row in zip(
                    signal_rows["date"],
                    (row for _, row in signal_rows.iterrows()),
                    strict=True,
                )
            }
            close_by_date = dict(
                zip(
                    signal_frames[symbol]["date"].dt.date,
                    signal_frames[symbol]["close"].astype(float),
                    strict=True,
                )
            )
            for assessment in retained:
                observations.append(
                    _build_observation(
                        fold=fold,
                        provider=provider,
                        candidate=candidate,
                        assessment=assessment,
                        signal_row=row_by_date[assessment.feature_date],
                        observed_sessions=session_dates[symbol],
                        close_by_date=close_by_date,
                        forward_sessions=purge_policy.forward_sessions,
                    )
                )

        summary = _signal_test_summary(candidate.candidate_id, observations)
        return SignalTestEvaluationResult(
            provider=provider,
            provider_price_basis=expected_basis,
            fold_id=fold.fold_id,
            candidate_id=candidate.candidate_id,
            requested_symbols=cohort.symbols,
            requested_symbol_count=len(cohort.symbols),
            admitted_symbol_count=len(signal_frames),
            observations=tuple(observations),
            observation_exclusions=tuple(observation_exclusions),
            symbol_exclusions=tuple(symbol_exclusions),
            summary=summary,
        )

    def _prepare_candidate(
        self,
        signal_frame: pd.DataFrame,
        candidate: SignalCandidateDefinition,
    ) -> pd.DataFrame:
        config = candidate.apply(self.base_config)
        detected = config.create_detector().transform(signal_frame.copy())
        scored = config.create_scorer().transform(detected)
        return config.create_strategy().prepare(scored)


def _build_observation(
    *,
    fold: WalkForwardFold,
    provider: str,
    candidate: SignalCandidateDefinition,
    assessment: ForwardObservation,
    signal_row: pd.Series,
    observed_sessions: tuple[date, ...],
    close_by_date: dict[date, float],
    forward_sessions: int,
) -> SignalOutcomeObservation:
    feature_date = assessment.feature_date
    label_start_date = assessment.label_start_date
    label_end_date = assessment.label_end_date
    if label_start_date is None or label_end_date is None:
        raise SignalEvaluationError("retained labels require start and end dates")
    positions = {value: index for index, value in enumerate(observed_sessions)}
    position = positions[feature_date]
    path_dates = observed_sessions[position + 1 : position + forward_sessions + 1]
    if (
        len(path_dates) != forward_sessions
        or path_dates[0] != label_start_date
        or path_dates[-1] != label_end_date
    ):
        raise SignalEvaluationError(
            "PurgePolicy label dates do not match the observed-session path"
        )
    try:
        future_closes = np.asarray(
            [close_by_date[path_date] for path_date in path_dates], dtype=float
        )
    except KeyError as error:
        raise SignalEvaluationError(
            "forward label date has no adjusted Signal Close"
        ) from error

    signal_close = _as_finite_float("signal_close", signal_row["close"])
    path_returns = future_closes / signal_close - 1.0
    if not np.isfinite(path_returns).all():
        raise SignalEvaluationError("forward path returns must be finite")
    signed_mae = min(0.0, float(path_returns.min()))
    signal_date_sma = _as_finite_float("signal_date_sma", signal_row["sma"])
    return SignalOutcomeObservation(
        fold_id=fold.fold_id,
        provider=provider,
        candidate_id=candidate.candidate_id,
        symbol=assessment.symbol,
        feature_date=feature_date,
        label_start_date=label_start_date,
        label_end_date=label_end_date,
        signal_close=signal_close,
        signal_date_sma=signal_date_sma,
        signal_date_atr=_as_finite_float("signal_date_atr", signal_row["atr"]),
        buy_threshold=_as_finite_float("buy_threshold", signal_row["buy_threshold"]),
        range_score=_as_finite_float("range_score", signal_row["range_score"]),
        adx=_as_finite_float("adx", signal_row["adx"]),
        forward_return=float(path_returns[-1]),
        mean_reversion_target_hit=bool((future_closes >= signal_date_sma).any()),
        maximum_adverse_excursion=signed_mae,
        maximum_adverse_excursion_magnitude=-signed_mae,
        maximum_favorable_excursion=max(0.0, float(path_returns.max())),
    )


def _aggregate_scores(
    catalog: SignalCandidateCatalog,
    observations: list[SignalOutcomeObservation],
) -> tuple[SignalValidationScore, ...]:
    scores = []
    for candidate_id in catalog.candidate_ids:
        selected = [item for item in observations if item.candidate_id == candidate_id]
        if not selected:
            scores.append(SignalValidationScore(candidate_id, 0, None, None, None))
            continue
        scores.append(
            SignalValidationScore(
                candidate_id=candidate_id,
                observation_count=len(selected),
                mean_reversion_target_hit_rate=float(
                    np.mean([item.mean_reversion_target_hit for item in selected])
                ),
                median_forward_return=float(
                    np.median([item.forward_return for item in selected])
                ),
                median_mae_magnitude=float(
                    np.median(
                        [item.maximum_adverse_excursion_magnitude for item in selected]
                    )
                ),
            )
        )
    return tuple(scores)


def _signal_test_summary(
    candidate_id: str,
    observations: list[SignalOutcomeObservation],
) -> SignalTestSummary:
    if not observations:
        return SignalTestSummary(candidate_id, 0, None, None, None)
    return SignalTestSummary(
        candidate_id=candidate_id,
        observation_count=len(observations),
        mean_reversion_target_hit_rate=float(
            np.mean([item.mean_reversion_target_hit for item in observations])
        ),
        median_forward_return=float(
            np.median([item.forward_return for item in observations])
        ),
        median_mae_magnitude=float(
            np.median(
                [item.maximum_adverse_excursion_magnitude for item in observations]
            )
        ),
    )


def _usable_validation_features(
    prepared: pd.DataFrame, fold: WalkForwardFold
) -> pd.DataFrame:
    validation = prepared.loc[
        prepared["date"].dt.date.map(fold.contains_validation_date)
    ]
    required = ("sma", "atr", "adx", "range_score", "buy_threshold")
    return validation.loc[validation.loc[:, required].notna().all(axis=1)]


def _usable_test_features(
    prepared: pd.DataFrame, fold: WalkForwardFold
) -> pd.DataFrame:
    test = prepared.loc[prepared["date"].dt.date.map(fold.contains_test_date)]
    required = ("sma", "atr", "adx", "range_score", "buy_threshold")
    finite = np.isfinite(test.loc[:, required].to_numpy(dtype=float)).all(axis=1)
    return test.loc[finite]


def _reject_unverified_corporate_action(
    symbol_bars: pd.DataFrame, provider: str
) -> None:
    if provider == "yfinance":
        split = pd.to_numeric(symbol_bars["stock_split"], errors="coerce")
        if split.fillna(0.0).ne(0.0).any():
            raise UnsupportedCorporateActionError(
                "yfinance Stock Splits != 0 in the evaluation interval"
            )
        return
    if provider == "jquants":
        adjustment = pd.to_numeric(symbol_bars["adjustment_factor"], errors="coerce")
        if not adjustment.eq(1.0).all():
            raise UnsupportedCorporateActionError(
                "J-Quants adjustment_factor != 1 in the evaluation interval"
            )
        return
    raise SignalEvaluationError(
        f"unsupported provider for Signal Validation: {provider}"
    )


def _validate_session_structure(frame: pd.DataFrame) -> None:
    _validate_date_column(frame)
    duplicated = frame.duplicated(["symbol", "date"], keep=False)
    if duplicated.any():
        raise CanonicalDataError("duplicate symbol/date observations detected")
    for symbol, group in frame.groupby("symbol", sort=False):
        if not group["date"].is_monotonic_increasing:
            raise CanonicalDataError(f"dates for {symbol} must be ascending")


def _validate_date_column(frame: pd.DataFrame) -> None:
    if not pd.api.types.is_datetime64_any_dtype(frame["date"].dtype):
        raise CanonicalDataError("canonical date must have a pandas datetime dtype")
    if frame["date"].isna().any():
        raise CanonicalDataError("canonical date contains invalid values")


def _symbol_exclusion(
    fold: WalkForwardFold,
    provider: str,
    symbol: str,
    status: str,
    reason: str,
) -> SignalSymbolExclusion:
    return SignalSymbolExclusion(
        fold_id=fold.fold_id,
        provider=provider,
        symbol=symbol,
        status=status,
        reason=reason,
    )


def _test_symbol_exclusion(
    fold: WalkForwardFold,
    provider: str,
    symbol: str,
    status: str,
    reason: str,
) -> SignalTestSymbolExclusion:
    return SignalTestSymbolExclusion(
        fold_id=fold.fold_id,
        provider=provider,
        symbol=symbol,
        status=status,
        reason=reason,
    )


def _as_finite_float(name: str, value: object) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError) as error:
        raise SignalEvaluationError(f"{name} must be numeric") from error
    _require_finite(name, converted)
    return converted


def _require_non_empty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SignalEvaluationError(f"{name} must be a non-empty string")


def _require_date(name: str, value: object) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise SignalEvaluationError(f"{name} must be a date")


def _require_finite(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise SignalEvaluationError(f"{name} must be a finite number")
    if not math.isfinite(float(value)):
        raise SignalEvaluationError(f"{name} must be a finite number")


def _require_positive_finite(name: str, value: object) -> None:
    _require_finite(name, value)
    if float(value) <= 0.0:
        raise SignalEvaluationError(f"{name} must be greater than zero")


def _require_non_negative_finite(name: str, value: object) -> None:
    _require_finite(name, value)
    if float(value) < 0.0:
        raise SignalEvaluationError(f"{name} must be non-negative")


def _require_non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SignalEvaluationError(f"{name} must be a non-negative integer")


def _require_tuple_of(name: str, value: object, item_type: type) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(item, item_type) for item in value
    ):
        raise TypeError(f"{name} must be a tuple of {item_type.__name__}")
