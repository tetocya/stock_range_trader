"""Artificial SQLite registries; no physical live Store, network or activation."""

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest
from test_feasibility_live_plan_enrollment import enroll, first, sources, stamp
from test_feasibility_live_preflight import NOW

from feasibility import live_store as store
from feasibility.http_contract import HttpContractError
from feasibility.live_http_evidence import LIVE_ACCOUNT_SCHEMA_V2, LIVE_PLAN_SCHEMA

UTC_TIME = stamp(NOW)


def document(kind, raw, reference="owner-reference"):
    content = store.canonical_bytes(raw)
    return store.CanonicalDocument(
        kind,
        raw["schema"],
        content,
        hashlib.sha256(content).hexdigest(),
        "owner-source",
        reference,
        UTC_TIME,
    )


def insert(connection, table, **values):
    # Test-only direct SQL fixtures. No production insertion/mutation API.
    names = tuple(values)
    connection.execute(
        f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
        tuple(values.values()),
    )


def insert_document(connection, doc):
    insert(
        connection,
        "documents",
        deployment_id="deployment-a",
        document_sha=doc.sha256,
        kind=doc.kind,
        schema=doc.schema,
        canonical=doc.content,
        source_identity=doc.source_identity,
        source_reference=doc.source_reference,
        registered_at=doc.registered_at,
    )


def new_connection(path=":memory:"):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA recursive_triggers=ON")
    return connection


def policy(n=2):
    return store.StorePolicy(n, 32768, 100000, 200000)


def bootstrap(connection, *, documents=None):
    src = sources()
    preflight = document("preflight", src["preflight"].to_dict())
    registry = document("owner_registry", src["preflight"].registry.to_dict())
    record = store.DeploymentRecord(
        "store-a",
        "deployment-a",
        src["plan"].account_ref,
        registry.sha256,
        preflight.sha256,
        policy().sha256,
        UTC_TIME,
    )
    store._initialize_schema(
        connection,
        record,
        policy(),
        documents if documents is not None else (registry, preflight),
    )
    return src, record


@pytest.fixture
def db():
    connection = new_connection()
    src, record = bootstrap(connection)
    insert(
        connection,
        "accounts",
        deployment_id=record.deployment_id,
        account_ref=record.account_ref,
        schema=LIVE_ACCOUNT_SCHEMA_V2,
        policy_sha=policy().sha256,
        enrollment_revision=0,
        request_generation=0,
        status="prepared",
    )
    connection.commit()
    yield connection, src, record
    connection.close()


def catalog_plan(db, *, name="plan-a"):
    connection, src, record = db
    src = sources(name)
    plan = document("plan", src["plan"].to_dict())
    approval = document("approval", src["approval"].to_dict())
    insert_document(connection, plan)
    insert_document(connection, approval)
    insert(
        connection,
        "plans",
        deployment_id=record.deployment_id,
        account_ref=record.account_ref,
        plan_sha=plan.sha256,
        preflight_sha=record.preflight_sha,
        approval_sha=approval.sha256,
        policy_sha=policy().sha256,
        plan_kind="plan",
        plan_document_schema=plan.schema,
        preflight_kind="preflight",
        preflight_document_schema=store.LIVE_ACCOUNT_PREFLIGHT_SCHEMA,
        approval_kind="approval",
        approval_document_schema=approval.schema,
        schema=LIVE_PLAN_SCHEMA,
        status="prepared",
    )
    return plan.sha256


def add_journal(db, journal, journal_id, kind, plan_sha=None):
    connection, _, record = db
    schema = store.BODY_EVENT_SCHEMA if kind == "body" else journal.schema
    insert(
        connection,
        "journals",
        deployment_id=record.deployment_id,
        account_ref=record.account_ref,
        plan_sha=plan_sha,
        journal_id=journal_id,
        kind=kind,
        schema=schema,
        status="prepared",
    )
    for sequence, line in enumerate(journal.records):
        raw = json.loads(line)
        insert(
            connection,
            "events",
            deployment_id=record.deployment_id,
            journal_id=journal_id,
            sequence=sequence,
            schema=schema,
            canonical=line.encode(),
            event_hash=raw["event_hash"],
            previous_hash=raw["previous_hash"],
            recorded_at=raw["event"]["recorded_at"],
            transition_id=raw["event"].get(
                "transition_id", raw["event"].get("operation_id")
            ),
        )
    insert(
        connection,
        "journal_heads",
        deployment_id=record.deployment_id,
        journal_id=journal_id,
        event_count=len(journal.records),
        head_sha=journal.head_sha,
        last_sequence=len(journal.records) - 1,
    )


