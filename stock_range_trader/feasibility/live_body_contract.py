"""C3 pure body/storage evidence. No filesystem, network, clock or permission.

All physical observations are claims, not authenticated measurements. Immutable
events can be replayed in memory; persistence and physical safety belong to I1-I3.
Legacy artificial v2 contracts are deliberately not reinterpreted.
"""

from __future__ import annotations

import hashlib
import json
import re
import types
import unicodedata
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from typing import ClassVar, get_args, get_origin, get_type_hints

from .http_contract import (
    HTTP_OUTPUT_ROOT,
    PLAN_SCHEMA,
    HttpAcquisitionPlan,
    HttpContractError,
)
from .live_http_evidence import (
    AttemptBinding,
    LiveJournal,
    check_body_generation,
    reconcile_live_journals,
)

PREFIX = "historical-feasibility-live-"
CONTRACT_SCHEMA = PREFIX + "body-storage-contract-v1"
OBJECT_SCHEMA = PREFIX + "body-object-v1"
RECEIPT_SCHEMA = PREFIX + "body-receipt-v1"
EVENT_SCHEMA = PREFIX + "storage-budget-event-v1"
ROOT_SCHEMA = PREFIX + "physical-root-identity-v1"
MAX_RECORD_BYTES = 32 * 1024
MAX_INT64 = (1 << 63) - 1
ZERO_HASH = "0" * 64


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise HttpContractError("c3_" + reason)


def _integer(value: object, minimum: int = 0) -> int:
    _require(type(value) is int and minimum <= value <= MAX_INT64, "integer_invalid")
    return value


def _sum(values) -> int:
    return _integer(sum(_integer(v) for v in values))


def _text(value: object) -> str:
    _require(type(value) is str and 0 < len(value) <= MAX_RECORD_BYTES, "text_invalid")
    _require(
        all(unicodedata.category(c) not in {"Cc", "Cf", "Cs"} for c in value),
        "text_control_character",
    )
    return value


def _sha(value: object) -> str:
    _require(
        type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "digest_invalid"
    )
    return value


def _time(value: datetime) -> str:
    _require(type(value) is datetime and value.tzinfo is UTC, "canonical_utc_required")
    return value.isoformat(timespec="microseconds")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise HttpContractError("c3_timestamp_invalid") from None
    _require(_time(parsed) == value, "timestamp_noncanonical")
    return parsed


def _canonical(value: object) -> str:
    try:
        result = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result.encode("utf-8")
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise HttpContractError("c3_json_invalid") from None


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _json_int(value: str) -> int:
    _require(len(value.lstrip("-")) <= 19, "integer_invalid")
    parsed = int(value)
    _require(-(1 << 63) <= parsed <= MAX_INT64, "integer_invalid")
    return parsed


def _no_number(value: str):
    raise HttpContractError("c3_noninteger_json_number")


def _load(data: bytes) -> dict:
    _require(
        type(data) is bytes and 0 < len(data) <= MAX_RECORD_BYTES, "record_size_invalid"
    )
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_int=_json_int,
            parse_float=_no_number,
            parse_constant=_no_number,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, HttpContractError):
            raise
        raise HttpContractError("c3_json_invalid") from None
    _require(type(value) is dict, "json_object_required")
    return value


def _keys(value: object, expected: set[str]) -> dict:
    _require(type(value) is dict and set(value) == expected, "fields_invalid")
    return value


def _path(value: str) -> str:
    _text(value)
    _require(
        value.startswith("/")
        and value != "/"
        and not value.endswith("/")
        and not any(c in value for c in ("//", "\\", "~", "$", ":", "%"))
        and all(p not in {".", "..", ""} for p in value.split("/")[1:])
        and not any(
            value == p or value.startswith(p + "/")
            for p in ("/dev/fd", "/proc", "/dev/stdin", "/dev/stdout", "/dev/stderr")
        ),
        "output_path_noncanonical",
    )
    return value


def _encode(value):
    if isinstance(value, Value):
        return value.to_dict()
    if type(value) is AttemptBinding:
        return asdict(value)
    if type(value) is datetime:
        return _time(value)
    if type(value) is tuple:
        return [_encode(v) for v in value]
    return value


def _typed(value, annotation, *, decode=False):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is types.UnionType:
        if value is None and type(None) in args:
            return None
        return _typed(
            value, next(a for a in args if a is not type(None)), decode=decode
        )
    if origin is tuple:
        _require(type(value) is (list if decode else tuple), "tuple_required")
        _require(len(args) == 2 and args[1] is Ellipsis, "tuple_schema_invalid")
        return tuple(_typed(v, args[0], decode=decode) for v in value)
    if annotation is datetime:
        if decode:
            return _parse_time(value)
        _time(value)
    elif annotation is int:
        _integer(value)
    elif annotation is str:
        _text(value)
    elif annotation is bool:
        _require(type(value) is bool, "boolean_required")
    elif annotation is AttemptBinding:
        if decode:
            value = AttemptBinding.from_dict(value)
        _require(type(value) is AttemptBinding, "attempt_binding_required")
        for text in asdict(value).values():
            if type(text) is str:
                _text(text)
    elif isinstance(annotation, type) and issubclass(annotation, Value):
        if decode:
            return annotation.from_dict(value)
        _require(type(value) is annotation, "value_type_invalid")
    else:
        _require(False, "unsupported_value_type")
    return value


