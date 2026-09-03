"""Position sizing and portfolio-level trading-risk controls."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class RiskManager:
    """Enforce allocation, lot-size, position-count, and drawdown limits."""

    max_position_pct: float = 0.10
    lot_size: int = 100
    max_positions: int = 1
    max_drawdown_stop: float = 0.20
    _peak_equity: float | None = field(init=False, default=None)
    _current_drawdown: float = field(init=False, default=0.0)
    _buying_halted: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_position_pct, bool)
            or not np.isfinite(self.max_position_pct)
            or not 0.0 < self.max_position_pct <= 1.0
        ):
            raise ValueError("max_position_pct must be finite and in (0, 1]")
        for name in ("lot_size", "max_positions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.max_drawdown_stop, bool)
            or not np.isfinite(self.max_drawdown_stop)
            or not 0.0 < self.max_drawdown_stop < 1.0
        ):
            raise ValueError("max_drawdown_stop must be finite and in (0, 1)")

    @property
    def current_drawdown(self) -> float:
        """Return current drawdown as a non-positive decimal return."""

        return self._current_drawdown

    @property
    def buying_halted(self) -> bool:
        """Return whether the drawdown circuit breaker has been triggered."""

        return self._buying_halted

    def reset(self) -> None:
        """Reset path-dependent state before an independent backtest run."""

        self._peak_equity = None
        self._current_drawdown = 0.0
        self._buying_halted = False

    def update_equity(self, equity: float) -> float:
        """Update peak/drawdown state and permanently halt new BUYs on breach."""

        if isinstance(equity, bool) or not np.isfinite(equity) or equity < 0.0:
            raise ValueError("equity must be finite and non-negative")
        if equity == 0.0 and self._peak_equity is None:
            raise ValueError("initial equity must be greater than zero")
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = float(equity)
        self._current_drawdown = equity / self._peak_equity - 1.0
        if self._current_drawdown <= -self.max_drawdown_stop or np.isclose(
            self._current_drawdown,
            -self.max_drawdown_stop,
            rtol=0.0,
            atol=1e-12,
        ):
            self._buying_halted = True
        return self._current_drawdown

    def allows_new_position(self, current_positions: int) -> bool:
        """Return whether portfolio-level controls permit another position."""

        if (
            isinstance(current_positions, bool)
            or not isinstance(current_positions, int)
            or current_positions < 0
        ):
            raise ValueError("current_positions must be a non-negative integer")
        return not self._buying_halted and current_positions < self.max_positions

    def calculate_position_size(
        self,
        portfolio_value: float,
        execution_price: float,
        available_cash: float,
        commission_rate: float = 0.0,
        current_positions: int = 0,
    ) -> int:
        """Return affordable shares rounded down to the configured lot size."""

        _validate_positive_value(portfolio_value, "portfolio_value")
        _validate_positive_value(execution_price, "execution_price")
        if (
            isinstance(available_cash, bool)
            or not np.isfinite(available_cash)
            or available_cash < 0.0
        ):
            raise ValueError("available_cash must be finite and non-negative")
        if (
            isinstance(commission_rate, bool)
            or not np.isfinite(commission_rate)
            or not 0.0 <= commission_rate < 1.0
        ):
            raise ValueError("commission_rate must be finite and in [0, 1)")
        if not self.allows_new_position(current_positions):
            return 0

        allocation = portfolio_value * self.max_position_pct
        position_lots = math.floor(allocation / execution_price / self.lot_size)
        cash_per_share = execution_price * (1.0 + commission_rate)
        affordable_lots = math.floor(available_cash / cash_per_share / self.lot_size)
        return max(0, min(position_lots, affordable_lots) * self.lot_size)


def _validate_positive_value(value: float, name: str) -> None:
    """Require a finite value greater than zero."""

    if isinstance(value, bool) or not np.isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be finite and greater than zero")