def pending(
    db, operation="enroll-a", *, plan_sha=None, kind="plan_enrolled", payload=None
):
    connection, _, record = db
    intent = store.OperationIntent(
        kind,
        record.account_ref,
        plan_sha,
        store.canonical_bytes(payload or {"decision": "explicit"}),
    )
    insert(
        connection,
        "operations",
        deployment_id=record.deployment_id,
        operation_id=operation,
        account_ref=record.account_ref,
        plan_sha=plan_sha,
        kind=kind,
        intent_schema=store.INTENT_SCHEMA,
        intent_sha=intent.sha256,
        canonical_intent=intent.canonical,
        required_roles=store.canonical_bytes({"roles": ["account", "plan"]}),
        execution_preconditions=store.canonical_bytes({"expected_head": "0" * 64}),
        status="pending",
        created_at=UTC_TIME,
    )
    return intent


def committed(db):
    connection, _, record = db
    proposal = first()
    plan_sha = catalog_plan(db)
    assert plan_sha == proposal.plan.projection.scopes[0].plan_sha
    add_journal(db, proposal.account, "account-journal", "account")
    add_journal(db, proposal.plan, "plan-journal", "plan", plan_sha)
    connection.execute(
        "UPDATE accounts SET journal_id='account-journal',enrollment_revision=1 WHERE deployment_id=?",
        (record.deployment_id,),
    )
    connection.execute(
        "UPDATE plans SET journal_id='plan-journal',enrolled_revision=1,status='enrolled' WHERE plan_sha=?",
        (plan_sha,),
    )
    intent = pending(db, plan_sha=plan_sha)
    parts = []
    for ordinal, (role, journal_id, journal) in enumerate(
        (
            ("account", "account-journal", proposal.account),
            ("plan", "plan-journal", proposal.plan),
        )
    ):
        raw = json.loads(journal.records[-1])
        part = dict(
            role=role,
            ordinal=ordinal,
            journal_id=journal_id,
            sequence=raw["sequence"],
            event_hash=raw["event_hash"],
            result_head=raw["event_hash"],
        )
        insert(
            connection,
            "event_parts",
            deployment_id=record.deployment_id,
            operation_id="enroll-a",
            **part,
        )
        parts.append(part)
    receipt = store.canonical_bytes(
        dict(
            schema=store.RECEIPT_SCHEMA,
            deployment_id=record.deployment_id,
            operation_id="enroll-a",
            intent_sha=intent.sha256,
            committed_at=UTC_TIME,
            parts=parts,
        )
    )
    sha = store.digest_bytes(receipt)
    insert(
        connection,
        "receipts",
        deployment_id=record.deployment_id,
        operation_id="enroll-a",
        schema=store.RECEIPT_SCHEMA,
        receipt_sha=sha,
        canonical=receipt,
        committed_at=UTC_TIME,
    )
    connection.execute(
        "UPDATE operations SET status='committed',committed_at=?,receipt_sha=? WHERE operation_id='enroll-a'",
        (UTC_TIME, sha),
    )
    connection.commit()
    return intent, receipt


def test_schema_deployment_policy_and_registry_round_trip(db):
    connection, src, record = db
    assert store.validate_store_contents(connection) == store.STORE_SCHEMA
    assert (
        connection.execute("SELECT canonical FROM deployments").fetchone()[0]
        == record.canonical
    )
    raw = connection.execute("SELECT canonical FROM policies").fetchone()[0]
    assert store.StorePolicy.from_bytes(raw) == policy()
    assert connection.execute(
        "SELECT canonical FROM documents WHERE kind='preflight'"
    ).fetchone()[0] == store.canonical_bytes(src["preflight"].to_dict())
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_temporary_file_database_round_trip(tmp_path):
    connection = new_connection(tmp_path / "artificial.sqlite")
    bootstrap(connection)
    connection.close()
    reopened = new_connection(tmp_path / "artificial.sqlite")
    assert store.validate_store_contents(reopened) == store.STORE_SCHEMA
    reopened.close()


