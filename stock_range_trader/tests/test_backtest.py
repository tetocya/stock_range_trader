"""Tests for chronological backtest-engine behavior."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest import BacktestEngine, MarketOnNextOpen, OrderSide
from risk import RiskManager
from strategy import ExitReason, MeanReversionStrategy, SignalAction


def _market(open_prices: list[float], closes: list[float]) -> pd.DataFrame:
    """Build valid bars with already-computed screening features."""

    opens = np.asarray(open_prices, dtype=float)
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-06", periods=len(close), freq="B"),
            "open": opens,
            "high": np.maximum(opens, close) + 1.0,
            "low": np.minimum(opens, close) - 1.0,
            "close": close,
            "volume": np.full(len(close), 100_000),
            "sma": np.full(len(close), 100.0),
            "atr": np.full(len(close), 10.0),
            "adx": np.full(len(close), 20.0),
            "range_score": np.full(len(close), 80.0),
        }
    )


def _engine(
    *,
    strategy: MeanReversionStrategy | None = None,
    slippage_pct: float = 0.001,
    commission_rate: float = 0.0,
) -> BacktestEngine:
    return BacktestEngine(
        strategy=strategy or MeanReversionStrategy(),
        execution_model=MarketOnNextOpen(slippage_pct, commission_rate),
        risk_manager=RiskManager(),
        initial_capital=1_000_000.0,
    )


def test_signals_execute_only_at_the_following_bar_open() -> None:
    frame = _market(
        open_prices=[100.0, 90.0, 100.0, 110.0],
        closes=[84.0, 100.0, 116.0, 110.0],
    )

    result = _engine().run("7203", frame)

    assert [fill.side for fill in result.fills] == [OrderSide.BUY, OrderSide.SELL]
    assert result.fills[0].signal_date == frame.loc[0, "date"]
    assert result.fills[0].execution_date == frame.loc[1, "date"]
    assert result.fills[0].raw_open_price == 90.0
    assert result.fills[1].signal_date == frame.loc[2, "date"]
    assert result.fills[1].execution_date == frame.loc[3, "date"]
    assert all(fill.execution_date > fill.signal_date for fill in result.fills)


def test_completed_trade_and_daily_equity_are_recorded() -> None:
    frame = _market(
        open_prices=[100.0, 90.0, 100.0, 110.0],
        closes=[84.0, 100.0, 116.0, 110.0],
    )

    result = _engine(commission_rate=0.001).run("7203", frame)

    assert len(result.trade_log) == 1
    trade = result.trade_log.iloc[0]
    assert trade["symbol"] == "7203"
    assert trade["entry_date"] == frame.loc[1, "date"]
    assert trade["exit_date"] == frame.loc[3, "date"]
    assert trade["exit_reason"] == "mean_reversion"
    assert trade["holding_days"] == 2
    assert trade["net_profit"] == pytest.approx(
        trade["gross_profit"] - trade["commission"]
    )
    assert len(result.equity_curve) == len(frame)
    assert list(result.equity_curve.columns) == [
        "date",
        "cash",
        "position_value",
        "total_equity",
        "drawdown",
    ]
    assert result.final_equity == pytest.approx(result.portfolio.cash)


def test_stop_loss_signal_exits_at_next_open() -> None:
    frame = _market(
        open_prices=[100.0, 100.0, 90.0],
        closes=[84.0, 94.0, 90.0],
    )

    result = _engine(slippage_pct=0.0).run("7203", frame)

    assert result.trade_log.loc[0, "exit_reason"] == "stop_loss"
    assert result.fills[1].signal_date == frame.loc[1, "date"]
    assert result.fills[1].execution_date == frame.loc[2, "date"]


def test_max_holding_counts_completed_position_sessions() -> None:
    frame = _market(
        open_prices=[100.0, 100.0, 100.0, 100.0],
        closes=[84.0, 100.0, 100.0, 100.0],
    )
    strategy = MeanReversionStrategy(max_holding_days=2)

    result = _engine(strategy=strategy, slippage_pct=0.0).run("7203", frame)

    assert result.trade_log.loc[0, "exit_reason"] == "max_holding_period"
    assert result.trade_log.loc[0, "holding_days"] == 2
    assert result.fills[1].execution_date == frame.loc[3, "date"]


def test_range_breakdown_exits_after_three_days_at_next_open() -> None:
    frame = _market(
        open_prices=[100.0, 100.0, 100.0, 100.0, 100.0],
        closes=[84.0, 100.0, 100.0, 100.0, 100.0],
    )
    frame["range_score"] = [80.0, 40.0, 40.0, 40.0, 80.0]

    result = _engine(slippage_pct=0.0).run("7203", frame)

    assert result.trade_log.loc[0, "exit_reason"] == "range_breakdown"
    assert result.fills[1].signal_date == frame.loc[3, "date"]
    assert result.fills[1].execution_date == frame.loc[4, "date"]


def test_last_bar_signal_remains_unexecuted_without_fabricated_price() -> None:
    frame = _market(
        open_prices=[100.0, 100.0, 100.0],
        closes=[84.0, 100.0, 116.0],
    )

    result = _engine(slippage_pct=0.0).run("7203", frame)

    assert result.trade_log.empty
    assert result.portfolio.position is not None
    assert result.unexecuted_signal is not None
    assert result.unexecuted_signal.action is SignalAction.SELL
    assert result.unexecuted_signal.exit_reason is ExitReason.MEAN_REVERSION
    assert len(result.fills) == 1


def test_buy_is_skipped_when_one_lot_exceeds_position_limit() -> None:
    frame = _market(
        open_prices=[100.0, 2_000.0],
        closes=[84.0, 2_000.0],
    )

    result = _engine().run("7203", frame)

    assert result.signal_log.iloc[0]["action"] == "buy"
    assert not result.fills
    assert result.portfolio.position is None
    assert result.final_equity == pytest.approx(1_000_000.0)


def test_result_writes_trade_and_equity_csv(tmp_path: Path) -> None:
    frame = _market(
        open_prices=[100.0, 90.0, 100.0, 110.0],
        closes=[84.0, 100.0, 116.0, 110.0],
    )
    result = _engine().run("7203", frame)
    trade_path = tmp_path / "nested" / "trade_log.csv"
    equity_path = tmp_path / "nested" / "equity_curve.csv"

    result.save_trade_log(trade_path)
    result.save_equity_curve(equity_path)

    trade_csv = pd.read_csv(trade_path)
    equity_csv = pd.read_csv(equity_path)
    assert list(trade_csv.columns) == list(result.trade_log.columns)
    assert len(trade_csv) == 1
    assert list(equity_csv.columns) == list(result.equity_curve.columns)
    assert len(equity_csv) == len(frame)


def test_independent_runs_reset_risk_state() -> None:
    engine = _engine(slippage_pct=0.0)
    engine.risk_manager.update_equity(1_000_000.0)
    engine.risk_manager.update_equity(700_000.0)
    assert engine.risk_manager.buying_halted
    frame = _market(open_prices=[100.0, 90.0], closes=[84.0, 90.0])

    first = engine.run("7203", frame)
    second = engine.run("7203", frame)

    assert len(first.fills) == 1
    assert len(second.fills) == 1
    pd.testing.assert_frame_equal(first.equity_curve, second.equity_curve)
