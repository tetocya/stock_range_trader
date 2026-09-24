"""Offline-only contracts for a future historical-feasibility HTTP acquisition.

No function in this module performs I/O, verifies an owner's identity, or opens
an HTTP connection. The live-acquisition gate is intentionally closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from .acquisition import CALENDAR, DAILY, MASTER, DateQuery

PLAN_SCHEMA = "historical-feasibility-http-plan-v1"
APPROVAL_SCHEMA = "historical-feasibility-owner-approval-v1"
RECEIPT_SCHEMA = "historical-feasibility-external-receipt-v1"
JOURNAL_SCHEMA = "historical-feasibility-journal-v1"
PURPOSE = "historical_feasibility"
AUTH_REFERENCE = "JQUANTS_API_KEY"
HTTP_OUTPUT_ROOT = (
    Path(__file__).resolve().parents[1] / "outputs" / "feasibility" / "http"
)
_SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")


class HttpContractError(ValueError):
    """A future HTTP acquisition contract is invalid or cannot be established."""


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise HttpContractError(f"{name}_must_be_positive_integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise HttpContractError(f"{name}_must_be_nonnegative_integer")
    return value


def _iso_day(value: object, name: str) -> str:
    if type(value) is not str:
        raise HttpContractError(f"{name}_must_be_iso_date")
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except ValueError:
        pass
    raise HttpContractError(f"{name}_must_be_iso_date")


def _utc(value: object, name: str) -> str:
    if type(value) is not datetime or value.tzinfo is None:
        raise HttpContractError(f"{name}_must_be_aware_datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _read_utc(value: object, name: str) -> datetime:
    if type(value) is not str:
        raise HttpContractError(f"{name}_must_be_canonical_utc")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise HttpContractError(f"{name}_must_be_canonical_utc") from None
    if _utc(parsed, name) != value:
        raise HttpContractError(f"{name}_must_be_canonical_utc")
    return parsed


def _hex(value: object, name: str) -> str:
    if type(value) is not str or not _HEX.fullmatch(value):
        raise HttpContractError(f"{name}_must_be_sha256")
    return value


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or len(value) > 256:
        raise HttpContractError(f"{name}_must_be_nonempty_text")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise HttpContractError(f"{name}_contains_control_character")
    return value


def _output_path(value: object, artifact_id: str) -> str:
    if type(value) is not str or not value:
        raise HttpContractError("output_dir_must_be_absolute")
    raw = Path(value)
    if (
        not raw.is_absolute()
        or ".." in raw.parts
        or str(raw) != os.path.normpath(value)
    ):
        raise HttpContractError("output_dir_must_be_absolute_canonical_path")
    expected = HTTP_OUTPUT_ROOT / artifact_id
    if raw != expected:
        raise HttpContractError("output_dir_outside_dedicated_http_root")
    for component in (raw, *raw.parents):
        if component.is_symlink():
            raise HttpContractError("output_dir_contains_symlink")
    return value


@dataclass(frozen=True)
class HttpLimits:
    max_attempts: int
    max_pages_total: int
    max_pages_per_query: int
    max_elapsed_seconds: int
    max_transfer_bytes: int
    max_decoded_bytes: int
    max_saved_bytes: int
    max_page_transfer_bytes: int
    max_page_decoded_bytes: int
    max_page_saved_bytes: int

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            _positive_int(value, name)
        for total, per_page in (
            (self.max_transfer_bytes, self.max_page_transfer_bytes),
            (self.max_decoded_bytes, self.max_page_decoded_bytes),
            (self.max_saved_bytes, self.max_page_saved_bytes),
        ):
            if per_page > total:
                raise HttpContractError("per_page_budget_exceeds_total_budget")


@dataclass(frozen=True)
class RetryRules:
    max_attempts_per_page: int
    min_interval_seconds: int
    min_wait_after_429_seconds: int
    min_wait_after_5xx_seconds: int
    min_wait_after_network_error_seconds: int
    timeout_seconds: int
    retry_after_policy: str = "max_server_and_local_wait"
    stop_policy: str = "stop_without_budget_reset"

    def __post_init__(self) -> None:
        for name in (
            "max_attempts_per_page",
            "min_interval_seconds",
            "min_wait_after_429_seconds",
            "min_wait_after_5xx_seconds",
            "min_wait_after_network_error_seconds",
            "timeout_seconds",
        ):
            _positive_int(getattr(self, name), name)
        if self.min_interval_seconds < 13 or self.min_wait_after_429_seconds < 120:
            raise HttpContractError("free_rate_limit_policy_too_weak")
        if self.retry_after_policy != "max_server_and_local_wait":
            raise HttpContractError("unsupported_retry_after_policy")
        if self.stop_policy != "stop_without_budget_reset":
            raise HttpContractError("unsupported_stop_policy")


@dataclass(frozen=True)
class HttpAcquisitionPlan:
    """Fixed scope; no field is an authorization or a transport switch."""

    artifact_id: str
    kind: str
    reference_date: str
    calendar_start: str
    calendar_end: str
    master_date: str | None
    daily_dates: tuple[str, ...]
    calendar_source_sha256: str | None
    calendar_source_reference: str | None
    output_dir: str
    not_before: datetime
    expires_at: datetime
    limits: HttpLimits
    retry: RetryRules
    auth_reference: str = AUTH_REFERENCE

    def __post_init__(self) -> None:
        if type(self.artifact_id) is not str or not _SAFE_NAME.fullmatch(
            self.artifact_id
        ):
            raise HttpContractError("invalid_artifact_id")
        if self.kind not in (
            "calendar_discovery",
            "predeclared_daily",
            "calendar_anchored_daily",
        ):
            raise HttpContractError("unknown_plan_kind")
        r = _iso_day(self.reference_date, "reference_date")
        start = _iso_day(self.calendar_start, "calendar_start")
        end = _iso_day(self.calendar_end, "calendar_end")
        if not start <= r <= end:
            raise HttpContractError("reference_date_outside_calendar_range")
        if type(self.daily_dates) is not tuple:
            raise HttpContractError("daily_dates_must_be_tuple")
        dates = tuple(_iso_day(day, "daily_date") for day in self.daily_dates)
        if len(set(dates)) != len(dates):
            raise HttpContractError("duplicate_daily_date")
        object.__setattr__(self, "daily_dates", tuple(sorted(dates)))
        if self.kind == "calendar_discovery":
            if (
                dates
                or self.master_date is not None
                or self.calendar_source_sha256
                or self.calendar_source_reference
            ):
                raise HttpContractError("calendar_discovery_must_be_calendar_only")
        else:
            if not dates or r not in dates or any(not start <= d <= r for d in dates):
                raise HttpContractError("daily_dates_outside_fixed_window")
            if self.master_date != r:
                raise HttpContractError("master_date_must_equal_reference_date")
            _hex(self.calendar_source_sha256, "calendar_source_sha256")
            _text(self.calendar_source_reference, "calendar_source_reference")
        _output_path(self.output_dir, self.artifact_id)
        if _utc(self.not_before, "not_before") >= _utc(self.expires_at, "expires_at"):
            raise HttpContractError("plan_time_window_invalid")
        if type(self.limits) is not HttpLimits or type(self.retry) is not RetryRules:
            raise HttpContractError("plan_limits_or_retry_type_invalid")
        if self.auth_reference != AUTH_REFERENCE:
            raise HttpContractError("unsupported_auth_reference")
        if len(self.queries) > self.limits.max_attempts or len(self.queries) > (
            self.limits.max_pages_total
        ):
            raise HttpContractError("budget_below_fixed_query_count")

    @property
    def queries(self) -> tuple[DateQuery, ...]:
        if self.kind == "calendar_discovery":
            return (
                DateQuery(CALENDAR, start=self.calendar_start, end=self.calendar_end),
            )
        queries = ()
        if self.kind == "predeclared_daily":
            queries += (
                DateQuery(CALENDAR, start=self.calendar_start, end=self.calendar_end),
            )
        queries += (DateQuery(MASTER, market_date=self.reference_date),)
        return queries + tuple(
            DateQuery(DAILY, market_date=day) for day in self.daily_dates
        )

    @property
    def allowed_endpoints(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(q.endpoint for q in self.queries))

    def scope(self) -> dict:
        return {
            "kind": self.kind,
            "reference_date": self.reference_date,
            "calendar_start": self.calendar_start,
            "calendar_end": self.calendar_end,
            "master_date": self.master_date,
            "daily_dates": list(self.daily_dates),
            "calendar_source_sha256": self.calendar_source_sha256,
            "calendar_source_reference": self.calendar_source_reference,
            "allowed_endpoints": list(self.allowed_endpoints),
            "queries": [q.to_dict() for q in self.queries],
            "pagination": "sequential_same_query_until_no_pagination_key",
        }

    def to_dict(self) -> dict:
        return {
            "schema": PLAN_SCHEMA,
            "purpose": PURPOSE,
            "provider": "jquants",
            "api_version": "v2",
            "artifact_id": self.artifact_id,
            "scope": self.scope(),
            "output_dir": self.output_dir,
            "not_before": _utc(self.not_before, "not_before"),
            "expires_at": _utc(self.expires_at, "expires_at"),
            "limits": asdict(self.limits),
            "retry": asdict(self.retry),
            "auth_reference": self.auth_reference,
            "resume_policy": "same_plan_same_cumulative_budget_only",
            "expiry_policy": "stop_without_reauthorization",
        }

    @property
    def sha256(self) -> str:
        return _digest(self.to_dict())

    @property
    def scope_sha256(self) -> str:
        return _digest(self.scope())


@dataclass(frozen=True)
class OwnerApprovalClaim:
    """Scope evidence only; this class cannot attest its issuer's identity."""

    plan_sha256: str
    artifact_id: str
    scope_sha256: str
    allowed_endpoints: tuple[str, ...]
    reference_date: str
    calendar_start: str
    calendar_end: str
    master_date: str | None
    daily_dates: tuple[str, ...]
    limits: HttpLimits
    retry: RetryRules
    output_dir: str
    valid_from: datetime
    valid_until: datetime
    approver_id: str
    approval_event_id: str
    evidence_reference: str
    purpose: str = PURPOSE

    def __post_init__(self) -> None:
        _hex(self.plan_sha256, "approval_plan_sha256")
        _hex(self.scope_sha256, "approval_scope_sha256")
        for name in (
            "artifact_id",
            "approver_id",
            "approval_event_id",
            "evidence_reference",
        ):
            _text(getattr(self, name), name)
        for name in ("reference_date", "calendar_start", "calendar_end"):
            _iso_day(getattr(self, name), name)
        if self.master_date is not None:
            _iso_day(self.master_date, "master_date")
        if (
            type(self.daily_dates) is not tuple
            or type(self.allowed_endpoints) is not tuple
        ):
            raise HttpContractError("approval_scope_must_be_immutable_tuples")
        if len(set(self.daily_dates)) != len(self.daily_dates):
            raise HttpContractError("duplicate_approval_daily_date")
        object.__setattr__(self, "daily_dates", tuple(sorted(self.daily_dates)))
        if any(type(day) is not str for day in self.daily_dates):
            raise HttpContractError("approval_daily_date_must_be_text")
        for day in self.daily_dates:
            _iso_day(day, "approval_daily_date")
        if self.purpose != PURPOSE or type(self.limits) is not HttpLimits:
            raise HttpContractError("approval_purpose_or_limits_invalid")
        if type(self.retry) is not RetryRules:
            raise HttpContractError("approval_retry_invalid")
        if _utc(self.valid_from, "valid_from") >= _utc(self.valid_until, "valid_until"):
            raise HttpContractError("approval_time_window_invalid")
        _output_path(self.output_dir, self.artifact_id)

    def to_dict(self) -> dict:
        return {
            "schema": APPROVAL_SCHEMA,
            "purpose": self.purpose,
            "plan_sha256": self.plan_sha256,
            "artifact_id": self.artifact_id,
            "scope_sha256": self.scope_sha256,
            "allowed_endpoints": list(self.allowed_endpoints),
            "reference_date": self.reference_date,
            "calendar_start": self.calendar_start,
            "calendar_end": self.calendar_end,
            "master_date": self.master_date,
            "daily_dates": list(self.daily_dates),
            "limits": asdict(self.limits),
            "retry": asdict(self.retry),
            "output_dir": self.output_dir,
            "valid_from": _utc(self.valid_from, "valid_from"),
            "valid_until": _utc(self.valid_until, "valid_until"),
            "approver_id": self.approver_id,
            "approval_event_id": self.approval_event_id,
            "evidence_reference": self.evidence_reference,
            "authenticity_claim": "unverified_external_authority_not_configured",
        }