class Value:
    """Strict immutable schema codec; strings/tuples contain no mutable aliases."""

    schema: ClassVar[str]

    def __post_init__(self):
        hints = get_type_hints(type(self))
        for f in fields(self):
            _typed(getattr(self, f.name), hints[f.name])

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            **{f.name: _encode(getattr(self, f.name)) for f in fields(self)},
        }

    def to_bytes(self) -> bytes:
        data = _canonical(self.to_dict()).encode("utf-8")
        _require(len(data) <= MAX_RECORD_BYTES, "record_size_invalid")
        return data

    @property
    def sha256(self) -> str:
        return _hash(self.to_dict())

    @classmethod
    def from_dict(cls, raw: dict):
        _keys(raw, {"schema", *(f.name for f in fields(cls))})
        _require(raw["schema"] == cls.schema, "schema_invalid")
        hints = get_type_hints(cls)
        for f in fields(cls):
            if not f.init:
                _require(raw[f.name] is False, "permission_must_be_false")
        return cls(
            **{
                f.name: _typed(raw[f.name], hints[f.name], decode=True)
                for f in fields(cls)
                if f.init
            }
        )

    @classmethod
    def from_bytes(cls, data: bytes):
        value = cls.from_dict(_load(data))
        _require(value.to_bytes() == data, "noncanonical_record")
        return value


@dataclass(frozen=True)
class FixedPlanSnapshot(Value):
    """Detached canonical content, not a new plan or authenticated approval.

    Use from_plan with an already constructed fixed plan. Loading a journal also
    requires the externally expected contract; a self-hash is not a trust anchor.
    No legacy plan constructor or path lookup runs during C3 decoding.
    """

    schema: ClassVar[str] = PREFIX + "body-plan-snapshot-v1"
    canonical_plan: str
    plan_sha: str

    def __post_init__(self):
        super().__post_init__()
        _sha(self.plan_sha)
        raw = _load(self.canonical_plan.encode("utf-8"))
        _keys(
            raw,
            {
                "schema",
                "purpose",
                "provider",
                "api_version",
                "artifact_id",
                "scope",
                "output_dir",
                "not_before",
                "expires_at",
                "limits",
                "retry",
                "account_ref",
                "auth_reference",
                "resume_policy",
                "expiry_policy",
            },
        )
        _require(
            raw["schema"] == PLAN_SCHEMA
            and _canonical(raw) == self.canonical_plan
            and _hash(raw) == self.plan_sha,
            "plan_snapshot_mismatch",
        )
        _path(raw["output_dir"])
        # Match the legacy dedicated-directory rule without invoking its
        # filesystem checks. Neither containment nor a valid hash is enough
        # when the directory does not belong to this exact artifact ID.
        artifact_id = raw["artifact_id"]
        _require(
            type(artifact_id) is str
            and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", artifact_id),
            "artifact_id_invalid",
        )
        _require(
            raw["output_dir"] == str(HTTP_OUTPUT_ROOT) + "/" + artifact_id,
            "output_dir_outside_dedicated_http_root",
        )
        _text(raw["account_ref"])
        _require(
            _parse_time(raw["not_before"]) < _parse_time(raw["expires_at"]),
            "plan_window_invalid",
        )
        _require(
            type(raw["scope"]) is dict
            and type(raw["scope"].get("queries")) is list
            and raw["scope"]["queries"],
            "plan_queries_required",
        )

    @classmethod
    def from_plan(cls, plan: HttpAcquisitionPlan):
        _require(type(plan) is HttpAcquisitionPlan, "fixed_plan_required")
        plan.verify_fixed_scope()
        return cls(_canonical(plan.to_dict()), plan.sha256)

    @property
    def content(self) -> dict:
        return _load(self.canonical_plan.encode("utf-8"))


@dataclass(frozen=True)
class StoragePolicy(Value):
    schema: ClassVar[str] = PREFIX + "body-storage-policy-v1"
    per_object_max_bytes: int
    cumulative_budget: int
    retained_body_budget: int
    reserved_capacity_budget: int
    operational_disk_limit: int

    def __post_init__(self):
        super().__post_init__()
        for f in fields(self):
            _integer(getattr(self, f.name), 1)
        _require(
            self.per_object_max_bytes
            <= min(
                self.cumulative_budget,
                self.retained_body_budget,
                self.reserved_capacity_budget,
            ),
            "policy_below_object_maximum",
        )


@dataclass(frozen=True)
class LiveBodyStorageContract(Value):
    schema: ClassVar[str] = CONTRACT_SCHEMA
    plan: FixedPlanSnapshot
    preflight_sha: str
    policy: StoragePolicy

    def __post_init__(self):
        super().__post_init__()
        _sha(self.preflight_sha)

    @property
    def root(self) -> str:
        return self.plan.content["output_dir"] + "/live-body-v1"

    @property
    def root_binding_sha(self) -> str:
        return _hash({"contract": self.sha256, "root": self.root})

    def layout(self) -> dict[str, str]:
        return {
            key: self.root + "/" + key
            for key in ("staging", "objects", "quarantine", "receipts")
        }


@dataclass(frozen=True)
class PhysicalRootIdentity(Value):
    """Caller-provided observation. It does not perform stat/resolve or prove truth."""

    schema: ClassVar[str] = ROOT_SCHEMA
    root_binding_sha: str
    owner_store_ref: str
    canonical_realpath: str
    device_id: int
    inode: int
    mount_ref: str
    root_epoch: int
    observed_at: datetime
    evidence_ref: str

    def __post_init__(self):
        super().__post_init__()
        _sha(self.root_binding_sha)
        _path(self.canonical_realpath)
        _integer(self.root_epoch, 1)


