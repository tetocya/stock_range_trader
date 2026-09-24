"""Offline-only contracts for a future historical-feasibility HTTP acquisition.

No function in this module opens a network connection, reads credentials,
writes files or verifies an owner's identity. The only file-system access is an
lstat of the fixed output path's components when a plan or approval is built.
The live-acquisition gate is intentionally closed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn

from .acquisition import CALENDAR, DAILY, HOLIDAY_DIVISIONS, MASTER, DateQuery

PLAN_SCHEMA = "historical-feasibility-http-plan-v1"
APPROVAL_SCHEMA = "historical-feasibility-owner-approval-v1"
RECEIPT_SCHEMA = "historical-feasibility-external-receipt-v1"
JOURNAL_SCHEMA = "historical-feasibility-journal-v2"
BODY_SET_SCHEMA = "historical-feasibility-body-set-v2"
ANCHOR_SCHEMA = "historical-feasibility-calendar-anchor-v1"
PURPOSE = "historical_feasibility"
AUTH_REFERENCE = "JQUANTS_API_KEY"
HTTP_OUTPUT_ROOT = (
    Path(__file__).resolve().parents[1] / "outputs" / "feasibility" / "http"
)
_SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z")
_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
# Contract bounds keep every wait, timeout, lease and budget representable.
MAX_WAIT_SECONDS = 86_400
MAX_ELAPSED_SECONDS = 366 * 86_400
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


def _bounded_seconds(
    value: object, name: str, maximum: int, *, allow_zero: bool = False
) -> int:
    (_nonnegative_int if allow_zero else _positive_int)(value, name)
    if value > maximum:
        raise HttpContractError(f"{name}_out_of_range")
    return value


def _add_seconds(at: datetime, seconds: int) -> datetime:
    try:
        return at + timedelta(seconds=seconds)
    except OverflowError:
        raise HttpContractError("time_not_representable") from None


def _label(value: object, name: str) -> str:
    if type(value) is not str or not _LABEL.fullmatch(value):
        raise HttpContractError(f"{name}_invalid")
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
        _bounded_seconds(
            self.max_elapsed_seconds, "max_elapsed_seconds", MAX_ELAPSED_SECONDS
        )
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
        for name in (
            "min_interval_seconds",
            "min_wait_after_429_seconds",
            "min_wait_after_5xx_seconds",
            "min_wait_after_network_error_seconds",
            "timeout_seconds",
        ):
            _bounded_seconds(getattr(self, name), name, MAX_WAIT_SECONDS)
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
    account_ref: str
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
                or self.calendar_source_sha256 is not None
                or self.calendar_source_reference is not None
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
        try:
            _add_seconds(self.expires_at, MAX_ELAPSED_SECONDS + MAX_WAIT_SECONDS)
        except HttpContractError:
            raise HttpContractError("plan_time_window_unrepresentable") from None
        if type(self.limits) is not HttpLimits or type(self.retry) is not RetryRules:
            raise HttpContractError("plan_limits_or_retry_type_invalid")
        _label(self.account_ref, "account_ref")
        if self.auth_reference != AUTH_REFERENCE:
            raise HttpContractError("unsupported_auth_reference")
        # Everything derived from the fields is computed once and stored as
        # immutable values; nothing the caller holds can change the scope later.
        queries = _derive_queries(self)
        if len(queries) > self.limits.max_attempts or len(queries) > (
            self.limits.max_pages_total
        ):
            raise HttpContractError("budget_below_fixed_query_count")
        for name, value in _derived_scope(self, queries).items():
            object.__setattr__(self, name, value)

    @property
    def queries(self) -> tuple[DateQuery, ...]:
        return self._queries

    @property
    def query_positions(self) -> MappingProxyType:
        """Read-only view; the fixed scope cannot be widened through it."""

        return self._positions

    @property
    def allowed_endpoints(self) -> tuple[str, ...]:
        return self._endpoints

    @property
    def sha256(self) -> str:
        return self._sha256

    @property
    def scope_sha256(self) -> str:
        return self._scope_sha256

    def verify_fixed_scope(self) -> None:
        """Re-derive the scope from the fields and compare with the stored values."""

        for name, expected in _derived_scope(self, _derive_queries(self)).items():
            stored = getattr(self, name, None)
            if name == "_positions":
                same = type(stored) is MappingProxyType and dict(stored) == dict(
                    expected
                )
            else:
                same = stored == expected
            if not same:
                raise HttpContractError("plan_fixed_scope_inconsistent")

    def scope(self) -> dict:
        return _scope_dict(self, self._queries)

    def to_dict(self) -> dict:
        return _plan_dict(self, self._queries)


def _derive_queries(plan: HttpAcquisitionPlan) -> tuple[DateQuery, ...]:
    if plan.kind == "calendar_discovery":
        return (DateQuery(CALENDAR, start=plan.calendar_start, end=plan.calendar_end),)
    queries = ()
    if plan.kind == "predeclared_daily":
        queries += (
            DateQuery(CALENDAR, start=plan.calendar_start, end=plan.calendar_end),
        )
    queries += (DateQuery(MASTER, market_date=plan.reference_date),)
    return queries + tuple(
        DateQuery(DAILY, market_date=day) for day in plan.daily_dates
    )


def _derived_scope(plan: HttpAcquisitionPlan, queries: tuple) -> dict:
    return {
        "_queries": queries,
        "_positions": MappingProxyType({q: i for i, q in enumerate(queries)}),
        "_endpoints": tuple(dict.fromkeys(q.endpoint for q in queries)),
        "_sha256": _digest(_plan_dict(plan, queries)),
        "_scope_sha256": _digest(_scope_dict(plan, queries)),
    }


def _scope_dict(plan: HttpAcquisitionPlan, queries: tuple) -> dict:
    return {
        "kind": plan.kind,
        "reference_date": plan.reference_date,
        "calendar_start": plan.calendar_start,
        "calendar_end": plan.calendar_end,
        "master_date": plan.master_date,
        "daily_dates": list(plan.daily_dates),
        "calendar_source_sha256": plan.calendar_source_sha256,
        "calendar_source_reference": plan.calendar_source_reference,
        "allowed_endpoints": list(dict.fromkeys(q.endpoint for q in queries)),
        "queries": [q.to_dict() for q in queries],
        "pagination": "sequential_same_query_until_no_pagination_key",
    }


def _plan_dict(plan: HttpAcquisitionPlan, queries: tuple) -> dict:
    return {
        "schema": PLAN_SCHEMA,
        "purpose": PURPOSE,
        "provider": "jquants",
        "api_version": "v2",
        "artifact_id": plan.artifact_id,
        "scope": _scope_dict(plan, queries),
        "output_dir": plan.output_dir,
        "not_before": _utc(plan.not_before, "not_before"),
        "expires_at": _utc(plan.expires_at, "expires_at"),
        "limits": asdict(plan.limits),
        "retry": asdict(plan.retry),
        "account_ref": plan.account_ref,
        "auth_reference": plan.auth_reference,
        "resume_policy": "same_plan_same_cumulative_budget_only",
        "expiry_policy": "stop_without_reauthorization",
    }


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
        if any(type(day) is not str for day in self.daily_dates):
            raise HttpContractError("approval_daily_date_must_be_text")
        for day in self.daily_dates:
            _iso_day(day, "approval_daily_date")
        if len(set(self.daily_dates)) != len(self.daily_dates):
            raise HttpContractError("duplicate_approval_daily_date")
        object.__setattr__(self, "daily_dates", tuple(sorted(self.daily_dates)))
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

    @property
    def sha256(self) -> str:
        """Hash of the normalized claim content; never proof of who issued it."""

        return _digest(self.to_dict())


def _check_approval_fields(
    plan: HttpAcquisitionPlan, approval: OwnerApprovalClaim
) -> None:
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


@dataclass(frozen=True)
class ApprovalScopeCheck:
    """Informational result; never an authorization token for any entry point."""

    metadata_matches: bool
    approval_sha256: str
    authenticity_verified: bool = field(default=False, init=False)


def check_approval_scope(
    plan: HttpAcquisitionPlan, approval: OwnerApprovalClaim, *, now: datetime
) -> ApprovalScopeCheck:
    """Reject all mismatches without asserting that the owner signed a claim."""

    if (
        type(plan) is not HttpAcquisitionPlan
        or type(approval) is not OwnerApprovalClaim
    ):
        raise HttpContractError("approval_claim_required")
    plan.verify_fixed_scope()
    _check_approval_fields(plan, approval)
    current = _utc(now, "now")
    if not (
        _utc(plan.not_before, "not_before")
        <= _utc(approval.valid_from, "valid_from")
        <= current
        < _utc(approval.valid_until, "valid_until")
        <= _utc(plan.expires_at, "expires_at")
    ):
        raise HttpContractError("approval_outside_validity_window")
    return ApprovalScopeCheck(metadata_matches=True, approval_sha256=approval.sha256)


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
    if page.query not in plan.query_positions:
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
_REQUIRED_FIELDS = {
    "attempt_reserved": frozenset(
        {
            "attempt_id",
            "page",
            "attempt_number",
            "approval_sha256",
            "allowed_transfer_bytes",
            "allowed_decoded_bytes",
            "allowed_saved_bytes",
        }
    ),
    "attempt_sent": frozenset({"attempt_id"}),
    "response_received": frozenset(
        {"attempt_id", "status", "transfer_bytes", "decoded_bytes"}
    ),
    "outcome_unknown": frozenset({"attempt_id"}),
    "body_saved": frozenset({"attempt_id", "body_sha256", "saved_bytes"}),
    "page_completed": frozenset({"attempt_id"}),
    "run_completed": frozenset(),
    "run_stopped": frozenset({"reason", "terminal"}),
}
# A final page has next_key=None; a server wait hint is optional on a response.
_OPTIONAL_FIELDS = {
    "page_completed": frozenset({"next_key"}),
    "response_received": frozenset({"retry_after_seconds"}),
}


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
    approval_sha256: str | None = None
    allowed_transfer_bytes: int | None = None
    allowed_decoded_bytes: int | None = None
    allowed_saved_bytes: int | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in _EVENT_KINDS:
            raise HttpContractError("unknown_journal_event")
        _utc(self.at, "event_at")
        for name in ("attempt_id", "body_sha256", "approval_sha256"):
            if getattr(self, name) is not None:
                _hex(getattr(self, name), name)
        if self.page is not None and type(self.page) is not PageRequest:
            raise HttpContractError("event_page_type_invalid")
        for name in (
            "attempt_number",
            "allowed_transfer_bytes",
            "allowed_decoded_bytes",
            "allowed_saved_bytes",
        ):
            if getattr(self, name) is not None:
                _positive_int(getattr(self, name), name)
        if self.status is not None and (
            type(self.status) is not int or not 100 <= self.status <= 599
        ):
            raise HttpContractError("invalid_http_status")
        for name in ("transfer_bytes", "decoded_bytes", "saved_bytes"):
            if getattr(self, name) is not None:
                _nonnegative_int(getattr(self, name), name)
        if self.retry_after_seconds is not None:
            _bounded_seconds(
                self.retry_after_seconds,
                "retry_after_seconds",
                MAX_WAIT_SECONDS,
                allow_zero=True,
            )
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
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name not in ("kind", "at")
        }
        required = _REQUIRED_FIELDS[self.kind]
        if any(fields[name] is None for name in required):
            raise HttpContractError("journal_event_missing_required_field")
        allowed = required | _OPTIONAL_FIELDS.get(self.kind, frozenset())
        if any(
            value is not None for name, value in fields.items() if name not in allowed
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
            "approval_sha256": self.approval_sha256,
            "allowed_transfer_bytes": self.allowed_transfer_bytes,
            "allowed_decoded_bytes": self.allowed_decoded_bytes,
            "allowed_saved_bytes": self.allowed_saved_bytes,
            "retry_after_seconds": self.retry_after_seconds,
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


_OPEN_STATES = frozenset({"reserved", "sent", "responded", "body_saved"})
_RETRYABLE_STATES = frozenset({"unknown", "failed"})


@dataclass(frozen=True)
class _Attempt:
    page: PageRequest
    reserved_at: datetime
    allowed: tuple[int, int, int]
    state: str = "reserved"
    sent_at: datetime | None = None
    status: int | None = None
    body_sha256: str | None = None
    settled_at: datetime | None = None  # response observed or outcome declared unknown
    retry_after: int | None = None  # Retry-After observed on the response


class _JournalState:
    """Verified transition state of one journal.

    ``apply`` checks one event and then updates the state. Opening a journal
    applies every line to a fresh state; ``append`` applies only the new event
    to a copy, so both paths enforce exactly the same rules.
    """

    _DICTS = (
        "attempts",
        "page_attempts",
        "pages",
        "saved_bodies",
        "completed_bodies",
        "query_bodies",
    )

    def __init__(
        self, plan: HttpAcquisitionPlan, approval: OwnerApprovalClaim | None
    ) -> None:
        plan.verify_fixed_scope()
        self.plan = plan
        # The verified, immutable scope used by every transition, resume and
        # completion check (never a caller-held structure).
        self.queries = plan.queries
        self.positions = plan.query_positions
        self.approval = approval
        self.approval_sha256 = approval.sha256 if approval is not None else None
        self.attempts: dict[str, _Attempt] = {}
        self.page_attempts: dict[str, tuple[int, str]] = {}
        # query_id -> completed pages in order: (request, next_key, body_sha256)
        self.pages: dict[str, tuple[tuple[PageRequest, str | None, str], ...]] = {}
        self.saved_bodies: dict[str, int] = {}
        self.completed_bodies: dict[str, bool] = {}
        self.query_bodies: dict[tuple[str, str], int] = {}
        self.first_reserved_at: datetime | None = None
        self.last_event_at: datetime | None = None
        self.last_network_at: datetime | None = None
        self.last_outcome: str | None = None
        self.last_retry_after = 0
        self.open_attempt: str | None = None
        self.complete_prefix = 0  # queries 0..n-1 have their final page
        self.over_budget = False
        self.completed = False
        self.terminal_stop = False
        self.transfer_bytes = self.decoded_bytes = self.saved_total = 0
        self.reserved = self.sent = self.responses = self.completed_pages = 0
        self.unknown = self.retries = self.http_429 = 0

    def clone(self) -> _JournalState:
        new = copy.copy(self)
        for name in self._DICTS:
            setattr(new, name, dict(getattr(self, name)))
        return new

    # ----------------------------------------------------------- derived rules

    def allowances(self) -> tuple[int, int, int]:
        """Per-attempt caps: min(page cap, remaining total), each budget separately."""

        lim = self.plan.limits
        return (
            min(
                lim.max_page_transfer_bytes,
                lim.max_transfer_bytes
                - self.transfer_bytes
                - self.unknown * lim.max_page_transfer_bytes,
            ),
            min(
                lim.max_page_decoded_bytes,
                lim.max_decoded_bytes
                - self.decoded_bytes
                - self.unknown * lim.max_page_decoded_bytes,
            ),
            min(lim.max_page_saved_bytes, lim.max_saved_bytes - self.saved_total),
        )

    def required_wait_seconds(self) -> int:
        retry = self.plan.retry
        if self.last_outcome is None:
            return 0
        specific = {
            "response": 0,
            "429": retry.min_wait_after_429_seconds,
            "5xx": retry.min_wait_after_5xx_seconds,
            "network": retry.min_wait_after_network_error_seconds,
        }[self.last_outcome]
        return max(retry.min_interval_seconds, specific, self.last_retry_after)

    def earliest_next_attempt_at(self) -> datetime:
        """Measured from the previous attempt's response or unknown outcome.

        That time is an upper bound of when the previous request was sent, so
        the interval is never shorter than the plan's rule.
        """

        start = self.plan.not_before
        if self.approval is not None:
            start = max(start, self.approval.valid_from)
        start = start.astimezone(UTC)
        if self.last_network_at is None:
            return start
        return max(
            start, _add_seconds(self.last_network_at, self.required_wait_seconds())
        )

    def deadline(self) -> datetime | None:
        if self.first_reserved_at is None:
            return None
        return _add_seconds(
            self.first_reserved_at, self.plan.limits.max_elapsed_seconds
        )

    def next_page(self) -> tuple[PageRequest | None, int | None, str | None]:
        """The page a runner would reserve next, or why none can be reserved."""

        for query in self.queries[self.complete_prefix :]:
            prior = self.pages.get(query.query_id, ())
            if len(prior) >= self.plan.limits.max_pages_per_query:
                return None, None, "page_limit_reached"
            request = PageRequest(query, len(prior), prior[-1][1] if prior else None)
            count, last_id = self.page_attempts.get(request.request_id, (0, None))
            if count >= self.plan.retry.max_attempts_per_page:
                return request, None, "page_retry_budget_exhausted"
            if last_id is not None and self.attempts[last_id].state not in (
                _RETRYABLE_STATES | _OPEN_STATES
            ):
                return request, None, "previous_attempt_not_retryable"
            return request, count + 1, None
        return None, None, "all_pages_completed"

    # ------------------------------------------------------------- transitions

    def apply(self, event: JournalEvent) -> None:
        at = event.at.astimezone(UTC)
        if self.last_event_at is not None and at < self.last_event_at:
            raise HttpContractError("journal_clock_regressed")
        if self.completed or self.terminal_stop:
            raise HttpContractError("journal_event_after_terminal_state")
        getattr(self, "_on_" + event.kind)(event, at)
        self.last_event_at = at

    def _attempt(self, event: JournalEvent) -> _Attempt:
        attempt = self.attempts.get(event.attempt_id)
        if attempt is None:
            raise HttpContractError("event_for_unknown_attempt")
        return attempt

    def _within(self, at: datetime, prefix: str) -> None:
        deadline = self.deadline()
        if deadline is not None and at >= deadline:
            raise HttpContractError(f"{prefix}_after_cumulative_deadline")
        if not self.plan.not_before <= at < self.plan.expires_at:
            raise HttpContractError(f"{prefix}_outside_plan_validity")

    def _on_attempt_reserved(self, event: JournalEvent, at: datetime) -> None:
        plan, limits, page = self.plan, self.plan.limits, event.page
        if type(page) is not PageRequest or page.query not in self.positions:
            raise HttpContractError("page_outside_fixed_plan")
        if page.page_index >= limits.max_pages_per_query:
            raise HttpContractError("page_index_exceeds_plan_limit")
        if self.over_budget:
            raise HttpContractError("previous_page_budget_overrun")
        if self.open_attempt is not None:
            raise HttpContractError("prior_attempt_unsettled")
        if self.positions[page.query] > self.complete_prefix:
            raise HttpContractError("prior_query_incomplete")
        if self.completed_pages >= limits.max_pages_total:
            raise HttpContractError("page_budget_exhausted")
        allowed = self.allowances()
        for value, name in zip(allowed, ("transfer", "decoded", "saved"), strict=True):
            if value <= 0:
                raise HttpContractError(f"{name}_budget_exhausted")
        self._within(at, "attempt")
        if self.approval is None:
            raise HttpContractError("attempt_requires_approval_claim")
        if event.approval_sha256 != self.approval_sha256:
            raise HttpContractError("attempt_approval_mismatch")
        if not self.approval.valid_from <= at < self.approval.valid_until:
            raise HttpContractError("attempt_outside_approval_validity")
        if self.reserved >= limits.max_attempts:
            raise HttpContractError("attempt_budget_exhausted")
        if event.attempt_id in self.attempts or event.attempt_id != make_attempt_id(
            plan, page, event.attempt_number
        ):
            raise HttpContractError("attempt_identity_invalid")
        prior = self.pages.get(page.query.query_id, ())
        if page.page_index < len(prior):
            raise HttpContractError("completed_page_retried")
        if page.page_index and (
            page.page_index != len(prior) or prior[-1][1] != page.pagination_key
        ):
            raise HttpContractError("pagination_chain_invalid")
        count, last_id = self.page_attempts.get(page.request_id, (0, None))
        if event.attempt_number != count + 1:
            raise HttpContractError("page_attempt_number_invalid")
        if count >= plan.retry.max_attempts_per_page:
            raise HttpContractError("page_retry_budget_exhausted")
        if last_id is not None and self.attempts[last_id].state not in (
            _RETRYABLE_STATES
        ):
            raise HttpContractError("previous_attempt_not_retryable")
        if at < self.earliest_next_attempt_at():
            raise HttpContractError("attempt_before_rate_limit_wait")
        declared = (
            event.allowed_transfer_bytes,
            event.allowed_decoded_bytes,
            event.allowed_saved_bytes,
        )
        if declared != allowed:
            raise HttpContractError("reserved_allowance_invalid")
        self.attempts[event.attempt_id] = _Attempt(page, at, allowed)
        self.page_attempts[page.request_id] = (count + 1, event.attempt_id)
        self.reserved += 1
        self.retries += event.attempt_number > 1
        if self.first_reserved_at is None:
            self.first_reserved_at = at
        self.open_attempt = event.attempt_id

    def _on_attempt_sent(self, event: JournalEvent, at: datetime) -> None:
        attempt = self._attempt(event)
        if attempt.state != "reserved":
            raise HttpContractError("attempt_send_order_invalid")
        # Re-checked at send time: time may have passed since the reservation.
        self._within(at, "send")
        if not self.approval.valid_from <= at < self.approval.valid_until:
            raise HttpContractError("send_outside_approval_validity")
        self.attempts[event.attempt_id] = replace(attempt, state="sent", sent_at=at)
        self.sent += 1

    def _on_response_received(self, event: JournalEvent, at: datetime) -> None:
        attempt = self._attempt(event)
        if attempt.state != "sent":
            raise HttpContractError("response_order_invalid")
        if (at - attempt.sent_at).total_seconds() > self.plan.retry.timeout_seconds:
            raise HttpContractError("response_after_timeout")
        if at > self.deadline():
            raise HttpContractError("response_after_cumulative_deadline")
        limits, status = self.plan.limits, event.status
        self.transfer_bytes += event.transfer_bytes
        self.decoded_bytes += event.decoded_bytes
        over = (
            event.transfer_bytes > attempt.allowed[0]
            or event.decoded_bytes > attempt.allowed[1]
            or self.transfer_bytes + self.unknown * limits.max_page_transfer_bytes
            > limits.max_transfer_bytes
            or self.decoded_bytes + self.unknown * limits.max_page_decoded_bytes
            > limits.max_decoded_bytes
        )
        transient = status == 429 or 500 <= status < 600
        if over:
            # Evidence can record an overrun; nothing may be saved or sent after it.
            state = "over_allowance"
            self.over_budget = True
        elif status == 200:
            state = "responded"
        else:
            state = "failed" if transient else "nonretryable"
        self.attempts[event.attempt_id] = replace(
            attempt,
            state=state,
            status=status,
            settled_at=at,
            retry_after=event.retry_after_seconds,
        )
        self.responses += 1
        self.http_429 += status == 429
        self.last_network_at = at
        self.last_outcome = (
            "429" if status == 429 else "5xx" if 500 <= status < 600 else "response"
        )
        self.last_retry_after = event.retry_after_seconds or 0
        if state != "responded":
            self.open_attempt = None

    def _on_outcome_unknown(self, event: JournalEvent, at: datetime) -> None:
        attempt = self._attempt(event)
        if attempt.state not in ("reserved", "sent"):
            raise HttpContractError("unknown_outcome_order_invalid")
        self.attempts[event.attempt_id] = replace(
            attempt, state="unknown", settled_at=at
        )
        self.unknown += 1
        self.open_attempt = None
        self.last_network_at = at
        self.last_outcome = "network"
        self.last_retry_after = 0

    def _on_body_saved(self, event: JournalEvent, at: datetime) -> None:
        attempt = self._attempt(event)
        if attempt.state != "responded" or attempt.status != 200:
            raise HttpContractError("body_save_order_invalid")
        if event.saved_bytes > attempt.allowed[2]:
            raise HttpContractError("saved_bytes_exceed_reserved_allowance")
        digest = event.body_sha256
        query_key = (attempt.page.query.query_id, digest)
        if query_key in self.query_bodies:
            raise HttpContractError("repeated_page_body")
        previous = self.saved_bodies.get(digest)
        if previous is not None and previous != event.saved_bytes:
            raise HttpContractError("body_hash_size_mismatch")
        if previous is None:
            self.saved_bodies[digest] = event.saved_bytes
            self.completed_bodies[digest] = False
            self.saved_total += event.saved_bytes
        self.query_bodies[query_key] = attempt.page.page_index
        if self.saved_total > self.plan.limits.max_saved_bytes:
            self.over_budget = True
        self.attempts[event.attempt_id] = replace(
            attempt, state="body_saved", body_sha256=digest
        )

    def _on_page_completed(self, event: JournalEvent, at: datetime) -> None:
        attempt = self._attempt(event)
        if attempt.state != "body_saved":
            raise HttpContractError("page_completion_order_invalid")
        page = attempt.page
        prior = self.pages.get(page.query.query_id, ())
        if page.page_index < len(prior):
            raise HttpContractError("duplicate_completed_page")
        if event.next_key is not None and event.next_key in {
            p.pagination_key for p, _, _ in prior
        } | {page.pagination_key}:
            raise HttpContractError("pagination_key_loop")
        self.pages[page.query.query_id] = (
            *prior,
            (page, event.next_key, attempt.body_sha256),
        )
        self.completed_pages += 1
        if event.next_key is None:
            self.complete_prefix += 1
        if self.completed_pages > self.plan.limits.max_pages_total:
            raise HttpContractError("page_budget_exhausted")
        self.completed_bodies[attempt.body_sha256] = True
        self.attempts[event.attempt_id] = replace(attempt, state="page_completed")
        self.open_attempt = None

    def _on_run_completed(self, event: JournalEvent, at: datetime) -> None:
        # Completion is bookkeeping: the deadline bounds reservations, sends and
        # responses, so a run whose pages all finished in time may be closed later.
        if self.complete_prefix != len(self.queries):
            raise HttpContractError("run_completed_with_missing_page")
        if self.over_budget:
            raise HttpContractError("run_completed_after_budget_overrun")
        if self.open_attempt is not None:
            raise HttpContractError("run_completed_with_unsettled_attempt")
        self.completed = True

    def _on_run_stopped(self, event: JournalEvent, at: datetime) -> None:
        self.terminal_stop = bool(event.terminal)


class EvidenceJournal:
    """Hash-chained journal contract (bytes in, bytes out); not a file writer.

    ``EvidenceJournal(plan, data, approval=...)`` re-validates every line (open
    and resume). ``append`` validates only the new event against a copy of the
    already verified state; its result has the same bytes and state as a full
    re-open, which the tests check.
    """

    def __init__(
        self,
        plan: HttpAcquisitionPlan,
        data: bytes = b"",
        *,
        approval: OwnerApprovalClaim | None = None,
    ) -> None:
        if type(plan) is not HttpAcquisitionPlan or type(data) is not bytes:
            raise HttpContractError("journal_requires_plan_and_bytes")
        plan.verify_fixed_scope()
        if approval is not None:
            if type(approval) is not OwnerApprovalClaim:
                raise HttpContractError("journal_approval_claim_type_invalid")
            _check_approval_fields(plan, approval)
            if not (
                plan.not_before <= approval.valid_from
                and approval.valid_until <= plan.expires_at
            ):
                raise HttpContractError("approval_outside_plan_window")
        if data and not data.endswith(b"\n"):
            raise HttpContractError("journal_incomplete_final_line")
        state = _JournalState(plan, approval)
        head, lines, events = "0" * 64, [], []
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
                or record["previous_hash"] != head
                or record["plan_sha256"] != plan.sha256
                or record["event_id"] != _digest(content)
            ):
                raise HttpContractError("journal_chain_or_plan_mismatch")
            event = JournalEvent.from_dict(record["event"])
            state.apply(event)
            events.append(event)
            lines.append(line + b"\n")
            head = record["event_id"]
        self._init(plan, approval, tuple(lines), tuple(events), head, len(data), state)

    def _init(self, plan, approval, lines, events, head, size, state) -> None:
        self.plan = plan
        self.approval = approval
        self._lines = lines
        self.events: tuple[JournalEvent, ...] = events
        self.head_hash = head
        self._byte_count = size
        self._state = state

    @property
    def data(self) -> bytes:
        return b"".join(self._lines)

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def byte_count(self) -> int:
        return self._byte_count

    @property
    def completed(self) -> bool:
        return self._state.completed

    @property
    def approval_sha256(self) -> str | None:
        return self._state.approval_sha256

    @property
    def next_reservation_allowances(self) -> tuple[int, int, int]:
        """(transfer, decoded, saved) a reservation must declare right now."""

        return self._state.allowances()

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
        line = (_canonical(record) + "\n").encode("utf-8")
        parsed = JournalEvent.from_dict(json.loads(line)["event"])  # as on re-open
        state = self._state.clone()
        state.apply(parsed)
        new = object.__new__(EvidenceJournal)
        new._init(
            self.plan,
            self.approval,
            (*self._lines, line),
            (*self.events, parsed),
            record["event_id"],
            self._byte_count + len(line),
            state,
        )
        return new


@dataclass(frozen=True)
class BodyFileEvidence:
    """A *declared* file; actual file verification belongs to the next PR.

    ``body_sha256`` and ``size`` describe the file's current bytes in every
    state: ``committed`` (body of a completed page), ``orphan`` (complete body
    no completed page references) or ``partial`` (incomplete bytes; their hash
    is not a page hash).
    """

    object_id: str
    size: int
    state: str
    body_sha256: str

    def __post_init__(self) -> None:
        _text(self.object_id, "body_object_id")
        _nonnegative_int(self.size, "body_file_size")
        if self.state not in ("committed", "orphan", "partial"):
            raise HttpContractError("unknown_body_file_state")
        _hex(self.body_sha256, "body_sha256")
        if self.state != "partial" and self.size == 0:
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
        # One content may not be counted twice, not even under different states.
        if len({item.body_sha256 for item in self.files}) != len(self.files):
            raise HttpContractError("duplicate_stored_body")

    @property
    def saved_bytes(self) -> int:
        return sum(item.size for item in self.files)

    @property
    def body_set_sha256(self) -> str:
        """Identifies every declared file: id, state, size and hash."""

        return _digest(
            {
                "schema": BODY_SET_SCHEMA,
                "files": [
                    {
                        "object_id": item.object_id,
                        "state": item.state,
                        "bytes": item.size,
                        "sha256": item.body_sha256,
                    }
                    for item in sorted(self.files, key=lambda item: item.object_id)
                ],
            }
        )


def _check_body_links(journal: EvidenceJournal, inventory: BodyInventory) -> None:
    state = journal._state
    items = {item.body_sha256: item for item in inventory.files}
    for digest, size in state.saved_bodies.items():
        item = items.get(digest)
        if item is None or item.size != size:
            raise HttpContractError("saved_body_missing_or_size_mismatch")
        if item.state == "partial":
            raise HttpContractError("recorded_body_declared_partial")
        if state.completed_bodies[digest] and item.state != "committed":
            raise HttpContractError("completed_page_body_not_committed")
        if not state.completed_bodies[digest] and item.state == "committed":
            raise HttpContractError("committed_body_without_completed_page")
    for item in inventory.files:
        if item.state == "committed" and item.body_sha256 not in state.saved_bodies:
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
    plan_allows_next_attempt: bool
    status: str
    earliest_next_attempt_at: datetime | None
    next_page: PageRequest | None
    next_attempt_number: int | None
    next_allowed_transfer_bytes: int
    next_allowed_decoded_bytes: int
    next_allowed_saved_bytes: int
    blocking_reasons: tuple[str, ...]


def budget_snapshot(
    journal: EvidenceJournal, inventory: BodyInventory, *, now: datetime
) -> BudgetSnapshot:
    """Reconstruct counters from an existing journal; resume never starts at zero.

    ``plan_allows_next_attempt`` covers this plan's own budget, validity and
    waits only; it is true when ``blocking_reasons`` is empty. It says nothing
    about other plans or processes on the same account: that is the account
    rate ledger (``assess_account_slot``), and real HTTP permission exists only
    through ``require_live_acquisition_permission`` (closed in this contract).
    A runner must re-check both at send time.
    """

    if type(journal) is not EvidenceJournal or type(inventory) is not BodyInventory:
        raise HttpContractError("journal_and_body_inventory_required")
    _check_body_links(journal, inventory)
    current = datetime.fromisoformat(_utc(now, "now"))
    state, plan, limits = journal._state, journal.plan, journal.plan.limits
    if state.last_event_at is not None and current < state.last_event_at:
        raise HttpContractError("resume_clock_regressed")
    first = state.first_reserved_at
    elapsed = max(0.0, (current - first).total_seconds()) if first else 0.0
    remaining_attempts = max(0, limits.max_attempts - state.reserved)
    remaining_pages = max(0, limits.max_pages_total - state.completed_pages)
    remaining_seconds = max(0.0, limits.max_elapsed_seconds - elapsed)
    # A lost reply may have consumed a full page: reserve that worst case.
    remaining_transfer = max(
        0,
        limits.max_transfer_bytes
        - state.transfer_bytes
        - state.unknown * limits.max_page_transfer_bytes,
    )
    remaining_decoded = max(
        0,
        limits.max_decoded_bytes
        - state.decoded_bytes
        - state.unknown * limits.max_page_decoded_bytes,
    )
    remaining_saved = max(0, limits.max_saved_bytes - inventory.saved_bytes)
    status = (
        "completed"
        if state.completed
        else "terminal_stop"
        if state.terminal_stop
        else "open"
    )
    next_page, next_number, page_problem = state.next_page()
    try:
        earliest = state.earliest_next_attempt_at()
        deadline = state.deadline()
    except HttpContractError:
        earliest = deadline = None
    approval = journal.approval
    if status == "completed":
        reasons = ["run_completed"]
    elif status == "terminal_stop":
        reasons = ["terminal_stop_recorded"]
    else:
        checks = (
            (approval is None, "approval_claim_missing"),
            (current < plan.not_before, "before_plan_validity"),
            (current >= plan.expires_at, "plan_expired"),
            (approval is not None and current < approval.valid_from,
             "before_approval_validity"),
            (approval is not None and current >= approval.valid_until,
             "approval_expired"),
            (state.open_attempt is not None, "unsettled_attempt"),
            (state.over_budget, "budget_overrun_recorded"),
            (any(item.state != "committed" for item in inventory.files),
             "orphan_or_partial_body_present"),
            (remaining_attempts == 0, "attempt_budget_exhausted"),
            (remaining_pages == 0, "page_budget_exhausted"),
            (remaining_seconds == 0, "time_budget_exhausted"),
            (remaining_transfer == 0, "transfer_budget_exhausted"),
            (remaining_decoded == 0, "decoded_budget_exhausted"),
            (remaining_saved == 0, "saved_budget_exhausted"),
            (page_problem is not None, page_problem),
            (earliest is None, "next_attempt_time_unrepresentable"),
        )  # fmt: skip
        if earliest is not None:
            checks += (
                (current < earliest, "rate_limit_wait"),
                (deadline is not None and earliest >= deadline,
                 "deadline_before_next_allowed_attempt"),
                (earliest >= plan.expires_at,
                 "plan_expires_before_next_allowed_attempt"),
                (approval is not None and earliest >= approval.valid_until,
                 "approval_expires_before_next_allowed_attempt"),
            )  # fmt: skip
        reasons = [reason for blocked, reason in checks if blocked]
    return BudgetSnapshot(
        reserved_attempts=state.reserved,
        sent_attempts=state.sent,
        received_responses=state.responses,
        completed_pages=state.completed_pages,
        retry_attempts=state.retries,
        http_429_responses=state.http_429,
        unknown_outcomes=state.unknown,
        unsettled_attempts=int(state.open_attempt is not None),
        orphan_files=sum(item.state == "orphan" for item in inventory.files),
        partial_files=sum(item.state == "partial" for item in inventory.files),
        elapsed_seconds=elapsed,
        remaining_attempts=remaining_attempts,
        remaining_pages=remaining_pages,
        remaining_seconds=remaining_seconds,
        remaining_transfer_bytes=remaining_transfer,
        remaining_decoded_bytes=remaining_decoded,
        remaining_saved_bytes=remaining_saved,
        plan_allows_next_attempt=not reasons,
        status=status,
        earliest_next_attempt_at=earliest,
        next_page=next_page,
        next_attempt_number=next_number,
        next_allowed_transfer_bytes=min(
            limits.max_page_transfer_bytes, remaining_transfer
        ),
        next_allowed_decoded_bytes=min(
            limits.max_page_decoded_bytes, remaining_decoded
        ),
        next_allowed_saved_bytes=min(limits.max_page_saved_bytes, remaining_saved),
        blocking_reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class ExternalReceiptClaim:
    """A receipt's *content*; its independent custody is not proven here."""

    artifact_id: str
    plan_sha256: str
    approval_sha256: str
    approval_event_id: str
    ledger_event_count: int
    ledger_bytes: int
    ledger_head_sha256: str
    body_set_sha256: str
    fixed_at: datetime
    issuer_id: str
    receipt_id: str
    external_reference: str

    def __post_init__(self) -> None:
        for name in (
            "artifact_id",
            "approval_event_id",
            "issuer_id",
            "receipt_id",
            "external_reference",
        ):
            _text(getattr(self, name), name)
        for name in (
            "plan_sha256",
            "approval_sha256",
            "ledger_head_sha256",
            "body_set_sha256",
        ):
            _hex(getattr(self, name), name)
        for name in ("ledger_event_count", "ledger_bytes"):
            _nonnegative_int(getattr(self, name), name)
        _utc(self.fixed_at, "fixed_at")

    def to_dict(self) -> dict:
        return {
            "schema": RECEIPT_SCHEMA,
            "artifact_id": self.artifact_id,
            "plan_sha256": self.plan_sha256,
            "approval_sha256": self.approval_sha256,
            "approval_event_id": self.approval_event_id,
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
    """Informational result; never an authorization token for any entry point."""

    content_matches: bool
    independent_custody_verified: bool = field(default=False, init=False)


def check_receipt_alignment(
    journal: EvidenceJournal,
    inventory: BodyInventory,
    receipt: ExternalReceiptClaim,
) -> ReceiptAlignment:
    if type(journal) is not EvidenceJournal or type(inventory) is not BodyInventory:
        raise HttpContractError("journal_and_body_inventory_required")
    if type(receipt) is not ExternalReceiptClaim:
        raise HttpContractError("external_receipt_claim_required")
    if journal.approval is None:
        raise HttpContractError("receipt_requires_approval_claim")
    _check_body_links(journal, inventory)
    expected = {
        "artifact_id": journal.plan.artifact_id,
        "plan_sha256": journal.plan.sha256,
        "approval_sha256": journal.approval.sha256,
        "approval_event_id": journal.approval.approval_event_id,
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


# ------------------------------------------------------------- calendar anchor


def calendar_anchor_reference(discovery_artifact_id: str) -> str:
    """The only accepted ``calendar_source_reference`` of an anchored plan."""

    return f"calendar-discovery:{discovery_artifact_id}"


@dataclass(frozen=True)
class CalendarDiscoveryEvidence:
    """Raw inputs from a completed ``calendar_discovery`` artifact (not a verdict)."""

    plan: HttpAcquisitionPlan
    approval: OwnerApprovalClaim
    journal_data: bytes
    inventory: BodyInventory
    receipt: ExternalReceiptClaim
    bodies: tuple[bytes, ...]


@dataclass(frozen=True)
class CalendarAnchor:
    """Derived from verified content; informational, never an authorization token."""

    discovery_artifact_id: str
    discovery_plan_sha256: str
    calendar_start: str
    calendar_end: str
    sessions: tuple[str, ...]
    anchor_sha256: str
    independent_custody_verified: bool = field(default=False, init=False)


def _calendar_rows(journal: EvidenceJournal, bodies: tuple[bytes, ...]) -> list:
    if type(bodies) is not tuple or any(type(body) is not bytes for body in bodies):
        raise HttpContractError("calendar_bodies_must_be_bytes")
    by_digest = {hashlib.sha256(body).hexdigest(): body for body in bodies}
    pages = journal._state.pages[journal.plan.queries[0].query_id]
    if len(by_digest) != len(bodies) or set(by_digest) != {p[2] for p in pages}:
        raise HttpContractError("calendar_body_set_mismatch")
    rows = []
    for _, next_key, digest in pages:
        body = by_digest[digest]
        if len(body) != journal._state.saved_bodies[digest]:
            raise HttpContractError("calendar_body_size_mismatch")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise HttpContractError("calendar_body_invalid_json") from None
        if type(payload) is not dict or type(payload.get("data")) is not list:
            raise HttpContractError("calendar_body_schema_invalid")
        if payload.get("pagination_key") != next_key:
            raise HttpContractError("calendar_body_pagination_mismatch")
        rows.extend(payload["data"])
    return rows


def derive_calendar_anchor(evidence: CalendarDiscoveryEvidence) -> CalendarAnchor:
    """Verify a completed calendar_discovery artifact and derive its session set.

    Checks the journal (with its approval claim), the receipt content, that the
    supplied bodies are exactly the completed pages' bodies, and that every
    calendar day in the fixed range appears once. It never creates or widens a
    Daily plan; custody of the receipt is still unverified.
    """

    from .census import SESSION_HOLIDAY_DIVISIONS  # one session rule for the project

    if type(evidence) is not CalendarDiscoveryEvidence:
        raise HttpContractError("calendar_evidence_required")
    plan = evidence.plan
    if type(plan) is not HttpAcquisitionPlan or plan.kind != "calendar_discovery":
        raise HttpContractError("calendar_evidence_plan_not_discovery")
    journal = EvidenceJournal(plan, evidence.journal_data, approval=evidence.approval)
    if not journal.completed:
        raise HttpContractError("calendar_evidence_run_incomplete")
    check_receipt_alignment(journal, evidence.inventory, evidence.receipt)
    days: dict[str, str] = {}
    for row in _calendar_rows(journal, evidence.bodies):
        if type(row) is not dict or type(row.get("HolDiv")) is not str:
            raise HttpContractError("calendar_row_invalid")
        try:
            day = _iso_day(row.get("Date"), "calendar_row_date")
        except HttpContractError:
            raise HttpContractError("calendar_row_invalid") from None
        if row["HolDiv"] not in HOLIDAY_DIVISIONS:
            raise HttpContractError("calendar_row_invalid")
        if not plan.calendar_start <= day <= plan.calendar_end:
            raise HttpContractError("calendar_row_outside_range")
        if day in days:
            raise HttpContractError("calendar_row_duplicate")
        days[day] = row["HolDiv"]
    first = date.fromisoformat(plan.calendar_start)
    span = (date.fromisoformat(plan.calendar_end) - first).days + 1
    if set(days) != {(first + timedelta(days=i)).isoformat() for i in range(span)}:
        raise HttpContractError("calendar_rows_incomplete")
    sessions = tuple(
        sorted(d for d, div in days.items() if div in SESSION_HOLIDAY_DIVISIONS)
    )
    anchor = _digest(
        {
            "schema": ANCHOR_SCHEMA,
            "discovery_artifact_id": plan.artifact_id,
            "discovery_plan_sha256": plan.sha256,
            "ledger_head_sha256": journal.head_hash,
            "body_set_sha256": evidence.inventory.body_set_sha256,
            "calendar_start": plan.calendar_start,
            "calendar_end": plan.calendar_end,
            "sessions": list(sessions),
        }
    )
    return CalendarAnchor(
        discovery_artifact_id=plan.artifact_id,
        discovery_plan_sha256=plan.sha256,
        calendar_start=plan.calendar_start,
        calendar_end=plan.calendar_end,
        sessions=sessions,
        anchor_sha256=anchor,
    )


def verify_calendar_anchor(
    plan: HttpAcquisitionPlan, evidence: CalendarDiscoveryEvidence
) -> CalendarAnchor:
    """Check an anchored Daily plan against re-derived calendar evidence."""

    if type(plan) is not HttpAcquisitionPlan or plan.kind != "calendar_anchored_daily":
        raise HttpContractError("anchored_daily_plan_required")
    anchor = derive_calendar_anchor(evidence)
    if plan.artifact_id == anchor.discovery_artifact_id:
        raise HttpContractError("anchored_plan_reuses_discovery_artifact")
    if plan.calendar_source_reference != calendar_anchor_reference(
        anchor.discovery_artifact_id
    ):
        raise HttpContractError("calendar_anchor_reference_mismatch")
    if plan.calendar_source_sha256 != anchor.anchor_sha256:
        raise HttpContractError("calendar_anchor_hash_mismatch")
    if not (
        anchor.calendar_start <= plan.calendar_start
        and plan.calendar_end <= anchor.calendar_end
    ):
        raise HttpContractError("anchored_window_outside_calendar_evidence")
    sessions = set(anchor.sessions)
    if plan.reference_date not in sessions:
        raise HttpContractError("reference_date_not_a_session")
    if not set(plan.daily_dates) <= sessions:
        raise HttpContractError("daily_dates_include_non_sessions")
    return anchor


# ----------------------------------------------------------- account rate ledger

ACCOUNT_LEDGER_SCHEMA = "historical-feasibility-account-rate-ledger-v2"
_ACCOUNT_OUTCOMES = frozenset({"response", "429", "5xx", "unknown"})


@dataclass(frozen=True)
class AccountRatePolicy:
    """Account-wide waits shared by every plan that uses the same account.

    ``slot_lease_seconds`` bounds how long one reserved send slot may stay
    open; a request must be able to finish (``request_timeout_seconds``)
    inside it, so a slot reclaimed after the lease can no longer be in flight.
    """

    min_interval_seconds: int
    min_wait_after_429_seconds: int
    min_wait_after_5xx_seconds: int
    min_wait_after_network_error_seconds: int
    request_timeout_seconds: int
    slot_lease_seconds: int

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            _bounded_seconds(value, name, MAX_WAIT_SECONDS)
        if self.min_interval_seconds < 13 or self.min_wait_after_429_seconds < 120:
            raise HttpContractError("free_rate_limit_policy_too_weak")
        if self.slot_lease_seconds < self.request_timeout_seconds:
            raise HttpContractError("slot_lease_shorter_than_request_timeout")

    def wait_after(self, outcome: str) -> int:
        specific = {
            "response": 0,
            "429": self.min_wait_after_429_seconds,
            "5xx": self.min_wait_after_5xx_seconds,
            "unknown": self.min_wait_after_network_error_seconds,
        }[outcome]
        return max(self.min_interval_seconds, specific)

    def covers(self, retry: RetryRules) -> bool:
        """True when this policy is at least as strict as a plan's own rules."""

        return (
            self.min_interval_seconds >= retry.min_interval_seconds
            and self.min_wait_after_429_seconds >= retry.min_wait_after_429_seconds
            and self.min_wait_after_5xx_seconds >= retry.min_wait_after_5xx_seconds
            and self.min_wait_after_network_error_seconds
            >= retry.min_wait_after_network_error_seconds
            and self.request_timeout_seconds >= retry.timeout_seconds
        )


_ACCOUNT_REQUIRED = {
    "ledger_opened": frozenset({"policy"}),
    "slot_reserved": frozenset({"slot_id", "plan_sha256", "holder_id"}),
    "slot_sent": frozenset({"slot_id", "holder_id"}),
    "slot_settled": frozenset(
        {"slot_id", "holder_id", "outcome", "effective_wait_seconds"}
    ),
    # Recovery of an abandoned slot by another holder: explicit evidence only.
    "slot_reclaimed": frozenset(
        {
            "slot_id",
            "holder_id",
            "previous_holder_id",
            "lease_expired_at",
            "reclaim_reason",
            "effective_wait_seconds",
        }
    ),
}
RECLAIM_REASONS = frozenset({"lease_expired"})


@dataclass(frozen=True)
class AccountRateEvent:
    """One account-wide slot event; ``slot_id`` is the plan journal's attempt id.

    On ``slot_settled`` (only by the slot's own holder),
    ``observed_retry_after_seconds`` is the value seen on the HTTP response
    (copied unchanged from the plan journal) and ``effective_wait_seconds`` is
    the wait the account applies after this slot; the latter may be longer than
    any rule, but never shorter. ``slot_reclaimed`` is the only way another
    holder may close a slot: it names the previous holder, the lease end it
    relies on and the reason, and always means an unknown outcome.
    """

    kind: str
    at: datetime
    policy: AccountRatePolicy | None = None
    slot_id: str | None = None
    plan_sha256: str | None = None
    holder_id: str | None = None
    outcome: str | None = None
    observed_retry_after_seconds: int | None = None
    effective_wait_seconds: int | None = None
    previous_holder_id: str | None = None
    lease_expired_at: datetime | None = None
    reclaim_reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _ACCOUNT_REQUIRED:
            raise HttpContractError("unknown_account_event")
        _utc(self.at, "event_at")
        if self.policy is not None and type(self.policy) is not AccountRatePolicy:
            raise HttpContractError("account_policy_type_invalid")
        for name in ("slot_id", "plan_sha256"):
            if getattr(self, name) is not None:
                _hex(getattr(self, name), name)
        for name in ("holder_id", "previous_holder_id"):
            if getattr(self, name) is not None:
                _label(getattr(self, name), name)
        if self.lease_expired_at is not None:
            _utc(self.lease_expired_at, "lease_expired_at")
        if (
            self.reclaim_reason is not None
            and self.reclaim_reason not in RECLAIM_REASONS
        ):
            raise HttpContractError("reclaim_reason_invalid")
        if self.outcome is not None and self.outcome not in _ACCOUNT_OUTCOMES:
            raise HttpContractError("account_outcome_invalid")
        if self.observed_retry_after_seconds is not None:
            _bounded_seconds(
                self.observed_retry_after_seconds,
                "retry_after_seconds",
                MAX_WAIT_SECONDS,
                allow_zero=True,
            )
        if self.effective_wait_seconds is not None:
            _bounded_seconds(
                self.effective_wait_seconds, "effective_wait_seconds", MAX_WAIT_SECONDS
            )
        fields = {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name not in ("kind", "at")
        }
        required = _ACCOUNT_REQUIRED[self.kind]
        optional = (
            {"observed_retry_after_seconds"} if self.kind == "slot_settled" else set()
        )
        if any(fields[name] is None for name in required):
            raise HttpContractError("account_event_missing_required_field")
        if any(
            value is not None
            for name, value in fields.items()
            if name not in required | optional
        ):
            raise HttpContractError("account_event_has_forbidden_field")
        if self.observed_retry_after_seconds is not None and self.outcome == "unknown":
            raise HttpContractError("retry_after_requires_a_response")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "at": _utc(self.at, "event_at"),
            "policy": asdict(self.policy) if self.policy is not None else None,
            "slot_id": self.slot_id,
            "plan_sha256": self.plan_sha256,
            "holder_id": self.holder_id,
            "outcome": self.outcome,
            "observed_retry_after_seconds": self.observed_retry_after_seconds,
            "effective_wait_seconds": self.effective_wait_seconds,
            "previous_holder_id": self.previous_holder_id,
            "lease_expired_at": (
                _utc(self.lease_expired_at, "lease_expired_at")
                if self.lease_expired_at is not None
                else None
            ),
            "reclaim_reason": self.reclaim_reason,
        }

    @classmethod
    def from_dict(cls, value: object) -> AccountRateEvent:
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise HttpContractError("account_event_schema_invalid")
        raw = dict(value)
        raw["at"] = _read_utc(raw["at"], "event_at")
        if raw["lease_expired_at"] is not None:
            raw["lease_expired_at"] = _read_utc(
                raw["lease_expired_at"], "lease_expired_at"
            )
        policy = raw["policy"]
        if policy is not None:
            if type(policy) is not dict or set(policy) != set(
                AccountRatePolicy.__dataclass_fields__
            ):
                raise HttpContractError("account_policy_schema_invalid")
            raw["policy"] = AccountRatePolicy(**policy)
        return cls(**raw)


@dataclass(frozen=True)
class _Slot:
    plan_sha256: str
    holder_id: str
    reserved_at: datetime
    sent_at: datetime | None = None
    outcome: str | None = None
    settled_at: datetime | None = None
    observed_retry_after: int | None = None
    effective_wait: int | None = None
    reclaimed_by: str | None = None  # set only by an explicit slot_reclaimed


class _AccountState:
    """At most one open send slot per account; waits carry across plans."""

    def __init__(self, account_ref: str) -> None:
        self.account_ref = account_ref
        self.policy: AccountRatePolicy | None = None
        self.opened_at: datetime | None = None
        self.slots: dict[str, _Slot] = {}
        self.open_slot: str | None = None
        self.last_event_at: datetime | None = None
        self.last_settled_at: datetime | None = None
        self.last_effective_wait = 0

    def clone(self) -> _AccountState:
        new = copy.copy(self)
        new.slots = dict(self.slots)
        return new

    def earliest_next_slot_at(self) -> datetime:
        if self.last_settled_at is None:
            return self.opened_at
        return _add_seconds(self.last_settled_at, self.last_effective_wait)

    def lease_end(self, slot: _Slot) -> datetime:
        return _add_seconds(slot.reserved_at, self.policy.slot_lease_seconds)

    def apply(self, event: AccountRateEvent) -> None:
        at = event.at.astimezone(UTC)
        if self.last_event_at is not None and at < self.last_event_at:
            raise HttpContractError("account_clock_regressed")
        if event.kind == "ledger_opened":
            if self.policy is not None or self.last_event_at is not None:
                raise HttpContractError("account_ledger_reopened")
            self.policy, self.opened_at = event.policy, at
        elif self.policy is None:
            raise HttpContractError("account_ledger_not_opened")
        else:
            getattr(self, "_on_" + event.kind)(event, at)
        self.last_event_at = at

    def _open(self, event: AccountRateEvent) -> _Slot:
        if event.slot_id != self.open_slot:
            raise HttpContractError("account_slot_not_open")
        return self.slots[event.slot_id]

    def _on_slot_reserved(self, event: AccountRateEvent, at: datetime) -> None:
        if self.open_slot is not None:
            raise HttpContractError("account_slot_in_use")
        if event.slot_id in self.slots:
            raise HttpContractError("account_slot_reused")
        if at < self.earliest_next_slot_at():
            raise HttpContractError("account_rate_limit_wait")
        self.slots[event.slot_id] = _Slot(event.plan_sha256, event.holder_id, at)
        self.open_slot = event.slot_id

    def _on_slot_sent(self, event: AccountRateEvent, at: datetime) -> None:
        slot = self._open(event)
        if event.holder_id != slot.holder_id:
            raise HttpContractError("account_slot_holder_mismatch")
        if slot.sent_at is not None:
            raise HttpContractError("account_slot_already_sent")
        # The whole request must fit in the lease, so a reclaim never overlaps it.
        if _add_seconds(at, self.policy.request_timeout_seconds) > self.lease_end(slot):
            raise HttpContractError("account_slot_lease_too_short_for_send")
        self.slots[event.slot_id] = replace(slot, sent_at=at)

    def _close(self, slot_id, slot, at, outcome, observed, effective, by=None):
        required = max(self.policy.wait_after(outcome), observed or 0)
        if effective < required:
            raise HttpContractError("account_effective_wait_below_rule")
        self.slots[slot_id] = replace(
            slot,
            outcome=outcome,
            settled_at=at,
            observed_retry_after=observed,
            effective_wait=effective,
            reclaimed_by=by,
        )
        self.open_slot = None
        self.last_settled_at = at
        self.last_effective_wait = effective

    def _on_slot_settled(self, event: AccountRateEvent, at: datetime) -> None:
        slot = self._open(event)
        if event.holder_id != slot.holder_id:
            # Only the holder settles; any other process must use slot_reclaimed.
            raise HttpContractError("account_slot_holder_mismatch")
        if event.outcome != "unknown" and slot.sent_at is None:
            raise HttpContractError("account_slot_outcome_without_send")
        self._close(
            event.slot_id,
            slot,
            at,
            event.outcome,
            event.observed_retry_after_seconds,
            event.effective_wait_seconds,
        )

    def _on_slot_reclaimed(self, event: AccountRateEvent, at: datetime) -> None:
        slot = self._open(event)
        if event.holder_id == slot.holder_id:
            raise HttpContractError("account_reclaim_by_the_holder_itself")
        if event.previous_holder_id != slot.holder_id:
            raise HttpContractError("account_reclaim_previous_holder_mismatch")
        lease_end = self.lease_end(slot)
        if event.lease_expired_at.astimezone(UTC) != lease_end:
            raise HttpContractError("account_reclaim_lease_evidence_mismatch")
        if at < lease_end:
            raise HttpContractError("account_slot_reclaim_before_lease_expiry")
        # A reclaim always means an unknown outcome: the request may have been sent.
        self._close(
            event.slot_id,
            slot,
            at,
            "unknown",
            None,
            event.effective_wait_seconds,
            by=event.holder_id,
        )


class AccountRateLedger:
    """Account-wide, hash-chained send-slot ledger contract (bytes in, bytes out).

    Every plan that uses the same account shares one ledger, so the waits of
    one plan (a 429, an unknown outcome, the plain interval) bind the next
    attempt of any other plan. ``account_ref`` is an owner-chosen label, never
    a credential; declaring it does not prove which real account a key uses,
    and the ledger cannot see requests made outside it (other apps, devices).
    """

    def __init__(self, account_ref: str, data: bytes = b"") -> None:
        _label(account_ref, "account_ref")
        if type(data) is not bytes:
            raise HttpContractError("account_ledger_requires_bytes")
        if data and not data.endswith(b"\n"):
            raise HttpContractError("account_ledger_incomplete_final_line")
        state = _AccountState(account_ref)
        head, lines = "0" * 64, []
        for sequence, line in enumerate(data.split(b"\n")[:-1]):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise HttpContractError("account_ledger_invalid_json") from None
            if type(record) is not dict or set(record) != {
                "schema",
                "sequence",
                "previous_hash",
                "account_ref",
                "event",
                "event_id",
            }:
                raise HttpContractError("account_ledger_record_schema_invalid")
            try:
                canonical_line = _canonical(record).encode("utf-8")
            except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
                raise HttpContractError("account_ledger_record_noncanonical") from None
            if line != canonical_line:
                raise HttpContractError("account_ledger_record_noncanonical")
            content = {k: v for k, v in record.items() if k != "event_id"}
            if (
                record["schema"] != ACCOUNT_LEDGER_SCHEMA
                or type(record["sequence"]) is not int
                or record["sequence"] != sequence
                or record["previous_hash"] != head
                or record["event_id"] != _digest(content)
            ):
                raise HttpContractError("account_ledger_chain_mismatch")
            if record["account_ref"] != account_ref:
                raise HttpContractError("account_ledger_account_mismatch")
            state.apply(AccountRateEvent.from_dict(record["event"]))
            lines.append(line + b"\n")
            head = record["event_id"]
        self._init(account_ref, tuple(lines), head, len(data), state)

    def _init(self, account_ref, lines, head, size, state) -> None:
        self.account_ref = account_ref
        self._lines = lines
        self.head_hash = head
        self._byte_count = size
        self._state = state

    @property
    def data(self) -> bytes:
        return b"".join(self._lines)

    @property
    def event_count(self) -> int:
        return len(self._lines)

    @property
    def byte_count(self) -> int:
        return self._byte_count

    @property
    def policy(self) -> AccountRatePolicy | None:
        return self._state.policy

    def append(self, event: AccountRateEvent) -> AccountRateLedger:
        if type(event) is not AccountRateEvent:
            raise HttpContractError("account_event_required")
        content = {
            "schema": ACCOUNT_LEDGER_SCHEMA,
            "sequence": self.event_count,
            "previous_hash": self.head_hash,
            "account_ref": self.account_ref,
            "event": event.to_dict(),
        }
        record = {**content, "event_id": _digest(content)}
        line = (_canonical(record) + "\n").encode("utf-8")
        state = self._state.clone()
        state.apply(AccountRateEvent.from_dict(json.loads(line)["event"]))
        new = object.__new__(AccountRateLedger)
        new._init(
            self.account_ref,
            (*self._lines, line),
            record["event_id"],
            self._byte_count + len(line),
            state,
        )
        return new


def commit_account_ledger(
    account_ref: str, current: bytes, proposed: bytes
) -> AccountRateLedger:
    """Acceptance rule of the shared store's atomic compare-and-append.

    ``proposed`` must be exactly ``current`` plus one valid record. The storage
    layer (not implemented here) must read ``current`` and write ``proposed``
    under one exclusive lock; a writer whose view is stale (another process
    appended first) is refused instead of reserving a slot already taken.
    """

    if type(current) is not bytes or type(proposed) is not bytes:
        raise HttpContractError("account_ledger_requires_bytes")
    AccountRateLedger(account_ref, current)
    if not proposed.startswith(current) or (
        proposed.count(b"\n") != current.count(b"\n") + 1
    ):
        raise HttpContractError("account_ledger_stale_or_conflicting_commit")
    return AccountRateLedger(account_ref, proposed)


def _status_outcome(status: int) -> str:
    return "429" if status == 429 else "5xx" if 500 <= status < 600 else "response"


def _plan_rule_wait(retry: RetryRules, outcome: str) -> int:
    specific = {
        "response": 0,
        "429": retry.min_wait_after_429_seconds,
        "5xx": retry.min_wait_after_5xx_seconds,
        "unknown": retry.min_wait_after_network_error_seconds,
    }[outcome]
    return max(retry.min_interval_seconds, specific)


@dataclass(frozen=True)
class LedgerReconciliation:
    """Informational comparison of one plan journal with the account ledger.

    ``consistent``: both ledgers describe the same attempts and results.
    ``pending``: a crash left exactly one side behind; the missing record is
    fixed by the other side's evidence and must be appended before any new
    slot. ``inconsistent``: a contradiction that is never repaired
    automatically. Only ``consistent`` lets the account state be used.
    """

    plan_sha256: str
    status: str
    issues: tuple[str, ...]


def reconcile_account_and_plan(
    ledger: AccountRateLedger, journal: EvidenceJournal
) -> LedgerReconciliation:
    """Match every attempt of the plan with its account slot (same plan, same id).

    Write protocol per attempt: account ``slot_reserved`` -> plan
    ``attempt_reserved`` -> plan ``attempt_sent`` -> account ``slot_sent`` ->
    HTTP request -> plan result -> account ``slot_settled`` copying the plan's
    outcome and observed Retry-After. Times are checked for causal order only
    (a send cannot follow the observed response; a result cannot be settled
    before it was observed); append times may differ.
    """

    if type(ledger) is not AccountRateLedger or type(journal) is not EvidenceJournal:
        raise HttpContractError("account_ledger_and_journal_required")
    plan = journal.plan
    bad: list[str] = []
    pending: list[str] = []
    state = ledger._state
    if ledger.account_ref != plan.account_ref:
        bad.append("account_ref_mismatch")
    if state.policy is None:
        bad.append("account_ledger_not_opened")
    attempts, slots = journal._state.attempts, state.slots
    for attempt_id, attempt in attempts.items():
        slot = slots.get(attempt_id)
        if slot is None:
            bad.append("account_ledger_missing_plan_attempt")
        elif slot.plan_sha256 != plan.sha256:
            bad.append("account_slot_plan_mismatch")
        elif state.policy is not None:
            _pair_attempt_and_slot(plan, state, attempt, slot, bad, pending)
    for slot_id, slot in slots.items():
        if slot.plan_sha256 != plan.sha256 or slot_id in attempts:
            continue
        # Stopped after the account reservation, before the plan recorded it.
        if slot.sent_at is not None:
            bad.append("account_sent_without_plan_attempt")
        elif slot.outcome is None:
            pending.append("pending_plan_attempt_record")
        # Settled as unknown without a send: an abandoned reservation (consistent).
    issues = tuple(dict.fromkeys(bad + pending))
    status = "inconsistent" if bad else "pending" if pending else "consistent"
    return LedgerReconciliation(plan.sha256, status, issues)


def _pair_attempt_and_slot(plan, state, attempt, slot, bad, pending) -> None:
    if slot.reserved_at > attempt.reserved_at:
        bad.append("account_slot_reserved_after_plan_attempt")
    if slot.sent_at is not None and (
        attempt.sent_at is None or slot.sent_at < attempt.sent_at
    ):
        bad.append("account_sent_before_plan_send")
    lease_end = state.lease_end(slot)
    if (
        attempt.state == "unknown"
        and slot.sent_at is not None
        and slot.sent_at > attempt.settled_at
    ):
        # The plan gave the attempt up before the account says it was sent.
        bad.append("account_send_after_plan_unknown")
    if attempt.status is not None:  # the plan observed an HTTP response
        outcome = _status_outcome(attempt.status)
        if slot.sent_at is None:
            bad.append("plan_result_without_account_send")
        elif slot.sent_at > attempt.settled_at:
            bad.append("account_send_after_plan_response")
        if attempt.settled_at > lease_end:
            bad.append("plan_result_after_account_lease")
        if slot.outcome is None:
            pending.append("pending_account_settlement")
            return
        if slot.outcome != outcome:
            bad.append("account_slot_outcome_mismatch")
            return
        if slot.observed_retry_after != attempt.retry_after:
            bad.append("account_retry_after_mismatch")
        if slot.settled_at < attempt.settled_at:
            bad.append("account_settled_before_plan_result")
        required = max(
            _plan_rule_wait(plan.retry, outcome),
            state.policy.wait_after(outcome),
            attempt.retry_after or 0,
        )
    elif attempt.state == "unknown":
        if slot.outcome is None:
            pending.append("pending_account_settlement")
            return
        if slot.outcome != "unknown":
            bad.append("account_result_contradicts_plan_unknown")
            return
        # The holder settles after the plan's record (write order); only an
        # explicit reclaim by another holder may come first.
        if slot.reclaimed_by is None and slot.settled_at < attempt.settled_at:
            bad.append("account_settled_before_plan_result")
        required = max(
            _plan_rule_wait(plan.retry, "unknown"), state.policy.wait_after("unknown")
        )
    else:  # the plan attempt is still open (reserved or sent)
        if slot.outcome is None:
            pending.append("pending_settlement_on_both_ledgers")
        elif slot.outcome != "unknown":
            bad.append("account_result_without_plan_result")
        elif slot.reclaimed_by is None:
            # The holder may settle only after the plan recorded its outcome.
            bad.append("account_settled_before_plan_result")
        else:
            pending.append("pending_plan_settlement")  # reclaimed: plan records unknown
        return
    if slot.effective_wait < required:
        bad.append("account_effective_wait_below_plan_rule")


def check_account_plan_consistency(
    ledger: AccountRateLedger, journal: EvidenceJournal
) -> None:
    """Raise unless the two ledgers are fully consistent (pending also raises)."""

    result = reconcile_account_and_plan(ledger, journal)
    if result.status != "consistent":
        raise HttpContractError(result.issues[0])


def _plan_side_next(state, by_plan, slot_id) -> datetime | None:
    """When the plan journal allows the next attempt after this slot's attempt."""

    slot = state.slots[slot_id]
    journal = by_plan.get(slot.plan_sha256)
    attempt = journal._state.attempts.get(slot_id) if journal is not None else None
    if attempt is None or attempt.settled_at is None:
        return None  # e.g. a reservation abandoned before the plan recorded it
    outcome = "unknown" if attempt.status is None else _status_outcome(attempt.status)
    wait = max(_plan_rule_wait(journal.plan.retry, outcome), attempt.retry_after or 0)
    return _add_seconds(attempt.settled_at, wait)


def _plan_side_waits(state, by_plan) -> tuple[str | None, datetime | None]:
    """Check each slot against the plan-side wait of the slot before it.

    Both ledgers are consistent here; this adds the waits that the plan side
    measures from its own records (for example an unknown recorded after a
    reclaim), so neither ledger's wait is shortened for the next plan.
    """

    slot_ids = list(state.slots)  # reservation order
    for previous, following in zip(slot_ids, slot_ids[1:], strict=False):
        required = _plan_side_next(state, by_plan, previous)
        if required is not None and state.slots[following].reserved_at < required:
            return "account_slot_reserved_before_plan_wait", None
    if not slot_ids:
        return None, None
    return None, _plan_side_next(state, by_plan, slot_ids[-1])


@dataclass(frozen=True)
class AccountSlotAssessment:
    """Account-wide rate state only; informational, never a send permission."""

    account_ref: str
    earliest_next_slot_at: datetime | None
    blocking_reasons: tuple[str, ...]
    account_identity_verified: bool = field(default=False, init=False)

    @property
    def account_allows_next_attempt(self) -> bool:
        return not self.blocking_reasons


def assess_account_slot(
    ledger: AccountRateLedger,
    plan: HttpAcquisitionPlan,
    *,
    now: datetime,
    plan_journals: tuple[EvidenceJournal, ...],
) -> AccountSlotAssessment:
    """Whether the shared account state allows a slot now (plan budget excluded).

    ``plan_journals`` must contain the journal of every plan that holds a slot
    in the ledger. Each is reconciled with the ledger; a missing journal, a
    pending crash state or an inconsistency blocks every plan on the account,
    so a known result of one plan can never be skipped by looking only at the
    ledger's latest event.
    """

    if type(ledger) is not AccountRateLedger or type(plan) is not HttpAcquisitionPlan:
        raise HttpContractError("account_ledger_and_plan_required")
    if type(plan_journals) is not tuple or any(
        type(journal) is not EvidenceJournal for journal in plan_journals
    ):
        raise HttpContractError("plan_journals_must_be_a_tuple_of_journals")
    current = datetime.fromisoformat(_utc(now, "now"))
    state = ledger._state
    if state.last_event_at is not None and current < state.last_event_at:
        raise HttpContractError("account_clock_regressed")
    reasons, earliest = [], None
    if ledger.account_ref != plan.account_ref:
        reasons.append("account_ref_mismatch")
    by_plan: dict[str, EvidenceJournal] = {}
    for journal in plan_journals:
        if journal.plan.sha256 in by_plan:
            reasons.append("duplicate_plan_journal")
        by_plan[journal.plan.sha256] = journal
    # Every plan with a slot, and every supplied journal (its attempts must all
    # hold slots: a stale or truncated account ledger is not a clean state).
    referenced = [slot.plan_sha256 for slot in state.slots.values()]
    all_consistent = True
    for plan_sha in dict.fromkeys([*referenced, *by_plan]):
        journal = by_plan.get(plan_sha)
        if journal is None:
            reasons.append("account_plan_journal_missing")
            all_consistent = False
            continue
        result = reconcile_account_and_plan(ledger, journal)
        if result.status != "consistent":
            all_consistent = False
            reasons.extend(f"account_ledger_{result.status}:{i}" for i in result.issues)
    plan_side_next = None
    if state.policy is not None:  # an ordering invariant, checked in every state
        try:
            issue, plan_side_next = _plan_side_waits(state, by_plan)
        except HttpContractError:
            reasons.append("account_time_not_representable")
        else:
            if issue is not None:
                all_consistent = False
                reasons.append(f"account_ledger_inconsistent:{issue}")
    if state.policy is None:
        reasons.append("account_ledger_not_opened")
    else:
        if not state.policy.covers(plan.retry):
            reasons.append("account_policy_weaker_than_plan")
        try:
            if state.open_slot is not None:
                reasons.append("account_slot_in_use")
                if current >= state.lease_end(state.slots[state.open_slot]):
                    reasons.append("account_slot_reclaimable_as_unknown")
            elif all_consistent:
                earliest = state.earliest_next_slot_at()
                if plan_side_next is not None and plan_side_next > earliest:
                    earliest = plan_side_next  # e.g. a plan record after a reclaim
                if current < earliest:
                    reasons.append("account_rate_limit_wait")
            # Otherwise the next time is not uniquely determined: no earliest.
        except HttpContractError:
            reasons.append("account_time_not_representable")
    return AccountSlotAssessment(
        ledger.account_ref, earliest, tuple(dict.fromkeys(reasons))
    )


# ------------------------------------------------------------------ live gate


@dataclass(frozen=True)
class LiveAcquisitionGate:
    """Informational assessment; the gate is closed and ``permitted`` cannot be set.

    No entry point may accept this object (or any other result object) as
    evidence of permission; see ``require_live_acquisition_permission``.
    """

    reasons: tuple[str, ...]
    permitted: bool = field(default=False, init=False)


class LiveAcquisitionClosed(HttpContractError):
    """Raised by the contract entry point; ``reasons`` is machine-readable."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("live_acquisition_gate_closed")
        self.reasons = reasons


def _code(error: HttpContractError) -> str:
    return str(error)


def _account_reasons(
    plan, verified_journal, account_ledger, related_journals, now
) -> list[str]:
    """Account-wide rate state; missing, pending or unprovable state never passes."""

    if account_ledger is None:
        return ["account_rate_state_missing"]
    if type(account_ledger) is not AccountRateLedger:
        return ["account_rate_state_required"]
    try:
        shared = AccountRateLedger(plan.account_ref, account_ledger.data)
    except HttpContractError as error:
        return [f"account_rate_state_invalid:{_code(error)}"]
    reasons, journals = [], []
    if verified_journal is not None:
        journals.append(verified_journal)
    if type(related_journals) is not tuple:
        reasons.append("related_journals_must_be_a_tuple")
        related_journals = ()
    for related in related_journals:
        if type(related) is not EvidenceJournal:
            reasons.append("related_journal_required")
            continue
        try:  # re-opened from its bytes under its own plan and approval
            journals.append(
                EvidenceJournal(related.plan, related.data, approval=related.approval)
            )
        except HttpContractError as error:
            reasons.append(f"related_journal_invalid:{_code(error)}")
    try:
        assessment = assess_account_slot(
            shared, plan, now=now, plan_journals=tuple(journals)
        )
    except HttpContractError as error:
        reasons.append(f"account_rate_state_invalid:{_code(error)}")
    else:
        reasons.extend(f"account_rate_blocked:{r}" for r in assessment.blocking_reasons)
    return reasons


def assess_live_acquisition_gate(
    plan: HttpAcquisitionPlan,
    approval: OwnerApprovalClaim | None,
    journal: EvidenceJournal,
    inventory: BodyInventory,
    receipt: ExternalReceiptClaim | None,
    *,
    now: datetime,
    calendar_evidence: CalendarDiscoveryEvidence | None = None,
    account_ledger: AccountRateLedger | None = None,
    related_journals: tuple[EvidenceJournal, ...] = (),
) -> LiveAcquisitionGate:
    """Never authorizes I/O: trusted approval/anchor and HTTP entry do not exist.

    Every input is re-evaluated here from raw evidence (the journal is re-opened
    from its bytes under the given plan and approval); earlier result objects
    are never consulted. Inconsistent evidence yields reasons, not exceptions.
    """

    if type(plan) is not HttpAcquisitionPlan:
        raise HttpContractError("plan_required")
    reasons = []
    if approval is None:
        reasons.append("owner_approval_missing")
    else:
        try:
            check_approval_scope(plan, approval, now=now)
        except HttpContractError as error:
            reasons.append(f"owner_approval_scope_or_validity_invalid:{_code(error)}")
    verified = None
    if type(journal) is not EvidenceJournal:
        reasons.append("journal_required")
    else:
        try:
            verified = EvidenceJournal(
                plan,
                journal.data,
                approval=approval if type(approval) is OwnerApprovalClaim else None,
            )
        except HttpContractError as error:
            reasons.append(f"journal_invalid:{_code(error)}")
    if receipt is None:
        reasons.append("external_receipt_missing")
    elif verified is not None:
        try:
            check_receipt_alignment(verified, inventory, receipt)
        except HttpContractError as error:
            reasons.append(f"external_receipt_content_invalid:{_code(error)}")
    if verified is not None:
        try:
            snapshot = budget_snapshot(verified, inventory, now=now)
        except HttpContractError as error:
            reasons.append(f"budget_state_invalid:{_code(error)}")
        else:
            if not snapshot.plan_allows_next_attempt:
                reasons.append("cumulative_budget_unavailable")
                reasons.extend(f"budget_blocked:{r}" for r in snapshot.blocking_reasons)
    reasons.extend(
        _account_reasons(plan, verified, account_ledger, related_journals, now)
    )
    if plan.kind == "calendar_anchored_daily":
        if calendar_evidence is None:
            reasons.append("calendar_anchor_declared_unverified")
        else:
            try:
                verify_calendar_anchor(plan, calendar_evidence)
            except HttpContractError as error:
                reasons.append(f"calendar_anchor_invalid:{_code(error)}")
    elif plan.kind == "predeclared_daily":
        reasons.append("calendar_source_declared_unverified")
    reasons.extend(
        (
            "account_identity_unverified",
            "account_shared_store_not_implemented",
            "owner_approval_authenticity_unverified",
            "independent_receipt_custody_unconfigured",
            "http_transport_not_implemented",
        )
    )
    return LiveAcquisitionGate(reasons=tuple(reasons))


def require_live_acquisition_permission(
    plan: HttpAcquisitionPlan,
    approval: OwnerApprovalClaim | None,
    journal: EvidenceJournal,
    inventory: BodyInventory,
    receipt: ExternalReceiptClaim | None,
    *,
    now: datetime,
    calendar_evidence: CalendarDiscoveryEvidence | None = None,
    account_ledger: AccountRateLedger | None = None,
    related_journals: tuple[EvidenceJournal, ...] = (),
) -> NoReturn:
    """The only contract entry a future HTTP runner may call before reserving.

    It accepts raw evidence of the exact expected types and evaluates it
    itself; a result object (``LiveAcquisitionGate``, ``ApprovalScopeCheck``,
    ``ReceiptAlignment``, ``CalendarAnchor``, ``AccountSlotAssessment``) is
    refused wherever evidence is expected. The account rate ledger is required
    evidence, together with the journal of every other plan that holds a slot
    in it (``related_journals``); without them, or when any pair is pending or
    inconsistent, the gate stays closed. In this contract PR it always raises
    ``LiveAcquisitionClosed``.
    Code running in the same process can still monkeypatch anything; this is a
    contract boundary, not a sandbox.
    """

    for value, expected in (
        (plan, HttpAcquisitionPlan),
        (journal, EvidenceJournal),
        (inventory, BodyInventory),
    ):
        if type(value) is not expected:
            raise HttpContractError("raw_evidence_required")
    for value, expected in (
        (approval, OwnerApprovalClaim),
        (receipt, ExternalReceiptClaim),
        (calendar_evidence, CalendarDiscoveryEvidence),
        (account_ledger, AccountRateLedger),
    ):
        if value is not None and type(value) is not expected:
            raise HttpContractError("raw_evidence_required")
    if type(related_journals) is not tuple or any(
        type(related) is not EvidenceJournal for related in related_journals
    ):
        raise HttpContractError("raw_evidence_required")
    gate = assess_live_acquisition_gate(
        plan,
        approval,
        journal,
        inventory,
        receipt,
        now=now,
        calendar_evidence=calendar_evidence,
        account_ledger=account_ledger,
        related_journals=related_journals,
    )
    raise LiveAcquisitionClosed(gate.reasons)
