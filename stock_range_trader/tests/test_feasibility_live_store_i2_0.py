"""I2-0 artificial logical oracle and real macOS/Linux Store guards. No HTTP."""

import inspect
import multiprocessing as mp
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from datetime import timedelta

import pytest
import test_feasibility_live_body_contract as body_fixture
import test_feasibility_live_http_evidence as evidence_fixture
import test_feasibility_live_store_i1a3 as old
import test_feasibility_live_store_i1b as i1b

from feasibility import live_body_contract as c3
from feasibility import live_http_evidence as c2
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


def release(s):
    for hs in s.a.projection.holds:
        if hs.released_at is not None:
            continue
        s.n = max(s.n, int((hs.hold.not_before - body_fixture.NOW).total_seconds()))
        s.a = s.a.append(
            "hold_released",
            recorded_at=s.tick(),
            transition_id="release-" + hs.hold.hold_id,
            hold_id=hs.hold.hold_id,
            expected_hold_sha256=hs.hold.hold_version_sha256,
            clock_reference="synthetic-clock",
            account_state_reference="synthetic-state",
            manual_review_reference="synthetic-review"
            if hs.hold.release_mode == "manual"
            else None,
            repair_reference="synthetic-repair"
            if hs.hold.reason_code == "ledger_inconsistency"
            else None,
        )
    return s


def state(name):
    s = body_fixture.Scenario()
    if name == "idle":
        return s
    s.begin()
    if name == "reserved":
        return s
    if name == "pre-send-reclaim":
        at = body_fixture.NOW + timedelta(
            seconds=1 + s.scope.account_policy.slot_lease_seconds
        )
        s.n = int((at - body_fixture.NOW).total_seconds())
        s.a = s.a.append(
            "slot_reclaimed",
            recorded_at=at,
            transition_id="reclaimed",
            binding=s.b,
            evidence={
                "lease_expired_at": body_fixture.stamp(at),
                "observed_at": body_fixture.stamp(at),
                "clock_status": "verified",
                "previous_transport_termination_reference": "synthetic-termination",
                "operator_review_reference": "synthetic-review",
                "process_restart_detected": True,
            },
        )
        s.sync_hold()
        return release(s)
    s.opened()
    if name in {"sent", "writer-open"}:
        return s
    if name == "writer-unknown":
        w = replace(s.j.object(s.oid).writer, state="unknown", observed_at=s.tick())
        s.j = s.j.writer(s.oid, w, operation_id="unknown-writer")
        return s
    if name == "headers":
        at = s.tick()
        h = c2.capture_headers(
            observation_id="h",
            status=200,
            header_block_complete=True,
            observed_at=at,
            retry_after_fields=(),
        )
        s.pair(
            "response_headers_observed",
            "slot_headers_observed",
            at,
            observation=h.to_dict(),
        )
        s.sync_hold()
        return s
    s.observed(
        state=name
        if name in {"partial", "orphan"}
        else "partial"
        if name in {"reviewed-unknown", "non-200"}
        else "staging"
    )
    s.received(
        outcome="unknown" if name in {"unknown", "reviewed-unknown"} else "response",
        status=503 if name == "non-200" else 200,
    )
    if name == "unknown":
        return s
    if name == "terminal-incomplete-c3":
        return release(s)
    s.closed()
    if name == "closed-no-rehash":
        return release(s)
    s.stabilized()
    if name in {"reviewed-unknown", "non-200"}:
        if name == "non-200":
            s.acquired()
        s.j = s.j.quarantine_object(s.quarantine_evidence())
    elif name in {"partial", "orphan"}:
        return release(s)
    else:
        s.acquired().committed()
    if name == "active-hold":
        return s
    return release(s)


