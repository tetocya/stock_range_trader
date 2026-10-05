"""I1a-1 persistence formats and validation; no operational Store entry point.

Only the private schema initializer writes, to a supplied artificial connection.
There is no path selection, clock, activation, enrollment, sender or recovery.
Stored timestamps are fields for a future Store-owned clock, not caller authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from .http_contract import APPROVAL_SCHEMA, PLAN_SCHEMA, HttpContractError
from .live_body_contract import EVENT_SCHEMA as BODY_EVENT_SCHEMA
from .live_body_contract import BodyEvent
from .live_http_evidence import (
    LIVE_ACCOUNT_SCHEMA_V1,
    LIVE_ACCOUNT_SCHEMA_V2,
    LIVE_PLAN_SCHEMA,
    LiveJournal,
)
from .live_preflight import (
    LIVE_ACCOUNT_PREFLIGHT_SCHEMA,
    LIVE_ACCOUNT_REGISTRY_SCHEMA,
)

STORE_SCHEMA = "historical-feasibility-live-store-v1"
POLICY_SCHEMA = "historical-feasibility-live-store-policy-v1"
DEPLOYMENT_SCHEMA = "historical-feasibility-live-store-deployment-v1"
INTENT_SCHEMA = "historical-feasibility-live-store-operation-intent-v1"
RECEIPT_SCHEMA = "historical-feasibility-live-store-operation-receipt-v1"
MAX_ENROLLMENT_RECORD_BYTES = 32 * 1024
ZERO_HASH = "0" * 64
_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_DOCUMENT_SCHEMAS = {
    "plan": PLAN_SCHEMA,
    "preflight": LIVE_ACCOUNT_PREFLIGHT_SCHEMA,
    "approval": APPROVAL_SCHEMA,
    "owner_registry": LIVE_ACCOUNT_REGISTRY_SCHEMA,
}
_JOURNAL_SCHEMAS = {
    "account": (LIVE_ACCOUNT_SCHEMA_V1, LIVE_ACCOUNT_SCHEMA_V2),
    "plan": (LIVE_PLAN_SCHEMA,),
    "body": (BODY_EVENT_SCHEMA,),
}


def _require(condition, reason):
    if not condition:
        raise HttpContractError("live_store_" + reason)


def _label(value):
    _require(type(value) is str and _LABEL.fullmatch(value), "label_invalid")
    return value


def _sha(value):
    _require(type(value) is str and _SHA.fullmatch(value), "digest_invalid")
    return value


def _positive(value):
    _require(type(value) is int and 0 < value < 2**63, "positive_integer_required")
    return value


def _time(value):
    _require(type(value) is str, "utc_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value)
        valid = (
            parsed.tzinfo is not None
            and parsed.utcoffset().total_seconds() == 0
            and parsed.astimezone(UTC).isoformat(timespec="microseconds") == value
        )
    except (ValueError, OverflowError, AttributeError):
        valid = False
    _require(valid, "utc_timestamp_invalid")
    return value


def canonical_bytes(value):
    """Deterministic UTF-8 bytes; validates rather than normalizing saved bytes."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise HttpContractError("live_store_canonical_content_invalid") from exc


def digest_bytes(data):
    _require(type(data) is bytes and bool(data), "original_bytes_required")
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _constant(value):
    raise HttpContractError("live_store_nonfinite_json")


def _load(data, expected_sha=None):
    digest = digest_bytes(data)
    if expected_sha is not None:
        _require(digest == _sha(expected_sha), "digest_mismatch")
    try:
        raw = json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        raise HttpContractError("live_store_canonical_content_invalid") from exc
    _require(type(raw) is dict and canonical_bytes(raw) == data, "noncanonical_content")
    return raw


@dataclass(frozen=True)
class StorePolicy:
    """All capacities explicit; there is no production plan-count default."""

    max_enrolled_plans_per_account: int
    max_record_bytes: int
    max_retained_body_bytes: int
    max_cumulative_acquired_bytes: int

    def __post_init__(self):
        for value in (
            self.max_enrolled_plans_per_account,
            self.max_record_bytes,
            self.max_retained_body_bytes,
            self.max_cumulative_acquired_bytes,
        ):
            _positive(value)
        _require(
            self.max_record_bytes <= MAX_ENROLLMENT_RECORD_BYTES, "record_limit_invalid"
        )

    def to_dict(self):
        return {"schema": POLICY_SCHEMA, **self.__dict__}

    @property
    def canonical(self):
        return canonical_bytes(self.to_dict())

    @property
    def sha256(self):
        return digest_bytes(self.canonical)

    @classmethod
    def from_bytes(cls, data, expected_sha=None):
        raw = _load(data, expected_sha)
        _require(raw.get("schema") == POLICY_SCHEMA, "policy_schema_invalid")
        _require(
            set(raw) == {"schema", *cls.__dataclass_fields__}, "policy_fields_invalid"
        )
        return cls(**{k: v for k, v in raw.items() if k != "schema"})


