"""Test-first C3 v1 / C2 account v2 compatibility; artificial evidence only."""

import builtins
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import test_feasibility_live_body_contract as body_fixture
from test_feasibility_live_body_contract import NOW, Scenario, canonical, stamp
from test_feasibility_live_plan_enrollment import sources

from feasibility import http_contract, live_body_contract, live_plan_enrollment
from feasibility.http_contract import HttpContractError
from feasibility.live_body_contract import (
    BodyEvent,
    FixedPlanSnapshot,
    LiveBodyJournal,
    LiveBodyReceipt,
    LiveBodyStorageContract,
    PhysicalRootIdentity,
    StoragePolicy,
    _heads,
    _historical_c2,
    _scope_check,
    reconcile_live_bodies,
)
from feasibility.live_http_evidence import (
    LIVE_ACCOUNT_SCHEMA_V1,
    LIVE_ACCOUNT_SCHEMA_V2,
    LIVE_PLAN_SCHEMA,
    AttemptBinding,
    LiveAccountAuthority,
    LiveJournal,
    check_body_generation,
    reconcile_live_journals,
)
from feasibility.live_plan_enrollment import (
    authority_from_preflight,
    propose_plan_enrollment,
)


def source(name):
    return sources(name, not_before=NOW, expires_at=NOW + timedelta(hours=1))


def body(src, inode=2):
    c = LiveBodyStorageContract(
        FixedPlanSnapshot.from_plan(src["plan"]),
        src["preflight"].sha256,
        StoragePolicy(100, 1000, 1000, 1000, 10000),
    )
    root = PhysicalRootIdentity(
        c.root_binding_sha,
        "owner-store-claim",
        c.root,
        1,
        inode,
        "mount-claim",
        1,
        NOW,
        "physical-observation-claim",
    )
    return LiveBodyJournal(c, root)


class EnrolledScenario(Scenario):
    """Reuse unchanged C3 writer/response fixtures, with genuine enrollment pairs."""

    def __init__(self, *, approval_start=None, approval_end=None):
        self.src = source("compat-a")
        approval = replace(
            self.src["approval"],
            valid_from=approval_start or self.src["plan"].not_before,
            valid_until=approval_end or self.src["plan"].expires_at,
        )
        self.src.update(
            approval=approval, canonical_approval=canonical(approval.to_dict())
        )
        authority = authority_from_preflight(
            self.src["preflight"], canonical_preflight=self.src["canonical_preflight"]
        )
        self.h0 = LiveJournal.create_account_v2(authority, recorded_at=NOW)
        proposal = propose_plan_enrollment(
            self.h0,
            (),
            expected_account_head=self.h0.head_sha,
            operation_id="enroll-a",
            at=max(NOW, approval.valid_from),
            **self.src,
        )
        self.a, self.p = proposal.account, proposal.plan
        self.h1 = self.a
        self.scope = self.p.projection.scopes[0]
        self.j = body(self.src)
        self.c, self.root = self.j.contract, self.j.root
        self.n = int((max(NOW, approval.valid_from) - NOW).total_seconds())
        self.index = 0
        self.pb = self.jb = None

    @property
    def plans(self):
        return (self.p,) if self.pb is None else (self.p, self.pb)

    @property
    def related(self):
        return () if self.jb is None else (self.jb,)

    def enroll_b(self, same_time=False, src=None):
        self.h2 = self.a
        self.old_body_bytes = self.j.to_bytes()
        self.src_b = src or source("compat-b")
        when = NOW + timedelta(seconds=self.n) if same_time else self.tick()
        proposal = propose_plan_enrollment(
            self.a,
            (self.p,),
            expected_account_head=self.a.head_sha,
            operation_id="enroll-b",
            at=when,
            **self.src_b,
        )
        self.a, self.pb = proposal.account, proposal.plan
        self.jb = body(self.src_b, 3)
        return self

    def result(self, **kwargs):
        values = dict(account=self.a, plans=self.plans, related_bodies=self.related)
        values.update(kwargs)
        return reconcile_live_bodies(self.j, **values)

    def committed(self, next_cursor=None):
        self.j = self.j.commit(
            self.oid,
            next_cursor=next_cursor,
            at=self.tick(),
            operation_id=f"commit-{self.index}",
            account=self.a,
            plans=self.plans,
            related_bodies=self.related,
        )
        return self

    def reserve_b_attempt(self):
        for state in self.a.projection.holds:
            if state.released_at is None:
                self.n = max(self.n, int((state.hold.not_before - NOW).total_seconds()))
                self.a = self.a.append(
                    "hold_released",
                    recorded_at=self.tick(),
                    transition_id="release-b-" + state.hold.hold_id,
                    hold_id=state.hold.hold_id,
                    expected_hold_sha256=state.hold.hold_version_sha256,
                    clock_reference="clock",
                    account_state_reference="state",
                    manual_review_reference="review",
                    repair_reference=None,
                )
        scope = self.pb.projection.scopes[0]
        b = AttemptBinding(
            scope.plan_sha,
            scope.preflight_sha,
            scope.account_ref,
            scope.approval_sha,
            "attempt-b",
            "slot-b",
            "holder-b",
            self.a.projection.generation + 1,
        )
        when = self.tick()
        self.pb = self.pb.append(
            "attempt_reserved",
            recorded_at=when,
            transition_id="reserve-b",
            binding=b,
            reserved_at=stamp(when),
        )
        self.a = self.a.append(
            "slot_reserved",
            recorded_at=when,
            transition_id="reserve-b",
            binding=b,
            reserved_at=stamp(when),
        )
        return self


