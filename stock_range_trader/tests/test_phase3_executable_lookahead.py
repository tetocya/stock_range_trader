"""Look-ahead isolation tests for Phase 3 Executable Validation."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from test_phase3_executable_evaluation import (
    _bars,
    _catalog,
    _controlled_prices,
    _dates,
    _evaluator,
    _fold,
    _install_constant_features,
)

import walkforward.executable_evaluation as executable_module
from backtest import BacktestEngine


def _replace_prices(frame: pd.DataFrame, mask: pd.Series, multiplier: float) -> None:
    for prefix in ("raw", "adjusted"):
        for field in ("open", "high", "low", "close"):
            frame.loc[mask, f"{prefix}_{field}"] *= multiplier
    frame.loc[mask, "raw_volume"] *= multiplier
    frame.loc[mask, "adjusted_volume"] *= multiplier
    frame.loc[mask, "turnover_value"] *= multiplier * multiplier


def test_test_price_changes_do_not_change_executable_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    original = _controlled_prices()
    changed = original.copy()
    test_mask = changed["date"].dt.date >= _fold().validation_end
    _replace_prices(changed, test_mask, 10_000.0)

    baseline = _evaluator().evaluate_validation(original, _fold(), _catalog())
    modified = _evaluator().evaluate_validation(changed, _fold(), _catalog())

    assert modified == baseline


def test_test_rows_can_be_added_or_removed_without_changing_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    full = _controlled_prices()
    without_test = full.loc[full["date"].dt.date < _fold().validation_end].copy()
    future_dates = pd.bdate_range(_fold().validation_end, periods=20)
    extra = _bars(
        dates=future_dates,
        signal_close=np.linspace(1.0, 10_000.0, len(future_dates)),
        execution_close=np.linspace(10_000.0, 1.0, len(future_dates)),
    )

    baseline = _evaluator().evaluate_validation(full, _fold(), _catalog())
    removed = _evaluator().evaluate_validation(without_test, _fold(), _catalog())
    extended = _evaluator().evaluate_validation(
        pd.concat([without_test, extra], ignore_index=True), _fold(), _catalog()
    )

    assert removed == baseline
    assert extended == baseline


@pytest.mark.parametrize("outside_provider", ("yfinance", "unknown"))
@pytest.mark.parametrize("side", ("before_train", "after_validation"))
def test_out_of_scope_provider_is_completely_ignored(
    monkeypatch: pytest.MonkeyPatch,
    outside_provider: str,
    side: str,
) -> None:
    _install_constant_features(monkeypatch)
    original = _controlled_prices()
    if side == "before_train":
        outside_dates = pd.bdate_range(
            end=_fold().train_start - timedelta(days=1), periods=3
        )
    else:
        outside_dates = pd.bdate_range(_fold().validation_end, periods=3)
    outside = _bars(
        "9999.T",
        provider=outside_provider,
        dates=outside_dates,
        signal_close=np.full(3, 9_999.0),
    )

    baseline = _evaluator().evaluate_validation(original, _fold(), _catalog())
    extended = _evaluator().evaluate_validation(
        pd.concat([outside, original], ignore_index=True), _fold(), _catalog()
    )

    assert extended == baseline


def test_test_provider_corporate_action_and_volume_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    original = _controlled_prices()
    changed = original.copy()
    test_mask = changed["date"].dt.date >= _fold().validation_end
    changed.loc[test_mask, "provider"] = "unknown"
    changed.loc[test_mask, "adjustment_factor"] = 0.5
    changed.loc[test_mask, "stock_split"] = 10.0
    changed.loc[test_mask, "raw_volume"] = 0.0
    changed.loc[test_mask, "adjusted_volume"] = 0.0

    baseline = _evaluator().evaluate_validation(original, _fold(), _catalog())
    modified = _evaluator().evaluate_validation(changed, _fold(), _catalog())

    assert modified == baseline


def test_feature_and_engine_inputs_never_include_test_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature_inputs: list[pd.DataFrame] = []
    engine_inputs: list[pd.DataFrame] = []

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
        engine_inputs.append(frame.copy())
        return original_run(self, symbol, frame, window=window)

    monkeypatch.setattr(executable_module, "_prepare_features", features)
    monkeypatch.setattr(BacktestEngine, "run", recording_run)

    _evaluator().evaluate_validation(
        _controlled_prices(), _fold(), _catalog("one", "two")
    )

    assert feature_inputs
    assert engine_inputs
    assert all(
        frame["date"].dt.date.max() < _fold().validation_end
        for frame in (*feature_inputs, *engine_inputs)
    )


def test_validation_last_signal_cannot_fill_on_existing_test_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    signal = np.full(len(_dates()), 100.0)
    signal[29] = 84.0
    bars = _bars(signal_close=signal)

    result = _evaluator().evaluate_validation(bars, _fold(), _catalog())

    outcome = result.symbol_outcomes[0]
    assert outcome.filled_order_count == 0
    assert outcome.canceled_order_count == 1
    assert outcome.open_position_at_end is False
    assert outcome.number_of_trades == 0


def test_test_winners_and_losers_cannot_change_candidate_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    original = _controlled_prices()
    winner = original.copy()
    loser = original.copy()
    test_mask = original["date"].dt.date >= _fold().validation_end
    _replace_prices(winner, test_mask, 1_000.0)
    _replace_prices(loser, test_mask, 0.001)
    evaluator = _evaluator()
    catalog = _catalog("one", "two")

    baseline = evaluator.evaluate_validation(original, _fold(), catalog)
    winning_test = evaluator.evaluate_validation(winner, _fold(), catalog)
    losing_test = evaluator.evaluate_validation(loser, _fold(), catalog)

    assert winning_test.scores == baseline.scores
    assert losing_test.scores == baseline.scores
    assert winning_test == baseline
    assert losing_test == baseline


def test_completed_early_trade_is_unchanged_by_later_validation_prices(
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
    later_validation = (changed["date"].dt.date >= _dates()[20].date()) & (
        changed["date"].dt.date < _fold().validation_end
    )
    _replace_prices(changed, later_validation, 10.0)

    _evaluator().evaluate_validation(baseline, _fold(), _catalog())
    _evaluator().evaluate_validation(changed, _fold(), _catalog())

    pd.testing.assert_series_equal(
        backtests[0].trade_log.iloc[0], backtests[1].trade_log.iloc[0]
    )
