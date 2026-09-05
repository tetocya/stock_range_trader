"""Executable Candidate Validation on isolated J-Quants trading windows."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype

from backtest import BacktestResult, BacktestWindow, OrderStatus
from config.settings import StrategyConfig
from data import (
    CANONICAL_COLUMNS,
    CanonicalDataError,
    UnsupportedCorporateActionError,
    canonical_to_phase1,
    provider_price_basis,
    require_single_provider,
    validate_backtest_price_contract,
    validate_canonical_bars,
)
from metrics import PerformanceMetrics, calculate_backtest_metrics

from .candidates import ExecutableCandidateCatalog, ExecutableCandidateDefinition
from .capabilities import AnalysisMode, ProviderCapabilityRegistry
from .folds import WalkForwardFold
from .result import ExecutableTestSummary, ValidationCohort
from .selection import ExecutableValidationScore

UNSUPPORTED_CORPORATE_ACTION_REASON = "unsupported_corporate_action"
INSUFFICIENT_FEATURE_HISTORY_REASON = "insufficient_feature_history"
NO_VALIDATION_OBSERVATIONS_REASON = "no_validation_observations"
NO_TEST_OBSERVATIONS_REASON = "no_test_observations"


class ExecutableEvaluationError(ValueError):
    """Raised when Executable Validation inputs or outcomes are invalid."""


@dataclass(frozen=True, slots=True)
class ExecutableSymbolOutcome:
    """One independent Candidate/symbol Validation backtest outcome."""

    fold_id: str
    provider: str
    candidate_id: str
    symbol: str
    validation_first_observation_date: date
    validation_last_observation_date: date
    initial_capital: float
    final_equity: float
    net_return: float
    maximum_drawdown_magnitude: float
    sharpe_ratio: float | None
    number_of_trades: int
    filled_order_count: int
    rejected_order_count: int
    canceled_order_count: int
    open_position_at_end: bool
    theoretical_buy_and_hold_return: float
    executable_buy_and_hold_return: float
    strategy_vs_executable_buy_and_hold: float

    def __post_init__(self) -> None:
        for name in ("fold_id", "provider", "candidate_id", "symbol"):
            _require_non_empty_string(name, getattr(self, name))
        for name in (
            "validation_first_observation_date",
            "validation_last_observation_date",
        ):
            _require_date(name, getattr(self, name))
        if (
            self.validation_first_observation_date
            > self.validation_last_observation_date
        ):
            raise ExecutableEvaluationError(
                "validation observation dates must be chronological"
            )
        _require_positive_finite("initial_capital", self.initial_capital)
        _require_positive_finite("final_equity", self.final_equity)
        for name in (
            "net_return",
            "theoretical_buy_and_hold_return",
            "executable_buy_and_hold_return",
            "strategy_vs_executable_buy_and_hold",
        ):
            _require_finite(name, getattr(self, name))
        _require_non_negative_finite(
            "maximum_drawdown_magnitude", self.maximum_drawdown_magnitude
        )
        if self.maximum_drawdown_magnitude > 1.0:
            raise ExecutableEvaluationError(
                "maximum_drawdown_magnitude must be at most 1"
            )
        if self.sharpe_ratio is not None:
            _require_finite("sharpe_ratio", self.sharpe_ratio)
        for name in (
            "number_of_trades",
            "filled_order_count",
            "rejected_order_count",
            "canceled_order_count",
        ):
            _require_non_negative_int(name, getattr(self, name))
        if self.filled_order_count < 2 * self.number_of_trades:
            raise ExecutableEvaluationError(
                "completed trades require at least two filled orders each"
            )
        if not isinstance(self.open_position_at_end, bool):
            raise ExecutableEvaluationError("open_position_at_end must be a boolean")
        expected_return = self.final_equity / self.initial_capital - 1.0
        if not math.isclose(
            self.net_return, expected_return, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ExecutableEvaluationError(
                "net_return must match final_equity and initial_capital"
            )
        expected_difference = self.net_return - self.executable_buy_and_hold_return
        if not math.isclose(
            self.strategy_vs_executable_buy_and_hold,
            expected_difference,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ExecutableEvaluationError(
                "strategy benchmark difference must match its component returns"
            )


@dataclass(frozen=True, slots=True)
class ExecutableSymbolExclusion:
    """One Candidate-independent symbol exclusion for a Validation fold."""

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
            INSUFFICIENT_FEATURE_HISTORY_REASON: "excluded",
            NO_VALIDATION_OBSERVATIONS_REASON: "excluded",
        }
        if self.reason not in allowed:
            raise ExecutableEvaluationError(
                "unknown Executable Validation symbol exclusion reason"
            )
        if self.status != allowed[self.reason]:
            raise ExecutableEvaluationError(
                "symbol exclusion status does not match its reason"
            )


@dataclass(frozen=True, slots=True)
class ExecutableOutcomeEvaluationResult:
    """All Executable Candidate Validation outcomes for one fold."""

    provider: str
    provider_price_basis: str
    fold_id: str
    input_symbol_count: int
    admitted_symbol_count: int
    symbol_outcomes: tuple[ExecutableSymbolOutcome, ...]
    symbol_exclusions: tuple[ExecutableSymbolExclusion, ...]
    scores: tuple[ExecutableValidationScore, ...]

    def __post_init__(self) -> None:
        for name in ("provider", "provider_price_basis", "fold_id"):
            _require_non_empty_string(name, getattr(self, name))
        _require_non_negative_int("input_symbol_count", self.input_symbol_count)
        _require_non_negative_int("admitted_symbol_count", self.admitted_symbol_count)
        if self.admitted_symbol_count > self.input_symbol_count:
            raise ExecutableEvaluationError(
                "admitted_symbol_count cannot exceed input_symbol_count"
            )
        _require_tuple_of(
            "symbol_outcomes", self.symbol_outcomes, ExecutableSymbolOutcome
        )
        _require_tuple_of(
            "symbol_exclusions", self.symbol_exclusions, ExecutableSymbolExclusion
        )
        _require_tuple_of("scores", self.scores, ExecutableValidationScore)
        if self.input_symbol_count != (
            self.admitted_symbol_count + len(self.symbol_exclusions)
        ):
            raise ExecutableEvaluationError(
                "input_symbol_count must equal admitted symbols plus exclusions"
            )

        excluded_symbols = tuple(item.symbol for item in self.symbol_exclusions)
        if excluded_symbols != tuple(sorted(excluded_symbols)):
            raise ExecutableEvaluationError("symbol exclusions must be sorted")
        if len(excluded_symbols) != len(set(excluded_symbols)):
            raise ExecutableEvaluationError("symbol exclusions must be unique")

        score_ids = tuple(score.candidate_id for score in self.scores)
        if len(score_ids) != len(set(score_ids)):
            raise ExecutableEvaluationError("scores must have unique candidate IDs")
        if not score_ids:
            raise ExecutableEvaluationError(
                "scores must contain every catalog candidate"
            )
        candidate_order = {
            candidate_id: index for index, candidate_id in enumerate(score_ids)
        }
        self._validate_children(candidate_order, set(excluded_symbols))

    def _validate_children(
        self, candidate_order: dict[str, int], excluded_symbols: set[str]
    ) -> None:
        expected_prefix = (self.fold_id, self.provider)
        outcome_keys: list[tuple[int, str]] = []
        symbols_by_candidate: dict[str, set[str]] = {
            candidate_id: set() for candidate_id in candidate_order
        }
        for outcome in self.symbol_outcomes:
            if (outcome.fold_id, outcome.provider) != expected_prefix:
                raise ExecutableEvaluationError(
                    "outcome fold/provider must match its result"
                )
            if outcome.candidate_id not in candidate_order:
                raise ExecutableEvaluationError(
                    "outcome candidate_id must have a matching score"
                )
            if outcome.symbol in excluded_symbols:
                raise ExecutableEvaluationError(
                    "excluded symbols cannot have executable outcomes"
                )
            symbols_by_candidate[outcome.candidate_id].add(outcome.symbol)
            outcome_keys.append((candidate_order[outcome.candidate_id], outcome.symbol))
        if len(outcome_keys) != len(set(outcome_keys)):
            raise ExecutableEvaluationError(
                "outcomes must be unique by candidate and symbol"
            )
        if tuple(outcome_keys) != tuple(sorted(outcome_keys)):
            raise ExecutableEvaluationError(
                "outcomes must follow catalog and symbol order"
            )
        expected_symbols: set[str] | None = None
        for candidate_id in candidate_order:
            candidate_symbols = symbols_by_candidate[candidate_id]
            if len(candidate_symbols) != self.admitted_symbol_count:
                raise ExecutableEvaluationError(
                    "each candidate requires one outcome per admitted symbol"
                )
            if expected_symbols is None:
                expected_symbols = candidate_symbols
            elif candidate_symbols != expected_symbols:
                raise ExecutableEvaluationError(
                    "all candidates must use the same admitted symbols"
                )

        for exclusion in self.symbol_exclusions:
            if (exclusion.fold_id, exclusion.provider) != expected_prefix:
                raise ExecutableEvaluationError(
                    "exclusion fold/provider must match its result"
                )
        for score in self.scores:
            if score.admitted_symbol_count != self.admitted_symbol_count:
                raise ExecutableEvaluationError(
                    "score admitted_symbol_count must match its result"
                )
            _validate_score_against_outcomes(
                score,
                tuple(
                    outcome
                    for outcome in self.symbol_outcomes
                    if outcome.candidate_id == score.candidate_id
                ),
            )


@dataclass(frozen=True, slots=True)
class ExecutableTestSymbolOutcome:
    """One selected Candidate/symbol backtest outcome on a Test interval."""

    fold_id: str
    provider: str
    candidate_id: str
    symbol: str
    test_first_observation_date: date
    test_last_observation_date: date
    initial_capital: float
    final_equity: float
    net_return: float
    maximum_drawdown_magnitude: float
    sharpe_ratio: float | None
    number_of_trades: int
    filled_order_count: int
    rejected_order_count: int
    canceled_order_count: int
    open_position_at_end: bool
    theoretical_buy_and_hold_return: float
    executable_buy_and_hold_return: float
    strategy_vs_executable_buy_and_hold: float

    def __post_init__(self) -> None:
        for name in ("fold_id", "provider", "candidate_id", "symbol"):
            _require_non_empty_string(name, getattr(self, name))
        for name in ("test_first_observation_date", "test_last_observation_date"):
            _require_date(name, getattr(self, name))
        if self.test_first_observation_date > self.test_last_observation_date:
            raise ExecutableEvaluationError(
                "Test observation dates must be chronological"
            )
        _require_positive_finite("initial_capital", self.initial_capital)
        _require_positive_finite("final_equity", self.final_equity)
        for name in (
            "net_return",
            "theoretical_buy_and_hold_return",
            "executable_buy_and_hold_return",
            "strategy_vs_executable_buy_and_hold",
        ):
            _require_finite(name, getattr(self, name))
        _require_non_negative_finite(
            "maximum_drawdown_magnitude", self.maximum_drawdown_magnitude
        )
        if self.maximum_drawdown_magnitude > 1.0:
            raise ExecutableEvaluationError(
                "maximum_drawdown_magnitude must be at most 1"
            )
        if self.sharpe_ratio is not None:
            _require_finite("sharpe_ratio", self.sharpe_ratio)
        for name in (
            "number_of_trades",
            "filled_order_count",
            "rejected_order_count",
            "canceled_order_count",
        ):
            _require_non_negative_int(name, getattr(self, name))
        if self.filled_order_count < 2 * self.number_of_trades:
            raise ExecutableEvaluationError(
                "completed trades require at least two filled orders each"
            )
        if not isinstance(self.open_position_at_end, bool):
            raise ExecutableEvaluationError("open_position_at_end must be a boolean")
        expected_return = self.final_equity / self.initial_capital - 1.0
        if not math.isclose(
            self.net_return, expected_return, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ExecutableEvaluationError(
                "net_return must match final_equity and initial_capital"
            )
        expected_difference = self.net_return - self.executable_buy_and_hold_return
        if not math.isclose(
            self.strategy_vs_executable_buy_and_hold,
            expected_difference,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ExecutableEvaluationError(
                "strategy benchmark difference must match its component returns"
            )


@dataclass(frozen=True, slots=True)
class ExecutableTestSymbolExclusion:
    """One Validation-cohort symbol excluded from Executable Test."""

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
            raise ExecutableEvaluationError(
                "unknown Executable Test symbol exclusion reason"
            )
        if self.status != allowed[self.reason]:
            raise ExecutableEvaluationError(
                "Executable Test symbol exclusion status does not match its reason"
            )


@dataclass(frozen=True, slots=True)
class ExecutableTestEvaluationResult:
    """One selected Executable candidate's outcomes on a Test interval."""

    provider: str
    provider_price_basis: str
    fold_id: str
    candidate_id: str
    requested_symbols: tuple[str, ...]
    requested_symbol_count: int
    admitted_symbol_count: int
    symbol_outcomes: tuple[ExecutableTestSymbolOutcome, ...]
    symbol_exclusions: tuple[ExecutableTestSymbolExclusion, ...]
    summary: ExecutableTestSummary

    def __post_init__(self) -> None:
        for name in ("provider", "provider_price_basis", "fold_id", "candidate_id"):
            _require_non_empty_string(name, getattr(self, name))
        if not isinstance(self.requested_symbols, tuple) or any(
            not isinstance(symbol, str) or not symbol.strip()
            for symbol in self.requested_symbols
        ):
            raise TypeError("requested_symbols must be a tuple of non-empty strings")
        if self.requested_symbols != tuple(sorted(self.requested_symbols)):
            raise ExecutableEvaluationError("requested_symbols must be sorted")
        if len(self.requested_symbols) != len(set(self.requested_symbols)):
            raise ExecutableEvaluationError("requested_symbols must be unique")
        _require_non_negative_int("requested_symbol_count", self.requested_symbol_count)
        _require_non_negative_int("admitted_symbol_count", self.admitted_symbol_count)
        if self.requested_symbol_count != len(self.requested_symbols):
            raise ExecutableEvaluationError(
                "requested_symbol_count must match requested_symbols"
            )
        _require_tuple_of(
            "symbol_outcomes", self.symbol_outcomes, ExecutableTestSymbolOutcome
        )
        _require_tuple_of(
            "symbol_exclusions",
            self.symbol_exclusions,
            ExecutableTestSymbolExclusion,
        )
        if not isinstance(self.summary, ExecutableTestSummary):
            raise TypeError("summary must be ExecutableTestSummary")
        excluded_symbols = tuple(item.symbol for item in self.symbol_exclusions)
        outcome_symbols = tuple(item.symbol for item in self.symbol_outcomes)
        if excluded_symbols != tuple(sorted(excluded_symbols)) or len(
            excluded_symbols
        ) != len(set(excluded_symbols)):
            raise ExecutableEvaluationError(
                "Executable Test exclusions must be unique and sorted"
            )
        if outcome_symbols != tuple(sorted(outcome_symbols)) or len(
            outcome_symbols
        ) != len(set(outcome_symbols)):
            raise ExecutableEvaluationError(
                "Executable Test outcomes must be unique and sorted"
            )
        if set(outcome_symbols).intersection(excluded_symbols):
            raise ExecutableEvaluationError(
                "Executable Test outcomes and exclusions cannot overlap"
            )
        if set((*outcome_symbols, *excluded_symbols)) != set(self.requested_symbols):
            raise ExecutableEvaluationError(
                "Executable Test outcomes and exclusions must cover requested_symbols"
            )
        if self.requested_symbol_count != (
            self.admitted_symbol_count + len(self.symbol_exclusions)
        ) or self.admitted_symbol_count != len(self.symbol_outcomes):
            raise ExecutableEvaluationError(
                "Executable Test symbol counts are inconsistent"
            )
        expected_prefix = (self.fold_id, self.provider, self.candidate_id)
        for item in self.symbol_outcomes:
            if (item.fold_id, item.provider, item.candidate_id) != expected_prefix:
                raise ExecutableEvaluationError(
                    "Executable Test outcome fold/provider/candidate must match result"
                )
        for item in self.symbol_exclusions:
            if (item.fold_id, item.provider) != expected_prefix[:2]:
                raise ExecutableEvaluationError(
                    "Executable Test exclusion fold/provider must match result"
                )
        if self.summary.candidate_id != self.candidate_id:
            raise ExecutableEvaluationError(
                "Executable Test summary candidate must match result"
            )
        if (
            self.summary.requested_symbol_count != self.requested_symbol_count
            or self.summary.admitted_symbol_count != self.admitted_symbol_count
        ):
            raise ExecutableEvaluationError(
                "Executable Test summary symbol counts must match result"
            )
        _validate_test_summary_against_outcomes(self.summary, self.symbol_outcomes)