@pytest.mark.parametrize(
    "name,logical",
    [
        ("idle", "terminal_proven"),
        ("reserved", "incomplete"),
        ("sent", "incomplete"),
        ("headers", "incomplete"),
        ("terminal", "terminal_proven"),
        ("non-200", "terminal_proven"),
        ("terminal-incomplete-c3", "incomplete"),
        ("active-hold", "incomplete"),
        ("unknown", "recovery_required"),
        ("reviewed-unknown", "terminal_proven"),
        ("pre-send-reclaim", "incomplete"),
        ("writer-open", "incomplete"),
        ("writer-unknown", "recovery_required"),
        ("closed-no-rehash", "incomplete"),
        ("partial", "recovery_required"),
        ("orphan", "recovery_required"),
    ],
)
def test_logical_physical_matrix(name, logical):
    s = state(name)
    result = terminal._assess_journals(s.a, (s.p,), (s.j,))
    assert result.logical_status == logical, result
    assert result.clean_eligible == (name == "idle")
    assert result.physical_status == (
        "not_required" if name == "idle" else "unsupported_physical_evidence"
    )
    if logical == "terminal_proven" and name != "idle":
        assert result.classification == "unsupported_physical_evidence"
        assert "trusted_physical_producer_not_implemented" in result.reasons
    assert not any(
        (
            result.live_send_permitted,
            result.reservation_permitted,
            result.writer_permitted,
            result.recovery_permitted,
            result.stop_clear_permitted,
        )
    )
    if name == "reviewed-unknown":
        inventory = st._load(result.evidence)["inventory"][0]["projection"]
        assert inventory["unresolved_acquisition_charge"] > 0
        assert inventory["retained_body_bytes"] > 0
    if name == "pre-send-reclaim":
        assert s.p.projection.attempts[0].state == "reserved"
        assert c2.reconcile_live_journals(s.a, (s.p,)).classification == "consistent"


def test_synthetic_proofs_never_grant_production_admission():
    s = state("terminal")
    assert s.j.object(s.oid).stable and s.j.object(s.oid).receipt is not None
    r = terminal._assess_journals(s.a, (s.p,), (s.j,))
    assert r.logical_status == "terminal_proven"
    assert {d.kind for d in r.physical_dependencies} == {
        "transport",
        "root_inventory",
        "writer_stability",
    }
    assert not r.clean_eligible
    assert list(inspect.signature(terminal.assess_terminal_state).parameters) == [
        "session"
    ]
    with pytest.raises(TypeError):
        terminal.assess_terminal_state(None, physical_verified=True)


def test_terminal_response_cannot_hide_missing_body_history():
    s = state("terminal")
    empty = c3.LiveBodyJournal(s.c, s.root)
    r = terminal._assess_journals(s.a, (s.p,), (empty,))
    assert r.logical_status == "incomplete"
    assert "sent_attempt_body_evidence_missing" in r.reasons
    assert r.physical_status == "unsupported_physical_evidence"


def test_canonical_assessment_is_independent_of_input_order():
    s = state("terminal")
    a = terminal._assess_journals(s.a, (s.p,), (s.j,))
    b = terminal._assess_journals(
        c2.LiveJournal(s.a.schema, s.a.records),
        (c2.LiveJournal(s.p.schema, s.p.records),),
        (c3.LiveBodyJournal(s.j.contract, s.j.root, s.j.records),),
    )
    assert a.canonical == b.canonical
    assert st._load(a.canonical)["physical_dependencies"]


def test_corrupt_historical_release_is_rejected_on_assessment():
    s = state("terminal")
    raw = st._load(s.a.records[-1].encode())
    raw["event"]["data"]["expected_hold_sha256"] = "a" * 64
    # Corruption test deliberately bypasses the immutable value constructor.
    damaged = object.__new__(c2.LiveJournal)
    object.__setattr__(damaged, "schema", s.a.schema)
    object.__setattr__(
        damaged, "records", (*s.a.records[:-1], st.canonical_bytes(raw).decode())
    )
    r = terminal._assess_journals(damaged, (s.p,), (s.j,))
    assert r.classification == "inconsistent" and not r.clean_eligible


def test_pre_send_reclaim_without_body_writer_is_only_logical_candidate():
    s = state("pre-send-reclaim")
    # No body operation happened in this distinct artificial timeline.
    body = c3.LiveBodyJournal(s.c, s.root)
    r = terminal._assess_journals(s.a, (s.p,), (body,))
    assert r.logical_status == "terminal_proven"
    assert r.classification == "unsupported_physical_evidence"
    assert any(d.kind == "review" for d in r.physical_dependencies)