@dataclass(frozen=True)
class ApprovalScopeCheck:
    metadata_matches: bool
    authenticity_verified: bool = False


def check_approval_scope(
    plan: HttpAcquisitionPlan, approval: OwnerApprovalClaim, *, now: datetime
) -> ApprovalScopeCheck:
    """Reject all mismatches without asserting that the owner signed a claim."""

    if (
        type(plan) is not HttpAcquisitionPlan
        or type(approval) is not OwnerApprovalClaim
    ):
        raise HttpContractError("approval_claim_required")
    expected = {
        "plan_sha256": plan.sha256,
        "artifact_id": plan.artifact_id,
        "scope_sha256": plan.scope_sha256,
        "allowed_endpoints": plan.allowed_endpoints,
        "reference_date": plan.reference_date,
        "calendar_start": plan.calendar_start,
        "calendar_end": plan.calendar_end,
        "master_date": plan.master_date,
        "daily_dates": plan.daily_dates,
        "limits": plan.limits,
        "retry": plan.retry,
        "output_dir": plan.output_dir,
        "purpose": PURPOSE,
    }
    for name, expected_value in expected.items():
        if getattr(approval, name) != expected_value:
            raise HttpContractError(f"approval_{name}_mismatch")
    current = _utc(now, "now")
    if not (
        _utc(plan.not_before, "not_before")
        <= _utc(approval.valid_from, "valid_from")
        <= current
        < _utc(approval.valid_until, "valid_until")
        <= _utc(plan.expires_at, "expires_at")
    ):
        raise HttpContractError("approval_outside_validity_window")
    return ApprovalScopeCheck(metadata_matches=True)


