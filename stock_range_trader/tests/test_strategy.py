"""Tests for mean-reversion signal generation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategy import (
    ExitReason,
    MeanReversionStrategy,
    PositionContext,
    SignalAction,
)


def _features(
    close: list[float],
    *,
    sma: float = 100.0,
    atr: float = 10.0,
    adx: list[float] | None = None,
    score: list[float] | None = None,
) -> pd.DataFrame:
    size = len(close)
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=size, freq="B"),
            "close": close,
            "sma": np.full(size, sma),
            "atr": np.full(size, atr),
            "adx": adx if adx is not None else np.full(size, 20.0),
            "range_score": score if score is not None else np.full(size, 80.0),
        }
    )


def test_prepare_calculates_atr_thresholds_without_mutating_input() -> None:
    frame = _features([85.0])
    original = frame.copy(deep=True)

    result = MeanReversionStrategy().prepare(frame)

    pd.testing.assert_frame_equal(frame, original)
    assert result.loc[0, "buy_threshold"] == pytest.approx(85.0)
    assert result.loc[0, "sell_threshold"] == pytest.approx(115.0)
    assert bool(result.loc[0, "entry_condition"])


@pytest.mark.parametrize(
    ("close", "score", "adx"),
    [
        (86.0, 80.0, 20.0),
        (85.0, 69.9, 20.0),
        (85.0, 80.0, 25.1),
    ],
)
def test_buy_requires_price_score_and_adx_conditions(
    close: float, score: float, adx: float
) -> None:
    strategy = MeanReversionStrategy()
    prepared = strategy.prepare(_features([close], score=[score], adx=[adx]))

    signal = strategy.generate_signal(prepared.iloc[0], PositionContext())

    assert signal.action is SignalAction.HOLD


def test_buy_signal_uses_completed_bar_date() -> None:
    strategy = MeanReversionStrategy()
    prepared = strategy.prepare(_features([84.0]))

    signal = strategy.generate_signal(prepared.iloc[0], PositionContext())

    assert signal.action is SignalAction.BUY
    assert signal.signal_date == prepared.loc[0, "date"]
    assert signal.exit_reason is None


def test_entry_filter_boundaries_are_inclusive() -> None:
    strategy = MeanReversionStrategy()
    prepared = strategy.prepare(_features([85.0], score=[70.0], adx=[25.0]))

    signal = strategy.generate_signal(prepared.iloc[0], PositionContext())

    assert signal.action is SignalAction.BUY


def test_open_position_cannot_generate_another_buy() -> None:
    strategy = MeanReversionStrategy()
    prepared = strategy.prepare(_features([84.0]))
    position = PositionContext(has_position=True, entry_price=80.0, holding_days=1)

    signal = strategy.generate_signal(prepared.iloc[0], position)

    assert signal.action is SignalAction.HOLD


def test_mean_reversion_exit() -> None:
    strategy = MeanReversionStrategy()
    prepared = strategy.prepare(_features([115.0]))
    position = PositionContext(has_position=True, entry_price=90.0, holding_days=5)

    signal = strategy.generate_signal(prepared.iloc[0], position)

    assert signal.action is SignalAction.SELL
    assert signal.exit_reason is ExitReason.MEAN_REVERSION


def test_stop_loss_uses_actual_entry_price() -> None:
    strategy = MeanReversionStrategy(stop_loss_pct=0.05)
    prepared = strategy.prepare(_features([94.99]))
    position = PositionContext(has_position=True, entry_price=100.0, holding_days=2)

    signal = strategy.generate_signal(prepared.iloc[0], position)

    assert signal.action is SignalAction.SELL
    assert signal.exit_reason is ExitReason.STOP_LOSS


def test_stop_loss_boundary_is_inclusive() -> None:
    strategy = MeanReversionStrategy(stop_loss_pct=0.05)
    prepared = strategy.prepare(_features([95.0]))
    position = PositionContext(has_position=True, entry_price=100.0, holding_days=2)

    signal = strategy.generate_signal(prepared.iloc[0], position)

    assert signal.exit_reason is ExitReason.STOP_LOSS


def test_range_breakdown_requires_configured_consecutive_days() -> None:
    strategy = MeanReversionStrategy(range_breakdown_days=3)
    prepared = strategy.prepare(
        _features(
            [100.0] * 5,
            score=[80.0, 40.0, 40.0, 40.0, 80.0],
        )
    )
    position = PositionContext(has_position=True, entry_price=100.0, holding_days=5)

    assert prepared["range_breakdown_streak"].tolist() == [0, 1, 2, 3, 0]
    assert (
        strategy.generate_signal(prepared.iloc[2], position).action is SignalAction.HOLD
    )
    signal = strategy.generate_signal(prepared.iloc[3], position)
    assert signal.action is SignalAction.SELL
    assert signal.exit_reason is ExitReason.RANGE_BREAKDOWN


def test_high_adx_also_counts_as_range_breakdown() -> None:
    strategy = MeanReversionStrategy(range_breakdown_days=2)
    prepared = strategy.prepare(
        _features([100.0, 100.0], adx=[31.0, 31.0], score=[80.0, 80.0])
    )
    position = PositionContext(has_position=True, entry_price=100.0, holding_days=2)

    signal = strategy.generate_signal(prepared.iloc[1], position)

    assert signal.exit_reason is ExitReason.RANGE_BREAKDOWN


def test_maximum_holding_period_exit() -> None:
    strategy = MeanReversionStrategy(max_holding_days=40)
    prepared = strategy.prepare(_features([100.0]))
    position = PositionContext(has_position=True, entry_price=100.0, holding_days=40)

    signal = strategy.generate_signal(prepared.iloc[0], position)

    assert signal.action is SignalAction.SELL
    assert signal.exit_reason is ExitReason.MAX_HOLDING_PERIOD


def test_stop_loss_has_highest_exit_priority() -> None:
    strategy = MeanReversionStrategy(max_holding_days=1, range_breakdown_days=1)
    prepared = strategy.prepare(_features([90.0], score=[0.0], adx=[100.0]))
    position = PositionContext(has_position=True, entry_price=100.0, holding_days=1)

    signal = strategy.generate_signal(prepared.iloc[0], position)

    assert signal.exit_reason is ExitReason.STOP_LOSS


def test_no_exit_condition_returns_hold() -> None:
    strategy = MeanReversionStrategy()
    prepared = strategy.prepare(_features([100.0]))
    position = PositionContext(has_position=True, entry_price=100.0, holding_days=2)

    signal = strategy.generate_signal(prepared.iloc[0], position)

    assert signal.action is SignalAction.HOLD


def test_future_changes_do_not_alter_past_strategy_conditions() -> None:
    strategy = MeanReversionStrategy()
    original = _features([100.0] * 100)
    original.loc[10:20, "close"] = 84.0
    original_result = strategy.prepare(original)

    changed = original.copy()
    changed.loc[80:, ["close", "sma", "atr", "adx", "range_score"]] = [
        1_000.0,
        2_000.0,
        500.0,
        100.0,
        0.0,
    ]
    changed_result = strategy.prepare(changed)

    pd.testing.assert_frame_equal(original_result.iloc[:80], changed_result.iloc[:80])


def test_invalid_position_context_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive entry_price"):
        PositionContext(has_position=True, entry_price=None, holding_days=1)