def test_released_rule_hold_does_not_hide_absent_operation_receipt(session, prepared):
    setup_enrolled(session, prepared)
    a = next(
        j
        for j in tx._journal_values(session._resources.connection)
        if j.kind == "account"
    )
    at = i1b.NOW + timedelta(milliseconds=20)
    h = c2.LiveHold(
        "rule",
        "synthetic",
        ("synthetic",),
        at,
        "rule_based",
        False,
        a.value.projection.scopes[0].policy_sha,
        at,
    )
    av = a.value.append(
        "hold_entered", recorded_at=at, transition_id="rule-enter", hold=h.to_dict()
    )
    av = av.append(
        "hold_released",
        recorded_at=at,
        transition_id="rule-release",
        hold_id=h.hold_id,
        expected_hold_sha256=h.hold_version_sha256,
        clock_reference="synthetic-clock",
        account_state_reference="synthetic-state",
        manual_review_reference=None,
        repair_reference=None,
    )
    i1b._append_evidence(session, a, av)
    r = terminal.assess_terminal_state(session)
    assert r.classification == "incomplete"
    assert "event_operation_receipt_coverage_missing" in r.reasons
    before = i1b.snapshot(prepared)
    with pytest.raises(HttpContractError, match="quiescence_unproven"):
        session.close()
    assert i1b.snapshot(prepared) == before


@pytest.mark.parametrize(
    "case", ["stale-version", "missing-manual", "early", "missing-repair"]
)
def test_hold_release_replayed_at_historical_time(case):
    p, a, b = evidence_fixture.settled(outcome="unknown")
    if case == "missing-repair":
        h = c2.LiveHold(
            "repair",
            "ledger_inconsistency",
            ("broken",),
            evidence_fixture.t(606),
            "manual",
            False,
            a.projection.scopes[0].policy_sha,
            evidence_fixture.t(7),
        )
        a = a.append(
            "hold_entered",
            recorded_at=evidence_fixture.t(7),
            transition_id="repair-hold",
            hold=h.to_dict(),
        )
    else:
        h = a.projection.holds[0].hold
    args = dict(at=evidence_fixture.t(606), manual="review", hold_id=h.hold_id)
    if case == "stale-version":
        args["expected_hash"] = "a" * 64
    elif case == "missing-manual":
        args["manual"] = None
    elif case == "early":
        args["at"] = evidence_fixture.t(605)
    with pytest.raises(HttpContractError):
        evidence_fixture.release(a, **args)
    good = evidence_fixture.release(
        a,
        at=evidence_fixture.t(606),
        manual="review",
        repair="repair",
        hold_id=h.hold_id,
    )
    assert (
        c2.LiveJournal(good.schema, good.records).projection.holds[-1].released_at
        is not None
    )


def test_missing_hold_and_stale_generation_are_not_terminal():
    s = state("terminal")
    # A valid earlier prefix drops hold/release evidence without altering C2 outcome.
    dropped = c2.LiveJournal(s.a.schema, s.a.records[:8])
    result = terminal._assess_journals(dropped, (s.p,), (s.j,))
    assert not result.clean_eligible
    with pytest.raises(HttpContractError):
        s.a.append(
            "slot_sent",
            recorded_at=s.tick(),
            transition_id="stale",
            binding=replace(s.b, generation=2),
            sent_at=body_fixture.stamp(body_fixture.NOW),
        )


@pytest.mark.parametrize(
    "case", ["missing-body", "duplicate-body", "pending-pair", "unknown-body-event"]
)
def test_inventory_and_pending_are_not_permission(case):
    s = state("reserved")
    bodies = (s.j,)
    if case == "missing-body":
        bodies = ()
    elif case == "duplicate-body":
        bodies = (s.j, s.j)
    elif case == "pending-pair":
        s.p = c2.LiveJournal(s.p.schema, s.p.records[:1])
    else:
        with pytest.raises(HttpContractError):
            s.j.append(
                c3.BodyEvent("unsupported", "future-event", s.tick(), None, "{}", None)
            )
        return
    r = terminal._assess_journals(s.a, (s.p,), bodies)
    assert r.classification in {"incomplete", "inconsistent"}
    assert not r.clean_eligible


