"""Explicitly opted-in Phase 3 validation against live market-data providers."""

from __future__ import annotations

import os
import warnings
from collections import Counter
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from phase3_live_helpers import jquants_live_window, yfinance_live_window

from config import load_phase3_config, load_strategy_config
from data import CANONICAL_COLUMNS, provider_price_basis, validate_canonical_bars
from data.providers import JQuantsV2Provider, YFinanceProvider
from walkforward import (
    EXECUTABLE_UNSUPPORTED_CORPORATE_ACTION_REASON,
    UNSUPPORTED_CORPORATE_ACTION_REASON,
    AnalysisMode,
    ExecutableCandidateCatalog,
    ExecutableOutcomeEvaluator,
    ProviderCapabilityRegistry,
    PurgePolicy,
    SignalCandidateCatalog,
    SignalOutcomeEvaluator,
)

PROJECT_ROOT = Path(__file__).parents[1]
YFINANCE_SYMBOL = "7203.T"
JQUANTS_SYMBOL = "72030"


@pytest.mark.live_yfinance
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_YFINANCE_TESTS") != "1",
    reason="requires RUN_LIVE_YFINANCE_TESTS=1",
)
def test_live_yfinance_reaches_phase3_signal_validation() -> None:
    today_jst = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    window = yfinance_live_window(today_jst)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        provider = YFinanceProvider(batch_size=1)
        _assert_provider_version(provider, "yfinance")
        bars = provider.get_daily_bars(
            [YFINANCE_SYMBOL],
            window.requested_start,
            window.requested_end_exclusive,
        )
        assert not provider.issues
        _assert_live_canonical(
            bars,
            provider="yfinance",
            symbol=YFINANCE_SYMBOL,
            start=window.requested_start,
            end=window.requested_end_exclusive,
        )

        registry = ProviderCapabilityRegistry()
        capability = registry.require(
            "yfinance",
            AnalysisMode.SIGNAL_VALIDATION,
        )
        assert capability.provider_price_basis == provider_price_basis("yfinance")
        strategy = load_strategy_config(PROJECT_ROOT / "config" / "strategy.yaml")
        phase3 = load_phase3_config(PROJECT_ROOT / "config" / "phase3.yaml")
        catalog = SignalCandidateCatalog.from_config(phase3.signal_candidate_catalog)
        result = SignalOutcomeEvaluator(strategy, registry).evaluate_validation(
            bars,
            window.fold,
            catalog,
            PurgePolicy(forward_sessions=20),
        )

    assert result.provider == "yfinance"
    assert result.provider_price_basis == provider_price_basis("yfinance")
    assert result.fold_id == window.fold.fold_id
    assert tuple(score.candidate_id for score in result.scores) == (
        catalog.candidate_ids
    )
    assert result.input_symbol_count == 1
    assert result.admitted_symbol_count + len(result.symbol_exclusions) == 1
    if result.admitted_symbol_count == 0:
        assert len(result.symbol_exclusions) == 1
        exclusion = result.symbol_exclusions[0]
        assert exclusion.symbol == YFINANCE_SYMBOL
        assert exclusion.status == "unsupported"
        assert exclusion.reason == UNSUPPORTED_CORPORATE_ACTION_REASON
    else:
        assert result.admitted_symbol_count == 1
        assert result.symbol_exclusions == ()
    for observation in result.observations:
        assert window.fold.contains_validation_date(observation.feature_date)
        assert observation.label_end_date < window.fold.test_start
    assert all(score.observation_count >= 0 for score in result.scores)
    _print_live_result(
        provider=provider,
        window=window,
        bars=bars,
        admitted=result.admitted_symbol_count,
        excluded=len(result.symbol_exclusions),
        score_count=len(result.scores),
        caught=caught,
        evaluation_path="SignalOutcomeEvaluator.evaluate_validation",
    )


