"""Tests for simulated execution, trade records, and portfolio accounting."""

from __future__ import annotations

import pandas as pd
import pytest

from backtest import MarketBar, MarketOnNextOpen, Order, OrderSide, Portfolio
from strategy import ExitReason


def _order(
    side: OrderSide,
    *,
    shares: int = 100,
    signal_date: str = "2025-01-06",
    reason: ExitReason | None = None,
) -> Order:
    return Order(
        symbol="7203",
        side=side,
        signal_date=pd.Timestamp(signal_date),
        shares=shares,
        exit_reason=reason,
    )


def _bar(
    date: str = "2025-01-07",
    *,
    open_price: float = 1_000.0,
    volume: float = 100_000.0,
) -> MarketBar:
    return MarketBar(
        date=pd.Timestamp(date),
        open=open_price,
        high=open_price + 10.0,
        low=open_price - 10.0,
        close=open_price,
        volume=volume,
    )


def test_buy_slippage_is_added_to_next_open() -> None:
    model = MarketOnNextOpen(slippage_pct=0.001)

    fill = model.execute(_order(OrderSide.BUY), _bar())

    assert fill is not None
    assert fill.execution_price == pytest.approx(1_001.0)
    assert fill.slippage_cost == pytest.approx(100.0)
    assert fill.commission == 0.0


def test_sell_slippage_is_subtracted_from_next_open() -> None:
    model = MarketOnNextOpen(slippage_pct=0.001)

    fill = model.execute(
        _order(OrderSide.SELL, reason=ExitReason.MEAN_REVERSION), _bar()
    )

    assert fill is not None
    assert fill.execution_price == pytest.approx(999.0)
    assert fill.slippage_cost == pytest.approx(100.0)


def test_commission_uses_executed_transaction_value() -> None:
    model = MarketOnNextOpen(slippage_pct=0.001, commission_rate=0.002)

    fill = model.execute(_order(OrderSide.BUY), _bar())

    assert fill is not None
    assert fill.commission == pytest.approx(1_001.0 * 100 * 0.002)


def test_signal_cannot_execute_at_same_day_open() -> None:
    model = MarketOnNextOpen()

    with pytest.raises(ValueError, match="after signal_date"):
        model.execute(
            _order(OrderSide.BUY),
            _bar("2025-01-06"),
        )


def test_portfolio_buy_and_sell_cash_flow_matches_net_profit() -> None:
    model = MarketOnNextOpen(slippage_pct=0.001, commission_rate=0.001)
    portfolio = Portfolio(initial_capital=1_000_000.0)
    entry = model.execute(_order(OrderSide.BUY), _bar())
    assert entry is not None
    portfolio.apply_fill(entry)
    for _ in range(5):
        portfolio.increment_holding_days()
    exit_fill = model.execute(
        _order(
            OrderSide.SELL,
            signal_date="2025-01-13",
            reason=ExitReason.MEAN_REVERSION,
        ),
        _bar("2025-01-14", open_price=1_100.0),
    )
    assert exit_fill is not None

    trade = portfolio.apply_fill(exit_fill)

    assert trade is not None
    assert portfolio.position is None
    assert trade.holding_days == 5
    assert trade.gross_profit == pytest.approx(
        (exit_fill.execution_price - entry.execution_price) * 100
    )
    assert trade.commission == pytest.approx(entry.commission + exit_fill.commission)
    assert trade.slippage_cost == pytest.approx(
        entry.slippage_cost + exit_fill.slippage_cost
    )
    assert trade.net_profit == pytest.approx(trade.gross_profit - trade.commission)
    assert portfolio.cash == pytest.approx(portfolio.initial_capital + trade.net_profit)
    assert trade.to_record()["exit_reason"] == "mean_reversion"


def test_portfolio_mark_to_market_equity() -> None:
    portfolio = Portfolio(initial_capital=1_000_000.0)
    fill = MarketOnNextOpen(slippage_pct=0.0).execute(_order(OrderSide.BUY), _bar())
    assert fill is not None
    portfolio.apply_fill(fill)

    assert portfolio.cash == pytest.approx(900_000.0)
    assert portfolio.position_value(1_050.0) == pytest.approx(105_000.0)
    assert portfolio.total_equity(1_050.0) == pytest.approx(1_005_000.0)


def test_portfolio_rejects_sell_without_position() -> None:
    fill = MarketOnNextOpen().execute(
        _order(OrderSide.SELL, reason=ExitReason.STOP_LOSS), _bar()
    )
    assert fill is not None

    with pytest.raises(ValueError, match="without an open"):
        Portfolio().apply_fill(fill)


def test_portfolio_rejects_second_long_position() -> None:
    model = MarketOnNextOpen()
    portfolio = Portfolio()
    first = model.execute(_order(OrderSide.BUY), _bar())
    second = model.execute(
        _order(OrderSide.BUY, signal_date="2025-01-07"),
        _bar("2025-01-08"),
    )
    assert first is not None
    assert second is not None
    portfolio.apply_fill(first)

    with pytest.raises(ValueError, match="second position"):
        portfolio.apply_fill(second)


def test_portfolio_rejects_buy_when_commission_exceeds_cash() -> None:
    portfolio = Portfolio(initial_capital=100_000.0)
    fill = MarketOnNextOpen(slippage_pct=0.0, commission_rate=0.001).execute(
        _order(OrderSide.BUY), _bar()
    )
    assert fill is not None

    with pytest.raises(ValueError, match="insufficient cash"):
        portfolio.apply_fill(fill)


def test_zero_volume_bar_cannot_generate_a_fill() -> None:
    fill = MarketOnNextOpen().execute(_order(OrderSide.BUY), _bar(volume=0.0))

    assert fill is None


def test_negative_volume_bar_is_rejected() -> None:
    with pytest.raises(ValueError, match="volume must be finite and non-negative"):
        _bar(volume=-1.0)
