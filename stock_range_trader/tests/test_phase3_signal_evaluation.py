"""Unit tests for Phase 3 causal Signal outcome evaluation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from config import load_phase3_config, load_strategy_config
from data import CANONICAL_COLUMNS, CanonicalDataError, provider_price_basis
from screening import RANGE_SCORE_DIVIDEND_POLICY, RANGE_SCORE_FORWARD_RETURN_MODE
from walkforward import (
    INSUFFICIENT_FEATURE_HISTORY_REASON,
    OVERLAPPING_FORWARD_WINDOW_REASON,
    SIGNAL_OUTCOME_DIVIDEND_POLICY,
    SIGNAL_OUTCOME_FORWARD_RETURN_MODE,
    UNSUPPORTED_CORPORATE_ACTION_REASON,
    ProviderCapability,
    ProviderCapabilityError,
    ProviderCapabilityRegistry,
    PurgePolicy,
    SignalCandidateCatalog,
    SignalCandidateDefinition,
    SignalCandidateSelector,
    SignalEvaluationError,
    SignalOutcomeEvaluator,
    SignalOutcomeObservation,
    WalkForwardFold,
)

PROJECT_ROOT = Path(__file__).parents[1]


def _base_config():
    return replace(
        load_strategy_config(PROJECT_ROOT / "config" / "strategy.yaml"),
        sma_period=2,
        atr_period=2,
        adx_period=2,
        range_window=2,
        slope_lookback=2,
        crossing_target=1,
        stability_window=2,
        liquidity_window=2,
        normalized_slope_limit=1.0,
        adx_score_limit=100.0,
        stability_cv_limit=10.0,
        median_trading_value_target=1.0,
    )


def _catalog(*candidate_ids: str) -> SignalCandidateCatalog:
    if not candidate_ids:
        candidate_ids = ("baseline",)
    return SignalCandidateCatalog(
        candidates=tuple(
            SignalCandidateDefinition(
                candidate_id=candidate_id,
                buy_atr_multiplier=float(index),
                range_score_threshold=10.0 + 10.0 * index,
                adx_entry_max=50.0 - 5.0 * index,
            )
            for index, candidate_id in enumerate(candidate_ids)
        )
    )


def _dates(periods: int = 30) -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-02", periods=periods)


def _bars(
    symbol: str = "7203.T",
    *,
    provider: str = "yfinance",
    dates: pd.DatetimeIndex | None = None,
    closes: np.ndarray | None = None,
) -> pd.DataFrame:
    selected_dates = _dates() if dates is None else dates
    size = len(selected_dates)
    adjusted_close = (
        np.full(size, 100.0, dtype=float)
        if closes is None
        else np.asarray(closes, dtype=float)
    )
    adjusted_open = adjusted_close + 0.5
    adjusted_high = np.maximum(adjusted_open, adjusted_close) + 2.0
    adjusted_low = np.minimum(adjusted_open, adjusted_close) - 2.0
    raw_close = adjusted_close * 10.0
    raw_open = adjusted_open * 10.0
    raw_high = adjusted_high * 10.0
    raw_low = adjusted_low * 10.0
    volume = np.full(size, 10_000.0)
    return pd.DataFrame(
        {
            "date": selected_dates,
            "symbol": symbol,
            "provider": provider,
            "raw_open": raw_open,
            "raw_high": raw_high,
            "raw_low": raw_low,
            "raw_close": raw_close,
            "raw_volume": volume,
            "turnover_value": raw_close * volume,
            "adjusted_open": adjusted_open,
            "adjusted_high": adjusted_high,
            "adjusted_low": adjusted_low,
            "adjusted_close": adjusted_close,
            "adjusted_volume": volume,
            "adjustment_factor": 1.0,
            "dividend": 0.0,
            "stock_split": 0.0,
            "fetched_at": datetime(2024, 3, 1, tzinfo=UTC),
        },
        columns=CANONICAL_COLUMNS,
    )


def _fold(dates: pd.DatetimeIndex | None = None, *, embargo: int = 2):
    selected = _dates() if dates is None else dates
    return WalkForwardFold(
        fold_id="fold_0001",
        train_start=selected[0].date(),
        train_end=selected[5].date(),
        validation_start=selected[5].date(),
        validation_end=selected[18].date(),
        test_start=selected[18].date(),
        test_end=(selected[-1] + pd.Timedelta(days=4)).date(),
        embargo_sessions=embargo,
    )


def _evaluator(registry: ProviderCapabilityRegistry | None = None):
    return SignalOutcomeEvaluator(
        _base_config(), registry or ProviderCapabilityRegistry()
    )


def _install_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    signal_dates: set[date],
    *,
    sma: float = 105.0,
    atr: float = 5.0,
    range_score: float = 80.0,
    adx: float = 10.0,
    received: list[pd.DataFrame] | None = None,
) -> None:
    def prepare(self, frame, candidate):
        if received is not None:
            received.append(frame.copy())
        result = frame.copy()
        result["sma"] = sma
        result["atr"] = atr
        result["adx"] = adx
        result["range_score"] = range_score
        result["buy_threshold"] = sma - candidate.buy_atr_multiplier * atr
        result["entry_condition"] = result["date"].dt.date.isin(signal_dates)
        return result

    monkeypatch.setattr(SignalOutcomeEvaluator, "_prepare_candidate", prepare)


def _evaluate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bars: pd.DataFrame | None = None,
    signals: set[date] | None = None,
    catalog: SignalCandidateCatalog | None = None,
    forward_sessions: int = 2,
):
    selected_bars = _bars() if bars is None else bars
    selected_signals = {_dates()[6].date()} if signals is None else signals
    _install_pipeline(monkeypatch, selected_signals)
    return _evaluator().evaluate_validation(
        selected_bars,
        _fold(embargo=forward_sessions),
        catalog or _catalog(),
        PurgePolicy(forward_sessions),
    )


def test_known_forward_outcome_uses_adjusted_close_and_fixed_sma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    closes = np.full(len(dates), 100.0)
    closes[7:9] = (90.0, 110.0)
    _install_pipeline(monkeypatch, {dates[6].date()}, sma=105.0)

    result = _evaluator().evaluate_validation(
        _bars(closes=closes), _fold(), _catalog(), PurgePolicy(2)
    )

    observation = result.observations[0]
    assert observation.signal_close == 100.0
    assert observation.label_start_date == dates[7].date()
    assert observation.label_end_date == dates[8].date()
    assert observation.forward_return == pytest.approx(0.10)
    assert observation.maximum_adverse_excursion == pytest.approx(-0.10)
    assert observation.maximum_adverse_excursion_magnitude == pytest.approx(0.10)
    assert observation.maximum_favorable_excursion == pytest.approx(0.10)
    assert observation.signal_date_sma == 105.0
    assert observation.mean_reversion_target_hit is True


def test_target_hit_uses_future_close_not_high_or_signal_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    closes = np.full(len(dates), 100.0)
    bars = _bars(closes=closes)
    bars.loc[7:8, "adjusted_high"] = 200.0
    _install_pipeline(monkeypatch, {dates[6].date()}, sma=100.0)

    result = _evaluator().evaluate_validation(bars, _fold(), _catalog(), PurgePolicy(2))

    assert result.observations[0].mean_reversion_target_hit is True
    bars.loc[7:8, "adjusted_close"] = 99.0
    bars.loc[7:8, "adjusted_open"] = 99.0
    _install_pipeline(monkeypatch, {dates[6].date()}, sma=100.0)
    result = _evaluator().evaluate_validation(bars, _fold(), _catalog(), PurgePolicy(2))
    assert result.observations[0].mean_reversion_target_hit is False


def test_purge_precedes_earliest_first_non_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    signals = {
        dates[6].date(),
        dates[7].date(),
        dates[8].date(),
        dates[9].date(),
        dates[16].date(),
    }
    result = _evaluate(monkeypatch, signals=signals)

    assert tuple(item.feature_date for item in result.observations) == (
        dates[6].date(),
        dates[9].date(),
    )
    reasons = {item.feature_date: item.reason for item in result.observation_exclusions}
    assert reasons[dates[7].date()] == OVERLAPPING_FORWARD_WINDOW_REASON
    assert reasons[dates[8].date()] == OVERLAPPING_FORWARD_WINDOW_REASON
    assert reasons[dates[16].date()] == (PurgePolicy.VALIDATION_OVERLAP_REASON)


def test_non_overlap_state_resets_per_candidate_and_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    bars = pd.concat([_bars("1301.T"), _bars("7203.T")], ignore_index=True)
    result = _evaluate(
        monkeypatch,
        bars=bars,
        signals={dates[6].date()},
        catalog=_catalog("first", "second"),
    )

    assert len(result.observations) == 4
    assert {(item.candidate_id, item.symbol) for item in result.observations} == {
        ("first", "1301.T"),
        ("first", "7203.T"),
        ("second", "1301.T"),
        ("second", "7203.T"),
    }


def test_scores_pool_retained_observations_and_preserve_catalog_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    closes = np.full(len(dates), 100.0)
    closes[7:9] = (90.0, 110.0)
    result = _evaluate(
        monkeypatch,
        bars=_bars(closes=closes),
        signals={dates[6].date(), dates[7].date()},
        catalog=_catalog("zeta", "alpha"),
    )

    assert tuple(score.candidate_id for score in result.scores) == ("zeta", "alpha")
    assert all(score.observation_count == 1 for score in result.scores)
    assert all(score.mean_reversion_target_hit_rate == 1.0 for score in result.scores)
    assert all(
        score.median_forward_return == pytest.approx(0.10) for score in result.scores
    )
    assert all(
        score.median_mae_magnitude == pytest.approx(0.10) for score in result.scores
    )


def test_zero_signal_candidate_has_explicit_empty_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _evaluate(monkeypatch, signals=set(), catalog=_catalog("one", "two"))

    assert result.observations == ()
    assert result.observation_exclusions == ()
    assert tuple(
        (
            score.candidate_id,
            score.observation_count,
            score.mean_reversion_target_hit_rate,
            score.median_forward_return,
            score.median_mae_magnitude,
        )
        for score in result.scores
    ) == (("one", 0, None, None, None), ("two", 0, None, None, None))


def test_generated_scores_are_direct_selector_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog("one", "two")
    result = _evaluate(monkeypatch, catalog=catalog)
    phase3 = load_phase3_config(PROJECT_ROOT / "config" / "phase3.yaml")

    selection = SignalCandidateSelector(phase3.signal_selection).select(
        catalog, result.scores
    )

    assert tuple(item.candidate_id for item in selection.assessments) == (
        "one",
        "two",
    )


@pytest.mark.parametrize(
    ("provider", "action_column", "action_value"),
    (
        ("yfinance", "stock_split", 2.0),
        ("jquants", "adjustment_factor", 0.5),
    ),
)
def test_corporate_action_excludes_only_affected_symbol_before_candidates(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    action_column: str,
    action_value: float,
) -> None:
    dates = _dates()
    affected = _bars("1301.T", provider=provider)
    affected.loc[6, action_column] = action_value
    normal = _bars("7203.T", provider=provider)
    bars = pd.concat([affected, normal], ignore_index=True)
    result = _evaluate(
        monkeypatch,
        bars=bars,
        signals={dates[6].date()},
        catalog=_catalog("one", "two"),
    )

    assert result.input_symbol_count == 2
    assert result.admitted_symbol_count == 1
    assert result.symbol_exclusions[0].symbol == "1301.T"
    assert result.symbol_exclusions[0].status == "unsupported"
    assert result.symbol_exclusions[0].reason == UNSUPPORTED_CORPORATE_ACTION_REASON
    assert {item.symbol for item in result.observations} == {"7203.T"}
    assert {item.candidate_id for item in result.observations} == {"one", "two"}


def test_insufficient_history_is_recorded_once_per_symbol() -> None:
    dates = _dates(4)
    fold = WalkForwardFold(
        "short",
        dates[0].date(),
        dates[1].date(),
        dates[1].date(),
        (dates[-1] + pd.Timedelta(days=1)).date(),
        (dates[-1] + pd.Timedelta(days=1)).date(),
        (dates[-1] + pd.Timedelta(days=10)).date(),
        1,
    )

    result = _evaluator().evaluate_validation(
        _bars(dates=dates), fold, _catalog("one", "two"), PurgePolicy(1)
    )

    assert result.admitted_symbol_count == 0
    assert len(result.symbol_exclusions) == 1
    assert result.symbol_exclusions[0].reason == INSUFFICIENT_FEATURE_HISTORY_REASON
    assert all(score.observation_count == 0 for score in result.scores)


def test_unexpected_pipeline_error_is_not_converted_to_symbol_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("unexpected detector failure")

    monkeypatch.setattr(SignalOutcomeEvaluator, "_prepare_candidate", fail)

    with pytest.raises(RuntimeError, match="unexpected detector failure"):
        _evaluator().evaluate_validation(_bars(), _fold(), _catalog(), PurgePolicy(2))


def test_capability_gate_rejects_mixed_unknown_and_unsupported_before_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("pipeline must not run")

    monkeypatch.setattr(SignalOutcomeEvaluator, "_prepare_candidate", forbidden)
    mixed = pd.concat([_bars("A", provider="yfinance"), _bars("B", provider="jquants")])
    with pytest.raises(CanonicalDataError, match="exactly one provider"):
        _evaluator().evaluate_validation(mixed, _fold(), _catalog(), PurgePolicy(2))

    unknown = _bars(provider="unknown")
    with pytest.raises(ProviderCapabilityError, match="unknown provider"):
        _evaluator().evaluate_validation(unknown, _fold(), _catalog(), PurgePolicy(2))

    unsupported = ProviderCapability(
        provider="blocked",
        signal_validation_supported=False,
        executable_validation_supported=False,
        benchmark_supported=False,
        provider_price_basis=provider_price_basis("blocked"),
        maximum_expected_history="none",
        availability_lag="unknown",
        notes=(),
    )
    with pytest.raises(ProviderCapabilityError, match="does not support"):
        _evaluator(ProviderCapabilityRegistry((unsupported,))).evaluate_validation(
            _bars(provider="blocked"), _fold(), _catalog(), PurgePolicy(2)
        )
    assert calls == []


def test_provider_price_basis_mismatch_fails_before_pipeline() -> None:
    wrong = ProviderCapability(
        provider="yfinance",
        signal_validation_supported=True,
        executable_validation_supported=False,
        benchmark_supported=False,
        provider_price_basis="wrong-basis",
        maximum_expected_history="history",
        availability_lag="lag",
        notes=(),
    )
    with pytest.raises(SignalEvaluationError, match="provider_price_basis"):
        _evaluator(ProviderCapabilityRegistry((wrong,))).evaluate_validation(
            _bars(), _fold(), _catalog(), PurgePolicy(2)
        )


@pytest.mark.parametrize(
    ("argument", "value", "message"),
    (
        ("bars", [], "bars must"),
        ("fold", object(), "fold must"),
        ("catalog", object(), "catalog must"),
        ("purge_policy", object(), "purge_policy must"),
    ),
)
def test_evaluator_rejects_wrong_input_types(
    argument: str, value: object, message: str
) -> None:
    arguments = {
        "bars": _bars(),
        "fold": _fold(),
        "catalog": _catalog(),
        "purge_policy": PurgePolicy(2),
    }
    arguments[argument] = value
    with pytest.raises(TypeError, match=message):
        _evaluator().evaluate_validation(**arguments)


def test_invalid_canonical_schema_duplicate_and_nonfinite_values_are_errors() -> None:
    with pytest.raises(CanonicalDataError, match="Missing canonical columns"):
        _evaluator().evaluate_validation(
            _bars().drop(columns="adjusted_close"),
            _fold(),
            _catalog(),
            PurgePolicy(2),
        )
    duplicated = pd.concat([_bars(), _bars().iloc[[0]]], ignore_index=True)
    with pytest.raises(CanonicalDataError, match="duplicate"):
        _evaluator().evaluate_validation(
            duplicated, _fold(), _catalog(), PurgePolicy(2)
        )
    invalid = _bars()
    invalid.loc[0, "adjusted_close"] = np.nan
    with pytest.raises(CanonicalDataError, match="finite"):
        _evaluator().evaluate_validation(invalid, _fold(), _catalog(), PurgePolicy(2))


def test_result_policy_and_immutable_value_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _evaluate(monkeypatch)

    assert result.provider_price_basis == provider_price_basis("yfinance")
    assert SIGNAL_OUTCOME_FORWARD_RETURN_MODE == RANGE_SCORE_FORWARD_RETURN_MODE
    assert SIGNAL_OUTCOME_DIVIDEND_POLICY == RANGE_SCORE_DIVIDEND_POLICY
    with pytest.raises(FrozenInstanceError):
        result.provider = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.observations[0].forward_return = 0.0  # type: ignore[misc]


def test_observation_constructor_rejects_nonfinite_and_inconsistent_mae() -> None:
    values = {
        "fold_id": "fold",
        "provider": "yfinance",
        "candidate_id": "candidate",
        "symbol": "7203.T",
        "feature_date": date(2024, 1, 1),
        "label_start_date": date(2024, 1, 2),
        "label_end_date": date(2024, 1, 3),
        "signal_close": 100.0,
        "signal_date_sma": 105.0,
        "signal_date_atr": 5.0,
        "buy_threshold": 100.0,
        "range_score": 80.0,
        "adx": 10.0,
        "forward_return": 0.1,
        "mean_reversion_target_hit": True,
        "maximum_adverse_excursion": -0.1,
        "maximum_adverse_excursion_magnitude": 0.1,
        "maximum_favorable_excursion": 0.2,
    }
    with pytest.raises(SignalEvaluationError, match="finite"):
        SignalOutcomeObservation(**{**values, "forward_return": float("nan")})
    with pytest.raises(SignalEvaluationError, match="must equal"):
        SignalOutcomeObservation(
            **{**values, "maximum_adverse_excursion_magnitude": 0.2}
        )


def test_candidate_specific_parameters_reach_strategy_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from strategy import MeanReversionStrategy

    seen: list[tuple[float, float, float]] = []
    original = MeanReversionStrategy.prepare

    def recording_prepare(self, frame):
        seen.append(
            (
                self.buy_atr_multiplier,
                self.range_score_threshold,
                self.adx_entry_max,
            )
        )
        return original(self, frame)

    monkeypatch.setattr(MeanReversionStrategy, "prepare", recording_prepare)
    catalog = _catalog("one", "two")
    closes = 100.0 + 5.0 * np.sin(np.arange(len(_dates()), dtype=float))
    _evaluator().evaluate_validation(
        _bars(closes=closes), _fold(), catalog, PurgePolicy(2)
    )

    assert seen == [
        (
            catalog.candidates[0].buy_atr_multiplier,
            catalog.candidates[0].range_score_threshold,
            catalog.candidates[0].adx_entry_max,
        ),
        (
            catalog.candidates[1].buy_atr_multiplier,
            catalog.candidates[1].range_score_threshold,
            catalog.candidates[1].adx_entry_max,
        ),
    ]


def test_fold_uses_actual_symbol_sessions_not_calendar_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    sparse_dates = dates.delete(7)
    sparse = _bars("1301.T", dates=sparse_dates)
    full = _bars("7203.T", dates=dates)
    signal_date = dates[6].date()
    result = _evaluate(
        monkeypatch,
        bars=pd.concat([sparse, full], ignore_index=True),
        signals={signal_date},
    )
    by_symbol = {item.symbol: item for item in result.observations}

    assert by_symbol["7203.T"].label_start_date == dates[7].date()
    assert by_symbol["7203.T"].label_end_date == dates[8].date()
    assert by_symbol["1301.T"].label_start_date == dates[8].date()
    assert by_symbol["1301.T"].label_end_date == dates[9].date()


def test_symbol_block_order_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    dates = _dates()
    first = pd.concat([_bars("7203.T"), _bars("1301.T")], ignore_index=True)
    second = pd.concat([_bars("1301.T"), _bars("7203.T")], ignore_index=True)
    _install_pipeline(monkeypatch, {dates[6].date()})
    evaluator = _evaluator()
    catalog = _catalog("two", "one")

    left = evaluator.evaluate_validation(first, _fold(), catalog, PurgePolicy(2))
    right = evaluator.evaluate_validation(second, _fold(), catalog, PurgePolicy(2))

    assert left == right
    assert tuple((item.candidate_id, item.symbol) for item in left.observations) == (
        ("two", "1301.T"),
        ("two", "7203.T"),
        ("one", "1301.T"),
        ("one", "7203.T"),
    )


def test_rows_at_or_after_test_end_are_completely_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = _dates()
    original = _bars()
    future_dates = pd.bdate_range(_fold().test_end + timedelta(days=2), periods=3)
    future = _bars("9999.T", dates=future_dates, closes=np.full(3, 9_999.0))
    _install_pipeline(monkeypatch, {dates[6].date()})
    evaluator = _evaluator()

    baseline = evaluator.evaluate_validation(
        original, _fold(), _catalog(), PurgePolicy(2)
    )
    extended = evaluator.evaluate_validation(
        pd.concat([original, future], ignore_index=True),
        _fold(),
        _catalog(),
        PurgePolicy(2),
    )

    assert extended == baseline


def test_signal_pipeline_receives_no_turnover_execution_or_raw_price_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[pd.DataFrame] = []
    _install_pipeline(monkeypatch, {_dates()[6].date()}, received=received)

    _evaluator().evaluate_validation(_bars(), _fold(), _catalog(), PurgePolicy(2))

    assert len(received) == 1
    assert tuple(received[0].columns) == (
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
    )
    assert received[0]["close"].eq(100.0).all()
    assert received[0]["open"].lt(1_000.0).all()