@dataclass(frozen=True, slots=True)
class ExecutableOutcomeEvaluator:
    """Evaluate every Executable Candidate on Validation only."""

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
        catalog: ExecutableCandidateCatalog,
    ) -> ExecutableOutcomeEvaluationResult:
        """Return independent per-symbol Validation backtests and scores."""

        if not isinstance(bars, pd.DataFrame):
            raise TypeError("bars must be a pandas DataFrame")
        if not isinstance(fold, WalkForwardFold):
            raise TypeError("fold must be WalkForwardFold")
        if not isinstance(catalog, ExecutableCandidateCatalog):
            raise TypeError("catalog must be ExecutableCandidateCatalog")
        missing = sorted(set(CANONICAL_COLUMNS).difference(bars.columns))
        if missing:
            raise CanonicalDataError("Missing canonical columns: " + ", ".join(missing))
        _validate_date_column(bars)
        evaluation_bars = bars.loc[
            (bars["date"].dt.date >= fold.train_start)
            & (bars["date"].dt.date < fold.validation_end)
        ].copy()
        if evaluation_bars.empty:
            raise ExecutableEvaluationError(
                "no observations exist in the executable evaluation range"
            )

        raw_provider = require_single_provider(evaluation_bars)
        capability = self.capability_registry.require(
            raw_provider,
            AnalysisMode.EXECUTABLE_VALIDATION,
            require_benchmark=True,
        )
        expected_basis = provider_price_basis(raw_provider)
        if capability.provider_price_basis != expected_basis:
            raise ExecutableEvaluationError(
                "provider_price_basis does not match the provider capability "
                "declaration"
            )
        validate_canonical_bars(evaluation_bars, expected_provider=raw_provider)

        provider = capability.provider
        symbols = tuple(sorted(set(evaluation_bars["symbol"].astype(str))))
        input_symbol_count = len(symbols)
        admitted_frames: dict[str, pd.DataFrame] = {}
        symbol_exclusions: list[ExecutableSymbolExclusion] = []

        for symbol in symbols:
            symbol_bars = evaluation_bars.loc[
                evaluation_bars["symbol"].astype(str) == symbol
            ].copy()
            validation_bars = symbol_bars.loc[
                symbol_bars["date"].dt.date >= fold.validation_start
            ]
            if validation_bars.empty:
                symbol_exclusions.append(
                    _symbol_exclusion(
                        fold,
                        provider,
                        symbol,
                        "excluded",
                        NO_VALIDATION_OBSERVATIONS_REASON,
                    )
                )
                continue
            try:
                adapted = canonical_to_phase1(symbol_bars, symbol=symbol)
                validate_backtest_price_contract(adapted)
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

            base_features = _prepare_features(adapted, self.base_config)
            validation_features = base_features.loc[
                base_features["date"].dt.date >= fold.validation_start
            ]
            finite = np.isfinite(
                validation_features.loc[
                    :, ["sma", "atr", "adx", "range_score"]
                ].to_numpy(dtype=float)
            ).all(axis=1)
            if not finite.any():
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
            admitted_frames[symbol] = adapted

        admitted_symbols = tuple(sorted(admitted_frames))
        window = BacktestWindow(fold.validation_start, fold.validation_end)
        outcomes: list[ExecutableSymbolOutcome] = []
        for candidate in catalog.candidates:
            for symbol in admitted_symbols:
                config = candidate.apply(self.base_config)
                features = _prepare_features(admitted_frames[symbol], config)
                engine = config.create_engine()
                result = engine.run(symbol, features, window=window)
                metrics = calculate_backtest_metrics(
                    result,
                    annual_trading_days=config.annual_trading_days,
                    risk_free_rate=config.risk_free_rate,
                )
                outcomes.append(
                    _outcome_from_backtest(
                        fold=fold,
                        provider=provider,
                        candidate=candidate,
                        result=result,
                        metrics=metrics,
                    )
                )

        scores = _aggregate_scores(catalog, outcomes, len(admitted_symbols))
        return ExecutableOutcomeEvaluationResult(
            provider=provider,
            provider_price_basis=expected_basis,
            fold_id=fold.fold_id,
            input_symbol_count=input_symbol_count,
            admitted_symbol_count=len(admitted_symbols),
            symbol_outcomes=tuple(outcomes),
            symbol_exclusions=tuple(symbol_exclusions),
            scores=scores,
        )

    def evaluate_test(
        self,
        bars: pd.DataFrame,
        fold: WalkForwardFold,
        candidate: ExecutableCandidateDefinition,
        cohort: ValidationCohort,
    ) -> ExecutableTestEvaluationResult:
        """Evaluate one selected Executable candidate on Test exactly once."""

        if not isinstance(bars, pd.DataFrame):
            raise TypeError("bars must be a pandas DataFrame")
        if not isinstance(fold, WalkForwardFold):
            raise TypeError("fold must be WalkForwardFold")
        if not isinstance(candidate, ExecutableCandidateDefinition):
            raise TypeError("candidate must be ExecutableCandidateDefinition")
        if not isinstance(cohort, ValidationCohort):
            raise TypeError("cohort must be ValidationCohort")
        if not cohort.symbols:
            raise ExecutableEvaluationError("Executable Test cohort must not be empty")
        missing = sorted(set(CANONICAL_COLUMNS).difference(bars.columns))
        if missing:
            raise CanonicalDataError("Missing canonical columns: " + ", ".join(missing))
        _validate_date_column(bars)

        capability = self.capability_registry.require(
            cohort.provider,
            AnalysisMode.EXECUTABLE_VALIDATION,
            require_benchmark=True,
        )
        expected_basis = provider_price_basis(cohort.provider)
        if (
            capability.provider_price_basis != expected_basis
            or cohort.provider_price_basis != expected_basis
        ):
            raise ExecutableEvaluationError(
                "provider_price_basis does not match the frozen Validation cohort"
            )
        evaluation_bars = bars.loc[
            (bars["date"].dt.date >= fold.train_start)
            & (bars["date"].dt.date < fold.test_end)
            & bars["symbol"].astype(str).isin(cohort.symbols)
        ].copy()
        if not evaluation_bars.empty:
            raw_provider = require_single_provider(evaluation_bars)
            if raw_provider != cohort.provider:
                raise ExecutableEvaluationError(
                    "Test provider does not match the frozen Validation cohort"
                )
            validate_canonical_bars(evaluation_bars, expected_provider=cohort.provider)

        provider = capability.provider
        config = candidate.apply(self.base_config)
        admitted_frames: dict[str, pd.DataFrame] = {}
        symbol_exclusions: list[ExecutableTestSymbolExclusion] = []
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
                adapted = canonical_to_phase1(symbol_bars, symbol=symbol)
                validate_backtest_price_contract(adapted)
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
            features = _prepare_features(adapted, config)
            test_features = features.loc[features["date"].dt.date >= fold.test_start]
            finite = np.isfinite(
                test_features.loc[:, ["sma", "atr", "adx", "range_score"]].to_numpy(
                    dtype=float
                )
            ).all(axis=1)
            if not finite.any():
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
            admitted_frames[symbol] = adapted

        window = BacktestWindow(fold.test_start, fold.test_end)
        outcomes: list[ExecutableTestSymbolOutcome] = []
        for symbol in sorted(admitted_frames):
            features = _prepare_features(admitted_frames[symbol], config)
            engine = config.create_engine()
            result = engine.run(symbol, features, window=window)
            metrics = calculate_backtest_metrics(
                result,
                annual_trading_days=config.annual_trading_days,
                risk_free_rate=config.risk_free_rate,
            )
            outcomes.append(
                _test_outcome_from_backtest(
                    fold=fold,
                    provider=provider,
                    candidate=candidate,
                    result=result,
                    metrics=metrics,
                )
            )

        summary = _executable_test_summary(
            candidate.candidate_id,
            outcomes,
            requested_symbol_count=len(cohort.symbols),
        )
        return ExecutableTestEvaluationResult(
            provider=provider,
            provider_price_basis=expected_basis,
            fold_id=fold.fold_id,
            candidate_id=candidate.candidate_id,
            requested_symbols=cohort.symbols,
            requested_symbol_count=len(cohort.symbols),
            admitted_symbol_count=len(admitted_frames),
            symbol_outcomes=tuple(outcomes),
            symbol_exclusions=tuple(symbol_exclusions),
            summary=summary,
        )


