"""Production I1b APIs on artificial physical Stores; no acquisition."""

import multiprocessing as mp
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import test_feasibility_live_store_i1a2b as prior
from test_feasibility_http_contract import approval
from test_feasibility_live_preflight import NOW, plan
from test_feasibility_live_store_i1a3 import Clock

from feasibility import live_body_contract as c3
from feasibility import live_enrollment_sources as codec
from feasibility import live_http_evidence as c2
from feasibility import live_store as st
from feasibility import live_store_enrollment as en
from feasibility import live_store_operational as op
from feasibility import live_store_runtime as rt
from feasibility import live_store_transactions as tx
from feasibility.http_contract import HTTP_OUTPUT_ROOT, HttpContractError

authority = prior.authority
pending = prior.pending
prepared = prior.prepared


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(op, "_capture_clock", value)
    return value


@pytest.fixture
def session(prepared):
    value = op.open_store(prepared)
    yield value
    if value.status == "running":
        value._invalidate()


def request(a, name="plan-a", until=None):
    p = plan(artifact_id=name, output_dir=str(HTTP_OUTPUT_ROOT / name))
    claim = approval(p, valid_from=p.not_before, valid_until=until or p.expires_at)
    policy = c3.StoragePolicy(100, 1000, 1000, 1000, 10000)
    contract = c3.LiveBodyStorageContract(
        c3.FixedPlanSnapshot.from_plan(p), a.preflight.sha256, policy
    )
    root = c3.PhysicalRootIdentity(
        contract.root_binding_sha,
        a.pin.store_uuid,
        contract.root,
        1,
        2,
        "synthetic-mount",
        1,
        NOW,
        "synthetic-observation",
    )
    return en.PlanEnrollmentRequest(
        en.EnrollmentSource(
            "plan",
            st.canonical_bytes(p.to_dict()),
            "fixture-owner",
            "urn:fixture:" + name,
        ),
        en.EnrollmentSource(
            "approval",
            st.canonical_bytes(claim.to_dict()),
            "fixture-owner",
            "urn:fixture:approval",
        ),
        contract,
        policy,
        root,
    )


def snapshot(a):
    with closing(sqlite3.connect(a.intent.db_path)) as c:
        return tuple(
            (table, tuple(c.execute("SELECT * FROM " + table)))
            for table in (
                "documents",
                "accounts",
                "plans",
                "journals",
                "events",
                "journal_heads",
                "operations",
                "event_parts",
                "operation_head_references",
                "receipts",
                "runtime_events",
            )
        )


def test_required_close_reopen_end_to_end(prepared):
    a = prepared
    s = op.open_store(a)
    opened = en.open_enrollment_account(s, "opening")
    assert opened.classification == "committed_receipt"
    assert st._load(tx.read_catalog(s).canonical)["accounts"][0]["status"] == "prepared"
    assert not tx._rows(s._resources.connection, "plans")
    s.close()
    receipts = []
    for name in ("plan-a", "plan-b"):
        s = op.open_store(a)
        result = en.enroll_plan(s, name, request(a, name))
        assert result.classification == "committed_receipt", result.reason
        receipts.append(result.receipt)
        assert len(st._load(result.receipt)["parts"]) == 3
        tx._reconcile(s._resources.connection)
        s.close()
    s = op.open_store(a)
    old = en.enroll_plan(s, "plan-a", request(a))
    assert old.receipt == receipts[0]
    assert len(st._load(tx.read_catalog(s).canonical)["plans"]) == 2
    s.close()
    assert (
        tx.read_committed_receipt(
            a, "plan-a", request(a).intent(a.intent.account_ref)
        ).receipt
        == receipts[0]
    )


def test_duplicate_capacity_and_conflict_preserve_session(session, prepared):
    first = en.open_enrollment_account(session, "open")
    before = snapshot(prepared)
    assert en.open_enrollment_account(session, "open") == first
    with pytest.raises(HttpContractError, match="duplicate_account"):
        en.open_enrollment_account(session, "other-open")
    assert snapshot(prepared) == before
    req = request(prepared)
    first = en.enroll_plan(session, "a", req)
    before = snapshot(prepared)
    with pytest.raises(HttpContractError, match="duplicate_plan"):
        en.enroll_plan(session, "another-a", req)
    assert snapshot(prepared) == before
    assert (
        en.enroll_plan(session, "a", request(prepared, "plan-b")).classification
        == "conflict"
    )
    assert en.enroll_plan(session, "a", req) == first
    en.enroll_plan(session, "b", request(prepared, "plan-b"))
    before = snapshot(prepared)
    with pytest.raises(HttpContractError, match="plan_count_exceeded"):
        en.enroll_plan(session, "c", request(prepared, "plan-c"))
    assert snapshot(prepared) == before and session.status == "running"
    session.close()


