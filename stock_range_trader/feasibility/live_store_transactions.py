"""I1a-3 local authority. No exported business handler, activation or sender.

Read APIs return immutable values, never a connection/cursor. Private typed
handlers are deliberately absent until their own implementation/review stage.
Tests install a bounded artificial handler, not an arbitrary SQL callback.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from . import live_store as st
from . import live_store_operational as op
from . import live_store_runtime as rt
from .http_contract import HttpContractError
from .live_http_evidence import LiveJournal, reconcile_live_journals


def _check(ok, reason):
    if not ok:
        raise HttpContractError("store_transaction_" + reason)


def _stage(name):
    """Private failure-injection seam; never a user callback."""


def _rows(c, table, where="", values=()):
    # All table/where strings below are internal constants, never public inputs.
    cursor = c.execute("SELECT * FROM " + table + where, values)
    names = tuple(column[0] for column in cursor.description)
    return tuple(dict(zip(names, row, strict=True)) for row in cursor)


def _insert(c, table, **row):
    _check(c.in_transaction, "transaction_required")
    c.execute(
        "INSERT INTO "
        + table
        + " ("
        + ",".join(row)
        + ") VALUES ("
        + ",".join("?" for _ in row)
        + ")",
        tuple(row.values()),
    )


@dataclass(frozen=True)
class OperationAssessment:
    classification: str
    receipt: bytes | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CatalogSnapshot:
    """Canonical DB-derived snapshot; no caller-supplied capacity history."""

    canonical: bytes

    @property
    def sha256(self):
        return st.digest_bytes(self.canonical)


@dataclass(frozen=True)
class _Document:
    kind: str
    content: bytes
    source_identity: str
    source_reference: str


@dataclass(frozen=True)
class _Journal:
    journal_id: str
    kind: str
    plan_sha: str | None
    value: LiveJournal


@dataclass(frozen=True)
class _Proposal:
    documents: tuple[_Document, ...]
    journals: tuple[_Journal, ...]


@dataclass(frozen=True)
class _Rules:
    # Role order and journal kind are fixed by the private command kind.
    roles: tuple[tuple[str, str], ...]


def _rules_for(kind):
    raise HttpContractError("store_transaction_business_handler_not_installed")


def _prepare(intent, operation_id, catalog, journals, documents, observed):
    """Typed dispatch seam. I1b/I2 must install reviewed internal handlers.

    This is not exported and accepts no callable, SQL or connection. Inputs
    are immutable DB originals. No production business command exists yet.
    """
    raise HttpContractError("store_transaction_business_handler_not_installed")


def _catalog(c):
    return CatalogSnapshot(
        st.canonical_bytes(
            {
                "accounts": sorted(
                    _rows(c, "accounts"), key=lambda r: r["account_ref"]
                ),
                "plans": sorted(_rows(c, "plans"), key=lambda r: r["plan_sha"]),
            }
        )
    )


def _document(c, sha):
    st._sha(sha)
    rows = _rows(c, "documents", " WHERE document_sha=?", (sha,))
    _check(len(rows) == 1, "document_missing")
    r = rows[0]
    return st.CanonicalDocument(
        r["kind"],
        r["schema"],
        r["canonical"],
        r["document_sha"],
        r["source_identity"],
        r["source_reference"],
        r["registered_at"],
    )


def _register_document(c, deployment, document, observed):
    _check(type(document) is _Document, "document_command_required")
    schema = st._DOCUMENT_SCHEMAS.get(document.kind)
    _check(schema is not None, "document_kind_invalid")
    candidate = st.CanonicalDocument(
        document.kind,
        schema,
        document.content,
        st.digest_bytes(document.content),
        document.source_identity,
        document.source_reference,
        observed,
    )
    existing = _rows(c, "documents", " WHERE document_sha=?", (candidate.sha256,))
    if existing:
        original = _document(c, candidate.sha256)
        _check(
            (
                original.kind,
                original.schema,
                original.content,
                original.source_identity,
                original.source_reference,
            )
            == (
                candidate.kind,
                candidate.schema,
                candidate.content,
                candidate.source_identity,
                candidate.source_reference,
            ),
            "document_source_conflict",
        )
        return original
    _insert(
        c,
        "documents",
        deployment_id=deployment,
        document_sha=candidate.sha256,
        kind=candidate.kind,
        schema=candidate.schema,
        canonical=candidate.content,
        source_identity=candidate.source_identity,
        source_reference=candidate.source_reference,
        registered_at=observed,
    )
    return candidate


def _read_session(session, getter):
    # Private, fixed read call sites only. Not a public transaction callback.
    _check(type(session) is op.StoreSession, "session_required")
    op._require(os.getpid() == session._resources.pid, "process_mismatch")
    op._require(threading.get_ident() == session._resources.thread, "thread_mismatch")
    try:
        c, _, _, _ = session._begin()
        value = getter(c)
        session._resources.guard()
        c.rollback()
        return value
    except BaseException:
        session._invalidate()
        raise


def read_catalog(session):
    return _read_session(session, _catalog)


def read_document(session, document_sha):
    return _read_session(session, lambda c: _document(c, document_sha))


def _core(c, authority):
    op._integrity(c)
    rt.validate_store_v2_schema(c)
    st._validate_core_contents(
        c,
        deployment_schema=rt.DEPLOYMENT_SCHEMA_V2,
        store_schema=rt.STORE_SCHEMA_V2,
        initial_lifecycle="bootstrap_pending",
        document_shas={authority.preflight.sha256, authority.preflight.registry.sha256},
    )
    projection = rt._validate_runtime_contents(c)
    deployment = _rows(c, "deployments")[0]
    owners = tuple(
        rt.OwnerDeploymentRecord.from_bytes(r["canonical"])
        for r in _rows(c, "owner_deployment_records")
    )
    _check(
        authority.intent in owners and authority.pin in owners, "owner_binding_mismatch"
    )
    _check(
        deployment["store_id"] == authority.pin.store_uuid
        and deployment["policy_sha"] == authority.policy.sha256
        and deployment["preflight_sha"] == authority.preflight.sha256
        and projection.owner_pin_sha == authority.pin.sha256,
        "core_binding_mismatch",
    )
    policy = _rows(c, "runtime_policies")[0]
    _check(
        policy["canonical"] == authority.runtime_policy.canonical,
        "runtime_policy_mismatch",
    )
    return projection


def _prefix(c, journal_id, sequence):
    rows = _rows(c, "journals", " WHERE journal_id=?", (journal_id,))
    _check(len(rows) == 1, "journal_missing")
    journal = rows[0]
    _check(journal["kind"] in {"account", "plan"}, "receipt_journal_unsupported")
    rows = _rows(
        c,
        "events",
        " WHERE journal_id=? AND sequence<=? ORDER BY sequence",
        (journal_id, sequence),
    )
    _check(len(rows) == sequence + 1, "historical_prefix_incomplete")
    previous = st.ZERO_HASH
    for i, r in enumerate(rows):
        raw = st._load(r["canonical"])
        event = raw.get("event", {})
        _check(
            r["sequence"] == raw.get("sequence") == i
            and r["previous_hash"] == raw.get("previous_hash") == previous
            and r["event_hash"] == raw.get("event_hash")
            and r["schema"] == raw.get("schema") == journal["schema"]
            and r["recorded_at"] == event.get("recorded_at")
            and r["transition_id"] == event.get("transition_id"),
            "historical_event_mismatch",
        )
        previous = r["event_hash"]
    value = LiveJournal(journal["schema"], tuple(r["canonical"].decode() for r in rows))
    return journal, rows, value


def _receipt(c, row, projection):
    """Verify original dependencies, not unrelated latest catalog/head state."""
    intent = st.OperationIntent.from_bytes(row["canonical_intent"], row["intent_sha"])
    _check(
        row["intent_schema"] == st.INTENT_SCHEMA
        and (row["kind"], row["account_ref"], row["plan_sha"])
        == (intent.kind, intent.account_ref, intent.plan_sha),
        "intent_binding",
    )
    roles = _rules_for(intent.kind).roles
    _check(
        st._roles(row["required_roles"]) == tuple(role for role, _ in roles),
        "required_roles_mismatch",
    )
    pre = st._load(row["execution_preconditions"])
    _check(
        set(pre)
        == {
            "schema",
            "source_heads",
            "dependency_heads",
            "catalog_sha",
            "session_id",
            "fence_epoch",
            "runtime_head",
            "started_at",
            "documents",
        }
        and pre["schema"] == "store-transaction-preconditions-v1",
        "preconditions_invalid",
    )
    st._sha(pre["catalog_sha"])
    _check(
        pre["session_id"] == row["session_ref"]
        and pre["fence_epoch"] == row["fence_ref"]
        and pre["started_at"] == st._time(row["created_at"]),
        "session_binding",
    )
    sessions = [s for s in projection.sessions if s.session_id == row["session_ref"]]
    _check(
        len(sessions) == 1 and sessions[0].fence_epoch == row["fence_ref"],
        "session_evidence_missing",
    )
    runtime = _rows(c, "runtime_events", " WHERE event_hash=?", (pre["runtime_head"],))
    _check(len(runtime) == 1, "runtime_source_missing")
    start = rt.RuntimeEvent.from_bytes(runtime[0]["canonical"])
    _check(
        start.kind == "clock_observed"
        and start.session_id == row["session_ref"]
        and start.fence_epoch == row["fence_ref"]
        and start.observation.utc == row["created_at"],
        "runtime_source_mismatch",
    )
    for sha in pre["documents"]:
        _document(c, sha)
    d = _rows(c, "deployments")[0]
    _document(c, d["preflight_sha"])
    _document(c, d["owner_registry_sha"])
    if intent.plan_sha is not None:
        _check(intent.plan_sha in pre["documents"], "plan_document_missing")
    key = (row["deployment_id"], row["operation_id"])
    parts = _rows(
        c,
        "event_parts",
        " WHERE deployment_id=? AND operation_id=? ORDER BY ordinal",
        key,
    )
    _check(
        tuple(p["role"] for p in parts) == tuple(role for role, _ in roles)
        and tuple(p["ordinal"] for p in parts) == tuple(range(len(roles)))
        and bool(parts),
        "required_parts_missing",
    )
    refs = _rows(
        c, "operation_head_references", " WHERE deployment_id=? AND operation_id=?", key
    )
    journal_ids = {p["journal_id"] for p in parts}
    _check(
        {(r["phase"], r["journal_id"]) for r in refs}
        == {(phase, j) for phase in ("source", "result") for j in journal_ids},
        "head_references_incomplete",
    )
    source = {
        r["journal_id"]: {"sequence": r["sequence"], "head_sha": r["head_sha"]}
        for r in refs
        if r["phase"] == "source"
    }
    _check(source == pre["source_heads"], "source_heads_mismatch")
    dependencies = pre["dependency_heads"]
    _check(type(dependencies) is dict, "dependency_heads_invalid")
    for journal_id, reference in source.items():
        if reference["sequence"] is not None:
            _check(
                dependencies.get(journal_id) == reference, "dependency_source_mismatch"
            )
        else:
            _check(journal_id not in dependencies, "dependency_source_mismatch")
    historical = dict(dependencies)
    historical.update(
        {
            r["journal_id"]: {"sequence": r["sequence"], "head_sha": r["head_sha"]}
            for r in refs
            if r["phase"] == "result"
        }
    )
    historical_journals = []
    for journal_id, reference in historical.items():
        _check(
            type(reference) is dict
            and set(reference) == {"sequence", "head_sha"}
            and type(reference["sequence"]) is int
            and reference["sequence"] >= 0,
            "dependency_reference_invalid",
        )
        meta, _, value = _prefix(c, journal_id, reference["sequence"])
        _check(
            value.head_sha == reference["head_sha"]
            and meta["account_ref"] == intent.account_ref,
            "dependency_head_mismatch",
        )
        historical_journals.append((meta["kind"], value))
        for scope in value.projection.scopes:
            for sha in (scope.plan_sha, scope.preflight_sha, scope.approval_sha):
                _document(c, sha)
    accounts = [j for kind, j in historical_journals if kind == "account"]
    _check(len(accounts) == 1, "historical_account_incomplete")
    _check(
        reconcile_live_journals(
            accounts[0], tuple(j for kind, j in historical_journals if kind == "plan")
        ).classification
        == "consistent",
        "historical_reconciliation_failed",
    )
    for journal_id in journal_ids:
        selected = [p for p in parts if p["journal_id"] == journal_id]
        result = next(
            r for r in refs if r["journal_id"] == journal_id and r["phase"] == "result"
        )
        first = source[journal_id]["sequence"]
        _check(
            [p["sequence"] for p in selected]
            == list(range(0 if first is None else first + 1, result["sequence"] + 1)),
            "operation_sequence_gap",
        )
        journal, events, value = _prefix(c, journal_id, result["sequence"])
        _check(value.head_sha == result["head_sha"], "result_head_mismatch")
        _check(
            source[journal_id]["head_sha"]
            == (st.ZERO_HASH if first is None else events[first]["event_hash"]),
            "source_head_mismatch",
        )
        _check(
            journal["account_ref"] == intent.account_ref
            and journal["plan_sha"] in {None, intent.plan_sha},
            "part_scope_mismatch",
        )
        for p in selected:
            e = events[p["sequence"]]
            _check(
                journal["kind"] == roles[p["ordinal"]][1]
                and e["transition_id"] == row["operation_id"]
                and e["event_hash"] == p["event_hash"] == p["result_head"]
                and e["session_ref"] == row["session_ref"]
                and e["fence_ref"] == row["fence_ref"]
                and row["created_at"] <= e["recorded_at"] <= row["committed_at"],
                "event_part_mismatch",
            )
        for scope in value.projection.scopes:
            for sha in (scope.plan_sha, scope.preflight_sha, scope.approval_sha):
                _document(c, sha)
    receipts = _rows(c, "receipts", " WHERE deployment_id=? AND operation_id=?", key)
    _check(len(receipts) == 1, "receipt_missing")
    receipt = receipts[0]
    content = st._load(receipt["canonical"], receipt["receipt_sha"])
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
        for p in parts
    ]
    _check(
        content
        == {
            "schema": st.RECEIPT_SCHEMA,
            "deployment_id": key[0],
            "operation_id": key[1],
            "intent_sha": intent.sha256,
            "committed_at": row["committed_at"],
            "parts": expected_parts,
        }
        and receipt["schema"] == st.RECEIPT_SCHEMA
        and receipt["receipt_sha"] == row["receipt_sha"]
        and receipt["committed_at"] == st._time(row["committed_at"])
        and row["created_at"] <= row["committed_at"],
        "receipt_mismatch",
    )
    endings = [
        rt.RuntimeEvent.from_bytes(r["canonical"]) for r in _rows(c, "runtime_events")
    ]
    _check(
        any(
            e.kind == "clock_observed"
            and e.session_id == row["session_ref"]
            and e.fence_epoch == row["fence_ref"]
            and e.sequence == start.sequence + 1
            and e.previous_hash == start.event_hash
            and e.observation.utc == row["committed_at"]
            for e in endings
        ),
        "end_clock_evidence_missing",
    )
    return receipt["canonical"]


def _assessment(c, operation_id, intent, projection):
    st._label(operation_id)
    _check(type(intent) is st.OperationIntent, "intent_required")
    st.OperationIntent.from_bytes(intent.canonical)
    rows = _rows(c, "operations", " WHERE operation_id=?", (operation_id,))
    if not rows:
        return OperationAssessment("absent")
    row = rows[0]
    try:
        original = st.OperationIntent.from_bytes(
            row["canonical_intent"], row["intent_sha"]
        )
        _check(
            row["intent_schema"] == st.INTENT_SCHEMA
            and (row["kind"], row["account_ref"], row["plan_sha"])
            == (original.kind, original.account_ref, original.plan_sha),
            "intent_binding",
        )
        st._roles(row["required_roles"])
        st._load(row["execution_preconditions"])
        st._time(row["created_at"])
        if (row["canonical_intent"], row["intent_sha"], row["kind"]) != (
            intent.canonical,
            intent.sha256,
            intent.kind,
        ):
            return OperationAssessment("conflict")
        if row["status"] == "pending":
            return OperationAssessment("pending_recovery_required")
        return OperationAssessment("committed_receipt", _receipt(c, row, projection))
    except (HttpContractError, KeyError, TypeError, ValueError, IndexError) as exc:
        return OperationAssessment("inconsistent", reason=str(exc))


def read_committed_receipt(authority, operation_id, intent):
    """Locked evidence-only read, including dirty/stopped Stores. Never starts a session."""
    a = op._authority(authority, pin_required=True)
    resources = op._Resources(a).acquire()
    c = resources.connection
    try:
        c.execute("BEGIN IMMEDIATE")
        projection = _core(c, a)
        result = _assessment(c, operation_id, intent, projection)
        resources.guard()
        op._check_pragmas(c)
        c.rollback()
        return result
    finally:
        resources.release()


def assess_operation(session, operation_id, intent):
    return _read_session(
        session,
        lambda c: _assessment(
            c, operation_id, intent, rt._validate_runtime_contents(c)
        ),
    )


def _capacity(c, plan_sha, record):
    _check(c.in_transaction, "transaction_required")
    st._sha(plan_sha)
    d = _rows(c, "deployments")[0]
    policy = st.StorePolicy.from_bytes(
        _rows(c, "policies", " WHERE policy_sha=?", (d["policy_sha"],))[0]["canonical"]
    )
    # Always validate the entire catalog before deriving historical occupancy.
    rt.validate_store_v2_contents(c)
    historical = tuple(
        r[0]
        for r in c.execute(
            "SELECT plan_sha FROM plans WHERE account_ref=? AND enrolled_revision IS NOT NULL ORDER BY plan_sha",
            (d["account_ref"],),
        )
    )
    st._load(record)
    _check(len(record) <= policy.max_record_bytes, "enrollment_record_oversize")
    if plan_sha not in historical:
        st.validate_enrollment_capacity(policy, historical, record)
    return len(historical)


def _journal_values(c):
    return tuple(
        _Journal(
            r["journal_id"],
            r["kind"],
            r["plan_sha"],
            _prefix(c, r["journal_id"], h["last_sequence"])[2],
        )
        for r in _rows(c, "journals")
        for h in _rows(c, "journal_heads", " WHERE journal_id=?", (r["journal_id"],))
        if h["event_count"]
    )


def _guard_commit(session, c, expected_head):
    session._resources.guard()
    op._integrity(c)
    p = rt.validate_store_v2_contents(c)
    _check(
        session.status == "running"
        and p.current_session_id == session.identity.session_id
        and p.fence_epoch == session.identity.fence_epoch
        and p.head_sha == expected_head
        and p.session_state == "running"
        and not p.persistent_stop,
        "final_session_guard",
    )


def _clock(session, c, context, records, observation):
    result = rt.record_clock_observation(context, records, observation, str(uuid4()))
    return result, rt.replay_runtime(context, result)


def _write_proposal(
    c, session, intent, operation_id, proposal, rules, observed, runtime_head
):
    """Persist typed, fully replayable C2 journals; no arbitrary SQL/event handler."""
    _check(type(proposal) is _Proposal and bool(proposal.journals), "proposal_required")
    d = session.identity.deployment_id
    docs = tuple(_register_document(c, d, doc, observed) for doc in proposal.documents)
    for sha in (intent.plan_sha,):
        if sha is not None:
            _document(c, sha)
    catalog_before = _catalog(c)
    source = {}
    additions = []
    for j in proposal.journals:
        _check(
            type(j) is _Journal
            and type(j.value) is LiveJournal
            and j.kind in {"account", "plan"},
            "typed_journal_required",
        )
        st._label(j.journal_id)
        _check(j.journal_id not in source, "duplicate_journal")
        rows = _rows(c, "journals", " WHERE journal_id=?", (j.journal_id,))
        old_records = ()
        if rows:
            r = rows[0]
            _check(
                (r["kind"], r["plan_sha"], r["schema"], r["account_ref"])
                == (j.kind, j.plan_sha, j.value.schema, intent.account_ref),
                "journal_binding",
            )
            h = _rows(c, "journal_heads", " WHERE journal_id=?", (j.journal_id,))[0]
            old_records = tuple(
                r[0].decode()
                for r in c.execute(
                    "SELECT canonical FROM events WHERE journal_id=? ORDER BY sequence",
                    (j.journal_id,),
                )
            )
            source[j.journal_id] = {
                "sequence": h["last_sequence"],
                "head_sha": h["head_sha"],
            }
        else:
            source[j.journal_id] = {"sequence": None, "head_sha": st.ZERO_HASH}
        _check(
            j.value.records[: len(old_records)] == old_records, "journal_prefix_changed"
        )
        new = j.value.records[len(old_records) :]
        _check(bool(new), "empty_journal_mutation")
        for data in new:
            raw = st._load(data.encode())
            _check(
                raw["event"]["transition_id"] == operation_id
                and raw["event"]["recorded_at"] == observed,
                "event_command_binding",
            )
            additions.append((j, raw, data.encode()))
    _check(
        tuple(j.kind for j, _, _ in additions)
        == tuple(kind for _, kind in rules.roles),
        "required_roles_mismatch",
    )
    _check(intent.account_ref == session.identity.account_ref, "account_mismatch")
    pre = {
        "schema": "store-transaction-preconditions-v1",
        "source_heads": source,
        "dependency_heads": {
            r["journal_id"]: {"sequence": r["last_sequence"], "head_sha": r["head_sha"]}
            for r in _rows(c, "journal_heads")
            if r["event_count"]
        },
        "catalog_sha": catalog_before.sha256,
        "session_id": session.identity.session_id,
        "fence_epoch": session.identity.fence_epoch,
        "runtime_head": runtime_head,
        "started_at": observed,
        "documents": sorted(
            {x.sha256 for x in docs} | ({intent.plan_sha} if intent.plan_sha else set())
        ),
    }
    # Capacity precedes catalog updates, but follows document registration in the same savepoint.
    account = next((j for j in proposal.journals if j.kind == "account"), None)
    if account is not None:
        projection = account.value.projection
        for enrollment in getattr(projection, "enrollments", ()):
            if enrollment.transition_id == operation_id:
                event = next(
                    data
                    for j, raw, data in additions
                    if j.kind == "account" and raw["event"]["kind"] == "plan_enrolled"
                )
                _capacity(c, enrollment.scope.plan_sha, event)
    if not _rows(c, "accounts"):
        _check(account is not None, "account_required")
        _insert(
            c,
            "accounts",
            deployment_id=d,
            account_ref=intent.account_ref,
            schema=account.value.schema,
            policy_sha=session._resources.authority.policy.sha256,
            journal_id=None,
            enrollment_revision=0,
            request_generation=0,
            status="prepared",
        )
    for j in proposal.journals:
        if j.kind == "plan" and not _rows(
            c, "plans", " WHERE plan_sha=?", (j.plan_sha,)
        ):
            scope = j.value.projection.scopes[0]
            _check(scope.plan_sha == intent.plan_sha == j.plan_sha, "plan_mismatch")
            _insert(
                c,
                "plans",
                deployment_id=d,
                account_ref=intent.account_ref,
                plan_sha=j.plan_sha,
                preflight_sha=scope.preflight_sha,
                approval_sha=scope.approval_sha,
                policy_sha=session._resources.authority.policy.sha256,
                plan_kind="plan",
                plan_document_schema=st._DOCUMENT_SCHEMAS["plan"],
                preflight_kind="preflight",
                preflight_document_schema=st._DOCUMENT_SCHEMAS["preflight"],
                approval_kind="approval",
                approval_document_schema=st._DOCUMENT_SCHEMAS["approval"],
                schema=j.value.schema,
                journal_id=None,
                status="prepared",
                enrolled_revision=None,
            )
        if not _rows(c, "journals", " WHERE journal_id=?", (j.journal_id,)):
            _insert(
                c,
                "journals",
                deployment_id=d,
                journal_id=j.journal_id,
                account_ref=intent.account_ref,
                plan_sha=j.plan_sha,
                kind=j.kind,
                schema=j.value.schema,
                status="prepared",
            )
            _insert(
                c,
                "journal_heads",
                deployment_id=d,
                journal_id=j.journal_id,
                event_count=0,
                head_sha=st.ZERO_HASH,
                last_sequence=None,
            )
    _insert(
        c,
        "operations",
        deployment_id=d,
        operation_id=operation_id,
        account_ref=intent.account_ref,
        plan_sha=intent.plan_sha,
        kind=intent.kind,
        intent_schema=st.INTENT_SCHEMA,
        intent_sha=intent.sha256,
        canonical_intent=intent.canonical,
        required_roles=st.canonical_bytes({"roles": [r for r, _ in rules.roles]}),
        execution_preconditions=st.canonical_bytes(pre),
        status="pending",
        created_at=observed,
        committed_at=None,
        receipt_sha=None,
        session_ref=session.identity.session_id,
        fence_ref=session.identity.fence_epoch,
    )
    _stage("operation_inserted")
    parts = []
    for ordinal, (j, raw, data) in enumerate(additions):
        _check(
            len(data) <= session._resources.authority.policy.max_record_bytes,
            "event_record_oversize",
        )
        _insert(
            c,
            "events",
            deployment_id=d,
            journal_id=j.journal_id,
            sequence=raw["sequence"],
            schema=j.value.schema,
            canonical=data,
            event_hash=raw["event_hash"],
            previous_hash=raw["previous_hash"],
            recorded_at=observed,
            transition_id=operation_id,
            session_ref=session.identity.session_id,
            fence_ref=session.identity.fence_epoch,
        )
        _stage("event_" + str(ordinal + 1))
        part = dict(
            role=rules.roles[ordinal][0],
            ordinal=ordinal,
            journal_id=j.journal_id,
            sequence=raw["sequence"],
            event_hash=raw["event_hash"],
            result_head=raw["event_hash"],
        )
        _insert(c, "event_parts", deployment_id=d, operation_id=operation_id, **part)
        parts.append(part)
        _stage("event_part")
    for n, j in enumerate(proposal.journals):
        prior = source[j.journal_id]
        for phase, seq, sha in (
            ("source", prior["sequence"], prior["head_sha"]),
            ("result", len(j.value.records) - 1, j.value.head_sha),
        ):
            _insert(
                c,
                "operation_head_references",
                deployment_id=d,
                operation_id=operation_id,
                phase=phase,
                journal_id=j.journal_id,
                sequence=seq,
                head_sha=sha,
            )
        changed = c.execute(
            "UPDATE journal_heads SET event_count=?,head_sha=?,last_sequence=? "
            "WHERE deployment_id=? AND journal_id=? AND head_sha=? AND event_count=?",
            (
                len(j.value.records),
                j.value.head_sha,
                len(j.value.records) - 1,
                d,
                j.journal_id,
                prior["head_sha"],
                0 if prior["sequence"] is None else prior["sequence"] + 1,
            ),
        ).rowcount
        _check(changed == 1, "head_compare_and_swap_failed")
        _stage("head_" + str(n + 1))
    if account is not None:
        p = account.value.projection
        c.execute(
            "UPDATE accounts SET journal_id=?,enrollment_revision=?,request_generation=? WHERE deployment_id=? AND account_ref=?",
            (
                account.journal_id,
                getattr(p, "enrollment_revision", 0),
                p.generation,
                d,
                intent.account_ref,
            ),
        )
        for revision, enrollment in enumerate(getattr(p, "enrollments", ()), 1):
            match = next(
                (
                    j
                    for j in proposal.journals
                    if j.plan_sha == enrollment.scope.plan_sha
                ),
                None,
            )
            if match:
                c.execute(
                    "UPDATE plans SET journal_id=?,status=CASE WHEN status='stopped' THEN status ELSE 'enrolled' END,enrolled_revision=? WHERE deployment_id=? AND plan_sha=?",
                    (match.journal_id, revision, d, match.plan_sha),
                )
    rt.validate_store_v2_contents(c)
    _reconcile(c)
    return parts


def _execute(session, operation_id, intent):
    """Private protected kernel. There is no caller callback or handler argument."""
    _check(
        type(session) is op.StoreSession and type(intent) is st.OperationIntent,
        "typed_input_required",
    )
    op._require(os.getpid() == session._resources.pid, "process_mismatch")
    op._require(threading.get_ident() == session._resources.thread, "thread_mismatch")
    started = False
    try:
        c, context, records, p = session._begin()
        existing = _assessment(c, operation_id, intent, p)
        if existing.classification != "absent":
            c.rollback()
            return existing
        # A global validator accepts pending as evidence, not as permission for
        # another mutation. Reconcile every existing operation before starting.
        for row in _rows(c, "operations"):
            _check(row["status"] == "committed", "pending_recovery_required")
            _receipt(c, row, p)
        _reconcile(c)
        rules = _rules_for(intent.kind)
        start = op._capture_clock()
        checked, p = _clock(session, c, context, records, start)
        if p.persistent_stop:
            op._persist(c, context, records, checked)
            session._resources.guard()
            c.commit()
            raise HttpContractError("store_clock_uncertain")
        op._persist(c, context, records, checked)
        c.execute("SAVEPOINT business")
        started = True
        proposal = _prepare(
            intent,
            operation_id,
            _catalog(c),
            _journal_values(c),
            tuple(_document(c, r["document_sha"]) for r in _rows(c, "documents")),
            datetime.fromisoformat(start.utc),
        )
        parts = _write_proposal(
            c, session, intent, operation_id, proposal, rules, start.utc, p.head_sha
        )
        end = op._capture_clock()
        ended, final = _clock(session, c, context, checked, end)
        if final.persistent_stop:
            c.execute("ROLLBACK TO business")
            c.execute("RELEASE business")
            op._persist(c, context, checked, ended)
            session._resources.guard()
            c.commit()
            raise HttpContractError("store_clock_uncertain")
        op._persist(c, context, checked, ended)
        raw = st.canonical_bytes(
            dict(
                schema=st.RECEIPT_SCHEMA,
                deployment_id=session.identity.deployment_id,
                operation_id=operation_id,
                intent_sha=intent.sha256,
                committed_at=end.utc,
                parts=parts,
            )
        )
        _insert(
            c,
            "receipts",
            deployment_id=session.identity.deployment_id,
            operation_id=operation_id,
            schema=st.RECEIPT_SCHEMA,
            receipt_sha=st.digest_bytes(raw),
            canonical=raw,
            committed_at=end.utc,
        )
        _stage("receipt_inserted")
        _stage("before_committed")
        _check(
            c.execute(
                "UPDATE operations SET status='committed',committed_at=?,receipt_sha=? WHERE deployment_id=? AND operation_id=? AND status='pending'",
                (
                    end.utc,
                    st.digest_bytes(raw),
                    session.identity.deployment_id,
                    operation_id,
                ),
            ).rowcount
            == 1,
            "operation_compare_and_swap_failed",
        )
        _guard_commit(session, c, final.head_sha)
        result = _assessment(c, operation_id, intent, final)
        _check(
            result.classification == "committed_receipt",
            "post_receipt_invalid:" + str(result.reason),
        )
        c.execute("RELEASE business")
        _stage("before_outer_commit")
        # Recheck after the failure-injection boundary, immediately before commit.
        _guard_commit(session, c, final.head_sha)
        c.commit()
        session._head = final.head_sha
        _stage("committed_before_return")
        return result
    except HttpContractError as exc:
        # Only deterministic semantic rejections preserve a valid session.
        recoverable = (
            "plan_count_exceeded",
            "enrollment_record_oversize",
            "document_source_conflict",
            "business_handler_not_installed",
        )
        if started and any(str(exc).endswith(x) for x in recoverable):
            try:
                c.rollback()
                _guard_commit(session, c, session._head)
            except BaseException:
                session._invalidate()
                raise
            raise
        session._invalidate()
        raise
    except BaseException:
        session._invalidate()
        raise


def _reconcile(c):
    journals = _journal_values(c)
    for a in (j for j in journals if j.kind == "account"):
        plans = tuple(j.value for j in journals if j.kind == "plan")
        result = reconcile_live_journals(a.value, plans)
        _check(result.classification == "consistent", "journals_not_consistent")
    return journals


def _clean_snapshot(c, identity, p):
    """Global quiescence. Body physical evidence remains unsupported in I1a-3."""
    rt.validate_store_v2_contents(c)
    _check(
        all(r["journal_id"] is not None for r in _rows(c, "accounts")),
        "quiescence_unproven",
    )
    _check(
        not p.persistent_stop and p.session_state == "running", "quiescence_unproven"
    )
    operations = _rows(c, "operations")
    for row in operations:
        _check(row["status"] == "committed", "quiescence_unproven")
        _receipt(c, row, p)
    _check(
        not any(r["kind"] == "body" for r in _rows(c, "journals")),
        "quiescence_unproven",
    )
    for j in _reconcile(c):
        q = j.value.projection
        _check(not q.attempts and not q.holds, "quiescence_unproven")
    _check(not _rows(c, "control_state"), "quiescence_unproven")
    return {
        "schema": "historical-feasibility-runtime-clean-validation-v2",
        "store_schema": rt.STORE_SCHEMA_V2,
        **identity.__dict__,
        "runtime_head": p.head_sha,
        "owner_pin_sha": p.owner_pin_sha,
        "physical_sha": p.physical_sha,
        "catalog": st._load(_catalog(c).canonical),
        "journal_heads": sorted(
            _rows(c, "journal_heads"), key=lambda r: r["journal_id"]
        ),
        "operation_receipt_set_sha": st.digest_bytes(
            st.canonical_bytes(
                {
                    "operations": sorted(
                        [
                            (r["operation_id"], r["intent_sha"], r["receipt_sha"])
                            for r in operations
                        ]
                    )
                }
            )
        ),
        "quiescence_profile": "local-c2-no-attempts-no-holds-no-body-v1",
    }