@pytest.mark.parametrize("value", [None, True, 0, -1, 1.5, "2", float("nan"), 2**63])
def test_explicit_plan_limit_invalid(value):
    with pytest.raises(HttpContractError):
        policy(value)


def test_policy_has_no_implicit_defaults():
    with pytest.raises(TypeError):
        store.StorePolicy()


@pytest.mark.parametrize(
    "field",
    ["max_record_bytes", "max_retained_body_bytes", "max_cumulative_acquired_bytes"],
)
def test_policy_capacities_invalid(field):
    with pytest.raises(HttpContractError):
        replace(policy(), **{field: 0})


def test_policy_record_limit_cannot_exceed_contract():
    with pytest.raises(HttpContractError, match="record_limit_invalid"):
        replace(policy(), max_record_bytes=32769)


@pytest.mark.parametrize(
    "schema", ["unknown", "historical-feasibility-account-rate-ledger-v2"]
)
def test_unknown_policy_schema(schema):
    raw = policy().to_dict() | {"schema": schema}
    with pytest.raises(HttpContractError, match="policy_schema_invalid"):
        store.StorePolicy.from_bytes(store.canonical_bytes(raw))


def test_plan_count_and_exact_record_size_boundaries():
    record = store.canonical_bytes(
        {"payload": "x" * (32768 - len(store.canonical_bytes({"payload": ""})))}
    )
    assert len(record) == 32768
    assert store.validate_enrollment_capacity(policy(), (), record) == 32768
    assert store.validate_enrollment_capacity(policy(), ("a" * 64,), record) == 32768
    with pytest.raises(HttpContractError, match="plan_count_exceeded"):
        store.validate_enrollment_capacity(policy(), ("a" * 64, "b" * 64), record)
    with pytest.raises(HttpContractError, match="record_oversize"):
        store.validate_enrollment_capacity(
            policy(), (), store.canonical_bytes({"payload": "x" * 32769})
        )


def test_record_size_measures_utf8_bytes():
    data = store.canonical_bytes({"payload": "株" * 11000})
    assert len(data.decode()) < 32768 < len(data)
    with pytest.raises(HttpContractError, match="record_oversize"):
        store.validate_enrollment_capacity(policy(), (), data)


def test_duplicate_plan_history_rejected():
    with pytest.raises(HttpContractError, match="duplicate_plan_history"):
        store.validate_enrollment_capacity(policy(), ("a" * 64, "a" * 64), b"{}")


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "sha-only",
        "unknown-schema",
        "malformed",
        "noncanonical",
        "duplicate-key",
    ],
)
def test_document_adversarial_cases(db, change):
    _, src, _ = db
    doc = document("plan", src["plan"].to_dict())
    changes = {
        "digest": {"sha256": "f" * 64},
        "sha-only": {"content": b""},
        "unknown-schema": {"schema": "unknown"},
        "malformed": {"content": b"{"},
        "noncanonical": {"content": b'{ "schema": "unknown" }'},
        "duplicate-key": {"content": b'{"schema":"a","schema":"b"}'},
    }[change]
    if change not in {"digest", "sha-only"}:
        changes["sha256"] = hashlib.sha256(
            changes.get("content", doc.content)
        ).hexdigest()
    with pytest.raises(HttpContractError):
        replace(doc, **changes)


def test_same_source_reference_is_not_document_identity(db):
    connection, _, _ = db
    a, b = sources("source-a"), sources("source-b")
    for src in (a, b):
        insert_document(
            connection, document("plan", src["plan"].to_dict(), "same-reference")
        )
    rows = connection.execute(
        "SELECT document_sha FROM documents WHERE kind='plan'"
    ).fetchall()
    assert len(rows) == 2 and rows[0] != rows[1]
    assert store.validate_store_contents(connection) == store.STORE_SCHEMA


