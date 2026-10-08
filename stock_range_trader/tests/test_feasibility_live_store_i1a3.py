"""Bounded artificial commands; no public handler, HTTP, activation or trial data."""

import multiprocessing as mp
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import test_feasibility_live_store_i1a2b as prior
from test_feasibility_http_contract import approval
from test_feasibility_live_preflight import NOW, plan

from feasibility import live_store as st
from feasibility import live_store_operational as op
from feasibility import live_store_runtime as rt
from feasibility import live_store_transactions as tx
from feasibility.http_contract import HTTP_OUTPUT_ROOT, HttpContractError
from feasibility.live_http_evidence import (
    AttemptBinding,
    LiveJournal,
    capture_headers,
    required_attempt_hold,
)
from feasibility.live_plan_enrollment import (
    authority_from_preflight,
    propose_plan_enrollment,
)

authority = prior.authority
pending = prior.pending
prepared = prior.prepared
read = prior.read


class Clock:
    def __init__(self, seconds=0):
        self.n = seconds * 1000

    def __call__(self):
        self.n += 1
        return rt.ClockObservation(
            (NOW + timedelta(milliseconds=self.n)).isoformat(timespec="microseconds"),
            self.n * 1_000_000,
        )


@pytest.fixture(autouse=True)
def artificial_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(op, "_capture_clock", clock)
    return clock


def sources(a, name):
    p = plan(artifact_id=name, output_dir=str(HTTP_OUTPUT_ROOT / name))
    return p, approval(p, valid_from=p.not_before, valid_until=p.expires_at)


def intent(a, name=None, kind=None):
    if name is None:
        return st.OperationIntent(
            "fixture-open",
            a.intent.account_ref,
            None,
            st.canonical_bytes({"version": 1}),
        )
    p, approval_value = sources(a, name)
    return st.OperationIntent(
        kind or "fixture-enroll",
        a.intent.account_ref,
        p.sha256,
        st.canonical_bytes(
            {"version": 1, "name": name, "approval_sha": approval_value.sha256}
        ),
    )


def rules(kind):
    mapping = {
        "fixture-reserve-send": (
            ("account-reserve", "account"),
            ("account-send", "account"),
            ("plan-reserve", "plan"),
            ("plan-send", "plan"),
        ),
        "fixture-open": (("account-opening", "account"),),
        "fixture-enroll": (("account-enrollment", "account"), ("plan-opening", "plan")),
        "fixture-reserve": (("account-reserve", "account"), ("plan-reserve", "plan")),
        "fixture-send": (("account-send", "account"), ("plan-send", "plan")),
        "fixture-header": (
            ("account-header", "account"),
            ("account-hold", "account"),
            ("plan-header", "plan"),
        ),
    }
    if kind not in mapping:
        raise HttpContractError("fixture_kind_unsupported")
    return tx._Rules(mapping[kind])