@dataclass(frozen=True)
class PageRequest:
    query: DateQuery
    page_index: int
    pagination_key: str | None = None

    def __post_init__(self) -> None:
        if type(self.query) is not DateQuery:
            raise HttpContractError("page_query_must_be_date_query")
        _nonnegative_int(self.page_index, "page_index")
        if self.page_index == 0 and self.pagination_key is not None:
            raise HttpContractError("first_page_cannot_have_pagination_key")
        if self.page_index > 0:
            _text(self.pagination_key, "pagination_key")
            if len(self.pagination_key) > 2048:
                raise HttpContractError("pagination_key_too_long")

    @property
    def request_id(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict:
        params = self.query.params()
        if self.pagination_key is not None:
            params["pagination_key"] = self.pagination_key
        return {
            "endpoint": self.query.endpoint,
            "params": params,
            "query_id": self.query.query_id,
            "page_index": self.page_index,
        }


def validate_page_request(plan: HttpAcquisitionPlan, page: PageRequest) -> None:
    if type(plan) is not HttpAcquisitionPlan or type(page) is not PageRequest:
        raise HttpContractError("plan_and_page_request_required")
    if page.query not in plan.queries:
        raise HttpContractError("page_outside_fixed_plan")
    if page.page_index >= plan.limits.max_pages_per_query:
        raise HttpContractError("page_index_exceeds_plan_limit")


def make_attempt_id(
    plan: HttpAcquisitionPlan, page: PageRequest, attempt_number: int
) -> str:
    validate_page_request(plan, page)
    _positive_int(attempt_number, "attempt_number")
    return _digest(
        {
            "plan_sha256": plan.sha256,
            "page_request_id": page.request_id,
            "attempt_number": attempt_number,
        }
    )


_EVENT_KINDS = frozenset(
    {
        "attempt_reserved",
        "attempt_sent",
        "response_received",
        "outcome_unknown",
        "body_saved",
        "page_completed",
        "run_completed",
        "run_stopped",
    }
)


@dataclass(frozen=True)
class JournalEvent:
    """One typed audit event; every optional field is serialized explicitly."""

    kind: str
    at: datetime
    attempt_id: str | None = None
    page: PageRequest | None = None
    attempt_number: int | None = None
    status: int | None = None
    transfer_bytes: int | None = None
    decoded_bytes: int | None = None
    body_sha256: str | None = None
    saved_bytes: int | None = None
    next_key: str | None = None
    reason: str | None = None
    terminal: bool | None = None

    def __post_init__(self) -> None:
        if self.kind not in _EVENT_KINDS:
            raise HttpContractError("unknown_journal_event")
        _utc(self.at, "event_at")
        if self.attempt_id is not None:
            _hex(self.attempt_id, "attempt_id")
        if self.body_sha256 is not None:
            _hex(self.body_sha256, "body_sha256")
        if self.page is not None and type(self.page) is not PageRequest:
            raise HttpContractError("event_page_type_invalid")
        if self.attempt_number is not None:
            _positive_int(self.attempt_number, "attempt_number")
        if self.status is not None and (
            type(self.status) is not int or not 100 <= self.status <= 599
        ):
            raise HttpContractError("invalid_http_status")
        for name in ("transfer_bytes", "decoded_bytes", "saved_bytes"):
            if getattr(self, name) is not None:
                _nonnegative_int(getattr(self, name), name)
        if self.next_key is not None:
            _text(self.next_key, "next_key")
        if self.reason is not None:
            if type(self.reason) is not str or not re.fullmatch(
                r"[a-z][a-z0-9_]{0,63}", self.reason
            ):
                raise HttpContractError("reason_must_be_safe_code")
        if self.terminal is not None and type(self.terminal) is not bool:
            raise HttpContractError("terminal_must_be_bool")
        fields = {
            "attempt_id": self.attempt_id,
            "page": self.page,
            "attempt_number": self.attempt_number,
            "status": self.status,
            "transfer_bytes": self.transfer_bytes,
            "decoded_bytes": self.decoded_bytes,
            "body_sha256": self.body_sha256,
            "saved_bytes": self.saved_bytes,
            "next_key": self.next_key,
            "reason": self.reason,
            "terminal": self.terminal,
        }
        required = {
            "attempt_reserved": {"attempt_id", "page", "attempt_number"},
            "attempt_sent": {"attempt_id"},
            "response_received": {
                "attempt_id",
                "status",
                "transfer_bytes",
                "decoded_bytes",
            },
            "outcome_unknown": {"attempt_id"},
            "body_saved": {"attempt_id", "body_sha256", "saved_bytes"},
            "page_completed": {"attempt_id"},
            "run_completed": set(),
            "run_stopped": {"reason", "terminal"},
        }[self.kind]
        if any(fields[name] is None for name in required):
            raise HttpContractError("journal_event_missing_required_field")
        if any(
            value is not None for name, value in fields.items() if name not in required
        ):
            # A final page has next_key=None; a continuing page may carry one.
            if self.kind != "page_completed" or any(
                value is not None
                for name, value in fields.items()
                if name not in required | {"next_key"}
            ):
                raise HttpContractError("journal_event_has_forbidden_field")
        if self.kind == "body_saved" and self.saved_bytes == 0:
            raise HttpContractError("saved_body_must_be_nonempty")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "at": _utc(self.at, "event_at"),
            "attempt_id": self.attempt_id,
            "page": self.page.to_dict() if self.page is not None else None,
            "attempt_number": self.attempt_number,
            "status": self.status,
            "transfer_bytes": self.transfer_bytes,
            "decoded_bytes": self.decoded_bytes,
            "body_sha256": self.body_sha256,
            "saved_bytes": self.saved_bytes,
            "next_key": self.next_key,
            "reason": self.reason,
            "terminal": self.terminal,
        }

    @classmethod
    def from_dict(cls, value: object) -> JournalEvent:
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise HttpContractError("journal_event_schema_invalid")
        raw = dict(value)
        raw["at"] = _read_utc(raw["at"], "event_at")
        page = raw["page"]
        if page is not None:
            if type(page) is not dict or set(page) != {
                "endpoint",
                "params",
                "query_id",
                "page_index",
            }:
                raise HttpContractError("journal_page_schema_invalid")
            params = page["params"]
            if type(params) is not dict:
                raise HttpContractError("journal_page_params_invalid")
            endpoint = page["endpoint"]
            try:
                if endpoint == CALENDAR and set(params) in (
                    {"from", "to"},
                    {"from", "to", "pagination_key"},
                ):
                    query = DateQuery(CALENDAR, start=params["from"], end=params["to"])
                elif endpoint in (MASTER, DAILY) and set(params) in (
                    {"date"},
                    {"date", "pagination_key"},
                ):
                    query = DateQuery(endpoint, market_date=params["date"])
                else:
                    raise HttpContractError("journal_page_params_invalid")
                raw["page"] = PageRequest(
                    query=query,
                    page_index=page["page_index"],
                    pagination_key=params.get("pagination_key"),
                )
            except ValueError:
                raise HttpContractError("journal_page_invalid") from None
            if raw["page"].to_dict() != page:
                raise HttpContractError("journal_page_identity_mismatch")
        return cls(**raw)