def test_same_digest_cannot_replace_document(db):
    connection, src, _ = db
    doc = document("plan", src["plan"].to_dict())
    insert_document(connection, doc)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE documents SET canonical=? WHERE document_sha=?", (b"{}", doc.sha256)
        )
    with pytest.raises(sqlite3.IntegrityError):
        insert_document(connection, replace(doc, source_reference="another"))


@pytest.mark.parametrize("target", ["plan", "document", "part", "receipt"])
def test_foreign_key_orphans_rejected(db, target):
    connection, _, _ = db
    with pytest.raises(sqlite3.IntegrityError):
        if target == "document":
            insert(
                connection,
                "documents",
                deployment_id="absent",
                document_sha="a" * 64,
                kind="plan",
                schema=store.PLAN_SCHEMA,
                canonical=b"{}",
                source_identity="s",
                source_reference="r",
                registered_at=UTC_TIME,
            )
        elif target == "receipt":
            insert(
                connection,
                "receipts",
                deployment_id="deployment-a",
                operation_id="missing",
                schema=store.RECEIPT_SCHEMA,
                receipt_sha="a" * 64,
                canonical=b"{}",
                committed_at=UTC_TIME,
            )
        elif target == "part":
            insert(
                connection,
                "event_parts",
                deployment_id="deployment-a",
                operation_id="missing",
                role="account",
                ordinal=0,
                journal_id="missing",
                sequence=0,
                event_hash="a" * 64,
                result_head="a" * 64,
            )
        else:
            catalog_plan(db)
            connection.execute("UPDATE plans SET account_ref='missing'")


@pytest.mark.parametrize("change", ["intent", "plan", "kind", "replace"])
def test_operation_id_is_deployment_global(db, change):
    connection, _, _ = db
    p1, p2 = catalog_plan(db), catalog_plan(db, name="plan-b")
    pending(db, plan_sha=p1)
    with pytest.raises(sqlite3.IntegrityError):
        if change == "replace":
            connection.execute(
                "INSERT OR REPLACE INTO operations SELECT * FROM operations"
            )
        else:
            pending(
                db,
                plan_sha=p2 if change == "plan" else p1,
                kind="audit" if change == "kind" else "plan_enrolled",
                payload={"decision": "different"} if change == "intent" else None,
            )


def test_committed_receipt_and_event_bytes_round_trip(db):
    connection, _, record = db
    intent, receipt = committed(db)
    assert store.validate_store_contents(connection) == store.STORE_SCHEMA
    replay = store.assess_operation_replay(
        connection, record.deployment_id, "enroll-a", intent.canonical
    )
    assert replay.classification == "committed" and replay.receipt == receipt
    original = first()
    rows = connection.execute(
        "SELECT canonical FROM events WHERE journal_id='account-journal' ORDER BY sequence"
    ).fetchall()
    assert tuple(r[0] for r in rows) == tuple(
        line.encode() for line in original.account.records
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM events WHERE transition_id='enroll-a'"
        ).fetchone()[0]
        == 2
    )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE receipts SET canonical=x'7b7d'",
        "DELETE FROM receipts",
        "INSERT OR REPLACE INTO receipts SELECT * FROM receipts",
        "UPDATE operations SET receipt_sha='broken' WHERE status='committed'",
        "DELETE FROM operations WHERE status='committed'",
        "INSERT OR REPLACE INTO events SELECT * FROM events",
    ],
)
def test_committed_immutability(db, sql):
    connection, _, _ = db
    _, receipt = committed(db)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql)
    assert connection.execute("SELECT canonical FROM receipts").fetchone()[0] == receipt


def test_pending_and_later_pending_do_not_change_receipt(db):
    connection, _, record = db
    intent, receipt = committed(db)
    later = pending(db, operation="later")
    connection.commit()
    assert (
        store.assess_operation_replay(
            connection, record.deployment_id, "later", later.canonical
        ).classification
        == "pending_recovery_required"
    )
    assert (
        store.assess_operation_replay(
            connection, record.deployment_id, "enroll-a", intent.canonical
        ).receipt
        == receipt
    )
    with pytest.raises(HttpContractError, match="operation_intent_conflict"):
        store.assess_operation_replay(
            connection,
            record.deployment_id,
            "enroll-a",
            replace(intent, kind="different").canonical,
        )