@pytest.mark.parametrize("end_expiry", [False, True])
def test_approval_expiry_rollback_and_session_continuation(
    session, prepared, clock, end_expiry
):
    en.open_enrollment_account(session, "open")
    boundary = NOW + timedelta(milliseconds=clock.n + (2 if end_expiry else 1))
    req = request(prepared, until=boundary)
    before = snapshot(prepared)
    with pytest.raises(HttpContractError, match="approval_expired"):
        en.enroll_plan(session, "expired", req)
    assert snapshot(prepared) == before
    assert session.status == "running"
    assert (
        en.enroll_plan(session, "valid", request(prepared)).classification
        == "committed_receipt"
    )
    session.close()


@pytest.mark.parametrize(
    "stage",
    [
        "documents_registered",
        "operation_inserted",
        "event_1",
        "event_2",
        "event_3",
        "catalog_updated",
        "head_1",
        "head_3",
        "event_part",
        "receipt_inserted",
        "before_committed",
        "before_outer_commit",
    ],
)
def test_atomic_failure(session, prepared, monkeypatch, stage):
    en.open_enrollment_account(session, "open")
    before = snapshot(prepared)

    def fail(name):
        if name == stage:
            raise RuntimeError("artificial failure")

    monkeypatch.setattr(tx, "_stage", fail)
    with pytest.raises(RuntimeError, match="artificial"):
        en.enroll_plan(session, "a", request(prepared))
    assert snapshot(prepared) == before
    assert session.status == "invalid"


@pytest.mark.parametrize(
    "field", ["source_identity", "source_reference", "registered_at"]
)
def test_provenance_tamper_rejects_original_receipt(session, prepared, field):
    en.open_enrollment_account(session, "open")
    req = request(prepared)
    en.enroll_plan(session, "a", req)
    session.close()
    with closing(sqlite3.connect(prepared.intent.db_path)) as c:
        # Test-only corruption restores original DDL after bypassing triggers.
        triggers = list(
            c.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='documents'"
            )
        )
        for name, _ in triggers:
            c.execute('DROP TRIGGER "' + name + '"')
        value = (
            "other-source"
            if field != "registered_at"
            else (NOW - timedelta(days=1)).isoformat(timespec="microseconds")
        )
        c.execute(
            "UPDATE documents SET " + field + "=? WHERE document_sha=?",
            (value, req.plan.sha256),
        )
        for _, sql in triggers:
            c.execute(sql)
        c.commit()
    result = tx.read_committed_receipt(
        prepared, "a", req.intent(prepared.intent.account_ref)
    )
    assert result.classification == "inconsistent" and result.receipt is None
    with pytest.raises(HttpContractError):
        op.open_store(prepared)


def test_decoder_no_filesystem_io(prepared, monkeypatch):
    req = request(prepared)

    def forbidden(*args, **kwargs):
        raise AssertionError("filesystem lookup")

    monkeypatch.setattr(Path, "is_symlink", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    p = codec.decode_plan(req.plan.content, req.plan.sha256)
    assert (
        codec.decode_approval(req.approval.content, req.approval.sha256, p).sha256
        == req.approval.sha256
    )
    raw = st.canonical_bytes(prepared.preflight.to_dict())
    assert codec.decode_preflight(raw, st.digest_bytes(raw)) == prepared.preflight


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "duplicate", "noncanonical", "schema", "scope", "digest", "nested"],
)
def test_strict_source_rejections(prepared, mutation):
    req = request(prepared)
    raw = st._load(req.plan.content)
    if mutation == "unknown":
        raw["surprise"] = True
    elif mutation == "schema":
        raw["schema"] = "unknown"
    elif mutation == "scope":
        raw["scope"]["queries"] = []
    elif mutation == "nested":
        raw["limits"]["surprise"] = 1
    data = st.canonical_bytes(raw)
    if mutation == "duplicate":
        data = b'{"schema":"x",' + data[1:]
    if mutation == "noncanonical":
        data += b"\n"
    digest = "a" * 64 if mutation == "digest" else st.digest_bytes(data)
    with pytest.raises(HttpContractError):
        codec.decode_plan(data, digest)