def adapter(a):
    # No SQL/event/callback parameter; fixed artificial command kinds only.
    def prepare(command, operation_id, catalog, journals, documents, observed):
        payload = st._load(command.payload)
        canonical_preflight = st.canonical_bytes(a.preflight.to_dict()).decode()
        if command.kind in {
            "fixture-reserve-send",
            "fixture-reserve",
            "fixture-send",
            "fixture-header",
        }:
            assert command == intent(a, payload["name"], command.kind)
            account = next(j.value for j in journals if j.kind == "account")
            target = next(j for j in journals if j.plan_sha == command.plan_sha)
            scope = target.value.projection.scopes[0]
            binding = AttemptBinding(
                scope.plan_sha,
                scope.preflight_sha,
                scope.account_ref,
                scope.approval_sha,
                "attempt",
                "slot",
                "holder",
                1,
            )
            stamp = observed.isoformat(timespec="microseconds")
            av, pv = account, target.value
            if command.kind in {"fixture-reserve", "fixture-reserve-send"}:
                av = av.append(
                    "slot_reserved",
                    recorded_at=observed,
                    transition_id=operation_id,
                    binding=binding,
                    reserved_at=stamp,
                )
                pv = pv.append(
                    "attempt_reserved",
                    recorded_at=observed,
                    transition_id=operation_id,
                    binding=binding,
                    reserved_at=stamp,
                )
            if command.kind in {"fixture-send", "fixture-reserve-send"}:
                av = av.append(
                    "slot_sent",
                    recorded_at=observed,
                    transition_id=operation_id,
                    binding=binding,
                    sent_at=stamp,
                )
                pv = pv.append(
                    "attempt_sent",
                    recorded_at=observed,
                    transition_id=operation_id,
                    binding=binding,
                    sent_at=stamp,
                )
            if command.kind == "fixture-header":
                header = capture_headers(
                    observation_id="artificial-header",
                    status=200,
                    header_block_complete=True,
                    observed_at=observed,
                    retry_after_fields=(),
                )
                av = av.append(
                    "slot_headers_observed",
                    recorded_at=observed,
                    transition_id=operation_id,
                    binding=binding,
                    observation=header.to_dict(),
                )
                pv = pv.append(
                    "response_headers_observed",
                    recorded_at=observed,
                    transition_id=operation_id,
                    binding=binding,
                    observation=header.to_dict(),
                )
                hold = required_attempt_hold(
                    scope, av.projection.attempt(scope.plan_sha, "attempt")
                )
                assert hold is not None
                av = av.append(
                    "hold_entered",
                    recorded_at=observed,
                    transition_id=operation_id,
                    hold=hold.to_dict(),
                )
            return tx._Proposal(
                (),
                (
                    tx._Journal("account", "account", None, av),
                    tx._Journal(target.journal_id, "plan", target.plan_sha, pv),
                ),
            )
        if command.kind == "fixture-open":
            assert payload == {"version": 1} and not journals
            value = LiveJournal.create_account_v2(
                authority_from_preflight(
                    a.preflight, canonical_preflight=canonical_preflight
                ),
                recorded_at=observed,
                transition_id=operation_id,
            )
            return tx._Proposal((), (tx._Journal("account", "account", None, value),))
        assert command.kind == "fixture-enroll"
        name = payload["name"]
        assert command == intent(a, name, command.kind)
        p, approval_value = sources(a, name)
        account = next(j.value for j in journals if j.kind == "account")
        prior = tuple(j.value for j in journals if j.kind == "plan")
        result = propose_plan_enrollment(
            account,
            prior,
            expected_account_head=account.head_sha,
            operation_id=operation_id,
            at=observed,
            plan=p,
            preflight=a.preflight,
            approval=approval_value,
            canonical_plan=st.canonical_bytes(p.to_dict()).decode(),
            canonical_preflight=canonical_preflight,
            canonical_approval=st.canonical_bytes(approval_value.to_dict()).decode(),
        )
        docs = (
            tx._Document("plan", st.canonical_bytes(p.to_dict()), "fixture", name),
            tx._Document(
                "approval",
                st.canonical_bytes(approval_value.to_dict()),
                "fixture",
                name,
            ),
        )
        return tx._Proposal(
            docs,
            (
                tx._Journal("account", "account", None, result.account),
                tx._Journal(name, "plan", p.sha256, result.plan),
            ),
        )

    return prepare


def install(monkeypatch, a):
    monkeypatch.setattr(tx, "_prepare", adapter(a))
    monkeypatch.setattr(tx, "_rules_for", rules)


@pytest.fixture
def session(prepared, monkeypatch):
    install(monkeypatch, prepared)
    s = op.open_store(prepared)
    yield s
    if s.status == "running":
        s._invalidate()


def open_account(session):
    a = session._resources.authority
    result = tx._execute(session, "open", intent(a))
    assert result.classification == "committed_receipt"
    return result


def enroll(session, name="plan-a", operation_id="enroll-a", kind=None):
    return tx._execute(
        session, operation_id, intent(session._resources.authority, name, kind)
    )


def test_open_enroll_receipt_replay_and_nonempty_close(session):
    a = session._resources.authority
    open_account(session)
    original = enroll(session)
    assert original.classification == "committed_receipt"
    assert enroll(session, "plan-b", "enroll-b").classification == "committed_receipt"
    before = read(a)
    assert enroll(session) == original
    assert read(a) == before
    assert tx.assess_operation(session, "enroll-a", intent(a, "plan-a")) == original
    assert len(st._load(tx.read_catalog(session).canonical)["plans"]) == 2
    assert tx.read_document(session, intent(a, "plan-a").plan_sha).kind == "plan"
    session.close()
    before = read(a)
    assert tx.read_committed_receipt(a, "enroll-a", intent(a, "plan-a")) == original
    assert read(a) == before
    s = op.open_store(a)
    s.close()


def test_multiple_events_same_journal(session):
    open_account(session)
    enroll(session)
    result = enroll(session, operation_id="reserve-send", kind="fixture-reserve-send")
    assert result.classification == "committed_receipt"
    parts = st._load(result.receipt)["parts"]
    assert [(p["journal_id"], p["sequence"]) for p in parts] == [
        ("account", 2),
        ("account", 3),
        ("plan-a", 1),
        ("plan-a", 2),
    ]
    with pytest.raises(HttpContractError, match="quiescence_unproven"):
        session.close()


