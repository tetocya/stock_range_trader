"""Supplied Japanese sessions, not an inferred exchange calendar."""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .replay_policy import fingerprint
from .validation import ReplayContractError, calendar_date, timestamp

JST = ZoneInfo("Asia/Tokyo")


def month_boundary(day: date) -> date:
    calendar_date(day, "day")
    return day.replace(day=1)


def shift_month(boundary: date, months: int) -> date:
    if type(months) is not int or boundary.day != 1:
        raise ReplayContractError("invalid_month_shift")
    year, month = divmod(boundary.year * 12 + boundary.month - 1 + months, 12)
    return date(year, month + 1, 1)


@dataclass(frozen=True, slots=True)
class ReplaySession:
    day: date
    selection_at: datetime
    open_at: datetime
    close_at: datetime
    timezone: str

    def __post_init__(self):
        calendar_date(self.day, "session")
        if self.timezone != "Asia/Tokyo":
            raise ReplayContractError("unsupported_session_timezone")
        for name in ("selection_at", "open_at", "close_at"):
            value = getattr(self, name)
            timestamp(value, name)
            if value.astimezone(JST).date() != self.day:
                raise ReplayContractError("session_timestamp_date_mismatch")
        if not self.selection_at < self.open_at < self.close_at:
            raise ReplayContractError("invalid_session_publication_order")


@dataclass(frozen=True, slots=True)
class ReplayCalendar:
    sessions: tuple[ReplaySession, ...]

    def __post_init__(self):
        if (
            type(self.sessions) is not tuple
            or not self.sessions
            or any(not isinstance(item, ReplaySession) for item in self.sessions)
        ):
            raise ReplayContractError("explicit_sessions_required")
        ordered = tuple(sorted(self.sessions, key=lambda item: item.day))
        if len({item.day for item in ordered}) != len(ordered):
            raise ReplayContractError("duplicate_calendar_session")
        object.__setattr__(self, "sessions", ordered)

    def between(self, start, end):
        return tuple(item for item in self.sessions if start <= item.day < end)

    @property
    def sha256(self):
        return fingerprint(self)
