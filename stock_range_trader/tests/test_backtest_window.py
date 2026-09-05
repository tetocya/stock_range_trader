"""Tests for isolated half-open BacktestEngine trading windows."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from backtest import (
    BacktestEngine,
    BacktestWindow,
    BacktestWindowError,
    MarketOnNextOpen,
)
from risk import RiskManager
from strategy import MeanReversionStrategy, SignalAction


def _market(
    closes: list[float],
    *,
    opens: list[float] | None = None,
) -> pd.DataFrame:
    close = np.asarray(closes, dtype=float)
    open_ = close.copy() if opens is None else np.asarray(opens, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2025-01-06", periods=len(close)),
            "open": open_,
            "high": np.maximum(open_, close) + 1.0,
            "low": np.minimum(open_, close) - 1.0,
            "close": close,
            "volume": np.full(len(close), 100_000.0),
            "sma": np.full(len(close), 100.0),
            "atr": np.full(len(close), 10.0),
            "adx": np.full(len(close), 20.0),
            "range_score": np.full(len(close), 80.0),
        }
    )


def _engine() -> BacktestEngine:
    return BacktestEngine(
        strategy=MeanReversionStrategy(),
        execution_model=MarketOnNextOpen(slippage_pct=0.0),
        risk_manager=RiskManager(),
        initial_capital=1_000_000.0,
    )


def _window(frame: pd.DataFrame, start: int, end: int) -> BacktestWindow:
    return BacktestWindow(
        trading_start=frame.loc[start, "date"].date(),
        trading_end=frame.loc[end, "date"].date(),
    )


def test_window_is_immutable_and_half_open() -> None:
    window = BacktestWindow(date(2025, 1, 1), date(2025, 2, 1))

    assert window.contains(date(2025, 1, 1))
    assert window.contains(date(2025, 1, 31))
    assert not window.contains(date(2025, 2, 1))
    with pytest.raises(FrozenInstanceError):
        window.trading_start = date(2025, 1, 2)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("start", "end"),
    (
        (date(2025, 1, 1), date(2025, 1, 1)),
        (date(2025, 2, 1), date(2025, 1, 1)),
    ),
)
def test_window_rejects_empty_and_reversed_boundaries(start: date, end: date) -> None:
    with pytest.raises(BacktestWindowError, match="before"):
        BacktestWindow(start, end)


@pytest.mark.parametrize(
    "value",
    (
        datetime(2025, 1, 1),
        pd.Timestamp("2025-01-01"),
        "2025-01-01",
    ),
)
def test_window_rejects_values_with_time_or_wrong_type(value: object) -> None:
    with pytest.raises(BacktestWindowError, match="date without a time"):
        BacktestWindow(value, date(2025, 2, 1))  # type: ignore[arg-type]


def test_window_none_is_bit_for_bit_backward_compatible() -> None:
    frame = _market([84.0, 100.0, 116.0, 110.0], opens=[100.0, 90.0, 100.0, 110.0])

    positional = _engine().run("7203", frame)
    explicit = _engine().run("7203", frame, window=None)

    for field in (
        "prepared_data",
        "trade_log",
        "order_log",
        "equity_curve",
        "signal_log",
    ):
        pd.testing.assert_frame_equal(
            getattr(explicit, field), getattr(positional, field)
        )
    assert explicit.fills == positional.fills
    assert explicit.unexecuted_signal == positional.unexecuted_signal
    assert explicit.final_equity == positional.final_equity


def test_window_outputs_only_observed_rows_inside_half_open_interval() -> None:
    frame = _market([100.0] * 7)
    window = _window(frame, 2, 6)

    result = _engine().run("7203", frame, window=window)

    expected_dates = frame.loc[2:5, "date"].reset_index(drop=True)
    pd.testing.assert_series_equal(
        result.prepared_data["date"].reset_index(drop=True), expected_dates
    )
    pd.testing.assert_series_equal(
        result.equity_curve["date"].reset_index(drop=True), expected_dates
    )


def test_non_observed_boundary_starts_at_first_real_session() -> None:
    frame = _market([100.0] * 8)
    saturday = date(2025, 1, 11)
    window = BacktestWindow(saturday, frame.loc[7, "date"].date())

    result = _engine().run("7203", frame, window=window)

    assert result.prepared_data.iloc[0]["date"] == pd.Timestamp("2025-01-13")


def test_empty_observed_window_has_deterministic_error() -> None:
    frame = _market([100.0] * 3)
    window = BacktestWindow(date(2026, 1, 1), date(2026, 2, 1))

    with pytest.raises(BacktestWindowError, match="no observations"):
        _engine().run("7203", frame, window=window)


def test_pre_window_buy_signal_cannot_fill_at_first_window_open() -> None:
    frame = _market([84.0, 84.0, 100.0, 100.0, 100.0])
    window = _window(frame, 2, 4)

    result = _engine().run("7203", frame, window=window)

    assert result.fills == ()
    assert result.portfolio.position is None
    assert result.signal_log.empty


def test_last_window_signal_is_canceled_even_when_source_has_next_bar() -> None:
    frame = _market([100.0, 100.0, 84.0, 100.0])
    window = _window(frame, 1, 3)

    result = _engine().run("7203", frame, window=window)

    assert result.unexecuted_signal is not None
    assert result.unexecuted_signal.action is SignalAction.BUY
    assert result.order_log.iloc[-1]["status"] == "canceled"
    assert result.order_log.iloc[-1]["reason"] == "no_next_bar"
    assert result.fills == ()


def test_open_position_is_marked_but_not_force_closed_at_window_end() -> None:
    frame = _market([100.0, 84.0, 90.0, 120.0])
    window = _window(frame, 1, 3)

    result = _engine().run("7203", frame, window=window)

    assert len(result.fills) == 1
    assert result.portfolio.position is not None
    assert result.trade_log.empty
    expected_equity = result.portfolio.cash + result.portfolio.position.shares * 90.0
    assert result.final_equity == pytest.approx(expected_equity)


def test_same_engine_resets_risk_state_between_window_runs() -> None:
    frame = _market([100.0, 84.0, 90.0, 90.0, 90.0])
    window = _window(frame, 1, 4)
    engine = _engine()
    engine.risk_manager.update_equity(1_000_000.0)
    engine.risk_manager.update_equity(500_000.0)
    assert engine.risk_manager.buying_halted

    first = engine.run("7203", frame, window=window)
    second = engine.run("7203", frame, window=window)

    assert first.fills == second.fills
    pd.testing.assert_frame_equal(first.equity_curve, second.equity_curve)


def test_strategy_path_state_is_reset_at_window_start() -> None:
    frame = _market([100.0] * 7)
    frame["range_score"] = [40.0, 40.0, 40.0, 40.0, 40.0, 80.0, 80.0]
    window = _window(frame, 3, 6)

    result = _engine().run("7203", frame, window=window)

    assert list(result.prepared_data["range_breakdown_streak"]) == [1, 2, 0]


def test_future_price_changes_and_rows_do_not_affect_window_result() -> None:
    frame = _market([100.0, 84.0, 90.0, 100.0, 100.0])
    window = _window(frame, 1, 4)
    changed = frame.copy()
    changed.loc[changed["date"].dt.date >= window.trading_end, "open"] = np.nan
    changed.loc[changed["date"].dt.date >= window.trading_end, "close"] = np.inf
    appended = _market([1.0])
    appended.loc[:, "date"] = pd.Timestamp("2026-01-01")
    changed = pd.concat([changed, appended], ignore_index=True)

    baseline = _engine().run("7203", frame, window=window)
    modified = _engine().run("7203", changed, window=window)

    for field in ("prepared_data", "trade_log", "order_log", "equity_curve"):
        pd.testing.assert_frame_equal(
            getattr(modified, field), getattr(baseline, field)
        )
    assert modified.fills == baseline.fills
    assert modified.unexecuted_signal == baseline.unexecuted_signal
    assert modified.final_equity == baseline.final_equity