@dataclass(frozen=True)
class DeploymentRecord:
    store_id: str
    deployment_id: str
    account_ref: str
    owner_registry_sha: str
    preflight_sha: str
    store_policy_sha: str
    created_at: str

    def __post_init__(self):
        for value in (self.store_id, self.deployment_id, self.account_ref):
            _label(value)
        for value in (
            self.owner_registry_sha,
            self.preflight_sha,
            self.store_policy_sha,
        ):
            _sha(value)
        _time(self.created_at)

    def to_dict(self):
        return {
            "schema": DEPLOYMENT_SCHEMA,
            "store_schema": STORE_SCHEMA,
            "lifecycle": "prepared",
            **self.__dict__,
        }

    @property
    def canonical(self):
        return canonical_bytes(self.to_dict())


@dataclass(frozen=True)
class CanonicalDocument:
    kind: str
    schema: str
    content: bytes
    sha256: str
    source_identity: str
    source_reference: str
    registered_at: str

    def __post_init__(self):
        _require(
            _DOCUMENT_SCHEMAS.get(self.kind) == self.schema, "document_schema_invalid"
        )
        raw = _load(self.content, self.sha256)
        _require(raw.get("schema") == self.schema, "document_schema_invalid")
        _label(self.source_identity)
        _require(
            type(self.source_reference) is str
            and bool(self.source_reference)
            and len(self.source_reference) <= 1024,
            "source_reference_invalid",
        )
        _time(self.registered_at)


@dataclass(frozen=True)
class OperationIntent:
    kind: str
    account_ref: str
    plan_sha: str | None
    payload: bytes

    def __post_init__(self):
        _label(self.kind)
        _label(self.account_ref)
        if self.plan_sha is not None:
            _sha(self.plan_sha)
        _load(self.payload)

    @property
    def canonical(self):
        return canonical_bytes(
            {
                "schema": INTENT_SCHEMA,
                "kind": self.kind,
                "account_ref": self.account_ref,
                "plan_sha": self.plan_sha,
                "payload": _load(self.payload),
            }
        )

    @property
    def sha256(self):
        return digest_bytes(self.canonical)

    @classmethod
    def from_bytes(cls, data, expected_sha=None):
        raw = _load(data, expected_sha)
        _require(
            set(raw) == {"schema", "kind", "account_ref", "plan_sha", "payload"}
            and raw["schema"] == INTENT_SCHEMA,
            "intent_schema_invalid",
        )
        return cls(
            raw["kind"],
            raw["account_ref"],
            raw["plan_sha"],
            canonical_bytes(raw["payload"]),
        )


def validate_enrollment_capacity(policy, historical_enrolled_plan_shas, record):
    """Checks a *new* enrollment against all historically enrolled identities.

    The later mutation must obtain this complete set inside its transaction.
    Prepared plans do not consume enrollment capacity; stopped enrollments do.
    """
    _require(type(policy) is StorePolicy, "policy_required")
    _require(type(historical_enrolled_plan_shas) is tuple, "plan_history_required")
    for value in historical_enrolled_plan_shas:
        _sha(value)
    _require(
        len(set(historical_enrolled_plan_shas)) == len(historical_enrolled_plan_shas),
        "duplicate_plan_history",
    )
    _require(
        len(historical_enrolled_plan_shas) < policy.max_enrolled_plans_per_account,
        "plan_count_exceeded",
    )
    _load(record)
    _require(len(record) <= policy.max_record_bytes, "enrollment_record_oversize")
    return len(record)