class EvidenceJournal:
    """Pure, immutable-by-value hash-chain contract; not a file writer."""

    def __init__(self, plan: HttpAcquisitionPlan, data: bytes = b"") -> None:
        if type(plan) is not HttpAcquisitionPlan or type(data) is not bytes:
            raise HttpContractError("journal_requires_plan_and_bytes")
        self.plan = plan
        self.data = data
        self.events: tuple[JournalEvent, ...] = ()
        self.head_hash = "0" * 64
        if data and not data.endswith(b"\n"):
            raise HttpContractError("journal_incomplete_final_line")
        parsed = []
        for sequence, line in enumerate(data.split(b"\n")[:-1]):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise HttpContractError("journal_invalid_json") from None
            if type(record) is not dict or set(record) != {
                "schema",
                "sequence",
                "previous_hash",
                "plan_sha256",
                "event",
                "event_id",
            }:
                raise HttpContractError("journal_record_schema_invalid")
            try:
                canonical_line = _canonical(record).encode("utf-8")
            except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
                raise HttpContractError("journal_record_noncanonical") from None
            if line != canonical_line:
                raise HttpContractError("journal_record_noncanonical")
            content = {k: v for k, v in record.items() if k != "event_id"}
            if (
                record["schema"] != JOURNAL_SCHEMA
                or type(record["sequence"]) is not int
                or record["sequence"] != sequence
                or record["previous_hash"] != self.head_hash
                or record["plan_sha256"] != plan.sha256
                or record["event_id"] != _digest(content)
            ):
                raise HttpContractError("journal_chain_or_plan_mismatch")
            parsed.append(JournalEvent.from_dict(record["event"]))
            self.head_hash = record["event_id"]
        self.events = tuple(parsed)
        self._validate_transitions()

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def byte_count(self) -> int:
        return len(self.data)

    def append(self, event: JournalEvent) -> EvidenceJournal:
        if type(event) is not JournalEvent:
            raise HttpContractError("journal_event_required")
        content = {
            "schema": JOURNAL_SCHEMA,
            "sequence": self.event_count,
            "previous_hash": self.head_hash,
            "plan_sha256": self.plan.sha256,
            "event": event.to_dict(),
        }
        record = {**content, "event_id": _digest(content)}
        return EvidenceJournal(
            self.plan, self.data + (_canonical(record) + "\n").encode()
        )

    def _validate_transitions(self) -> None:
        attempts: dict[str, dict] = {}
        page_attempts: dict[str, int] = {}
        pages: dict[str, dict[int, tuple[PageRequest, str | None]]] = {}
        first: datetime | None = None
        previous: datetime | None = None
        completed = False
        terminal_stop = False
        transfer_bytes = 0
        decoded_bytes = 0
        saved_bodies: dict[str, int] = {}
        unknown_count = 0
        for event in self.events:
            at = event.at.astimezone(UTC)
            if previous is not None and at < previous:
                raise HttpContractError("journal_clock_regressed")
            previous = at
            if completed or terminal_stop:
                raise HttpContractError("journal_event_after_terminal_state")
            if event.kind == "attempt_reserved":
                page = event.page
                validate_page_request(self.plan, page)
                if any(a.get("over_budget") for a in attempts.values()):
                    raise HttpContractError("previous_page_budget_overrun")
                if any(
                    a["state"] in ("reserved", "sent", "responded", "body_saved")
                    for a in attempts.values()
                ):
                    raise HttpContractError("prior_attempt_unsettled")
                earlier_queries = self.plan.queries[
                    : self.plan.queries.index(page.query)
                ]
                if any(
                    not pages.get(q.query_id)
                    or pages[q.query_id][max(pages[q.query_id])][1] is not None
                    for q in earlier_queries
                ):
                    raise HttpContractError("prior_query_incomplete")
                if sum(map(len, pages.values())) >= self.plan.limits.max_pages_total:
                    raise HttpContractError("page_budget_exhausted")
                if (
                    transfer_bytes
                    + unknown_count * self.plan.limits.max_page_transfer_bytes
                    >= self.plan.limits.max_transfer_bytes
                ):
                    raise HttpContractError("transfer_budget_exhausted")
                if (
                    decoded_bytes
                    + unknown_count * self.plan.limits.max_page_decoded_bytes
                    >= self.plan.limits.max_decoded_bytes
                ):
                    raise HttpContractError("decoded_budget_exhausted")
                if sum(saved_bodies.values()) >= self.plan.limits.max_saved_bytes:
                    raise HttpContractError("saved_budget_exhausted")
                if first is None:
                    first = at
                elif (
                    at - first
                ).total_seconds() >= self.plan.limits.max_elapsed_seconds:
                    raise HttpContractError("attempt_after_cumulative_deadline")
                if not self.plan.not_before <= at < self.plan.expires_at:
                    raise HttpContractError("attempt_outside_plan_validity")
                if len(attempts) >= self.plan.limits.max_attempts:
                    raise HttpContractError("attempt_budget_exhausted")
                if event.attempt_id in attempts or event.attempt_id != make_attempt_id(
                    self.plan, page, event.attempt_number
                ):
                    raise HttpContractError("attempt_identity_invalid")
                prior_pages = pages.get(page.query.query_id, {})
                if page.page_index in prior_pages:
                    raise HttpContractError("completed_page_retried")
                if page.page_index:
                    previous_page = prior_pages.get(page.page_index - 1)
                    if previous_page is None or previous_page[1] != page.pagination_key:
                        raise HttpContractError("pagination_chain_invalid")
                request_id = page.request_id
                count = page_attempts.get(request_id, 0)
                if event.attempt_number != count + 1:
                    raise HttpContractError("page_attempt_number_invalid")
                if count >= self.plan.retry.max_attempts_per_page:
                    raise HttpContractError("page_retry_budget_exhausted")
                if count:
                    previous_id = make_attempt_id(self.plan, page, count)
                    previous_state = attempts[previous_id]["state"]
                    if previous_state not in ("unknown", "failed"):
                        raise HttpContractError("previous_attempt_not_retryable")
                page_attempts[request_id] = count + 1
                attempts[event.attempt_id] = {"page": page, "state": "reserved"}
            elif event.kind in (
                "attempt_sent",
                "response_received",
                "outcome_unknown",
                "body_saved",
                "page_completed",
            ):
                attempt = attempts.get(event.attempt_id)
                if attempt is None:
                    raise HttpContractError("event_for_unknown_attempt")
                state = attempt["state"]
                if event.kind == "attempt_sent":
                    if state != "reserved":
                        raise HttpContractError("attempt_send_order_invalid")
                    attempt["state"] = "sent"
                elif event.kind == "response_received":
                    if state != "sent":
                        raise HttpContractError("response_order_invalid")
                    if (
                        event.transfer_bytes > self.plan.limits.max_page_transfer_bytes
                        or event.decoded_bytes > self.plan.limits.max_page_decoded_bytes
                    ):
                        # Evidence can record an overrun; no subsequent send is allowed.
                        attempt["over_budget"] = True
                    transfer_bytes += event.transfer_bytes
                    decoded_bytes += event.decoded_bytes
                    if (
                        transfer_bytes
                        + unknown_count * self.plan.limits.max_page_transfer_bytes
                        > self.plan.limits.max_transfer_bytes
                        or decoded_bytes
                        + unknown_count * self.plan.limits.max_page_decoded_bytes
                        > self.plan.limits.max_decoded_bytes
                    ):
                        attempt["over_budget"] = True
                    attempt["status"] = event.status
                    attempt["state"] = (
                        "responded"
                        if event.status == 200
                        else "failed"
                        if event.status == 429 or 500 <= event.status < 600
                        else "nonretryable"
                    )
                elif event.kind == "outcome_unknown":
                    if state not in ("reserved", "sent"):
                        raise HttpContractError("unknown_outcome_order_invalid")
                    attempt["state"] = "unknown"
                    unknown_count += 1
                elif event.kind == "body_saved":
                    if state != "responded" or attempt["status"] != 200:
                        raise HttpContractError("body_save_order_invalid")
                    if event.saved_bytes > self.plan.limits.max_page_saved_bytes:
                        attempt["over_budget"] = True
                    attempt["state"] = "body_saved"
                    attempt["body_sha256"] = event.body_sha256
                    attempt["saved_bytes"] = event.saved_bytes
                    previous_size = saved_bodies.get(event.body_sha256)
                    if previous_size is not None and previous_size != event.saved_bytes:
                        raise HttpContractError("body_hash_size_mismatch")
                    saved_bodies[event.body_sha256] = event.saved_bytes
                    if sum(saved_bodies.values()) > self.plan.limits.max_saved_bytes:
                        attempt["over_budget"] = True
                else:
                    if state != "body_saved":
                        raise HttpContractError("page_completion_order_invalid")
                    page = attempt["page"]
                    query_pages = pages.setdefault(page.query.query_id, {})
                    if page.page_index in query_pages:
                        raise HttpContractError("duplicate_completed_page")
                    if event.next_key is not None and event.next_key in {
                        p.pagination_key for p, _ in query_pages.values()
                    } | {page.pagination_key}:
                        raise HttpContractError("pagination_key_loop")
                    query_pages[page.page_index] = (page, event.next_key)
                    if sum(map(len, pages.values())) > self.plan.limits.max_pages_total:
                        raise HttpContractError("page_budget_exhausted")
                    attempt["state"] = "page_completed"
            elif event.kind == "run_completed":
                if (
                    first is not None
                    and (at - first).total_seconds()
                    >= self.plan.limits.max_elapsed_seconds
                ):
                    raise HttpContractError("run_completed_after_cumulative_deadline")
                for query in self.plan.queries:
                    query_pages = pages.get(query.query_id, {})
                    if not query_pages or query_pages[max(query_pages)][1] is not None:
                        raise HttpContractError("run_completed_with_missing_page")
                if any(a.get("over_budget") for a in attempts.values()):
                    raise HttpContractError("run_completed_after_budget_overrun")
                if any(
                    a["state"] in ("reserved", "sent", "responded", "body_saved")
                    for a in attempts.values()
                ):
                    raise HttpContractError("run_completed_with_unsettled_attempt")
                completed = True
            else:
                terminal_stop = bool(event.terminal)

    @property
    def completed(self) -> bool:
        return bool(self.events and self.events[-1].kind == "run_completed")


