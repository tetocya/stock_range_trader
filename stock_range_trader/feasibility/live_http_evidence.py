"""C2 pure live-evidence contracts: no transport, Store, credentials or permission.

Only immutable values and in-memory event replay are implemented. The live schema
does not reinterpret artificial v2 journals. Recorded timestamps are supplied
evidence here; I1 must obtain them from its authoritative Store clock.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta

from .http_contract import (
    MAX_WAIT_SECONDS,
    AccountRatePolicy,
    HttpContractError,
    RetryRules,
)
from .live_preflight import LiveClockPolicy, LiveReclaimEvidence, assess_live_reclaim

LIVE_PLAN_SCHEMA = "historical-feasibility-live-plan-journal-v1"
LIVE_ACCOUNT_SCHEMA = "historical-feasibility-live-account-rate-ledger-v1"
LIVE_ACCOUNT_SCHEMA_V1 = LIVE_ACCOUNT_SCHEMA
LIVE_ACCOUNT_SCHEMA_V2 = "historical-feasibility-live-account-rate-ledger-v2"
LIVE_HEADER_SCHEMA = "historical-feasibility-live-header-observation-v1"
MAX_EVENT_BYTES = 32 * 1024
MAX_INT64 = (1 << 63) - 1
ZERO_HASH = "0" * 64
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


def _fail(reason: str) -> None:
    raise HttpContractError(reason)


def _keys(value: object, expected: set[str]) -> dict:
    if type(value) is not dict or set(value) != expected:
        _fail("live_evidence_fields_invalid")
    return value


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_INT64:
        _fail("live_evidence_integer_invalid")
    return value


def _label(value: object) -> str:
    if type(value) is not str or not _LABEL.fullmatch(value):
        _fail("live_evidence_label_invalid")
    return value


def _digest_ref(value: object) -> str:
    if type(value) is not str or not _HEX.fullmatch(value):
        _fail("live_evidence_digest_invalid")
    return value


def _text(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 256
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        _fail("live_evidence_reference_invalid")
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        _fail("live_evidence_boolean_required")
    return value


def _utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        _fail("live_evidence_aware_datetime_required")
    try:
        return value.astimezone(UTC)
    except (ValueError, OverflowError):
        _fail("live_evidence_datetime_out_of_range")


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _time(value: object) -> datetime:
    if type(value) is not str:
        _fail("live_evidence_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _fail("live_evidence_timestamp_invalid")
    if _stamp(parsed) != value:
        _fail("live_evidence_timestamp_not_canonical")
    return parsed


def _add(value: datetime, seconds: int) -> datetime:
    try:
        return _utc(value) + timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        _fail("live_evidence_deadline_out_of_range")


def _canonical(value: object) -> str:
    try:
        result = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        result.encode("utf-8")
        return result
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        _fail("live_evidence_json_invalid")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            _fail("live_evidence_duplicate_json_key")
        result[key] = value
    return result


def _json_integer(value: str) -> int:
    if len(value.lstrip("-")) > 19:
        _fail("live_evidence_integer_invalid")
    parsed = int(value)
    if not -(1 << 63) <= parsed <= MAX_INT64:
        _fail("live_evidence_integer_invalid")
    return parsed


def _no_constant(value: str) -> None:
    _fail("live_evidence_nonfinite_json")


def _load(data: bytes) -> dict:
    if type(data) is not bytes or len(data) > MAX_EVENT_BYTES:
        _fail("live_evidence_record_size_invalid")
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_int=_json_integer,
            parse_constant=_no_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, HttpContractError):
            raise
        _fail("live_evidence_json_invalid")


_MONTHS = {
    name: i
    for i, name in enumerate(
        (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ),
        1,
    )
}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_LONG_DAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_DATE_PATTERNS = (
    re.compile(
        r"(?P<w>[A-Za-z]{3}), (?P<d>[0-9]{2}) (?P<m>[A-Za-z]{3}) (?P<y>[0-9]{4}) (?P<h>[0-9]{2}):(?P<n>[0-9]{2}):(?P<s>[0-9]{2}) GMT\Z"
    ),
    re.compile(
        r"(?P<w>[A-Za-z]+), (?P<d>[0-9]{2})-(?P<m>[A-Za-z]{3})-(?P<y>[0-9]{2}) (?P<h>[0-9]{2}):(?P<n>[0-9]{2}):(?P<s>[0-9]{2}) GMT\Z"
    ),
    re.compile(
        r"(?P<w>[A-Za-z]{3}) (?P<m>[A-Za-z]{3}) (?P<d> [0-9]|[0-9]{2}) (?P<h>[0-9]{2}):(?P<n>[0-9]{2}):(?P<s>[0-9]{2}) (?P<y>[0-9]{4})\Z"
    ),
)


def _http_date(raw: str, observed_at: datetime) -> datetime | None:
    """IMF-fixdate, RFC850 and asctime, with no locale or current-clock lookup."""
    for variant, pattern in enumerate(_DATE_PATTERNS):
        match = pattern.fullmatch(raw)
        if match is None:
            continue
        parts = match.groupdict()
        year = int(parts["y"])
        if variant == 1:
            year += observed_at.year // 100 * 100
            if year > observed_at.year + 50:
                year -= 100
        try:
            result = datetime(
                year,
                _MONTHS[parts["m"]],
                int(parts["d"]),
                int(parts["h"]),
                int(parts["n"]),
                int(parts["s"]),
                tzinfo=UTC,
            )
            if (
                variant == 1
                and year == observed_at.year + 50
                and (
                    result.month,
                    result.day,
                    result.hour,
                    result.minute,
                    result.second,
                    result.microsecond,
                )
                > (
                    observed_at.month,
                    observed_at.day,
                    observed_at.hour,
                    observed_at.minute,
                    observed_at.second,
                    observed_at.microsecond,
                )
            ):
                result = result.replace(year=year - 100)
        except (ValueError, KeyError):
            return None
        days = _LONG_DAYS if variant == 1 else _WEEKDAYS
        if parts["w"] != days[result.weekday()]:
            return None
        return result
    return None


@dataclass(frozen=True)
class RetryAfterParse:
    kind: str
    delay_seconds: int | None = None
    http_date: str | None = None


@dataclass(frozen=True)
class CaptureIssue:
    """Bounded diagnostic only. prefix_hex must never be decoded for logs."""

    code: str
    field_index: int
    input_length: int
    length_unit: str
    prefix_hex: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or self.code not in {
            "too_many_fields",
            "oversized",
            "non_ascii",
            "control_character",
            "invalid_type",
        }:
            _fail("live_header_capture_code_invalid")
        _integer(self.field_index)
        _integer(self.input_length)
        if type(self.length_unit) is not str or self.length_unit not in {
            "octets",
            "characters",
            "fields",
            "unknown",
        }:
            _fail("live_header_capture_unit_invalid")
        if type(self.prefix_hex) is not str or not re.fullmatch(
            r"(?:[0-9a-f]{2}){0,64}", self.prefix_hex
        ):
            _fail("live_header_capture_prefix_invalid")


@dataclass(frozen=True)
class HeaderObservation:
    observation_id: str
    status: int | None
    header_block_complete: bool
    observed_at: datetime
    retry_after_presence: str
    raw_retry_after: tuple[str, ...]
    capture_issues: tuple[CaptureIssue, ...] = ()

    def __post_init__(self) -> None:
        _label(self.observation_id)
        _bool(self.header_block_complete)
        _utc(self.observed_at)
        if self.status is None:
            if self.header_block_complete:
                _fail("live_header_status_required")
        elif type(self.status) is not int or not 200 <= self.status <= 599:
            _fail("live_header_status_invalid")
        if type(
            self.retry_after_presence
        ) is not str or self.retry_after_presence not in {
            "present",
            "absent",
            "undetermined",
        }:
            _fail("live_header_presence_invalid")
        if type(self.raw_retry_after) is not tuple or len(self.raw_retry_after) > 4:
            _fail("live_header_raw_limit")
        for raw in self.raw_retry_after:
            if (
                type(raw) is not str
                or len(raw) > 1024
                or any(not 32 <= ord(c) <= 126 for c in raw)
            ):
                _fail("live_header_raw_invalid_use_capture")
        if (
            type(self.capture_issues) is not tuple
            or len(self.capture_issues) > 5
            or any(type(c) is not CaptureIssue for c in self.capture_issues)
        ):
            _fail("live_header_capture_invalid")
        if self.retry_after_presence == "absent" and (
            not self.header_block_complete
            or self.raw_retry_after
            or self.capture_issues
        ):
            _fail("incomplete_headers_cannot_mean_absent")
        if self.retry_after_presence == "undetermined" and (
            self.header_block_complete or self.raw_retry_after or self.capture_issues
        ):
            _fail("live_header_undetermined_invalid")
        if self.retry_after_presence == "present" and not (
            self.raw_retry_after or self.capture_issues
        ):
            _fail("live_header_present_without_evidence")

    @property
    def parsed(self) -> RetryAfterParse:
        if self.capture_issues:
            return RetryAfterParse("capture_rejected")
        if len(self.raw_retry_after) > 1:
            return RetryAfterParse("duplicate_field")
        if self.retry_after_presence == "absent":
            return RetryAfterParse("absent", 0)
        if self.retry_after_presence == "undetermined":
            return RetryAfterParse("incomplete")
        raw = self.raw_retry_after[0].strip(" ")
        if re.fullmatch(r"[0-9]+", raw):
            significant = raw.lstrip("0") or "0"
            # Bound BEFORE int conversion. Retain exact values above the automatic
            # wait limit only when both the integer and its UTC deadline fit.
            remaining = datetime.max.replace(tzinfo=UTC) - _utc(self.observed_at)
            maximum = str(min(MAX_INT64, remaining.days * 86400 + remaining.seconds))
            if len(significant) > len(maximum) or (
                len(significant) == len(maximum) and significant > maximum
            ):
                return RetryAfterParse("capture_rejected")
            value = int(significant)
            return (
                RetryAfterParse("delta_seconds", value)
                if value <= MAX_WAIT_SECONDS
                else RetryAfterParse("out_of_range", value)
            )
        at = _http_date(raw, _utc(self.observed_at))
        if at is None:
            return RetryAfterParse("malformed")
        difference = (at - _utc(self.observed_at)).total_seconds()
        if difference <= 0:
            return RetryAfterParse("past_http_date", 0, _stamp(at))
        if difference > MAX_WAIT_SECONDS:
            return RetryAfterParse("out_of_range", http_date=_stamp(at))
        return RetryAfterParse("http_date", http_date=_stamp(at))

    @property
    def requires_indefinite_hold(self) -> bool:
        return not self.header_block_complete or self.parsed.kind in {
            "malformed",
            "out_of_range",
            "duplicate_field",
            "capture_rejected",
            "incomplete",
        }

    def known_deadline(self, anchor: datetime) -> datetime:
        """Keep interpretable lower bounds even when duplicate/capture evidence is bad."""
        deadline = max(_utc(anchor), _utc(self.observed_at))
        for raw in self.raw_retry_after:
            parsed = replace(self, raw_retry_after=(raw,), capture_issues=()).parsed
            if parsed.kind in {"delta_seconds", "out_of_range"} and (
                parsed.delay_seconds is not None
            ):
                deadline = max(
                    deadline, _add(max(anchor, self.observed_at), parsed.delay_seconds)
                )
            elif parsed.kind in {"http_date", "out_of_range"} and (
                parsed.http_date is not None
            ):
                deadline = max(deadline, _time(parsed.http_date))
        return deadline

    def to_dict(self) -> dict:
        return {
            "schema": LIVE_HEADER_SCHEMA,
            "observation_id": self.observation_id,
            "status": self.status,
            "header_block_complete": self.header_block_complete,
            "observed_at": _stamp(self.observed_at),
            "retry_after_presence": self.retry_after_presence,
            "raw_retry_after": list(self.raw_retry_after),
            "parsed": asdict(self.parsed),
            "capture_issues": [asdict(c) for c in self.capture_issues],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> HeaderObservation:
        _keys(
            raw,
            {
                "schema",
                "observation_id",
                "status",
                "header_block_complete",
                "observed_at",
                "retry_after_presence",
                "raw_retry_after",
                "parsed",
                "capture_issues",
            },
        )
        if raw["schema"] != LIVE_HEADER_SCHEMA:
            _fail("live_header_schema_mismatch")
        if (
            type(raw["raw_retry_after"]) is not list
            or type(raw["capture_issues"]) is not list
        ):
            _fail("live_header_arrays_required")
        issues = tuple(
            CaptureIssue(
                **_keys(
                    c,
                    {
                        "code",
                        "field_index",
                        "input_length",
                        "length_unit",
                        "prefix_hex",
                    },
                )
            )
            for c in raw["capture_issues"]
        )
        result = cls(
            raw["observation_id"],
            raw["status"],
            raw["header_block_complete"],
            _time(raw["observed_at"]),
            raw["retry_after_presence"],
            tuple(raw["raw_retry_after"]),
            issues,
        )
        if _canonical(result.to_dict()) != _canonical(raw):
            _fail("retry_after_parsed_evidence_mismatch")
        return result


def capture_headers(
    *,
    observation_id: str,
    status: int | None,
    header_block_complete: bool,
    observed_at: datetime,
    retry_after_fields: tuple[bytes | str, ...] = (),
) -> HeaderObservation:
    """Normalize supplied artificial observations without dropping abnormal evidence."""
    if type(retry_after_fields) is not tuple:
        _fail("live_header_fields_tuple_required")
    raw: list[str] = []
    issues: list[CaptureIssue] = []
    if len(retry_after_fields) > 4:
        issues.append(
            CaptureIssue("too_many_fields", 4, len(retry_after_fields), "fields", "")
        )
    for index, value in enumerate(retry_after_fields[:4]):
        if type(value) not in (bytes, str):
            issues.append(CaptureIssue("invalid_type", index, 0, "unknown", ""))
            continue
        unit = "octets" if type(value) is bytes else "characters"
        prefix = (
            value[:64]
            if type(value) is bytes
            else value[:64].encode("utf-8", errors="backslashreplace")[:64]
        )
        if len(value) > 1024:
            code = "oversized"
        elif (type(value) is bytes and any(c > 127 for c in value)) or (
            type(value) is str and not value.isascii()
        ):
            code = "non_ascii"
        else:
            text = value.decode("ascii") if type(value) is bytes else value
            code = (
                "control_character"
                if any(not 32 <= ord(c) <= 126 for c in text)
                else ""
            )
        if code:
            issues.append(CaptureIssue(code, index, len(value), unit, prefix.hex()))
        else:
            raw.append(text)
    presence = (
        "present"
        if retry_after_fields
        else ("absent" if header_block_complete else "undetermined")
    )
    return HeaderObservation(
        observation_id,
        status,
        header_block_complete,
        observed_at,
        presence,
        tuple(raw),
        tuple(issues),
    )


@dataclass(frozen=True)
class LivePlanScope:
    """Fixed content claims bound into both journals, not authenticated approval."""

    plan_sha: str
    preflight_sha: str
    account_ref: str
    approval_sha: str
    not_before: datetime
    expires_at: datetime
    retry: RetryRules
    account_policy: AccountRatePolicy
    clock_policy: LiveClockPolicy

    def __post_init__(self) -> None:
        for value in (self.plan_sha, self.preflight_sha, self.approval_sha):
            _digest_ref(value)
        _label(self.account_ref)
        if _utc(self.not_before) >= _utc(self.expires_at):
            _fail("live_scope_window_invalid")
        if (
            type(self.retry) is not RetryRules
            or type(self.account_policy) is not AccountRatePolicy
            or type(self.clock_policy) is not LiveClockPolicy
        ):
            _fail("live_scope_policy_required")
        if not self.account_policy.covers(self.retry):
            _fail("live_account_policy_weaker_than_plan")
        self.clock_policy.validate_rate_policy(self.account_policy)

    @property
    def policy_sha(self) -> str:
        return _hash(
            {
                "retry": asdict(self.retry),
                "account": asdict(self.account_policy),
                "clock": asdict(self.clock_policy),
            }
        )

    def to_dict(self) -> dict:
        return {
            "plan_sha": self.plan_sha,
            "preflight_sha": self.preflight_sha,
            "account_ref": self.account_ref,
            "approval_sha": self.approval_sha,
            "not_before": _stamp(self.not_before),
            "expires_at": _stamp(self.expires_at),
            "retry": asdict(self.retry),
            "account_policy": asdict(self.account_policy),
            "clock_policy": asdict(self.clock_policy),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> LivePlanScope:
        _keys(raw, {f.name for f in fields(cls)})
        return cls(
            raw["plan_sha"],
            raw["preflight_sha"],
            raw["account_ref"],
            raw["approval_sha"],
            _time(raw["not_before"]),
            _time(raw["expires_at"]),
            RetryRules(**_keys(raw["retry"], {f.name for f in fields(RetryRules)})),
            AccountRatePolicy(
                **_keys(
                    raw["account_policy"], {f.name for f in fields(AccountRatePolicy)}
                )
            ),
            LiveClockPolicy(
                **_keys(raw["clock_policy"], {f.name for f in fields(LiveClockPolicy)})
            ),
        )


@dataclass(frozen=True)
class AttemptBinding:
    plan_sha: str
    preflight_sha: str
    account_ref: str
    approval_sha: str
    attempt_id: str
    slot_id: str
    holder_id: str
    generation: int

    def __post_init__(self) -> None:
        for value in (self.plan_sha, self.preflight_sha, self.approval_sha):
            _digest_ref(value)
        for value in (self.account_ref, self.attempt_id, self.slot_id, self.holder_id):
            _label(value)
        _integer(self.generation, minimum=1)

    @classmethod
    def from_dict(cls, raw: dict) -> AttemptBinding:
        return cls(**_keys(raw, {f.name for f in fields(cls)}))

    def check_scope(self, scope: LivePlanScope) -> None:
        if any(
            getattr(self, name) != getattr(scope, name)
            for name in ("plan_sha", "preflight_sha", "account_ref", "approval_sha")
        ):
            _fail("header_binding_mismatch")


@dataclass(frozen=True)
class LiveHold:
    hold_id: str
    reason_code: str
    source_refs: tuple[str, ...]
    not_before: datetime | None
    release_mode: str
    indefinite: bool
    policy_sha: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        _label(self.hold_id)
        _label(self.reason_code)
        if type(self.source_refs) is not tuple or not self.source_refs:
            _fail("live_hold_sources_invalid")
        for ref in self.source_refs:
            _text(ref)
        if len(set(self.source_refs)) != len(self.source_refs):
            _fail("live_hold_sources_invalid")
        if self.not_before is not None:
            _utc(self.not_before)
        _utc(self.recorded_at)
        _digest_ref(self.policy_sha)
        _bool(self.indefinite)
        if (
            type(self.release_mode) is not str
            or self.release_mode not in {"rule_based", "manual"}
            or (self.indefinite and self.release_mode != "manual")
        ):
            _fail("live_hold_release_mode_invalid")
        if (
            self.reason_code
            in {
                "clock_anomaly",
                "restart_uncertainty",
                "ledger_inconsistency",
                "stale_generation",
            }
            and self.release_mode != "manual"
        ):
            _fail("live_hold_requires_manual_recovery")

    def to_dict(self) -> dict:
        return {
            "hold_id": self.hold_id,
            "reason_code": self.reason_code,
            "source_refs": list(self.source_refs),
            "not_before": _stamp(self.not_before)
            if self.not_before is not None
            else None,
            "release_mode": self.release_mode,
            "indefinite": self.indefinite,
            "policy_sha": self.policy_sha,
            "recorded_at": _stamp(self.recorded_at),
        }

    @property
    def hold_version_sha256(self) -> str:
        """Bind a release to this snapshot, without authenticating its review."""
        return _hash(self.to_dict())

    @classmethod
    def from_dict(cls, raw: dict) -> LiveHold:
        _keys(raw, {f.name for f in fields(cls)})
        if type(raw["source_refs"]) is not list:
            _fail("live_hold_sources_invalid")
        return cls(
            raw["hold_id"],
            raw["reason_code"],
            tuple(raw["source_refs"]),
            _time(raw["not_before"]) if raw["not_before"] is not None else None,
            raw["release_mode"],
            raw["indefinite"],
            raw["policy_sha"],
            _time(raw["recorded_at"]),
        )

    def dominates(self, older: LiveHold) -> bool:
        return (
            self.hold_id == older.hold_id
            and self.reason_code == older.reason_code
            and self.policy_sha == older.policy_sha
            and set(self.source_refs).issuperset(older.source_refs)
            and (
                older.not_before is None
                or (self.not_before is not None and self.not_before >= older.not_before)
            )
            and (older.release_mode != "manual" or self.release_mode == "manual")
            and (not older.indefinite or self.indefinite)
            and self.recorded_at >= older.recorded_at
        )


@dataclass(frozen=True)
class HoldState:
    hold: LiveHold
    released_at: datetime | None = None


@dataclass(frozen=True)
class AttemptState:
    binding: AttemptBinding
    reserved_at: datetime
    reservation_transition: str
    state: str = "reserved"
    sent_at: datetime | None = None
    send_transition: str | None = None
    header: HeaderObservation | None = None
    header_transition: str | None = None
    settled_at: datetime | None = None
    settlement_transition: str | None = None
    outcome: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.binding.plan_sha, self.binding.attempt_id


def _rule_wait(scope: LivePlanScope, *, outcome: str | None, status: int | None) -> int:
    waits: list[int] = []
    for policy in (scope.retry, scope.account_policy):
        value = policy.min_interval_seconds
        if outcome in {"unknown", "pre_send_reclaim"}:
            value = max(
                value,
                policy.min_wait_after_network_error_seconds,
                policy.min_wait_after_429_seconds,
            )
        if status == 429:
            value = max(value, policy.min_wait_after_429_seconds)
        elif status is not None and status >= 500:
            value = max(value, policy.min_wait_after_5xx_seconds)
        waits.append(value)
    return max(waits)


def required_attempt_hold(
    scope: LivePlanScope, attempt: AttemptState
) -> LiveHold | None:
    """Minimum projection, including facts retained after an unknown settlement."""
    header = attempt.header
    if header is None and attempt.settled_at is None:
        return None
    anchor = attempt.settled_at or header.observed_at
    if header is not None:
        anchor = max(anchor, header.observed_at)
    deadline = _add(
        anchor,
        _rule_wait(
            scope, outcome=attempt.outcome, status=header.status if header else None
        ),
    )
    indefinite = header.requires_indefinite_hold if header else False
    manual = indefinite or attempt.outcome in {"unknown", "pre_send_reclaim"}
    if header is not None:
        deadline = max(deadline, header.known_deadline(anchor))
    refs = [attempt.reservation_transition]
    if attempt.header_transition:
        refs.extend((attempt.header_transition, "observation:" + header.observation_id))
    if attempt.settlement_transition:
        refs.append(attempt.settlement_transition)
    return LiveHold(
        _hash(
            {
                "plan": scope.plan_sha,
                "attempt": attempt.binding.attempt_id,
                "generation": attempt.binding.generation,
            }
        ),
        "attempt_evidence",
        tuple(sorted(set(refs))),
        deadline,
        "manual" if manual else "rule_based",
        indefinite,
        scope.policy_sha,
        anchor,
    )


@dataclass(frozen=True)
class LiveProjection:
    scopes: tuple[LivePlanScope, ...]
    attempts: tuple[AttemptState, ...]
    holds: tuple[HoldState, ...]
    generation: int

    def scope(self, plan_sha: str) -> LivePlanScope:
        for scope in self.scopes:
            if scope.plan_sha == plan_sha:
                return scope
        _fail("live_plan_scope_unknown")

    def attempt(self, plan_sha: str, attempt_id: str) -> AttemptState:
        for attempt in self.attempts:
            if attempt.key == (plan_sha, attempt_id):
                return attempt
        _fail("live_attempt_unknown")


def _holds_cover(projection: LiveProjection) -> bool:
    holds = {s.hold.hold_id: s for s in projection.holds}
    for attempt in projection.attempts:
        required = required_attempt_hold(
            projection.scope(attempt.binding.plan_sha), attempt
        )
        if required is not None:
            actual = holds.get(required.hold_id)
            if actual is None or not actual.hold.dominates(required):
                return False
            if actual.released_at is not None and (
                actual.released_at < required.recorded_at
                or (
                    required.not_before is not None
                    and actual.released_at < required.not_before
                )
            ):
                return False
    return True


_PLAN_KINDS = {
    "journal_opened",
    "attempt_reserved",
    "attempt_sent",
    "response_headers_observed",
    "response_received",
    "outcome_unknown",
}
_ACCOUNT_KINDS = {
    "ledger_opened",
    "slot_reserved",
    "slot_sent",
    "slot_headers_observed",
    "slot_settled",
    "slot_reclaimed",
    "hold_entered",
    "hold_extended",
    "hold_released",
}


class _Reducer:
    """Ephemeral replay state; never exposed as a mutable journal or Store."""

    def __init__(self, schema: str) -> None:
        if schema not in {LIVE_PLAN_SCHEMA, LIVE_ACCOUNT_SCHEMA}:
            _fail("live_journal_schema_mismatch")
        self.account = schema == LIVE_ACCOUNT_SCHEMA
        self.scopes: tuple[LivePlanScope, ...] = ()
        self.attempts: dict[tuple[str, str], AttemptState] = {}
        self.holds: dict[str, HoldState] = {}
        self.generation = 0
        self.last_at: datetime | None = None
        self.transitions: set[tuple[str, str]] = set()

    def projection(self) -> LiveProjection:
        return LiveProjection(
            self.scopes,
            tuple(self.attempts.values()),
            tuple(self.holds.values()),
            self.generation,
        )

    def apply(self, event: dict) -> None:
        _keys(event, {"kind", "transition_id", "recorded_at", "binding", "data"})
        kind = event["kind"]
        if type(kind) is not str or kind not in (
            _ACCOUNT_KINDS if self.account else _PLAN_KINDS
        ):
            _fail("live_event_unknown")
        transition = _label(event["transition_id"])
        at = _time(event["recorded_at"])
        if self.last_at is not None and at < self.last_at:
            _fail("live_recorded_clock_regressed")
        if (kind, transition) in self.transitions:
            _fail("live_transition_reused")
        data = event["data"]
        if not self.scopes:
            if (
                kind != ("ledger_opened" if self.account else "journal_opened")
                or event["binding"] is not None
            ):
                _fail("live_open_event_required")
            _keys(data, {"scopes"})
            if (
                type(data["scopes"]) is not list
                or not data["scopes"]
                or (not self.account and len(data["scopes"]) != 1)
            ):
                _fail("live_journal_scope_invalid")
            scopes = tuple(LivePlanScope.from_dict(raw) for raw in data["scopes"])
            if len({s.plan_sha for s in scopes}) != len(scopes):
                _fail("live_duplicate_plan_scope")
            if (
                len(
                    {
                        (
                            s.account_ref,
                            s.preflight_sha,
                            _hash(asdict(s.account_policy)),
                        )
                        for s in scopes
                    }
                )
                != 1
            ):
                _fail("live_account_scope_mismatch")
            self.scopes = scopes
        elif kind in {"ledger_opened", "journal_opened"}:
            _fail("live_duplicate_open")
        elif kind.startswith("hold_"):
            if event["binding"] is not None:
                _fail("live_account_hold_binding_invalid")
            self._hold(kind, data, at)
        else:
            binding = AttemptBinding.from_dict(event["binding"])
            scope = self.projection().scope(binding.plan_sha)
            binding.check_scope(scope)
            self._attempt(kind, binding, data, transition, at, scope)
        self.transitions.add((kind, transition))
        self.last_at = at

    def _attempt(
        self,
        kind: str,
        binding: AttemptBinding,
        data: dict,
        transition: str,
        at: datetime,
        scope: LivePlanScope,
    ) -> None:
        key = binding.plan_sha, binding.attempt_id
        if kind in {"attempt_reserved", "slot_reserved"}:
            _keys(data, {"reserved_at"})
            reserved = _time(data["reserved_at"])
            if reserved > at or not scope.not_before <= reserved < scope.expires_at:
                _fail("live_reservation_time_invalid")
            if key in self.attempts or any(
                a.binding.slot_id == binding.slot_id for a in self.attempts.values()
            ):
                _fail("live_attempt_or_slot_reused")
            if self.account:
                if binding.generation != self.generation + 1:
                    _fail("live_generation_not_next")
                self._require_no_restrictions(reserved)
            elif binding.generation <= self.generation:
                _fail("live_generation_reused")
            self.generation = binding.generation
            self.attempts[key] = AttemptState(binding, reserved, transition)
            return
        attempt = self.attempts.get(key)
        if attempt is None:
            _fail("live_attempt_unknown")
        if attempt.binding != binding:
            _fail("header_binding_mismatch")
        if binding.generation != self.generation or attempt.state == "reclaimed":
            _fail("stale_generation_manual_hold_review_required")
        if kind in {"attempt_sent", "slot_sent"}:
            _keys(data, {"sent_at"})
            sent = _time(data["sent_at"])
            if attempt.state != "reserved" or not attempt.reserved_at <= sent <= at:
                _fail("live_send_time_or_state_invalid")
            if not scope.not_before <= sent < scope.expires_at:
                _fail("live_plan_expired_for_send")
            if (
                sent - attempt.reserved_at
            ).total_seconds() > scope.clock_policy.max_reservation_to_send_seconds:
                _fail("live_reservation_to_send_bound_exceeded")
            if self.account:
                self._require_no_restrictions(sent, ignore_open=key)
            attempt = replace(
                attempt, state="sent", sent_at=sent, send_transition=transition
            )
        elif kind in {"response_headers_observed", "slot_headers_observed"}:
            _keys(data, {"observation"})
            header = HeaderObservation.from_dict(data["observation"])
            if attempt.state != "sent" or attempt.header is not None:
                _fail("live_header_state_invalid")
            if header.observed_at < attempt.sent_at:
                _fail("header_before_send")
            if header.observed_at > at:
                _fail("live_header_after_recorded_at")
            if any(
                a.header and a.header.observation_id == header.observation_id
                for a in self.attempts.values()
            ):
                _fail("live_observation_id_reused")
            attempt = replace(
                attempt,
                state="headers_observed",
                header=header,
                header_transition=transition,
            )
        elif kind in {"response_received", "outcome_unknown", "slot_settled"}:
            _keys(data, {"outcome", "settled_at", "status"})
            outcome = data["outcome"]
            if (
                type(outcome) is not str
                or outcome not in {"response", "unknown"}
                or (kind == "response_received" and outcome != "response")
                or (kind == "outcome_unknown" and outcome != "unknown")
            ):
                _fail("live_outcome_invalid")
            settled = _time(data["settled_at"])
            if attempt.sent_at is None or attempt.state not in {
                "sent",
                "headers_observed",
            }:
                _fail("live_unknown_requires_sent_attempt")
            if settled < (
                attempt.header.observed_at if attempt.header else attempt.sent_at
            ):
                _fail("settlement_before_header_evidence")
            if settled > at:
                _fail("live_settlement_after_recorded_at")
            if outcome == "response":
                if attempt.header is None or not attempt.header.header_block_complete:
                    _fail("complete_response_requires_complete_header_evidence")
                if (
                    type(data["status"]) is not int
                    or data["status"] != attempt.header.status
                ):
                    _fail("header_status_mismatch")
            elif data["status"] is not None:
                _fail("live_unknown_final_status_forbidden")
            attempt = replace(
                attempt,
                state="response_received" if outcome == "response" else "unknown",
                outcome=outcome,
                settled_at=settled,
                settlement_transition=transition,
            )
        elif kind == "slot_reclaimed":
            _keys(data, {"evidence"})
            raw = _keys(data["evidence"], {f.name for f in fields(LiveReclaimEvidence)})
            evidence = LiveReclaimEvidence(
                _time(raw["lease_expired_at"]),
                _time(raw["observed_at"]),
                raw["clock_status"],
                raw["previous_transport_termination_reference"],
                raw["operator_review_reference"],
                raw["process_restart_detected"],
            )
            if attempt.state not in {"reserved", "sent", "headers_observed"}:
                _fail("live_reclaim_closed_slot")
            if (
                evidence.lease_expired_at
                != _add(attempt.reserved_at, scope.account_policy.slot_lease_seconds)
                or evidence.observed_at != at
            ):
                _fail("live_reclaim_time_mismatch")
            if not assess_live_reclaim(
                scope.clock_policy, evidence
            ).manual_reclaim_eligible:
                _fail("live_reclaim_manual_evidence_required")
            self.generation = _integer(self.generation + 1)
            attempt = replace(
                attempt,
                state="reclaimed",
                outcome="unknown" if attempt.sent_at else "pre_send_reclaim",
                settled_at=at,
                settlement_transition=transition,
            )
        else:
            _fail("live_event_unknown")
        self.attempts[key] = attempt

    def _require_no_restrictions(
        self, at: datetime, *, ignore_open: tuple[str, str] | None = None
    ) -> None:
        if self.last_at is not None and at < self.last_at:
            _fail("live_control_time_before_recorded_evidence")
        if any(
            a.state in {"reserved", "sent", "headers_observed"} and a.key != ignore_open
            for a in self.attempts.values()
        ):
            _fail("live_open_slot")
        if not _holds_cover(self.projection()):
            _fail("hold_projection_mismatch")
        if any(
            h.released_at is None
            or (h.hold.not_before is not None and at < h.hold.not_before)
            for h in self.holds.values()
        ):
            _fail("live_account_hold_active")

    def _hold(self, kind: str, data: dict, at: datetime) -> None:
        if kind in {"hold_entered", "hold_extended"}:
            _keys(data, {"hold"})
            hold = LiveHold.from_dict(data["hold"])
            if hold.recorded_at != at or hold.policy_sha not in {
                s.policy_sha for s in self.scopes
            }:
                _fail("live_hold_policy_or_time_mismatch")
            older = self.holds.get(hold.hold_id)
            if kind == "hold_entered" and older is not None:
                _fail("live_hold_id_reused")
            if kind == "hold_extended" and (
                older is None
                or older.released_at is not None
                or not hold.dominates(older.hold)
            ):
                _fail("live_hold_extension_weakened")
            self.holds[hold.hold_id] = HoldState(hold)
            return
        _keys(
            data,
            {
                "hold_id",
                "expected_hold_sha256",
                "clock_reference",
                "account_state_reference",
                "manual_review_reference",
                "repair_reference",
            },
        )
        hold_id = _label(data["hold_id"])
        state = self.holds.get(hold_id)
        if state is None or state.released_at is not None:
            _fail("live_hold_not_active")
        hold = state.hold
        if _digest_ref(data["expected_hold_sha256"]) != hold.hold_version_sha256:
            _fail("live_hold_version_mismatch")
        if hold.not_before is not None and at < hold.not_before:
            _fail("live_hold_known_wait_not_elapsed")
        _text(data["clock_reference"])
        _text(data["account_state_reference"])
        if hold.release_mode == "manual":
            _text(data["manual_review_reference"])
        elif data["manual_review_reference"] is not None:
            _text(data["manual_review_reference"])
        if hold.reason_code == "ledger_inconsistency":
            _text(data["repair_reference"])
        elif data["repair_reference"] is not None:
            _text(data["repair_reference"])
        if not _holds_cover(self.projection()) or any(
            a.state in {"reserved", "sent", "headers_observed"}
            for a in self.attempts.values()
        ):
            _fail("live_hold_release_unsettled_evidence")
        self.holds[hold_id] = replace(state, released_at=at)


@dataclass(frozen=True)
class LiveAccountAuthority:
    """Account-wide claims fixed at v2 opening; not authenticated identity."""

    account_ref: str
    preflight_sha: str
    account_policy: AccountRatePolicy
    clock_policy: LiveClockPolicy

    def __post_init__(self) -> None:
        _label(self.account_ref)
        _digest_ref(self.preflight_sha)
        if (
            type(self.account_policy) is not AccountRatePolicy
            or type(self.clock_policy) is not LiveClockPolicy
        ):
            _fail("live_authority_policy_required")
        self.clock_policy.validate_rate_policy(self.account_policy)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> LiveAccountAuthority:
        _keys(raw, {f.name for f in fields(cls)})
        return cls(
            raw["account_ref"],
            raw["preflight_sha"],
            AccountRatePolicy(
                **_keys(
                    raw["account_policy"], {f.name for f in fields(AccountRatePolicy)}
                )
            ),
            LiveClockPolicy(
                **_keys(raw["clock_policy"], {f.name for f in fields(LiveClockPolicy)})
            ),
        )

    def matches(self, scope: LivePlanScope) -> bool:
        return (
            self.account_ref,
            self.preflight_sha,
            self.account_policy,
            self.clock_policy,
        ) == (
            scope.account_ref,
            scope.preflight_sha,
            scope.account_policy,
            scope.clock_policy,
        )


@dataclass(frozen=True)
class PlanEnrollment:
    scope: LivePlanScope
    initial_head: str
    prior_plan_heads: tuple[tuple[str, str], ...]
    transition_id: str
    recorded_at: datetime
    previous_account_head: str
    account_enrollment_head: str


@dataclass(frozen=True)
class LiveAccountProjectionV2(LiveProjection):
    opened: bool
    authority: LiveAccountAuthority | None
    enrollment_revision: int
    enrollments: tuple[PlanEnrollment, ...]


def require_enrollment_idle(projection: LiveProjection) -> None:
    """Holds alone are legal; unresolved unknown recovery and open slots are not."""
    if any(
        a.state in {"reserved", "sent", "headers_observed"} for a in projection.attempts
    ):
        _fail("plan_enrollment_in_flight")
    for attempt in projection.attempts:
        if attempt.outcome == "unknown":
            required = required_attempt_hold(
                projection.scope(attempt.binding.plan_sha), attempt
            )
            state = next(
                (
                    s
                    for s in projection.holds
                    if required and s.hold.hold_id == required.hold_id
                ),
                None,
            )
            if state is None or state.released_at is None:
                _fail("plan_enrollment_unknown_recovery_pending")


class _AccountReducerV2(_Reducer):
    """Explicitly separate opening/enrollment semantics; v1 reducer is unchanged."""

    def __init__(self) -> None:
        super().__init__(LIVE_ACCOUNT_SCHEMA_V1)
        self.opened = False
        self.authority: LiveAccountAuthority | None = None
        self.enrollments: list[PlanEnrollment] = []
        self.previous_hash = ZERO_HASH
        self.event_hash = ZERO_HASH

    def projection(self) -> LiveAccountProjectionV2:
        return LiveAccountProjectionV2(
            self.scopes,
            tuple(self.attempts.values()),
            tuple(self.holds.values()),
            self.generation,
            self.opened,
            self.authority,
            len(self.enrollments),
            tuple(self.enrollments),
        )

    def apply(self, event: dict) -> None:
        _keys(event, {"kind", "transition_id", "recorded_at", "binding", "data"})
        kind = event["kind"]
        if type(kind) is not str or kind not in _ACCOUNT_KINDS | {"plan_enrolled"}:
            _fail("live_event_unknown")
        transition = _label(event["transition_id"])
        at = _time(event["recorded_at"])
        if self.last_at is not None and at < self.last_at:
            _fail("live_recorded_clock_regressed")
        if kind not in {"ledger_opened", "plan_enrolled"}:
            if not self.opened or not self.scopes:
                _fail("live_plan_scope_unknown")
            if any(
                t == transition and k == "plan_enrolled" for k, t in self.transitions
            ):
                _fail("live_transition_reused")
            super().apply(event)
            return
        if event["binding"] is not None:
            _fail("plan_enrollment_binding_mismatch")
        if any(t == transition for _, t in self.transitions):
            _fail("live_transition_reused")
        if kind == "ledger_opened":
            if self.opened:
                _fail("live_duplicate_open")
            _keys(event["data"], {"authority"})
            self.authority = LiveAccountAuthority.from_dict(event["data"]["authority"])
            self.opened = True
        else:
            if not self.opened:
                _fail("live_open_event_required")
            data = _keys(
                event["data"],
                {"scope", "plan_journal_initial_head", "prior_plan_heads"},
            )
            scope = LivePlanScope.from_dict(data["scope"])
            if not self.authority.matches(scope):
                _fail("plan_enrollment_binding_mismatch")
            if scope.plan_sha in {s.plan_sha for s in self.scopes}:
                _fail("plan_already_enrolled")
            if not scope.not_before <= at < scope.expires_at:
                _fail("plan_enrollment_outside_validity_window")
            require_enrollment_idle(self.projection())
            if not _holds_cover(self.projection()):
                _fail("plan_enrollment_reconciliation_required")
            raw = data["prior_plan_heads"]
            if type(raw) is not list:
                _fail("plan_enrollment_prior_heads_mismatch")
            pairs = []
            for item in raw:
                _keys(item, {"plan_sha", "head"})
                pairs.append((_digest_ref(item["plan_sha"]), _digest_ref(item["head"])))
            if [p[0] for p in pairs] != sorted(s.plan_sha for s in self.scopes):
                _fail("plan_enrollment_prior_heads_mismatch")
            initial = _digest_ref(data["plan_journal_initial_head"])
            self.enrollments.append(
                PlanEnrollment(
                    scope,
                    initial,
                    tuple(pairs),
                    transition,
                    at,
                    self.previous_hash,
                    self.event_hash,
                )
            )
            self.scopes = tuple(sorted((*self.scopes, scope), key=lambda s: s.plan_sha))
        self.transitions.add((kind, transition))
        self.last_at = at


def _replay(schema: str, records: tuple[str, ...]) -> LiveProjection:
    reducer = (
        _AccountReducerV2() if schema == LIVE_ACCOUNT_SCHEMA_V2 else _Reducer(schema)
    )
    if type(records) is not tuple or not records:
        _fail("live_journal_records_required")
    previous = ZERO_HASH
    for sequence, line in enumerate(records):
        _integer(sequence)
        if type(line) is not str:
            _fail("live_journal_record_type_invalid")
        try:
            encoded = line.encode("utf-8")
        except UnicodeError:
            _fail("live_evidence_json_invalid")
        record = _keys(
            _load(encoded),
            {"schema", "sequence", "previous_hash", "event", "event_hash"},
        )
        if record["schema"] != schema:
            _fail("live_journal_schema_mismatch")
        if (
            _integer(record["sequence"]) != sequence
            or record["previous_hash"] != previous
        ):
            _fail("live_journal_chain_invalid")
        digest = _hash({k: v for k, v in record.items() if k != "event_hash"})
        if digest != _digest_ref(record["event_hash"]):
            _fail("live_journal_hash_mismatch")
        if _canonical(record) != line:
            _fail("live_journal_noncanonical_record")
        if isinstance(reducer, _AccountReducerV2):
            reducer.previous_hash = previous
            reducer.event_hash = digest
        reducer.apply(record["event"])
        previous = digest
    return reducer.projection()


@dataclass(frozen=True)
class LiveJournal:
    """Append returns a new, fully replayed value; never writes to a file or DB."""

    schema: str
    records: tuple[str, ...]

    def __post_init__(self) -> None:
        _replay(self.schema, self.records)

    @classmethod
    def create_account_v2(
        cls,
        authority: LiveAccountAuthority,
        *,
        recorded_at: datetime,
        transition_id: str = "opened",
    ) -> LiveJournal:
        if type(authority) is not LiveAccountAuthority:
            _fail("live_account_authority_required")
        event = {
            "kind": "ledger_opened",
            "recorded_at": _stamp(recorded_at),
            "transition_id": transition_id,
            "binding": None,
            "data": {"authority": authority.to_dict()},
        }
        return cls(
            LIVE_ACCOUNT_SCHEMA_V2,
            (cls._record(LIVE_ACCOUNT_SCHEMA_V2, 0, ZERO_HASH, event),),
        )

    def prefix(self, head: str) -> LiveJournal:
        _digest_ref(head)
        for index, line in enumerate(self.records):
            if json.loads(line)["event_hash"] == head:
                return LiveJournal(self.schema, self.records[: index + 1])
        _fail("live_journal_head_unknown")

    @classmethod
    def create(
        cls,
        schema: str,
        scopes: tuple[LivePlanScope, ...],
        *,
        recorded_at: datetime,
        transition_id: str = "opened",
    ) -> LiveJournal:
        if schema not in {LIVE_PLAN_SCHEMA, LIVE_ACCOUNT_SCHEMA}:
            _fail("live_journal_schema_mismatch")
        if type(scopes) is not tuple or any(
            type(s) is not LivePlanScope for s in scopes
        ):
            _fail("live_journal_scope_invalid")
        event = {
            "kind": "journal_opened" if schema == LIVE_PLAN_SCHEMA else "ledger_opened",
            "recorded_at": _stamp(recorded_at),
            "transition_id": transition_id,
            "binding": None,
            "data": {"scopes": [s.to_dict() for s in scopes]},
        }
        return cls(schema, (cls._record(schema, 0, ZERO_HASH, event),))

    @staticmethod
    def _record(schema: str, sequence: int, previous: str, event: dict) -> str:
        value = {
            "schema": schema,
            "sequence": sequence,
            "previous_hash": previous,
            "event": event,
        }
        result = _canonical({**value, "event_hash": _hash(value)})
        if len(result.encode("utf-8")) > MAX_EVENT_BYTES:
            _fail("live_evidence_record_size_invalid")
        return result

    @property
    def head_sha(self) -> str:
        return json.loads(self.records[-1])["event_hash"]

    @property
    def projection(self) -> LiveProjection:
        return _replay(self.schema, self.records)

    def append(
        self,
        kind: str,
        *,
        recorded_at: datetime,
        transition_id: str,
        binding: AttemptBinding | None = None,
        **data: object,
    ) -> LiveJournal:
        if binding is not None and type(binding) is not AttemptBinding:
            _fail("live_attempt_binding_required")
        event = {
            "kind": kind,
            "recorded_at": _stamp(recorded_at),
            "transition_id": transition_id,
            "binding": asdict(binding) if binding else None,
            "data": data,
        }
        line = self._record(self.schema, len(self.records), self.head_sha, event)
        return LiveJournal(self.schema, (*self.records, line))

    def to_bytes(self) -> bytes:
        return ("\n".join(self.records) + "\n").encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes, *, expected_schema: str) -> LiveJournal:
        if expected_schema not in {
            LIVE_PLAN_SCHEMA,
            LIVE_ACCOUNT_SCHEMA_V1,
            LIVE_ACCOUNT_SCHEMA_V2,
        }:
            _fail("live_journal_schema_mismatch")
        if type(data) is not bytes or not data.endswith(b"\n"):
            _fail("live_journal_incomplete_tail")
        lines = data[:-1].split(b"\n")
        for line in lines:
            # Bound before JSON decoding, including gigantic numeric/string literals.
            if not line or len(line) > MAX_EVENT_BYTES:
                _fail("live_evidence_record_size_invalid")
        try:
            return cls(expected_schema, tuple(line.decode("utf-8") for line in lines))
        except UnicodeError:
            _fail("live_evidence_json_invalid")


@dataclass(frozen=True)
class LiveReconciliation:
    classification: str
    reasons: tuple[str, ...]
    live_send_permitted: bool = field(default=False, init=False)
    authenticated_account_verified: bool = field(default=False, init=False)
    owner_approval_verified: bool = field(default=False, init=False)
    store_implemented: bool = field(default=False, init=False)


def _reconcile_attempt_journals(
    account: LiveJournal,
    plan_journals: tuple[LiveJournal, ...],
    *,
    claimed_projection: LiveProjection | None = None,
) -> LiveReconciliation:
    """Compare matching attempts and transition IDs, never repair one-sided evidence."""
    if type(account) is not LiveJournal or account.schema not in {
        LIVE_ACCOUNT_SCHEMA_V1,
        LIVE_ACCOUNT_SCHEMA_V2,
    }:
        _fail("live_account_journal_required")
    if type(plan_journals) is not tuple or any(
        type(p) is not LiveJournal or p.schema != LIVE_PLAN_SCHEMA
        for p in plan_journals
    ):
        _fail("live_plan_journals_required")
    ap = account.projection
    reasons: list[str] = []
    pending: list[str] = []
    plans: dict[str, LiveProjection] = {}
    for journal in plan_journals:
        projection = journal.projection
        sha = projection.scopes[0].plan_sha
        if sha in plans:
            reasons.append("duplicate_plan_journal")
        plans[sha] = projection
    expected = {s.plan_sha: s for s in ap.scopes}
    if set(plans) != set(expected):
        reasons.append("related_plan_journals_incomplete_or_unrelated")
    for sha in plans.keys() & expected.keys():
        pp = plans[sha]
        if pp.scopes[0] != expected[sha]:
            reasons.append("live_scope_evidence_mismatch")
        p_attempts = {a.key: a for a in pp.attempts}
        a_attempts = {a.key: a for a in ap.attempts if a.binding.plan_sha == sha}
        for key in p_attempts.keys() | a_attempts.keys():
            left, right = p_attempts.get(key), a_attempts.get(key)
            if left is None or right is None:
                existing = left or right
                (pending if existing.state == "reserved" else reasons).append(
                    "pending_attempt_pair"
                    if existing.state == "reserved"
                    else "unpaired_attempt_progressed"
                )
                continue
            if left.binding != right.binding:
                reasons.append("header_binding_mismatch")
                continue
            if (left.reserved_at, left.reservation_transition) != (
                right.reserved_at,
                right.reservation_transition,
            ):
                reasons.append("reservation_evidence_mismatch")
            if (left.sent_at, left.send_transition) != (
                right.sent_at,
                right.send_transition,
            ):
                (
                    pending
                    if left.state == "reserved" or right.state == "reserved"
                    else reasons
                ).append(
                    "pending_send_pair"
                    if left.state == "reserved" or right.state == "reserved"
                    else "send_evidence_mismatch"
                )
            lh, rh = left.header, right.header
            if (lh is None) != (rh is None):
                peer = left if lh is None else right
                if peer.state in {"reserved", "sent"}:
                    pending.append("pending_header_pair")
                else:
                    reasons.append("unpaired_header_peer_progressed")
            elif lh is not None and rh is not None:
                if lh.status != rh.status:
                    reasons.append("header_status_mismatch")
                if (
                    lh.retry_after_presence,
                    lh.raw_retry_after,
                    lh.capture_issues,
                    lh.parsed,
                ) != (
                    rh.retry_after_presence,
                    rh.raw_retry_after,
                    rh.capture_issues,
                    rh.parsed,
                ):
                    reasons.append("retry_after_evidence_mismatch")
                if (
                    lh.observation_id,
                    left.header_transition,
                    lh.header_block_complete,
                ) != (
                    rh.observation_id,
                    right.header_transition,
                    rh.header_block_complete,
                ):
                    reasons.append("header_observation_mismatch")
                if lh.observed_at != rh.observed_at:
                    reasons.append("header_observation_time_mismatch")
                if any(
                    sent is not None and min(lh.observed_at, rh.observed_at) < sent
                    for sent in (left.sent_at, right.sent_at)
                ):
                    reasons.append("header_before_send")
            # A never-sent reservation reclaimed with C1 evidence has no HTTP outcome.
            abandoned = (
                right.outcome == "pre_send_reclaim"
                and left.state == "reserved"
                and left.sent_at is None
            )
            if not abandoned:
                if (left.outcome is None) != (right.outcome is None):
                    pending.append("pending_settlement_pair")
                elif left.outcome is not None and (
                    left.outcome,
                    left.settled_at,
                    left.settlement_transition,
                ) != (right.outcome, right.settled_at, right.settlement_transition):
                    reasons.append("settlement_evidence_mismatch")
            for result in (left, right):
                if result.settled_at is not None and any(
                    h and result.settled_at < h.observed_at for h in (lh, rh)
                ):
                    reasons.append("settlement_before_header_evidence")
    if not pending and not _holds_cover(ap):
        reasons.append("hold_projection_mismatch")
    if claimed_projection is not None:
        if type(claimed_projection) is not type(ap):
            _fail("live_projection_required")
        if claimed_projection.generation != ap.generation:
            reasons.append("generation_projection_mismatch")
        if claimed_projection.holds != ap.holds:
            reasons.append("hold_projection_mismatch")
        if account.schema == LIVE_ACCOUNT_SCHEMA_V2 and claimed_projection != ap:
            reasons.append("enrollment_projection_mismatch")
    classification = (
        "inconsistent" if reasons else ("pending" if pending else "consistent")
    )
    return LiveReconciliation(classification, tuple(sorted(set(reasons + pending))))


def _reconcile_enrollments(
    account: LiveJournal, plans: tuple[LiveJournal, ...]
) -> list[str]:
    reasons: list[str] = []
    by_sha = {p.projection.scopes[0].plan_sha: p for p in plans}
    enrolled = {s.plan_sha for s in account.projection.scopes}
    if enrolled - by_sha.keys():
        reasons.append("plan_enrollment_missing_journal")
    if by_sha.keys() - enrolled:
        reasons.append("plan_journal_not_enrolled")
    for item in account.projection.enrollments:
        plan = by_sha.get(item.scope.plan_sha)
        if plan is None:
            continue
        first = json.loads(plan.records[0])
        event = first["event"]
        if first["event_hash"] != item.initial_head:
            reasons.append("plan_enrollment_initial_head_mismatch")
        if plan.projection.scopes != (item.scope,):
            reasons.append("plan_enrollment_binding_mismatch")
        if event["transition_id"] != item.transition_id:
            reasons.append("plan_enrollment_transition_mismatch")
        if _time(event["recorded_at"]) != item.recorded_at:
            reasons.append("plan_enrollment_time_mismatch")
        try:
            prior = tuple(
                by_sha[sha].prefix(head) for sha, head in item.prior_plan_heads
            )
            if any(
                _time(json.loads(p.records[-1])["event"]["recorded_at"])
                > item.recorded_at
                for p in prior
            ):
                reasons.append("plan_enrollment_prior_heads_mismatch")
            prefix = account.prefix(item.previous_account_head)
            if (
                _reconcile_attempt_journals(prefix, prior).classification
                != "consistent"
            ):
                reasons.append("plan_enrollment_prior_heads_mismatch")
        except (KeyError, HttpContractError):
            reasons.append("plan_enrollment_prior_heads_mismatch")
    return reasons


def reconcile_live_journals(
    account: LiveJournal,
    plan_journals: tuple[LiveJournal, ...],
    *,
    claimed_projection: LiveProjection | None = None,
) -> LiveReconciliation:
    """Version-aware completeness and paired evidence, without repair or fallback."""
    result = _reconcile_attempt_journals(
        account, plan_journals, claimed_projection=claimed_projection
    )
    if account.schema == LIVE_ACCOUNT_SCHEMA_V2:
        reasons = _reconcile_enrollments(account, plan_journals)
        if reasons:
            return LiveReconciliation(
                "inconsistent", tuple(sorted(set((*result.reasons, *reasons))))
            )
    return result


@dataclass(frozen=True)
class LiveRestrictionAssessment:
    reasons: tuple[str, ...]
    known_not_before: datetime | None
    rule_release_candidates: tuple[str, ...]
    live_send_permitted: bool = field(default=False, init=False)


def assess_live_restrictions(
    account: LiveJournal,
    plan_journals: tuple[LiveJournal, ...],
    *,
    plan_sha: str,
    at: datetime,
) -> LiveRestrictionAssessment:
    """A declarative restriction report. Even an empty reason list grants no send."""
    now = _utc(at)
    reconciliation = reconcile_live_journals(account, plan_journals)
    projection = account.projection
    scope = projection.scope(plan_sha)
    reasons = list(reconciliation.reasons)
    clock_regressed = any(
        now < _time(json.loads(j.records[-1])["event"]["recorded_at"])
        for j in (account, *plan_journals)
    )
    if clock_regressed:
        reasons.append("live_assessment_clock_regressed")
    if reconciliation.classification != "consistent":
        reasons.append("live_evidence_not_consistent")
    if now < scope.not_before:
        reasons.append("live_plan_not_started")
    if now >= scope.expires_at:
        reasons.append("live_plan_expired_for_send")
    if any(
        a.state in {"reserved", "sent", "headers_observed"} for a in projection.attempts
    ):
        reasons.append("live_open_slot")
    known = scope.not_before
    candidates: list[str] = []
    for item in projection.holds:
        hold = item.hold
        if hold.not_before is not None:
            known = max(known, hold.not_before)
        if item.released_at is None:
            reasons.append(
                "live_manual_hold_active"
                if hold.release_mode == "manual"
                else "live_rule_hold_active"
            )
            if (
                not clock_regressed
                and hold.release_mode == "rule_based"
                and not hold.indefinite
                and (hold.not_before is None or now >= hold.not_before)
                and reconciliation.classification == "consistent"
                and not any(
                    a.state in {"reserved", "sent", "headers_observed"}
                    for a in projection.attempts
                )
                and not any(
                    h.released_at is None and h.hold.release_mode == "manual"
                    for h in projection.holds
                )
            ):
                candidates.append(hold.hold_id)
    if now < known:
        reasons.append("live_known_wait_not_elapsed")
    return LiveRestrictionAssessment(
        tuple(sorted(set(reasons))),
        known if reconciliation.classification == "consistent" else None,
        tuple(sorted(candidates)),
    )


def check_body_generation(account: LiveJournal, binding: AttemptBinding) -> None:
    """Only a pure prerequisite check. It cannot commit or authorize a body."""
    if (
        type(account) is not LiveJournal
        or account.schema not in {LIVE_ACCOUNT_SCHEMA_V1, LIVE_ACCOUNT_SCHEMA_V2}
        or type(binding) is not AttemptBinding
    ):
        _fail("live_body_generation_inputs_invalid")
    projection = account.projection
    attempt = projection.attempt(binding.plan_sha, binding.attempt_id)
    if (
        binding != attempt.binding
        or binding.generation != projection.generation
        or attempt.state == "reclaimed"
    ):
        _fail("stale_generation_manual_hold_review_required")
    if attempt.outcome != "response":
        _fail("live_body_requires_known_complete_response")