# Explicit SQL; no caller-provided identifiers or paths are interpolated.
_DDL = (
    """CREATE TABLE policies (
        policy_sha TEXT PRIMARY KEY, schema TEXT NOT NULL,
        canonical BLOB NOT NULL CHECK(typeof(canonical)='blob' AND length(canonical)>0),
        max_plans INTEGER NOT NULL CHECK(typeof(max_plans)='integer' AND max_plans>0),
        max_record INTEGER NOT NULL CHECK(typeof(max_record)='integer' AND max_record BETWEEN 1 AND 32768),
        retained_bytes INTEGER NOT NULL CHECK(typeof(retained_bytes)='integer' AND retained_bytes>0),
        acquired_bytes INTEGER NOT NULL CHECK(typeof(acquired_bytes)='integer' AND acquired_bytes>0)
    )""",
    """CREATE TABLE documents (
        deployment_id TEXT NOT NULL, document_sha TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('plan','preflight','approval','owner_registry')),
        schema TEXT NOT NULL,
        canonical BLOB NOT NULL CHECK(typeof(canonical)='blob' AND length(canonical)>0),
        source_identity TEXT NOT NULL, source_reference TEXT NOT NULL,
        registered_at TEXT NOT NULL,
        PRIMARY KEY(deployment_id,document_sha),
        UNIQUE(deployment_id,kind,schema,document_sha),
        FOREIGN KEY(deployment_id) REFERENCES deployments(deployment_id)
    )""",
    """CREATE TABLE deployments (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        deployment_id TEXT NOT NULL UNIQUE, store_id TEXT NOT NULL UNIQUE,
        account_ref TEXT NOT NULL, policy_sha TEXT NOT NULL,
        owner_registry_sha TEXT NOT NULL, preflight_sha TEXT NOT NULL,
        owner_kind TEXT NOT NULL CHECK(owner_kind='owner_registry'),
        owner_schema TEXT NOT NULL, preflight_kind TEXT NOT NULL CHECK(preflight_kind='preflight'),
        preflight_schema TEXT NOT NULL, created_at TEXT NOT NULL,
        lifecycle TEXT NOT NULL CHECK(lifecycle='prepared'),
        canonical BLOB NOT NULL CHECK(typeof(canonical)='blob' AND length(canonical)>0),
        UNIQUE(deployment_id,account_ref),
        FOREIGN KEY(policy_sha) REFERENCES policies(policy_sha),
        FOREIGN KEY(deployment_id,owner_kind,owner_schema,owner_registry_sha)
            REFERENCES documents(deployment_id,kind,schema,document_sha) DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY(deployment_id,preflight_kind,preflight_schema,preflight_sha)
            REFERENCES documents(deployment_id,kind,schema,document_sha) DEFERRABLE INITIALLY DEFERRED
    )""",
    """CREATE TABLE store_metadata (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema TEXT NOT NULL, deployment_id TEXT NOT NULL UNIQUE,
        canonical BLOB NOT NULL CHECK(typeof(canonical)='blob' AND length(canonical)>0),
        FOREIGN KEY(deployment_id) REFERENCES deployments(deployment_id)
    )""",
    """CREATE TABLE accounts (
        deployment_id TEXT NOT NULL, account_ref TEXT NOT NULL,
        schema TEXT NOT NULL, policy_sha TEXT NOT NULL,
        journal_id TEXT, enrollment_revision INTEGER NOT NULL CHECK(typeof(enrollment_revision)='integer' AND enrollment_revision>=0),
        request_generation INTEGER NOT NULL CHECK(typeof(request_generation)='integer' AND request_generation>=0),
        status TEXT NOT NULL CHECK(status IN ('prepared','stopped')),
        PRIMARY KEY(deployment_id,account_ref),
        FOREIGN KEY(deployment_id,account_ref) REFERENCES deployments(deployment_id,account_ref),
        FOREIGN KEY(policy_sha) REFERENCES policies(policy_sha),
        FOREIGN KEY(deployment_id,journal_id) REFERENCES journals(deployment_id,journal_id)
    )""",
    """CREATE TABLE plans (
        deployment_id TEXT NOT NULL, account_ref TEXT NOT NULL, plan_sha TEXT NOT NULL,
        preflight_sha TEXT NOT NULL, approval_sha TEXT NOT NULL, policy_sha TEXT NOT NULL,
        plan_kind TEXT NOT NULL CHECK(plan_kind='plan'), plan_document_schema TEXT NOT NULL,
        preflight_kind TEXT NOT NULL CHECK(preflight_kind='preflight'), preflight_document_schema TEXT NOT NULL,
        approval_kind TEXT NOT NULL CHECK(approval_kind='approval'), approval_document_schema TEXT NOT NULL,
        schema TEXT NOT NULL, journal_id TEXT,
        status TEXT NOT NULL CHECK(status IN ('prepared','enrolled','stopped')),
        enrolled_revision INTEGER CHECK(enrolled_revision IS NULL OR (typeof(enrolled_revision)='integer' AND enrolled_revision>0)),
        CHECK((status='prepared' AND enrolled_revision IS NULL) OR (status IN ('enrolled','stopped') AND enrolled_revision IS NOT NULL)),
        PRIMARY KEY(deployment_id,plan_sha),
        UNIQUE(deployment_id,account_ref,plan_sha),
        UNIQUE(deployment_id,account_ref,enrolled_revision),
        FOREIGN KEY(deployment_id,account_ref) REFERENCES accounts(deployment_id,account_ref),
        FOREIGN KEY(policy_sha) REFERENCES policies(policy_sha),
        FOREIGN KEY(deployment_id,plan_kind,plan_document_schema,plan_sha) REFERENCES documents(deployment_id,kind,schema,document_sha),
        FOREIGN KEY(deployment_id,preflight_kind,preflight_document_schema,preflight_sha) REFERENCES documents(deployment_id,kind,schema,document_sha),
        FOREIGN KEY(deployment_id,approval_kind,approval_document_schema,approval_sha) REFERENCES documents(deployment_id,kind,schema,document_sha),
        FOREIGN KEY(deployment_id,journal_id) REFERENCES journals(deployment_id,journal_id)
    )""",
    """CREATE TABLE journals (
        deployment_id TEXT NOT NULL, journal_id TEXT NOT NULL, account_ref TEXT NOT NULL,
        plan_sha TEXT, kind TEXT NOT NULL CHECK(kind IN ('account','plan','body','control')),
        schema TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('prepared','stopped')),
        CHECK((kind IN ('account','control') AND plan_sha IS NULL) OR (kind IN ('plan','body') AND plan_sha IS NOT NULL)),
        PRIMARY KEY(deployment_id,journal_id),
        UNIQUE(deployment_id,journal_id,schema),
        FOREIGN KEY(deployment_id,account_ref) REFERENCES accounts(deployment_id,account_ref),
        FOREIGN KEY(deployment_id,account_ref,plan_sha) REFERENCES plans(deployment_id,account_ref,plan_sha)
    )""",
    """CREATE TABLE events (
        deployment_id TEXT NOT NULL, journal_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(typeof(sequence)='integer' AND sequence>=0),
        schema TEXT NOT NULL, canonical BLOB NOT NULL CHECK(typeof(canonical)='blob' AND length(canonical)>0),
        event_hash TEXT NOT NULL, previous_hash TEXT NOT NULL, recorded_at TEXT NOT NULL,
        transition_id TEXT NOT NULL, session_ref TEXT, fence_ref INTEGER CHECK(fence_ref IS NULL OR (typeof(fence_ref)='integer' AND fence_ref>0)),
        PRIMARY KEY(deployment_id,journal_id,sequence),
        UNIQUE(deployment_id,journal_id,event_hash),
        UNIQUE(deployment_id,journal_id,sequence,event_hash),
        FOREIGN KEY(deployment_id,journal_id,schema) REFERENCES journals(deployment_id,journal_id,schema)
    )""",
    """CREATE TABLE journal_heads (
        deployment_id TEXT NOT NULL, journal_id TEXT NOT NULL, event_count INTEGER NOT NULL CHECK(typeof(event_count)='integer' AND event_count>=0),
        head_sha TEXT NOT NULL, last_sequence INTEGER,
        CHECK((event_count=0 AND last_sequence IS NULL AND head_sha='0000000000000000000000000000000000000000000000000000000000000000') OR (event_count>0 AND last_sequence IS NOT NULL AND typeof(last_sequence)='integer' AND last_sequence=event_count-1)),
        PRIMARY KEY(deployment_id,journal_id),
        FOREIGN KEY(deployment_id,journal_id) REFERENCES journals(deployment_id,journal_id),
        FOREIGN KEY(deployment_id,journal_id,last_sequence,head_sha) REFERENCES events(deployment_id,journal_id,sequence,event_hash)
    )""",
    """CREATE TABLE operations (
        deployment_id TEXT NOT NULL, operation_id TEXT NOT NULL, account_ref TEXT NOT NULL,
        plan_sha TEXT, kind TEXT NOT NULL, intent_schema TEXT NOT NULL, intent_sha TEXT NOT NULL,
        canonical_intent BLOB NOT NULL CHECK(typeof(canonical_intent)='blob' AND length(canonical_intent)>0),
        required_roles BLOB NOT NULL CHECK(typeof(required_roles)='blob'),
        execution_preconditions BLOB NOT NULL CHECK(typeof(execution_preconditions)='blob'),
        status TEXT NOT NULL CHECK(status IN ('pending','committed')),
        created_at TEXT NOT NULL, committed_at TEXT, receipt_sha TEXT,
        session_ref TEXT, fence_ref INTEGER CHECK(fence_ref IS NULL OR (typeof(fence_ref)='integer' AND fence_ref>0)),
        CHECK((status='pending' AND committed_at IS NULL AND receipt_sha IS NULL) OR (status='committed' AND committed_at IS NOT NULL AND receipt_sha IS NOT NULL)),
        PRIMARY KEY(deployment_id,operation_id),
        FOREIGN KEY(deployment_id,account_ref) REFERENCES accounts(deployment_id,account_ref),
        FOREIGN KEY(deployment_id,account_ref,plan_sha) REFERENCES plans(deployment_id,account_ref,plan_sha),
        FOREIGN KEY(deployment_id,operation_id,receipt_sha) REFERENCES receipts(deployment_id,operation_id,receipt_sha) DEFERRABLE INITIALLY DEFERRED
    )""",
    """CREATE TABLE event_parts (
        deployment_id TEXT NOT NULL, operation_id TEXT NOT NULL, role TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal>=0),
        journal_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_hash TEXT NOT NULL,
        result_head TEXT NOT NULL CHECK(result_head=event_hash),
        PRIMARY KEY(deployment_id,operation_id,role),
        UNIQUE(deployment_id,operation_id,ordinal),
        UNIQUE(deployment_id,journal_id,sequence),
        FOREIGN KEY(deployment_id,operation_id) REFERENCES operations(deployment_id,operation_id),
        FOREIGN KEY(deployment_id,journal_id,sequence,event_hash) REFERENCES events(deployment_id,journal_id,sequence,event_hash)
    )""",
    """CREATE TABLE operation_head_references (
        deployment_id TEXT NOT NULL, operation_id TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('source','result')),
        journal_id TEXT NOT NULL, sequence INTEGER, head_sha TEXT NOT NULL,
        CHECK((sequence IS NULL AND head_sha='0000000000000000000000000000000000000000000000000000000000000000') OR (sequence IS NOT NULL AND typeof(sequence)='integer' AND sequence>=0)),
        PRIMARY KEY(deployment_id,operation_id,phase,journal_id),
        FOREIGN KEY(deployment_id,operation_id) REFERENCES operations(deployment_id,operation_id),
        FOREIGN KEY(deployment_id,journal_id) REFERENCES journals(deployment_id,journal_id),
        FOREIGN KEY(deployment_id,journal_id,sequence,head_sha) REFERENCES events(deployment_id,journal_id,sequence,event_hash)
    )""",
    """CREATE TABLE receipts (
        deployment_id TEXT NOT NULL, operation_id TEXT NOT NULL, schema TEXT NOT NULL,
        receipt_sha TEXT NOT NULL, canonical BLOB NOT NULL CHECK(typeof(canonical)='blob' AND length(canonical)>0),
        committed_at TEXT NOT NULL,
        PRIMARY KEY(deployment_id,operation_id),
        UNIQUE(deployment_id,operation_id,receipt_sha),
        FOREIGN KEY(deployment_id,operation_id) REFERENCES operations(deployment_id,operation_id)
    )""",
    """CREATE TABLE control_state (
        deployment_id TEXT NOT NULL, account_ref TEXT NOT NULL, journal_id TEXT NOT NULL,
        head_sha TEXT NOT NULL, sequence INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status='stopped'), reason TEXT NOT NULL,
        PRIMARY KEY(deployment_id,account_ref),
        FOREIGN KEY(deployment_id,account_ref) REFERENCES accounts(deployment_id,account_ref),
        FOREIGN KEY(deployment_id,journal_id,sequence,head_sha) REFERENCES events(deployment_id,journal_id,sequence,event_hash)
    )""",
)


