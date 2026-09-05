"""Unit tests for Phase 3 Executable Candidate Validation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import walkforward.executable_evaluation as executable_module
from backtest import BacktestEngine
from config import load_phase3_config, load_strategy_config
from data import CANONICAL_COLUMNS, CanonicalDataError, provider_price_basis
from walkforward import (
    EXECUTABLE_INSUFFICIENT_FEATURE_HISTORY_REASON,
    EXECUTABLE_UNSUPPORTED_CORPORATE_ACTION_REASON,
    NO_VALIDATION_OBSERVATIONS_REASON,
    AnalysisMode,
    ExecutableCandidateCatalog,
    ExecutableCandidateDefinition,
    ExecutableCandidateSelector,
    ExecutableEvaluationError,
    ExecutableOutcomeEvaluationResult,
    ExecutableOutcomeEvaluator,
    ExecutableSymbolOutcome,
    ProviderCapability,
    ProviderCapabilityError,
    ProviderCapabilityRegistry,
    WalkForwardFold,
)

PROJECT_ROOT = Path(__file__).parents[1]


def _dates(periods: int = 40) -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-02", periods=periods)


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
        initial_capital=1_000.0,
        max_position_pct=1.0,
        lot_size=1,
        slippage_pct=0.0,
        commission_rate=0.0,
        max_drawdown_stop=0.9,
    )


def _catalog(*candidate_ids: str) -> ExecutableCandidateCatalog:
    if not candidate_ids:
        candidate_ids = ("baseline",)
    return ExecutableCandidateCatalog(
        candidates=tuple(
            ExecutableCandidateDefinition(
                candidate_id=candidate_id,
                buy_atr_multiplier=0.5 + 0.25 * index,
                sell_atr_multiplier=0.5 + 0.25 * index,
                range_score_threshold=70.0,
                adx_entry_max=25.0,
            )
            for index, candidate_id in enumerate(candidate_ids)
        )
    )


def _fold() -> WalkForwardFold:
    dates = _dates()
    return WalkForwardFold(
        fold_id="fold_0001",
        train_start=dates[5].date(),
        train_end=dates[15].date(),
        validation_start=dates[15].date(),
        validation_end=dates[30].date(),
        test_start=dates[30].date(),
        test_end=(dates[-1] + pd.Timedelta(days=4)).date(),
        embargo_sessions=2,
    )


def _bars(
    symbol: str = "7203.T",
    *,
    provider: str = "jquants",
    dates: pd.DatetimeIndex | None = None,
    signal_close: np.ndarray | None = None,
    execution_close: np.ndarray | None = None,
    execution_open: np.ndarray | None = None,
) -> pd.DataFrame:
    selected_dates = _dates() if dates is None else dates
    size = len(selected_dates)
    adjusted_close = (
        100.0 + 5.0 * np.sin(np.arange(size, dtype=float))
        if signal_close is None
        else np.asarray(signal_close, dtype=float)
    )
    raw_close = (
        np.full(size, 50.0)
        if execution_close is None
        else np.asarray(execution_close, dtype=float)
    )
    raw_open = (
        raw_close.copy()
        if execution_open is None
        else np.asarray(execution_open, dtype=float)
    )
    adjusted_open = adjusted_close.copy()
    volume = np.full(size, 100_000.0)
    return pd.DataFrame(
        {
            "date": selected_dates,
            "symbol": symbol,
            "provider": provider,
            "raw_open": raw_open,
            "raw_high": np.maximum(raw_open, raw_close) + 1.0,
            "raw_low": np.minimum(raw_open, raw_close) - 1.0,
            "raw_close": raw_close,
            "raw_volume": volume,
            "turnover_value": raw_close * volume,
            "adjusted_open": adjusted_open,
            "adjusted_high": adjusted_close + 1.0,
            "adjusted_low": adjusted_close - 1.0,
            "adjusted_close": adjusted_close,
            "adjusted_volume": volume,
            "adjustment_factor": 1.0,
            "dividend": 0.0,
            "stock_split": 0.0,
            "fetched_at": datetime(2024, 4, 1, tzinfo=UTC),
        },
        columns=CANONICAL_COLUMNS,
    )


def _evaluator(registry: ProviderCapabilityRegistry | None = None):
    return ExecutableOutcomeEvaluator(
        _base_config(), registry or ProviderCapabilityRegistry()
    )


def _controlled_prices() -> pd.DataFrame:
    dates = _dates()
    signal = np.full(len(dates), 100.0)
    signal[15] = 84.0
    signal[17] = 116.0
    execution_open = np.full(len(dates), 60.0)
    execution_close = np.full(len(dates), 60.0)
    execution_open[15] = 40.0
    execution_close[15] = 40.0
    execution_open[16] = 50.0
    execution_close[16] = 50.0
    execution_open[17] = 55.0
    execution_close[17] = 55.0
    execution_open[18] = 60.0
    return _bars(
        signal_close=signal,
        execution_open=execution_open,
        execution_close=execution_close,
    )


def _install_constant_features(monkeypatch: pytest.MonkeyPatch) -> None:
    def prepare(frame, config):
        result = frame.copy()
        result["sma"] = 100.0
        result["atr"] = 10.0
        result["adx"] = 20.0
        result["range_score"] = 80.0
        return result

    monkeypatch.setattr(executable_module, "_prepare_features", prepare)


def test_known_outcome_uses_execution_lane_and_existing_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)

    result = _evaluator().evaluate_validation(_controlled_prices(), _fold(), _catalog())

    outcome = result.symbol_outcomes[0]
    assert outcome.initial_capital == 1_000.0
    assert outcome.final_equity == pytest.approx(1_200.0)
    assert outcome.net_return == pytest.approx(0.20)
    assert outcome.maximum_drawdown_magnitude == 0.0
    assert outcome.number_of_trades == 1
    assert outcome.filled_order_count == 2
    assert outcome.rejected_order_count == 0
    assert outcome.canceled_order_count == 0
    assert outcome.open_position_at_end is False
    assert outcome.theoretical_buy_and_hold_return == pytest.approx(0.50)
    assert outcome.executable_buy_and_hold_return == pytest.approx(0.50)
    assert outcome.strategy_vs_executable_buy_and_hold == pytest.approx(-0.30)


def test_fill_and_position_size_use_execution_open_not_signal_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    result = _evaluator().evaluate_validation(_controlled_prices(), _fold(), _catalog())
    outcome = result.symbol_outcomes[0]

    assert outcome.final_equity == 1_200.0
    assert outcome.net_return == pytest.approx(0.20)
    assert outcome.number_of_trades == 1


def test_stop_loss_uses_signal_lane_not_execution_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    original_metrics = executable_module.calculate_backtest_metrics
    backtests = []

    def recording_metrics(result, **kwargs):
        backtests.append(result)
        return original_metrics(result, **kwargs)

    monkeypatch.setattr(
        executable_module, "calculate_backtest_metrics", recording_metrics
    )
    signal = np.full(len(_dates()), 100.0)
    signal[15] = 84.0
    execution = np.full(len(_dates()), 50.0)
    execution[17] = 10.0

    _evaluator().evaluate_validation(
        _bars(signal_close=signal, execution_close=execution),
        _fold(),
        _catalog(),
    )
    signal[17] = 90.0
    _evaluator().evaluate_validation(
        _bars(signal_close=signal, execution_close=execution),
        _fold(),
        _catalog(),
    )

    execution_drop, signal_drop = backtests
    assert execution_drop.portfolio.position is not None
    assert execution_drop.trade_log.empty
    assert signal_drop.portfolio.position is None
    assert signal_drop.trade_log.iloc[0]["exit_reason"] == "stop_loss"


def test_signal_price_changes_do_not_directly_change_fill_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    original_metrics = executable_module.calculate_backtest_metrics
    backtests = []

    def recording_metrics(result, **kwargs):
        backtests.append(result)
        return original_metrics(result, **kwargs)

    monkeypatch.setattr(
        executable_module, "calculate_backtest_metrics", recording_metrics
    )
    baseline = _controlled_prices()
    changed = baseline.copy()
    changed.loc[
        15, ["adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close"]
    ] = [80.0, 81.0, 79.0, 80.0]
    changed.loc[
        17, ["adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close"]
    ] = [120.0, 121.0, 119.0, 120.0]

    _evaluator().evaluate_validation(baseline, _fold(), _catalog())
    _evaluator().evaluate_validation(changed, _fold(), _catalog())

    assert backtests[0].fills == backtests[1].fills


def test_commission_and_slippage_use_execution_notional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    original_metrics = executable_module.calculate_backtest_metrics
    backtests = []

    def recording_metrics(result, **kwargs):
        backtests.append(result)
        return original_metrics(result, **kwargs)

    monkeypatch.setattr(
        executable_module, "calculate_backtest_metrics", recording_metrics
    )
    config = replace(_base_config(), slippage_pct=0.10, commission_rate=0.02)
    evaluator = ExecutableOutcomeEvaluator(config, ProviderCapabilityRegistry())

    evaluator.evaluate_validation(_controlled_prices(), _fold(), _catalog())

    buy, sell = backtests[0].fills
    assert buy.raw_open_price == 50.0
    assert buy.execution_price == pytest.approx(55.0)
    assert buy.shares == 17
    assert buy.commission == pytest.approx(18.70)
    assert buy.slippage_cost == pytest.approx(85.0)
    assert sell.raw_open_price == 55.0
    assert sell.execution_price == pytest.approx(49.5)
    assert sell.shares == 17
    assert sell.commission == pytest.approx(16.83)
    assert sell.slippage_cost == pytest.approx(93.5)


def test_execution_price_changes_do_not_change_signal_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    original_metrics = executable_module.calculate_backtest_metrics
    backtests = []

    def recording_metrics(result, **kwargs):
        backtests.append(result)
        return original_metrics(result, **kwargs)

    monkeypatch.setattr(
        executable_module, "calculate_backtest_metrics", recording_metrics
    )
    baseline = _controlled_prices()
    changed = baseline.copy()
    for field in ("raw_open", "raw_high", "raw_low", "raw_close"):
        changed[field] *= 2.0
    changed["turnover_value"] *= 2.0

    _evaluator().evaluate_validation(baseline, _fold(), _catalog())
    _evaluator().evaluate_validation(changed, _fold(), _catalog())

    pd.testing.assert_frame_equal(backtests[0].signal_log, backtests[1].signal_log)
    assert backtests[0].fills != backtests[1].fills


def test_scores_aggregate_trade_sharpe_drawdown_and_return_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    first = _controlled_prices()
    second = _controlled_prices().copy()
    second.loc[:, "symbol"] = "1301.T"
    second.loc[:, "raw_open"] *= 2.0
    second.loc[:, "raw_high"] *= 2.0
    second.loc[:, "raw_low"] *= 2.0
    second.loc[:, "raw_close"] *= 2.0
    second.loc[:, "turnover_value"] *= 2.0

    result = _evaluator().evaluate_validation(
        pd.concat([first, second], ignore_index=True),
        _fold(),
        _catalog("one", "two"),
    )

    assert result.admitted_symbol_count == 2
    assert tuple(score.candidate_id for score in result.scores) == ("one", "two")
    for score in result.scores:
        candidate_outcomes = tuple(
            outcome
            for outcome in result.symbol_outcomes
            if outcome.candidate_id == score.candidate_id
        )
        assert score.traded_symbol_count == sum(
            outcome.number_of_trades > 0 for outcome in candidate_outcomes
        )
        assert score.total_trade_count == sum(
            outcome.number_of_trades for outcome in candidate_outcomes
        )
        assert score.finite_sharpe_count == sum(
            outcome.sharpe_ratio is not None for outcome in candidate_outcomes
        )
        assert score.median_symbol_maximum_drawdown_magnitude == pytest.approx(
            np.median(
                [outcome.maximum_drawdown_magnitude for outcome in candidate_outcomes]
            )
        )
        assert score.worst_symbol_maximum_drawdown_magnitude == pytest.approx(
            max(outcome.maximum_drawdown_magnitude for outcome in candidate_outcomes)
        )
        assert score.median_symbol_net_return == pytest.approx(
            np.median([outcome.net_return for outcome in candidate_outcomes])
        )


def test_zero_trade_symbol_stays_in_return_and_drawdown_distributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    traded = _controlled_prices()
    idle = _bars(
        "1301.T",
        signal_close=np.full(len(_dates()), 100.0),
        execution_close=np.full(len(_dates()), 50.0),
    )

    result = _evaluator().evaluate_validation(
        pd.concat([traded, idle], ignore_index=True), _fold(), _catalog()
    )

    outcomes = result.symbol_outcomes
    score = result.scores[0]
    assert tuple(outcome.initial_capital for outcome in outcomes) == (1_000.0, 1_000.0)
    assert score.admitted_symbol_count == 2
    assert score.traded_symbol_count == 1
    assert score.total_trade_count == 1
    assert score.median_symbol_net_return == pytest.approx(0.10)
    assert score.median_symbol_maximum_drawdown_magnitude == pytest.approx(
        np.median([outcome.maximum_drawdown_magnitude for outcome in outcomes])
    )
    finite_sharpes = [
        outcome.sharpe_ratio for outcome in outcomes if outcome.sharpe_ratio is not None
    ]
    assert score.finite_sharpe_count == len(finite_sharpes) == 1
    assert score.median_symbol_sharpe_ratio == finite_sharpes[0]


def test_nonfinite_sharpe_becomes_none_without_removing_return_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    bars = _bars(
        signal_close=np.full(len(_dates()), 100.0),
        execution_close=np.full(len(_dates()), 50.0),
    )

    result = _evaluator().evaluate_validation(bars, _fold(), _catalog())

    outcome = result.symbol_outcomes[0]
    score = result.scores[0]
    assert outcome.sharpe_ratio is None
    assert score.finite_sharpe_count == 0
    assert score.median_symbol_sharpe_ratio is None
    assert score.median_symbol_net_return == 0.0
    assert score.median_symbol_maximum_drawdown_magnitude == 0.0


def test_generated_scores_are_direct_selector_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    catalog = _catalog("baseline", "conservative", "moderate")
    result = _evaluator().evaluate_validation(_controlled_prices(), _fold(), catalog)
    phase3 = load_phase3_config(PROJECT_ROOT / "config" / "phase3.yaml")

    selection = ExecutableCandidateSelector(phase3.executable_selection).select(
        catalog, result.scores
    )

    assert {item.candidate_id for item in selection.assessments} == set(
        catalog.candidate_ids
    )


@pytest.mark.parametrize("provider", ("yfinance", "unknown"))
def test_unsupported_provider_fails_before_any_feature_or_engine_call(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    calls: list[str] = []

    def forbidden(*args, **kwargs):
        calls.append("features")
        raise AssertionError("feature pipeline must not run")

    monkeypatch.setattr(executable_module, "_prepare_features", forbidden)
    expected_error = ProviderCapabilityError
    with pytest.raises(expected_error):
        _evaluator().evaluate_validation(_bars(provider=provider), _fold(), _catalog())
    assert calls == []


def test_in_scope_provider_mixing_fails_before_feature_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden(*args, **kwargs):
        calls.append("features")
        raise AssertionError("feature pipeline must not run")

    monkeypatch.setattr(executable_module, "_prepare_features", forbidden)
    mixed = pd.concat(
        [_bars("7203.T"), _bars("1301.T", provider="yfinance")],
        ignore_index=True,
    )

    with pytest.raises(CanonicalDataError, match="exactly one provider"):
        _evaluator().evaluate_validation(mixed, _fold(), _catalog())
    assert calls == []


def test_capability_gate_requires_benchmark_and_only_in_scope_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingRegistry(ProviderCapabilityRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, object, bool]] = []

        def require(self, provider, mode, *, require_benchmark=False):
            self.calls.append((provider, mode, require_benchmark))
            return super().require(provider, mode, require_benchmark=require_benchmark)

    _install_constant_features(monkeypatch)
    registry = RecordingRegistry()
    _evaluator(registry).evaluate_validation(_bars(), _fold(), _catalog())

    assert registry.calls == [("jquants", AnalysisMode.EXECUTABLE_VALIDATION, True)]


def test_provider_price_basis_mismatch_is_rejected() -> None:
    capability = ProviderCapability(
        provider="jquants",
        signal_validation_supported=True,
        executable_validation_supported=True,
        benchmark_supported=True,
        provider_price_basis="wrong",
        maximum_expected_history="history",
        availability_lag="lag",
        notes=(),
    )

    with pytest.raises(ExecutableEvaluationError, match="provider_price_basis"):
        _evaluator(ProviderCapabilityRegistry((capability,))).evaluate_validation(
            _bars(), _fold(), _catalog()
        )


def test_benchmark_capability_is_mandatory() -> None:
    capability = ProviderCapability(
        provider="jquants",
        signal_validation_supported=True,
        executable_validation_supported=True,
        benchmark_supported=False,
        provider_price_basis=provider_price_basis("jquants"),
        maximum_expected_history="history",
        availability_lag="lag",
        notes=(),
    )

    with pytest.raises(ProviderCapabilityError, match="benchmark"):
        _evaluator(ProviderCapabilityRegistry((capability,))).evaluate_validation(
            _bars(), _fold(), _catalog()
        )


def test_empty_executable_evaluation_range_is_explicit() -> None:
    future = _bars(dates=pd.bdate_range(_fold().validation_end, periods=3))

    with pytest.raises(ExecutableEvaluationError, match="no observations"):
        _evaluator().evaluate_validation(future, _fold(), _catalog())


def test_adjustment_factor_excludes_only_affected_symbol_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    affected = _bars("1301.T")
    affected.loc[10, "adjustment_factor"] = 0.5
    normal = _bars("7203.T")

    result = _evaluator().evaluate_validation(
        pd.concat([affected, normal], ignore_index=True),
        _fold(),
        _catalog("one", "two"),
    )

    assert result.input_symbol_count == 2
    assert result.admitted_symbol_count == 1
    assert len(result.symbol_exclusions) == 1
    assert result.symbol_exclusions[0].symbol == "1301.T"
    assert result.symbol_exclusions[0].status == "unsupported"
    assert result.symbol_exclusions[0].reason == (
        EXECUTABLE_UNSUPPORTED_CORPORATE_ACTION_REASON
    )
    assert len(result.symbol_outcomes) == 2


def test_no_validation_observations_and_insufficient_history_are_distinct() -> None:
    no_validation = _bars("1301.T", dates=_dates()[:15])
    too_short = _bars("7203.T", dates=_dates()[14:17])
    result = _evaluator().evaluate_validation(
        pd.concat([no_validation, too_short], ignore_index=True),
        _fold(),
        _catalog("one", "two"),
    )

    assert result.admitted_symbol_count == 0
    assert tuple((item.symbol, item.reason) for item in result.symbol_exclusions) == (
        ("1301.T", NO_VALIDATION_OBSERVATIONS_REASON),
        ("7203.T", EXECUTABLE_INSUFFICIENT_FEATURE_HISTORY_REASON),
    )
    assert result.symbol_outcomes == ()
    assert all(score.admitted_symbol_count == 0 for score in result.scores)
    assert all(score.median_symbol_net_return is None for score in result.scores)


def test_unexpected_pipeline_error_is_not_converted_to_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("unexpected detector failure")

    monkeypatch.setattr(executable_module, "_prepare_features", fail)

    with pytest.raises(RuntimeError, match="unexpected detector failure"):
        _evaluator().evaluate_validation(_bars(), _fold(), _catalog())


def test_candidate_and_symbol_each_receive_a_fresh_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    created: list[BacktestEngine] = []
    original = type(_base_config()).create_engine

    def recording_create_engine(config):
        engine = original(config)
        created.append(engine)
        return engine

    monkeypatch.setattr(type(_base_config()), "create_engine", recording_create_engine)
    bars = pd.concat([_bars("7203.T"), _bars("1301.T")], ignore_index=True)

    _evaluator().evaluate_validation(bars, _fold(), _catalog("one", "two"))

    assert len(created) == 4
    assert len({id(engine) for engine in created}) == 4
    assert len({id(engine.risk_manager) for engine in created}) == 4


def test_symbol_block_order_does_not_change_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    first = pd.concat([_bars("7203.T"), _bars("1301.T")], ignore_index=True)
    second = pd.concat([_bars("1301.T"), _bars("7203.T")], ignore_index=True)

    left = _evaluator().evaluate_validation(first, _fold(), _catalog("two", "one"))
    right = _evaluator().evaluate_validation(second, _fold(), _catalog("two", "one"))

    assert left == right
    assert tuple((item.candidate_id, item.symbol) for item in left.symbol_outcomes) == (
        ("two", "1301.T"),
        ("two", "7203.T"),
        ("one", "1301.T"),
        ("one", "7203.T"),
    )


def test_candidate_order_only_changes_catalog_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    one = ExecutableCandidateDefinition("one", 0.5, 0.5, 70.0, 25.0)
    two = ExecutableCandidateDefinition("two", 0.75, 0.75, 70.0, 25.0)
    forward = ExecutableCandidateCatalog((one, two))
    backward = ExecutableCandidateCatalog((two, one))
    evaluator = _evaluator()

    left = evaluator.evaluate_validation(_controlled_prices(), _fold(), forward)
    right = evaluator.evaluate_validation(_controlled_prices(), _fold(), backward)
    left_by_id = {item.candidate_id: item for item in left.symbol_outcomes}
    right_by_id = {item.candidate_id: item for item in right.symbol_outcomes}

    assert left_by_id == right_by_id
    assert tuple(score.candidate_id for score in right.scores) == ("two", "one")


def test_result_and_outcome_are_immutable_and_validate_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    result = _evaluator().evaluate_validation(_controlled_prices(), _fold(), _catalog())

    with pytest.raises(FrozenInstanceError):
        result.provider = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.symbol_outcomes[0].net_return = 0.0  # type: ignore[misc]
    with pytest.raises(ExecutableEvaluationError, match="input_symbol_count"):
        ExecutableOutcomeEvaluationResult(
            provider=result.provider,
            provider_price_basis=result.provider_price_basis,
            fold_id=result.fold_id,
            input_symbol_count=2,
            admitted_symbol_count=result.admitted_symbol_count,
            symbol_outcomes=result.symbol_outcomes,
            symbol_exclusions=result.symbol_exclusions,
            scores=result.scores,
        )


def test_outcome_rejects_nonfinite_return_and_sharpe() -> None:
    values = {
        "fold_id": "fold",
        "provider": "jquants",
        "candidate_id": "candidate",
        "symbol": "7203.T",
        "validation_first_observation_date": date(2024, 1, 1),
        "validation_last_observation_date": date(2024, 1, 2),
        "initial_capital": 100.0,
        "final_equity": 110.0,
        "net_return": 0.1,
        "maximum_drawdown_magnitude": 0.0,
        "sharpe_ratio": 1.0,
        "number_of_trades": 0,
        "filled_order_count": 0,
        "rejected_order_count": 0,
        "canceled_order_count": 0,
        "open_position_at_end": False,
        "theoretical_buy_and_hold_return": 0.0,
        "executable_buy_and_hold_return": 0.0,
        "strategy_vs_executable_buy_and_hold": 0.1,
    }
    with pytest.raises(ExecutableEvaluationError, match="finite"):
        ExecutableSymbolOutcome(**{**values, "net_return": float("nan")})
    with pytest.raises(ExecutableEvaluationError, match="finite"):
        ExecutableSymbolOutcome(**{**values, "sharpe_ratio": float("inf")})
