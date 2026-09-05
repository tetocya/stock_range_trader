"""Tests for selected-candidate Executable evaluation on the Test interval."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from test_phase3_executable_evaluation import (
    _bars,
    _catalog,
    _dates,
    _evaluator,
    _fold,
    _install_constant_features,
)

import walkforward.executable_evaluation as executable_module
from backtest import BacktestEngine, BacktestWindow
from data import provider_price_basis
from walkforward import (
    EXECUTABLE_INSUFFICIENT_FEATURE_HISTORY_REASON,
    EXECUTABLE_NO_TEST_OBSERVATIONS_REASON,
    EXECUTABLE_UNSUPPORTED_CORPORATE_ACTION_REASON,
    ExecutableTestSummary,
    ProviderCapabilityError,
    ProviderCapabilityRegistry,
    ValidationCohort,
)


def _cohort(*symbols: str, provider: str = "jquants") -> ValidationCohort:
    return ValidationCohort(
        provider=provider,
        provider_price_basis=provider_price_basis(provider),
        symbols=tuple(sorted(symbols or ("7203.T",))),
    )


def _test_trade_bars(symbol: str = "7203.T") -> pd.DataFrame:
    dates = _dates()
    signal = np.full(len(dates), 100.0)
    signal[30] = 84.0
    signal[32] = 116.0
    execution_open = np.full(len(dates), 60.0)
    execution_close = np.full(len(dates), 60.0)
    execution_open[30] = 40.0
    execution_close[30] = 40.0
    execution_open[31] = 50.0
    execution_close[31] = 50.0
    execution_open[32] = 55.0
    execution_close[32] = 55.0
    execution_open[33] = 60.0
    return _bars(
        symbol,
        signal_close=signal,
        execution_open=execution_open,
        execution_close=execution_close,
    )


def test_executable_test_uses_test_window_and_existing_execution_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)

    result = _evaluator().evaluate_test(
        _test_trade_bars(), _fold(), _catalog().candidates[0], _cohort()
    )

    outcome = result.symbol_outcomes[0]
    assert outcome.test_first_observation_date == _dates()[30].date()
    assert outcome.test_last_observation_date == _dates()[39].date()
    assert outcome.initial_capital == 1_000.0
    assert outcome.final_equity == pytest.approx(1_200.0)
    assert outcome.net_return == pytest.approx(0.20)
    assert outcome.number_of_trades == 1
    assert outcome.filled_order_count == 2
    assert outcome.open_position_at_end is False
    assert outcome.theoretical_buy_and_hold_return == pytest.approx(0.50)
    assert outcome.executable_buy_and_hold_return == pytest.approx(0.50)
    assert outcome.strategy_vs_executable_buy_and_hold == pytest.approx(-0.30)


def test_executable_test_features_use_train_and_validation_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature_inputs: list[pd.DataFrame] = []
    windows: list[BacktestWindow] = []

    def features(frame, config):
        feature_inputs.append(frame.copy())
        result = frame.copy()
        result["sma"] = 100.0
        result["atr"] = 10.0
        result["adx"] = 20.0
        result["range_score"] = 80.0
        return result

    original_run = BacktestEngine.run

    def recording_run(self, symbol, frame, *, window=None):
        windows.append(window)
        return original_run(self, symbol, frame, window=window)

    monkeypatch.setattr(executable_module, "_prepare_features", features)
    monkeypatch.setattr(BacktestEngine, "run", recording_run)

    _evaluator().evaluate_test(
        _test_trade_bars(), _fold(), _catalog().candidates[0], _cohort()
    )

    assert len(feature_inputs) == 2
    assert all(
        frame["date"].dt.date.min() == _fold().train_start for frame in feature_inputs
    )
    assert all(
        frame["date"].dt.date.max() < _fold().test_end for frame in feature_inputs
    )
    assert windows == [BacktestWindow(_fold().test_start, _fold().test_end)]


def test_executable_test_capability_gate_requires_benchmark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingRegistry(ProviderCapabilityRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.calls = []

        def require(self, provider, mode, *, require_benchmark=False):
            self.calls.append((provider, mode, require_benchmark))
            return super().require(provider, mode, require_benchmark=require_benchmark)

    _install_constant_features(monkeypatch)
    registry = RecordingRegistry()
    evaluator = type(_evaluator())(_evaluator().base_config, registry)

    evaluator.evaluate_test(
        _test_trade_bars(), _fold(), _catalog().candidates[0], _cohort()
    )

    assert len(registry.calls) == 1
    assert registry.calls[0][0] == "jquants"
    assert registry.calls[0][2] is True


@pytest.mark.parametrize("provider", ("yfinance", "unknown"))
def test_executable_test_rejects_unsupported_cohort_before_features(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("feature pipeline must not run")

    monkeypatch.setattr(executable_module, "_prepare_features", forbidden)

    with pytest.raises(ProviderCapabilityError):
        _evaluator().evaluate_test(
            _bars(provider=provider),
            _fold(),
            _catalog().candidates[0],
            _cohort(provider=provider),
        )
    assert calls == []


def test_executable_test_rejects_data_provider_mismatch_before_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("feature pipeline must not run")

    monkeypatch.setattr(executable_module, "_prepare_features", forbidden)

    with pytest.raises(ValueError, match="provider"):
        _evaluator().evaluate_test(
            _bars(provider="yfinance"),
            _fold(),
            _catalog().candidates[0],
            _cohort(),
        )
    assert calls == []


def test_executable_test_corporate_action_excludes_only_affected_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    affected = _test_trade_bars("1301.T")
    affected.loc[32, "adjustment_factor"] = 0.5
    normal = _test_trade_bars("7203.T")

    result = _evaluator().evaluate_test(
        pd.concat([affected, normal], ignore_index=True),
        _fold(),
        _catalog().candidates[0],
        _cohort("1301.T", "7203.T"),
    )

    assert result.requested_symbol_count == 2
    assert result.admitted_symbol_count == 1
    assert tuple(
        (item.symbol, item.status, item.reason) for item in result.symbol_exclusions
    ) == (
        (
            "1301.T",
            "unsupported",
            EXECUTABLE_UNSUPPORTED_CORPORATE_ACTION_REASON,
        ),
    )
    assert tuple(item.symbol for item in result.symbol_outcomes) == ("7203.T",)


def test_executable_test_reports_missing_test_and_feature_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = pd.concat(
        [_bars("1301.T", dates=_dates()[:30]), _bars("7203.T")],
        ignore_index=True,
    )

    def missing_features(frame, config):
        result = frame.copy()
        for field in ("sma", "atr", "adx", "range_score"):
            result[field] = np.nan
        return result

    monkeypatch.setattr(executable_module, "_prepare_features", missing_features)
    result = _evaluator().evaluate_test(
        bars,
        _fold(),
        _catalog().candidates[0],
        _cohort("1301.T", "7203.T"),
    )

    assert tuple((item.symbol, item.reason) for item in result.symbol_exclusions) == (
        ("1301.T", EXECUTABLE_NO_TEST_OBSERVATIONS_REASON),
        ("7203.T", EXECUTABLE_INSUFFICIENT_FEATURE_HISTORY_REASON),
    )
    assert result.symbol_outcomes == ()
    assert result.summary == ExecutableTestSummary(
        "baseline", 2, 0, 0, 0, 0, None, None, None, None
    )


def test_executable_test_final_signal_cannot_fill_on_post_test_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    signal = np.full(len(_dates()), 100.0)
    signal[39] = 84.0
    base = _bars(signal_close=signal)
    future_dates = pd.bdate_range(_fold().test_end, periods=3)
    future = _bars(dates=future_dates, signal_close=np.full(3, 100.0))

    result = _evaluator().evaluate_test(
        pd.concat([base, future], ignore_index=True),
        _fold(),
        _catalog().candidates[0],
        _cohort(),
    )

    outcome = result.symbol_outcomes[0]
    assert outcome.filled_order_count == 0
    assert outcome.canceled_order_count == 1
    assert outcome.number_of_trades == 0
    assert outcome.open_position_at_end is False


def test_executable_test_does_not_force_close_open_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    signal = np.full(len(_dates()), 100.0)
    signal[30] = 84.0

    result = _evaluator().evaluate_test(
        _bars(signal_close=signal),
        _fold(),
        _catalog().candidates[0],
        _cohort(),
    )

    outcome = result.symbol_outcomes[0]
    assert outcome.filled_order_count == 1
    assert outcome.number_of_trades == 0
    assert outcome.open_position_at_end is True


def test_symbol_appearing_only_in_test_is_ignored_when_not_in_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    baseline = _test_trade_bars()
    newcomer = _bars(
        "9999.T",
        provider="unknown",
        dates=_dates()[30:],
        signal_close=np.linspace(1.0, 10_000.0, 10),
    )
    evaluator = _evaluator()
    candidate = _catalog().candidates[0]

    original = evaluator.evaluate_test(baseline, _fold(), candidate, _cohort())
    extended = evaluator.evaluate_test(
        pd.concat([baseline, newcomer], ignore_index=True),
        _fold(),
        candidate,
        _cohort(),
    )

    assert extended == original
    assert extended.requested_symbols == ("7203.T",)


def test_executable_test_ignores_future_and_new_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    baseline = _test_trade_bars()
    future_dates = pd.bdate_range(_fold().test_end + timedelta(days=1), periods=4)
    future = _bars(
        "9999.T",
        provider="unknown",
        dates=future_dates,
        signal_close=np.linspace(1.0, 10_000.0, 4),
    )
    evaluator = _evaluator()
    candidate = _catalog().candidates[0]

    original = evaluator.evaluate_test(baseline, _fold(), candidate, _cohort())
    extended = evaluator.evaluate_test(
        pd.concat([baseline, future], ignore_index=True),
        _fold(),
        candidate,
        _cohort(),
    )

    assert extended == original
    assert extended.requested_symbols == ("7203.T",)


def test_executable_test_zero_trade_symbols_remain_in_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    first = _bars("1301.T", signal_close=np.full(len(_dates()), 100.0))
    second = _bars("7203.T", signal_close=np.full(len(_dates()), 100.0))

    result = _evaluator().evaluate_test(
        pd.concat([first, second], ignore_index=True),
        _fold(),
        _catalog().candidates[0],
        _cohort("1301.T", "7203.T"),
    )

    assert result.summary.requested_symbol_count == 2
    assert result.summary.admitted_symbol_count == 2
    assert result.summary.traded_symbol_count == 0
    assert result.summary.finite_sharpe_count == 0
    assert result.summary.median_symbol_sharpe_ratio is None
    assert result.summary.median_symbol_net_return == 0.0
    assert result.summary.median_symbol_maximum_drawdown_magnitude == 0.0
    assert not hasattr(result, "scores")
    with pytest.raises(FrozenInstanceError):
        result.candidate_id = "other"  # type: ignore[misc]


def test_nonfinite_sharpe_is_excluded_only_from_test_sharpe_median(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    traded = _test_trade_bars("1301.T")
    idle = _bars("7203.T", signal_close=np.full(len(_dates()), 100.0))

    result = _evaluator().evaluate_test(
        pd.concat([traded, idle], ignore_index=True),
        _fold(),
        _catalog().candidates[0],
        _cohort("1301.T", "7203.T"),
    )

    finite = [
        outcome.sharpe_ratio
        for outcome in result.symbol_outcomes
        if outcome.sharpe_ratio is not None
    ]
    assert result.summary.finite_sharpe_count == len(finite) == 1
    assert result.summary.median_symbol_sharpe_ratio == finite[0]
    assert result.summary.median_symbol_net_return == pytest.approx(0.10)