@pytest.mark.parametrize(
    "stage",
    [
        "operation_inserted",
        "event_1",
        "event_2",
        "head_1",
        "event_part",
        "receipt_inserted",
        "before_committed",
        "before_outer_commit",
    ],
)
def test_failure_is_atomic(session, monkeypatch, stage):
    a = session._resources.authority
    open_account(session)
    before = read(a)

    def fail(value):
        if value == stage:
            raise RuntimeError("injected")

    monkeypatch.setattr(tx, "_stage", fail)
    with pytest.raises(RuntimeError, match="injected"):
        enroll(session)
    assert session.status == "invalid"
    assert read(a) == before
    assert (
        tx.read_committed_receipt(a, "enroll-a", intent(a, "plan-a")).classification
        == "absent"
    )
    with closing(sqlite3.connect(a.intent.db_path)) as c:
        assert c.execute("SELECT count(*) FROM plans").fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM operations").fetchone() == (1,)


@pytest.mark.parametrize("field", ["kind", "plan", "payload", "document"])
def test_conflicting_intent_no_mutation(session, field):
    open_account(session)
    enroll(session)
    a = session._resources.authority
    command = intent(a, "plan-a")
    changes = {
        "kind": dict(kind="other-kind"),
        "plan": dict(plan_sha="f" * 64),
        "payload": dict(payload=st.canonical_bytes({"version": 2})),
        "document": dict(payload=st.canonical_bytes({"approval_sha": "f" * 64})),
    }
    before = read(a)
    assert (
        tx._execute(
            session, "enroll-a", replace(command, **changes[field])
        ).classification
        == "conflict"
    )
    assert read(a) == before and session.status == "running"
    session.close()


def test_capacity_and_exact_record_bytes(session):
    open_account(session)
    enroll(session)
    enroll(session, "plan-b", "enroll-b")
    a = session._resources.authority
    before = read(a)
    with pytest.raises(HttpContractError, match="plan_count_exceeded"):
        enroll(session, "plan-c", "enroll-c")
    assert read(a) == before and session.status == "running"
    c, _, _, _ = session._begin()
    prefix = st.canonical_bytes({"x": ""})
    data = st.canonical_bytes({"x": "é" * ((32768 - len(prefix)) // 2)})
    assert len(data) == 32768
    assert tx._capacity(c, intent(a, "plan-a").plan_sha, data) == 2
    with pytest.raises(HttpContractError, match="record_oversize"):
        tx._capacity(
            c, intent(a, "plan-a").plan_sha, st.canonical_bytes({"x": "é" * 16381})
        )
    c.rollback()
    session.close()


def test_rejected_business_with_failed_revalidation_invalidates(session, monkeypatch):
    a = session._resources.authority
    open_account(session)
    enroll(session)
    enroll(session, "plan-b", "enroll-b")
    before = read(a)
    guard = session._resources.guard
    calls = 0

    def changed_physical_evidence():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise HttpContractError("artificial_post_rollback_guard_failure")
        guard()

    monkeypatch.setattr(session._resources, "guard", changed_physical_evidence)
    with pytest.raises(HttpContractError, match="post_rollback_guard_failure"):
        enroll(session, "plan-c", "enroll-c")
    assert session.status == "invalid"
    assert read(a) == before
    assert (
        tx.read_committed_receipt(a, "enroll-c", intent(a, "plan-c")).classification
        == "absent"
    )


@pytest.mark.parametrize("at_end", [False, True])
def test_clock_regression_persists_stop_without_business(session, monkeypatch, at_end):
    a = session._resources.authority
    open_account(session)
    before = read(a)[0]
    normal = op._capture_clock
    count = 0

    def clock():
        nonlocal count
        count += 1
        if count == (2 if at_end else 1):
            return rt.ClockObservation(
                (NOW - timedelta(days=1)).isoformat(timespec="microseconds"), 1
            )
        return normal()

    monkeypatch.setattr(op, "_capture_clock", clock)
    with pytest.raises(HttpContractError, match="clock_uncertain"):
        enroll(session)
    p, _ = read(a)
    assert p.persistent_stop and p.event_count == before.event_count + (
        2 if at_end else 1
    )
    assert (
        tx.read_committed_receipt(a, "enroll-a", intent(a, "plan-a")).classification
        == "absent"
    )
    assert (
        tx.read_committed_receipt(a, "open", intent(a)).classification
        == "committed_receipt"
    )


def test_public_api_is_read_only_and_schema_unchanged(session):
    assert not any(
        hasattr(tx, name)
        for name in ("execute", "transaction", "register_document", "append_event")
    )
    c = session._resources.connection
    assert sorted(
        r[0]
        for r in c.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        )
    ) == sorted(rt._DDL_V2)


def _crash_worker(a, target, pipe):
    op._capture_clock = Clock(10)
    tx._rules_for = rules
    tx._prepare = adapter(a)

    def stop(stage):
        if stage == target:
            pipe.send(stage)
            pipe.recv()
            os._exit(37)

    tx._stage = stop
    try:
        s = op.open_store(a)
        tx._execute(s, "open", intent(a))
    except BaseException as exc:
        pipe.send(str(exc))
    finally:
        pipe.close()


@pytest.mark.parametrize(
    "target,expected",
    [
        ("committed_before_return", "committed_receipt"),
        ("before_outer_commit", "absent"),
    ],
)
def test_real_process_crash_receipt_only(prepared, monkeypatch, target, expected):
    install(monkeypatch, prepared)
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_crash_worker, args=(prepared, target, child))
    process.start()
    child.close()
    try:
        assert parent.poll(30)
        assert parent.recv() == target
        parent.send("exit")
        process.join(30)
        assert process.exitcode == 37
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent.close()
    before = Path(prepared.intent.db_path).read_bytes()
    first = tx.read_committed_receipt(prepared, "open", intent(prepared))
    assert first.classification == expected
    assert Path(prepared.intent.db_path).read_bytes() == before
    # Isolate dirty restart from a separate UTC-regression failure.
    monkeypatch.setattr(op, "_capture_clock", Clock(20))
    with pytest.raises(HttpContractError, match="restart_uncertain"):
        op.open_store(prepared)
    stopped = read(prepared)
    assert tx.read_committed_receipt(prepared, "open", intent(prepared)) == first
    assert read(prepared) == stopped


def tamper(c, sql, values=()):
    """Artificial corruption only; restore exact DDL so content checks are tested."""
    triggers = c.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
    ).fetchall()
    for name, _ in triggers:
        c.execute('DROP TRIGGER "' + name + '"')
    c.execute(sql, values)
    for _, ddl in triggers:
        c.execute(ddl)


