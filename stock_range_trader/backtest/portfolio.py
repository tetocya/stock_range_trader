"""Cash, long-position, and equity accounting."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from data import UnsupportedCorporateActionError

from .trade import Fill, OrderSide, Trade, validate_price


@dataclass(slots=True)
class Position:
    """The single long position supported by a Phase 1 portfolio."""

    entry_fill: Fill
    signal_entry_price: float | None = None
    holding_days: int = 0
    current_shares: int = field(init=False)
    cumulative_split_ratio: float = field(init=False, default=1.0)

    def __post_init__(self) -> None:
        self.current_shares = self.entry_fill.shares
        if self.signal_entry_price is None:
            self.signal_entry_price = self.entry_fill.execution_price
        validate_price(self.signal_entry_price, "signal_entry_price")

    @property
    def symbol(self) -> str:
        return self.entry_fill.symbol

    @property
    def shares(self) -> int:
        return self.current_shares

    @property
    def entry_price(self) -> float:
        return self.entry_fill.execution_price

    @property
    def split_adjusted_entry_price(self) -> float:
        """Return the raw cost basis per currently held share."""

        return self.entry_price / self.cumulative_split_ratio

    def apply_split(self, ratio: float) -> None:
        """Adjust shares and per-share cost basis without moving cash."""

        if isinstance(ratio, bool) or not np.isfinite(ratio) or ratio <= 0.0:
            raise ValueError("split ratio must be finite and greater than zero")
        adjusted_shares = self.current_shares * float(ratio)
        rounded_shares = round(adjusted_shares)
        if not np.isclose(adjusted_shares, rounded_shares, rtol=0.0, atol=1e-9):
            raise UnsupportedCorporateActionError(
                "split creates fractional shares; cash-in-lieu is not modeled"
            )
        self.current_shares = int(rounded_shares)
        self.cumulative_split_ratio *= float(ratio)


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

    def apply_fill(
        self, fill: Fill, *, signal_entry_price: float | None = None
    ) -> Trade | None:
        """Apply a simulated fill and return a trade when a position closes."""

        if not isinstance(fill, Fill):
            raise TypeError("fill must be a Fill")
        if fill.side is OrderSide.BUY:
            self._open_position(fill, signal_entry_price)
            return None
        return self._close_position(fill)

    def apply_split(self, ratio: float) -> None:
        """Apply an effective-date split to an existing long position."""

        if isinstance(ratio, bool) or not np.isfinite(ratio) or ratio <= 0.0:
            raise ValueError("split ratio must be finite and greater than zero")
        if self.position is not None and not np.isclose(ratio, 1.0):
            self.position.apply_split(ratio)

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

    def _open_position(
        self, fill: Fill, signal_entry_price: float | None = None
    ) -> None:
        if self.position is not None:
            raise ValueError("cannot open a second position in a Phase 1 portfolio")
        total_cost = fill.notional + fill.commission
        if total_cost > self.cash + 1e-9:
            raise ValueError("insufficient cash for BUY fill and commission")
        self.cash -= total_cost
        self.position = Position(
            entry_fill=fill,
            signal_entry_price=signal_entry_price,
        )

    def _close_position(self, fill: Fill) -> Trade:
        if self.position is None:
            raise ValueError("cannot SELL without an open long position")
        if fill.symbol != self.position.symbol:
            raise ValueError("SELL symbol does not match the open position")
        if fill.shares != self.position.shares:
            raise ValueError("partial position exits are not supported in Phase 1")

        self.cash += fill.notional - fill.commission
        trade = Trade.from_fills(
            self.position.entry_fill,
            fill,
            self.position.holding_days,
            split_adjustment_ratio=self.position.cumulative_split_ratio,
        )
        self.closed_trades.append(trade)
        self.position = None
        return trade