def _worker(a, req, stage, pipe, seconds, operation_id="child"):
    op._capture_clock = Clock(seconds)
    try:
        s = op.open_store(a)

        def crash(name):
            if name == stage:
                pipe.send(name)
                pipe.recv()
                os._exit(37)

        tx._stage = crash
        result = en.enroll_plan(s, operation_id, req)
        s.close()
        pipe.send(result.classification)
    except Exception as exc:
        if "s" in locals() and s.status == "running":
            s.close()
        pipe.send(str(exc))
    finally:
        pipe.close()


@pytest.mark.parametrize("stage", ["before_outer_commit", "committed_before_return"])
def test_subprocess_crash_receipt(prepared, monkeypatch, stage):
    s = op.open_store(prepared)
    en.open_enrollment_account(s, "open")
    s.close()
    req = request(prepared)
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_worker, args=(prepared, req, stage, child, 1))
    process.start()
    child.close()
    try:
        assert parent.poll(30) and parent.recv() == stage
        parent.send("exit")
        process.join(30)
        assert process.exitcode == 37
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join()
    monkeypatch.setattr(op, "_capture_clock", Clock(2))
    with pytest.raises(HttpContractError, match="restart_uncertain"):
        op.open_store(prepared)
    before = snapshot(prepared)
    result = tx.read_committed_receipt(
        prepared, "child", req.intent(prepared.intent.account_ref)
    )
    assert result.classification == (
        "committed_receipt" if stage == "committed_before_return" else "absent"
    )
    if result.receipt:
        assert len(st._load(result.receipt)["parts"]) == 3
        with closing(sqlite3.connect(prepared.intent.db_path)) as c:
            assert (
                result.receipt
                == c.execute(
                    "SELECT canonical FROM receipts WHERE operation_id='child'"
                ).fetchone()[0]
            )
    assert snapshot(prepared) == before


def _corrupt(a, table, statement, values=()):
    with closing(sqlite3.connect(a.intent.db_path)) as c:
        triggers = list(
            c.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                (table,),
            )
        )
        for name, _ in triggers:
            c.execute('DROP TRIGGER "' + name + '"')
        c.execute(statement, values)
        for _, sql in triggers:
            c.execute(sql)
        c.commit()


@pytest.mark.parametrize(
    "damage",
    [
        "manifest",
        "root",
        "contract",
        "session",
        "fence",
        "prior-head",
        "part",
        "historical-event",
    ],
)
def test_damaged_enrollment_evidence_never_returns_receipt(session, prepared, damage):
    en.open_enrollment_account(session, "open")
    req = request(prepared)
    en.enroll_plan(session, "a", req)
    c = session._resources.connection
    row = tx._rows(c, "operations", " WHERE operation_id='a'")[0]
    pre = st._load(row["execution_preconditions"])
    session.close()
    if damage in {"manifest", "root", "contract", "prior-head"}:
        m = pre["initialization_manifest"]
        if damage == "manifest":
            m["operation_id"] = "wrong"
        elif damage == "root":
            m["root_claim"]["inode"] += 1
        elif damage == "contract":
            m["body_contract"]["policy"]["cumulative_budget"] += 1
        else:
            m["source_account_head"] = "a" * 64
        _corrupt(
            prepared,
            "operations",
            "UPDATE operations SET execution_preconditions=? WHERE operation_id='a'",
            (st.canonical_bytes(pre),),
        )
    elif damage in {"session", "fence"}:
        field, value = (
            ("session_ref", "wrong") if damage == "session" else ("fence_ref", 500)
        )
        _corrupt(
            prepared,
            "operations",
            "UPDATE operations SET " + field + "=? WHERE operation_id='a'",
            (value,),
        )
    elif damage == "part":
        _corrupt(
            prepared,
            "event_parts",
            "DELETE FROM event_parts WHERE operation_id='a' AND ordinal=2",
        )
    else:
        _corrupt(
            prepared,
            "events",
            "UPDATE events SET recorded_at=? WHERE journal_id='i1b-account' AND sequence=0",
            ((NOW - timedelta(days=1)).isoformat(timespec="microseconds"),),
        )
    try:
        result = tx.read_committed_receipt(
            prepared, "a", req.intent(prepared.intent.account_ref)
        )
    except HttpContractError:
        pass  # Structural/FK failures reject before operation assessment.
    else:
        assert result.classification == "inconsistent" and result.receipt is None


