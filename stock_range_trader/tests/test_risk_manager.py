"""Tests for position sizing and portfolio-level risk controls."""

from __future__ import annotations

import pytest

from risk import RiskManager


def test_position_size_obeys_allocation_and_lot_size() -> None:
    manager = RiskManager(max_position_pct=0.10, lot_size=100)

    shares = manager.calculate_position_size(
        portfolio_value=1_000_000.0,
        execution_price=497.0,
        available_cash=1_000_000.0,
    )

    assert shares == 200
    assert shares % 100 == 0
    assert shares * 497.0 <= 100_000.0


def test_position_size_returns_zero_below_one_lot() -> None:
    manager = RiskManager(max_position_pct=0.10, lot_size=100)

    shares = manager.calculate_position_size(
        portfolio_value=1_000_000.0,
        execution_price=1_001.0,
        available_cash=1_000_000.0,
    )

    assert shares == 0


def test_position_size_includes_commission_in_cash_limit() -> None:
    manager = RiskManager(max_position_pct=1.0, lot_size=100)

    shares = manager.calculate_position_size(
        portfolio_value=100_000.0,
        execution_price=1_000.0,
        available_cash=100_000.0,
        commission_rate=0.001,
    )

    assert shares == 0


def test_maximum_position_count_blocks_new_buy() -> None:
    manager = RiskManager(max_positions=1)

    shares = manager.calculate_position_size(
        portfolio_value=1_000_000.0,
        execution_price=500.0,
        available_cash=1_000_000.0,
        current_positions=1,
    )

    assert shares == 0


def test_drawdown_threshold_halts_future_buys() -> None:
    manager = RiskManager(max_drawdown_stop=0.20)
    manager.update_equity(1_000_000.0)

    drawdown = manager.update_equity(800_000.0)

    assert drawdown == pytest.approx(-0.20)
    assert manager.buying_halted
    assert not manager.allows_new_position(0)


def test_drawdown_halt_is_sticky_after_recovery() -> None:
    manager = RiskManager(max_drawdown_stop=0.20)
    manager.update_equity(1_000_000.0)
    manager.update_equity(790_000.0)

    manager.update_equity(1_010_000.0)

    assert manager.current_drawdown == 0.0
    assert manager.buying_halted


def test_drawdown_below_threshold_still_allows_buy() -> None:
    manager = RiskManager(max_drawdown_stop=0.20)
    manager.update_equity(1_000_000.0)

    manager.update_equity(810_000.0)

    assert manager.allows_new_position(0)


def test_available_cash_can_reduce_position_below_allocation_limit() -> None:
    manager = RiskManager(max_position_pct=1.0, lot_size=100)

    shares = manager.calculate_position_size(
        portfolio_value=1_000_000.0,
        execution_price=1_000.0,
        available_cash=250_000.0,
    )

    assert shares == 200
