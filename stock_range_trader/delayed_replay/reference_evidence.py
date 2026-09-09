"""Explicit instrument/period/source evidence. No weekday or lot-size inference."""

from dataclasses import dataclass
from datetime import date, timedelta

from .replay_calendar import ReplayCalendar
from .serialization import JsonObject, require_hash, require_text
from .validation import ReplayContractError, calendar_date


@dataclass(frozen=True, slots=True)
class ReferenceReview:
    source: str
    subject_hash: str
    review_hash: str | None

    def __post_init__(self):
        require_text(self.source)
        require_hash(self.subject_hash)
        if self.review_hash is not None:
            require_hash(self.review_hash)

    def require(self, subject_hash):
        if subject_hash != self.subject_hash or self.review_hash is None:
            raise ReplayContractError("reference_unverified_or_subject_mismatch")


@dataclass(frozen=True, slots=True)
class LotEvidence:
    instrument: str
    effective_start: date
    effective_end: date
    lot_size: int | None
    review: ReferenceReview

    def __post_init__(self):
        require_text(self.instrument)
        interval(self.effective_start, self.effective_end)
        if self.lot_size is not None and (
            type(self.lot_size) is not int or self.lot_size <= 0
        ):
            raise ReplayContractError("invalid_lot_size")
        if not isinstance(self.review, ReferenceReview):
            raise ReplayContractError("reference_review_required")

    @property
    def subject_hash(self):
        return JsonObject.from_value(
            dict(
                instrument=self.instrument,
                start=self.effective_start.isoformat(),
                end=self.effective_end.isoformat(),
                lot_size=self.lot_size,
            )
        ).sha256

    def require(self, instrument, session):
        calendar_date(session, "session")
        self.review.require(self.subject_hash)
        if (
            instrument != self.instrument
            or not self.effective_start <= session < self.effective_end
        ):
            raise ReplayContractError("lot_instrument_or_period_mismatch")
        if self.lot_size != 100:
            raise ReplayContractError("lot_unverified_or_unsupported")
        return 100


def interval(start, end):
    calendar_date(start, "start")
    calendar_date(end, "end")
    if start >= end:
        raise ReplayContractError("positive_reference_period_required")


@dataclass(frozen=True, slots=True)
class CalendarEvidence:
    calendar: ReplayCalendar
    coverage_start: date
    coverage_end: date
    review: ReferenceReview
    market: str

    def __post_init__(self):
        interval(self.coverage_start, self.coverage_end)
        if (
            not isinstance(self.calendar, ReplayCalendar)
            or not isinstance(self.review, ReferenceReview)
            or self.market != "TSE"
        ):
            raise ReplayContractError("calendar_contract")
        if any(
            not self.coverage_start <= s.day < self.coverage_end
            for s in self.calendar.sessions
        ):
            raise ReplayContractError("calendar_outside_coverage")

    @property
    def subject_hash(self):
        return JsonObject.from_value(
            dict(
                calendar_hash=self.calendar.sha256,
                start=self.coverage_start.isoformat(),
                end=self.coverage_end.isoformat(),
                market=self.market,
            )
        ).sha256

    def require(self, start, end):
        interval(start, end)
        self.review.require(self.subject_hash)
        if not self.coverage_start <= start < end <= self.coverage_end:
            raise ReplayContractError("calendar_period_mismatch")
        return self.calendar.between(start, end)


def lot_from_master(row, *, instrument, requested_date, effective_end, source):
    """V2 master has no documented lot field. Never adopt an invented Unit key."""
    calendar_date(requested_date, "requested_date")
    if row.get("Code") != instrument or row.get("Date") != requested_date.isoformat():
        raise ReplayContractError("master_instrument_or_effective_date_mismatch")
    item = LotEvidence(
        instrument,
        requested_date,
        effective_end,
        None,
        ReferenceReview(source, "0" * 64, None),
    )
    from dataclasses import replace

    return replace(item, review=ReferenceReview(source, item.subject_hash, None))


def calendar_from_rows(rows, *, start, end, sessions, review):
    """Explicit complete response plus independently supplied session hours.

    This checks coverage, not a weekday-generated replacement. HolDiv=3 is an
    OSE holiday session, NOT a TSE equity session. Hours are never invented.
    """
    interval(start, end)
    if type(rows) is not tuple or any(
        type(r) is not dict or set(r) != {"Date", "HolDiv"} for r in rows
    ):
        raise ReplayContractError("calendar_response_schema")
    parsed = {}
    for row in rows:
        day = date.fromisoformat(row["Date"])
        if (
            day.isoformat() != row["Date"]
            or day in parsed
            or row["HolDiv"] not in ("0", "1", "2", "3")
        ):
            raise ReplayContractError("calendar_response_duplicate_or_code")
        parsed[day] = row["HolDiv"]
    expected = {start + timedelta(days=i) for i in range((end - start).days)}
    if set(parsed) != expected:
        raise ReplayContractError("calendar_response_incomplete")
    calendar = ReplayCalendar(sessions)
    if {s.day for s in calendar.sessions} != {
        d for d, code in parsed.items() if code in ("1", "2")
    }:
        raise ReplayContractError("calendar_session_evidence_mismatch")
    return CalendarEvidence(calendar, start, end, review, "TSE")
