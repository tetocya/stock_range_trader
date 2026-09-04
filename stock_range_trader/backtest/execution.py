"""Simulated order-execution models with no external broker connection."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .trade import Fill, Order, OrderSide, validate_price


@dataclass(frozen=True, slots=True)
class MarketBar:
    """Market data visible to the execution model at one daily open."""

    date: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if not isinstance(self.date, pd.Timestamp) or pd.isna(self.date):
            raise ValueError("date must be a valid pandas Timestamp")
        for name in ("open", "high", "low", "close"):
            validate_price(getattr(self, name), name)
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be at least open, close, and low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be at most open, close, and high")
        if (
            isinstance(self.volume, bool)
            or not np.isfinite(self.volume)
            or self.volume < 0.0
        ):
            raise ValueError("volume must be finite and non-negative")

    @classmethod
    def from_series(cls, row: pd.Series) -> MarketBar:
        """Create a validated bar from an OHLCV DataFrame row."""

        required = {"date", "open", "high", "low", "close", "volume"}
        missing = sorted(required.difference(row.index))
        if missing:
            raise ValueError("Missing MarketBar fields: " + ", ".join(missing))
        return cls(
            date=pd.Timestamp(row["date"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )


class ExecutionModel(ABC):
    """Interface for deterministic, local-only order simulation."""

    @abstractmethod
    def execution_price(self, side: OrderSide, open_price: float) -> float:
        """Return the simulated fill price for a market open."""

    @abstractmethod
    def execute(self, order: Order, market_bar: MarketBar) -> Fill | None:
        """Simulate a fill, or return None for a non-tradable bar."""

    @property
    def position_sizing_commission_rate(self) -> float:
        """Return a linear commission estimate used for position sizing."""

        return 0.0


@dataclass(frozen=True, slots=True)
class MarketOnNextOpen(ExecutionModel):
    """Fill an earlier signal at a later daily open with configured costs."""

    slippage_pct: float = 0.001
    commission_rate: float = 0.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.slippage_pct, bool)
            or not np.isfinite(self.slippage_pct)
            or not 0.0 <= self.slippage_pct < 1.0
        ):
            raise ValueError("slippage_pct must be finite and in [0, 1)")
        if (
            isinstance(self.commission_rate, bool)
            or not np.isfinite(self.commission_rate)
            or not 0.0 <= self.commission_rate < 1.0
        ):
            raise ValueError("commission_rate must be finite and in [0, 1)")

    def execution_price(self, side: OrderSide, open_price: float) -> float:
        """Apply adverse slippage to the supplied market open."""

        if not isinstance(side, OrderSide):
            raise TypeError("side must be an OrderSide")
        validate_price(open_price, "open_price")
        multiplier = (
            1.0 + self.slippage_pct
            if side is OrderSide.BUY
            else 1.0 - self.slippage_pct
        )
        return float(open_price * multiplier)

    @property
    def position_sizing_commission_rate(self) -> float:
        """Expose the Phase 1 linear commission rate to RiskManager."""

        return self.commission_rate

    def execute(self, order: Order, market_bar: MarketBar) -> Fill | None:
        """Fill locally when the scheduled bar has strictly positive volume."""

        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if not isinstance(market_bar, MarketBar):
            raise TypeError("market_bar must be a MarketBar")
        if market_bar.date <= order.signal_date:
            raise ValueError("execution_date must be after signal_date")
        if market_bar.volume == 0.0:
            return None

        price = self.execution_price(order.side, market_bar.open)
        notional = price * order.shares
        return Fill(
            symbol=order.symbol,
            side=order.side,
            signal_date=order.signal_date,
            execution_date=market_bar.date,
            raw_open_price=market_bar.open,
            execution_price=price,
            shares=order.shares,
            commission=notional * self.commission_rate,
            slippage_cost=abs(price - market_bar.open) * order.shares,
            exit_reason=order.exit_reason,
        )
