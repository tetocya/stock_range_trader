"""Base strategy interface and signal-domain types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


class SignalAction(str, Enum):
    """An end-of-day strategy decision, not an executed order."""

    HOLD = "hold"
    BUY = "buy"
    SELL = "sell"


class ExitReason(str, Enum):
    """Reasons a long position can be closed in Phase 1."""

    MEAN_REVERSION = "mean_reversion"
    STOP_LOSS = "stop_loss"
    RANGE_BREAKDOWN = "range_breakdown"
    MAX_HOLDING_PERIOD = "max_holding_period"


@dataclass(frozen=True, slots=True)
class Signal:
    """A decision generated after all data for ``signal_date`` is known."""

    action: SignalAction
    signal_date: pd.Timestamp
    exit_reason: ExitReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, SignalAction):
            raise TypeError("action must be a SignalAction")
        if not isinstance(self.signal_date, pd.Timestamp) or pd.isna(self.signal_date):
            raise ValueError("signal_date must be a valid pandas Timestamp")
        if self.action is SignalAction.SELL and self.exit_reason is None:
            raise ValueError("a SELL signal must include an exit reason")
        if self.action is not SignalAction.SELL and self.exit_reason is not None:
            raise ValueError("only a SELL signal may include an exit reason")


@dataclass(frozen=True, slots=True)
class PositionContext:
    """Minimal portfolio state exposed to a strategy at the daily close."""

    has_position: bool = False
    entry_price: float | None = None
    holding_days: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.has_position, bool):
            raise TypeError("has_position must be bool")
        if isinstance(self.holding_days, bool) or not isinstance(
            self.holding_days, int
        ):
            raise TypeError("holding_days must be an integer")

        if self.has_position:
            if (
                self.entry_price is None
                or not np.isfinite(self.entry_price)
                or self.entry_price <= 0.0
            ):
                raise ValueError("an open position requires a positive entry_price")
            if self.holding_days <= 0:
                raise ValueError("an open position requires holding_days >= 1")
        elif self.entry_price is not None or self.holding_days != 0:
            raise ValueError(
                "a flat position requires entry_price=None and holding_days=0"
            )


class Strategy(ABC):
    """Interface separating close-time decisions from order execution."""

    @abstractmethod
    def prepare(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of market features with strategy conditions added."""

    @abstractmethod
    def generate_signal(self, row: pd.Series, position: PositionContext) -> Signal:
        """Generate a decision using one completed bar and current position."""
