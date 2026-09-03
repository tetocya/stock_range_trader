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
class Trade:
    """A completed long trade in the requested CSV-log schema."""

    symbol: str
    entry_signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    entry_price: float
    shares: int
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
    def from_fills(cls, entry: Fill, exit: Fill, holding_days: int) -> Trade:
        """Create and validate a completed trade from matching fills."""

        if entry.side is not OrderSide.BUY or exit.side is not OrderSide.SELL:
            raise ValueError("a trade requires a BUY fill followed by a SELL fill")
        if entry.symbol != exit.symbol or entry.shares != exit.shares:
            raise ValueError("entry and exit fills must have matching symbol and shares")
        if exit.exit_reason is None:
            raise ValueError("the exit fill must include an exit reason")
        if exit.execution_date <= entry.execution_date:
            raise ValueError("exit execution must occur after entry execution")
        if isinstance(holding_days, bool) or not isinstance(holding_days, int):
            raise TypeError("holding_days must be an integer")
        if holding_days <= 0:
            raise ValueError("holding_days must be greater than zero")

        gross_profit = (exit.execution_price - entry.execution_price) * entry.shares
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