def test_receipt_after_expiry_uses_original_clock(session, prepared, monkeypatch):
    en.open_enrollment_account(session, "open")
    req = request(prepared, until=NOW + timedelta(seconds=1))
    old = en.enroll_plan(session, "a", req)
    session.close()
    monkeypatch.setattr(op, "_capture_clock", Clock(4000))
    s = op.open_store(prepared)
    before = snapshot(prepared)
    assert en.enroll_plan(s, "a", req) == old
    assert snapshot(prepared) == before
    s.close()
    before = snapshot(prepared)

    def forbidden():
        raise AssertionError("receipt reader observed clock")

    monkeypatch.setattr(op, "_capture_clock", forbidden)
    assert (
        tx.read_committed_receipt(
            prepared, "a", req.intent(prepared.intent.account_ref)
        )
        == old
    )
    assert snapshot(prepared) == before


@pytest.mark.parametrize(
    "change",
    [
        "root-missing",
        "policy-missing",
        "root-owner",
        "root-future",
        "body-preflight",
        "store-capacity",
        "approval-binding",
    ],
)
def test_explicit_claims_and_binding_rejected(session, prepared, change):
    en.open_enrollment_account(session, "open")
    req = request(prepared)
    before = snapshot(prepared)
    with pytest.raises(HttpContractError):
        if change == "root-missing":
            req = replace(req, root_claim=None)
        elif change == "policy-missing":
            req = replace(req, storage_policy=None)
        elif change == "root-owner":
            req = replace(
                req, root_claim=replace(req.root_claim, owner_store_ref="wrong-store")
            )
        elif change == "root-future":
            req = replace(
                req,
                root_claim=replace(
                    req.root_claim, observed_at=NOW + timedelta(seconds=1)
                ),
            )
        elif change == "body-preflight":
            contract = replace(req.body_contract, preflight_sha="a" * 64)
            req = replace(
                req,
                body_contract=contract,
                root_claim=replace(
                    req.root_claim, root_binding_sha=contract.root_binding_sha
                ),
            )
        elif change == "store-capacity":
            policy = replace(
                req.storage_policy,
                cumulative_budget=prepared.policy.max_cumulative_acquired_bytes + 1,
            )
            contract = replace(req.body_contract, policy=policy)
            req = replace(
                req,
                storage_policy=policy,
                body_contract=contract,
                root_claim=replace(
                    req.root_claim, root_binding_sha=contract.root_binding_sha
                ),
            )
        else:
            data = st._load(req.approval.content)
            data["scope_sha256"] = "b" * 64
            req = replace(
                req, approval=replace(req.approval, content=st.canonical_bytes(data))
            )
        en.enroll_plan(session, "bad", req)
    assert snapshot(prepared) == before and session.status == "running"
    assert (
        en.enroll_plan(session, "good", request(prepared)).classification
        == "committed_receipt"
    )
    session.close()


@pytest.mark.parametrize("source", ["registry", "preflight", "approval"])
def test_nested_strict_decoders(prepared, source):
    req = request(prepared)
    if source == "registry":
        raw = prepared.preflight.registry.to_dict()
        raw["active_records"][0]["extra"] = "invalid"
        decode = codec.decode_registry
    elif source == "preflight":
        raw = prepared.preflight.to_dict()
        raw["registry"]["active_records"][0]["account_ref"] = "wrong"
        decode = codec.decode_preflight
    else:
        raw = st._load(req.approval.content)
        raw["retry"]["extra"] = 1
        p = codec.decode_plan(req.plan.content, req.plan.sha256)

        def decode(data, sha):
            return codec.decode_approval(data, sha, p)

    data = st.canonical_bytes(raw)
    with pytest.raises(HttpContractError):
        decode(data, st.digest_bytes(data))