@dataclass(frozen=True)
class BodyFileEvidence:
    """A *declared* disk footprint; actual file verification belongs to the next PR."""

    object_id: str
    size: int
    state: str
    body_sha256: str | None = None

    def __post_init__(self) -> None:
        _text(self.object_id, "body_object_id")
        _nonnegative_int(self.size, "body_file_size")
        if self.state not in ("committed", "orphan", "partial"):
            raise HttpContractError("unknown_body_file_state")
        if self.state == "partial":
            if self.body_sha256 is not None:
                raise HttpContractError("partial_body_cannot_claim_final_hash")
        else:
            _hex(self.body_sha256, "body_sha256")
            if self.size == 0:
                raise HttpContractError("complete_body_must_be_nonempty")


@dataclass(frozen=True)
class BodyInventory:
    files: tuple[BodyFileEvidence, ...]

    def __post_init__(self) -> None:
        if type(self.files) is not tuple or any(
            type(item) is not BodyFileEvidence for item in self.files
        ):
            raise HttpContractError("body_inventory_must_be_tuple_of_evidence")
        if len({item.object_id for item in self.files}) != len(self.files):
            raise HttpContractError("duplicate_body_object_id")
        digests = [item.body_sha256 for item in self.files if item.body_sha256]
        if len(set(digests)) != len(digests):
            raise HttpContractError("duplicate_stored_body")

    @property
    def saved_bytes(self) -> int:
        return sum(item.size for item in self.files)

    @property
    def body_set_sha256(self) -> str:
        return _digest(
            sorted(
                (
                    {"sha256": item.body_sha256, "bytes": item.size}
                    for item in self.files
                    if item.body_sha256
                ),
                key=lambda item: item["sha256"],
            )
        )


