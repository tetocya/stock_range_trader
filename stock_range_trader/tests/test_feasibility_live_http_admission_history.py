"""Artificial operation originals, not Store committed reservation receipts.

Positive C2/C3 histories use their existing append APIs. Negative operation
envelopes intentionally fabricate impossible commits or admission decisions.
"""

import builtins
import hashlib
import json
import socket
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest
from test_feasibility_live_body_contract import NOW, stamp
from test_feasibility_live_body_enrollment_compatibility import EnrolledScenario
from test_feasibility_live_store_i1a2a import physical

from feasibility import http_contract as hc
from feasibility import live_body_contract as c3
from feasibility import live_http_admission_history as h
from feasibility import live_http_budget as v1
from feasibility import live_http_budget_v2 as v2
from feasibility import live_http_evidence as c2
from feasibility import live_store as store
from feasibility import live_store_runtime as rt

LIMITS = h.ValidationLimitsV1(1500, 4_000_000, 24, 4, 5000, 32768)
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


class History(EnrolledScenario):
    def __init__(self, *, account_capacity=100000, second_plan=False):
        super().__init__()
        if second_plan:
            self.enroll_b(same_time=True)
        self.policy = store.StorePolicy(4, 32768, account_capacity, account_capacity)
        pre = self.src["preflight"]
        runtime_policy = rt.RuntimePolicy()
        deployment = store.DeploymentRecord(
            "12345678-1234-4234-8234-123456789abc",
            "artificial-deployment",
            pre.identity.account_ref,
            pre.registry.sha256,
            pre.sha256,
            self.policy.sha256,
            stamp(NOW),
        )
        owner = rt.OwnerDeploymentRecord(
            deployment.deployment_id,
            deployment.account_ref,
            pre.registry.sha256,
            pre.sha256,
            pre.identity.sha256,
            self.policy.sha256,
            runtime_policy.sha256,
            pre.registry.canonical_store_root,
            pre.canonical_ledger_path,
            pre.canonical_ledger_path.rsplit("/", 1)[0] + "/" + rt.LOCK_FILENAME,
            "bootstrap",
            "owner-decision",
            "approved",
        )
        self.context = rt.RuntimeContext(deployment, runtime_policy, owner)
        self.runtime = ()
        self.runtime_append(
            "bootstrap_created",
            payload={"intent_sha": owner.sha256, "physical": physical().to_dict()},
            session=None,
            fence=0,
        )
        pin = replace(
            owner,
            state="pinned",
            store_uuid=deployment.store_id,
            physical=physical(),
            bootstrap_evidence_sha=rt.replay_runtime(
                self.context, self.runtime
            ).head_sha,
            owner_pin_reference="pin",
        )
        self.context = replace(self.context, owner_pins=(pin,))
        self.runtime_append(
            "bootstrap_finalized",
            payload={
                "owner_pin_sha": pin.sha256,
                "owner_pin_reference": pin.owner_pin_reference,
                "bootstrap_head": pin.bootstrap_evidence_sha,
                "lifecycle": "prepared",
            },
            session=None,
            fence=0,
        )
        self.runtime_append("session_started", payload={"owner_pin_sha": pin.sha256})
        self.clock()
        self.ops, self.reservations, self.measurements, self.avail = [], [], [], []
        self.initial = self.cut()

    def switch_plan(self):
        self.p, self.pb = self.pb, self.p
        self.j, self.jb = self.jb, self.j
        self.src, self.src_b = self.src_b, self.src
        self.c, self.root = self.j.contract, self.j.root
        self.scope = self.p.projection.scopes[0]

    def acquired(self, size=60):
        self.j = self.j.acquired(
            self.oid,
            size=size,
            evidence_ref="complete-body-claim",
            at=self.tick(),
            operation_id=f"acquired-{self.index}",
            account=self.a,
            plans=self.plans,
        )
        return self

    def runtime_append(self, kind, *, payload=None, session="session", fence=1):
        self.runtime = rt.append_runtime_event(
            self.context,
            self.runtime,
            kind=kind,
            observation=rt.ClockObservation(
                stamp(NOW + timedelta(seconds=self.n)), self.n * 1_000_000_000
            ),
            transition_id=f"runtime-{len(self.runtime)}",
            session_id=session,
            fence_epoch=fence,
            payload=payload or {},
        )

    def clock(self):
        self.tick()
        self.runtime_append("clock_observed")

    def journals(self):
        js = [
            h.JournalOriginalV1(
                "account", "account", None, self.a.schema, self.a.records
            )
        ]
        for p, b in ((self.p, self.j), *(((self.pb, self.jb),) if self.pb else ())):
            sha = b.contract.plan.plan_sha
            js += [
                h.JournalOriginalV1(
                    "body-" + sha, "body", sha, c3.EVENT_SCHEMA, b.records
                ),
                h.JournalOriginalV1("plan-" + sha, "plan", sha, p.schema, p.records),
            ]
        return tuple(sorted(js, key=lambda j: j.journal_id))

    def cut(self):
        e = rt.RuntimeEvent.from_bytes(self.runtime[-1])
        return h.HistoricalCutV1(
            tuple(
                h.HeadReferenceV1(
                    j.journal_id,
                    len(j.records),
                    json.loads(j.records[-1])["event_hash"]
                    if j.records
                    else c3.ZERO_HASH,
                )
                for j in self.journals()
            ),
            e.sequence,
            e.event_hash,
            self.policy.sha256,
        )

    def pool(self):
        plans = [
            h.PlanOriginalV1(
                self.c,
                self.root,
                self.src["canonical_approval"],
                self.src["canonical_preflight"],
                "enrolled",
            )
        ]
        if self.pb:
            plans.append(
                h.PlanOriginalV1(
                    self.jb.contract,
                    self.jb.root,
                    self.src_b["canonical_approval"],
                    self.src_b["canonical_preflight"],
                    "stopped",
                )
            )
        return h.SourcePoolV1(
            v1.Identity(
                self.context.deployment.deployment_id,
                self.context.deployment.store_id,
                self.scope.account_ref,
            ),
            self.policy.canonical.decode(),
            self.context,
            self.runtime,
            tuple(sorted(plans, key=lambda p: p.contract.plan.plan_sha)),
            self.journals(),
            self.initial,
            tuple(self.ops),
            tuple(self.reservations),
            tuple(self.measurements),
            tuple(self.avail),
        )

    def finish(self, source, kind, op_id, core=None, measurement=None):
        self.clock()
        result = self.cut()
        old = {r.journal_id: r.count for r in source.heads}
        parts = []
        new_rows = {}
        for j in self.journals():
            for raw in j.records[old.get(j.journal_id, 0) :]:
                r = json.loads(raw)
                new_rows[(j.journal_id, r["sequence"])] = r
        if kind == h.RESERVATION_KIND:
            ids = (
                "account",
                "plan-" + self.c.plan.plan_sha,
                "body-" + self.c.plan.plan_sha,
            )
            roles = ("account-reservation", "plan-reservation", "body-reservation")
            ordered = [
                (jid, old[jid], role) for jid, role in zip(ids, roles, strict=True)
            ]
        else:
            ordered = [
                (jid, seq, f"evidence-{i}")
                for i, (jid, seq) in enumerate(sorted(new_rows))
            ]
        for i, (jid, seq, role) in enumerate(ordered):
            r = new_rows[jid, seq]
            parts.append(
                h.OperationPartV1(role, i, jid, seq, r["event_hash"], r["event_hash"])
            )
        measurements = [] if measurement is None else [measurement.sha256]
        intent = c3._canonical(
            {
                "schema": h.PREFIX + "intent-v1",
                "kind": kind,
                "account_ref": self.scope.account_ref,
                "plan_sha": self.c.plan.plan_sha,
                "measurement_shas": measurements,
                "core_sha": None if core is None else core.sha256,
            }
        )
        pre = c3._canonical(
            {
                "schema": h.PREFIX + "preconditions-v1",
                "source": source.to_dict(),
                "session_id": "session",
                "fence_epoch": 1,
                "measurement_shas": measurements,
                "core_sha": None if core is None else core.sha256,
            }
        )
        receipt = c3._canonical(
            {
                "schema": store.RECEIPT_SCHEMA,
                "deployment_id": self.context.deployment.deployment_id,
                "operation_id": op_id,
                "intent_sha": h.digest(intent.encode()),
                "committed_at": stamp(NOW + timedelta(seconds=self.n)),
                "parts": [
                    {k: v for k, v in p.to_dict().items() if k != "schema"}
                    for p in parts
                ],
            }
        )
        op = h.OperationOriginalV1(
            op_id,
            kind,
            self.c.plan.plan_sha,
            "session",
            1,
            intent,
            pre,
            source,
            result,
            tuple(parts),
            receipt,
        )
        self.ops.append(op)
        if core is not None:
            self.reservations.append(
                h.HistoricalReservationV1(
                    op.sha256,
                    core.to_bytes().decode(),
                    h.derive_slot_id_v2(core, op_id),
                )
            )
        if measurement is not None:
            audit = json.loads(self.j.records[-1])
            self.avail.append(
                h.MeasurementAvailabilityV1(
                    measurement.sha256, op.sha256, audit["event_hash"]
                )
            )
        return op

    def release(self):
        holds = [x for x in self.a.projection.holds if x.released_at is None]
        if not holds:
            return
        self.n = max(
            self.n,
            *(
                int((x.hold.not_before - NOW).total_seconds())
                for x in holds
                if x.hold.not_before
            ),
        )
        self.clock()
        before = self.cut()
        for x in holds:
            self.a = self.a.append(
                "hold_released",
                recorded_at=self.tick(),
                transition_id=f"release-{self.n}",
                hold_id=x.hold.hold_id,
                expected_hold_sha256=x.hold.hold_version_sha256,
                clock_reference="clock",
                account_state_reference="state",
                manual_review_reference="review"
                if x.hold.release_mode == "manual"
                else None,
                repair_reference=None,
            )
        self.finish(before, h.EVIDENCE_KIND, "release-" + str(self.n))

    def begin(self, page_index=0, cursor=None, *, illegal=False):
        self.release()
        self.clock()
        before = self.cut()
        page = c3.LivePageIdentity(
            self.c.plan.plan_sha,
            c3._canonical(self.c.plan.content["scope"]["queries"][0]),
            page_index,
            cursor,
        )
        pool = self.pool()
        if illegal:
            # Deliberately illegal history: the private diagnostic constructs the
            # claimed core, while the public historical verifier must reject it.
            index = h._Index(pool, True, h.ReplayStatistics())
            prior = h._chain_start()
            for op in pool.operations:
                prior = h._chain_next(prior, op)
            available = pool.measurements
            allowances = {}
            for record in pool.reservations:
                old_core = h.AdmissionCoreV2.from_bytes(record.core_json.encode())
                for obj in self.j.projection.objects:
                    if obj.identity.binding.slot_id == record.slot_id:
                        allowances[obj.identity.binding] = old_core.allowance
            core = h._core(pool, index, before, page, available, allowances, prior)[0]
        else:
            core = h.build_admission_v2(pool, limits=LIMITS, cut=before, page=page)
        self.index += 1
        op_id = f"reserve-{self.index}"
        plan = self.src["plan"]
        self.b = c2.AttemptBinding(
            plan.sha256,
            self.c.preflight_sha,
            self.scope.account_ref,
            self.scope.approval_sha,
            hc.make_attempt_id(
                plan,
                hc.PageRequest(plan.queries[0], page_index, cursor),
                core.attempt_number,
            ),
            h.derive_slot_id_v2(core, op_id),
            "holder",
            self.a.projection.generation + 1,
        )
        at = NOW + timedelta(seconds=self.n)
        self.p = self.p.append(
            "attempt_reserved",
            recorded_at=at,
            transition_id=op_id,
            binding=self.b,
            reserved_at=stamp(at),
        )
        self.a = self.a.append(
            "slot_reserved",
            recorded_at=at,
            transition_id=op_id,
            binding=self.b,
            reserved_at=stamp(at),
        )
        self.identity = c3.ObjectIdentity(self.b, page)
        self.oid = self.identity.object_id
        self.j = self.j.reserve(
            self.identity,
            at=at,
            operation_id=op_id,
            account=self.a,
            plans=self.plans,
            related_bodies=self.related,
        )
        self.finish(before, h.RESERVATION_KIND, op_id, core=core)
        return self

    def settle(
        self,
        *,
        status=503,
        size=0,
        digest=EMPTY_DIGEST,
        outcome="response",
        transfer=0,
        decoded=None,
        partial=None,
        early_quarantine=False,
        next_cursor=None,
    ):
        self.clock()
        before = self.cut()
        self.opened().observed(
            size=size, digest=digest, state="staging" if status == 200 else "partial"
        ).received(status=status, outcome=outcome)
        if early_quarantine:
            self.j = self.j.quarantine_object(self.quarantine_evidence())
        self.closed().stabilized()
        if outcome == "response":
            self.acquired(size)
        if status == 200 and outcome == "response":
            self.committed(next_cursor)
        elif not early_quarantine:
            self.j = self.j.quarantine_object(self.quarantine_evidence())
        m = v1.MeasurementOriginal(
            self.pool().identity,
            self.b,
            self.identity.page,
            sum(
                x.binding.plan_sha == self.b.plan_sha and x.page == self.identity.page
                for x in self.measurements
            )
            + 1,
            "complete" if outcome == "response" else "partial",
            transfer if outcome == "response" else partial,
            (size if decoded is None else decoded)
            if outcome == "response"
            else partial,
            size,
            size if status == 200 and outcome == "response" else None,
            digest if status == 200 and outcome == "response" else None,
            self.tick(),
            "artificial",
            "artificial-runtime",
            self.a.head_sha,
            self.p.head_sha,
            self.j.head_sha,
        )
        self.measurements.append(m)
        self.j = self.j.audit(
            evidence_ref=m.sha256, at=self.tick(), operation_id=f"audit-{self.index}"
        )
        self.finish(before, h.SETTLEMENT_KIND, f"settle-{self.index}", measurement=m)
        return self