def pending_operation(c, s, operation_id="pending"):
    row = dict(tx._rows(c, "operations")[0])
    row.update(
        operation_id=operation_id, status="pending", receipt_sha=None, committed_at=None
    )
    tx._insert(c, "operations", **row)


@pytest.mark.parametrize("mutation", ["pending", "later-bad-event", "old-head"])
def test_scoped_old_receipt_ignores_unrelated_state(session, mutation):
    a = session._resources.authority
    original = open_account(session)
    enroll(session)
    c = session._resources.connection
    c.execute("BEGIN IMMEDIATE")
    if mutation == "pending":
        pending_operation(c, session)
    elif mutation == "later-bad-event":
        tamper(
            c,
            "UPDATE events SET canonical=? WHERE journal_id='plan-a'",
            (b'{"broken":true}',),
        )
    else:
        row = c.execute(
            "SELECT event_hash FROM events WHERE journal_id='account' AND sequence=0"
        ).fetchone()
        c.execute(
            "UPDATE journal_heads SET event_count=1,last_sequence=0,head_sha=? WHERE journal_id='account'",
            row,
        )
    c.commit()
    session._invalidate()
    before = Path(a.intent.db_path).read_bytes()
    assert tx.read_committed_receipt(a, "open", intent(a)) == original
    assert Path(a.intent.db_path).read_bytes() == before
    if mutation != "pending":
        with pytest.raises(HttpContractError):
            op.open_store(a)


@pytest.mark.parametrize(
    "mutation",
    [
        "receipt-sha",
        "receipt-content",
        "part",
        "role",
        "source-head",
        "event",
        "preconditions",
        "document",
        "runtime",
    ],
)
def test_required_evidence_corruption_never_returns_receipt(session, mutation):
    a = session._resources.authority
    open_account(session)
    enroll(session)
    c = session._resources.connection
    c.execute("BEGIN IMMEDIATE")
    if mutation == "receipt-sha":
        # Change body, not FK key: a pure logical evidence failure.
        tamper(
            c,
            "UPDATE receipts SET canonical=? WHERE operation_id='enroll-a'",
            (b'{"broken":true}',),
        )
    elif mutation == "receipt-content":
        row = tx._rows(c, "receipts", " WHERE operation_id='enroll-a'")[0]
        value = st._load(row["canonical"])
        value["parts"] = []
        raw = st.canonical_bytes(value)
        c.execute("PRAGMA defer_foreign_keys=ON")
        tamper(
            c,
            "UPDATE receipts SET canonical=?,receipt_sha=? WHERE operation_id='enroll-a'",
            (raw, st.digest_bytes(raw)),
        )
        tamper(
            c,
            "UPDATE operations SET receipt_sha=? WHERE operation_id='enroll-a'",
            (st.digest_bytes(raw),),
        )
    elif mutation == "part":
        tamper(c, "DELETE FROM event_parts WHERE operation_id='enroll-a' AND ordinal=1")
    elif mutation == "role":
        tamper(
            c,
            "UPDATE operations SET required_roles=? WHERE operation_id='enroll-a'",
            (st.canonical_bytes({"roles": ["bad"]}),),
        )
    elif mutation == "source-head":
        tamper(
            c,
            "DELETE FROM operation_head_references WHERE operation_id='enroll-a' AND phase='source'",
        )
    elif mutation == "event":
        tamper(
            c,
            "UPDATE events SET canonical=? WHERE journal_id='plan-a'",
            (b'{"broken":true}',),
        )
    elif mutation == "preconditions":
        tamper(
            c,
            "UPDATE operations SET execution_preconditions=? WHERE operation_id='enroll-a'",
            (b"{}",),
        )
    elif mutation == "document":
        tamper(c, "UPDATE documents SET canonical=? WHERE kind='plan'", (b"{}",))
    else:
        tamper(c, "UPDATE runtime_events SET canonical=? WHERE sequence=0", (b"{}",))
    c.commit()
    session._invalidate()
    if mutation == "runtime":
        with pytest.raises(HttpContractError):
            tx.read_committed_receipt(a, "enroll-a", intent(a, "plan-a"))
    else:
        result = tx.read_committed_receipt(a, "enroll-a", intent(a, "plan-a"))
        assert result.classification == "inconsistent" and result.receipt is None


