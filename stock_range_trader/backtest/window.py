"""Half-open trading windows for isolated backtest evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


class BacktestWindowError(ValueError):
    """Raised when a trading window or its observed range is invalid."""


@dataclass(frozen=True, slots=True)
class BacktestWindow:
    """An immutable half-open ``[trading_start, trading_end)`` interval."""

    trading_start: date
    trading_end: date

    def __post_init__(self) -> None:
        for name in ("trading_start", "trading_end"):
            value = getattr(self, name)
            if not isinstance(value, date) or isinstance(value, datetime):
                raise BacktestWindowError(f"{name} must be a date without a time")
        if self.trading_start >= self.trading_end:
            raise BacktestWindowError("trading_start must be before trading_end")

    def contains(self, value: date) -> bool:
        """Return membership in the half-open trading interval."""

        if not isinstance(value, date) or isinstance(value, datetime):
            raise BacktestWindowError("value must be a date without a time")
        return self.trading_start <= value < self.trading_end