def verified(f):
    pool = f.pool()
    a = h.verify_history_reference(pool, limits=LIMITS)
    b = h.verify_history_incremental(pool, limits=LIMITS)
    assert a.to_bytes() == b.to_bytes()
    return a


def test_initial_and_open_final_reservation_are_valid_but_new_admission_is_blocked():
    f = History()
    assert verified(f).historical_admissions_valid
    f.begin()
    r = verified(f)
    assert r.historical_admissions_valid, r.reason
    f.clock()
    with pytest.raises(
        hc.HttpContractError, match="account_open_attempt|open_unsettled"
    ):
        h.build_admission_v2(f.pool(), limits=LIMITS, cut=f.cut(), page=f.identity.page)


def test_B_two_zero_byte_quarantines_and_valid_retry():
    f = History().begin().settle(status=503).begin().settle(status=429).begin()
    r = verified(f)
    assert r.historical_admissions_valid, r.reason
    assert r.checks[-1].assessment.budget.saved.saved_total == 0
    assert r.checks[-1].assessment.budget.attempts == 2


def test_A_404_first_illegal_retry_not_hidden_by_later_status():
    f = (
        History()
        .begin()
        .settle(status=404)
        .begin(illegal=True)
        .settle(status=429)
        .begin(illegal=True)
        .settle(status=200)
    )
    r = verified(f)
    assert not r.historical_admissions_valid
    assert r.first_violation == 1
    assert "previous_attempt_not_retryable" in r.reason
    saved = v2.saved_compatibility_v2(
        f.j, account=f.a, plans=(f.p,), originals=tuple(f.measurements)
    )
    assert saved.compatible