@dataclass(frozen=True)
class LivePageIdentity(Value):
    schema: ClassVar[str] = PREFIX + "body-page-v1"
    plan_sha: str
    query_json: str
    page_index: int
    cursor: str | None

    def __post_init__(self):
        super().__post_init__()
        _sha(self.plan_sha)
        query = _load(self.query_json.encode("utf-8"))
        _keys(query, {"endpoint", "params"})
        _require(_canonical(query) == self.query_json, "query_noncanonical")
        _require((self.page_index == 0) == (self.cursor is None), "page_cursor_invalid")

    @property
    def query_id(self) -> str:
        return _hash(_load(self.query_json.encode("utf-8")))

    @property
    def page_id(self) -> str:
        return self.sha256

    @property
    def cursor_key(self) -> str:
        return _hash(
            {"plan": self.plan_sha, "query": self.query_id, "cursor": self.cursor}
        )


@dataclass(frozen=True)
class ObjectIdentity(Value):
    schema: ClassVar[str] = PREFIX + "body-object-identity-v1"
    binding: AttemptBinding
    page: LivePageIdentity
    body_role: str = "decoded_response_body"

    def __post_init__(self):
        super().__post_init__()
        _require(
            self.binding.plan_sha == self.page.plan_sha
            and self.body_role == "decoded_response_body",
            "object_identity_invalid",
        )

    @property
    def object_id(self) -> str:
        return self.sha256


@dataclass(frozen=True)
class WriterEvidence(Value):
    schema: ClassVar[str] = PREFIX + "body-writer-evidence-v1"
    binding: AttemptBinding
    writer_id: str
    state: str
    observed_at: datetime
    observed_bytes: int | None
    close_ref: str | None = None
    lease_reclaim_ref: str | None = None

    def __post_init__(self):
        super().__post_init__()
        _require(
            self.state in {"not_started", "open", "closed", "unknown"},
            "writer_state_invalid",
        )
        _require(
            (self.state == "closed") == (self.close_ref is not None),
            "writer_close_evidence_required",
        )


@dataclass(frozen=True)
class StableBodyEvidence(Value):
    """I3 must actually close all writers and measure; C3 checks declared order."""

    schema: ClassVar[str] = PREFIX + "body-stability-evidence-v1"
    object_id: str
    root_identity_sha: str
    writer_evidence_sha: str
    physical_checked_at: datetime
    stable_at: datetime
    recounted_at: datetime
    rehashed_at: datetime
    size: int
    digest: str
    physical_object_ref: str
    stability_ref: str
    recount_ref: str
    rehash_ref: str

    def __post_init__(self):
        super().__post_init__()
        for name in ("object_id", "root_identity_sha", "writer_evidence_sha", "digest"):
            _sha(getattr(self, name))
        _require(
            self.physical_checked_at
            <= self.stable_at
            <= self.recounted_at
            <= self.rehashed_at,
            "stability_order_invalid",
        )


@dataclass(frozen=True)
class C2Heads(Value):
    """References to full immutable C2 prefixes, checked against supplied journals."""

    schema: ClassVar[str] = PREFIX + "body-c2-heads-v1"
    account_head: str
    plan_heads: tuple[str, ...]

    def __post_init__(self):
        super().__post_init__()
        _sha(self.account_head)
        for head in self.plan_heads:
            _sha(head)
        _require(
            bool(self.plan_heads)
            and tuple(sorted(set(self.plan_heads))) == self.plan_heads,
            "plan_heads_invalid",
        )


@dataclass(frozen=True)
class LiveBodyReceipt(Value):
    schema: ClassVar[str] = RECEIPT_SCHEMA
    contract_sha: str
    identity: ObjectIdentity
    root_identity_sha: str
    stability_sha: str
    final_digest: str
    size: int
    committed_at: datetime
    c2_heads: C2Heads
    next_cursor: str | None
    transition_id: str
    state: str = "committed"

    def __post_init__(self):
        super().__post_init__()
        for name in (
            "contract_sha",
            "root_identity_sha",
            "stability_sha",
            "final_digest",
        ):
            _sha(getattr(self, name))
        _require(self.state == "committed", "receipt_state_invalid")


@dataclass(frozen=True)
class QuarantineEvidence(Value):
    schema: ClassVar[str] = PREFIX + "body-quarantine-receipt-v1"
    operation_id: str
    contract_sha: str
    identity: ObjectIdentity
    object_version: int
    root_identity_sha: str
    original_path: str
    quarantine_path: str
    observed_size: int | None
    observed_digest: str | None
    digest_class: str
    prior_state: str
    reason: str
    evidence_ref: str
    writer: WriterEvidence
    recorded_at: datetime

    def __post_init__(self):
        super().__post_init__()
        _sha(self.contract_sha)
        _sha(self.root_identity_sha)
        _path(self.original_path)
        _path(self.quarantine_path)
        if self.observed_digest is not None:
            _sha(self.observed_digest)
        _require(self.digest_class in {"provisional", "final"}, "digest_class_invalid")
        _require(self.prior_state in {"partial", "orphan"}, "quarantine_state_invalid")
        _require(
            self.writer.binding == self.identity.binding, "writer_binding_mismatch"
        )


@dataclass(frozen=True)
class LiveBodyObject(Value):
    schema: ClassVar[str] = OBJECT_SCHEMA
    identity: ObjectIdentity
    state: str
    version: int
    reservation: int
    observed_size: int | None
    observed_digest: str | None
    writer: WriterEvidence
    acquisition: str = "unresolved"
    complete_size: int | None = None
    stability: StableBodyEvidence | None = None
    receipt: LiveBodyReceipt | None = None
    quarantine: QuarantineEvidence | None = None
    retained_lower_bound: int = 0

    def __post_init__(self):
        super().__post_init__()
        _require(
            self.state
            in {"reserved", "staging", "partial", "orphan", "committed", "quarantine"},
            "object_state_invalid",
        )
        _require(
            self.writer.binding == self.identity.binding, "writer_binding_mismatch"
        )
        _require(
            self.acquisition in {"unresolved", "complete"}, "acquisition_state_invalid"
        )
        _require(
            (self.acquisition == "complete") == (self.complete_size is not None),
            "complete_size_invalid",
        )
        _require(
            (self.state == "committed") == (self.receipt is not None),
            "receipt_state_mismatch",
        )
        _require(
            (self.state == "quarantine") == (self.quarantine is not None),
            "quarantine_state_mismatch",
        )
        if self.observed_digest is not None:
            _sha(self.observed_digest)

    @property
    def stable(self) -> bool:
        return self.writer.state == "closed" and self.stability is not None


