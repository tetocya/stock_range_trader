"""Tests for selected-candidate Signal evaluation on the Test interval."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from test_phase3_signal_evaluation import (
    _bars,
    _catalog,
    _dates,
    _evaluator,
    _fold,
    _install_pipeline,
)

from data import provider_price_basis
from walkforward import (
    INSUFFICIENT_FEATURE_HISTORY_REASON,
    NO_TEST_OBSERVATIONS_REASON,
    OVERLAPPING_FORWARD_WINDOW_REASON,
    UNSUPPORTED_CORPORATE_ACTION_REASON,
    PurgePolicy,
    SignalEvaluationError,
    SignalOutcomeEvaluator,
    SignalTestSummary,
    ValidationCohort,
)


def _cohort(*symbols: str) -> ValidationCohort:
    return ValidationCohort(
        provider="yfinance",
        provider_price_basis=provider_price_basis("yfinance"),
        symbols=tuple(sorted(symbols or ("7203.T",))),
    )


def test_signal_test_uses_train_and_validation_for_warmup_but_only_test_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    received: list[pd.DataFrame] = []
    _install_pipeline(
        monkeypatch,
        {dates[6].date(), dates[19].date()},
        received=received,
    )

    result = _evaluator().evaluate_test(
        _bars(), _fold(), _catalog().candidates[0], PurgePolicy(2), _cohort()
    )

    assert len(received) == 1
    assert tuple(received[0].columns) == (
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
    )
    assert received[0]["date"].dt.date.min() == _fold().train_start
    assert received[0]["date"].dt.date.max() < _fold().test_end
    assert tuple(item.feature_date for item in result.observations) == (
        dates[19].date(),
    )


def test_signal_test_outcome_uses_adjusted_close_and_fixed_signal_date_sma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    closes = np.full(len(dates), 100.0)
    closes[20:22] = (90.0, 110.0)
    _install_pipeline(monkeypatch, {dates[19].date()}, sma=105.0)

    result = _evaluator().evaluate_test(
        _bars(closes=closes),
        _fold(),
        _catalog().candidates[0],
        PurgePolicy(2),
        _cohort(),
    )

    outcome = result.observations[0]
    assert outcome.signal_close == 100.0
    assert outcome.signal_date_sma == 105.0
    assert outcome.label_start_date == dates[20].date()
    assert outcome.label_end_date == dates[21].date()
    assert outcome.forward_return == pytest.approx(0.10)
    assert outcome.maximum_adverse_excursion == pytest.approx(-0.10)
    assert outcome.maximum_favorable_excursion == pytest.approx(0.10)
    assert outcome.mean_reversion_target_hit is True
    assert result.summary.candidate_id == "baseline"
    assert result.summary.observation_count == 1
    assert result.summary.mean_reversion_target_hit_rate == 1.0
    assert result.summary.median_forward_return == pytest.approx(0.10)
    assert result.summary.median_mae_magnitude == pytest.approx(0.10)


def test_signal_test_right_censors_before_earliest_first_non_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    _install_pipeline(
        monkeypatch,
        {
            dates[19].date(),
            dates[20].date(),
            dates[22].date(),
            dates[28].date(),
            dates[29].date(),
        },
    )

    result = _evaluator().evaluate_test(
        _bars(), _fold(), _catalog().candidates[0], PurgePolicy(2), _cohort()
    )

    assert tuple(item.feature_date for item in result.observations) == (
        dates[19].date(),
        dates[22].date(),
    )
    reasons = {item.feature_date: item.reason for item in result.observation_exclusions}
    assert reasons[dates[20].date()] == OVERLAPPING_FORWARD_WINDOW_REASON
    assert reasons[dates[28].date()] == PurgePolicy.RIGHT_CENSORED_REASON
    assert reasons[dates[29].date()] == PurgePolicy.RIGHT_CENSORED_REASON


def test_signal_test_does_not_use_raw_or_execution_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    _install_pipeline(monkeypatch, {dates[19].date()})
    baseline = _bars()
    changed = baseline.copy()
    for field in ("raw_open", "raw_high", "raw_low", "raw_close"):
        changed[field] *= 5.0
    changed["turnover_value"] *= 5.0
    evaluator = _evaluator()
    candidate = _catalog().candidates[0]

    original = evaluator.evaluate_test(
        baseline, _fold(), candidate, PurgePolicy(2), _cohort()
    )
    modified = evaluator.evaluate_test(
        changed, _fold(), candidate, PurgePolicy(2), _cohort()
    )

    assert modified == original


def test_signal_test_ignores_rows_at_or_after_test_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    _install_pipeline(monkeypatch, {dates[19].date()})
    future_dates = pd.bdate_range(_fold().test_end + timedelta(days=1), periods=4)
    future = _bars(
        provider="unknown",
        dates=future_dates,
        closes=np.linspace(1.0, 10_000.0, 4),
    )
    evaluator = _evaluator()
    candidate = _catalog().candidates[0]
    baseline = evaluator.evaluate_test(
        _bars(), _fold(), candidate, PurgePolicy(2), _cohort()
    )
    extended = evaluator.evaluate_test(
        pd.concat([_bars(), future], ignore_index=True),
        _fold(),
        candidate,
        PurgePolicy(2),
        _cohort(),
    )

    assert extended == baseline


def test_signal_test_rejects_cohort_provider_mismatch_before_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Signal pipeline must not run")

    monkeypatch.setattr(SignalOutcomeEvaluator, "_prepare_candidate", forbidden)

    with pytest.raises(SignalEvaluationError, match="provider"):
        _evaluator().evaluate_test(
            _bars(provider="jquants"),
            _fold(),
            _catalog().candidates[0],
            PurgePolicy(2),
            _cohort(),
        )
    assert calls == []


def test_later_test_prices_do_not_change_completed_earlier_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    _install_pipeline(monkeypatch, {dates[19].date(), dates[24].date()})
    baseline = _bars()
    changed = baseline.copy()
    later = changed["date"].dt.date >= dates[23].date()
    for prefix in ("raw", "adjusted"):
        for field in ("open", "high", "low", "close"):
            changed.loc[later, f"{prefix}_{field}"] *= 2.0
    changed.loc[later, "turnover_value"] *= 2.0
    evaluator = _evaluator()
    candidate = _catalog().candidates[0]

    original = evaluator.evaluate_test(
        baseline, _fold(), candidate, PurgePolicy(2), _cohort()
    )
    modified = evaluator.evaluate_test(
        changed, _fold(), candidate, PurgePolicy(2), _cohort()
    )

    assert modified.observations[0] == original.observations[0]
    assert modified.observations[0].label_end_date == dates[21].date()


def test_signal_test_corporate_action_excludes_only_affected_cohort_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    _install_pipeline(monkeypatch, {dates[19].date()})
    affected = _bars("1301.T")
    affected.loc[20, "stock_split"] = 2.0
    normal = _bars("7203.T")

    result = _evaluator().evaluate_test(
        pd.concat([affected, normal], ignore_index=True),
        _fold(),
        _catalog().candidates[0],
        PurgePolicy(2),
        _cohort("1301.T", "7203.T"),
    )

    assert result.requested_symbol_count == 2
    assert result.admitted_symbol_count == 1
    assert tuple(
        (item.symbol, item.status, item.reason) for item in result.symbol_exclusions
    ) == (("1301.T", "unsupported", UNSUPPORTED_CORPORATE_ACTION_REASON),)
    assert {item.symbol for item in result.observations} == {"7203.T"}


def test_signal_test_reports_no_observations_and_insufficient_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    bars = pd.concat(
        [
            _bars("1301.T", dates=dates[:18]),
            _bars("7203.T", dates=dates),
        ],
        ignore_index=True,
    )

    def missing_features(self, frame, candidate):
        result = frame.copy()
        for field in ("sma", "atr", "adx", "range_score", "buy_threshold"):
            result[field] = np.nan
        result["entry_condition"] = False
        return result

    monkeypatch.setattr(SignalOutcomeEvaluator, "_prepare_candidate", missing_features)
    result = _evaluator().evaluate_test(
        bars,
        _fold(),
        _catalog().candidates[0],
        PurgePolicy(2),
        _cohort("1301.T", "7203.T"),
    )

    assert tuple((item.symbol, item.reason) for item in result.symbol_exclusions) == (
        ("1301.T", NO_TEST_OBSERVATIONS_REASON),
        ("7203.T", INSUFFICIENT_FEATURE_HISTORY_REASON),
    )
    assert result.summary == SignalTestSummary("baseline", 0, None, None, None)


def test_signal_test_zero_signals_returns_summary_and_immutable_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_pipeline(monkeypatch, set())

    result = _evaluator().evaluate_test(
        _bars(), _fold(), _catalog().candidates[0], PurgePolicy(2), _cohort()
    )

    assert result.observations == ()
    assert result.summary == SignalTestSummary("baseline", 0, None, None, None)
    assert not hasattr(result, "scores")
    with pytest.raises(FrozenInstanceError):
        result.candidate_id = "other"  # type: ignore[misc]
