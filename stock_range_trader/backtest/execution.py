"""Simulated order-execution models with no external broker connection."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .trade import Fill, Order, OrderSide, validate_price


class ExecutionModel(ABC):
    """Interface for deterministic, local-only order simulation."""

    @abstractmethod
    def execution_price(self, side: OrderSide, open_price: float) -> float:
        """Return the simulated fill price for a market open."""

    @abstractmethod
    def execute(
        self, order: Order, execution_date: pd.Timestamp, open_price: float
    ) -> Fill:
        """Simulate a fill without sending an order anywhere."""

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

    def execute(
        self, order: Order, execution_date: pd.Timestamp, open_price: float
    ) -> Fill:
        """Return a local simulated fill strictly after the signal date."""

        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if not isinstance(execution_date, pd.Timestamp) or pd.isna(execution_date):
            raise ValueError("execution_date must be a valid pandas Timestamp")
        if execution_date <= order.signal_date:
            raise ValueError("execution_date must be after signal_date")

        price = self.execution_price(order.side, open_price)
        notional = price * order.shares
        return Fill(
            symbol=order.symbol,
            side=order.side,
            signal_date=order.signal_date,
            execution_date=execution_date,
            raw_open_price=float(open_price),
            execution_price=price,
            shares=order.shares,
            commission=notional * self.commission_rate,
            slippage_cost=abs(price - open_price) * order.shares,
            exit_reason=order.exit_reason,
        )