@dataclass(frozen=True)
class LiveStorageProjection(Value):
    schema: ClassVar[str] = PREFIX + "body-storage-projection-v1"
    objects: tuple[LiveBodyObject, ...]
    retained_body_bytes: int | None
    retained_observed_minimum_bytes: int
    reserved_remaining_bytes: int
    cumulative_acquired_bytes: int
    unresolved_acquisition_charge: int
    unstable: bool
    unknown_size_objects: int


def _projection(objects: dict[str, LiveBodyObject]) -> LiveStorageProjection:
    ordered = tuple(objects[k] for k in sorted(objects))
    return LiveStorageProjection(
        ordered,
        None
        if any(o.observed_size is None for o in ordered)
        else _sum(o.observed_size for o in ordered),
        _sum(o.retained_lower_bound for o in ordered),
        _sum(
            0 if o.stable else max(0, o.reservation - (o.observed_size or 0))
            for o in ordered
        ),
        _sum(o.complete_size or 0 for o in ordered),
        _sum(o.reservation for o in ordered if o.acquisition == "unresolved"),
        any(
            o.writer.state in {"open", "unknown"}
            or (o.writer.state == "closed" and not o.stable)
            or o.observed_size is None
            for o in ordered
        ),
        sum(o.observed_size is None for o in ordered),
    )


@dataclass(frozen=True)
class BodyAssessment(Value):
    schema: ClassVar[str] = PREFIX + "body-assessment-v1"
    classification: str
    reasons: tuple[str, ...]
    live_send_permitted: bool = field(default=False, init=False)
    live_acquisition_permitted: bool = field(default=False, init=False)
    identity_verified: bool = field(default=False, init=False)
    store_implemented: bool = field(default=False, init=False)

    def __post_init__(self):
        super().__post_init__()
        _require(
            self.classification in {"consistent", "pending", "inconsistent"},
            "classification_invalid",
        )


def _heads(account: LiveJournal, plans: tuple[LiveJournal, ...]) -> C2Heads:
    result = reconcile_live_journals(account, plans)
    _require(result.classification == "consistent", "c2_" + result.classification)
    return C2Heads(account.head_sha, tuple(sorted(p.head_sha for p in plans)))


def _scope_check(contract, account):
    scope = account.projection.scope(contract.plan.plan_sha)
    raw = contract.plan.content
    _require(
        scope.preflight_sha == contract.preflight_sha
        and scope.account_ref == raw["account_ref"]
        and _time(scope.not_before) == raw["not_before"]
        and _time(scope.expires_at) == raw["expires_at"]
        and asdict(scope.retry) == raw["retry"],
        "c2_scope_mismatch",
    )
    return scope


def _c2_check(contract, identity, kind, at, account, plans):
    heads = _heads(account, plans)
    _require(
        all(
            _parse_time(json.loads(j.records[-1])["event"]["recorded_at"]) <= at
            for j in (account, *plans)
        ),
        "c2_head_recorded_after_body_event",
    )
    scope = _scope_check(contract, account)
    identity.binding.check_scope(scope)
    attempt = account.projection.attempt(
        identity.binding.plan_sha, identity.binding.attempt_id
    )
    _require(attempt.binding == identity.binding, "c2_attempt_mismatch")
    if kind == "reserved":
        _require(
            attempt.state == "reserved"
            and attempt.binding.generation == account.projection.generation
            and attempt.reserved_at <= at,
            "c2_reservation_mismatch",
        )
    else:
        _require(
            attempt.sent_at is not None
            and attempt.settled_at is not None
            and attempt.settled_at <= at
            and attempt.outcome == "response",
            "c2_complete_response_required",
        )
        if kind == "committed":
            check_body_generation(account, identity.binding)
            _require(
                attempt.header is not None and attempt.header.status == 200,
                "c2_success_response_required",
            )
    return heads


def _prefix(journal: LiveJournal, head: str) -> LiveJournal:
    for i, line in enumerate(journal.records):
        if json.loads(line)["event_hash"] == head:
            return LiveJournal(journal.schema, journal.records[: i + 1])
    raise HttpContractError("c3_required_c2_head_missing")


def _historical_c2(heads: C2Heads, account, plans):
    a = _prefix(account, heads.account_head)
    selected = []
    for head in heads.plan_heads:
        matches = []
        for plan in plans:
            try:
                matches.append(_prefix(plan, head))
            except HttpContractError as exc:
                if str(exc) != "c3_required_c2_head_missing":
                    raise
        _require(len(matches) == 1, "required_c2_head_missing")
        selected.append(matches[0])
    return a, tuple(selected)


def _capacity(projection, policy, amount):
    _require(
        not projection.unstable and projection.retained_body_bytes is not None,
        "unstable_writer_or_size",
    )
    _require(
        _sum(
            (
                projection.retained_body_bytes,
                projection.reserved_remaining_bytes,
                amount,
            )
        )
        <= policy.retained_body_budget,
        "retained_budget_exceeded",
    )
    _require(
        _sum((projection.reserved_remaining_bytes, amount))
        <= policy.reserved_capacity_budget,
        "reserved_budget_exceeded",
    )
    _require(
        _sum(
            (
                projection.cumulative_acquired_bytes,
                projection.unresolved_acquisition_charge,
                amount,
            )
        )
        <= policy.cumulative_budget,
        "cumulative_budget_exceeded",
    )