def test_K_quarantine_precedes_final_stability_without_retroactive_writer():
    f = History().begin().settle(early_quarantine=True).begin()
    assert verified(f).historical_admissions_valid
    obj = f.j.projection.objects[0]
    q = next(o.quarantine for o in f.j.projection.objects if o.quarantine)
    assert q.writer.state == "open" and q.digest_class == "provisional"
    assert any(o.stable for o in f.j.projection.objects)
    assert obj is not None


def test_O_incremental_reuses_projections_and_result_is_identical():
    f = History().begin().settle().begin().settle(status=429).begin()
    a, b = h.ReplayStatistics(), h.ReplayStatistics()
    reference = h.verify_history_reference(f.pool(), limits=LIMITS, statistics=a)
    incremental = h.verify_history_incremental(f.pool(), limits=LIMITS, statistics=b)
    assert reference == incremental
    assert reference.historical_admissions_valid, reference.reason
    assert a.full_replays > b.full_replays * 5
    assert b.full_replays == len(f.pool().journals) + 1
    assert b.indexed_events == sum(len(j.records) for j in f.pool().journals)
    assert b.prefix_hits > 0


def test_P_pure_verification_and_build_with_io_trapped(monkeypatch):
    f = History().begin().settle()
    f.release()
    f.clock()
    pool, cut, page = f.pool(), f.cut(), f.identity.page

    def forbidden(*args, **kwargs):
        raise AssertionError("I/O forbidden")

    import os
    from pathlib import Path

    for owner, name in (
        (builtins, "open"),
        (socket, "socket"),
        (sqlite3, "connect"),
        (os, "getenv"),
        (Path, "write_bytes"),
        (Path, "mkdir"),
        (Path, "resolve"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    assert h.verify_history_incremental(pool, limits=LIMITS).historical_admissions_valid
    core = h.build_admission_v2(pool, limits=LIMITS, cut=cut, page=page)
    assert (
        not core.reservation_permitted
        and not core.live_send_permitted
        and not core.producer_authenticated
    )
    assert (
        h.verify_admission_v2(
            pool,
            limits=LIMITS,
            cut=cut,
            page=page,
            claimed_core=core,
            claimed_slot=h.derive_slot_id_v2(core, "next"),
            operation_id="next",
        )
        == core
    )


def rebind_operation(pool, old, new):
    """Negative fixture preserves references to expose the changed contract."""
    return replace(
        pool,
        operations=tuple(new if x == old else x for x in pool.operations),
        reservations=tuple(
            replace(r, operation_sha=new.sha256) if r.operation_sha == old.sha256 else r
            for r in pool.reservations
        ),
        availability=tuple(
            replace(a, operation_sha=new.sha256) if a.operation_sha == old.sha256 else a
            for a in pool.availability
        ),
    )


def rejected(pool, reason):
    reference = h.verify_history_reference(pool, limits=LIMITS)
    actual = h.verify_history_incremental(pool, limits=LIMITS)
    assert reference == actual
    assert not actual.historical_admissions_valid
    assert reason in actual.reason
    return actual


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("fence", "stale_fence_or_session"),
        ("session", "stale_fence_or_session"),
        ("receipt", "original_receipt_mismatch"),
        ("part", "operation_part_binding"),
        ("cut", "prefix_head"),
        ("unknown_kind", "unsupported_recovery_operation"),
        ("clock_only_kind", "reservation_operation_required"),
        ("source_gap", "operation_source_gap"),
    ],
)
def test_N_original_dependency_mutations_reject_at_first_reservation(mutation, reason):
    f = History().begin()
    pool, old = f.pool(), f.ops[0]
    if mutation == "fence":
        new = replace(old, fence_epoch=2)
    elif mutation == "session":
        new = replace(old, session_id="another")
    elif mutation == "receipt":
        raw = json.loads(old.receipt_json)
        raw["intent_sha"] = "f" * 64
        new = replace(old, receipt_json=c3._canonical(raw))
    elif mutation == "part":
        new = replace(
            old, parts=(replace(old.parts[0], event_hash="f" * 64), *old.parts[1:])
        )
    elif mutation == "cut":
        new = replace(
            old,
            result=replace(
                old.result,
                heads=(
                    replace(old.result.heads[0], head_sha="f" * 64),
                    *old.result.heads[1:],
                ),
            ),
        )
    elif mutation == "unknown_kind":
        new = replace(old, kind="artificial-recovery-v1")
    elif mutation == "clock_only_kind":
        raw = json.loads(old.intent_json)
        raw.update(kind=h.EVIDENCE_KIND, core_sha=None)
        pre = json.loads(old.preconditions_json)
        pre["core_sha"] = None
        new = replace(
            old,
            kind=h.EVIDENCE_KIND,
            intent_json=c3._canonical(raw),
            preconditions_json=c3._canonical(pre),
        )
    else:
        new = replace(old, source=replace(old.source, heads=old.result.heads))
    changed = rebind_operation(pool, old, new)
    if mutation == "clock_only_kind":
        changed = replace(changed, reservations=())
    assert rejected(changed, reason).first_violation == 0