def setup_enrolled(session, prepared):
    en.open_enrollment_account(session, "open")
    req = i1b.request(prepared)
    result = en.enroll_plan(session, "a", req)
    return req, result


def add_audit(session):
    journal = next(
        j for j in tx._journal_values(session._resources.connection) if j.kind == "body"
    )
    value = journal.value.audit(
        evidence_ref="synthetic-extra",
        at=i1b.NOW + timedelta(milliseconds=20),
        operation_id="extra-audit",
    )
    i1b._append_evidence(session, journal, value)


def test_read_only_and_v3_preservation(session, prepared):
    req, original = setup_enrolled(session, prepared)
    before = i1b.snapshot(prepared)
    head = session._head
    result = terminal.assess_terminal_state(session)
    assert (
        result.classification == "terminal_proven"
        and result.physical_status == "not_required"
    )
    assert before == i1b.snapshot(prepared) and session._head == head
    c, _, _, p = session._begin()
    legacy = tx._clean_snapshot(c, session.identity, p)
    selected = terminal._select_clean_snapshot(c, session.identity, p)
    assert st.canonical_bytes(legacy) == st.canonical_bytes(selected)
    assert legacy["schema"].endswith("v3")
    snap = terminal._snapshot(
        c, session.identity, p, terminal._assess_db(c, session.identity, p)
    )
    assert snap["schema"] == terminal.SNAPSHOT
    assert snap["quiescence_profile"] == terminal.PROFILE
    assert snap["terminal_assessment"]["live_send_permitted"] is False
    assert (
        snap["physical_sha"] == p.physical_sha
        and snap["owner_pin_sha"] == p.owner_pin_sha
    )
    assert snap == terminal._snapshot(
        c, session.identity, p, terminal._assess_db(c, session.identity, p)
    )
    c.rollback()
    session.close()
    again = op.open_store(prepared)
    assert en.enroll_plan(again, "a", req) == original
    again.close()


def test_new_profile_rejects_missing_physical_without_clean_marker(session, prepared):
    req, receipt = setup_enrolled(session, prepared)
    add_audit(session)
    before = i1b.snapshot(prepared)
    result = terminal.assess_terminal_state(session)
    assert result.logical_status == "incomplete"
    assert result.classification == "incomplete"
    assert result.physical_status == "unsupported_physical_evidence"
    assert "event_operation_receipt_coverage_missing" in result.reasons
    assert i1b.snapshot(prepared) == before
    with pytest.raises(HttpContractError, match="quiescence_unproven:incomplete"):
        session.close()
    assert session.status == "invalid" and i1b.snapshot(prepared) == before
    assert (
        tx.read_committed_receipt(
            prepared, "a", req.intent(prepared.intent.account_ref)
        )
        == receipt
    )
    with pytest.raises(HttpContractError, match="restart_uncertain"):
        op.open_store(prepared)


def test_prior_read_only_success_is_not_close_authority(session, prepared):
    setup_enrolled(session, prepared)
    earlier = terminal.assess_terminal_state(session)
    assert earlier.clean_eligible
    add_audit(session)
    before = i1b.snapshot(prepared)
    with pytest.raises(HttpContractError, match="quiescence_unproven"):
        session.close()
    assert i1b.snapshot(prepared) == before


def test_complete_evidence_is_bound_to_all_enrolled_plans(session, prepared):
    setup_enrolled(session, prepared)
    en.enroll_plan(session, "b", i1b.request(prepared, "plan-b"))
    r = terminal.assess_terminal_state(session)
    evidence = st._load(r.evidence)
    assert r.clean_eligible and len(evidence["plan_heads"]) == 2
    assert len(evidence["inventory"]) == 2
    journals = tx._journal_values(session._resources.connection)
    a = next(j.value for j in journals if j.kind == "account")
    plans = tuple(j.value for j in journals if j.kind == "plan")
    bodies = tuple(j.value for j in journals if j.kind == "body")
    assert (
        terminal._assess_journals(a, plans, bodies).canonical
        == terminal._assess_journals(a, plans[::-1], bodies[::-1]).canonical
    )
    assert not terminal._assess_journals(a, plans, bodies[:1]).clean_eligible
    assert not terminal._assess_journals(a, plans[:1], bodies).clean_eligible
    session.close()