def prepared_complete():
    return (
        EnrolledScenario()
        .begin()
        .opened()
        .observed()
        .received()
        .closed()
        .stabilized()
        .acquired()
    )


@pytest.mark.parametrize(
    "start,end",
    [
        (NOW, NOW + timedelta(hours=1)),
        (NOW + timedelta(minutes=30), NOW + timedelta(hours=1)),
        (NOW, NOW + timedelta(minutes=45)),
        (NOW + timedelta(minutes=30), NOW + timedelta(minutes=45)),
        (NOW + timedelta(microseconds=1), NOW + timedelta(hours=1)),
        (NOW, NOW + timedelta(hours=1, microseconds=-1)),
    ],
    ids=["exact", "start-narrow", "end-narrow", "both-narrow", "start-us", "end-us"],
)
def test_scope_window_genuine_v2_enrollment_reservation_and_commit(start, end):
    s = EnrolledScenario(approval_start=start, approval_end=end)
    assert s.a.schema == LIVE_ACCOUNT_SCHEMA_V2
    assert (s.scope.not_before, s.scope.expires_at) == (start, end)
    assert s.scope.approval_sha == s.src["approval"].sha256
    assert reconcile_live_journals(s.a, s.plans).classification == "consistent"
    assert s.result().classification == "consistent"
    s.begin().complete()
    assert s.j.object(s.oid).state == "committed"
    result = s.result()
    assert result.classification == "consistent"
    assert not any(
        (
            result.live_send_permitted,
            result.live_acquisition_permitted,
            result.identity_verified,
            result.store_implemented,
        )
    )