def _prepare_features(frame: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    signal_frame = frame.copy()
    signal_frame["turnover_value"] = signal_frame["signal_close"].astype(
        float
    ) * signal_frame["signal_volume"].astype(float)
    detected = config.create_detector().transform(signal_frame)
    return config.create_scorer().transform(detected)


def _outcome_from_backtest(
    *,
    fold: WalkForwardFold,
    provider: str,
    candidate: ExecutableCandidateDefinition,
    result: BacktestResult,
    metrics: PerformanceMetrics,
) -> ExecutableSymbolOutcome:
    dates = result.prepared_data["date"].dt.date
    statuses = result.order_log["status"].astype(str)
    sharpe = (
        float(metrics.sharpe_ratio)
        if math.isfinite(float(metrics.sharpe_ratio))
        else None
    )
    return ExecutableSymbolOutcome(
        fold_id=fold.fold_id,
        provider=provider,
        candidate_id=candidate.candidate_id,
        symbol=result.symbol,
        validation_first_observation_date=dates.iloc[0],
        validation_last_observation_date=dates.iloc[-1],
        initial_capital=float(metrics.initial_capital),
        final_equity=float(metrics.final_equity),
        net_return=float(metrics.total_return),
        maximum_drawdown_magnitude=max(0.0, -float(metrics.maximum_drawdown)),
        sharpe_ratio=sharpe,
        number_of_trades=int(metrics.number_of_trades),
        filled_order_count=int(statuses.eq(OrderStatus.FILLED.value).sum()),
        rejected_order_count=int(statuses.eq(OrderStatus.REJECTED.value).sum()),
        canceled_order_count=int(statuses.eq(OrderStatus.CANCELED.value).sum()),
        open_position_at_end=result.portfolio.position is not None,
        theoretical_buy_and_hold_return=float(metrics.theoretical_buy_and_hold_return),
        executable_buy_and_hold_return=float(metrics.executable_buy_and_hold_return),
        strategy_vs_executable_buy_and_hold=float(
            metrics.strategy_vs_executable_buy_and_hold
        ),
    )


def _test_outcome_from_backtest(
    *,
    fold: WalkForwardFold,
    provider: str,
    candidate: ExecutableCandidateDefinition,
    result: BacktestResult,
    metrics: PerformanceMetrics,
) -> ExecutableTestSymbolOutcome:
    dates = result.prepared_data["date"].dt.date
    statuses = result.order_log["status"].astype(str)
    sharpe = (
        float(metrics.sharpe_ratio)
        if math.isfinite(float(metrics.sharpe_ratio))
        else None
    )
    return ExecutableTestSymbolOutcome(
        fold_id=fold.fold_id,
        provider=provider,
        candidate_id=candidate.candidate_id,
        symbol=result.symbol,
        test_first_observation_date=dates.iloc[0],
        test_last_observation_date=dates.iloc[-1],
        initial_capital=float(metrics.initial_capital),
        final_equity=float(metrics.final_equity),
        net_return=float(metrics.total_return),
        maximum_drawdown_magnitude=max(0.0, -float(metrics.maximum_drawdown)),
        sharpe_ratio=sharpe,
        number_of_trades=int(metrics.number_of_trades),
        filled_order_count=int(statuses.eq(OrderStatus.FILLED.value).sum()),
        rejected_order_count=int(statuses.eq(OrderStatus.REJECTED.value).sum()),
        canceled_order_count=int(statuses.eq(OrderStatus.CANCELED.value).sum()),
        open_position_at_end=result.portfolio.position is not None,
        theoretical_buy_and_hold_return=float(metrics.theoretical_buy_and_hold_return),
        executable_buy_and_hold_return=float(metrics.executable_buy_and_hold_return),
        strategy_vs_executable_buy_and_hold=float(
            metrics.strategy_vs_executable_buy_and_hold
        ),
    )


def _aggregate_scores(
    catalog: ExecutableCandidateCatalog,
    outcomes: list[ExecutableSymbolOutcome],
    admitted_symbol_count: int,
) -> tuple[ExecutableValidationScore, ...]:
    scores = []
    for candidate_id in catalog.candidate_ids:
        selected = [
            outcome for outcome in outcomes if outcome.candidate_id == candidate_id
        ]
        if not selected:
            scores.append(
                ExecutableValidationScore(
                    candidate_id=candidate_id,
                    admitted_symbol_count=0,
                    traded_symbol_count=0,
                    total_trade_count=0,
                    finite_sharpe_count=0,
                    median_symbol_sharpe_ratio=None,
                    median_symbol_maximum_drawdown_magnitude=None,
                    worst_symbol_maximum_drawdown_magnitude=None,
                    median_symbol_net_return=None,
                )
            )
            continue
        finite_sharpes = [
            outcome.sharpe_ratio
            for outcome in selected
            if outcome.sharpe_ratio is not None
        ]
        drawdowns = [outcome.maximum_drawdown_magnitude for outcome in selected]
        returns = [outcome.net_return for outcome in selected]
        scores.append(
            ExecutableValidationScore(
                candidate_id=candidate_id,
                admitted_symbol_count=admitted_symbol_count,
                traded_symbol_count=sum(
                    outcome.number_of_trades > 0 for outcome in selected
                ),
                total_trade_count=sum(outcome.number_of_trades for outcome in selected),
                finite_sharpe_count=len(finite_sharpes),
                median_symbol_sharpe_ratio=(
                    float(np.median(finite_sharpes)) if finite_sharpes else None
                ),
                median_symbol_maximum_drawdown_magnitude=float(np.median(drawdowns)),
                worst_symbol_maximum_drawdown_magnitude=float(np.max(drawdowns)),
                median_symbol_net_return=float(np.median(returns)),
            )
        )
    return tuple(scores)


def _executable_test_summary(
    candidate_id: str,
    outcomes: list[ExecutableTestSymbolOutcome],
    *,
    requested_symbol_count: int,
) -> ExecutableTestSummary:
    if not outcomes:
        return ExecutableTestSummary(
            candidate_id=candidate_id,
            requested_symbol_count=requested_symbol_count,
            admitted_symbol_count=0,
            traded_symbol_count=0,
            total_trade_count=0,
            finite_sharpe_count=0,
            median_symbol_sharpe_ratio=None,
            median_symbol_maximum_drawdown_magnitude=None,
            worst_symbol_maximum_drawdown_magnitude=None,
            median_symbol_net_return=None,
        )
    finite_sharpes = [
        outcome.sharpe_ratio for outcome in outcomes if outcome.sharpe_ratio is not None
    ]
    drawdowns = [outcome.maximum_drawdown_magnitude for outcome in outcomes]
    returns = [outcome.net_return for outcome in outcomes]
    return ExecutableTestSummary(
        candidate_id=candidate_id,
        requested_symbol_count=requested_symbol_count,
        admitted_symbol_count=len(outcomes),
        traded_symbol_count=sum(outcome.number_of_trades > 0 for outcome in outcomes),
        total_trade_count=sum(outcome.number_of_trades for outcome in outcomes),
        finite_sharpe_count=len(finite_sharpes),
        median_symbol_sharpe_ratio=(
            float(np.median(finite_sharpes)) if finite_sharpes else None
        ),
        median_symbol_maximum_drawdown_magnitude=float(np.median(drawdowns)),
        worst_symbol_maximum_drawdown_magnitude=float(np.max(drawdowns)),
        median_symbol_net_return=float(np.median(returns)),
    )


def _validate_score_against_outcomes(
    score: ExecutableValidationScore,
    outcomes: tuple[ExecutableSymbolOutcome, ...],
) -> None:
    if score.traded_symbol_count != sum(
        outcome.number_of_trades > 0 for outcome in outcomes
    ):
        raise ExecutableEvaluationError(
            "score traded_symbol_count must match symbol outcomes"
        )
    if score.total_trade_count != sum(outcome.number_of_trades for outcome in outcomes):
        raise ExecutableEvaluationError(
            "score total_trade_count must match symbol outcomes"
        )
    if score.finite_sharpe_count != sum(
        outcome.sharpe_ratio is not None for outcome in outcomes
    ):
        raise ExecutableEvaluationError(
            "score finite_sharpe_count must match symbol outcomes"
        )


def _validate_test_summary_against_outcomes(
    summary: ExecutableTestSummary,
    outcomes: tuple[ExecutableTestSymbolOutcome, ...],
) -> None:
    if summary.traded_symbol_count != sum(
        outcome.number_of_trades > 0 for outcome in outcomes
    ):
        raise ExecutableEvaluationError(
            "Test summary traded_symbol_count must match outcomes"
        )
    if summary.total_trade_count != sum(
        outcome.number_of_trades for outcome in outcomes
    ):
        raise ExecutableEvaluationError(
            "Test summary total_trade_count must match outcomes"
        )
    if summary.finite_sharpe_count != sum(
        outcome.sharpe_ratio is not None for outcome in outcomes
    ):
        raise ExecutableEvaluationError(
            "Test summary finite_sharpe_count must match outcomes"
        )


def _symbol_exclusion(
    fold: WalkForwardFold,
    provider: str,
    symbol: str,
    status: str,
    reason: str,
) -> ExecutableSymbolExclusion:
    return ExecutableSymbolExclusion(
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
) -> ExecutableTestSymbolExclusion:
    return ExecutableTestSymbolExclusion(
        fold_id=fold.fold_id,
        provider=provider,
        symbol=symbol,
        status=status,
        reason=reason,
    )


def _validate_date_column(frame: pd.DataFrame) -> None:
    if not is_datetime64_any_dtype(frame["date"].dtype):
        raise CanonicalDataError("canonical date must have a pandas datetime dtype")
    if frame["date"].isna().any():
        raise CanonicalDataError("canonical date contains invalid values")


def _require_non_empty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ExecutableEvaluationError(f"{name} must be a non-empty string")


def _require_date(name: str, value: object) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ExecutableEvaluationError(f"{name} must be a date")


def _require_finite(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise ExecutableEvaluationError(f"{name} must be a finite number")
    if not math.isfinite(float(value)):
        raise ExecutableEvaluationError(f"{name} must be a finite number")


def _require_positive_finite(name: str, value: object) -> None:
    _require_finite(name, value)
    if float(value) <= 0.0:
        raise ExecutableEvaluationError(f"{name} must be greater than zero")


def _require_non_negative_finite(name: str, value: object) -> None:
    _require_finite(name, value)
    if float(value) < 0.0:
        raise ExecutableEvaluationError(f"{name} must be non-negative")


def _require_non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExecutableEvaluationError(f"{name} must be a non-negative integer")


def _require_tuple_of(name: str, value: object, item_type: type) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(item, item_type) for item in value
    ):
        raise TypeError(f"{name} must be a tuple of {item_type.__name__}")