@pytest.mark.parametrize(
    "stage",
    [
        "terminal_validation",
        "hold_validation",
        "inventory_validation",
        "snapshot_build",
        "before_session_closed",
        "before_close_commit",
    ],
)
def test_close_failure_has_no_partial_marker(session, prepared, monkeypatch, stage):
    setup_enrolled(session, prepared)
    if stage in {"terminal_validation", "hold_validation", "inventory_validation"}:
        add_audit(session)
    if stage == "snapshot_build":
        # Test-only route exercises the new v4 snapshot on eligible idle evidence;
        # it does NOT simulate a physical producer or admit HTTP history.
        monkeypatch.setattr(terminal, "_requires_terminal_profile", lambda c: True)
    before = i1b.snapshot(prepared)

    def fail(name):
        if name == stage:
            raise RuntimeError("artificial-close-failure")

    monkeypatch.setattr(terminal, "_stage", fail)
    monkeypatch.setattr(op, "_stage", fail)
    with pytest.raises(RuntimeError, match="artificial-close-failure"):
        session.close()
    assert session.status == "invalid" and i1b.snapshot(prepared) == before


@pytest.mark.parametrize("damage", ["pending", "receipt", "head", "fence", "stop"])
def test_authoritative_store_diagnostics_never_clean(
    session, prepared, monkeypatch, damage
):
    setup_enrolled(session, prepared)
    if damage == "pending":
        c = session._resources.connection
        c.execute("BEGIN IMMEDIATE")
        row = dict(tx._rows(c, "operations")[0])
        intent = st.OperationIntent(
            "synthetic-future-operation", session.identity.account_ref, None, b"{}"
        )
        row.update(
            operation_id="pending",
            kind=intent.kind,
            canonical_intent=intent.canonical,
            intent_sha=intent.sha256,
            status="pending",
            receipt_sha=None,
            committed_at=None,
            execution_preconditions=b"{}",
        )
        tx._insert(c, "operations", **row)
        c.commit()
        r = terminal.assess_terminal_state(session)
        assert r.classification == "incomplete" and "pending_operation" in r.reasons
    elif damage == "fence":
        session._identity = replace(
            session.identity, fence_epoch=session.identity.fence_epoch + 1
        )
        with pytest.raises(HttpContractError, match="fence"):
            terminal.assess_terminal_state(session)
    elif damage == "stop":
        monkeypatch.setattr(
            op,
            "_capture_clock",
            lambda: rt.ClockObservation(
                (i1b.NOW - timedelta(days=1)).isoformat(timespec="microseconds"), 0
            ),
        )
        with pytest.raises(HttpContractError, match="clock"):
            session.checkpoint_clock()
        with closing(sqlite3.connect(prepared.intent.db_path)) as c:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA recursive_triggers=ON")
            p = rt._validate_runtime_contents(c)
            assert (
                terminal._assess_db(c, session.identity, p).classification
                == "stop_blocked"
            )
    else:
        table, sql = (
            ("receipts", "UPDATE receipts SET canonical=x'7b7d' WHERE operation_id='a'")
            if damage == "receipt"
            else (
                "journal_heads",
                "UPDATE journal_heads SET head_sha='" + "0" * 64 + "'",
            )
        )
        i1b._corrupt(prepared, table, sql)
        with pytest.raises(HttpContractError):
            terminal.assess_terminal_state(session)
    with pytest.raises(HttpContractError):
        session.close()


def _close_worker(a, stage, pipe):
    op._capture_clock = old.Clock(1)
    s = op.open_store(a)

    def crash(name):
        if name == stage:
            pipe.send(name)
            pipe.recv()
            os._exit(43)

    op._stage = crash
    s.close()