@pytest.mark.live_jquants
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_JQUANTS_TESTS") != "1"
    or not os.environ.get("JQUANTS_API_KEY"),
    reason="requires RUN_LIVE_JQUANTS_TESTS=1 and JQUANTS_API_KEY",
)
def test_live_jquants_reaches_phase3_executable_validation() -> None:
    today_jst = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    window = jquants_live_window(today_jst)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        provider = JQuantsV2Provider()
        _assert_provider_version(provider, "jquants-api-client")
        bars = provider.get_daily_bars(
            [JQUANTS_SYMBOL],
            window.requested_start,
            window.requested_end_exclusive,
        )
        assert not provider.issues
        _assert_live_canonical(
            bars,
            provider="jquants",
            symbol=JQUANTS_SYMBOL,
            start=window.requested_start,
            end=window.requested_end_exclusive,
        )

        registry = ProviderCapabilityRegistry()
        capability = registry.require(
            "jquants",
            AnalysisMode.EXECUTABLE_VALIDATION,
            require_benchmark=True,
        )
        assert capability.provider_price_basis == provider_price_basis("jquants")
        strategy = load_strategy_config(PROJECT_ROOT / "config" / "strategy.yaml")
        phase3 = load_phase3_config(PROJECT_ROOT / "config" / "phase3.yaml")
        catalog = ExecutableCandidateCatalog.from_config(
            phase3.executable_candidate_catalog
        )
        result = ExecutableOutcomeEvaluator(strategy, registry).evaluate_validation(
            bars,
            window.fold,
            catalog,
        )

    assert result.provider == "jquants"
    assert result.provider_price_basis == provider_price_basis("jquants")
    assert result.fold_id == window.fold.fold_id
    assert tuple(score.candidate_id for score in result.scores) == (
        catalog.candidate_ids
    )
    assert result.input_symbol_count == 1
    assert result.admitted_symbol_count + len(result.symbol_exclusions) == 1
    evaluation_rows = bars.loc[
        (bars["date"].dt.date >= window.fold.train_start)
        & (bars["date"].dt.date < window.fold.validation_end)
    ]
    has_unverified_adjustment = not evaluation_rows["adjustment_factor"].eq(1.0).all()
    if has_unverified_adjustment:
        assert result.admitted_symbol_count == 0
        assert len(result.symbol_exclusions) == 1
        exclusion = result.symbol_exclusions[0]
        assert exclusion.symbol == JQUANTS_SYMBOL
        assert exclusion.status == "unsupported"
        assert exclusion.reason == EXECUTABLE_UNSUPPORTED_CORPORATE_ACTION_REASON
    else:
        assert result.admitted_symbol_count == 1
        assert result.symbol_exclusions == ()
        assert len(result.symbol_outcomes) == len(catalog.candidates)
        for outcome in result.symbol_outcomes:
            assert window.fold.contains_validation_date(
                outcome.validation_first_observation_date
            )
            assert window.fold.contains_validation_date(
                outcome.validation_last_observation_date
            )
    for score in result.scores:
        assert score.admitted_symbol_count >= 0
        assert score.traded_symbol_count >= 0
        assert score.total_trade_count >= 0
        assert score.finite_sharpe_count >= 0
    _print_live_result(
        provider=provider,
        window=window,
        bars=bars,
        admitted=result.admitted_symbol_count,
        excluded=len(result.symbol_exclusions),
        score_count=len(result.scores),
        caught=caught,
        evaluation_path="ExecutableOutcomeEvaluator.evaluate_validation",
    )


def _assert_provider_version(provider: object, distribution: str) -> None:
    installed = version(distribution)
    observed = getattr(provider, "library_version", "")
    assert isinstance(observed, str) and observed
    assert observed == installed


def _assert_live_canonical(
    bars: pd.DataFrame,
    *,
    provider: str,
    symbol: str,
    start,
    end,
) -> None:
    assert not bars.empty
    assert tuple(bars.columns) == CANONICAL_COLUMNS
    assert set(bars["provider"].astype(str)) == {provider}
    assert set(bars["symbol"].astype(str)) == {symbol}
    assert (bars["date"].dt.date >= start).all()
    assert (bars["date"].dt.date < end).all()
    assert not bars.duplicated(["symbol", "date"]).any()
    expected = bars.sort_values(["symbol", "date"], kind="stable").reset_index(
        drop=True
    )
    pd.testing.assert_frame_equal(bars.reset_index(drop=True), expected)
    validate_canonical_bars(
        bars,
        expected_provider=provider,
        requested_symbols={symbol},
        start=start,
        end=end,
    )


def _print_live_result(
    *,
    provider: object,
    window: object,
    bars: pd.DataFrame,
    admitted: int,
    excluded: int,
    score_count: int,
    caught: list[object],
    evaluation_path: str,
) -> None:
    warning_counts = Counter(item.category.__name__ for item in caught)
    warnings_text = ",".join(
        f"{name}:{count}" for name, count in sorted(warning_counts.items())
    )
    print(
        "STEP11_LIVE_RESULT "
        f"provider={provider.name} "
        f"version={provider.library_version} "
        f"requested=[{window.requested_start},{window.requested_end_exclusive}) "
        f"actual=[{bars['date'].min().date()},{bars['date'].max().date()}] "
        f"rows={len(bars)} symbols={bars['symbol'].nunique()} "
        f"issues={len(provider.issues)} admitted={admitted} excluded={excluded} "
        f"candidate_scores={score_count} evaluation={evaluation_path} "
        f"warnings={warnings_text or 'none'}"
    )