@pytest.mark.parametrize("schema", [LIVE_ACCOUNT_SCHEMA_V1, LIVE_ACCOUNT_SCHEMA_V2])
@pytest.mark.parametrize("start_offset,end_offset", [(0, 0), (1, 0), (0, -1), (1, -1)])
def test_scope_window_schema_policy(schema, start_offset, end_offset):
    s = EnrolledScenario()
    scope = replace(
        s.scope,
        not_before=s.scope.not_before + timedelta(seconds=start_offset),
        expires_at=s.scope.expires_at + timedelta(seconds=end_offset),
    )
    at = NOW + timedelta(seconds=2)
    plan = LiveJournal.create(
        LIVE_PLAN_SCHEMA, (scope,), recorded_at=at, transition_id="opening"
    )
    if schema == LIVE_ACCOUNT_SCHEMA_V1:
        account = LiveJournal.create(schema, (scope,), recorded_at=at)
    else:
        account = s.h0.append(
            "plan_enrolled",
            recorded_at=at,
            transition_id="opening",
            scope=scope.to_dict(),
            plan_journal_initial_head=plan.head_sha,
            prior_plan_heads=[],
        )
    assert reconcile_live_journals(account, (plan,)).classification == "consistent"
    result = s.result(account=account, plans=(plan,))
    if schema == LIVE_ACCOUNT_SCHEMA_V1 and (start_offset or end_offset):
        assert result.classification == "inconsistent"
        assert result.reasons == ("c3_c2_scope_mismatch",)
    else:
        assert result.classification == "consistent"


@pytest.mark.parametrize("side", ["start", "end"])
def test_scope_window_outside_approval_rejected_by_genuine_builder(side):
    start = NOW - timedelta(microseconds=1) if side == "start" else NOW
    end = NOW + timedelta(hours=1, microseconds=1 if side == "end" else 0)
    with pytest.raises(HttpContractError, match="^approval_outside_validity_window$"):
        EnrolledScenario(approval_start=start, approval_end=end)


@pytest.mark.parametrize("delta", [0, -1])
def test_scope_window_empty_or_inverted_rejected_by_c2_value(delta):
    s = EnrolledScenario()
    with pytest.raises(HttpContractError, match="^live_scope_window_invalid$"):
        replace(s.scope, expires_at=s.scope.not_before + timedelta(microseconds=delta))


@pytest.mark.parametrize(
    "boundary,delta,valid",
    [("start", -1, False), ("start", 0, True), ("end", -1, True), ("end", 0, False)],
)
def test_scope_window_genuine_enrollment_is_half_open(boundary, delta, valid):
    s = EnrolledScenario(
        approval_start=NOW + timedelta(minutes=30),
        approval_end=NOW + timedelta(minutes=45),
    )
    at = (
        s.scope.not_before if boundary == "start" else s.scope.expires_at
    ) + timedelta(microseconds=delta)
    args = dict(
        expected_account_head=s.h0.head_sha, operation_id="boundary", at=at, **s.src
    )
    before = s.h0.to_bytes()
    if valid:
        result = propose_plan_enrollment(s.h0, (), **args)
        assert (
            reconcile_live_journals(result.account, (result.plan,)).classification
            == "consistent"
        )
        assert (
            s.result(account=result.account, plans=(result.plan,)).classification
            == "consistent"
        )
    else:
        with pytest.raises(
            HttpContractError, match="^approval_outside_validity_window$"
        ):
            propose_plan_enrollment(s.h0, (), **args)
    assert s.h0.to_bytes() == before


def narrow_scenario():
    return EnrolledScenario(
        approval_start=NOW + timedelta(minutes=30),
        approval_end=NOW + timedelta(minutes=45),
    )


def test_scope_window_narrow_history_current_and_generation():
    s = narrow_scenario().begin().complete()
    old_bytes = s.j.to_bytes()
    receipt_bytes = s.j.object(s.oid).receipt.to_bytes()
    s.enroll_b()
    assert s.j.to_bytes() == old_bytes
    assert s.result().classification == "consistent"
    assert s.a.projection.generation == 1
    assert s.a.projection.enrollment_revision == 2
    for raw in s.j.records:
        event = BodyEvent.from_dict(json.loads(raw)["event"])
        if event.c2_heads is not None:
            account, plans = _historical_c2(event.c2_heads, s.a, s.plans)
            assert account.schema == LIVE_ACCOUNT_SCHEMA_V2
            assert len(account.projection.scopes) == len(plans) == 1
            assert _scope_check(s.c, account) == s.scope
            assert _heads(account, plans) == event.c2_heads
    missing_body = s.result(related_bodies=())
    assert missing_body.classification == "pending"
    assert missing_body.reasons == ("required_related_body_journal_missing",)
    missing_plan = s.result(plans=(s.p,))
    assert missing_plan.classification == "inconsistent"
    assert "plan_enrollment_missing_journal" in missing_plan.reasons
    s.reserve_b_attempt()
    assert s.a.projection.generation == 2
    assert s.j.object(s.oid).receipt.to_bytes() == receipt_bytes
    assert s.result().classification == "consistent"
    with pytest.raises(HttpContractError, match="stale_generation"):
        check_body_generation(s.a, s.b)