def test_E_future_settlement_commit_cannot_be_made_available_by_observed_at():
    f = History().begin().settle().begin()
    pool = f.pool()
    settlement = next(o for o in pool.operations if o.kind == h.SETTLEMENT_KIND)
    reservation = f.ops[-1]
    assert f.measurements[0].observed_at < c2._time(
        rt.RuntimeEvent.from_bytes(
            pool.runtime_records[reservation.source.runtime_sequence]
        ).observation.utc
    )
    future = replace(
        settlement,
        result=replace(
            settlement.result,
            runtime_sequence=reservation.result.runtime_sequence,
            runtime_head=reservation.result.runtime_head,
        ),
    )
    rejected(rebind_operation(pool, settlement, future), "operation_commit_order")


def test_F_same_utc_clock_pair_has_causal_sequence(monkeypatch):
    f = History()
    original = f.finish

    def same_clock(*args, **kwargs):
        clock = f.clock
        f.clock = lambda: f.runtime_append("clock_observed")
        try:
            return original(*args, **kwargs)
        finally:
            f.clock = clock

    monkeypatch.setattr(f, "finish", same_clock)
    f.begin()
    o = f.ops[0]
    start, end = (
        rt.RuntimeEvent.from_bytes(f.runtime[n])
        for n in (o.source.runtime_sequence, o.result.runtime_sequence)
    )
    assert start.observation.utc == end.observation.utc
    assert start.sequence < end.sequence
    assert verified(f).historical_admissions_valid


