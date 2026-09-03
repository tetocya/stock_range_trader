"""Cash, long-position, and equity accounting."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .trade import Fill, OrderSide, Trade, validate_price


@dataclass(slots=True)
class Position:
    """The single long position supported by a Phase 1 portfolio."""

    entry_fill: Fill
    holding_days: int = 0

    @property
    def symbol(self) -> str:
        return self.entry_fill.symbol

    @property
    def shares(self) -> int:
        return self.entry_fill.shares

    @property
    def entry_price(self) -> float:
        return self.entry_fill.execution_price


@dataclass(slots=True)
class Portfolio:
    """Account for one long position without any broker-side operations."""

    initial_capital: float = 1_000_000.0
    cash: float = field(init=False)
    position: Position | None = field(init=False, default=None)
    closed_trades: list[Trade] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        validate_price(self.initial_capital, "initial_capital")
        self.cash = float(self.initial_capital)

    @property
    def position_count(self) -> int:
        return int(self.position is not None)

    def apply_fill(self, fill: Fill) -> Trade | None:
        """Apply a simulated fill and return a trade when a position closes."""

        if not isinstance(fill, Fill):
            raise TypeError("fill must be a Fill")
        if fill.side is OrderSide.BUY:
            self._open_position(fill)
            return None
        return self._close_position(fill)

    def increment_holding_days(self) -> None:
        """Record one completed trading session of long exposure."""

        if self.position is not None:
            self.position.holding_days += 1

    def position_value(self, market_price: float) -> float:
        """Return marked-to-market position value."""

        validate_price(market_price, "market_price")
        if self.position is None:
            return 0.0
        return self.position.shares * market_price

    def total_equity(self, market_price: float) -> float:
        """Return cash plus the marked-to-market long position."""

        return self.cash + self.position_value(market_price)

    def _open_position(self, fill: Fill) -> None:
        if self.position is not None:
            raise ValueError("cannot open a second position in a Phase 1 portfolio")
        total_cost = fill.notional + fill.commission
        if total_cost > self.cash + 1e-9:
            raise ValueError("insufficient cash for BUY fill and commission")
        self.cash -= total_cost
        self.position = Position(entry_fill=fill)

    def _close_position(self, fill: Fill) -> Trade:
        if self.position is None:
            raise ValueError("cannot SELL without an open long position")
        if fill.symbol != self.position.symbol:
            raise ValueError("SELL symbol does not match the open position")
        if fill.shares != self.position.shares:
            raise ValueError("partial position exits are not supported in Phase 1")

        self.cash += fill.notional - fill.commission
        trade = Trade.from_fills(
            self.position.entry_fill, fill, self.position.holding_days
        )
        self.closed_trades.append(trade)
        self.position = None
        return trade