def test_scope_window_narrow_stale_new_commit_rejected():
    s = (
        narrow_scenario()
        .begin()
        .opened()
        .observed()
        .received()
        .closed()
        .stabilized()
        .acquired()
    )
    s.enroll_b().reserve_b_attempt()
    with pytest.raises(HttpContractError, match="stale_generation"):
        s.committed()


@pytest.mark.parametrize("paired", [False, True])
def test_scope_window_narrow_missing_writer_and_c2_pending(paired):
    s = narrow_scenario().begin()
    at = s.tick()
    if paired:
        s.pair("attempt_sent", "slot_sent", at, sent_at=stamp(at))
        s.received().enroll_b()
        assert reconcile_live_journals(s.a, s.plans).classification == "consistent"
    else:
        s.p = s.p.append(
            "attempt_sent",
            recorded_at=at,
            transition_id="unpaired",
            binding=s.b,
            sent_at=stamp(at),
        )
        assert reconcile_live_journals(s.a, s.plans).classification == "pending"
    result = s.result()
    assert result.classification == "pending"
    assert "writer_evidence_missing" in result.reasons
    if not paired:
        assert "pending_send_pair" in result.reasons


def test_scope_window_narrow_approval_binding_remains_exact():
    s = narrow_scenario().begin()
    identity = replace(s.identity, binding=replace(s.b, approval_sha="f" * 64))
    before = s.j.to_bytes()
    with pytest.raises(HttpContractError, match="^header_binding_mismatch$"):
        s.j.reserve(
            identity,
            at=s.tick(),
            operation_id="wrong-approval",
            account=s.a,
            plans=s.plans,
        )
    assert s.j.to_bytes() == before


def test_scope_window_unknown_schema_rejected_by_c2_reader():
    s = EnrolledScenario()
    forged = replace(s.a)
    object.__setattr__(forged, "schema", "unknown-account")
    with pytest.raises(HttpContractError, match="schema_mismatch"):
        _scope_check(s.c, forged)


def test_empty_account_needs_no_fictitious_global_body():
    s = EnrolledScenario()
    assert s.h0.projection.scopes == ()
    assert reconcile_live_journals(s.h0, ()).classification == "consistent"
    assert s.h1.projection.enrollment_revision == 1
    assert s.j.records == ()
    assert s.result().classification == "consistent"


def test_v2_reservation_complete_and_commit():
    s = EnrolledScenario().begin()
    assert s.j.object(s.oid).state == "reserved"
    assert s.result().classification == "consistent"
    s.complete()
    assert s.j.object(s.oid).state == "committed"
    assert s.result().classification == "consistent"
    check_body_generation(s.a, s.b)
    assert (
        not s.result().live_send_permitted and not s.result().live_acquisition_permitted
    )


