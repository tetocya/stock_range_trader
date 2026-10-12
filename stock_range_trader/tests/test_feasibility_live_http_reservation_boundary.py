"""2A pure boundary and artificial runtime tests, without reservation handlers."""

import hashlib
import inspect
import json
import multiprocessing as mp
import os
import sqlite3
import time
from dataclasses import FrozenInstanceError, fields, replace
from datetime import timedelta

import pytest
import test_feasibility_live_store_i1a2b as runtime_fixture
import test_feasibility_live_store_i1b as i1b
from test_feasibility_live_body_enrollment_compatibility import EnrolledScenario
from test_feasibility_live_http_budget import IDENTITY, Fixture
from test_feasibility_live_store_i1a3 import Clock

from feasibility import live_body_contract as c3
from feasibility import live_http_budget as budget
from feasibility import live_http_reservation_boundary as boundary
from feasibility import live_store as st
from feasibility import live_store_enrollment as en
from feasibility import live_store_operational as op
from feasibility import live_store_runtime as rt
from feasibility import live_store_terminal as terminal
from feasibility import live_store_transactions as tx
from feasibility.http_contract import HttpContractError

authority = i1b.authority
pending = i1b.pending
prepared = i1b.prepared
clock = i1b.clock
session = i1b.session


def request(snapshot, *, query_id=None, page_index=0):
    query_id = query_id or c3._hash(snapshot.content["scope"]["queries"][0])
    return boundary.HTTPReservationRequestV1(
        snapshot.plan_sha,
        query_id,
        page_index,
        boundary.semantic_request_identity(snapshot.plan_sha, query_id, page_index),
    )


def test_request_roundtrip_and_budget_semantics():
    f = Fixture()
    value = request(f.c.plan)
    page = c3.LivePageIdentity(
        f.c.plan.plan_sha, c3._canonical(f.fixed.queries[0].to_dict()), 0, None
    )
    core = budget.build_admission(
        f.c.plan,
        **f.sources(),
        identity=IDENTITY,
        page=page,
        start_observation_reference="artificial-clock",
    )
    assert value.semantic_request_sha == core.semantic_request_sha
    assert boundary.validate_request(value, f.c.plan) is None
    assert boundary.HTTPReservationRequestV1.from_bytes(value.to_bytes()) == value
    assert len(fields(value)) == 4
    with pytest.raises(FrozenInstanceError):
        value.page_index = 1
    raw = {
        "domain": "http-reservation-semantic-v1",
        "value": {
            "plan_sha": value.plan_sha,
            "query_id": value.query_id,
            "page_index": 0,
        },
    }
    independent = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert independent == value.semantic_request_sha
    assert request(f.c.plan, page_index=1).semantic_request_sha != independent


@pytest.mark.parametrize(
    "field",
    ["plan_sha", "query_id", "page_index", "semantic_request_sha", "schema"],
)
def test_missing_fields(field):
    raw = request(Fixture().c.plan).to_dict()
    del raw[field]
    with pytest.raises(HttpContractError, match="fields_invalid"):
        boundary.HTTPReservationRequestV1.from_dict(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("current_heads", {}),
        ("holder", "caller"),
        ("attempt_number", 1),
        ("generation", 1),
        ("slot_id", "caller-slot"),
        ("cursor", "arbitrary"),
        ("clock", "2026-01-01T00:00:00+00:00"),
        ("sql", "INSERT"),
        ("callback", lambda: None),
        ("live_permission", True),
        ("operation_id", "separate"),
    ],
)
def test_caller_overrides_and_callback_rejected(field, value):
    original = request(Fixture().c.plan)
    with pytest.raises(HttpContractError, match="fields_invalid"):
        boundary.HTTPReservationRequestV1.from_dict(
            {**original.to_dict(), field: value}
        )
    with pytest.raises(TypeError):
        boundary.validate_request(original, Fixture().c.plan, **{field: value})


@pytest.mark.parametrize("page", [-1, True, 1.0, "0", None, 1 << 63])
def test_invalid_page_types_and_overflow(page):
    value = request(Fixture().c.plan)
    with pytest.raises(HttpContractError):
        replace(value, page_index=page)
    with pytest.raises(HttpContractError):
        boundary.semantic_request_identity(value.plan_sha, value.query_id, page)