@pytest.mark.parametrize(
    "remove",
    ["operation", "availability", "measurement", "journal", "plan", "part", "runtime"],
)
def test_N_source_omission_never_becomes_known_zero(remove):
    f = History().begin().settle()
    pool = f.pool()
    if remove == "operation":
        pool = replace(pool, operations=pool.operations[:-1])
    elif remove == "availability":
        pool = replace(pool, availability=())
    elif remove == "measurement":
        pool = replace(pool, measurements=())
    elif remove == "journal":
        pool = replace(pool, journals=pool.journals[:-1])
    elif remove == "plan":
        pool = replace(pool, plans=())
    elif remove == "part":
        old = pool.operations[-1]
        pool = rebind_operation(pool, old, replace(old, parts=old.parts[:-1]))
    else:
        pool = replace(pool, runtime_records=pool.runtime_records[:-1])
    result = h.verify_history_incremental(pool, limits=LIMITS)
    assert not result.historical_admissions_valid


@pytest.mark.parametrize("field", ["measurement_shas", "audit", "receipt"])
def test_L2C6_measurement_requires_business_original_precondition_and_audit(field):
    f = History().begin().settle()
    pool, old = f.pool(), f.ops[-1]
    if field == "audit":
        pool = replace(
            pool,
            availability=(
                replace(pool.availability[0], audit_event_sha=old.parts[0].event_hash),
            ),
        )
        rejected(pool, "measurement_audit_missing")
        return
    if field == "measurement_shas":
        raw = json.loads(old.preconditions_json)
        raw["measurement_shas"] = []
        new = replace(old, preconditions_json=c3._canonical(raw))
        reason = "preconditions_binding"
    else:
        new = replace(old, receipt_json="{}")
        reason = "original_receipt_mismatch"
    rejected(rebind_operation(pool, old, new), reason)