@pytest.mark.parametrize("same_time", [False, True])
def test_old_events_require_historical_a_but_current_requires_a_b(same_time):
    s = EnrolledScenario().begin().complete()
    old_events = tuple(BodyEvent.from_dict(json.loads(r)["event"]) for r in s.j.records)
    receipt_bytes = s.j.object(s.oid).receipt.to_bytes()
    s.enroll_b(same_time=same_time)
    assert s.j.to_bytes() == s.old_body_bytes
    assert s.jb.records == ()
    assert s.j.object(s.oid).receipt.to_bytes() == receipt_bytes
    for event in old_events:
        if event.c2_heads is None:
            continue
        old_account, old_plans = _historical_c2(event.c2_heads, s.a, s.plans)
        assert {p.projection.scopes[0].plan_sha for p in old_plans} == {
            s.scope.plan_sha
        }
        assert {scope.plan_sha for scope in old_account.projection.scopes} == {
            s.scope.plan_sha
        }
        assert _heads(old_account, old_plans) == event.c2_heads
        assert (
            reconcile_live_journals(old_account, old_plans).classification
            == "consistent"
        )
    assert len(s.a.projection.scopes) == 2
    assert s.result().classification == "consistent"
    assert "c3_historical_generation_already_superseded" not in s.result().reasons
    assert s.a.projection.generation == s.h2.projection.generation == 1
    assert s.a.projection.enrollment_revision == s.h2.projection.enrollment_revision + 1
    check_body_generation(s.a, s.b)


@pytest.mark.parametrize("side", ["a", "b"])
def test_current_missing_body_is_pending_not_historical_corruption(side):
    s = EnrolledScenario().begin().complete().enroll_b()
    journal = s.j if side == "a" else s.jb
    result = reconcile_live_bodies(journal, account=s.a, plans=s.plans)
    assert result.classification == "pending"
    assert result.reasons == ("required_related_body_journal_missing",)


def test_prepared_c_not_required_but_supplied_unrelated_c_rejected():
    s = EnrolledScenario().begin().complete().enroll_b()
    c = body(source("compat-c"), 4)
    assert c.contract.plan.plan_sha not in {p.plan_sha for p in s.a.projection.scopes}
    assert s.result().classification == "consistent"
    result = s.result(related_bodies=(s.jb, c))
    assert result.classification == "inconsistent"
    assert "unrelated_body_journal" in result.reasons


def test_duplicate_body_rejected():
    s = EnrolledScenario().enroll_b()
    with pytest.raises(HttpContractError, match="duplicate_body_journal"):
        s.result(related_bodies=(s.jb, s.j))


def test_different_plans_same_dedicated_root_still_rejected():
    s = EnrolledScenario()
    other = sources(
        "compat-a",
        not_before=NOW,
        expires_at=NOW + timedelta(hours=1),
        limits=replace(s.src["plan"].limits, max_attempts=9),
    )
    s.enroll_b(src=other)
    assert s.j.contract.root == s.jb.contract.root
    assert s.scope.plan_sha != s.pb.projection.scopes[0].plan_sha
    result = s.result()
    assert result.classification == "inconsistent"
    assert "different_plans_share_body_root" in result.reasons


def test_empty_b_requires_valid_root_binding():
    s = EnrolledScenario().enroll_b()
    assert s.result().classification == "consistent"
    with pytest.raises(HttpContractError, match="root"):
        LiveBodyJournal(s.jb.contract, s.j.root)


def test_current_c2_cannot_omit_b_even_while_evaluating_a():
    s = prepared_complete().enroll_b()
    result = s.result(plans=(s.p,))
    assert result.classification == "inconsistent"
    assert "plan_enrollment_missing_journal" in result.reasons
    with pytest.raises(HttpContractError, match="c2_inconsistent"):
        _heads(s.a, (s.p,))
    with pytest.raises(HttpContractError, match="commit_inconsistent"):
        s.j.commit(
            s.oid,
            next_cursor=None,
            at=s.tick(),
            operation_id="incomplete-plans",
            account=s.a,
            plans=(s.p,),
            related_bodies=s.related,
        )


def test_wholly_old_snapshot_is_pure_limit_not_current_authority():
    s = prepared_complete()
    old_account, old_plan = s.a, s.p
    s.enroll_b()
    # A pure function cannot discover that BOTH supplied values are stale.
    # I1 must compare locked current heads before accepting this proposal.
    claimed = s.j.commit(
        s.oid,
        next_cursor=None,
        at=s.tick(),
        operation_id="old-snapshot-claim",
        account=old_account,
        plans=(old_plan,),
    )
    assert claimed.object(s.oid).receipt.c2_heads.account_head == old_account.head_sha
    assert old_account.head_sha != s.a.head_sha
    assert len(s.a.projection.scopes) == 2