def _object_path(contract, obj):
    directory = "objects" if obj.state in {"orphan", "committed"} else "staging"
    return contract.layout()[directory] + "/" + obj.identity.object_id


@dataclass(frozen=True)
class BodyEvent(Value):
    schema: ClassVar[str] = EVENT_SCHEMA
    operation_id: str
    kind: str
    recorded_at: datetime
    object_id: str | None
    payload_json: str
    c2_heads: C2Heads | None

    def __post_init__(self):
        super().__post_init__()
        if self.object_id is not None:
            _sha(self.object_id)
        _require(
            _canonical(_load(self.payload_json.encode("utf-8"))) == self.payload_json,
            "event_payload_noncanonical",
        )


def _apply(contract, root, objects, event):
    data = _load(event.payload_json.encode("utf-8"))
    kind, oid, at = event.kind, event.object_id, event.recorded_at
    _require(
        (event.c2_heads is not None) == (kind in {"reserved", "acquired", "committed"}),
        "event_c2_evidence_invalid",
    )
    if kind == "audit":
        _keys(data, {"evidence_ref"})
        _text(data["evidence_ref"])
        _require(oid is None, "audit_object_invalid")
        return
    if kind == "reserved":
        identity = ObjectIdentity.from_dict(data)
        _require(
            oid == identity.object_id and oid not in objects, "object_already_exists"
        )
        _require(
            identity.binding.plan_sha == contract.plan.plan_sha
            and identity.binding.preflight_sha == contract.preflight_sha
            and identity.binding.account_ref == contract.plan.content["account_ref"],
            "object_scope_mismatch",
        )
        page = identity.page
        _require(
            _load(page.query_json.encode("utf-8"))
            in contract.plan.content["scope"]["queries"],
            "query_outside_plan",
        )
        _require(
            _parse_time(contract.plan.content["not_before"])
            <= at
            < _parse_time(contract.plan.content["expires_at"]),
            "reservation_outside_plan_window",
        )
        existing = tuple(objects.values())
        _require(
            all(
                o.identity.binding.attempt_id != identity.binding.attempt_id
                for o in existing
            ),
            "attempt_already_has_object",
        )
        _require(
            all(
                o.identity.binding.slot_id != identity.binding.slot_id for o in existing
            ),
            "slot_already_has_object",
        )
        _require(
            not any(
                o.identity.page.page_id == page.page_id and o.receipt is not None
                for o in existing
            ),
            "page_already_committed",
        )
        _require(
            not any(
                o.identity.page.cursor_key == page.cursor_key
                and o.identity.page.page_id != page.page_id
                for o in existing
            ),
            "pagination_cursor_revisited",
        )
        if page.page_index:
            previous = [
                o
                for o in existing
                if o.receipt is not None
                and o.identity.page.query_id == page.query_id
                and o.identity.page.page_index == page.page_index - 1
            ]
            _require(
                len(previous) == 1 and previous[0].receipt.next_cursor == page.cursor,
                "page_predecessor_missing",
            )
        _capacity(
            _projection(objects), contract.policy, contract.policy.per_object_max_bytes
        )
        objects[oid] = LiveBodyObject(
            identity,
            "reserved",
            0,
            contract.policy.per_object_max_bytes,
            0,
            None,
            WriterEvidence(
                identity.binding, identity.binding.attempt_id, "not_started", at, 0
            ),
        )
        return
    _require(oid in objects, "object_unknown")
    obj = objects[oid]
    _require(obj.state != "committed", "committed_write_once")
    if kind == "writer":
        writer = WriterEvidence.from_dict(data)
        _require(
            writer.binding == obj.identity.binding
            and writer.observed_at == at
            and writer.observed_bytes == obj.observed_size,
            "writer_evidence_mismatch",
        )
        if obj.writer.state == "not_started":
            _require(
                writer.state in {"open", "unknown", "closed"},
                "writer_transition_invalid",
            )
        else:
            _require(
                writer.writer_id == obj.writer.writer_id
                and obj.writer.state != "closed"
                and writer.state in {"unknown", "closed"},
                "writer_transition_invalid",
            )
        obj = replace(
            obj,
            writer=writer,
            state="staging"
            if obj.state == "reserved" and writer.state == "open"
            else obj.state,
        )
    elif kind == "observed":
        _keys(data, {"size", "digest", "state", "evidence_ref"})
        _text(data["evidence_ref"])
        _require(
            obj.writer.state in {"open", "unknown"},
            "observation_requires_unstable_writer",
        )
        size, digest, state = data["size"], data["digest"], data["state"]
        if size is not None:
            _integer(size)
            _require(size >= obj.retained_lower_bound, "retained_size_regressed")
        if digest is not None:
            _sha(digest)
        _require(
            state in {"staging", "partial", "orphan", "quarantine"},
            "observation_state_invalid",
        )
        _require(
            (obj.state == "quarantine") == (state == "quarantine"),
            "quarantine_state_mismatch",
        )
        # Even over-limit discovered bytes must be retained as evidence, never hidden.
        obj = replace(
            obj,
            state=state,
            observed_size=size,
            observed_digest=digest,
            writer=replace(obj.writer, observed_bytes=size),
            retained_lower_bound=max(obj.retained_lower_bound, size or 0),
        )
    elif kind == "stable":
        stable = StableBodyEvidence.from_dict(data)
        _require(obj.writer.state == "closed", "writer_close_required")
        _require(
            stable.object_id == oid
            and stable.root_identity_sha == root.sha256
            and stable.writer_evidence_sha == obj.writer.sha256,
            "stability_binding_mismatch",
        )
        _require(
            obj.writer.observed_at <= stable.physical_checked_at
            and stable.rehashed_at <= at,
            "stability_order_invalid",
        )
        _require(stable.size >= obj.retained_lower_bound, "retained_size_regressed")
        _require(obj.stability is None, "stability_already_final")
        _require(
            obj.complete_size is None or obj.complete_size == stable.size,
            "complete_size_mismatch",
        )
        obj = replace(
            obj,
            stability=stable,
            observed_size=stable.size,
            observed_digest=stable.digest,
            retained_lower_bound=stable.size,
        )
    elif kind == "acquired":
        _keys(data, {"size", "evidence_ref"})
        size = _integer(data["size"])
        _text(data["evidence_ref"])
        _require(obj.acquisition == "unresolved", "acquisition_already_resolved")
        _require(
            size >= (obj.observed_size or 0)
            and (obj.stability is None or size == obj.stability.size),
            "complete_size_mismatch",
        )
        obj = replace(obj, acquisition="complete", complete_size=size)
    elif kind == "committed":
        receipt = LiveBodyReceipt.from_dict(data)
        _require(
            obj.stable and obj.acquisition == "complete" and obj.state != "quarantine",
            "finalization_evidence_missing",
        )
        _require(
            receipt.contract_sha == contract.sha256
            and receipt.identity == obj.identity
            and receipt.root_identity_sha == root.sha256
            and receipt.stability_sha == obj.stability.sha256
            and receipt.size == obj.stability.size == obj.complete_size
            and receipt.final_digest == obj.stability.digest
            and receipt.committed_at == at
            and receipt.c2_heads == event.c2_heads
            and receipt.transition_id == event.operation_id,
            "receipt_object_mismatch",
        )
        _require(receipt.size <= obj.reservation, "object_maximum_exceeded")
        _require(
            not any(
                o.receipt and o.identity.page.page_id == obj.identity.page.page_id
                for o in objects.values()
            ),
            "page_already_committed",
        )
        if receipt.next_cursor is not None:
            _require(
                not any(
                    o.identity.page.query_id == obj.identity.page.query_id
                    and o.identity.page.cursor == receipt.next_cursor
                    for o in objects.values()
                ),
                "pagination_cursor_revisited",
            )
        obj = replace(obj, state="committed", receipt=receipt)
    elif kind == "quarantined":
        q = QuarantineEvidence.from_dict(data)
        _require(
            obj.state in {"partial", "orphan"}, "quarantine_requires_partial_or_orphan"
        )
        _require(
            q.operation_id == event.operation_id
            and q.contract_sha == contract.sha256
            and q.identity == obj.identity
            and q.object_version == obj.version
            and q.root_identity_sha == root.sha256
            and q.original_path == _object_path(contract, obj)
            and q.quarantine_path == contract.layout()["quarantine"] + "/" + oid
            and q.observed_size == obj.observed_size
            and q.observed_digest == obj.observed_digest
            and q.prior_state == obj.state
            and q.writer == obj.writer
            and q.recorded_at == at
            and q.digest_class == ("final" if obj.stable else "provisional"),
            "quarantine_evidence_mismatch",
        )
        obj = replace(obj, state="quarantine", quarantine=q)
    else:
        raise HttpContractError("c3_unknown_event_kind")
    objects[oid] = replace(obj, version=_integer(obj.version + 1))