@pytest.mark.parametrize("table", ["receipts", "events"])
def test_missing_fk_evidence_rejects_at_core_boundary(session, table):
    a = session._resources.authority
    open_account(session)
    enroll(session)
    c = session._resources.connection
    c.execute("PRAGMA foreign_keys=OFF")
    if table == "receipts":
        tamper(c, "DELETE FROM receipts WHERE operation_id='enroll-a'")
    else:
        tamper(c, "DELETE FROM events WHERE journal_id='plan-a'")
    c.execute("PRAGMA foreign_keys=ON")
    session._invalidate()
    with pytest.raises(HttpContractError, match="foreign_key"):
        tx.read_committed_receipt(a, "enroll-a", intent(a, "plan-a"))


@pytest.mark.parametrize("table", ["receipts", "event_parts", "operations"])
@pytest.mark.parametrize(
    "verb", ["UPDATE", "DELETE", "REPLACE", "INSERT OR REPLACE", "UPSERT"]
)
def test_immutable_operation_evidence(session, table, verb):
    open_account(session)
    c = session._resources.connection
    row = tx._rows(c, table)[0]
    columns = ",".join(row)
    binds = ",".join("?" for _ in row)
    if verb == "UPDATE":
        query, values = f"UPDATE {table} SET operation_id=operation_id", ()
    elif verb == "DELETE":
        query, values = f"DELETE FROM {table}", ()
    elif verb == "UPSERT":
        query, values = (
            f"INSERT INTO {table} ({columns}) VALUES ({binds}) ON CONFLICT DO UPDATE SET operation_id=excluded.operation_id",
            tuple(row.values()),
        )
    else:
        query, values = (
            f"{verb} INTO {table} ({columns}) VALUES ({binds})",
            tuple(row.values()),
        )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(query, values)
    assert tx._rows(c, table) == (row,)
    session.close()


def test_pending_no_completion_and_no_clean_close(session):
    a = session._resources.authority
    open_account(session)
    c, _, _, _ = session._begin()
    pending_operation(c, session)
    c.commit()
    before = read(a)
    assert (
        tx._execute(session, "pending", intent(a)).classification
        == "pending_recovery_required"
    )
    assert read(a) == before
    with pytest.raises(HttpContractError, match="quiescence_unproven"):
        session.close()
    before = read(a)
    assert (
        tx.read_committed_receipt(a, "pending", intent(a)).classification
        == "pending_recovery_required"
    )
    assert read(a) == before


def test_document_registration_idempotency_and_source_conflict(session):
    a = session._resources.authority
    open_account(session)
    enroll(session)
    c, _, _, p = session._begin()
    doc = tx._document(c, intent(a, "plan-a").plan_sha)
    value = tx._Document(
        doc.kind, doc.content, doc.source_identity, doc.source_reference
    )
    original = tx._register_document(
        c, session.identity.deployment_id, value, p.last_accepted_utc
    )
    assert original == doc  # original registered_at, not a retry timestamp
    with pytest.raises(HttpContractError, match="document_source_conflict"):
        tx._register_document(
            c,
            session.identity.deployment_id,
            replace(value, source_reference="other"),
            p.last_accepted_utc,
        )
    p2, _ = sources(a, "plan-b")
    new = tx._register_document(
        c,
        session.identity.deployment_id,
        replace(value, content=st.canonical_bytes(p2.to_dict())),
        p.last_accepted_utc,
    )
    assert new.sha256 != doc.sha256 and new.source_reference == doc.source_reference
    c.rollback()
    session.close()