def test_clock_and_stop_only_tail_do_not_supply_missing_commit():
    f = History().begin().settle()
    f.runtime_append(
        "runtime_stop_entered",
        payload={
            "reason": "runtime_integrity_uncertain",
            "clock_reasons": [],
            "evidence_reference": "rollback",
        },
    )
    pool = replace(f.pool(), operations=tuple(f.ops[:-1]))
    rejected(pool, "measurement_operation_missing")


@pytest.mark.parametrize(
    "name",
    [
        "original_records",
        "total_canonical_bytes",
        "reservations",
        "plans",
        "references",
        "single_record_bytes",
    ],
)
def test_limit_exact_and_plus_one(name):
    f = History().begin().settle()
    pool = f.pool()
    measured = h.measure_input_size(pool)
    assert h.verify_history_incremental(
        pool, limits=measured
    ).historical_admissions_valid
    exact = getattr(measured, name)
    if exact == 1:
        with pytest.raises(hc.HttpContractError, match="positive_limits_required"):
            replace(measured, **{name: 0})
    else:
        with pytest.raises(hc.HttpContractError, match="limit_" + name):
            h.verify_history_incremental(
                pool, limits=replace(measured, **{name: exact - 1})
            )


@pytest.mark.parametrize(
    "bad", [True, False, -1, 0, float("nan"), 1.5, "5", None, 2**63]
)
def test_validation_limits_strict_and_no_defaults(bad):
    with pytest.raises(hc.HttpContractError):
        replace(LIMITS, original_records=bad)
    with pytest.raises(TypeError):
        h.ValidationLimitsV1()