def _append_evidence(s, journal, value):
    """Artificial future I2/I3 evidence, never exposed by production APIs."""
    c = s._resources.connection
    c.execute("BEGIN IMMEDIATE")
    for data in value.records[len(journal.value.records) :]:
        raw = st._load(data.encode())
        event = raw["event"]
        tx._insert(
            c,
            "events",
            deployment_id=s.identity.deployment_id,
            journal_id=journal.journal_id,
            sequence=raw["sequence"],
            schema=raw["schema"],
            canonical=data.encode(),
            event_hash=raw["event_hash"],
            previous_hash=raw["previous_hash"],
            recorded_at=event["recorded_at"],
            transition_id=event.get("transition_id", event.get("operation_id")),
            session_ref=s.identity.session_id,
            fence_ref=s.identity.fence_epoch,
        )
    c.execute(
        "UPDATE journal_heads SET event_count=?,last_sequence=?,head_sha=? WHERE journal_id=?",
        (
            len(value.records),
            len(value.records) - 1,
            value.head_sha,
            journal.journal_id,
        ),
    )
    if journal.kind == "account":
        c.execute(
            "UPDATE accounts SET request_generation=?", (value.projection.generation,)
        )
    c.commit()


@pytest.mark.parametrize(
    "history",
    [
        "additional-audit",
        "reserved",
        "sent",
        "terminal",
        "released-hold",
        "active-hold",
        "body-object",
        "writer",
    ],
)
def test_unsupported_history_has_no_clean_marker(
    session, prepared, monkeypatch, history
):
    en.open_enrollment_account(session, "open")
    req = request(prepared)
    original = en.enroll_plan(session, "a", req)
    journals = tx._journal_values(session._resources.connection)
    a = next(j for j in journals if j.kind == "account")
    p = next(j for j in journals if j.kind == "plan")
    b = next(j for j in journals if j.kind == "body")
    at = NOW + timedelta(seconds=1)
    if history == "additional-audit":
        body = b.value.append(
            c3.BodyEvent(
                "extra", "audit", at, None, '{"evidence_ref":"artificial-extra"}', None
            )
        )
        _append_evidence(session, b, body)
    else:
        scope = p.value.projection.scopes[0]
        binding = c2.AttemptBinding(
            scope.plan_sha,
            scope.preflight_sha,
            scope.account_ref,
            scope.approval_sha,
            "attempt",
            "slot",
            "holder",
            1,
        )
        av, pv = a.value, p.value
        if history in {"active-hold", "released-hold"}:
            hold = c2.LiveHold(
                "hold",
                "manual-review",
                ("artificial",),
                at,
                "manual",
                False,
                scope.policy_sha,
                at,
            )
            av = av.append(
                "hold_entered",
                recorded_at=at,
                transition_id="hold",
                hold=hold.to_dict(),
            )
            if history == "released-hold":
                av = av.append(
                    "hold_released",
                    recorded_at=at,
                    transition_id="release",
                    hold_id="hold",
                    expected_hold_sha256=hold.hold_version_sha256,
                    clock_reference="clock",
                    account_state_reference="account",
                    manual_review_reference="review",
                    repair_reference=None,
                )
        else:
            av = av.append(
                "slot_reserved",
                recorded_at=at,
                transition_id="reserve",
                binding=binding,
                reserved_at=at.isoformat(timespec="microseconds"),
            )
            pv = pv.append(
                "attempt_reserved",
                recorded_at=at,
                transition_id="reserve",
                binding=binding,
                reserved_at=at.isoformat(timespec="microseconds"),
            )
            if history in {"sent", "terminal"}:
                av = av.append(
                    "slot_sent",
                    recorded_at=at,
                    transition_id="send",
                    binding=binding,
                    sent_at=at.isoformat(timespec="microseconds"),
                )
                pv = pv.append(
                    "attempt_sent",
                    recorded_at=at,
                    transition_id="send",
                    binding=binding,
                    sent_at=at.isoformat(timespec="microseconds"),
                )
            if history == "terminal":
                av = av.append(
                    "slot_settled",
                    recorded_at=at,
                    transition_id="settle",
                    binding=binding,
                    outcome="unknown",
                    status=None,
                    settled_at=at.isoformat(timespec="microseconds"),
                )
                pv = pv.append(
                    "outcome_unknown",
                    recorded_at=at,
                    transition_id="settle",
                    binding=binding,
                    outcome="unknown",
                    status=None,
                    settled_at=at.isoformat(timespec="microseconds"),
                )
                hold = c2.required_attempt_hold(
                    scope, av.projection.attempt(scope.plan_sha, "attempt")
                )
                av = av.append(
                    "hold_entered",
                    recorded_at=at,
                    transition_id="wait",
                    hold=hold.to_dict(),
                )
        _append_evidence(session, a, av)
        _append_evidence(session, p, pv)
        if history in {"body-object", "writer"}:
            identity = c3.ObjectIdentity(
                binding,
                c3.LivePageIdentity(
                    scope.plan_sha,
                    st.canonical_bytes(
                        req.body_contract.plan.content["scope"]["queries"][0]
                    ).decode(),
                    0,
                    None,
                ),
            )
            body = b.value.reserve(
                identity, at=at, operation_id="reserve-body", account=av, plans=(pv,)
            )
            if history == "writer":
                body = body.writer(
                    identity.object_id,
                    c3.WriterEvidence(binding, "writer", "open", at, 0),
                    operation_id="writer-open",
                )
            _append_evidence(session, b, body)
    monkeypatch.setattr(op, "_capture_clock", Clock(2))
    before = prior.read(prepared)[1]
    with pytest.raises(
        HttpContractError, match="quiescence_unproven|body_reconciliation_failed"
    ):
        session.close()
    assert prior.read(prepared)[1] == before
    assert (
        tx.read_committed_receipt(
            prepared, "a", req.intent(prepared.intent.account_ref)
        )
        == original
    )


