"""Regression tests for signal/execution price and corporate-action policy."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest import BacktestEngine, MarketOnNextOpen
from data import UnsupportedCorporateActionError
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


def test_position_size_and_costs_use_historical_unadjusted_open() -> None:
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


def test_split_adjusts_shares_and_cost_basis_without_equity_jump() -> None:
    frame = _dual_price_market(
        [84.0, 100.0, 100.0, 100.0],
        raw_opens=[168.0, 200.0, 100.0, 100.0],
        raw_closes=[168.0, 200.0, 100.0, 100.0],
        split_ratios=[1.0, 1.0, 2.0, 1.0],
    )

    result = _engine().run("7203", frame)

    assert result.portfolio.position is not None
    assert result.portfolio.position.shares == 1_000
    assert result.portfolio.position.split_adjusted_entry_price == 100.0
    assert result.equity_curve.loc[1, "position_value"] == pytest.approx(100_000.0)
    assert result.equity_curve.loc[2, "position_value"] == pytest.approx(100_000.0)
    assert result.equity_curve.loc[2, "total_equity"] == pytest.approx(
        result.equity_curve.loc[1, "total_equity"]
    )


def test_trade_across_split_uses_adjusted_exit_shares_and_total_cost_basis() -> None:
    frame = _dual_price_market(
        [84.0, 100.0, 116.0, 110.0],
        raw_opens=[168.0, 200.0, 116.0, 110.0],
        raw_closes=[168.0, 200.0, 116.0, 110.0],
        split_ratios=[1.0, 1.0, 2.0, 1.0],
    )

    result = _engine().run("7203", frame)
    trade = result.trade_log.iloc[0]

    assert trade["shares"] == 500
    assert trade["exit_shares"] == 1_000
    assert trade["split_adjustment_ratio"] == 2.0
    assert trade["split_adjusted_entry_price"] == 100.0
    assert trade["gross_profit"] == pytest.approx(10_000.0)
    assert result.portfolio.cash == pytest.approx(1_009_790.0)


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


def test_theoretical_and_executable_benchmarks_use_raw_prices_and_splits() -> None:
    frame = _dual_price_market(
        [100.0, 100.0],
        raw_opens=[200.0, 100.0],
        raw_closes=[200.0, 100.0],
        split_ratios=[1.0, 2.0],
    )
    benchmark = executable_buy_and_hold_equity(
        frame,
        initial_capital=1_000_000.0,
        lot_size=100,
    )
    metrics = calculate_backtest_metrics(_engine().run("7203", frame))

    assert list(benchmark) == [1_000_000.0, 1_000_000.0]
    assert metrics.theoretical_buy_and_hold_return == 0.0
    assert metrics.executable_buy_and_hold_return == pytest.approx(-0.00098)


def test_future_split_does_not_rewrite_an_earlier_fill_price_or_size() -> None:
    original = _dual_price_market(
        [84.0, 100.0, 100.0, 100.0],
        raw_opens=[168.0, 200.0, 200.0, 200.0],
        raw_closes=[168.0, 200.0, 200.0, 200.0],
    )
    with_future_split = original.copy()
    signal_columns = [
        "open",
        "high",
        "low",
        "close",
        "signal_open",
        "signal_high",
        "signal_low",
        "signal_close",
    ]
    with_future_split.loc[:2, signal_columns] *= 0.5
    with_future_split.loc[3, "split_ratio"] = 2.0
    with_future_split.loc[3, "execution_open"] = 100.0
    with_future_split.loc[3, "execution_high"] = 102.0
    with_future_split.loc[3, "execution_low"] = 98.0
    with_future_split.loc[3, "execution_close"] = 100.0

    first = _engine().run("7203", original).fills[0]
    second = _engine().run("7203", with_future_split).fills[0]

    assert second.execution_date == first.execution_date
    assert second.raw_open_price == first.raw_open_price
    assert second.execution_price == first.execution_price
    assert second.shares == first.shares


def test_unsupported_corporate_action_refuses_executable_result() -> None:
    frame = _dual_price_market(
        [84.0, 100.0],
        raw_opens=[168.0, 200.0],
        raw_closes=[168.0, 200.0],
    )
    frame.loc[1, "corporate_action_supported"] = False

    with pytest.raises(UnsupportedCorporateActionError, match="split share ratio"):
        _engine().run("7203", frame)