def test_no_caller_history_verified_override_and_v1_core_rejected():
    f = History()
    with pytest.raises(TypeError):
        h.build_admission_v2(f.pool(), limits=LIMITS, history_verified=True)
    with pytest.raises(hc.HttpContractError, match="core_v2_required"):
        h.verify_admission_v2(
            f.pool(),
            limits=LIMITS,
            cut=f.cut(),
            page=None,
            claimed_core=v1.DEFAULT_POLICY,
            claimed_slot="a" * 64,
            operation_id="op",
        )


def test_O_count_actual_legacy_replay_calls(monkeypatch):
    f = History().begin().settle().begin().settle(status=429).begin()
    pool = f.pool()
    counts = {"c2": 0, "c3": 0}

    def spy(name, original):
        def call(*args, **kwargs):
            counts[name] += 1
            return original(*args, **kwargs)

        return call

    monkeypatch.setattr(c2, "_replay", spy("c2", c2._replay))
    monkeypatch.setattr(c3, "_replay", spy("c3", c3._replay))
    a = h.verify_history_reference(pool, limits=LIMITS)
    reference = dict(counts)
    counts.update(c2=0, c3=0)
    b = h.verify_history_incremental(pool, limits=LIMITS)
    assert a == b and b.historical_admissions_valid
    assert reference["c2"] > counts["c2"] * 3
    assert reference["c3"] > counts["c3"] * 3


@pytest.mark.parametrize("capacity,allowed", [(160, True), (159, False)])
def test_G_H_full_history_other_stopped_plan_is_in_account_capacity(capacity, allowed):
    f = History(account_capacity=capacity, second_plan=True)
    f.switch_plan()
    f.begin().settle(size=60)
    f.switch_plan()
    f.release()
    f.clock()
    pool = f.pool()
    assert any(p.catalog_state == "stopped" for p in pool.plans)
    assert h.verify_history_incremental(pool, limits=LIMITS).historical_admissions_valid
    page = c3.LivePageIdentity(
        f.c.plan.plan_sha,
        c3._canonical(f.c.plan.content["scope"]["queries"][0]),
        0,
        None,
    )
    if allowed:
        core = h.build_admission_v2(pool, limits=LIMITS, cut=f.cut(), page=page)
        assert core.allowance.transfer > 0
    else:
        with pytest.raises(hc.HttpContractError, match="account_.*capacity"):
            h.build_admission_v2(pool, limits=LIMITS, cut=f.cut(), page=page)
    missing = replace(
        pool, plans=tuple(p for p in pool.plans if p.catalog_state != "stopped")
    )
    rejected(missing, "catalog_coverage")


def test_J_presend_reclaim_charges_without_invented_plan_unknown_and_blocks_admission():
    f = History().begin()
    f.n = 90
    f.clock()
    before = f.cut()
    at = f.tick()
    expiry = f.a.projection.attempts[0].reserved_at + timedelta(
        seconds=f.scope.account_policy.slot_lease_seconds
    )
    f.a = f.a.append(
        "slot_reclaimed",
        recorded_at=at,
        transition_id="reclaim",
        binding=f.b,
        evidence={
            "lease_expired_at": stamp(expiry),
            "observed_at": stamp(at),
            "clock_status": "verified",
            "previous_transport_termination_reference": "artificial-termination",
            "operator_review_reference": "artificial-review",
            "process_restart_detected": True,
        },
    )
    f.sync_hold()
    attempts = f.a.projection.attempts
    assert attempts[0].outcome == "pre_send_reclaim"
    assert f.p.projection.attempts[0].outcome is None
    result = v2.budget_result(
        f.src["plan"], f.j.projection, attempts, f.p.projection.attempts, (), {}
    )
    assert result.unknown_count == 1 and result.charged_transfer == 500
    assert result.charged_decoded == 600 and result.saved.compatible
    assert f.j.projection.unresolved_acquisition_charge == 100
    assert "unsupported_recovery_operation" in result.reasons
    f.finish(before, "artificial-recovery-v1", "reclaim-operation")
    rejected(f.pool(), "unsupported_recovery_operation")
    with pytest.raises(hc.HttpContractError, match="unsupported_recovery_operation"):
        h.build_admission_v2(f.pool(), limits=LIMITS, cut=f.cut(), page=f.identity.page)


