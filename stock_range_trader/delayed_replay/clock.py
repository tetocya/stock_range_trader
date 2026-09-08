"""Explicit replay clocks: no wall-clock reads or inferred trading calendar."""

from dataclasses import dataclass
from datetime import datetime

from .validation import ReplayContractError, timestamp


@dataclass(frozen=True, slots=True)
class ReplayClock:
    market_decision_at: datetime
    replayed_at: datetime

    def __post_init__(self) -> None:
        timestamp(self.market_decision_at, "market_decision_at")
        timestamp(self.replayed_at, "replayed_at")
        if self.market_decision_at > self.replayed_at:
            raise ReplayContractError("market_decision_at cannot exceed replayed_at")

    def advance(
        self, *, market_decision_at: datetime, replayed_at: datetime
    ) -> "ReplayClock":
        next_clock = ReplayClock(market_decision_at, replayed_at)
        if (
            next_clock.market_decision_at < self.market_decision_at
            or next_clock.replayed_at < self.replayed_at
        ):
            raise ReplayContractError("replay clocks cannot move backwards")
        return next_clock