def test_end_clock_stop_persistence_failure_rolls_back(session, monkeypatch):
    a = session._resources.authority
    open_account(session)
    before = read(a)
    normal_clock, normal_persist = op._capture_clock, op._persist
    count = 0

    def clock():
        nonlocal count
        count += 1
        return (
            normal_clock()
            if count == 1
            else rt.ClockObservation(NOW.isoformat(timespec="microseconds"), 0)
        )

    def persist(c, context, old, records):
        if rt.replay_runtime(context, records).persistent_stop:
            raise sqlite3.OperationalError("stop-write-failed")
        return normal_persist(c, context, old, records)

    monkeypatch.setattr(op, "_capture_clock", clock)
    monkeypatch.setattr(op, "_persist", persist)
    with pytest.raises(sqlite3.OperationalError, match="stop-write-failed"):
        enroll(session)
    assert session.status == "invalid" and read(a) == before
    assert (
        tx.read_committed_receipt(a, "enroll-a", intent(a, "plan-a")).classification
        == "absent"
    )


def test_clock_receipt_and_transaction_trace(session):
    c = session._resources.connection
    statements = []
    c.set_trace_callback(statements.append)
    result = open_account(session)
    c.set_trace_callback(None)
    assert sum(s == "BEGIN IMMEDIATE" for s in statements) == 1
    assert "SAVEPOINT business" in statements and "COMMIT" in statements
    _, records = read(session._resources.authority)
    start, end = (rt.RuntimeEvent.from_bytes(r) for r in records[-2:])
    assert start.kind == end.kind == "clock_observed"
    assert st._load(result.receipt)["committed_at"] == end.observation.utc
    session.close()


def test_snapshot_is_order_independent(session, monkeypatch):
    open_account(session)
    enroll(session)
    c, _, _, p = session._begin()
    first = tx._clean_snapshot(c, session.identity, p)
    normal = tx._rows

    def reverse(c, table, where="", values=()):
        rows = normal(c, table, where, values)
        return rows if "ORDER BY" in where else tuple(reversed(rows))

    monkeypatch.setattr(tx, "_rows", reverse)
    assert tx._clean_snapshot(c, session.identity, p) == first
    c.rollback()
    session.close()


def test_current_head_and_catalog_are_not_caller_authority(session):
    open_account(session)
    enroll(session)
    c = session._resources.connection
    c.execute("UPDATE accounts SET enrollment_revision=0")
    with pytest.raises(HttpContractError, match="enrollment_revision_invalid"):
        enroll(session, "plan-b", "enroll-b")
    assert session.status == "invalid"


def test_receipt_validates_historical_unchanged_plan_dependency(session):
    a = session._resources.authority
    open_account(session)
    enroll(session)
    original_b = enroll(session, "plan-b", "enroll-b")
    assert original_b.classification == "committed_receipt"
    c, _, _, _ = session._begin()
    tamper(c, "UPDATE events SET canonical=? WHERE journal_id='plan-a'", (b"{}",))
    c.commit()
    session._invalidate()
    result = tx.read_committed_receipt(a, "enroll-b", intent(a, "plan-b"))
    assert result.classification == "inconsistent" and result.receipt is None
    assert (
        tx.read_committed_receipt(a, "open", intent(a)).classification
        == "committed_receipt"
    )


@pytest.mark.parametrize("guard", ["pid", "thread", "fence", "session", "pragma"])
def test_stale_session_mutation_rejected(session, guard):
    if guard == "pid":
        original = session._resources.pid
        session._resources.pid = -1
    elif guard == "thread":
        original = session._resources.thread
        session._resources.thread = -1
    elif guard == "fence":
        session._identity = replace(session.identity, fence_epoch=10)
    elif guard == "session":
        session._identity = replace(session.identity, session_id="stale")
    else:
        session._resources.connection.execute("PRAGMA foreign_keys=OFF")
    try:
        with pytest.raises(HttpContractError):
            open_account(session)
    finally:
        if guard == "pid":
            session._resources.pid = original
        if guard == "thread":
            session._resources.thread = original


def test_receipt_read_requires_exclusive_lock(session):
    a = session._resources.authority
    open_account(session)
    with pytest.raises(HttpContractError, match="lock_unavailable"):
        tx.read_committed_receipt(a, "open", intent(a))
    session.close()


def test_unrelated_pending_blocks_new_mutation_not_original_receipt(session):
    a = session._resources.authority
    original = open_account(session)
    c, _, _, _ = session._begin()
    pending_operation(c, session)
    c.commit()
    with pytest.raises(HttpContractError, match="pending_recovery_required"):
        enroll(session)
    assert tx.read_committed_receipt(a, "open", intent(a)) == original
    assert (
        tx.read_committed_receipt(a, "enroll-a", intent(a, "plan-a")).classification
        == "absent"
    )