def _immutable(table, keys):
    # Names are internal fixed literals, never caller-controlled SQL identifiers.
    match = " AND ".join(f"{k}=NEW.{k}" for k in keys)
    return (
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'immutable_{table}'); END",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'immutable_{table}'); END",
        f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {match}) BEGIN SELECT RAISE(ABORT,'immutable_{table}'); END",
    )


_TRIGGERS = tuple(
    sql
    for table, keys in (
        ("policies", ("policy_sha",)),
        ("documents", ("deployment_id", "document_sha")),
        ("deployments", ("singleton",)),
        ("store_metadata", ("singleton",)),
        ("events", ("deployment_id", "journal_id", "sequence")),
        ("event_parts", ("deployment_id", "operation_id", "role")),
        (
            "operation_head_references",
            ("deployment_id", "operation_id", "phase", "journal_id"),
        ),
        ("receipts", ("deployment_id", "operation_id")),
    )
    for sql in _immutable(table, keys)
) + (
    """CREATE TRIGGER operation_intent_immutable BEFORE UPDATE ON operations
    WHEN OLD.status='committed' OR NEW.deployment_id<>OLD.deployment_id OR NEW.operation_id<>OLD.operation_id
      OR NEW.account_ref<>OLD.account_ref OR NEW.plan_sha IS NOT OLD.plan_sha OR NEW.kind<>OLD.kind
      OR NEW.intent_schema<>OLD.intent_schema OR NEW.intent_sha<>OLD.intent_sha
      OR NEW.canonical_intent<>OLD.canonical_intent OR NEW.required_roles<>OLD.required_roles
      OR NEW.created_at<>OLD.created_at
    BEGIN SELECT RAISE(ABORT,'immutable_operation'); END""",
    "CREATE TRIGGER operations_no_delete BEFORE DELETE ON operations BEGIN SELECT RAISE(ABORT,'immutable_operation'); END",
    """CREATE TRIGGER operations_no_replace BEFORE INSERT ON operations
    WHEN EXISTS(SELECT 1 FROM operations WHERE deployment_id=NEW.deployment_id AND operation_id=NEW.operation_id)
    BEGIN SELECT RAISE(ABORT,'operation_id_conflict'); END""",
)