def test_new_commit_after_enrollment_records_all_current_heads():
    s = prepared_complete().enroll_b()
    old_records = s.j.records
    s.committed()
    receipt = s.j.object(s.oid).receipt
    assert receipt.c2_heads == _heads(s.a, s.plans)
    assert len(receipt.c2_heads.plan_heads) == 2
    assert s.j.records[:-1] == old_records
    assert s.a.projection.generation == 1
    assert s.result().classification == "consistent"


def test_later_real_generation_rejects_new_commit_but_keeps_old_receipt():
    s = prepared_complete().enroll_b().reserve_b_attempt()
    assert s.a.projection.generation == 2
    with pytest.raises(HttpContractError, match="stale_generation"):
        s.committed()
    s = EnrolledScenario().begin().complete().enroll_b()
    receipt = s.j.object(s.oid).receipt.to_bytes()
    s.reserve_b_attempt()
    assert s.j.object(s.oid).receipt.to_bytes() == receipt
    assert s.result().classification == "consistent"


def test_backdated_account_prefix_cannot_hide_known_newer_generation():
    s = prepared_complete()
    before = s.j
    s.committed()
    event = BodyEvent.from_dict(json.loads(s.j.records[-1])["event"])
    s.j = before
    s.enroll_b().reserve_b_attempt()
    when = s.tick()
    receipt = LiveBodyReceipt.from_dict(json.loads(event.payload_json))
    event = replace(
        event,
        recorded_at=when,
        payload_json=canonical(replace(receipt, committed_at=when).to_dict()),
    )
    s.j = s.j.append(event)
    result = s.result()
    assert result.classification == "inconsistent"
    assert "c3_historical_generation_already_superseded" in result.reasons


@pytest.mark.parametrize("outcome", ["response", "unknown"])
def test_v2_sent_without_writer_is_pending(outcome):
    s = EnrolledScenario().begin()
    when = s.tick()
    s.pair("attempt_sent", "slot_sent", when, sent_at=stamp(when))
    s.received(outcome=outcome)
    if outcome == "response":
        s.enroll_b()
    result = s.result()
    assert result.classification == "pending"
    assert "writer_evidence_missing" in result.reasons
    assert not result.live_send_permitted


def test_current_pending_send_pair_is_not_promoted():
    s = EnrolledScenario().begin()
    when = s.tick()
    s.p = s.p.append(
        "attempt_sent",
        recorded_at=when,
        transition_id="one-sided-send",
        binding=s.b,
        sent_at=stamp(when),
    )
    result = s.result()
    assert result.classification == "pending"
    assert "pending_send_pair" in result.reasons
    assert "writer_evidence_missing" in result.reasons


@pytest.mark.parametrize(
    "field",
    [
        "account",
        "preflight",
        "window",
        "expiry",
        "outside-start",
        "outside-end",
        "retry",
        "plan",
    ],
)
def test_v2_scope_check_not_relaxed(field):
    s = EnrolledScenario()
    scope = s.scope
    if field == "account":
        scope = replace(scope, account_ref="wrong-account")
    if field == "preflight":
        scope = replace(scope, preflight_sha="f" * 64)
    if field == "window":
        scope = replace(scope, not_before=NOW + timedelta(seconds=1))
    if field == "expiry":
        scope = replace(scope, expires_at=scope.expires_at - timedelta(seconds=1))
    if field == "outside-start":
        scope = replace(scope, not_before=scope.not_before - timedelta(microseconds=1))
    if field == "outside-end":
        scope = replace(scope, expires_at=scope.expires_at + timedelta(microseconds=1))
    if field == "retry":
        scope = replace(scope, retry=replace(scope.retry, max_attempts_per_page=2))
    if field == "plan":
        scope = replace(scope, plan_sha="e" * 64)
    auth = LiveAccountAuthority(
        scope.account_ref, scope.preflight_sha, scope.account_policy, scope.clock_policy
    )
    account = LiveJournal.create_account_v2(auth, recorded_at=NOW)
    when = NOW + timedelta(seconds=2)
    plan = LiveJournal.create(
        LIVE_PLAN_SCHEMA, (scope,), recorded_at=when, transition_id="different-opening"
    )
    account = account.append(
        "plan_enrolled",
        recorded_at=when,
        transition_id="different-opening",
        scope=scope.to_dict(),
        plan_journal_initial_head=plan.head_sha,
        prior_plan_heads=[],
    )
    assert reconcile_live_journals(account, (plan,)).classification == "consistent"
    result = s.result(account=account, plans=(plan,))
    if field in {"window", "expiry"}:
        # OD-CMP-WINDOW-02 intentionally replaces the old v2 exact-window rule.
        assert result.classification == "consistent"
    else:
        assert result.classification == "inconsistent"
        assert any("scope" in r for r in result.reasons)