def test_strict_serialization_and_semantic_binding():
    value = request(Fixture().c.plan)
    invalid = [
        value.to_bytes()[:-1] + b',"page_index":0}',
        b" " + value.to_bytes(),
        b" " * (c3.MAX_RECORD_BYTES + 1),
        b'{"page_index":' + b"9" * 5000 + b"}",
    ]
    for raw in invalid:
        with pytest.raises(HttpContractError):
            boundary.HTTPReservationRequestV1.from_bytes(raw)
    for field in ("plan_sha", "query_id", "semantic_request_sha"):
        with pytest.raises(HttpContractError):
            replace(value, **{field: "0" * 64})
    with pytest.raises(HttpContractError, match="schema_invalid"):
        boundary.HTTPReservationRequestV1.from_dict(
            {**value.to_dict(), "schema": "future-v2"}
        )


def test_plan_membership_and_page_bound():
    f = Fixture()
    with pytest.raises(HttpContractError, match="plan_mismatch"):
        boundary.validate_request(request(EnrolledScenario().c.plan), f.c.plan)
    with pytest.raises(HttpContractError, match="query_not_in_plan"):
        boundary.validate_request(request(f.c.plan, query_id="0" * 64), f.c.plan)
    cap = f.fixed.limits.max_pages_per_query
    assert (
        boundary.validate_request(request(f.c.plan, page_index=cap - 1), f.c.plan)
        is None
    )
    with pytest.raises(HttpContractError, match="page_limit"):
        boundary.validate_request(request(f.c.plan, page_index=cap), f.c.plan)
    # Well-formed later page is allowed here. Historical progression belongs to 2B.
    assert boundary.validate_request(request(f.c.plan, page_index=1), f.c.plan) is None
    with pytest.raises(HttpContractError, match="request_type"):
        boundary.validate_request(lambda: None, f.c.plan)
    with pytest.raises(HttpContractError, match="snapshot_type"):
        boundary.validate_request(request(f.c.plan), f.c.plan.to_dict())


def test_fixed_roles_kind_and_permissions():
    contract = boundary.ReservationBoundaryV1()
    assert boundary.ReservationBoundaryV1.from_bytes(contract.to_bytes()) == contract
    assert [(r.role, r.journal_kind, r.event_kind) for r in contract.roles] == [
        ("account-reservation", "account", "slot_reserved"),
        ("plan-reservation", "plan", "attempt_reserved"),
        ("body-reservation", "body", "reserved"),
    ]
    for roles in (
        contract.roles[::-1],
        contract.roles[:-1],
        contract.roles + (contract.roles[-1],),
        (*contract.roles[:-1], replace(contract.roles[-1], event_kind="committed")),
    ):
        with pytest.raises(HttpContractError, match="roles_mismatch"):
            replace(contract, roles=roles)
    with pytest.raises(HttpContractError, match="kind_mismatch"):
        replace(contract, kind="i2-http-send-v1")
    for field in ("reservation_permitted", "live_send_permitted"):
        assert getattr(contract, field) is False
        with pytest.raises(HttpContractError, match="permission_must_be_false"):
            boundary.ReservationBoundaryV1.from_dict(
                {**contract.to_dict(), field: True}
            )
    with pytest.raises(TypeError):
        boundary.ReservationBoundaryV1(callback=lambda: None)
    with pytest.raises(HttpContractError, match="business_handler_not_installed"):
        tx._rules_for(contract.kind)
    with pytest.raises(HttpContractError, match="business_handler_not_installed"):
        tx._prepare(None, "operation", None, None, None, None)


@pytest.mark.parametrize("value", ["op-1", "a" * 128, "", "a" * 129, "A", True, None])
def test_separate_operation_id_uses_store_label_contract(value):
    if value in ("op-1", "a" * 128):
        assert boundary.validate_operation_id(value) is None
        assert st._label(value) == value
    else:
        with pytest.raises(HttpContractError):
            boundary.validate_operation_id(value)
        with pytest.raises(HttpContractError):
            st._label(value)