def test_preconditions_do_not_define_semantic_intent(db):
    connection, _, record = db
    intent = pending(db)
    connection.execute(
        "UPDATE operations SET execution_preconditions=?",
        (
            store.canonical_bytes(
                {"expected_head": "a" * 64, "execution_timestamp": UTC_TIME}
            ),
        ),
    )
    assert (
        store.assess_operation_replay(
            connection, record.deployment_id, "enroll-a", intent.canonical
        ).classification
        == "pending_recovery_required"
    )


def test_committed_without_receipt_database_constraint(db):
    connection, _, _ = db
    pending(db)
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        with connection:
            connection.execute(
                "UPDATE operations SET status='committed',committed_at=?,receipt_sha=?",
                (UTC_TIME, "a" * 64),
            )
    assert (
        connection.execute("SELECT status FROM operations").fetchone()[0] == "pending"
    )


def corrupt(connection, trigger, sql, params=()):
    # Model out-of-band alteration. Recreate the original schema so the content
    # validator (not merely the DDL check) must detect the corrupted evidence.
    ddl = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name=?", (trigger,)
    ).fetchone()[0]
    connection.execute(f"DROP TRIGGER {trigger}")
    connection.execute(sql, params)
    connection.execute(ddl)


@pytest.mark.parametrize(
    "mode",
    [
        "receipt-digest",
        "event-sequence",
        "event-previous",
        "event-hash",
        "missing-part",
    ],
)
def test_corruption_is_detected_without_repair(db, mode):
    connection, _, _ = db
    committed(db)
    if mode == "receipt-digest":
        corrupt(
            connection,
            "receipts_no_update",
            "UPDATE receipts SET canonical=?",
            (b"{}",),
        )
    elif mode == "missing-part":
        corrupt(
            connection,
            "event_parts_no_delete",
            "DELETE FROM event_parts WHERE role='plan'",
        )
    else:
        field, value = {
            "event-sequence": ("canonical", b"{}"),
            "event-previous": ("previous_hash", "f" * 64),
            "event-hash": ("canonical", store.canonical_bytes({"schema": "unknown"})),
        }[mode]
        corrupt(
            connection,
            "events_no_update",
            f"UPDATE events SET {field}=? WHERE journal_id='account-journal' AND sequence=0",
            (value,),
        )
    snapshot = tuple(connection.iterdump())
    with pytest.raises(HttpContractError):
        store.validate_store_contents(connection)
    assert tuple(connection.iterdump()) == snapshot


def test_current_head_cannot_be_historical_prefix(db):
    connection, _, _ = db
    committed(db)
    raw = json.loads(first().account.records[0])
    connection.execute(
        "UPDATE journal_heads SET event_count=1,last_sequence=0,head_sha=? WHERE journal_id='account-journal'",
        (raw["event_hash"],),
    )
    with pytest.raises(HttpContractError, match="current_head_mismatch"):
        store.validate_store_contents(connection)


def test_rollback_leaves_no_event_head_or_receipt_fragments(db):
    connection, _, _ = db
    before = tuple(connection.iterdump())
    with pytest.raises(sqlite3.IntegrityError):
        with connection:
            # committed() intentionally commits for other tests; use a private
            # transaction fixture here to ensure constraint failure rolls back.
            plan_sha = catalog_plan(db)
            add_journal(db, first().plan, "plan-journal", "plan", plan_sha)
            intent = pending(db, plan_sha=plan_sha)
            insert(
                connection,
                "receipts",
                deployment_id="deployment-a",
                operation_id="enroll-a",
                schema=store.RECEIPT_SCHEMA,
                receipt_sha=intent.sha256,
                canonical=b"{}",
                committed_at=UTC_TIME,
            )
            pending(db, plan_sha=plan_sha)
    assert tuple(connection.iterdump()) == before


