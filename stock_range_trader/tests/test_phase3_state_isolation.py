"""Adversarial state-isolation tests across candidates, symbols, folds, and runs."""

from __future__ import annotations

from dataclasses import asdict

import pandas as pd
import pytest
from phase3_adversarial_helpers import (
    adversarial_dates,
    adversarial_fold,
    canonical_bars,
    executable_catalog,
    fold_schedule,
    install_signal_pipeline,
    later_fold,
    signal_catalog,
    strategy_config,
)

import walkforward.executable_evaluation as executable_module
from config import SignalSelectionPolicy, StrategyConfig
from walkforward import (
    ExecutableOutcomeEvaluator,
    ProviderCapabilityRegistry,
    PurgePolicy,
    SignalCandidateSelector,
    SignalOutcomeEvaluator,
    SignalWalkForwardRunner,
)


def _constant_features(frame, config):
    result = frame.copy()
    result["sma"] = 100.0
    result["atr"] = 10.0
    result["adx"] = 10.0
    result["range_score"] = 90.0
    return result


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


def test_executable_uses_a_fresh_engine_for_every_candidate_and_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executable_module, "_prepare_features", _constant_features)
    original_create_engine = StrategyConfig.create_engine
    engines = []

    def recording_create_engine(self):
        engine = original_create_engine(self)
        engines.append(engine)
        return engine

    monkeypatch.setattr(StrategyConfig, "create_engine", recording_create_engine)
    bars = pd.concat(
        [
            canonical_bars("1301.T"),
            canonical_bars("7203.T"),
            canonical_bars("9984.T"),
        ],
        ignore_index=True,
    )
    evaluator = ExecutableOutcomeEvaluator(
        strategy_config(), ProviderCapabilityRegistry()
    )

    result = evaluator.evaluate_validation(
        bars, adversarial_fold(), executable_catalog()
    )

    assert result.admitted_symbol_count == 3
    assert len(engines) == 9
    assert len({id(engine) for engine in engines}) == 9
    assert all(engine.initial_capital == 10_000.0 for engine in engines)


def test_candidate_application_does_not_mutate_base_strategy_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executable_module, "_prepare_features", _constant_features)
    base_config = strategy_config()
    original_values = asdict(base_config)
    evaluator = ExecutableOutcomeEvaluator(base_config, ProviderCapabilityRegistry())

    evaluator.evaluate_validation(
        canonical_bars(), adversarial_fold(), executable_catalog()
    )

    assert evaluator.base_config is base_config
    assert asdict(evaluator.base_config) == original_values


def test_same_runner_instance_has_no_cross_run_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = adversarial_dates()
    fold = adversarial_fold()
    install_signal_pipeline(
        monkeypatch, {dates[10].date(), dates[25].date(), dates[29].date()}
    )
    runner = SignalWalkForwardRunner(
        SignalOutcomeEvaluator(strategy_config(), ProviderCapabilityRegistry()),
        _selector(),
        PurgePolicy(3),
    )
    bars = canonical_bars(provider="yfinance")

    first = runner.run(bars, fold_schedule(fold), signal_catalog())
    second = runner.run(bars, fold_schedule(fold), signal_catalog())

    assert second == first


def test_symbol_added_only_for_later_fold_cannot_change_prior_fold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = adversarial_dates()
    first_fold = adversarial_fold()
    second_fold = later_fold()
    install_signal_pipeline(
        monkeypatch,
        {dates[10].date(), dates[25].date(), dates[36].date(), dates[43].date()},
    )
    runner = SignalWalkForwardRunner(
        SignalOutcomeEvaluator(strategy_config(), ProviderCapabilityRegistry()),
        _selector(),
        PurgePolicy(3),
    )
    base = canonical_bars(provider="yfinance")
    newcomer = canonical_bars("9984.T", provider="yfinance", dates=dates[35:])
    schedule = fold_schedule(first_fold, second_fold)

    baseline = runner.run(base, schedule, signal_catalog())
    extended = runner.run(
        pd.concat([base, newcomer], ignore_index=True), schedule, signal_catalog()
    )

    assert extended.fold_results[0] == baseline.fold_results[0]
    assert baseline.fold_results[1].validation_result.input_symbol_count == 1
    assert extended.fold_results[1].validation_result.input_symbol_count == 2
