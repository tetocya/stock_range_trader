"""I1a-2A: pure runtime evidence and artificial Store v2 persistence.

No filesystem identity capture, clock capture, lock, operational connection or
StoreSession is provided. Claims and clean-restart candidates are not permission.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import PurePosixPath
from uuid import UUID

from . import live_store as store
from .http_contract import HttpContractError
from .live_preflight import LIVE_LEDGER_FILENAME, _canonical_owner_store_root

STORE_SCHEMA_V2 = "historical-feasibility-live-store-v2"
DEPLOYMENT_SCHEMA_V2 = "historical-feasibility-live-store-deployment-v2"
RUNTIME_POLICY_SCHEMA = "historical-feasibility-live-store-runtime-policy-v1"
OWNER_DEPLOYMENT_SCHEMA = "historical-feasibility-live-store-owner-deployment-v1"
PHYSICAL_BINDING_SCHEMA = "historical-feasibility-live-store-physical-binding-v1"
RUNTIME_EVENT_SCHEMA = "historical-feasibility-live-store-runtime-event-v1"
RUNTIME_PROJECTION_SCHEMA = "historical-feasibility-live-store-runtime-projection-v1"
SESSION_SCHEMA = "historical-feasibility-live-store-session-v1"
LOCK_FILENAME = "account-rate-ledger.lock"
MAX_INTEGER = 2**63 - 1
DRIFT_NS = 5_000_000_000


def _check(condition, reason):
    if not condition:
        raise HttpContractError("live_runtime_" + reason)


def _integer(value, *, positive=False):
    _check(
        type(value) is int and int(positive) <= value <= MAX_INTEGER,
        "integer_invalid",
    )
    return value


def _fields(raw, expected):
    _check(type(raw) is dict and set(raw) == set(expected), "fields_invalid")


def _uuid(value):
    try:
        valid = type(value) is str and str(UUID(value)) == value
    except (ValueError, AttributeError):
        valid = False
    _check(valid, "store_uuid_invalid")


class _Canonical:
    @property
    def canonical(self):
        return store.canonical_bytes(self.to_dict())

    @property
    def sha256(self):
        return store.digest_bytes(self.canonical)


@dataclass(frozen=True)
class RuntimePolicy(_Canonical):
    """Fixed approved policy, not tunable runtime permission or production N."""

    drift_threshold_ns: int = DRIFT_NS
    utc_backward_stop: bool = True
    monotonic_backward_stop: bool = True
    utc_source: str = "store_owned_utc"
    elapsed_source: str = "monotonic_ns"
    supported_platforms: tuple[str, ...] = ("darwin", "linux")
    filesystem_policy: str = "owner_managed_local_only"
    sqlite_minimum: str = "3.31.0"
    foreign_keys: bool = True
    recursive_triggers: bool = True
    journal_mode: str = "DELETE"
    synchronous: str = "EXTRA"
    busy_timeout_ms: int = 0
    locking_mode: str = "NORMAL"
    trusted_schema: bool = False
    ignore_check_constraints: bool = False
    macos_fullfsync: bool = True
    linux_fullfsync_required: bool = False
    lock_policy: str = "persistent_file_flock_exclusive_nonblocking"
    restart_policy: str = "dirty_restart_manual_block"
    automatic_takeover: bool = False
    suspend_policy: str = "unsupported_no_complete_detection_guarantee"

    def __post_init__(self):
        for name, field in self.__dataclass_fields__.items():
            value = getattr(self, name)
            _check(
                type(value) is type(field.default) and value == field.default,
                "policy_value_invalid",
            )

    def to_dict(self):
        return {"schema": RUNTIME_POLICY_SCHEMA, **asdict(self)}

    @classmethod
    def from_bytes(cls, data, expected_sha=None):
        raw = store._load(data, expected_sha)
        _fields(raw, {"schema", *cls.__dataclass_fields__})
        _check(raw.pop("schema") == RUNTIME_POLICY_SCHEMA, "policy_schema_invalid")
        _check(type(raw["supported_platforms"]) is list, "platform_policy_invalid")
        raw["supported_platforms"] = tuple(raw["supported_platforms"])
        return cls(**raw)


@dataclass(frozen=True)
class PhysicalIdentity:
    device: int
    inode: int
    object_kind: str
    link_count: int

    def __post_init__(self):
        _integer(self.device)
        _integer(self.inode, positive=True)
        _integer(self.link_count, positive=True)
        _check(
            self.object_kind in ("directory", "regular_file"), "physical_kind_invalid"
        )


@dataclass(frozen=True)
class PhysicalBinding(_Canonical):
    root: PhysicalIdentity
    account_directory: PhysicalIdentity
    db: PhysicalIdentity
    lock: PhysicalIdentity

    def __post_init__(self):
        identities = (self.root, self.account_directory, self.db, self.lock)
        _check(
            all(type(v) is PhysicalIdentity for v in identities), "physical_required"
        )
        for index, value in enumerate(identities):
            _check(
                value.object_kind == ("directory" if index < 2 else "regular_file"),
                "physical_role_invalid",
            )
            if index >= 2:
                _check(value.link_count == 1, "file_hardlink_forbidden")
        _check(
            len({(v.device, v.inode) for v in identities}) == 4,
            "physical_alias_claim",
        )

    def to_dict(self):
        return {"schema": PHYSICAL_BINDING_SCHEMA, **asdict(self)}

    @classmethod
    def from_dict(cls, raw):
        _fields(raw, {"schema", "root", "account_directory", "db", "lock"})
        _check(raw["schema"] == PHYSICAL_BINDING_SCHEMA, "physical_schema_invalid")
        values = {}
        for name in cls.__dataclass_fields__:
            _fields(raw[name], PhysicalIdentity.__dataclass_fields__)
            values[name] = PhysicalIdentity(**raw[name])
        return cls(**values)


@dataclass(frozen=True)
class OwnerDeploymentRecord(_Canonical):
    """Intent has no allocated UUID/inodes; pin binds the created-event hash.

    Paths follow C1 lexically. No stat, realpath or owner authenticity is claimed.
    """

    deployment_id: str
    account_ref: str
    registry_sha: str
    preflight_sha: str
    identity_sha: str
    store_policy_sha: str
    runtime_policy_sha: str
    canonical_root: str
    db_path: str
    lock_path: str
    bootstrap_intent_id: str
    policy_decision_reference: str
    policy_approval_status: str
    state: str = "intent"
    store_uuid: str | None = None
    physical: PhysicalBinding | None = None
    bootstrap_evidence_sha: str | None = None
    owner_pin_reference: str | None = None

    def __post_init__(self):
        for value in (
            self.deployment_id,
            self.account_ref,
            self.bootstrap_intent_id,
            self.policy_decision_reference,
        ):
            store._label(value)
        for value in (
            self.registry_sha,
            self.preflight_sha,
            self.identity_sha,
            self.store_policy_sha,
            self.runtime_policy_sha,
        ):
            store._sha(value)
        _canonical_owner_store_root(self.canonical_root)
        parent = PurePosixPath(self.canonical_root) / "accounts" / self.account_ref
        _check(self.db_path == str(parent / LIVE_LEDGER_FILENAME), "db_path_invalid")
        _check(self.lock_path == str(parent / LOCK_FILENAME), "lock_path_invalid")
        _check(
            self.policy_approval_status in ("pending", "approved"),
            "approval_status_invalid",
        )
        _check(self.state in ("intent", "pinned"), "owner_state_invalid")
        allocated = (
            self.store_uuid,
            self.physical,
            self.bootstrap_evidence_sha,
            self.owner_pin_reference,
        )
        if self.state == "intent":
            _check(all(v is None for v in allocated), "intent_already_pinned")
        else:
            _uuid(self.store_uuid)
            _check(type(self.physical) is PhysicalBinding, "physical_required")
            store._sha(self.bootstrap_evidence_sha)
            store._label(self.owner_pin_reference)
            _check(self.policy_approval_status == "approved", "policy_not_approved")

    def to_dict(self):
        raw = asdict(self)
        raw["physical"] = None if self.physical is None else self.physical.to_dict()
        return {"schema": OWNER_DEPLOYMENT_SCHEMA, **raw}

    @classmethod
    def from_bytes(cls, data, expected_sha=None):
        raw = store._load(data, expected_sha)
        _fields(raw, {"schema", *cls.__dataclass_fields__})
        _check(raw.pop("schema") == OWNER_DEPLOYMENT_SCHEMA, "owner_schema_invalid")
        if raw["physical"] is not None:
            raw["physical"] = PhysicalBinding.from_dict(raw["physical"])
        return cls(**raw)


@dataclass(frozen=True)
class ClockObservation:
    utc: str
    monotonic_ns: int

    def __post_init__(self):
        store._time(self.utc)
        _integer(self.monotonic_ns)


def _utc_ns_delta(later, earlier):
    delta = datetime.fromisoformat(later) - datetime.fromisoformat(earlier)
    return (
        (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    ) * 1000


def clock_stop_reasons(anchor, previous, observed):
    """Integer arithmetic; monotonic anchors are local to a single session."""
    _check(
        all(type(v) is ClockObservation for v in (anchor, previous, observed)),
        "clock_observation_required",
    )
    reasons = []
    if observed.utc < previous.utc:
        reasons.append("utc_backward")
    if observed.monotonic_ns < previous.monotonic_ns:
        reasons.append("monotonic_backward")
    drift = _utc_ns_delta(observed.utc, anchor.utc) - (
        observed.monotonic_ns - anchor.monotonic_ns
    )
    if abs(drift) > DRIFT_NS:
        reasons.append("drift_exceeded")
    return tuple(reasons)


@dataclass(frozen=True)
class RuntimeContext:
    deployment: store.DeploymentRecord
    runtime_policy: RuntimePolicy
    owner_intent: OwnerDeploymentRecord
    owner_pins: tuple[OwnerDeploymentRecord, ...] = ()

    def __post_init__(self):
        _check(type(self.deployment) is store.DeploymentRecord, "deployment_required")
        _check(type(self.runtime_policy) is RuntimePolicy, "runtime_policy_required")
        _check(
            type(self.owner_intent) is OwnerDeploymentRecord, "owner_intent_required"
        )
        _uuid(self.deployment.store_id)
        _check(self.owner_intent.state == "intent", "owner_intent_required")
        d, intent = self.deployment, self.owner_intent
        _check(
            (
                d.deployment_id,
                d.account_ref,
                d.owner_registry_sha,
                d.preflight_sha,
                d.store_policy_sha,
                self.runtime_policy.sha256,
            )
            == (
                intent.deployment_id,
                intent.account_ref,
                intent.registry_sha,
                intent.preflight_sha,
                intent.store_policy_sha,
                intent.runtime_policy_sha,
            ),
            "owner_deployment_binding_mismatch",
        )
        _check(
            type(self.owner_pins) is tuple and len(self.owner_pins) <= 1,
            "pin_set_invalid",
        )
        for pin in self.owner_pins:
            _check(
                type(pin) is OwnerDeploymentRecord and pin.state == "pinned",
                "pin_required",
            )
            unpinned = replace(
                pin,
                state="intent",
                store_uuid=None,
                physical=None,
                bootstrap_evidence_sha=None,
                owner_pin_reference=None,
            )
            _check(unpinned == intent, "pin_intent_mismatch")
            _check(pin.store_uuid == d.store_id, "pin_store_mismatch")


@dataclass(frozen=True)
class RuntimeEvent(_Canonical):
    kind: str
    sequence: int
    previous_hash: str
    deployment_id: str
    store_id: str
    runtime_policy_sha: str
    session_id: str | None
    fence_epoch: int
    observation: ClockObservation
    transition_id: str
    payload: bytes

    def __post_init__(self):
        _check(
            type(self.kind) is str
            and self.kind
            in {
                "bootstrap_created",
                "bootstrap_finalized",
                "session_started",
                "clock_observed",
                "runtime_stop_entered",
                "session_closed",
            },
            "event_kind_invalid",
        )
        _integer(self.sequence)
        _integer(self.fence_epoch)
        store._sha(self.previous_hash)
        store._sha(self.runtime_policy_sha)
        store._label(self.deployment_id)
        _uuid(self.store_id)
        if self.session_id is not None:
            store._label(self.session_id)
        _check(type(self.observation) is ClockObservation, "clock_observation_required")
        store._label(self.transition_id)
        store._load(self.payload)

    def _unsigned(self):
        return {
            "schema": RUNTIME_EVENT_SCHEMA,
            "kind": self.kind,
            "sequence": self.sequence,
            "previous_hash": self.previous_hash,
            "deployment_id": self.deployment_id,
            "store_id": self.store_id,
            "runtime_policy_sha": self.runtime_policy_sha,
            "session_id": self.session_id,
            "fence_epoch": self.fence_epoch,
            "observed_utc": self.observation.utc,
            "observed_monotonic_ns": self.observation.monotonic_ns,
            "transition_id": self.transition_id,
            "payload": store._load(self.payload),
        }

    @property
    def event_hash(self):
        return store.digest_bytes(store.canonical_bytes(self._unsigned()))

    def to_dict(self):
        return {**self._unsigned(), "event_hash": self.event_hash}

    @classmethod
    def from_bytes(cls, data):
        raw = store._load(data)
        _fields(
            raw,
            {
                "schema",
                "kind",
                "sequence",
                "previous_hash",
                "deployment_id",
                "store_id",
                "runtime_policy_sha",
                "session_id",
                "fence_epoch",
                "observed_utc",
                "observed_monotonic_ns",
                "transition_id",
                "payload",
                "event_hash",
            },
        )
        _check(raw["schema"] == RUNTIME_EVENT_SCHEMA, "event_schema_invalid")
        event = cls(
            raw["kind"],
            raw["sequence"],
            raw["previous_hash"],
            raw["deployment_id"],
            raw["store_id"],
            raw["runtime_policy_sha"],
            raw["session_id"],
            raw["fence_epoch"],
            ClockObservation(raw["observed_utc"], raw["observed_monotonic_ns"]),
            raw["transition_id"],
            store.canonical_bytes(raw["payload"]),
        )
        _check(raw["event_hash"] == event.event_hash, "event_hash_mismatch")
        return event


@dataclass(frozen=True)
class SessionRecord(_Canonical):
    deployment_id: str
    store_id: str
    session_id: str
    fence_epoch: int
    start_event: str
    close_event: str | None
    anchor: ClockObservation
    runtime_policy_sha: str
    status: str

    def to_dict(self):
        return {"schema": SESSION_SCHEMA, **asdict(self)}


@dataclass(frozen=True)
class RuntimeProjection(_Canonical):
    lifecycle: str = "not_created"
    head_sha: str = store.ZERO_HASH
    event_count: int = 0
    fence_epoch: int = 0
    current_session_id: str | None = None
    session_state: str = "no_session"
    persistent_stop: bool = False
    stop_reason: str | None = None
    clock_uncertain: bool = False
    restart_uncertain: bool = False
    last_accepted_utc: str | None = None
    last_monotonic_ns: int | None = None
    last_clean_session: str | None = None
    bootstrap_head: str | None = None
    physical_sha: str | None = None
    owner_pin_sha: str | None = None
    sessions: tuple[SessionRecord, ...] = ()

    def to_dict(self):
        raw = asdict(self)
        raw["sessions"] = [s.to_dict() for s in self.sessions]
        return {"schema": RUNTIME_PROJECTION_SCHEMA, **raw}


def _replace_session(projection, **changes):
    return (*projection.sessions[:-1], replace(projection.sessions[-1], **changes))


def _reduce(context, projection, event):
    p, e = projection, event
    raw = store._load(e.payload)
    _check(not p.persistent_stop, "persistent_stop")
    if e.kind == "bootstrap_created":
        _check(p.event_count == 0, "bootstrap_already_created")
        _fields(raw, {"intent_sha", "physical"})
        _check(
            raw["intent_sha"] == context.owner_intent.sha256,
            "bootstrap_intent_mismatch",
        )
        physical = PhysicalBinding.from_dict(raw["physical"])
        _check(e.session_id is None and e.fence_epoch == 0, "bootstrap_epoch_invalid")
        _check(
            e.observation.utc == context.deployment.created_at,
            "bootstrap_time_mismatch",
        )
        return replace(
            p,
            lifecycle="bootstrap_pending",
            bootstrap_head=e.event_hash,
            physical_sha=physical.sha256,
            last_accepted_utc=e.observation.utc,
        )
    _check(p.lifecycle != "not_created", "bootstrap_missing")
    if e.kind == "bootstrap_finalized":
        _check(p.lifecycle == "bootstrap_pending", "bootstrap_not_pending")
        _fields(
            raw, {"owner_pin_sha", "owner_pin_reference", "bootstrap_head", "lifecycle"}
        )
        pins = [pin for pin in context.owner_pins if pin.sha256 == raw["owner_pin_sha"]]
        _check(len(pins) == 1, "owner_pin_missing")
        pin = pins[0]
        _check(
            raw["bootstrap_head"] == pin.bootstrap_evidence_sha == p.bootstrap_head
            and raw["owner_pin_reference"] == pin.owner_pin_reference
            and pin.physical.sha256 == p.physical_sha
            and raw["lifecycle"] == "prepared",
            "bootstrap_pin_mismatch",
        )
        _check(e.session_id is None and e.fence_epoch == 0, "bootstrap_epoch_invalid")
        _check(e.observation.utc >= p.last_accepted_utc, "clock_stop_required")
        return replace(
            p,
            lifecycle="prepared",
            owner_pin_sha=pin.sha256,
            last_accepted_utc=e.observation.utc,
        )
    if e.kind == "session_started":
        _fields(raw, {"owner_pin_sha"})
        _check(p.lifecycle == "prepared", "bootstrap_not_prepared")
        _check(p.session_state in ("no_session", "clean_closed"), "dirty_restart")
        _check(p.fence_epoch < MAX_INTEGER, "fence_overflow")
        _check(e.fence_epoch == p.fence_epoch + 1, "fence_step_invalid")
        _check(e.session_id is not None, "session_id_required")
        _check(
            e.session_id not in {s.session_id for s in p.sessions}, "session_id_reused"
        )
        _check(raw["owner_pin_sha"] == p.owner_pin_sha, "session_pin_mismatch")
        _check(e.observation.utc >= p.last_accepted_utc, "clock_stop_required")
        session = SessionRecord(
            context.deployment.deployment_id,
            context.deployment.store_id,
            e.session_id,
            e.fence_epoch,
            e.event_hash,
            None,
            e.observation,
            e.runtime_policy_sha,
            "running",
        )
        return replace(
            p,
            fence_epoch=e.fence_epoch,
            current_session_id=e.session_id,
            session_state="running",
            sessions=(*p.sessions, session),
            last_accepted_utc=e.observation.utc,
            last_monotonic_ns=e.observation.monotonic_ns,
        )
    _check(e.fence_epoch == p.fence_epoch, "fence_mismatch")
    _check(e.session_id == p.current_session_id, "session_mismatch")
    reasons = ()
    if p.session_state == "running":
        reasons = clock_stop_reasons(
            p.sessions[-1].anchor,
            ClockObservation(p.last_accepted_utc, p.last_monotonic_ns),
            e.observation,
        )
    elif e.observation.utc < p.last_accepted_utc:
        reasons = ("utc_backward",)
    if e.kind == "runtime_stop_entered":
        _fields(raw, {"reason", "clock_reasons", "evidence_reference"})
        store._label(raw["evidence_reference"])
        _check(type(raw["clock_reasons"]) is list, "clock_reasons_invalid")
        _check(
            raw["reason"]
            in ("clock_uncertain", "restart_uncertain", "runtime_integrity_uncertain"),
            "stop_reason_invalid",
        )
        if raw["reason"] == "restart_uncertain":
            # A new process's monotonic sample is not comparable to the old
            # session. Preserve dirty restart even when UTC has also regressed.
            _check(p.session_state == "running", "restart_stop_without_session")
            reasons = (
                ("utc_backward",) if e.observation.utc < p.last_accepted_utc else ()
            )
            _check(raw["clock_reasons"] == list(reasons), "clock_reasons_mismatch")
        elif raw["reason"] == "clock_uncertain":
            _check(
                bool(reasons) and raw["clock_reasons"] == list(reasons),
                "clock_reasons_mismatch",
            )
        else:
            _check(not raw["clock_reasons"] and not reasons, "clock_stop_required")
        sessions = p.sessions
        if p.session_state == "running":
            sessions = _replace_session(p, status="stopped")
        return replace(
            p,
            persistent_stop=True,
            stop_reason=raw["reason"],
            clock_uncertain=bool(reasons),
            restart_uncertain=raw["reason"] == "restart_uncertain",
            session_state="stopped",
            sessions=sessions,
        )
    _check(p.session_state == "running", "session_not_running")
    _check(not reasons, "clock_stop_required")
    if e.kind == "clock_observed":
        _fields(raw, set())
        return replace(
            p,
            last_accepted_utc=e.observation.utc,
            last_monotonic_ns=e.observation.monotonic_ns,
        )
    _check(e.kind == "session_closed", "transition_invalid")
    _fields(raw, {"predecessor", "quiescent", "validation_sha"})
    _check(raw["predecessor"] == p.head_sha, "close_predecessor_mismatch")
    _check(raw["quiescent"] is True, "quiescence_required")
    store._sha(raw["validation_sha"])
    return replace(
        p,
        session_state="clean_closed",
        current_session_id=None,
        last_clean_session=e.session_id,
        last_accepted_utc=e.observation.utc,
        last_monotonic_ns=e.observation.monotonic_ns,
        sessions=_replace_session(p, status="clean_closed", close_event=e.event_hash),
    )


def replay_runtime(context, records):
    """Rebuild from exact canonical originals. UTC does not order this chain."""
    _check(
        type(context) is RuntimeContext and type(records) is tuple,
        "replay_input_invalid",
    )
    projection = RuntimeProjection()
    transitions = set()
    for sequence, data in enumerate(records):
        event = RuntimeEvent.from_bytes(data)
        _check(
            event.sequence == sequence and event.previous_hash == projection.head_sha,
            "chain_mismatch",
        )
        _check(
            (event.deployment_id, event.store_id, event.runtime_policy_sha)
            == (
                context.deployment.deployment_id,
                context.deployment.store_id,
                context.runtime_policy.sha256,
            ),
            "event_binding_mismatch",
        )
        _check(event.transition_id not in transitions, "transition_id_reused")
        transitions.add(event.transition_id)
        projection = _reduce(context, projection, event)
        projection = replace(
            projection, head_sha=event.event_hash, event_count=sequence + 1
        )
    if records and context.owner_pins:
        pin = context.owner_pins[0]
        _check(
            pin.bootstrap_evidence_sha == projection.bootstrap_head
            and pin.physical.sha256 == projection.physical_sha,
            "bootstrap_pin_mismatch",
        )
    return projection


def append_runtime_event(
    context,
    records,
    *,
    kind,
    observation,
    transition_id,
    session_id,
    fence_epoch,
    payload,
):
    """Pure candidate construction, not a Store mutation or clock capture API."""
    prior = replay_runtime(context, records)
    event = RuntimeEvent(
        kind,
        len(records),
        prior.head_sha,
        context.deployment.deployment_id,
        context.deployment.store_id,
        context.runtime_policy.sha256,
        session_id,
        fence_epoch,
        observation,
        transition_id,
        store.canonical_bytes(payload),
    )
    result = (*records, event.canonical)
    replay_runtime(context, result)
    return result


def record_clock_observation(context, records, observation, transition_id):
    """Pure sample-to-evidence transition; anomalous samples produce sticky stop."""
    p = replay_runtime(context, records)
    _check(
        p.session_state == "running" and not p.persistent_stop, "session_not_running"
    )
    reasons = clock_stop_reasons(
        p.sessions[-1].anchor,
        ClockObservation(p.last_accepted_utc, p.last_monotonic_ns),
        observation,
    )
    return append_runtime_event(
        context,
        records,
        kind="runtime_stop_entered" if reasons else "clock_observed",
        observation=observation,
        transition_id=transition_id,
        session_id=p.current_session_id,
        fence_epoch=p.fence_epoch,
        payload={
            "reason": "clock_uncertain",
            "clock_reasons": list(reasons),
            "evidence_reference": transition_id,
        }
        if reasons
        else {},
    )


@dataclass(frozen=True)
class StartupAssessment:
    classification: str
    reasons: tuple[str, ...]


def assess_startup(context, records):
    """Pure next-open assessment; a candidate still needs every I1a-2B guard."""
    p = replay_runtime(context, records)
    if p.persistent_stop:
        return StartupAssessment("manual_blocked", (p.stop_reason,))
    if p.lifecycle != "prepared":
        return StartupAssessment(
            "bootstrap_incomplete", ("explicit_finalization_required",)
        )
    if p.session_state == "running":
        return StartupAssessment("manual_blocked", ("restart_uncertain",))
    if p.fence_epoch == MAX_INTEGER:
        return StartupAssessment("manual_blocked", ("fence_overflow",))
    return StartupAssessment("new_session_candidate", ())


# A complete, separate schema: the v1 definitions and validator remain intact.
# The immutable deployment envelope records initial bootstrap_pending, while
# runtime_current contains the replay-derived current lifecycle.
_BASE_DDL_V2 = tuple(
    sql.replace("CHECK(lifecycle='prepared')", "CHECK(lifecycle='bootstrap_pending')")
    for sql in store._DDL
)
_RUNTIME_DDL = (
    """CREATE TABLE runtime_policies (
        policy_sha TEXT PRIMARY KEY, schema TEXT NOT NULL,
        canonical BLOB NOT NULL CHECK(typeof(canonical)='blob')
    )""",
    """CREATE TABLE owner_deployment_records (
        deployment_id TEXT NOT NULL, record_sha TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('intent','pinned')),
        canonical BLOB NOT NULL CHECK(typeof(canonical)='blob'),
        PRIMARY KEY(deployment_id,record_sha),
        UNIQUE(deployment_id,state),
        FOREIGN KEY(deployment_id) REFERENCES deployments(deployment_id)
    )""",
    """CREATE TABLE runtime_bindings (
        deployment_id TEXT PRIMARY KEY, policy_sha TEXT NOT NULL,
        owner_intent_sha TEXT NOT NULL,
        FOREIGN KEY(deployment_id) REFERENCES deployments(deployment_id),
        FOREIGN KEY(policy_sha) REFERENCES runtime_policies(policy_sha),
        FOREIGN KEY(deployment_id,owner_intent_sha)
            REFERENCES owner_deployment_records(deployment_id,record_sha)
    )""",
    """CREATE TABLE runtime_events (
        deployment_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK(typeof(sequence)='integer' AND sequence>=0),
        event_hash TEXT NOT NULL, previous_hash TEXT NOT NULL,
        canonical BLOB NOT NULL CHECK(typeof(canonical)='blob'),
        PRIMARY KEY(deployment_id,sequence),
        UNIQUE(deployment_id,event_hash),
        UNIQUE(deployment_id,sequence,event_hash),
        FOREIGN KEY(deployment_id) REFERENCES runtime_bindings(deployment_id)
    )""",
    """CREATE TABLE runtime_current (
        deployment_id TEXT PRIMARY KEY,
        event_count INTEGER NOT NULL CHECK(typeof(event_count)='integer' AND event_count>0),
        last_sequence INTEGER NOT NULL CHECK(last_sequence=event_count-1),
        head_sha TEXT NOT NULL,
        canonical BLOB NOT NULL CHECK(typeof(canonical)='blob'),
        FOREIGN KEY(deployment_id,last_sequence,head_sha)
            REFERENCES runtime_events(deployment_id,sequence,event_hash)
    )""",
    """CREATE TABLE runtime_sessions (
        deployment_id TEXT NOT NULL, session_id TEXT NOT NULL,
        fence_epoch INTEGER NOT NULL CHECK(typeof(fence_epoch)='integer' AND fence_epoch>0),
        start_event TEXT NOT NULL, close_event TEXT,
        canonical BLOB NOT NULL CHECK(typeof(canonical)='blob'),
        PRIMARY KEY(deployment_id,session_id),
        UNIQUE(deployment_id,fence_epoch),
        FOREIGN KEY(deployment_id,start_event) REFERENCES runtime_events(deployment_id,event_hash),
        FOREIGN KEY(deployment_id,close_event) REFERENCES runtime_events(deployment_id,event_hash)
    )""",
)
_RUNTIME_TRIGGERS = tuple(
    sql
    for table, keys in (
        ("runtime_policies", ("policy_sha",)),
        ("owner_deployment_records", ("deployment_id", "record_sha")),
        ("runtime_bindings", ("deployment_id",)),
        ("runtime_events", ("deployment_id", "sequence")),
    )
    for sql in store._immutable(table, keys)
)
_DDL_V2 = (*_BASE_DDL_V2, *store._TRIGGERS, *_RUNTIME_DDL, *_RUNTIME_TRIGGERS)


def _deployment_bytes(deployment):
    return store.canonical_bytes(
        {
            **deployment.__dict__,
            "schema": DEPLOYMENT_SCHEMA_V2,
            "store_schema": STORE_SCHEMA_V2,
            "lifecycle": "bootstrap_pending",
        }
    )


def _initialize_schema_v2(connection, context, policy, documents, records):
    """Private artificial initializer, only a fresh supplied connection.

    Records are explicit artificial evidence, not captured production facts.
    No path choice, connection creation, v1 migration or operational handle.
    """
    store._connection(connection)
    _check(not connection.in_transaction, "existing_transaction")
    _check(
        not connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall(),
        "existing_database_no_migration",
    )
    _check(type(context) is RuntimeContext, "context_required")
    _check(type(policy) is store.StorePolicy, "store_policy_required")
    _check(type(documents) is tuple, "documents_required")
    policy = store.StorePolicy.from_bytes(policy.canonical)
    d = context.deployment
    _check(d.store_policy_sha == policy.sha256, "store_policy_mismatch")
    projection = replay_runtime(context, records)
    _check(projection.event_count > 0, "bootstrap_missing")
    with connection:
        connection.execute("BEGIN")
        for sql in _DDL_V2:
            connection.execute(sql)
        connection.execute(
            "INSERT INTO policies VALUES (?,?,?,?,?,?,?)",
            (
                policy.sha256,
                store.POLICY_SCHEMA,
                policy.canonical,
                policy.max_enrolled_plans_per_account,
                policy.max_record_bytes,
                policy.max_retained_body_bytes,
                policy.max_cumulative_acquired_bytes,
            ),
        )
        connection.execute(
            "INSERT INTO deployments VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                d.deployment_id,
                d.store_id,
                d.account_ref,
                policy.sha256,
                d.owner_registry_sha,
                d.preflight_sha,
                "owner_registry",
                store.LIVE_ACCOUNT_REGISTRY_SCHEMA,
                "preflight",
                store.LIVE_ACCOUNT_PREFLIGHT_SCHEMA,
                d.created_at,
                "bootstrap_pending",
                _deployment_bytes(d),
            ),
        )
        for document in documents:
            _check(type(document) is store.CanonicalDocument, "document_required")
            doc = store.CanonicalDocument(**document.__dict__)
            connection.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
                (
                    d.deployment_id,
                    doc.sha256,
                    doc.kind,
                    doc.schema,
                    doc.content,
                    doc.source_identity,
                    doc.source_reference,
                    doc.registered_at,
                ),
            )
        connection.execute(
            "INSERT INTO store_metadata VALUES (1,?,?,?)",
            (
                STORE_SCHEMA_V2,
                d.deployment_id,
                store.canonical_bytes(
                    {"schema": STORE_SCHEMA_V2, "deployment_id": d.deployment_id}
                ),
            ),
        )
        rp = context.runtime_policy
        connection.execute(
            "INSERT INTO runtime_policies VALUES (?,?,?)",
            (rp.sha256, RUNTIME_POLICY_SCHEMA, rp.canonical),
        )
        for owner in (context.owner_intent, *context.owner_pins):
            connection.execute(
                "INSERT INTO owner_deployment_records VALUES (?,?,?,?)",
                (d.deployment_id, owner.sha256, owner.state, owner.canonical),
            )
        connection.execute(
            "INSERT INTO runtime_bindings VALUES (?,?,?)",
            (d.deployment_id, rp.sha256, context.owner_intent.sha256),
        )
        for data in records:
            event = RuntimeEvent.from_bytes(data)
            connection.execute(
                "INSERT INTO runtime_events VALUES (?,?,?,?,?)",
                (
                    d.deployment_id,
                    event.sequence,
                    event.event_hash,
                    event.previous_hash,
                    data,
                ),
            )
        connection.execute(
            "INSERT INTO runtime_current VALUES (?,?,?,?,?)",
            (
                d.deployment_id,
                projection.event_count,
                projection.event_count - 1,
                projection.head_sha,
                projection.canonical,
            ),
        )
        for session in projection.sessions:
            connection.execute(
                "INSERT INTO runtime_sessions VALUES (?,?,?,?,?,?)",
                (
                    d.deployment_id,
                    session.session_id,
                    session.fence_epoch,
                    session.start_event,
                    session.close_event,
                    session.canonical,
                ),
            )
        validate_store_v2_contents(connection)


def validate_store_v2_schema(connection):
    """Read-only exact v2 identity/DDL check; v1 is never reinterpreted."""
    store._connection(connection)
    actual = sorted(
        row[0]
        for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        )
    )
    _check(actual == sorted(_DDL_V2), "schema_definition_mismatch")
    rows = connection.execute(
        "SELECT schema,deployment_id,canonical FROM store_metadata"
    ).fetchall()
    _check(len(rows) == 1, "metadata_missing")
    schema, deployment, data = rows[0]
    _check(schema == STORE_SCHEMA_V2, "schema_identity_invalid")
    _check(
        store._load(data) == {"schema": STORE_SCHEMA_V2, "deployment_id": deployment},
        "metadata_binding_invalid",
    )
    _check(
        not connection.execute("PRAGMA foreign_key_check").fetchall(),
        "foreign_key_violation",
    )
    return STORE_SCHEMA_V2


def validate_store_v2_contents(connection):
    """Validate original contracts then rebuild all runtime-derived rows."""
    validate_store_v2_schema(connection)
    store._validate_relational_contents(
        connection,
        deployment_schema=DEPLOYMENT_SCHEMA_V2,
        store_schema=STORE_SCHEMA_V2,
        initial_lifecycle="bootstrap_pending",
    )
    return _validate_runtime_contents(connection)


def _validate_runtime_contents(connection):
    """Runtime originals/projections only, after exact schema and core checks."""
    d = store._rows(connection, "deployments")[0]
    deployment = store.DeploymentRecord(
        **{
            k: d[k] if k != "store_policy_sha" else d["policy_sha"]
            for k in store.DeploymentRecord.__dataclass_fields__
        }
    )
    bindings = store._rows(connection, "runtime_bindings")
    policies = store._rows(connection, "runtime_policies")
    _check(len(bindings) == len(policies) == 1, "runtime_binding_missing")
    binding, policy_row = bindings[0], policies[0]
    policy = RuntimePolicy.from_bytes(policy_row["canonical"], policy_row["policy_sha"])
    _check(
        policy_row["schema"] == RUNTIME_POLICY_SCHEMA
        and binding["policy_sha"] == policy.sha256
        and binding["deployment_id"] == deployment.deployment_id,
        "runtime_policy_mismatch",
    )
    owners = []
    for row in store._rows(connection, "owner_deployment_records"):
        owner = OwnerDeploymentRecord.from_bytes(row["canonical"], row["record_sha"])
        _check(
            owner.deployment_id == row["deployment_id"] == deployment.deployment_id
            and owner.state == row["state"],
            "owner_columns_mismatch",
        )
        owners.append(owner)
    intents = [o for o in owners if o.state == "intent"]
    _check(
        len(intents) == 1 and intents[0].sha256 == binding["owner_intent_sha"],
        "owner_intent_missing",
    )
    context = RuntimeContext(
        deployment, policy, intents[0], tuple(o for o in owners if o.state == "pinned")
    )
    docs = {
        row["document_sha"]: store._load(row["canonical"])
        for row in store._rows(connection, "documents")
        if row["document_sha"]
        in {deployment.owner_registry_sha, deployment.preflight_sha}
    }
    registry, preflight = (
        docs[deployment.owner_registry_sha],
        docs[deployment.preflight_sha],
    )
    intent = context.owner_intent
    _check(
        registry.get("canonical_store_root") == intent.canonical_root
        and preflight.get("canonical_ledger_path") == intent.db_path
        and preflight.get("registry") == registry,
        "c1_source_binding_mismatch",
    )
    identity = preflight.get("identity")
    _check(
        type(identity) is dict
        and identity.get("account_ref") == intent.account_ref
        and store.digest_bytes(store.canonical_bytes(identity)) == intent.identity_sha,
        "identity_binding_mismatch",
    )
    events = sorted(
        store._rows(connection, "runtime_events"), key=lambda r: r["sequence"]
    )
    records = tuple(r["canonical"] for r in events)
    for row, data in zip(events, records, strict=True):
        event = RuntimeEvent.from_bytes(data)
        _check(
            (
                row["deployment_id"],
                row["sequence"],
                row["event_hash"],
                row["previous_hash"],
            )
            == (
                event.deployment_id,
                event.sequence,
                event.event_hash,
                event.previous_hash,
            ),
            "event_columns_mismatch",
        )
    projection = replay_runtime(context, records)
    current = store._rows(connection, "runtime_current")
    _check(len(current) == 1, "runtime_projection_missing")
    row = current[0]
    _check(
        (
            row["deployment_id"],
            row["event_count"],
            row["last_sequence"],
            row["head_sha"],
            row["canonical"],
        )
        == (
            deployment.deployment_id,
            projection.event_count,
            projection.event_count - 1,
            projection.head_sha,
            projection.canonical,
        ),
        "runtime_projection_mismatch",
    )
    sessions = store._rows(connection, "runtime_sessions")
    _check(len(sessions) == len(projection.sessions), "session_projection_mismatch")
    expected = {s.session_id: s for s in projection.sessions}
    for row in sessions:
        _check(row["session_id"] in expected, "session_projection_mismatch")
        session = expected[row["session_id"]]
        _check(
            (
                row["deployment_id"],
                row["fence_epoch"],
                row["start_event"],
                row["close_event"],
                row["canonical"],
            )
            == (
                deployment.deployment_id,
                session.fence_epoch,
                session.start_event,
                session.close_event,
                session.canonical,
            ),
            "session_projection_mismatch",
        )
    return projection