def test_bootstrap_failure_rolls_back_schema():
    connection = new_connection()
    with pytest.raises(HttpContractError, match="foreign_key_violation"):
        bootstrap(connection, documents=())
    assert not connection.execute("SELECT name FROM sqlite_master").fetchall()
    connection.close()


@pytest.mark.parametrize(
    "mode",
    [
        "foreign-keys",
        "recursive-triggers",
        "unknown",
        "missing",
        "legacy",
        "removed-trigger",
    ],
)
def test_schema_enforcement_fail_closed(db, mode):
    connection, _, _ = db
    connection.commit()
    if mode == "foreign-keys":
        connection.execute("PRAGMA foreign_keys=OFF")
    elif mode == "recursive-triggers":
        connection.execute("PRAGMA recursive_triggers=OFF")
    elif mode == "unknown":
        corrupt(
            connection,
            "store_metadata_no_update",
            "UPDATE store_metadata SET schema='unknown'",
        )
    elif mode == "missing":
        corrupt(connection, "store_metadata_no_delete", "DELETE FROM store_metadata")
    elif mode == "removed-trigger":
        connection.execute("DROP TRIGGER receipts_no_update")
    else:
        connection.close()
        connection = new_connection()
        connection.execute("CREATE TABLE legacy_artificial (schema TEXT)")
    with pytest.raises(HttpContractError):
        store.validate_store_schema(connection)
    if mode == "legacy":
        connection.close()


def test_no_existing_database_or_silent_migration(db):
    connection, _, _ = db
    with pytest.raises(HttpContractError, match="existing_database"):
        bootstrap(connection)


def test_historical_policy_identity_retained(db):
    connection, _, _ = db
    catalog_plan(db)
    newer = policy(3)
    insert(
        connection,
        "policies",
        policy_sha=newer.sha256,
        schema=store.POLICY_SCHEMA,
        canonical=newer.canonical,
        max_plans=3,
        max_record=32768,
        retained_bytes=100000,
        acquired_bytes=200000,
    )
    assert (
        connection.execute("SELECT policy_sha FROM plans").fetchone()[0]
        == policy().sha256
    )
    original = connection.execute(
        "SELECT canonical FROM policies WHERE policy_sha=?", (policy().sha256,)
    ).fetchone()[0]
    assert store.StorePolicy.from_bytes(original).max_enrolled_plans_per_account == 2
    assert store.validate_store_contents(connection) == store.STORE_SCHEMA


@pytest.mark.parametrize(
    "value", ["Deployment", "部署", "a\u202e", "a b", "a' OR 1=1", "A", "é"]
)
def test_identity_is_exact_ascii_label(db, value):
    _, _, record = db
    with pytest.raises(HttpContractError):
        replace(record, deployment_id=value)


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01",
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00.000000+09:00",
        "2026-02-30T00:00:00.000000+00:00",
    ],
)
def test_timestamp_requires_canonical_utc(db, value):
    _, _, record = db
    with pytest.raises(HttpContractError, match="utc_timestamp_invalid"):
        replace(record, created_at=value)


def test_no_operational_surface_or_live_permission():
    for name in (
        "open",
        "activate_plan",
        "enroll_plan",
        "reserve",
        "send",
        "commit_body",
        "recover",
    ):
        assert not hasattr(store, name)
    assert not hasattr(store.DeploymentRecord, "permitted")


def test_historical_and_prepared_catalog_capacity(db):
    connection, _, _ = db
    committed(db)
    connection.execute("UPDATE plans SET status='stopped'")
    # A stopped enrollment still consumes a historical slot; prepared B does not.
    catalog_plan(db, name="plan-b")
    assert store.validate_store_contents(connection) == store.STORE_SCHEMA
    snapshot = connection.execute(
        "SELECT plan_sha FROM plans WHERE enrolled_revision IS NOT NULL"
    ).fetchall()
    assert len(snapshot) == 1
    assert (
        store.validate_enrollment_capacity(
            policy(), tuple(r[0] for r in snapshot), b"{}"
        )
        == 2
    )