def _connection(connection):
    _require(type(connection) is sqlite3.Connection, "sqlite_connection_required")
    _require(
        connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1,
        "foreign_keys_required",
    )
    _require(
        connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 1,
        "recursive_triggers_required",
    )


def _initialize_schema(connection, deployment, policy, documents):
    """Artificial/test-only initializer. No physical Store or readiness result.

    The caller supplies a new SQLite connection, FK/trigger enforcement and
    explicit source documents. There is no path API, migration or activation.
    The transaction rolls back DDL and records together on failure.
    """
    _connection(connection)
    _require(not connection.in_transaction, "existing_transaction")
    _require(
        not connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall(),
        "existing_database",
    )
    _require(
        type(deployment) is DeploymentRecord and type(policy) is StorePolicy,
        "bootstrap_records_required",
    )
    deployment = DeploymentRecord(**deployment.__dict__)
    policy = StorePolicy.from_bytes(policy.canonical)
    _require(deployment.store_policy_sha == policy.sha256, "policy_binding_mismatch")
    _require(
        type(documents) is tuple
        and all(type(d) is CanonicalDocument for d in documents),
        "documents_required",
    )
    with connection:
        connection.execute("BEGIN")
        for sql in (*_DDL, *_TRIGGERS):
            connection.execute(sql)
        connection.execute(
            "INSERT INTO policies VALUES (?,?,?,?,?,?,?)",
            (
                policy.sha256,
                POLICY_SCHEMA,
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
                deployment.deployment_id,
                deployment.store_id,
                deployment.account_ref,
                policy.sha256,
                deployment.owner_registry_sha,
                deployment.preflight_sha,
                "owner_registry",
                LIVE_ACCOUNT_REGISTRY_SCHEMA,
                "preflight",
                LIVE_ACCOUNT_PREFLIGHT_SCHEMA,
                deployment.created_at,
                "prepared",
                deployment.canonical,
            ),
        )
        for document in documents:
            # Revalidate frozen values too; do not treat a supplied digest as proof.
            checked = CanonicalDocument(**document.__dict__)
            connection.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
                (
                    deployment.deployment_id,
                    checked.sha256,
                    checked.kind,
                    checked.schema,
                    checked.content,
                    checked.source_identity,
                    checked.source_reference,
                    checked.registered_at,
                ),
            )
        metadata = canonical_bytes(
            {"schema": STORE_SCHEMA, "deployment_id": deployment.deployment_id}
        )
        connection.execute(
            "INSERT INTO store_metadata VALUES (1,?,?,?)",
            (STORE_SCHEMA, deployment.deployment_id, metadata),
        )
        validate_store_contents(connection)