def test_boundary_is_pure_and_has_no_runtime_surface(monkeypatch):
    import builtins
    import socket

    snapshot = Fixture().c.plan
    value = request(snapshot)

    def forbidden(*args, **kwargs):
        raise AssertionError("I/O or Store mutation attempted")

    for target, name in (
        (builtins, "open"),
        (socket, "socket"),
        (sqlite3, "connect"),
        (os, "getenv"),
        (op, "open_store"),
        (tx, "_execute"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    assert boundary.validate_request(value, snapshot) is None
    assert boundary.HTTPReservationRequestV1.from_bytes(value.to_bytes()) == value
    contract = boundary.ReservationBoundaryV1()
    assert not contract.reservation_permitted and not contract.live_send_permitted
    for obj in (boundary, value, contract, op.StoreSession):
        for name in (
            "reserve",
            "send",
            "dispatch",
            "resume",
            "recover",
            "repin",
            "clear_stop",
        ):
            assert not hasattr(obj, name)


def test_pure_reserved_oracle_preserves_nonterminal_evidence():
    s = EnrolledScenario().begin()
    before = (s.a.records, s.p.records, s.j.to_bytes())
    reserved = s.j.projection.reserved_remaining_bytes
    assert reserved > 0
    result = terminal._assess_journals(s.a, (s.p,), (s.j,))
    assert result.logical_status == "incomplete" and not result.clean_eligible
    assert s.a.projection.attempts[0].state == "reserved"
    assert s.p.projection.attempts[0].state == "reserved"
    assert s.j.object(s.oid).state == "reserved"
    assert not result.reservation_permitted and not result.recovery_permitted
    assert before == (s.a.records, s.p.records, s.j.to_bytes())
    assert reserved == s.j.projection.reserved_remaining_bytes
    # No operation/receipt is forged and these journals are never inserted in SQLite.


def enroll(session, prepared):
    en.open_enrollment_account(session, "open")
    req = i1b.request(prepared)
    receipt = en.enroll_plan(session, "enroll", req)
    return req, receipt


def test_idle_close_reopen_and_original_receipt(session, prepared, capsys):
    req, original = enroll(session, prepared)
    assert terminal.assess_terminal_state(session).clean_eligible
    start = time.perf_counter()
    session.close()
    closed = time.perf_counter()
    before = i1b.snapshot(prepared)
    read_start = time.perf_counter()
    assert (
        tx.read_committed_receipt(
            prepared, "enroll", req.intent(prepared.intent.account_ref)
        )
        == original
    )
    read = time.perf_counter()
    assert i1b.snapshot(prepared) == before
    reopen_start = time.perf_counter()
    again = op.open_store(prepared)
    reopened = time.perf_counter()
    try:
        assert again.identity.fence_epoch > session.identity.fence_epoch
        assert en.enroll_plan(again, "enroll", req) == original
    finally:
        again.close()
    with capsys.disabled():
        print(
            "2A artificial timings",
            {
                "close_s": closed - start,
                "receipt_s": read - read_start,
                "reopen_s": reopened - reopen_start,
            },
        )


def test_reserved_oracle_close_failure_no_marker(session, prepared, monkeypatch):
    req, original = enroll(session, prepared)
    s = EnrolledScenario().begin()
    source_bytes = (s.a.records, s.p.records, s.j.to_bytes())
    before = i1b.snapshot(prepared)
    runtime_before = runtime_fixture.read(prepared)

    # Private TEST ONLY seam: the real Store has idle enrollment, not a valid
    # persisted reservation. Exercise existing close failure with the pure oracle.
    def incomplete(c, identity, projection):
        return terminal._assess_journals(s.a, (s.p,), (s.j,))

    monkeypatch.setattr(terminal, "_assess_db", incomplete)
    monkeypatch.setattr(terminal, "_select_clean_snapshot", terminal._clean_snapshot)
    with pytest.raises(HttpContractError, match="quiescence_unproven:incomplete"):
        session.close()
    assert session.status == "invalid"
    assert runtime_fixture.read(prepared) == runtime_before
    assert runtime_before[0].session_state == "running"
    assert i1b.snapshot(prepared) == before  # No session_closed/checkpoint persisted.
    assert source_bytes == (s.a.records, s.p.records, s.j.to_bytes())
    # Acquiring the locked reader proves close released its OS lock.
    assert (
        tx.read_committed_receipt(
            prepared, "enroll", req.intent(prepared.intent.account_ref)
        )
        == original
    )
    assert i1b.snapshot(prepared) == before
    with pytest.raises(HttpContractError, match="restart_uncertain"):
        op.open_store(prepared)
    stopped = i1b.snapshot(prepared)
    with pytest.raises(HttpContractError, match="persistent_stop"):
        op.open_store(prepared)
    assert i1b.snapshot(prepared) == stopped


def _dirty_worker(a, req, pipe):
    op._capture_clock = Clock(1)
    try:
        session = op.open_store(a)
        en.open_enrollment_account(session, "open")
        original = en.enroll_plan(session, "enroll", req)
        pipe.send(("ready", original))
        if pipe.recv() == "exit-dirty":
            os._exit(38)
        raise AssertionError("unexpected child command")
    except BaseException as exc:
        pipe.send(("error", repr(exc)))
    finally:
        pipe.close()


def test_process_exit_lock_stop_and_receipt_only(prepared, monkeypatch, capsys):
    req = i1b.request(prepared)
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_dirty_worker, args=(prepared, req, child))
    process.start()
    child.close()
    try:
        assert parent.poll(30)
        status, original = parent.recv()
        assert status == "ready", original
        before = i1b.snapshot(prepared)
        with pytest.raises(HttpContractError, match="lock_unavailable"):
            op.open_store(prepared)
        with pytest.raises(HttpContractError, match="lock_unavailable"):
            tx.read_committed_receipt(
                prepared, "enroll", req.intent(prepared.intent.account_ref)
            )
        assert i1b.snapshot(prepared) == before
        parent.send("exit-dirty")
        process.join(30)
        assert process.exitcode == 38
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(10)
    assert runtime_fixture.read(prepared)[0].session_state == "running"
    assert i1b.snapshot(prepared) == before
    start = time.perf_counter()
    assert (
        tx.read_committed_receipt(
            prepared, "enroll", req.intent(prepared.intent.account_ref)
        )
        == original
    )
    read = time.perf_counter()
    assert i1b.snapshot(prepared) == before
    monkeypatch.setattr(op, "_capture_clock", Clock(2))
    restart_start = time.perf_counter()
    with pytest.raises(HttpContractError, match="restart_uncertain"):
        op.open_store(prepared)
    restarted = time.perf_counter()
    p, records = runtime_fixture.read(prepared)
    assert p.persistent_stop and p.session_state == "stopped"
    assert all(b'"session_closed"' not in raw for raw in records)
    stopped = i1b.snapshot(prepared)
    with pytest.raises(HttpContractError, match="persistent_stop"):
        op.open_store(prepared)
    assert (
        tx.read_committed_receipt(
            prepared, "enroll", req.intent(prepared.intent.account_ref)
        )
        == original
    )
    assert (
        tx.read_committed_receipt(
            prepared, "absent", req.intent(prepared.intent.account_ref)
        ).classification
        == "absent"
    )
    changed = replace(req.intent(prepared.intent.account_ref), payload=b"{}")
    assert (
        tx.read_committed_receipt(prepared, "enroll", changed).classification
        == "conflict"
    )
    assert i1b.snapshot(prepared) == stopped
    with capsys.disabled():
        print(
            "2A dirty timings",
            {"receipt_s": read - start, "restart_reject_s": restarted - restart_start},
        )


@pytest.mark.parametrize("guard", ["fence", "physical"])
def test_guard_failure_preserves_receipt_and_runtime(session, prepared, guard):
    enroll(session, prepared)
    before = i1b.snapshot(prepared)
    if guard == "fence":
        session._identity = replace(
            session.identity, fence_epoch=session.identity.fence_epoch + 1
        )
    else:
        # Change only the artificial lock inode; keep DB and evidence intact.
        path = prepared.intent.lock_path
        os.rename(path, path + ".displaced")
        with open(path, "xb"):
            pass
        os.chmod(path, 0o600)
    reason = "session_fence_mismatch" if guard == "fence" else "physical_path_replaced"
    with pytest.raises(HttpContractError, match=reason):
        session.close()
    assert session.status == "invalid"
    assert i1b.snapshot(prepared) == before


def test_clock_stop_has_no_close_marker(session, prepared, monkeypatch):
    req, original = enroll(session, prepared)
    monkeypatch.setattr(
        op,
        "_capture_clock",
        lambda: rt.ClockObservation(
            (i1b.NOW - timedelta(days=1)).isoformat(timespec="microseconds"), 0
        ),
    )
    with pytest.raises(HttpContractError, match="clock_uncertain"):
        session.checkpoint_clock()
    assert session.status == "invalid"
    p, records = runtime_fixture.read(prepared)
    assert p.persistent_stop and p.session_state == "stopped"
    assert all(b'"session_closed"' not in raw for raw in records)
    before = i1b.snapshot(prepared)
    with pytest.raises(HttpContractError, match="persistent_stop"):
        op.open_store(prepared)
    assert (
        tx.read_committed_receipt(
            prepared, "enroll", req.intent(prepared.intent.account_ref)
        )
        == original
    )
    assert i1b.snapshot(prepared) == before


def test_unregistered_kind_has_no_store_mutation(session, prepared):
    req, _ = enroll(session, prepared)
    value = request(req.body_contract.plan)
    before = i1b.snapshot(prepared)
    intent = st.OperationIntent(
        boundary.RESERVATION_KIND,
        session.identity.account_ref,
        value.plan_sha,
        value.to_bytes(),
    )
    with pytest.raises(HttpContractError, match="business_handler_not_installed"):
        tx._execute(session, "not-executable", intent)
    assert i1b.snapshot(prepared) == before
    assert "callback" not in inspect.signature(tx._execute).parameters
