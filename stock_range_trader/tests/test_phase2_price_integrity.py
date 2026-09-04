"""Regression tests for signal/execution price and corporate-action policy."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from phase2_helpers import canonical_bars

from backtest import BacktestEngine, MarketOnNextOpen
from data import (
    UnsupportedCorporateActionError,
    canonical_to_phase1,
    validate_backtest_price_contract,
)
from metrics import calculate_backtest_metrics, executable_buy_and_hold_equity
from risk import RiskManager
from strategy import MeanReversionStrategy


def _dual_price_market(
    signal_closes: list[float],
    raw_opens: list[float],
    raw_closes: list[float],
    *,
    split_ratios: list[float] | None = None,
    dividends: list[float] | None = None,
) -> pd.DataFrame:
    signal_close = np.asarray(signal_closes, dtype=float)
    signal_open = signal_close.copy()
    raw_open = np.asarray(raw_opens, dtype=float)
    raw_close = np.asarray(raw_closes, dtype=float)
    size = len(signal_close)
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-06", periods=size, freq="B"),
            "open": signal_open,
            "high": np.maximum(signal_open, signal_close) + 1.0,
            "low": np.minimum(signal_open, signal_close) - 1.0,
            "close": signal_close,
            "volume": np.full(size, 100_000.0),
            "signal_open": signal_open,
            "signal_high": np.maximum(signal_open, signal_close) + 1.0,
            "signal_low": np.minimum(signal_open, signal_close) - 1.0,
            "signal_close": signal_close,
            "signal_volume": np.full(size, 100_000.0),
            "execution_open": raw_open,
            "execution_high": np.maximum(raw_open, raw_close) + 2.0,
            "execution_low": np.minimum(raw_open, raw_close) - 2.0,
            "execution_close": raw_close,
            "execution_volume": np.full(size, 100_000.0),
            "split_ratio": split_ratios or [1.0] * size,
            "dividend": dividends or [0.0] * size,
            "corporate_action_supported": [True] * size,
            "sma": np.full(size, 100.0),
            "atr": np.full(size, 10.0),
            "adx": np.full(size, 20.0),
            "range_score": np.full(size, 80.0),
        }
    )


def _engine() -> BacktestEngine:
    return BacktestEngine(
        strategy=MeanReversionStrategy(),
        execution_model=MarketOnNextOpen(slippage_pct=0.0, commission_rate=0.001),
        risk_manager=RiskManager(),
        initial_capital=1_000_000.0,
    )


def test_position_size_and_costs_use_provider_reported_open() -> None:
    frame = _dual_price_market(
        [42.0, 50.0],
        raw_opens=[168.0, 200.0],
        raw_closes=[168.0, 200.0],
    )

    result = _engine().run("7203", frame)
    fill = result.fills[0]

    assert fill.raw_open_price == 200.0
    assert fill.shares == 500
    assert fill.commission == pytest.approx(100.0)
    assert result.portfolio.position is not None
    assert result.portfolio.position.signal_entry_price == 50.0


def test_non_unit_split_ratio_is_rejected_before_share_adjustment() -> None:
    frame = _dual_price_market(
        [84.0, 100.0, 100.0, 100.0],
        raw_opens=[168.0, 200.0, 100.0, 100.0],
        raw_closes=[168.0, 200.0, 100.0, 100.0],
        split_ratios=[1.0, 1.0, 2.0, 1.0],
    )

    with pytest.raises(UnsupportedCorporateActionError, match="adjustment is disabled"):
        _engine().run("7203", frame)


def test_7011_pre_split_window_is_still_executable_unsupported() -> None:
    bars = canonical_bars("7011.T", periods=5, end=date(2024, 3, 27))

    adapted = canonical_to_phase1(bars, symbol="7011.T")

    assert bars["date"].dt.date.max() < date(2024, 3, 28)
    assert bars["stock_split"].eq(0.0).all()
    assert adapted["split_ratio"].eq(1.0).all()
    assert not adapted["corporate_action_supported"].any()
    with pytest.raises(UnsupportedCorporateActionError, match="always unsupported"):
        validate_backtest_price_contract(adapted)


def test_yfinance_split_event_remains_executable_unsupported() -> None:
    bars = canonical_bars(periods=4)
    bars.loc[2, "stock_split"] = 2.0

    adapted = canonical_to_phase1(bars, symbol="7203.T")

    assert adapted["split_ratio"].eq(1.0).all()
    assert not adapted["corporate_action_supported"].any()
    with pytest.raises(
        UnsupportedCorporateActionError,
        match="always unsupported",
    ):
        validate_backtest_price_contract(adapted)


def test_dividends_are_excluded_from_strategy_and_both_benchmarks() -> None:
    without_dividend = _dual_price_market(
        [84.0, 100.0, 100.0],
        raw_opens=[84.0, 100.0, 100.0],
        raw_closes=[84.0, 100.0, 100.0],
    )
    with_dividend = without_dividend.copy()
    with_dividend["dividend"] = [0.0, 25.0, 0.0]

    first = _engine().run("7203", without_dividend)
    second = _engine().run("7203", with_dividend)
    first_metrics = calculate_backtest_metrics(first)
    second_metrics = calculate_backtest_metrics(second)

    pd.testing.assert_series_equal(
        first.equity_curve["total_equity"], second.equity_curve["total_equity"]
    )
    assert second_metrics.total_return == first_metrics.total_return
    assert (
        second_metrics.theoretical_buy_and_hold_return
        == first_metrics.theoretical_buy_and_hold_return
    )
    assert (
        second_metrics.executable_buy_and_hold_return
        == first_metrics.executable_buy_and_hold_return
    )


def test_benchmarks_use_provider_reported_prices_only_for_split_free_interval() -> None:
    frame = _dual_price_market(
        [100.0, 100.0],
        raw_opens=[200.0, 100.0],
        raw_closes=[200.0, 100.0],
    )
    benchmark = executable_buy_and_hold_equity(
        frame,
        initial_capital=1_000_000.0,
        lot_size=100,
    )
    metrics = calculate_backtest_metrics(_engine().run("7203", frame))

    assert list(benchmark) == [1_000_000.0, 500_000.0]
    assert metrics.theoretical_buy_and_hold_return == -0.5
    assert metrics.executable_buy_and_hold_return == pytest.approx(-0.49098)

    frame.loc[1, "split_ratio"] = 2.0
    with pytest.raises(UnsupportedCorporateActionError, match="adjustment is disabled"):
        executable_buy_and_hold_equity(frame, initial_capital=1_000_000.0)


def test_future_split_is_unsupported_without_rewriting_prior_prefix_fill() -> None:
    original = _dual_price_market(
        [84.0, 100.0, 100.0, 100.0],
        raw_opens=[168.0, 200.0, 200.0, 200.0],
        raw_closes=[168.0, 200.0, 200.0, 200.0],
    )
    with_future_split = original.copy()
    with_future_split.loc[3, "split_ratio"] = 2.0

    first = _engine().run("7203", original.iloc[:3].copy()).fills[0]
    second = _engine().run("7203", with_future_split.iloc[:3].copy()).fills[0]

    assert second.execution_date == first.execution_date
    assert second.raw_open_price == first.raw_open_price
    assert second.execution_price == first.execution_price
    assert second.shares == first.shares
    with pytest.raises(UnsupportedCorporateActionError, match="adjustment is disabled"):
        _engine().run("7203", with_future_split)


def test_unsupported_corporate_action_refuses_executable_result() -> None:
    frame = _dual_price_market(
        [84.0, 100.0],
        raw_opens=[168.0, 200.0],
        raw_closes=[168.0, 200.0],
    )
    frame.loc[1, "corporate_action_supported"] = False

    with pytest.raises(UnsupportedCorporateActionError, match="price basis"):
        _engine().run("7203", frame)