def validate_store_schema(connection):
    """Read-only identity/DDL validation. user_version is not schema authority."""
    _connection(connection)
    actual = sorted(
        row[0]
        for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        )
    )
    _require(actual == sorted((*_DDL, *_TRIGGERS)), "schema_definition_mismatch")
    rows = connection.execute(
        "SELECT schema,deployment_id,canonical FROM store_metadata"
    ).fetchall()
    _require(len(rows) == 1, "metadata_missing")
    schema, deployment_id, data = rows[0]
    _require(schema == STORE_SCHEMA, "schema_identity_invalid")
    _require(
        _load(data) == {"schema": STORE_SCHEMA, "deployment_id": deployment_id},
        "metadata_binding_invalid",
    )
    _require(
        not connection.execute("PRAGMA foreign_key_check").fetchall(),
        "foreign_key_violation",
    )
    return STORE_SCHEMA


def _rows(connection, table):
    # Only internal constant table names are passed here.
    cursor = connection.execute(f"SELECT * FROM {table}")
    keys = tuple(item[0] for item in cursor.description)
    return tuple(dict(zip(keys, row, strict=True)) for row in cursor.fetchall())


def _roles(data):
    raw = _load(data)
    _require(
        set(raw) == {"roles"} and type(raw["roles"]) is list and bool(raw["roles"]),
        "required_roles_invalid",
    )
    roles = tuple(_label(x) for x in raw["roles"])
    _require(len(set(roles)) == len(roles), "required_roles_invalid")
    return roles


def validate_store_contents(connection):
    """Read-only relational/byte/chain/completeness validator; never repairs."""
    validate_store_schema(connection)
    _validate_relational_contents(connection)
    return STORE_SCHEMA