def _check_body_links(journal: EvidenceJournal, inventory: BodyInventory) -> None:
    recorded: dict[str, tuple[int, bool]] = {}
    completed_attempts = {
        e.attempt_id for e in journal.events if e.kind == "page_completed"
    }
    for event in journal.events:
        if event.kind == "body_saved":
            old = recorded.get(event.body_sha256)
            new = (event.saved_bytes, event.attempt_id in completed_attempts)
            if old is not None and old[0] != new[0]:
                raise HttpContractError("body_hash_size_mismatch")
            recorded[event.body_sha256] = (
                event.saved_bytes,
                new[1] or (old[1] if old else False),
            )
    inventory_by_sha = {
        item.body_sha256: item for item in inventory.files if item.body_sha256
    }
    for digest, (size, completed) in recorded.items():
        item = inventory_by_sha.get(digest)
        if item is None or item.size != size:
            raise HttpContractError("saved_body_missing_or_size_mismatch")
        if completed and item.state != "committed":
            raise HttpContractError("completed_page_body_not_committed")
    for item in inventory.files:
        if item.state == "committed" and item.body_sha256 not in recorded:
            raise HttpContractError("unrecorded_committed_body")


@dataclass(frozen=True)
class BudgetSnapshot:
    reserved_attempts: int
    sent_attempts: int
    received_responses: int
    completed_pages: int
    retry_attempts: int
    http_429_responses: int
    unknown_outcomes: int
    unsettled_attempts: int
    orphan_files: int
    partial_files: int
    elapsed_seconds: float
    remaining_attempts: int
    remaining_pages: int
    remaining_seconds: float
    remaining_transfer_bytes: int
    remaining_decoded_bytes: int
    remaining_saved_bytes: int
    can_reserve_next_attempt: bool
    status: str