def _replay(contract, root, records):
    _require(
        type(contract) is LiveBodyStorageContract
        and type(root) is PhysicalRootIdentity,
        "contract_and_root_required",
    )
    _require(
        root.root_binding_sha == contract.root_binding_sha
        and root.canonical_realpath == contract.root,
        "physical_root_binding_mismatch",
    )
    _require(type(records) is tuple, "records_tuple_required")
    previous, last = ZERO_HASH, root.observed_at
    objects, operations = {}, {}
    for sequence, line in enumerate(records):
        _require(type(line) is str, "record_type_invalid")
        raw = _load(line.encode("utf-8"))
        _keys(
            raw,
            {
                "schema",
                "sequence",
                "previous_hash",
                "contract_sha",
                "root_identity_sha",
                "event",
                "event_hash",
            },
        )
        _require(raw["schema"] == EVENT_SCHEMA, "schema_invalid")
        _require(
            _integer(raw["sequence"]) == sequence
            and raw["previous_hash"] == previous
            and raw["contract_sha"] == contract.sha256
            and raw["root_identity_sha"] == root.sha256,
            "chain_binding_mismatch",
        )
        _require(
            _hash({k: v for k, v in raw.items() if k != "event_hash"})
            == _sha(raw["event_hash"])
            and _canonical(raw) == line,
            "chain_hash_mismatch",
        )
        event = BodyEvent.from_dict(raw["event"])
        _require(event.operation_id not in operations, "duplicate_operation")
        _require(event.recorded_at >= last, "clock_regressed")
        _apply(contract, root, objects, event)
        operations[event.operation_id] = event
        previous, last = raw["event_hash"], event.recorded_at
    return _projection(objects), operations


