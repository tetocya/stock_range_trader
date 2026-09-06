"""Pure calendar windows for explicitly opted-in Phase 3 live validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from walkforward import WalkForwardFold


@dataclass(frozen=True, slots=True)
class LiveValidationWindow:
    """One 6/3/3-month half-open window ending at a completed month boundary."""

    requested_start: date
    requested_end_exclusive: date
    fold: WalkForwardFold

    def __post_init__(self) -> None:
        for name in ("requested_start", "requested_end_exclusive"):
            _require_date(name, getattr(self, name))
        if not isinstance(self.fold, WalkForwardFold):
            raise TypeError("fold must be WalkForwardFold")
        if self.requested_start != self.fold.train_start:
            raise ValueError("requested_start must equal fold.train_start")
        if self.requested_end_exclusive != self.fold.test_end:
            raise ValueError("requested_end_exclusive must equal fold.test_end")


def yfinance_live_window(today_jst: date) -> LiveValidationWindow:
    """Use the twelve completed months before the current JST month."""

    _require_date("today_jst", today_jst)
    end_exclusive = date(today_jst.year, today_jst.month, 1)
    return _twelve_month_window(end_exclusive, embargo_sessions=20)


def jquants_live_window(today_jst: date) -> LiveValidationWindow:
    """Use twelve months ending no later than the thirteen-week safety boundary."""

    _require_date("today_jst", today_jst)
    safe_date = today_jst - timedelta(weeks=13)
    end_exclusive = date(safe_date.year, safe_date.month, 1)
    return _twelve_month_window(end_exclusive, embargo_sessions=0)


def _twelve_month_window(
    end_exclusive: date,
    *,
    embargo_sessions: int,
) -> LiveValidationWindow:
    requested_start = _shift_month_start(end_exclusive, -12)
    train_end = _shift_month_start(requested_start, 6)
    validation_end = _shift_month_start(train_end, 3)
    fold = WalkForwardFold(
        fold_id="live_fold_0001",
        train_start=requested_start,
        train_end=train_end,
        validation_start=train_end,
        validation_end=validation_end,
        test_start=validation_end,
        test_end=end_exclusive,
        embargo_sessions=embargo_sessions,
    )
    return LiveValidationWindow(requested_start, end_exclusive, fold)


def _shift_month_start(value: date, months: int) -> date:
    _require_date("value", value)
    if value.day != 1:
        raise ValueError("month boundaries must fall on the first day")
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    return date(year, zero_based_month + 1, 1)


def _require_date(name: str, value: object) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be a date")