@pytest.mark.parametrize("stage", ["before_close_commit", "session_closed_committed"])
def test_spawn_close_crash_and_lock(prepared, monkeypatch, stage):
    s = op.open_store(prepared)
    req, original = setup_enrolled(s, prepared)
    s.close()
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    worker = ctx.Process(target=_close_worker, args=(prepared, stage, child))
    worker.start()
    child.close()
    try:
        assert parent.poll(30) and parent.recv() == stage
        with pytest.raises(HttpContractError, match="lock"):
            op.open_store(prepared)
        parent.send("crash")
        worker.join(30)
        assert worker.exitcode == 43
    finally:
        parent.close()
        if worker.is_alive():
            worker.terminate()
            worker.join()
    monkeypatch.setattr(op, "_capture_clock", old.Clock(2))
    before = i1b.snapshot(prepared)
    assert (
        tx.read_committed_receipt(
            prepared, "a", req.intent(prepared.intent.account_ref)
        )
        == original
    )
    assert i1b.snapshot(prepared) == before
    if stage == "before_close_commit":
        with pytest.raises(HttpContractError, match="restart_uncertain"):
            op.open_store(prepared)
    else:
        op.open_store(prepared).close()


def test_no_permission_or_schema_mutation(session, prepared):
    setup_enrolled(session, prepared)
    result = terminal.assess_terminal_state(session)
    with pytest.raises(TypeError):
        terminal.TerminalAssessment(
            "terminal_proven",
            "terminal_proven",
            "not_required",
            (),
            b"{}",
            (),
            live_send_permitted=True,
        )
    assert st._load(result.canonical)["schema"] == terminal.ASSESSMENT
    assert (
        rt.validate_store_v2_schema(session._resources.connection) == rt.STORE_SCHEMA_V2
    )
    session.close()
    assert (
        len(
            st._load(
                tx.read_committed_receipt(
                    prepared,
                    "a",
                    i1b.request(prepared).intent(prepared.intent.account_ref),
                ).receipt
            )
        )
        == 6
    )