def budget_snapshot(
    journal: EvidenceJournal, inventory: BodyInventory, *, now: datetime
) -> BudgetSnapshot:
    """Reconstruct counters from an existing journal; resume never starts at zero."""

    if type(journal) is not EvidenceJournal or type(inventory) is not BodyInventory:
        raise HttpContractError("journal_and_body_inventory_required")
    _check_body_links(journal, inventory)
    current = datetime.fromisoformat(_utc(now, "now"))
    if journal.events and current < journal.events[-1].at.astimezone(UTC):
        raise HttpContractError("resume_clock_regressed")
    events = journal.events
    reserved = [e for e in events if e.kind == "attempt_reserved"]
    sent = [e for e in events if e.kind == "attempt_sent"]
    responses = [e for e in events if e.kind == "response_received"]
    completed = [e for e in events if e.kind == "page_completed"]
    unknown = [e for e in events if e.kind == "outcome_unknown"]
    last_by_attempt = {e.attempt_id: e for e in events if e.attempt_id is not None}
    unsettled = sum(
        event.kind in ("attempt_reserved", "attempt_sent", "body_saved")
        or (event.kind == "response_received" and event.status == 200)
        for event in last_by_attempt.values()
    )
    first = reserved[0].at.astimezone(UTC) if reserved else None
    elapsed = max(0.0, (current - first).total_seconds()) if first else 0.0
    limits = journal.plan.limits
    remaining_attempts = max(0, limits.max_attempts - len(reserved))
    remaining_pages = max(0, limits.max_pages_total - len(completed))
    remaining_seconds = max(0.0, limits.max_elapsed_seconds - elapsed)
    # A lost reply may have consumed a full page. Reserve that worst-case amount
    # until the next PR can reconcile the actual transport/file evidence.
    remaining_transfer = max(
        0,
        limits.max_transfer_bytes
        - sum(e.transfer_bytes for e in responses)
        - len(unknown) * limits.max_page_transfer_bytes,
    )
    remaining_decoded = max(
        0,
        limits.max_decoded_bytes
        - sum(e.decoded_bytes for e in responses)
        - len(unknown) * limits.max_page_decoded_bytes,
    )
    remaining_saved = max(0, limits.max_saved_bytes - inventory.saved_bytes)
    status = (
        "completed"
        if journal.completed
        else "terminal_stop"
        if events and events[-1].kind == "run_stopped" and events[-1].terminal
        else "open"
    )
    can_reserve = (
        status == "open"
        and current >= journal.plan.not_before
        and current < journal.plan.expires_at
        and unsettled == 0
        and not any(item.state in ("orphan", "partial") for item in inventory.files)
        and all(
            number > 0
            for number in (
                remaining_attempts,
                remaining_pages,
                remaining_seconds,
                remaining_transfer,
                remaining_decoded,
                remaining_saved,
            )
        )
    )
    return BudgetSnapshot(
        reserved_attempts=len(reserved),
        sent_attempts=len(sent),
        received_responses=len(responses),
        completed_pages=len(completed),
        retry_attempts=sum(e.attempt_number > 1 for e in reserved),
        http_429_responses=sum(e.status == 429 for e in responses),
        unknown_outcomes=len(unknown),
        unsettled_attempts=unsettled,
        orphan_files=sum(e.state == "orphan" for e in inventory.files),
        partial_files=sum(e.state == "partial" for e in inventory.files),
        elapsed_seconds=elapsed,
        remaining_attempts=remaining_attempts,
        remaining_pages=remaining_pages,
        remaining_seconds=remaining_seconds,
        remaining_transfer_bytes=remaining_transfer,
        remaining_decoded_bytes=remaining_decoded,
        remaining_saved_bytes=remaining_saved,
        can_reserve_next_attempt=can_reserve,
        status=status,
    )