def _run_worker(a, req, seconds, operation_id="child"):
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(
        target=_worker, args=(a, req, "no-crash", child, seconds, operation_id)
    )
    process.start()
    child.close()
    try:
        assert parent.poll(30)
        result = parent.recv()
        process.join(30)
        assert process.exitcode == 0
        return result
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join()


def test_process_lock_and_serialized_last_capacity(session, prepared):
    en.open_enrollment_account(session, "open")
    en.enroll_plan(session, "a", request(prepared))
    assert "lock" in _run_worker(prepared, request(prepared, "plan-b"), 1)
    session.close()
    assert _run_worker(prepared, request(prepared, "plan-b"), 2) == "committed_receipt"
    # Worker uses the same operation ID; change its semantic target must conflict.
    assert _run_worker(prepared, request(prepared, "plan-c"), 3) == "conflict"
    assert "plan_count_exceeded" in _run_worker(
        prepared, request(prepared, "plan-c"), 4, "new-c"
    )
    with closing(sqlite3.connect(prepared.intent.db_path)) as c:
        assert (
            c.execute(
                "SELECT count(*) FROM plans WHERE enrolled_revision IS NOT NULL"
            ).fetchone()[0]
            == 2
        )


def test_claims_are_not_physical_measurements_and_no_network(
    session, prepared, monkeypatch
):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("network/credentials forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    en.open_enrollment_account(session, "open")
    req = request(prepared)
    assert en.enroll_plan(session, "a", req).classification == "committed_receipt"
    assert not Path(req.body_contract.root).exists()
    session.close()


@pytest.mark.parametrize("at_end", [False, True])
def test_i1b_clock_stop_has_no_partial_enrollment(
    session, prepared, monkeypatch, at_end
):
    en.open_enrollment_account(session, "open")
    before = snapshot(prepared)
    normal = op._capture_clock
    count = 0

    def clock():
        nonlocal count
        count += 1
        if count == (2 if at_end else 1):
            return rt.ClockObservation(
                (NOW - timedelta(days=1)).isoformat(timespec="microseconds"), 0
            )
        return normal()

    monkeypatch.setattr(op, "_capture_clock", clock)
    with pytest.raises(HttpContractError, match="clock_uncertain"):
        en.enroll_plan(session, "a", request(prepared))
    assert snapshot(prepared)[:-1] == before[:-1]
    assert session.status == "invalid" and prior.read(prepared)[0].persistent_stop
    assert (
        tx.read_committed_receipt(
            prepared, "a", request(prepared).intent(prepared.intent.account_ref)
        ).classification
        == "absent"
    )


def test_i1b_stop_write_failure_invalidates_without_partial_state(
    session, prepared, monkeypatch
):
    en.open_enrollment_account(session, "open")
    before = snapshot(prepared)
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
            raise sqlite3.OperationalError("artificial-stop-write")
        return normal_persist(c, context, old, records)

    monkeypatch.setattr(op, "_capture_clock", clock)
    monkeypatch.setattr(op, "_persist", persist)
    with pytest.raises(sqlite3.OperationalError, match="artificial-stop-write"):
        en.enroll_plan(session, "a", request(prepared))
    assert session.status == "invalid" and snapshot(prepared) == before


def test_record_oversize_is_business_rejection(authority):
    policy = replace(authority.policy, max_record_bytes=5000)
    a = replace(
        authority,
        policy=policy,
        intent=replace(authority.intent, store_policy_sha=policy.sha256),
    )
    a = prior.pin(a, op.create_bootstrap(a))
    op.finalize_bootstrap(a)
    s = op.open_store(a)
    try:
        en.open_enrollment_account(s, "open")
        before = snapshot(a)
        with pytest.raises(HttpContractError, match="record_oversize"):
            en.enroll_plan(s, "large", request(a))
        assert snapshot(a) == before and s.status == "running"
        s.close()
    finally:
        if s.status == "running":
            s._invalidate()


def test_prepared_only_and_stopped_capacity_through_handler(session, prepared):
    en.open_enrollment_account(session, "open")
    en.enroll_plan(session, "a", request(prepared))
    c = session._resources.connection
    c.execute("UPDATE plans SET status='stopped'")
    c.commit()
    b = request(prepared, "plan-b")
    c.execute("BEGIN IMMEDIATE")
    for source in (b.plan, b.approval):
        tx._register_document(
            c,
            session.identity.deployment_id,
            tx._Document(
                source.kind,
                source.content,
                source.source_identity,
                source.source_reference,
            ),
            NOW.isoformat(timespec="microseconds"),
        )
    tx._insert(
        c,
        "plans",
        deployment_id=session.identity.deployment_id,
        account_ref=session.identity.account_ref,
        plan_sha=b.plan.sha256,
        preflight_sha=prepared.preflight.sha256,
        approval_sha=b.approval.sha256,
        policy_sha=prepared.policy.sha256,
        plan_kind="plan",
        plan_document_schema=st._DOCUMENT_SCHEMAS["plan"],
        preflight_kind="preflight",
        preflight_document_schema=st._DOCUMENT_SCHEMAS["preflight"],
        approval_kind="approval",
        approval_document_schema=st._DOCUMENT_SCHEMAS["approval"],
        schema=c2.LIVE_PLAN_SCHEMA,
        journal_id=None,
        status="prepared",
        enrolled_revision=None,
    )
    c.commit()
    assert en.enroll_plan(session, "b", b).classification == "committed_receipt"
    before = snapshot(prepared)
    with pytest.raises(HttpContractError, match="plan_count_exceeded"):
        en.enroll_plan(session, "c", request(prepared, "plan-c"))
    assert snapshot(prepared) == before
    assert [
        p["status"] for p in st._load(tx.read_catalog(session).canonical)["plans"]
    ].count("stopped") == 1
    session.close()


@pytest.mark.parametrize("change", ["kind", "source", "approval", "root"])
def test_same_operation_changed_semantic_intent_conflicts(session, prepared, change):
    en.open_enrollment_account(session, "open")
    req = request(prepared)
    old = en.enroll_plan(session, "a", req)
    before = snapshot(prepared)
    if change == "kind":
        result = en.open_enrollment_account(session, "a")
    else:
        if change == "source":
            req = replace(req, plan=replace(req.plan, source_identity="other"))
        elif change == "approval":
            data = st._load(req.approval.content)
            data["approval_event_id"] = "different"
            req = replace(
                req, approval=replace(req.approval, content=st.canonical_bytes(data))
            )
        else:
            req = replace(req, root_claim=replace(req.root_claim, inode=3))
        result = en.enroll_plan(session, "a", req)
    assert result.classification == "conflict" and result.receipt is None
    assert snapshot(prepared) == before
    assert en.enroll_plan(session, "a", request(prepared)) == old
    session.close()


def test_current_head_tamper_blocks_new_enrollment_only(session, prepared):
    en.open_enrollment_account(session, "open")
    req = request(prepared)
    old = en.enroll_plan(session, "a", req)
    c = session._resources.connection
    head = c.execute(
        "SELECT event_hash FROM events WHERE journal_id='i1b-account' AND sequence=0"
    ).fetchone()[0]
    c.execute(
        "UPDATE journal_heads SET event_count=1,last_sequence=0,head_sha=? WHERE journal_id='i1b-account'",
        (head,),
    )
    c.commit()
    before = snapshot(prepared)
    with pytest.raises(HttpContractError, match="current_head_mismatch"):
        en.enroll_plan(session, "b", request(prepared, "plan-b"))
    assert session.status == "invalid" and snapshot(prepared) == before
    assert (
        tx.read_committed_receipt(
            prepared, "a", req.intent(prepared.intent.account_ref)
        )
        == old
    )


def test_public_api_and_ddl_receipt_contract(session, prepared):
    import inspect

    assert tuple(inspect.signature(en.open_enrollment_account).parameters) == (
        "session",
        "operation_id",
    )
    assert tuple(inspect.signature(en.enroll_plan).parameters) == (
        "session",
        "operation_id",
        "request",
    )
    en.open_enrollment_account(session, "open")
    result = en.enroll_plan(session, "a", request(prepared))
    raw = st._load(result.receipt)
    assert set(raw) == {
        "schema",
        "deployment_id",
        "operation_id",
        "intent_sha",
        "committed_at",
        "parts",
    }
    assert [p["role"] for p in raw["parts"]] == [
        "account-enrollment",
        "plan-opening",
        "body-initialization",
    ]
    assert sorted(
        r[0]
        for r in session._resources.connection.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        )
    ) == sorted(rt._DDL_V2)
    assert not any(
        hasattr(en, name)
        for name in ("connection", "cursor", "transaction", "send", "activate")
    )
    session.close()