@pytest.mark.parametrize(
    "damage", ["initial", "transition", "time", "prior", "plan_only", "account_only"]
)
def test_enrollment_pair_corruption_propagates(damage):
    s = EnrolledScenario().begin().complete()
    old_account = s.a
    s.enroll_b()
    if damage == "account_only":
        plans = (s.p,)
    elif damage == "plan_only":
        s.a = old_account
        plans = s.plans
    elif damage in {"transition", "time"}:
        s.pb = LiveJournal.create(
            LIVE_PLAN_SCHEMA,
            s.pb.projection.scopes,
            recorded_at=NOW + timedelta(seconds=s.n + (damage == "time")),
            transition_id="wrong" if damage == "transition" else "enroll-b",
        )
        plans = s.plans
    else:
        raw = json.loads(s.a.records[-1])["event"]["data"]
        if damage == "initial":
            raw["plan_journal_initial_head"] = s.p.head_sha
        if damage == "prior":
            raw["prior_plan_heads"][0]["head"] = "f" * 64
        s.a = old_account.append(
            "plan_enrolled",
            recorded_at=NOW + timedelta(seconds=s.n),
            transition_id="enroll-b",
            **raw,
        )
        plans = s.plans
    c2 = reconcile_live_journals(s.a, plans)
    assert c2.classification == "inconsistent"
    result = s.result(plans=plans)
    assert result.classification == "inconsistent"
    assert set(c2.reasons).issubset(result.reasons)


def test_stale_prior_head_safety_without_claiming_l_en_1_fixed():
    s = EnrolledScenario().begin().complete()
    opening = s.p.records[:1]
    before = s.a
    s.enroll_b()
    data = json.loads(s.a.records[-1])["event"]["data"]
    data["prior_plan_heads"][0]["head"] = LiveJournal(
        LIVE_PLAN_SCHEMA, opening
    ).head_sha
    s.a = before.append(
        "plan_enrolled",
        recorded_at=NOW + timedelta(seconds=s.n),
        transition_id="enroll-b",
        **data,
    )
    assert reconcile_live_journals(s.a, s.plans).classification != "consistent"
    assert s.result().classification != "consistent"


def test_unknown_account_schema_has_no_fallback():
    s = EnrolledScenario()
    with pytest.raises(HttpContractError, match="schema_mismatch"):
        LiveJournal.from_bytes(s.a.to_bytes(), expected_schema="unknown-account-v2")
    # Deliberately corrupt an otherwise frozen in-memory claim; C2 reader still
    # rejects it. This is not a supported constructor for unknown schemas.
    forged = replace(s.a)
    object.__setattr__(forged, "schema", "unknown-account-v2")
    with pytest.raises(HttpContractError, match="live_account_journal_required"):
        _heads(forged, (s.p,))