@dataclass(frozen=True)
class ExternalReceiptClaim:
    """A receipt's *content*; its independent custody is not proven here."""

    artifact_id: str
    plan_sha256: str
    ledger_event_count: int
    ledger_bytes: int
    ledger_head_sha256: str
    body_set_sha256: str
    fixed_at: datetime
    issuer_id: str
    receipt_id: str
    external_reference: str

    def __post_init__(self) -> None:
        for name in ("artifact_id", "issuer_id", "receipt_id", "external_reference"):
            _text(getattr(self, name), name)
        for name in ("plan_sha256", "ledger_head_sha256", "body_set_sha256"):
            _hex(getattr(self, name), name)
        for name in ("ledger_event_count", "ledger_bytes"):
            _nonnegative_int(getattr(self, name), name)
        _utc(self.fixed_at, "fixed_at")

    def to_dict(self) -> dict:
        return {
            "schema": RECEIPT_SCHEMA,
            "artifact_id": self.artifact_id,
            "plan_sha256": self.plan_sha256,
            "ledger_event_count": self.ledger_event_count,
            "ledger_bytes": self.ledger_bytes,
            "ledger_head_sha256": self.ledger_head_sha256,
            "body_set_sha256": self.body_set_sha256,
            "fixed_at": _utc(self.fixed_at, "fixed_at"),
            "issuer_id": self.issuer_id,
            "receipt_id": self.receipt_id,
            "external_reference": self.external_reference,
            "independent_custody_verified": False,
        }


@dataclass(frozen=True)
class ReceiptAlignment:
    content_matches: bool
    independent_custody_verified: bool = False


def check_receipt_alignment(
    journal: EvidenceJournal,
    inventory: BodyInventory,
    receipt: ExternalReceiptClaim,
) -> ReceiptAlignment:
    if type(receipt) is not ExternalReceiptClaim:
        raise HttpContractError("external_receipt_claim_required")
    _check_body_links(journal, inventory)
    expected = {
        "artifact_id": journal.plan.artifact_id,
        "plan_sha256": journal.plan.sha256,
        "ledger_event_count": journal.event_count,
        "ledger_bytes": journal.byte_count,
        "ledger_head_sha256": journal.head_hash,
        "body_set_sha256": inventory.body_set_sha256,
    }
    for name, value in expected.items():
        if getattr(receipt, name) != value:
            raise HttpContractError(f"receipt_{name}_mismatch")
    if journal.events and receipt.fixed_at < journal.events[-1].at:
        raise HttpContractError("receipt_fixed_before_ledger_head")
    return ReceiptAlignment(content_matches=True)


@dataclass(frozen=True)
class LiveAcquisitionGate:
    permitted: bool
    reasons: tuple[str, ...]


def assess_live_acquisition_gate(
    plan: HttpAcquisitionPlan,
    approval: OwnerApprovalClaim | None,
    journal: EvidenceJournal,
    inventory: BodyInventory,
    receipt: ExternalReceiptClaim | None,
    *,
    now: datetime,
) -> LiveAcquisitionGate:
    """Never authorizes I/O: trusted approval/anchor and HTTP entry do not exist."""

    reasons = []
    if approval is None:
        reasons.append("owner_approval_missing")
    else:
        try:
            check_approval_scope(plan, approval, now=now)
        except HttpContractError:
            reasons.append("owner_approval_scope_or_validity_invalid")
    if receipt is None:
        reasons.append("external_receipt_missing")
    else:
        try:
            check_receipt_alignment(journal, inventory, receipt)
        except HttpContractError:
            reasons.append("external_receipt_content_invalid")
    if not budget_snapshot(journal, inventory, now=now).can_reserve_next_attempt:
        reasons.append("cumulative_budget_unavailable")
    reasons.extend(
        (
            "owner_approval_authenticity_unverified",
            "independent_receipt_custody_unconfigured",
            "http_transport_not_implemented",
        )
    )
    return LiveAcquisitionGate(permitted=False, reasons=tuple(reasons))
