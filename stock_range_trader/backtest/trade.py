"""Immutable order, fill, and completed-trade records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

import numpy as np
import pandas as pd

from strategy import ExitReason


class OrderSide(str, Enum):
    """Supported order directions for the long-only backtester."""

    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    """Terminal states for Phase 1.1 next-open orders."""

    FILLED = "filled"
    REJECTED = "rejected"
    CANCELED = "canceled"


class OrderReason(str, Enum):
    """Explicit reasons attached to each terminal order state."""

    NONE = "none"
    RISK_LIMIT = "risk_limit"
    INSUFFICIENT_CAPITAL_FOR_LOT = "insufficient_capital_for_lot"
    NON_TRADABLE_BAR = "non_tradable_bar"
    NO_NEXT_BAR = "no_next_bar"


@dataclass(frozen=True, slots=True)
class Order:
    """A simulated order created from an earlier strategy signal."""

    symbol: str
    side: OrderSide
    signal_date: pd.Timestamp
    shares: int
    exit_reason: ExitReason | None = None

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not isinstance(self.signal_date, pd.Timestamp) or pd.isna(self.signal_date):
            raise ValueError("signal_date must be a valid pandas Timestamp")
        if isinstance(self.shares, bool) or not isinstance(self.shares, int):
            raise TypeError("shares must be an integer")
        if self.shares <= 0:
            raise ValueError("shares must be greater than zero")
        if self.side is OrderSide.SELL and self.exit_reason is None:
            raise ValueError("a SELL order must include an exit reason")
        if self.side is OrderSide.BUY and self.exit_reason is not None:
            raise ValueError("a BUY order cannot include an exit reason")


@dataclass(frozen=True, slots=True)
class Fill:
    """The result of executing an order against a simulated market open."""

    symbol: str
    side: OrderSide
    signal_date: pd.Timestamp
    execution_date: pd.Timestamp
    raw_open_price: float
    execution_price: float
    shares: int
    commission: float
    slippage_cost: float
    exit_reason: ExitReason | None = None

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        for name in ("signal_date", "execution_date"):
            value = getattr(self, name)
            if not isinstance(value, pd.Timestamp) or pd.isna(value):
                raise ValueError(f"{name} must be a valid pandas Timestamp")
        if self.execution_date <= self.signal_date:
            raise ValueError("execution_date must be after signal_date")
        validate_price(self.raw_open_price, "raw_open_price")
        validate_price(self.execution_price, "execution_price")
        if isinstance(self.shares, bool) or not isinstance(self.shares, int):
            raise TypeError("shares must be an integer")
        if self.shares <= 0:
            raise ValueError("shares must be greater than zero")
        for name in ("commission", "slippage_cost"):
            value = getattr(self, name)
            if isinstance(value, bool) or not np.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.side is OrderSide.SELL and self.exit_reason is None:
            raise ValueError("a SELL fill must include an exit reason")
        if self.side is OrderSide.BUY and self.exit_reason is not None:
            raise ValueError("a BUY fill cannot include an exit reason")

    @property
    def notional(self) -> float:
        """Return absolute executed transaction value before commission."""

        return self.execution_price * self.shares


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Auditable terminal result for a signal-derived order attempt."""

    symbol: str
    signal_date: pd.Timestamp
    scheduled_execution_date: pd.Timestamp | None
    side: OrderSide
    requested_shares: int | None
    filled_shares: int
    status: OrderStatus
    reason: OrderReason
    raw_open_price: float | None = None
    execution_price: float | None = None
    commission: float | None = None
    slippage_cost: float | None = None

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        if not isinstance(self.signal_date, pd.Timestamp) or pd.isna(self.signal_date):
            raise ValueError("signal_date must be a valid pandas Timestamp")
        if self.scheduled_execution_date is not None:
            if not isinstance(self.scheduled_execution_date, pd.Timestamp) or pd.isna(
                self.scheduled_execution_date
            ):
                raise ValueError(
                    "scheduled_execution_date must be a valid pandas Timestamp"
                )
            if self.scheduled_execution_date <= self.signal_date:
                raise ValueError("scheduled_execution_date must be after signal_date")
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        for name in ("requested_shares", "filled_shares"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if not isinstance(self.status, OrderStatus):
            raise TypeError("status must be an OrderStatus")
        if not isinstance(self.reason, OrderReason):
            raise TypeError("reason must be an OrderReason")

        if self.status is OrderStatus.FILLED:
            if self.reason is not OrderReason.NONE:
                raise ValueError("a filled order must use reason=none")
            if self.scheduled_execution_date is None:
                raise ValueError("a filled order requires an execution date")
            if self.requested_shares != self.filled_shares or self.filled_shares <= 0:
                raise ValueError("a filled order requires equal positive share counts")
            for name in ("raw_open_price", "execution_price"):
                value = getattr(self, name)
                if value is None:
                    raise ValueError(f"a filled order requires {name}")
                validate_price(value, name)
            for name in ("commission", "slippage_cost"):
                value = getattr(self, name)
                if value is None or not np.isfinite(value) or value < 0.0:
                    raise ValueError(
                        f"a filled order requires finite non-negative {name}"
                    )
        else:
            if self.reason is OrderReason.NONE:
                raise ValueError("a non-filled order requires an explicit reason")
            if self.filled_shares != 0:
                raise ValueError("a non-filled order must have zero filled shares")
            if any(
                value is not None
                for value in (
                    self.execution_price,
                    self.commission,
                    self.slippage_cost,
                )
            ):
                raise ValueError("a non-filled order cannot contain fill values")
            if self.raw_open_price is not None:
                validate_price(self.raw_open_price, "raw_open_price")

    @classmethod
    def from_fill(cls, fill: Fill) -> OrderResult:
        """Create a filled order result without duplicating fill arithmetic."""

        if not isinstance(fill, Fill):
            raise TypeError("fill must be a Fill")
        return cls(
            symbol=fill.symbol,
            signal_date=fill.signal_date,
            scheduled_execution_date=fill.execution_date,
            side=fill.side,
            requested_shares=fill.shares,
            filled_shares=fill.shares,
            status=OrderStatus.FILLED,
            reason=OrderReason.NONE,
            raw_open_price=fill.raw_open_price,
            execution_price=fill.execution_price,
            commission=fill.commission,
            slippage_cost=fill.slippage_cost,
        )

    def to_record(self) -> dict[str, object]:
        """Return a flat mapping in the public Order Log schema."""

        record = asdict(self)
        record["side"] = self.side.value
        record["status"] = self.status.value
        record["reason"] = self.reason.value
        return record


@dataclass(frozen=True, slots=True)
class Trade:
    """A completed long trade in the requested CSV-log schema."""

    symbol: str
    entry_signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    entry_price: float
    shares: int
    split_adjustment_ratio: float
    split_adjusted_entry_price: float
    exit_shares: int
    exit_signal_date: pd.Timestamp
    exit_date: pd.Timestamp
    exit_price: float
    exit_reason: ExitReason
    gross_profit: float
    commission: float
    slippage_cost: float
    net_profit: float
    return_pct: float
    holding_days: int

    @classmethod
    def from_fills(
        cls,
        entry: Fill,
        exit: Fill,
        holding_days: int,
    ) -> Trade:
        """Create and validate a completed trade from matching fills."""

        if entry.side is not OrderSide.BUY or exit.side is not OrderSide.SELL:
            raise ValueError("a trade requires a BUY fill followed by a SELL fill")
        if entry.symbol != exit.symbol:
            raise ValueError("entry and exit fills must have matching symbols")
        if entry.shares != exit.shares:
            raise ValueError("entry and exit fills must have matching shares")
        if exit.exit_reason is None:
            raise ValueError("the exit fill must include an exit reason")
        if exit.execution_date <= entry.execution_date:
            raise ValueError("exit execution must occur after entry execution")
        if isinstance(holding_days, bool) or not isinstance(holding_days, int):
            raise TypeError("holding_days must be an integer")
        if holding_days <= 0:
            raise ValueError("holding_days must be greater than zero")

        gross_profit = exit.notional - entry.notional
        commission = entry.commission + exit.commission
        slippage_cost = entry.slippage_cost + exit.slippage_cost
        net_profit = gross_profit - commission
        invested_amount = entry.notional + entry.commission
        return_pct = net_profit / invested_amount
        return cls(
            symbol=entry.symbol,
            entry_signal_date=entry.signal_date,
            entry_date=entry.execution_date,
            entry_price=entry.execution_price,
            shares=entry.shares,
            split_adjustment_ratio=1.0,
            split_adjusted_entry_price=entry.execution_price,
            exit_shares=exit.shares,
            exit_signal_date=exit.signal_date,
            exit_date=exit.execution_date,
            exit_price=exit.execution_price,
            exit_reason=exit.exit_reason,
            gross_profit=gross_profit,
            commission=commission,
            slippage_cost=slippage_cost,
            net_profit=net_profit,
            return_pct=return_pct,
            holding_days=holding_days,
        )

    def to_record(self) -> dict[str, object]:
        """Return a flat mapping suitable for a trade-log DataFrame."""

        record = asdict(self)
        record["exit_reason"] = self.exit_reason.value
        return record


def validate_price(value: float, name: str) -> None:
    """Validate a positive finite price-like value."""

    if isinstance(value, bool) or not np.isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be finite and greater than zero")