def test_stopped_counts_and_prepared_does_not(session):
    a = session._resources.authority
    open_account(session)
    enroll(session)
    c, _, _, p = session._begin()
    c.execute(
        "UPDATE plans SET status='stopped' WHERE plan_sha=?",
        (intent(a, "plan-a").plan_sha,),
    )
    first = tx._rows(c, "plans")[0]
    plan_b, approval_b = sources(a, "plan-b")
    for kind, value in (("plan", plan_b), ("approval", approval_b)):
        tx._register_document(
            c,
            session.identity.deployment_id,
            tx._Document(
                kind, st.canonical_bytes(value.to_dict()), "fixture", "plan-b"
            ),
            p.last_accepted_utc,
        )
    first.update(
        plan_sha=plan_b.sha256,
        approval_sha=approval_b.sha256,
        journal_id=None,
        status="prepared",
        enrolled_revision=None,
    )
    tx._insert(c, "plans", **first)
    assert tx._capacity(c, plan_b.sha256, st.canonical_bytes({"artificial": True})) == 1
    c.commit()
    result = enroll(session, "plan-b", "enroll-b")
    assert result.classification == "committed_receipt"
    with pytest.raises(HttpContractError, match="plan_count_exceeded"):
        enroll(session, "plan-c", "enroll-c")
    session.close()


def _serial_worker(a, name, seconds, pipe):
    op._capture_clock = Clock(seconds)
    tx._rules_for, tx._prepare = rules, adapter(a)
    try:
        s = op.open_store(a)
        try:
            result = enroll(s, name, "enroll-" + name)
            pipe.send(result.classification)
        except HttpContractError as exc:
            pipe.send(str(exc))
        s.close()
    except BaseException as exc:
        pipe.send("unexpected:" + str(exc))
    finally:
        pipe.close()


def test_serial_processes_one_remaining_slot(session):
    a = session._resources.authority
    open_account(session)
    enroll(session)
    session.close()
    for n, (name, expected) in enumerate(
        (("plan-b", "committed_receipt"), ("plan-c", "live_store_plan_count_exceeded"))
    ):
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe()
        process = ctx.Process(target=_serial_worker, args=(a, name, 10 + n * 10, child))
        process.start()
        child.close()
        try:
            assert parent.poll(30)
            assert parent.recv() == expected
            process.join(30)
            assert process.exitcode == 0
        finally:
            if process.is_alive():
                process.terminate()
                process.join(10)
            parent.close()
    with closing(sqlite3.connect(a.intent.db_path)) as c:
        assert c.execute(
            "SELECT count(*) FROM plans WHERE enrolled_revision IS NOT NULL"
        ).fetchone() == (2,)


def test_clock_stop_and_physical_guard_immediately_before_commit(session, monkeypatch):
    a = session._resources.authority
    open_account(session)
    original = session._resources.guard
    armed = False

    def stage(value):
        nonlocal armed
        if value == "before_outer_commit":
            armed = True

    def guard():
        if armed:
            raise HttpContractError("artificial_physical_mismatch")
        original()

    monkeypatch.setattr(tx, "_stage", stage)
    monkeypatch.setattr(session._resources, "guard", guard)
    with pytest.raises(HttpContractError, match="physical_mismatch"):
        enroll(session)
    assert session.status == "invalid"
    assert (
        tx.read_committed_receipt(a, "enroll-a", intent(a, "plan-a")).classification
        == "absent"
    )


def test_unsupported_body_journal_not_clean(session):
    open_account(session)
    enroll(session)
    c, _, _, _ = session._begin()
    d = session.identity.deployment_id
    tx._insert(
        c,
        "journals",
        deployment_id=d,
        journal_id="body",
        account_ref=session.identity.account_ref,
        plan_sha=intent(session._resources.authority, "plan-a").plan_sha,
        kind="body",
        schema=st.BODY_EVENT_SCHEMA,
        status="prepared",
    )
    tx._insert(
        c,
        "journal_heads",
        deployment_id=d,
        journal_id="body",
        event_count=0,
        head_sha=st.ZERO_HASH,
        last_sequence=None,
    )
    c.commit()
    with pytest.raises(HttpContractError, match="quiescence_unproven"):
        session.close()