@pytest.mark.parametrize(
    "axis,extra",
    [
        (a, n)
        for a in (
            "plans",
            "operations",
            "receipts",
            "c2-events",
            "body-events",
            "runtime-events",
        )
        for n in (0, 2)
    ],
)
def test_validation_cost_axes(session, prepared, axis, extra, capsys):
    """Full validation, three repeats; not an SLA or a production cache.

    Receipt count is inseparable from committed operation count. Its scale uses
    account opening plus A vs account opening plus A/B, reporting all coupled
    dimensions. Operations alone grow with pending rows (negative workload).
    Plans alone grow prepared catalog rows, not a fabricated enrolled universe.
    """
    setup_enrolled(session, prepared)
    c = session._resources.connection
    if axis == "runtime-events":
        for _ in range(extra):
            session.checkpoint_clock()
    elif axis == "receipts" and extra:
        en.enroll_plan(session, "b", i1b.request(prepared, "plan-b"))
    elif axis == "plans":
        c.execute("BEGIN IMMEDIATE")
        template = dict(tx._rows(c, "plans")[0])
        for index in range(extra):
            req = i1b.request(prepared, f"prepared-{index}")
            for source in (req.plan, req.approval):
                tx._register_document(
                    c,
                    session.identity.deployment_id,
                    tx._Document(
                        source.kind,
                        source.content,
                        source.source_identity,
                        source.source_reference,
                    ),
                    rt._validate_runtime_contents(c).last_accepted_utc,
                )
            row = dict(template)
            row.update(
                plan_sha=req.plan.sha256,
                approval_sha=req.approval.sha256,
                journal_id=None,
                status="prepared",
                enrolled_revision=None,
            )
            tx._insert(c, "plans", **row)
        c.commit()
    elif axis == "operations":
        c.execute("BEGIN IMMEDIATE")
        for index in range(extra):
            intent = st.OperationIntent(
                "synthetic-pending", session.identity.account_ref, None, b"{}"
            )
            row = dict(tx._rows(c, "operations")[0])
            row.update(
                operation_id=f"pending-{index}",
                kind=intent.kind,
                canonical_intent=intent.canonical,
                intent_sha=intent.sha256,
                status="pending",
                receipt_sha=None,
                committed_at=None,
                execution_preconditions=b"{}",
            )
            tx._insert(c, "operations", **row)
        c.commit()
    elif axis in {"c2-events", "body-events"} and extra:
        journal = next(
            j
            for j in tx._journal_values(c)
            if j.kind == ("account" if axis == "c2-events" else "body")
        )
        value = journal.value
        for index in range(extra):
            at = i1b.NOW + timedelta(milliseconds=20 + index)
            if axis == "body-events":
                value = value.audit(
                    evidence_ref="synthetic", at=at, operation_id=f"audit-{index}"
                )
            else:
                scope = value.projection.scopes[0]
                hold = c2.LiveHold(
                    f"extra-{index}",
                    "synthetic",
                    ("synthetic",),
                    at,
                    "rule_based",
                    False,
                    scope.policy_sha,
                    at,
                )
                value = value.append(
                    "hold_entered",
                    recorded_at=at,
                    transition_id=f"hold-{index}",
                    hold=hold.to_dict(),
                )
                value = value.append(
                    "hold_released",
                    recorded_at=at,
                    transition_id=f"release-{index}",
                    hold_id=hold.hold_id,
                    expected_hold_sha256=hold.hold_version_sha256,
                    clock_reference="synthetic-clock",
                    account_state_reference="synthetic-state",
                    manual_review_reference=None,
                    repair_reference=None,
                )
        i1b._append_evidence(session, journal, value)
    counts = {
        t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("operations", "plans", "events", "runtime_events", "receipts")
    }
    counts["body_events"] = c.execute(
        "SELECT count(*) FROM events JOIN journals USING(journal_id) WHERE journals.kind='body'"
    ).fetchone()[0]
    p = rt._validate_runtime_contents(c)
    journals = tx._journal_values(c)
    account = next(j.value for j in journals if j.kind == "account")
    plans = tuple(j.value for j in journals if j.kind == "plan")
    bodies = tuple(j.value for j in journals if j.kind == "body")
    committed = [r for r in tx._rows(c, "operations") if r["status"] == "committed"]

    def manifests():
        for row in committed:
            en._validate_manifest(c, row, st._load(row["execution_preconditions"]))

    def receipts():
        for row in committed:
            tx._receipt(c, row, p)

    logical = terminal._assess_journals(account, plans, bodies)
    actions = {
        "store_global": lambda: rt.validate_store_v2_contents(c),
        "runtime_replay": lambda: rt._validate_runtime_contents(c),
        "i1b_manifest": manifests,
        "prefix_replay": lambda: tx._journal_values(c),
        "receipts": receipts,
        "c2_reconciliation": lambda: c2.reconcile_live_journals(account, plans),
        "c3_reconciliation": lambda: c3.reconcile_live_bodies(
            bodies[0], account=account, plans=plans, related_bodies=bodies[1:]
        ),
        "terminal": lambda: terminal._assess_db(c, session.identity, p),
        "snapshot_canonicalization": lambda: st.canonical_bytes(
            terminal._snapshot(c, session.identity, p, logical)
        ),
    }
    measurements = {}
    for name, action in actions.items():
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            action()
            samples.append(time.perf_counter() - start)
        measurements[name] = samples
    start = time.perf_counter()
    try:
        session.close()
        close_result = "closed"
    except HttpContractError as exc:
        close_result = str(exc)
    measurements["close_total"] = [time.perf_counter() - start]
    if axis in {"operations", "body-events", "c2-events"} and extra:
        assert close_result != "closed"
    with capsys.disabled():
        print(
            "I2-0 artificial timings",
            {
                "axis": axis,
                "variant": extra,
                "counts": counts,
                "repeats": 3,
                "close_repeats": 1,
                "seconds": measurements,
                "close": close_result,
            },
        )