def pin_golden_http_output_root(monkeypatch):
    """Pin imported aliases before construction; never inspect/create this path."""
    root = Path("/__stock_range_trader_golden__/http-output")
    for module in (
        http_contract,
        live_plan_enrollment,
        live_body_contract,
        body_fixture,
    ):
        monkeypatch.setattr(module, "HTTP_OUTPUT_ROOT", root)

    # The legacy plan constructor checks symlinks. Supply an artificial claim
    # only for this fixture's path/ancestors, without weakening production code.
    output = root / "c3-artificial"
    allowed = (output, *output.parents)

    def synthetic_non_symlink(path):
        assert path in allowed, "unexpected path in golden fixture"
        return False

    def forbidden(*args, **kwargs):
        raise AssertionError("golden fixture must not access the filesystem")

    monkeypatch.setattr(Path, "is_symlink", synthetic_non_symlink)
    for name in ("resolve", "stat", "lstat", "exists", "mkdir", "touch", "open"):
        monkeypatch.setattr(Path, name, forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    return root


def test_v1_golden_bytes_receipt_projection_and_hashes_unchanged(monkeypatch):
    # Fixed digests captured once from unchanged production C3 using
    # /__stock_range_trader_golden__/http-output, not the checkout's real path.
    # Expected values must never be recomputed from the runtime fixture.
    with monkeypatch.context() as pinned:
        root = pin_golden_http_output_root(pinned)
        s = Scenario().begin().complete()
        assert s.c.plan.content["output_dir"] == str(root / "c3-artificial")
        assert s.c.root == str(root / "c3-artificial" / "live-body-v1")
        assert s.root.canonical_realpath == s.c.root
        expected = {
            "contract": "4b21e43d9b28c70227a22278dc49328ec98096db6f794c486c533961a8557379",
            "root": "81498f29023d1f5d5f11e76a3c23f13d7748e0f4517b943f2280039a4f1acc37",
            "journal": "f56aff527b082a36848d801c220dce8bbd911a12877bcf652758ba8cb72a56f0",
            "receipt": "469f4245d996334ccd89c1ff94033dda7155ada336246c93a9dfc638f13b799a",
            "projection": "a7d2093331d35102342e67d83a06793297a4fa70d82783546acdda65352554e1",
            "result": "1fec4a83ffac24782b71252f2c3c7850b1ea6042948eba56bdcbc2674019cf1e",
        }
        values = dict(
            contract=s.c,
            root=s.root,
            journal=s.j,
            receipt=s.j.object(s.oid).receipt,
            projection=s.j.projection,
            result=s.result(),
        )
        for name, value in values.items():
            assert hashlib.sha256(value.to_bytes()).hexdigest() == expected[name]
        assert (
            s.j.head_sha
            == "3625483fb31a53bd0bfe25fd8c156d6a1af0825022ba38f7da75e0618592a522"
        )
        assert all(json.loads(line)["schema"].endswith("-v1") for line in s.j.records)


def test_v2_body_saved_schema_and_roundtrip_are_still_v1():
    s = EnrolledScenario().begin().complete().enroll_b()
    for value in (s.c, s.root, s.j.object(s.oid).receipt, s.j.projection):
        assert value.schema.endswith("-v1")
        assert type(value).from_bytes(value.to_bytes()).to_bytes() == value.to_bytes()
    decoded = LiveBodyJournal.from_bytes(
        s.j.to_bytes(), expected_contract=s.c, expected_root=s.root
    )
    assert decoded.to_bytes() == s.j.to_bytes()
    assert decoded.projection == s.j.projection
    assert (
        reconcile_live_bodies(
            decoded, account=s.a, plans=s.plans, related_bodies=s.related
        )
        == s.result()
    )


def test_reconciliation_uses_no_io_or_real_clock(monkeypatch):
    s = EnrolledScenario().begin().complete().enroll_b()
    import os
    import socket
    import time

    def forbidden(*args, **kwargs):
        raise AssertionError("I/O forbidden")

    for name in ("exists", "resolve", "stat", "open"):
        monkeypatch.setattr(Path, name, forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(time, "time", forbidden)
    assert s.result().classification == "consistent"
    assert not s.result().live_send_permitted