@dataclass(frozen=True)
class LiveBodyJournal:
    """In-memory, append-only value. Append returns a new journal, never writes."""

    contract: LiveBodyStorageContract
    root: PhysicalRootIdentity
    records: tuple[str, ...] = ()

    def __post_init__(self):
        _replay(self.contract, self.root, self.records)

    @property
    def projection(self):
        return _replay(self.contract, self.root, self.records)[0]

    @property
    def head_sha(self):
        return json.loads(self.records[-1])["event_hash"] if self.records else ZERO_HASH

    def object(self, object_id: str) -> LiveBodyObject:
        for obj in self.projection.objects:
            if obj.identity.object_id == object_id:
                return obj
        raise HttpContractError("c3_object_unknown")

    def append(self, event: BodyEvent) -> LiveBodyJournal:
        _require(type(event) is BodyEvent, "event_required")
        _, operations = _replay(self.contract, self.root, self.records)
        if event.operation_id in operations:
            _require(
                operations[event.operation_id] == event, "operation_evidence_conflict"
            )
            return self
        raw = {
            "schema": EVENT_SCHEMA,
            "sequence": len(self.records),
            "previous_hash": self.head_sha,
            "contract_sha": self.contract.sha256,
            "root_identity_sha": self.root.sha256,
            "event": event.to_dict(),
        }
        raw["event_hash"] = _hash(raw)
        line = _canonical(raw)
        _require(len(line.encode("utf-8")) <= MAX_RECORD_BYTES, "record_size_invalid")
        return LiveBodyJournal(self.contract, self.root, (*self.records, line))

    def _event(self, kind, oid, at, operation_id, payload, heads=None):
        return self.append(
            BodyEvent(operation_id, kind, at, oid, _canonical(payload), heads)
        )

    def reserve(
        self,
        identity: ObjectIdentity,
        *,
        at: datetime,
        operation_id: str,
        account: LiveJournal,
        plans: tuple[LiveJournal, ...],
        related_bodies: tuple[LiveBodyJournal, ...] = (),
    ):
        result = reconcile_live_bodies(
            self, account=account, plans=plans, related_bodies=related_bodies
        )
        _require(
            result.classification == "consistent",
            "reservation_" + result.classification,
        )
        heads = _c2_check(self.contract, identity, "reserved", at, account, plans)
        return self._event(
            "reserved", identity.object_id, at, operation_id, identity.to_dict(), heads
        )

    def writer(self, oid: str, evidence: WriterEvidence, *, operation_id: str):
        return self._event(
            "writer", oid, evidence.observed_at, operation_id, evidence.to_dict()
        )

    def observe(
        self,
        oid: str,
        *,
        size: int | None,
        digest: str | None,
        state: str,
        evidence_ref: str,
        at: datetime,
        operation_id: str,
    ):
        return self._event(
            "observed",
            oid,
            at,
            operation_id,
            {
                "size": size,
                "digest": digest,
                "state": state,
                "evidence_ref": evidence_ref,
            },
        )

    def stabilize(
        self, oid: str, evidence: StableBodyEvidence, *, at: datetime, operation_id: str
    ):
        return self._event("stable", oid, at, operation_id, evidence.to_dict())

    def acquired(
        self,
        oid: str,
        *,
        size: int,
        evidence_ref: str,
        at: datetime,
        operation_id: str,
        account: LiveJournal,
        plans: tuple[LiveJournal, ...],
    ):
        heads = _c2_check(
            self.contract, self.object(oid).identity, "acquired", at, account, plans
        )
        return self._event(
            "acquired",
            oid,
            at,
            operation_id,
            {"size": size, "evidence_ref": evidence_ref},
            heads,
        )

    def commit(
        self,
        oid: str,
        *,
        next_cursor: str | None,
        at: datetime,
        operation_id: str,
        account: LiveJournal,
        plans: tuple[LiveJournal, ...],
        related_bodies: tuple[LiveBodyJournal, ...] = (),
    ):
        result = reconcile_live_bodies(
            self, account=account, plans=plans, related_bodies=related_bodies
        )
        _require(
            result.classification == "consistent", "commit_" + result.classification
        )
        obj = self.object(oid)
        _require(obj.stable, "finalization_evidence_missing")
        heads = _c2_check(self.contract, obj.identity, "committed", at, account, plans)
        receipt = LiveBodyReceipt(
            self.contract.sha256,
            obj.identity,
            self.root.sha256,
            obj.stability.sha256,
            obj.stability.digest,
            obj.stability.size,
            at,
            heads,
            next_cursor,
            operation_id,
        )
        return self._event("committed", oid, at, operation_id, receipt.to_dict(), heads)

    def quarantine_object(self, evidence: QuarantineEvidence):
        return self._event(
            "quarantined",
            evidence.identity.object_id,
            evidence.recorded_at,
            evidence.operation_id,
            evidence.to_dict(),
        )

    def audit(self, *, evidence_ref: str, at: datetime, operation_id: str):
        return self._event(
            "audit", None, at, operation_id, {"evidence_ref": evidence_ref}
        )

    def to_bytes(self) -> bytes:
        header = _canonical(
            {
                "schema": PREFIX + "body-journal-v1",
                "contract": self.contract.to_dict(),
                "root": self.root.to_dict(),
            }
        ).encode("utf-8")
        _require(len(header) <= MAX_RECORD_BYTES, "record_size_invalid")
        return (
            header
            + b"\n"
            + b"".join(line.encode("utf-8") + b"\n" for line in self.records)
        )

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        expected_contract: LiveBodyStorageContract,
        expected_root: PhysicalRootIdentity,
    ):
        _require(
            type(data) is bytes and data.endswith(b"\n"), "incomplete_journal_tail"
        )
        lines = data[:-1].split(b"\n")
        for line in lines:
            _require(0 < len(line) <= MAX_RECORD_BYTES, "record_size_invalid")
        header = _load(lines[0])
        _keys(header, {"schema", "contract", "root"})
        _require(header["schema"] == PREFIX + "body-journal-v1", "schema_invalid")
        contract, root = (
            LiveBodyStorageContract.from_dict(header["contract"]),
            PhysicalRootIdentity.from_dict(header["root"]),
        )
        _require(
            contract == expected_contract and root == expected_root,
            "expected_binding_mismatch",
        )
        try:
            result = cls(
                contract, root, tuple(line.decode("utf-8") for line in lines[1:])
            )
        except UnicodeError:
            raise HttpContractError("c3_json_invalid") from None
        _require(result.to_bytes() == data, "noncanonical_record")
        return result


