"""Adversarial tests that prevent Test-driven candidate selection."""

from __future__ import annotations

from datetime import timedelta

import pytest
from phase3_adversarial_helpers import (
    adversarial_fold,
    canonical_bars,
    fold_schedule,
    signal_catalog,
)

from config import SignalSelectionPolicy
from data import provider_price_basis
from walkforward import (
    PurgePolicy,
    SignalCandidateSelector,
    SignalOutcomeEvaluationResult,
    SignalOutcomeObservation,
    SignalTestEvaluationResult,
    SignalTestSummary,
    SignalValidationScore,
    SignalWalkForwardRunner,
)


def _selector() -> SignalCandidateSelector:
    return SignalCandidateSelector(
        SignalSelectionPolicy(
            primary_metric="mean_reversion_target_hit_rate",
            mean_reversion_target="signal_date_sma",
            minimum_observation_count=1,
            tie_breakers=(
                "median_forward_return_desc",
                "median_mae_magnitude_asc",
                "candidate_id_asc",
            ),
        )
    )


def _validation(fold, rates: tuple[float, ...]) -> SignalOutcomeEvaluationResult:
    ids = signal_catalog().candidate_ids
    observations = tuple(
        SignalOutcomeObservation(
            fold_id=fold.fold_id,
            provider="yfinance",
            candidate_id=candidate_id,
            symbol="7203.T",
            feature_date=fold.validation_start,
            label_start_date=fold.validation_start + timedelta(days=1),
            label_end_date=fold.validation_start + timedelta(days=2),
            signal_close=100.0,
            signal_date_sma=105.0,
            signal_date_atr=5.0,
            buy_threshold=95.0,
            range_score=80.0,
            adx=10.0,
            forward_return=rate / 100.0,
            mean_reversion_target_hit=rate >= 0.5,
            maximum_adverse_excursion=-0.01,
            maximum_adverse_excursion_magnitude=0.01,
            maximum_favorable_excursion=0.02,
        )
        for candidate_id, rate in zip(ids, rates, strict=True)
    )
    return SignalOutcomeEvaluationResult(
        provider="yfinance",
        provider_price_basis=provider_price_basis("yfinance"),
        fold_id=fold.fold_id,
        input_symbol_count=1,
        admitted_symbol_count=1,
        observations=observations,
        observation_exclusions=(),
        symbol_exclusions=(),
        scores=tuple(
            SignalValidationScore(candidate_id, 1, rate, rate / 100.0, 0.01)
            for candidate_id, rate in zip(ids, rates, strict=True)
        ),
    )


def _test_result(fold, candidate_id: str) -> SignalTestEvaluationResult:
    return SignalTestEvaluationResult(
        provider="yfinance",
        provider_price_basis=provider_price_basis("yfinance"),
        fold_id=fold.fold_id,
        candidate_id=candidate_id,
        requested_symbols=("7203.T",),
        requested_symbol_count=1,
        admitted_symbol_count=1,
        observations=(),
        observation_exclusions=(),
        symbol_exclusions=(),
        summary=SignalTestSummary(candidate_id, 0, None, None, None),
    )


def test_runner_trace_uses_validation_winner_not_test_winner() -> None:
    fold = adversarial_fold()
    events: list[str] = []
    hypothetical_test_quality = {"signal_a": -1.0, "signal_b": 0.0, "signal_c": 9.0}

    class Evaluator:
        def evaluate_validation(self, bars, received_fold, catalog, policy):
            events.append("evaluate_validation:all=3")
            return _validation(received_fold, (0.9, 0.8, 0.7))

        def evaluate_test(self, bars, received_fold, candidate, policy, cohort):
            events.append(f"evaluate_test:{candidate.candidate_id}")
            return _test_result(received_fold, candidate.candidate_id)

    class Selector:
        def select(self, catalog, scores):
            events.append("selector.select")
            return _selector().select(catalog, scores)

    result = SignalWalkForwardRunner(Evaluator(), Selector(), PurgePolicy(3)).run(
        canonical_bars(provider="yfinance"),
        fold_schedule(fold),
        signal_catalog(),
    )

    assert max(hypothetical_test_quality, key=hypothetical_test_quality.get) == (
        "signal_c"
    )
    assert result.fold_results[0].selection.selected_candidate_id == "signal_a"
    assert events == [
        "evaluate_validation:all=3",
        "selector.select",
        "evaluate_test:signal_a",
    ]


def test_test_failure_neither_reselects_nor_falls_back() -> None:
    fold = adversarial_fold()
    events: list[str] = []

    class Evaluator:
        def evaluate_validation(self, bars, received_fold, catalog, policy):
            events.append("evaluate_validation")
            return _validation(received_fold, (0.9, 0.8, 0.7))

        def evaluate_test(self, bars, received_fold, candidate, policy, cohort):
            events.append(f"evaluate_test:{candidate.candidate_id}")
            raise RuntimeError("synthetic Test failure containing SECRET")

    class Selector:
        def select(self, catalog, scores):
            events.append("selector.select")
            return _selector().select(catalog, scores)

    with pytest.raises(RuntimeError, match="synthetic Test failure"):
        SignalWalkForwardRunner(Evaluator(), Selector(), PurgePolicy(3)).run(
            canonical_bars(provider="yfinance"),
            fold_schedule(fold),
            signal_catalog(),
        )

    assert events == [
        "evaluate_validation",
        "selector.select",
        "evaluate_test:signal_a",
    ]