def _validate_relational_contents(
    connection,
    *,
    deployment_schema=DEPLOYMENT_SCHEMA,
    store_schema=STORE_SCHEMA,
    initial_lifecycle="prepared",
):
    """Shared content checks after the caller's exact versioned DDL validation.

    v2 has a different deployment envelope and a separate runtime lifecycle.
    Existing policy/document/business journal/receipt bytes are never rewritten.
    """
    policies = {}
    for row in _rows(connection, "policies"):
        policy = StorePolicy.from_bytes(row["canonical"], row["policy_sha"])
        _require(
            row["schema"] == POLICY_SCHEMA
            and (
                row["max_plans"],
                row["max_record"],
                row["retained_bytes"],
                row["acquired_bytes"],
            )
            == tuple(policy.__dict__.values()),
            "policy_columns_mismatch",
        )
        policies[row["policy_sha"]] = policy
    deployment = _rows(connection, "deployments")
    _require(len(deployment) == 1, "deployment_missing")
    d = deployment[0]
    record = DeploymentRecord(
        **{
            k: d[k] if k != "store_policy_sha" else d["policy_sha"]
            for k in DeploymentRecord.__dataclass_fields__
        }
    )
    expected_deployment = canonical_bytes(
        {
            **record.__dict__,
            "schema": deployment_schema,
            "store_schema": store_schema,
            "lifecycle": initial_lifecycle,
        }
    )
    _require(
        expected_deployment == d["canonical"] and d["lifecycle"] == initial_lifecycle,
        "deployment_binding_mismatch",
    )
    for row in _rows(connection, "documents"):
        CanonicalDocument(
            row["kind"],
            row["schema"],
            row["canonical"],
            row["document_sha"],
            row["source_identity"],
            row["source_reference"],
            row["registered_at"],
        )
    accounts = _rows(connection, "accounts")
    plans = _rows(connection, "plans")
    journals = {
        (r["deployment_id"], r["journal_id"]): r for r in _rows(connection, "journals")
    }
    events = _rows(connection, "events")
    heads = {
        (r["deployment_id"], r["journal_id"]): r
        for r in _rows(connection, "journal_heads")
    }
    projections = {}
    for row in accounts:
        _label(row["account_ref"])
        _require(
            row["schema"] in _JOURNAL_SCHEMAS["account"]
            and row["policy_sha"] == d["policy_sha"],
            "account_binding_invalid",
        )
        enrolled = [
            p
            for p in plans
            if p["account_ref"] == row["account_ref"]
            and p["enrolled_revision"] is not None
        ]
        _require(
            len(enrolled) <= policies[row["policy_sha"]].max_enrolled_plans_per_account,
            "plan_count_exceeded",
        )
        _require(
            sorted(p["enrolled_revision"] for p in enrolled)
            == list(range(1, row["enrollment_revision"] + 1)),
            "enrollment_revision_invalid",
        )
        if row["journal_id"] is not None:
            journal = journals[(row["deployment_id"], row["journal_id"])]
            _require(
                journal["kind"] == "account"
                and journal["schema"] == row["schema"]
                and journal["account_ref"] == row["account_ref"],
                "account_journal_binding_invalid",
            )
    for row in plans:
        _require(
            row["schema"] == LIVE_PLAN_SCHEMA
            and row["preflight_sha"] == d["preflight_sha"],
            "plan_binding_invalid",
        )
        if row["journal_id"] is not None:
            journal = journals[(row["deployment_id"], row["journal_id"])]
            _require(
                journal["kind"] == "plan"
                and journal["plan_sha"] == row["plan_sha"]
                and journal["schema"] == row["schema"],
                "plan_journal_binding_invalid",
            )
        _require(
            row["status"] == "prepared" or row["journal_id"] is not None,
            "enrolled_journal_missing",
        )
    for key, journal in journals.items():
        _label(journal["journal_id"])
        _require(
            journal["schema"] in _JOURNAL_SCHEMAS.get(journal["kind"], ()),
            "journal_schema_unsupported",
        )
        chain = sorted(
            (r for r in events if (r["deployment_id"], r["journal_id"]) == key),
            key=lambda r: r["sequence"],
        )
        previous, last_time = ZERO_HASH, None
        for sequence, row in enumerate(chain):
            raw = _load(row["canonical"])
            _require(
                row["sequence"] == sequence and row["previous_hash"] == previous,
                "event_chain_invalid",
            )
            _require(
                raw.get("schema") == journal["schema"]
                and raw.get("sequence") == sequence
                and raw.get("previous_hash") == previous
                and raw.get("event_hash") == row["event_hash"],
                "event_metadata_mismatch",
            )
            expected = digest_bytes(
                canonical_bytes({k: v for k, v in raw.items() if k != "event_hash"})
            )
            _require(expected == _sha(row["event_hash"]), "event_hash_mismatch")
            event = raw.get("event")
            _require(type(event) is dict, "event_content_invalid")
            if journal["kind"] == "body":
                _require(
                    set(raw)
                    == {
                        "schema",
                        "sequence",
                        "previous_hash",
                        "contract_sha",
                        "root_identity_sha",
                        "event",
                        "event_hash",
                    },
                    "body_envelope_invalid",
                )
                _sha(raw["contract_sha"])
                _sha(raw["root_identity_sha"])
                parsed = BodyEvent.from_dict(event)
                _require(
                    parsed.kind
                    in {
                        "audit",
                        "reserved",
                        "writer",
                        "observed",
                        "stable",
                        "acquired",
                        "committed",
                        "quarantined",
                    },
                    "body_event_kind_unsupported",
                )
            timestamp = _time(row["recorded_at"])
            transition = event.get("transition_id", event.get("operation_id"))
            _require(
                event.get("recorded_at") == timestamp
                and transition == _label(row["transition_id"]),
                "event_metadata_mismatch",
            )
            _require(
                last_time is None or last_time <= timestamp, "event_clock_regressed"
            )
            _require(
                len(row["canonical"]) <= policies[d["policy_sha"]].max_record_bytes,
                "event_record_oversize",
            )
            previous, last_time = row["event_hash"], timestamp
        if journal["kind"] in {"account", "plan"} and chain:
            projections[key] = LiveJournal(
                journal["schema"], tuple(r["canonical"].decode("utf-8") for r in chain)
            ).projection
        _require(key in heads, "current_head_missing")
        head = heads[key]
        _require(
            head["event_count"] == len(chain) and head["head_sha"] == previous,
            "current_head_mismatch",
        )
    for row in accounts:
        if row["journal_id"] is None:
            _require(
                row["enrollment_revision"] == row["request_generation"] == 0,
                "account_journal_missing",
            )
        else:
            projection = projections.get((row["deployment_id"], row["journal_id"]))
            _require(
                projection is not None
                and projection.generation == row["request_generation"],
                "account_projection_mismatch",
            )
            if row["schema"] == LIVE_ACCOUNT_SCHEMA_V2:
                _require(
                    projection.enrollment_revision == row["enrollment_revision"],
                    "account_projection_mismatch",
                )
                catalog = sorted(
                    (
                        p
                        for p in plans
                        if p["account_ref"] == row["account_ref"]
                        and p["enrolled_revision"] is not None
                    ),
                    key=lambda p: p["enrolled_revision"],
                )
                _require(
                    tuple(p["plan_sha"] for p in catalog)
                    == tuple(e.scope.plan_sha for e in projection.enrollments),
                    "enrolled_catalog_mismatch",
                )
    for row in plans:
        if row["status"] != "prepared":
            projection = projections.get((row["deployment_id"], row["journal_id"]))
            _require(
                projection is not None and len(projection.scopes) == 1,
                "plan_projection_missing",
            )
            scope = projection.scopes[0]
            _require(
                scope.plan_sha == row["plan_sha"]
                and scope.account_ref == row["account_ref"]
                and scope.preflight_sha == row["preflight_sha"]
                and scope.approval_sha == row["approval_sha"],
                "plan_projection_mismatch",
            )
    parts = _rows(connection, "event_parts")
    references = _rows(connection, "operation_head_references")
    receipts = {
        (r["deployment_id"], r["operation_id"]): r
        for r in _rows(connection, "receipts")
    }
    for row in _rows(connection, "operations"):
        _label(row["operation_id"])
        _label(row["kind"])
        intent = OperationIntent.from_bytes(row["canonical_intent"], row["intent_sha"])
        _require(row["intent_schema"] == INTENT_SCHEMA, "intent_schema_invalid")
        _require(
            intent.kind == row["kind"]
            and intent.account_ref == row["account_ref"]
            and intent.plan_sha == row["plan_sha"],
            "intent_binding_mismatch",
        )
        _load(row["execution_preconditions"])
        roles = _roles(row["required_roles"])
        _time(row["created_at"])
        key = row["deployment_id"], row["operation_id"]
        current_parts = sorted(
            (p for p in parts if (p["deployment_id"], p["operation_id"]) == key),
            key=lambda p: p["ordinal"],
        )
        _require(
            all(p["role"] in roles for p in current_parts), "unexpected_event_part"
        )
        for part in current_parts:
            event = next(
                e
                for e in events
                if e["journal_id"] == part["journal_id"]
                and e["sequence"] == part["sequence"]
            )
            _require(
                event["transition_id"] == row["operation_id"],
                "event_operation_mismatch",
            )
            journal = journals[(part["deployment_id"], part["journal_id"])]
            _require(
                journal["account_ref"] == row["account_ref"]
                and journal["plan_sha"] in {None, row["plan_sha"]},
                "event_part_scope_mismatch",
            )
        for ref in references:
            if (ref["deployment_id"], ref["operation_id"]) == key:
                journal = journals[(ref["deployment_id"], ref["journal_id"])]
                _require(
                    journal["account_ref"] == row["account_ref"]
                    and journal["plan_sha"] in {None, row["plan_sha"]},
                    "operation_head_scope_mismatch",
                )
        if row["status"] == "pending":
            _require(key not in receipts, "pending_operation_has_receipt")
            continue
        _require(key in receipts, "committed_receipt_missing")
        receipt = receipts[key]
        content = _load(receipt["canonical"], receipt["receipt_sha"])
        _require(
            receipt["schema"] == RECEIPT_SCHEMA
            and receipt["receipt_sha"] == row["receipt_sha"],
            "receipt_binding_mismatch",
        )
        _require(
            tuple(p["role"] for p in current_parts) == roles
            and [p["ordinal"] for p in current_parts] == list(range(len(roles))),
            "required_event_parts_missing",
        )
        expected_parts = [
            {
                k: p[k]
                for k in (
                    "role",
                    "ordinal",
                    "journal_id",
                    "sequence",
                    "event_hash",
                    "result_head",
                )
            }
            for p in current_parts
        ]
        for ref in references:
            if (ref["deployment_id"], ref["operation_id"]) == key and ref[
                "phase"
            ] == "result":
                matching = [
                    p for p in current_parts if p["journal_id"] == ref["journal_id"]
                ]
                _require(
                    bool(matching)
                    and (ref["sequence"], ref["head_sha"])
                    == (matching[-1]["sequence"], matching[-1]["result_head"]),
                    "operation_result_head_mismatch",
                )
        expected = {
            "schema": RECEIPT_SCHEMA,
            "deployment_id": row["deployment_id"],
            "operation_id": row["operation_id"],
            "intent_sha": row["intent_sha"],
            "committed_at": row["committed_at"],
            "parts": expected_parts,
        }
        _require(
            content == expected
            and receipt["committed_at"] == _time(row["committed_at"])
            and row["created_at"] <= row["committed_at"],
            "receipt_content_mismatch",
        )


@dataclass(frozen=True)
class OperationReplay:
    classification: str
    receipt: bytes | None


def assess_operation_replay(connection, deployment_id, operation_id, canonical_intent):
    """Read-only schema capability; no current mutation or recovery API.

    Execution timestamps/expected heads live in separate preconditions columns.
    Later unrelated pending operations do not replace a committed receipt.
    """
    _label(deployment_id)
    _label(operation_id)
    OperationIntent.from_bytes(canonical_intent)
    validate_store_contents(connection)
    row = connection.execute(
        "SELECT canonical_intent,status FROM operations WHERE deployment_id=? AND operation_id=?",
        (deployment_id, operation_id),
    ).fetchone()
    if row is None:
        return OperationReplay("absent", None)
    _require(row[0] == canonical_intent, "operation_intent_conflict")
    if row[1] == "pending":
        return OperationReplay("pending_recovery_required", None)
    receipt = connection.execute(
        "SELECT canonical FROM receipts WHERE deployment_id=? AND operation_id=?",
        (deployment_id, operation_id),
    ).fetchone()[0]
    return OperationReplay("committed", receipt)