def test_catalog_all_enrolled_plans_count_not_only_current_status(db):
    connection, _, record = db
    a = first()
    b = enroll(a.account, (a.plan,), name="plan-b", operation="enroll-b")
    p1, p2 = catalog_plan(db), catalog_plan(db, name="plan-b")
    add_journal(db, b.account, "account-journal", "account")
    add_journal(db, a.plan, "plan-a-journal", "plan", p1)
    add_journal(db, b.plan, "plan-b-journal", "plan", p2)
    connection.execute(
        "UPDATE accounts SET journal_id='account-journal',enrollment_revision=2 WHERE deployment_id=?",
        (record.deployment_id,),
    )
    for sha, name, revision, state in (
        (p1, "plan-a-journal", 1, "stopped"),
        (p2, "plan-b-journal", 2, "enrolled"),
    ):
        connection.execute(
            "UPDATE plans SET journal_id=?,enrolled_revision=?,status=? WHERE plan_sha=?",
            (name, revision, state, sha),
        )
    assert store.validate_store_contents(connection) == store.STORE_SCHEMA
    with pytest.raises(HttpContractError, match="plan_count_exceeded"):
        store.validate_enrollment_capacity(policy(), (p1, p2), b"{}")
    # The DB validator also checks capacity, not just the candidate helper.
    smaller = policy(1)
    insert(
        connection,
        "policies",
        policy_sha=smaller.sha256,
        schema=store.POLICY_SCHEMA,
        canonical=smaller.canonical,
        max_plans=1,
        max_record=32768,
        retained_bytes=100000,
        acquired_bytes=200000,
    )
    changed = replace(record, store_policy_sha=smaller.sha256)
    corrupt(
        connection,
        "deployments_no_update",
        "UPDATE deployments SET policy_sha=?,canonical=?",
        (smaller.sha256, changed.canonical),
    )
    connection.execute("UPDATE accounts SET policy_sha=?", (smaller.sha256,))
    with pytest.raises(HttpContractError, match="plan_count_exceeded"):
        store.validate_store_contents(connection)


def test_source_result_head_references_separate_from_intent(db):
    connection, _, _ = db
    intent, receipt = committed(db)
    initial = json.loads(first().account.records[0])
    latest = json.loads(first().account.records[-1])
    for phase, raw in (("source", initial), ("result", latest)):
        insert(
            connection,
            "operation_head_references",
            deployment_id="deployment-a",
            operation_id="enroll-a",
            phase=phase,
            journal_id="account-journal",
            sequence=raw["sequence"],
            head_sha=raw["event_hash"],
        )
    assert store.validate_store_contents(connection) == store.STORE_SCHEMA
    assert (
        connection.execute("SELECT canonical_intent FROM operations").fetchone()[0]
        == intent.canonical
    )
    assert connection.execute("SELECT canonical FROM receipts").fetchone()[0] == receipt


@pytest.mark.parametrize(
    "table,sql",
    [
        (
            "operation_head_references",
            "INSERT INTO operation_head_references VALUES ('deployment-a','enroll-a','source','missing',NULL,'"
            + "0" * 64
            + "')",
        ),
        (
            "event_parts",
            "INSERT INTO event_parts SELECT deployment_id,operation_id,'new-role',ordinal,journal_id,sequence,event_hash,result_head FROM event_parts LIMIT 1",
        ),
        (
            "journal_heads",
            "UPDATE journal_heads SET last_sequence=NULL WHERE event_count>0",
        ),
    ],
)
def test_identity_constraints_for_references_parts_and_heads(db, table, sql):
    connection, _, _ = db
    committed(db)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sequence", False),
        ("sequence", 2),
        ("previous_hash", "f" * 64),
        ("event_hash", "f" * 64),
    ],
)
def test_event_envelope_corruption_detected(db, field, value):
    connection, _, _ = db
    committed(db)
    raw = json.loads(first().account.records[0])
    raw[field] = value
    corrupt(
        connection,
        "events_no_update",
        "UPDATE events SET canonical=? WHERE journal_id='account-journal' AND sequence=0",
        (store.canonical_bytes(raw),),
    )
    with pytest.raises(HttpContractError):
        store.validate_store_contents(connection)