@pytest.mark.parametrize(
    "field", ["producer_authenticated", "reservation_permitted", "live_send_permitted"]
)
def test_permission_true_and_unknown_core_version_rejected(field):
    f = History().begin()
    raw = json.loads(f.reservations[0].core_json)
    raw[field] = True
    with pytest.raises(hc.HttpContractError, match="permission_must_be_false"):
        h.AdmissionCoreV2.from_dict(raw)
    raw[field] = False
    raw["schema"] += "-unknown"
    with pytest.raises(hc.HttpContractError, match="schema"):
        h.AdmissionCoreV2.from_dict(raw)


def test_M_real_v1_core_remains_usable_but_not_v2_slot_input():
    from test_feasibility_live_http_budget import IDENTITY, Fixture

    f = Fixture()
    page = c3.LivePageIdentity(
        f.fixed.sha256, c3._canonical(f.fixed.queries[0].to_dict()), 0, None
    )
    core = v1.build_admission(
        f.c.plan,
        identity=IDENTITY,
        page=page,
        start_observation_reference="start",
        **f.sources(),
    )
    assert v1.AdmissionCore.from_bytes(core.to_bytes()) == core
    assert v1.derive_slot_id(core, "old")
    with pytest.raises(hc.HttpContractError, match="core_v2_required"):
        h.derive_slot_id_v2(core, "new")


def test_reservation_and_plan_count_limit_plus_one_with_positive_limit():
    f = History(second_plan=True).begin().settle().begin()
    pool = f.pool()
    assert verified(f).historical_admissions_valid
    for name in ("plans", "reservations"):
        with pytest.raises(hc.HttpContractError, match="limit_" + name):
            h.verify_history_incremental(pool, limits=replace(LIMITS, **{name: 1}))


def test_future_body_receipt_head_and_attempt_number_rejected():
    f = (
        History()
        .begin()
        .settle(status=200, size=5, next_cursor="next")
        .begin(1, "next")
        .settle(status=200, size=6, digest=hashlib.sha256(b"other").hexdigest())
    )
    original = f.measurements[0]
    # Do not reseal unrelated references: missing original binding is itself a
    # required rejection before any foreign receipt can be consumed.
    for m in (
        replace(original, body_head=f.j.head_sha),
        replace(original, attempt_number=9),
    ):
        pool = replace(f.pool(), measurements=(m, f.measurements[1]))
        rejected(pool, "availability_coverage")


def test_later_enrollment_preserves_earlier_core_and_all_historical_checks():
    f = History().begin().settle()
    earlier = f.reservations[0].core_json
    f.release()
    f.clock()
    before = f.cut()
    f.enroll_b()
    f.finish(before, h.EVIDENCE_KIND, "enrollment-original")
    assert f.reservations[0].core_json == earlier
    r = verified(f)
    assert r.historical_admissions_valid, r.reason
    assert len(r.final_capacity) == 2


def test_later_runtime_stop_cannot_be_hidden_by_selecting_older_current_clock():
    f = History()
    old = f.cut()
    f.runtime_append(
        "runtime_stop_entered",
        payload={
            "reason": "runtime_integrity_uncertain",
            "clock_reasons": [],
            "evidence_reference": "stop",
        },
    )
    page = c3.LivePageIdentity(
        f.c.plan.plan_sha,
        c3._canonical(f.c.plan.content["scope"]["queries"][0]),
        0,
        None,
    )
    with pytest.raises(hc.HttpContractError, match="current_cut"):
        h.build_admission_v2(f.pool(), limits=LIMITS, cut=old, page=page)


@pytest.mark.parametrize("row", [{}, {"event": []}, {"event": {"kind": 7}}])
def test_malformed_dependency_original_rejected_before_raw_key_access(row):
    pool = History().pool()
    damaged = replace(pool.journals[0], records=(c3._canonical(row),))
    pool = replace(pool, journals=(damaged, *pool.journals[1:]))
    for verifier in (h.verify_history_reference, h.verify_history_incremental):
        with pytest.raises(hc.HttpContractError, match="dependency_event_shape"):
            verifier(pool, limits=LIMITS)
