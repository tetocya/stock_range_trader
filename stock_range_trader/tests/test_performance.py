"""Tests for strategy, trade, risk, and benchmark metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from metrics import (
    calculate_performance_metrics,
    executable_buy_and_hold_equity,
)


def _inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = pd.date_range("2025-01-01", periods=4, freq="B")
    equity = pd.DataFrame(
        {
            "date": dates,
            "cash": [100.0, 100.0, 90.0, 120.0],
            "position_value": [0.0, 10.0, 9.0, 0.0],
            "total_equity": [100.0, 110.0, 99.0, 120.0],
            "drawdown": [0.0, 0.0, -0.10, 0.0],
        }
    )
    trades = pd.DataFrame(
        {
            "net_profit": [10.0, -5.0, 20.0],
            "holding_days": [2, 4, 6],
        }
    )
    market = pd.DataFrame(
        {
            "date": dates,
            "open": [50.0, 55.0, 60.0, 66.0],
            "close": [50.0, 55.0, 60.0, 66.0],
            "volume": [100.0, 100.0, 100.0, 100.0],
        }
    )
    return equity, trades, market


def test_return_cagr_drawdown_exposure_and_benchmark() -> None:
    equity, trades, market = _inputs()

    result = calculate_performance_metrics(
        equity, trades, market, initial_capital=100.0, annual_trading_days=3
    )

    assert result.final_equity == 120.0
    assert result.total_return == pytest.approx(0.20)
    assert result.cagr == pytest.approx(0.20)
    assert result.maximum_drawdown == pytest.approx(-0.10)
    assert result.exposure == pytest.approx(0.50)
    assert result.theoretical_buy_and_hold_return == pytest.approx(0.32)
    assert result.executable_buy_and_hold_return == 0.0
    assert result.strategy_vs_executable_buy_and_hold == pytest.approx(0.20)


def test_trade_statistics_use_net_profit() -> None:
    equity, trades, market = _inputs()

    result = calculate_performance_metrics(
        equity, trades, market, initial_capital=100.0, annual_trading_days=3
    )

    assert result.number_of_trades == 3
    assert result.win_rate == pytest.approx(2.0 / 3.0)
    assert result.average_profit_per_trade == pytest.approx(25.0 / 3.0)
    assert result.average_winning_trade == pytest.approx(15.0)
    assert result.average_losing_trade == pytest.approx(-5.0)
    assert result.profit_factor == pytest.approx(6.0)
    assert result.average_holding_period == pytest.approx(4.0)


def test_sharpe_and_sortino_match_explicit_daily_formulas() -> None:
    equity, trades, market = _inputs()
    returns = np.array([0.10, -0.10, 120.0 / 99.0 - 1.0])
    expected_sharpe = returns.mean() / returns.std(ddof=1) * np.sqrt(3.0)
    downside_deviation = np.sqrt(np.mean(np.square(np.minimum(returns, 0.0))))
    expected_sortino = returns.mean() / downside_deviation * np.sqrt(3.0)

    result = calculate_performance_metrics(
        equity, trades, market, initial_capital=100.0, annual_trading_days=3
    )

    assert result.sharpe_ratio == pytest.approx(expected_sharpe)
    assert result.sortino_ratio == pytest.approx(expected_sortino)


def test_profit_factor_is_infinite_when_there_are_wins_but_no_losses() -> None:
    equity, trades, market = _inputs()
    trades = pd.DataFrame({"net_profit": [10.0, 20.0], "holding_days": [1, 2]})

    result = calculate_performance_metrics(
        equity, trades, market, initial_capital=100.0
    )

    assert np.isinf(result.profit_factor)


def test_no_trades_returns_explicit_empty_statistics() -> None:
    equity, _, market = _inputs()
    trades = pd.DataFrame(columns=["net_profit", "holding_days"])

    result = calculate_performance_metrics(
        equity, trades, market, initial_capital=100.0
    )

    assert result.number_of_trades == 0
    assert result.win_rate == 0.0
    assert np.isnan(result.average_profit_per_trade)
    assert np.isnan(result.average_winning_trade)
    assert np.isnan(result.average_losing_trade)
    assert np.isnan(result.average_holding_period)
    assert np.isnan(result.profit_factor)


def test_cagr_uses_configured_trading_periods() -> None:
    dates = pd.date_range("2024-01-01", periods=253, freq="B")
    values = np.geomspace(100.0, 121.0, len(dates))
    equity = pd.DataFrame(
        {"date": dates, "position_value": values, "total_equity": values}
    )
    trades = pd.DataFrame(columns=["net_profit", "holding_days"])
    market = pd.DataFrame(
        {
            "date": dates,
            "open": values,
            "close": values,
            "volume": np.full(len(dates), 100.0),
        }
    )

    result = calculate_performance_metrics(
        equity, trades, market, initial_capital=100.0, annual_trading_days=252
    )

    assert result.cagr == pytest.approx(0.21)


def test_market_and_equity_dates_must_match() -> None:
    equity, trades, market = _inputs()
    market.loc[3, "date"] += pd.Timedelta(days=1)

    with pytest.raises(ValueError, match="dates must match exactly"):
        calculate_performance_metrics(equity, trades, market, initial_capital=100.0)


def test_metric_mapping_contains_all_public_values() -> None:
    equity, trades, market = _inputs()
    result = calculate_performance_metrics(
        equity, trades, market, initial_capital=100.0
    )

    mapping = result.to_dict()

    assert mapping["number_of_trades"] == 3
    assert mapping["final_equity"] == 120.0
    assert len(mapping) == 18


def test_maximum_drawdown_is_recomputed_from_equity_not_trusted_input() -> None:
    equity, trades, market = _inputs()
    equity["drawdown"] = 0.0

    result = calculate_performance_metrics(
        equity, trades, market, initial_capital=100.0
    )

    assert result.maximum_drawdown == pytest.approx(-0.10)


def test_executable_benchmark_uses_lots_cash_slippage_and_commission() -> None:
    dates = pd.date_range("2025-01-01", periods=2, freq="B")
    market = pd.DataFrame(
        {
            "date": dates,
            "open": [31.0, 39.0],
            "close": [30.0, 40.0],
            "volume": [1_000.0, 1_000.0],
        }
    )

    equity = executable_buy_and_hold_equity(
        market,
        initial_capital=10_000.0,
        lot_size=100,
        slippage_pct=0.10,
        commission_rate=0.01,
    )

    execution_price = 31.0 * 1.10
    shares = 200
    commission = execution_price * shares * 0.01
    residual_cash = 10_000.0 - execution_price * shares - commission
    assert equity.iloc[0] == pytest.approx(residual_cash + shares * 30.0)
    assert equity.iloc[-1] == pytest.approx(residual_cash + shares * 40.0)


def test_executable_benchmark_is_cash_when_one_lot_is_unaffordable() -> None:
    _, _, market = _inputs()

    equity = executable_buy_and_hold_equity(market, initial_capital=100.0, lot_size=100)

    np.testing.assert_allclose(equity, 100.0)


def test_benchmark_metrics_keep_theoretical_and_executable_returns_distinct() -> None:
    equity, trades, market = _inputs()

    result = calculate_performance_metrics(
        equity, trades, market, initial_capital=100.0
    )

    assert result.theoretical_buy_and_hold_return == pytest.approx(0.32)
    assert result.executable_buy_and_hold_return == 0.0
    assert result.strategy_vs_executable_buy_and_hold == pytest.approx(0.20)