def test_extra_empty_body_catalog_is_not_hidden(session, prepared):
    en.open_enrollment_account(session, "open")
    req = request(prepared)
    old = en.enroll_plan(session, "a", req)
    c = session._resources.connection
    c.execute("BEGIN IMMEDIATE")
    tx._insert(
        c,
        "journals",
        deployment_id=session.identity.deployment_id,
        journal_id="extra-empty-body",
        account_ref=session.identity.account_ref,
        plan_sha=req.plan.sha256,
        kind="body",
        schema=c3.EVENT_SCHEMA,
        status="prepared",
    )
    tx._insert(
        c,
        "journal_heads",
        deployment_id=session.identity.deployment_id,
        journal_id="extra-empty-body",
        event_count=0,
        head_sha=st.ZERO_HASH,
        last_sequence=None,
    )
    c.commit()
    with pytest.raises(HttpContractError, match="body_catalog_incomplete"):
        en.enroll_plan(session, "b", request(prepared, "plan-b"))
    assert session.status == "invalid"
    assert (
        tx.read_committed_receipt(
            prepared, "a", req.intent(prepared.intent.account_ref)
        )
        == old
    )


@pytest.mark.parametrize("boundary", ["before", "exact", "inside"])
def test_start_approval_boundary(session, prepared, clock, boundary):
    en.open_enrollment_account(session, "open")
    req = request(prepared)
    p = codec.decode_plan(req.plan.content, req.plan.sha256)
    # The next Store observation, never a caller time supplied to the API.
    at = NOW + timedelta(milliseconds=clock.n + 1)
    start = at + timedelta(
        milliseconds={"before": 1, "exact": 0, "inside": -1}[boundary]
    )
    claim = approval(p, valid_from=start, valid_until=p.expires_at)
    req = replace(
        req, approval=replace(req.approval, content=st.canonical_bytes(claim.to_dict()))
    )
    before = snapshot(prepared)
    if boundary == "before":
        with pytest.raises(HttpContractError, match="approval_expired"):
            en.enroll_plan(session, "a", req)
        assert snapshot(prepared) == before and session.status == "running"
    else:
        assert en.enroll_plan(session, "a", req).classification == "committed_receipt"
    session.close()