def _reconcile_one(
    journal: LiveBodyJournal,
    *,
    account: LiveJournal | None,
    plans: tuple[LiveJournal, ...],
    claimed_projection: LiveStorageProjection | None = None,
    current_root: PhysicalRootIdentity | None = None,
) -> BodyAssessment:
    """Revalidate historical C2 prefixes, including every plan in those prefixes.

    Historical commits stay valid after later generations. A new commit, unlike
    an audit of an existing receipt, must match the current C2 generation.
    """
    _require(type(journal) is LiveBodyJournal, "journal_required")
    reasons, pending = [], []
    projection, operations = _replay(journal.contract, journal.root, journal.records)
    if claimed_projection is not None and claimed_projection != projection:
        reasons.append("storage_projection_mismatch")
    if current_root is not None and current_root != journal.root:
        reasons.append("physical_root_identity_mismatch")
    if account is None:
        pending.append("required_c2_evidence_missing")
    else:
        try:
            _scope_check(journal.contract, account)
            current = reconcile_live_journals(account, plans)
            if current.classification != "consistent":
                (pending if current.classification == "pending" else reasons).extend(
                    current.reasons
                )
            # not_started is only safe before C2 send. Inspect the latest
            # evidence (including a one-sided plan send), not just the older
            # C2 prefix referenced by the original body reservation. Absence
            # of a writer event must never be interpreted as proof of no FD.
            latest_attempts = (
                *account.projection.attempts,
                *(attempt for p in plans for attempt in p.projection.attempts),
            )
            for obj in projection.objects:
                if obj.writer.state == "not_started" and any(
                    attempt.key
                    == (obj.identity.binding.plan_sha, obj.identity.binding.attempt_id)
                    and (
                        attempt.sent_at is not None
                        or attempt.send_transition is not None
                        or attempt.state in {"response_received", "unknown"}
                        or attempt.outcome in {"response", "unknown"}
                    )
                    for attempt in latest_attempts
                ):
                    pending.append("writer_evidence_missing")
            for event in operations.values():
                if event.c2_heads is None:
                    continue
                a, p = _historical_c2(event.c2_heads, account, plans)
                obj = journal.object(event.object_id)
                if event.kind in {"reserved", "committed"}:
                    # A caller cannot hide an already-established newer generation
                    # by selecting an older otherwise-consistent journal prefix.
                    # Equal timestamps alone do not order separate journal events;
                    # their recorded prefix is still required in that case.
                    _require(
                        not any(
                            attempt.binding.generation > obj.identity.binding.generation
                            and attempt.reserved_at < event.recorded_at
                            for attempt in account.projection.attempts
                        ),
                        "historical_generation_already_superseded",
                    )
                _require(
                    _c2_check(
                        journal.contract,
                        obj.identity,
                        event.kind,
                        event.recorded_at,
                        a,
                        p,
                    )
                    == event.c2_heads,
                    "c2_head_mismatch",
                )
        except HttpContractError as exc:
            reasons.append(str(exc))
    if projection.unstable:
        pending.append("unstable_writer_or_size")
    try:
        # Audit can report over-budget evidence; it must not discard those bytes.
        _capacity(
            replace(
                projection,
                unstable=False,
                retained_body_bytes=projection.retained_observed_minimum_bytes,
            ),
            journal.contract.policy,
            0,
        )
    except HttpContractError as exc:
        reasons.append(str(exc))
    return BodyAssessment(
        "inconsistent" if reasons else "pending" if pending else "consistent",
        tuple(sorted(set(reasons + pending))),
    )


def reconcile_live_bodies(
    journal: LiveBodyJournal,
    *,
    account: LiveJournal | None,
    plans: tuple[LiveJournal, ...],
    claimed_projection: LiveStorageProjection | None = None,
    current_root: PhysicalRootIdentity | None = None,
    related_bodies: tuple[LiveBodyJournal, ...] = (),
) -> BodyAssessment:
    """All account plans require a C3 journal, including an empty one before use.

    This models complete inventory coverage, not physical discovery or an atomic
    snapshot. I1/I3 must obtain all these values from the authoritative Store.
    """
    _require(
        type(related_bodies) is tuple
        and all(type(j) is LiveBodyJournal for j in related_bodies),
        "related_body_journals_invalid",
    )
    _require(
        account is None or type(account) is LiveJournal, "account_journal_required"
    )
    result = _reconcile_one(
        journal,
        account=account,
        plans=plans,
        claimed_projection=claimed_projection,
        current_root=current_root,
    )
    if account is None:
        return result
    results = [result]
    supplied = (journal, *related_bodies)
    shas = [j.contract.plan.plan_sha for j in supplied]
    _require(len(shas) == len(set(shas)), "duplicate_body_journal")
    expected = {s.plan_sha for s in account.projection.scopes}
    if set(shas) - expected:
        results.append(BodyAssessment("inconsistent", ("unrelated_body_journal",)))
    if expected - set(shas):
        results.append(
            BodyAssessment("pending", ("required_related_body_journal_missing",))
        )
    roots = [j.contract.root for j in supplied]
    if len(set(roots)) != len(roots):
        results.append(
            BodyAssessment("inconsistent", ("different_plans_share_body_root",))
        )
    for other in related_bodies:
        results.append(_reconcile_one(other, account=account, plans=plans))
    classification = (
        "inconsistent"
        if any(r.classification == "inconsistent" for r in results)
        else "pending"
        if any(r.classification == "pending" for r in results)
        else "consistent"
    )
    return BodyAssessment(
        classification, tuple(sorted({reason for r in results for reason in r.reasons}))
    )