def test_missing_committed_receipt_detected_after_out_of_band_deletion(db):
    connection, _, _ = db
    committed(db)
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    corrupt(connection, "receipts_no_delete", "DELETE FROM receipts")
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(HttpContractError, match="foreign_key_violation"):
        store.validate_store_contents(connection)


@pytest.mark.parametrize(
    "field,value", [("request_generation", 1), ("enrollment_revision", 2)]
)
def test_account_catalog_cannot_override_original_projection(db, field, value):
    connection, _, _ = db
    committed(db)
    connection.execute(f"UPDATE accounts SET {field}=?", (value,))
    with pytest.raises(HttpContractError):
        store.validate_store_contents(connection)


def test_global_operation_namespace_spans_journals(db):
    connection, _, _ = db
    committed(db)
    assert (
        connection.execute(
            "SELECT count(*) FROM event_parts WHERE operation_id='enroll-a'"
        ).fetchone()[0]
        == 2
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE operations SET kind='different'")


def test_unknown_control_event_schema_is_not_escape_hatch(db):
    connection, _, record = db
    insert(
        connection,
        "journals",
        deployment_id=record.deployment_id,
        journal_id="control",
        account_ref=record.account_ref,
        kind="control",
        schema="future-physical-contradiction",
        status="stopped",
    )
    insert(
        connection,
        "journal_heads",
        deployment_id=record.deployment_id,
        journal_id="control",
        event_count=0,
        head_sha="0" * 64,
    )
    with pytest.raises(HttpContractError, match="journal_schema_unsupported"):
        store.validate_store_contents(connection)


@pytest.mark.parametrize(
    "value",
    [b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e400}', b'{"x":' + b"9" * 5000 + b"}"],
)
def test_invalid_json_is_reasoned_failure(value):
    with pytest.raises(HttpContractError):
        store.OperationIntent("audit", "account", None, value)


@pytest.mark.parametrize("unknown_kind", [False, True])
def test_body_original_envelope_round_trip_and_unknown_kind_rejection(db, unknown_kind):
    from test_feasibility_live_body_enrollment_compatibility import body

    from feasibility.live_body_contract import BodyEvent

    connection, _, _ = db
    src = sources()
    plan_sha = catalog_plan(db)
    original = body(src)
    journal = original.append(
        BodyEvent(
            "audit-original",
            "audit",
            original.root.observed_at,
            None,
            '{"evidence_ref":"offline-only"}',
            None,
        )
    )
    add_journal(db, journal, "body-journal", "body", plan_sha)
    if unknown_kind:
        raw = json.loads(journal.records[0])
        raw["event"]["kind"] = "physical-contradiction-unknown"
        raw["event_hash"] = store.digest_bytes(
            store.canonical_bytes({k: v for k, v in raw.items() if k != "event_hash"})
        )
        # Update hash and bytes as an out-of-band alteration, with the same
        # schema and a valid envelope hash: kind validation must still reject it.
        connection.execute("DELETE FROM journal_heads WHERE journal_id='body-journal'")
        corrupt(
            connection,
            "events_no_update",
            "UPDATE events SET canonical=?,event_hash=? WHERE journal_id='body-journal'",
            (store.canonical_bytes(raw), raw["event_hash"]),
        )
        insert(
            connection,
            "journal_heads",
            deployment_id="deployment-a",
            journal_id="body-journal",
            event_count=1,
            last_sequence=0,
            head_sha=raw["event_hash"],
        )
        with pytest.raises(HttpContractError, match="body_event_kind_unsupported"):
            store.validate_store_contents(connection)
    else:
        assert store.validate_store_contents(connection) == store.STORE_SCHEMA
        assert (
            connection.execute(
                "SELECT canonical FROM events WHERE journal_id='body-journal'"
            ).fetchone()[0]
            == journal.records[0].encode()
        )


@pytest.mark.parametrize("pragma", ["foreign_keys", "recursive_triggers"])
def test_initializer_rejects_unenforced_connection_before_writing(pragma):
    connection = new_connection()
    connection.execute(f"PRAGMA {pragma}=OFF")
    with pytest.raises(HttpContractError):
        bootstrap(connection)
    assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    connection.close()