@pytest.mark.parametrize("runtime_count", [0, 20])
def test_artificial_performance_observation(session, runtime_count, capsys):
    # Observations, not timing gates. Runtime growth is independent of business.
    # Enrollment growth necessarily couples plans/events/operations; report all
    # four dimensions rather than pretending they are independent variables.
    for _ in range(runtime_count):
        session.checkpoint_clock()
    times = {}
    for name, action in (
        ("open", lambda: open_account(session)),
        ("enroll-1", lambda: enroll(session)),
        ("enroll-2", lambda: enroll(session, "plan-b", "enroll-b")),
        (
            "receipt",
            lambda: tx.assess_operation(
                session, "open", intent(session._resources.authority)
            ),
        ),
        ("catalog", lambda: tx.read_catalog(session)),
    ):
        start = time.perf_counter()
        action()
        times[name] = time.perf_counter() - start
    c, _, _, _ = session._begin()
    counts = {
        t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("operations", "events", "plans", "runtime_events")
    }
    started = time.perf_counter()
    tx._capacity(c, intent(session._resources.authority, "plan-a").plan_sha, b"{}")
    times["capacity"] = time.perf_counter() - started
    c.rollback()
    started = time.perf_counter()
    session.close()
    times["close"] = time.perf_counter() - started
    with capsys.disabled():
        print("I1a3 artificial performance", counts, times)


@pytest.mark.parametrize(
    "axis,variant",
    [
        ("plans", 0),
        ("plans", 16),
        ("operations", 0),
        ("operations", 1),
        ("events", 0),
        ("events", 1),
    ],
)
def test_separate_performance_axes(session, axis, variant, capsys):
    a = session._resources.authority
    open_account(session)
    enroll(session)
    next_kind, next_name = "fixture-enroll", "plan-b"
    expected_close = "success"
    if axis == "plans":
        c, _, _, p = session._begin()
        template = tx._rows(c, "plans")[0]
        for i in range(variant):
            name = f"prepared-{i}"
            fixed, claim = sources(a, name)
            for kind, value in (("plan", fixed), ("approval", claim)):
                tx._register_document(
                    c,
                    session.identity.deployment_id,
                    tx._Document(
                        kind, st.canonical_bytes(value.to_dict()), "fixture", name
                    ),
                    p.last_accepted_utc,
                )
            row = dict(template)
            row.update(
                plan_sha=fixed.sha256,
                approval_sha=claim.sha256,
                journal_id=None,
                status="prepared",
                enrolled_revision=None,
            )
            tx._insert(c, "plans", **row)
        c.commit()
    else:
        expected_close = "quiescence_unproven"
        next_name = "plan-a"
        if axis == "operations":
            if variant:
                enroll(session, operation_id="reserve", kind="fixture-reserve")
                enroll(session, operation_id="send", kind="fixture-send")
            else:
                enroll(
                    session, operation_id="reserve-send", kind="fixture-reserve-send"
                )
                # Same runtime-event count as the split operation alternative.
                session.checkpoint_clock()
                session.checkpoint_clock()
            next_kind = "fixture-header"
        else:
            enroll(
                session,
                operation_id="event-growth",
                kind="fixture-reserve-send" if variant else "fixture-reserve",
            )
            next_kind = "fixture-header" if variant else "fixture-send"
    c = session._resources.connection
    counts = {
        table: c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("operations", "events", "plans", "runtime_events")
    }
    if axis == "operations":
        assert counts == {
            "operations": 3 + variant,
            "events": 7,
            "plans": 1,
            "runtime_events": 11,
        }
    elif axis == "events":
        assert counts == {
            "operations": 3,
            "events": 5 + 2 * variant,
            "plans": 1,
            "runtime_events": 9,
        }
    else:
        assert counts == {
            "operations": 2,
            "events": 3,
            "plans": 1 + variant,
            "runtime_events": 7,
        }
    times = {}
    start = time.perf_counter()
    assert (
        enroll(session, next_name, "measured", next_kind).classification
        == "committed_receipt"
    )
    times["mutation"] = time.perf_counter() - start
    start = time.perf_counter()
    assert (
        tx.assess_operation(session, "open", intent(a)).classification
        == "committed_receipt"
    )
    times["receipt"] = time.perf_counter() - start
    c, _, _, _ = session._begin()
    start = time.perf_counter()
    tx._capacity(c, intent(a, "plan-a").plan_sha, b"{}")
    times["capacity"] = time.perf_counter() - start
    c.rollback()
    start = time.perf_counter()
    if expected_close == "success":
        session.close()
    else:
        with pytest.raises(HttpContractError, match=expected_close):
            session.close()
    times["close"] = time.perf_counter() - start
    start = time.perf_counter()
    assert (
        tx.read_committed_receipt(a, "open", intent(a)).classification
        == "committed_receipt"
    )
    times["receipt-only"] = time.perf_counter() - start
    with capsys.disabled():
        print(
            "I1a3 separate axis", axis, variant, counts, times, "close", expected_close
        )
