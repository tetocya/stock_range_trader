"""C3 tests use only immutable artificial evidence, never files or HTTP."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from feasibility.http_contract import (
    ACCOUNT_LEDGER_SCHEMA,
    HTTP_OUTPUT_ROOT,
    AccountRatePolicy,
    BodyFileEvidence,
    BodyInventory,
    HttpAcquisitionPlan,
    HttpContractError,
    HttpLimits,
    LiveAcquisitionGate,
    RetryRules,
)
from feasibility.live_body_contract import (
    MAX_INT64,
    MAX_RECORD_BYTES,
    BodyAssessment,
    BodyEvent,
    FixedPlanSnapshot,
    LiveBodyJournal,
    LiveBodyObject,
    LiveBodyReceipt,
    LiveBodyStorageContract,
    LivePageIdentity,
    ObjectIdentity,
    PhysicalRootIdentity,
    QuarantineEvidence,
    StableBodyEvidence,
    StoragePolicy,
    WriterEvidence,
    reconcile_live_bodies,
)
from feasibility.live_http_evidence import (
    LIVE_ACCOUNT_SCHEMA,
    LIVE_PLAN_SCHEMA,
    AttemptBinding,
    LiveJournal,
    LivePlanScope,
    capture_headers,
    required_attempt_hold,
)
from feasibility.live_preflight import LiveClockPolicy

NOW = datetime(2040, 1, 1, tzinfo=UTC)
DIGEST = hashlib.sha256(b"artificial").hexdigest()


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stamp(value):
    return value.isoformat(timespec="microseconds")


def contract(policy=None):
    plan = HttpAcquisitionPlan(
        artifact_id="c3-artificial",
        kind="calendar_discovery",
        reference_date="2039-01-01",
        calendar_start="2039-01-01",
        calendar_end="2039-01-02",
        master_date=None,
        daily_dates=(),
        calendar_source_sha256=None,
        calendar_source_reference=None,
        output_dir=str(HTTP_OUTPUT_ROOT / "c3-artificial"),
        not_before=NOW,
        expires_at=NOW + timedelta(hours=1),
        limits=HttpLimits(20, 20, 20, 1200, 10000, 10000, 10000, 1000, 1000, 1000),
        retry=RetryRules(3, 13, 120, 13, 13, 30),
        account_ref="c3-artificial-account",
    )
    return LiveBodyStorageContract(
        FixedPlanSnapshot.from_plan(plan),
        "a" * 64,
        policy or StoragePolicy(100, 1000, 1000, 1000, 10000),
    )


class Scenario:
    """One artificial Store timeline; C2 operations precede their C3 evidence."""

    def __init__(self, policy=None):
        self.c = contract(policy)
        raw = self.c.plan.content
        self.scope = LivePlanScope(
            self.c.plan.plan_sha,
            self.c.preflight_sha,
            raw["account_ref"],
            "b" * 64,
            NOW,
            NOW + timedelta(hours=1),
            RetryRules(**raw["retry"]),
            AccountRatePolicy(13, 120, 13, 13, 30, 60),
            LiveClockPolicy(5, 10),
        )
        self.p = LiveJournal.create(LIVE_PLAN_SCHEMA, (self.scope,), recorded_at=NOW)
        self.a = LiveJournal.create(LIVE_ACCOUNT_SCHEMA, (self.scope,), recorded_at=NOW)
        self.root = PhysicalRootIdentity(
            self.c.root_binding_sha,
            "owner-store-claim",
            self.c.root,
            1,
            2,
            "mount-claim",
            1,
            NOW,
            "physical-observation-claim",
        )
        self.j = LiveBodyJournal(self.c, self.root)
        self.n = 0
        self.index = 0

    def tick(self):
        self.n += 1
        return NOW + timedelta(seconds=self.n)

    def pair(self, pk, ak, at, **data):
        transition = pk + "-" + str(self.n)
        self.p = self.p.append(
            pk, recorded_at=at, transition_id=transition, binding=self.b, **data
        )
        self.a = self.a.append(
            ak, recorded_at=at, transition_id=transition, binding=self.b, **data
        )

    def begin(self, page_index=0, cursor=None):
        for hs in self.a.projection.holds:
            if hs.released_at is None:
                self.n = max(self.n, int((hs.hold.not_before - NOW).total_seconds()))
                self.a = self.a.append(
                    "hold_released",
                    recorded_at=self.tick(),
                    transition_id="release-" + str(self.n),
                    hold_id=hs.hold.hold_id,
                    expected_hold_sha256=hs.hold.hold_version_sha256,
                    clock_reference="clock",
                    account_state_reference="state",
                    manual_review_reference="review"
                    if hs.hold.release_mode == "manual"
                    else None,
                    repair_reference=None,
                )
        self.index += 1
        self.b = AttemptBinding(
            self.c.plan.plan_sha,
            self.c.preflight_sha,
            self.scope.account_ref,
            self.scope.approval_sha,
            f"attempt-{self.index}",
            f"slot-{self.index}",
            "holder",
            self.a.projection.generation + 1,
        )
        at = self.tick()
        self.pair("attempt_reserved", "slot_reserved", at, reserved_at=stamp(at))
        page = LivePageIdentity(
            self.c.plan.plan_sha,
            canonical(self.c.plan.content["scope"]["queries"][0]),
            page_index,
            cursor,
        )
        self.identity = ObjectIdentity(self.b, page)
        self.oid = self.identity.object_id
        self.j = self.j.reserve(
            self.identity,
            at=at,
            operation_id=f"reserve-{self.index}",
            account=self.a,
            plans=(self.p,),
        )
        return self

    def opened(self):
        at = self.tick()
        self.pair("attempt_sent", "slot_sent", at, sent_at=stamp(at))
        w = WriterEvidence(
            self.b,
            "writer-" + str(self.index),
            "open",
            at,
            self.j.object(self.oid).observed_size,
        )
        self.j = self.j.writer(self.oid, w, operation_id=f"open-{self.index}")
        return self

    def observed(self, size=60, digest=DIGEST, state="staging"):
        self.j = self.j.observe(
            self.oid,
            size=size,
            digest=digest,
            state=state,
            evidence_ref="bytes-observed",
            at=self.tick(),
            operation_id=f"observe-{self.index}-{self.n}",
        )
        return self

    def received(self, outcome="response", status=200):
        at = self.tick()
        header = capture_headers(
            observation_id="header-" + str(self.index),
            status=status,
            header_block_complete=True,
            observed_at=at,
            retry_after_fields=(),
        )
        self.pair(
            "response_headers_observed",
            "slot_headers_observed",
            at,
            observation=header.to_dict(),
        )
        self.sync_hold()
        at = self.tick()
        self.pair(
            "response_received" if outcome == "response" else "outcome_unknown",
            "slot_settled",
            at,
            settled_at=stamp(at),
            outcome=outcome,
            status=status if outcome == "response" else None,
        )
        self.sync_hold()
        return self

    def sync_hold(self):
        hold = required_attempt_hold(
            self.scope, self.a.projection.attempt(self.b.plan_sha, self.b.attempt_id)
        )
        kind = (
            "hold_extended"
            if any(h.hold.hold_id == hold.hold_id for h in self.a.projection.holds)
            else "hold_entered"
        )
        self.a = self.a.append(
            kind,
            recorded_at=hold.recorded_at,
            transition_id=f"hold-{self.index}-{self.n}",
            hold=hold.to_dict(),
        )

    def closed(self):
        w = replace(
            self.j.object(self.oid).writer,
            state="closed",
            close_ref="all-writers-closed-claim",
            observed_at=self.tick(),
        )
        self.j = self.j.writer(self.oid, w, operation_id=f"close-{self.index}")
        return self

    def stable_evidence(self, size=None, digest=None):
        obj = self.j.object(self.oid)
        return StableBodyEvidence(
            self.oid,
            self.root.sha256,
            obj.writer.sha256,
            self.tick(),
            self.tick(),
            self.tick(),
            self.tick(),
            obj.observed_size if size is None else size,
            obj.observed_digest if digest is None else digest,
            "object-physical-claim",
            "stable-claim",
            "recount-claim",
            "rehash-claim",
        )

    def stabilized(self, **changes):
        evidence = self.stable_evidence(**changes)
        self.j = self.j.stabilize(
            self.oid, evidence, at=self.tick(), operation_id=f"stable-{self.index}"
        )
        return self

    def acquired(self, size=60):
        self.j = self.j.acquired(
            self.oid,
            size=size,
            evidence_ref="complete-body-claim",
            at=self.tick(),
            operation_id=f"acquired-{self.index}",
            account=self.a,
            plans=(self.p,),
        )
        return self

    def committed(self, next_cursor=None):
        self.j = self.j.commit(
            self.oid,
            next_cursor=next_cursor,
            at=self.tick(),
            operation_id=f"commit-{self.index}",
            account=self.a,
            plans=(self.p,),
        )
        return self

    def complete(self, next_cursor=None):
        return (
            self.opened()
            .observed()
            .received()
            .closed()
            .stabilized()
            .acquired()
            .committed(next_cursor)
        )

    def quarantine_evidence(self):
        o = self.j.object(self.oid)
        source = "objects" if o.state == "orphan" else "staging"
        return QuarantineEvidence(
            f"quarantine-{self.index}",
            self.c.sha256,
            o.identity,
            o.version,
            self.root.sha256,
            self.c.layout()[source] + "/" + self.oid,
            self.c.layout()["quarantine"] + "/" + self.oid,
            o.observed_size,
            o.observed_digest,
            "final" if o.stable else "provisional",
            o.state,
            "crash-evidence",
            "operator-observation",
            o.writer,
            self.tick(),
        )

    def result(self, **kw):
        return reconcile_live_bodies(self.j, account=self.a, plans=(self.p,), **kw)


def test_duplicate_digest_distinct_pages_objects_and_both_budgets():
    s = Scenario().begin().complete("next")
    first = s.oid
    s.begin(1, "next").complete()
    assert first != s.oid
    p = s.j.projection
    assert p.retained_body_bytes == p.cumulative_acquired_bytes == 120
    assert p.reserved_remaining_bytes == p.unresolved_acquisition_charge == 0
    assert [o.receipt.final_digest for o in p.objects] == [DIGEST, DIGEST]
    assert s.result().classification == "consistent"


def test_retained_and_reserved_not_double_counted():
    s = Scenario().begin().opened().observed(20)
    p = s.j.projection
    assert (p.retained_body_bytes, p.reserved_remaining_bytes) == (20, 80)
    assert p.cumulative_acquired_bytes == 0
    assert p.unresolved_acquisition_charge == 100
    assert p.unstable


@pytest.mark.parametrize("state", ["partial", "orphan"])
@pytest.mark.parametrize("stable", [False, True])
def test_artifacts_and_quarantine_keep_capacity(state, stable):
    s = Scenario().begin().opened().observed(20, state=state)
    if stable:
        s.closed().stabilized()
    before = s.j.projection
    q = s.quarantine_evidence()
    s.j = s.j.quarantine_object(q)
    after = s.j.projection
    assert after.retained_body_bytes == before.retained_body_bytes == 20
    assert after.reserved_remaining_bytes == before.reserved_remaining_bytes
    assert after.cumulative_acquired_bytes == before.cumulative_acquired_bytes == 0
    assert after.unresolved_acquisition_charge == 100
    assert after.unstable is (not stable)
    assert s.j.object(s.oid).receipt is None


def test_quarantine_idempotency_returns_original_receipt_and_no_events():
    s = Scenario().begin().opened().observed(state="partial")
    q = s.quarantine_evidence()
    j = s.j.quarantine_object(q)
    assert j.quarantine_object(q) is j
    assert j.object(s.oid).quarantine == q
    assert j.to_bytes() == j.quarantine_object(q).to_bytes()


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason", "other"),
        ("observed_size", 59),
        ("observed_digest", "f" * 64),
        ("object_version", 99),
        ("root_identity_sha", "f" * 64),
    ],
)
def test_quarantine_conflicting_evidence(field, value):
    s = Scenario().begin().opened().observed(state="partial")
    q = s.quarantine_evidence()
    j = s.j.quarantine_object(q)
    with pytest.raises(HttpContractError, match="conflict"):
        j.quarantine_object(replace(q, **{field: value}))


@pytest.mark.parametrize("state", ["open", "unknown"])
def test_unstable_writer_blocks_reservation_and_finalization(state):
    s = Scenario().begin().opened().observed(state="partial").received("unknown")
    if state == "unknown":
        w = replace(s.j.object(s.oid).writer, state="unknown", observed_at=s.tick())
        s.j = s.j.writer(s.oid, w, operation_id="unknown-writer")
    q = s.quarantine_evidence()
    s.j = s.j.quarantine_object(q)
    assert s.result().classification == "pending"
    with pytest.raises(HttpContractError, match="writer_close_required"):
        s.j.stabilize(
            s.oid, s.stable_evidence(), at=s.tick(), operation_id="illegal-final"
        )
    with pytest.raises(HttpContractError, match="reservation_pending"):
        s.begin()


def test_closed_alone_does_not_release_or_finalize():
    s = Scenario().begin().opened().observed().received().closed().acquired()
    assert s.j.projection.reserved_remaining_bytes == 40
    assert s.j.projection.unstable
    with pytest.raises(HttpContractError, match="commit_pending"):
        s.committed()
    s.stabilized().committed()
    assert s.j.projection.reserved_remaining_bytes == 0
    assert not s.j.projection.unstable


def test_unknown_charge_survives_close_recount_and_quarantine():
    s = (
        Scenario()
        .begin()
        .opened()
        .observed(20, state="partial")
        .received("unknown")
        .closed()
        .stabilized()
    )
    s.j = s.j.quarantine_object(s.quarantine_evidence())
    p = s.j.projection
    assert (p.cumulative_acquired_bytes, p.unresolved_acquisition_charge) == (0, 100)
    assert (p.retained_body_bytes, p.reserved_remaining_bytes) == (20, 0)
    with pytest.raises(HttpContractError, match="complete_response_required"):
        s.acquired(20)


def test_same_page_retry_has_distinct_objects_one_authority():
    s = (
        Scenario()
        .begin()
        .opened()
        .observed(20, state="partial")
        .received("unknown")
        .closed()
        .stabilized()
    )
    first = s.oid
    s.begin().complete()
    assert first != s.oid
    assert len(s.j.projection.objects) == 2
    assert sum(o.receipt is not None for o in s.j.projection.objects) == 1
    assert s.j.projection.retained_body_bytes == 80
    assert s.j.projection.unresolved_acquisition_charge == 100
    with pytest.raises(HttpContractError, match="page_already_committed"):
        s.begin()


def test_cursor_revisit_rejected_even_with_different_digest():
    s = Scenario().begin().complete("next")
    s.begin(1, "next").opened().observed(
        digest="e" * 64
    ).received().closed().stabilized().acquired()
    with pytest.raises(HttpContractError, match="cursor_revisited"):
        s.committed("next")


@pytest.mark.parametrize(
    "field,value",
    [
        ("final_digest", "c" * 64),
        ("size", 59),
        ("root_identity_sha", "d" * 64),
        ("stability_sha", "e" * 64),
    ],
)
def test_receipt_object_mismatch(field, value):
    s = Scenario().begin().complete()
    # Rewind just the C3 commit, then supply a corrupt receipt through the event codec.
    good_event = BodyEvent.from_dict(json.loads(s.j.records[-1])["event"])
    receipt = LiveBodyReceipt.from_dict(json.loads(good_event.payload_json))
    previous = LiveBodyJournal(s.c, s.root, s.j.records[:-1])
    bad = replace(
        good_event, payload_json=canonical(replace(receipt, **{field: value}).to_dict())
    )
    with pytest.raises(HttpContractError, match="receipt_object_mismatch"):
        previous.append(bad)


def test_commit_write_once_and_quarantine_cannot_promote():
    s = Scenario().begin().complete()
    with pytest.raises(HttpContractError, match="write_once"):
        s.observed(60)
    s = (
        Scenario()
        .begin()
        .opened()
        .observed(state="orphan")
        .received()
        .closed()
        .stabilized()
        .acquired()
    )
    s.j = s.j.quarantine_object(s.quarantine_evidence())
    with pytest.raises(HttpContractError, match="finalization_evidence_missing"):
        s.committed()


def test_stale_generation_commit_rejected_and_old_receipt_auditable():
    s = (
        Scenario()
        .begin()
        .opened()
        .observed()
        .received()
        .closed()
        .stabilized()
        .acquired()
    )
    old = s.oid
    s.begin()  # retry gets the next C2 generation; old physical writer was closed
    with pytest.raises(HttpContractError, match="stale_generation"):
        s.j.commit(
            old,
            next_cursor=None,
            at=s.tick(),
            operation_id="stale-commit",
            account=s.a,
            plans=(s.p,),
        )
    s = Scenario().begin().complete("next")
    s.begin(1, "next")
    assert s.result().classification == "consistent"


@pytest.mark.parametrize(
    "field",
    [
        "retained_body_bytes",
        "reserved_remaining_bytes",
        "cumulative_acquired_bytes",
        "unresolved_acquisition_charge",
    ],
)
def test_projection_tamper(field):
    s = Scenario().begin().complete()
    p = s.j.projection
    result = s.result(claimed_projection=replace(p, **{field: getattr(p, field) + 1}))
    assert result.classification == "inconsistent"
    assert "storage_projection_mismatch" in result.reasons


@pytest.mark.parametrize(
    "field,value",
    [
        ("device_id", 9),
        ("inode", 9),
        ("root_epoch", 2),
        ("mount_ref", "replaced-mount"),
        ("owner_store_ref", "other-store"),
    ],
)
def test_physical_directory_replacement(field, value):
    s = Scenario()
    other = replace(s.root, **{field: value})
    assert s.result(current_root=other).classification == "inconsistent"
    with pytest.raises(HttpContractError, match="expected_binding_mismatch"):
        LiveBodyJournal.from_bytes(
            s.j.to_bytes(), expected_contract=s.c, expected_root=other
        )


def test_root_override_absent_and_layout_deterministic():
    c = contract()
    assert c.layout() == contract().layout()
    assert all(
        p.startswith(c.plan.content["output_dir"] + "/live-body-v1/")
        for p in c.layout().values()
    )
    with pytest.raises(TypeError):
        LiveBodyStorageContract(c.plan, c.preflight_sha, c.policy, root="/other")
    with pytest.raises(HttpContractError, match="physical_root_binding_mismatch"):
        s = Scenario()
        LiveBodyJournal(s.c, replace(s.root, canonical_realpath="/other"))


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "/",
        "/a/../b",
        "/a/./b",
        "/a//b",
        "/a/",
        "//a",
        "/~/a",
        "/$ROOT/a",
        "https://other/a",
        "/dev/fd/4",
        "/proc/self/fd/4",
        "/a\\b",
        "/a/%2e%2e/b",
    ],
)
def test_lexical_output_root_reject(path):
    raw = contract().plan.content
    raw["output_dir"] = path
    text = canonical(raw)
    with pytest.raises(HttpContractError, match="path_noncanonical"):
        FixedPlanSnapshot(text, hashlib.sha256(text.encode()).hexdigest())


def test_plan_snapshot_detached_and_hash_rechecked():
    c = contract()
    raw = c.plan.content
    raw["output_dir"] = "/different"
    assert c.plan.content["output_dir"] != "/different"
    with pytest.raises(HttpContractError, match="snapshot_mismatch"):
        FixedPlanSnapshot(canonical(raw), c.plan.plan_sha)
    assert LiveBodyStorageContract.from_bytes(c.to_bytes()) == c
    with pytest.raises(FrozenInstanceError):
        c.preflight_sha = "e" * 64


def test_unknown_size_is_not_zero_and_prior_observation_is_retained():
    s = (
        Scenario()
        .begin()
        .opened()
        .observed(20, state="partial")
        .observed(None, None, "partial")
    )
    p = s.j.projection
    assert p.retained_body_bytes is None
    assert p.retained_observed_minimum_bytes == 20
    assert p.unknown_size_objects == 1
    assert p.unstable
    s.closed().stabilized(size=20, digest=DIGEST)
    assert s.j.projection.retained_body_bytes == 20
    s = Scenario().begin().opened().observed(0)
    assert s.j.projection.retained_body_bytes == 0
    assert s.j.projection.unknown_size_objects == 0


def test_capacity_exact_boundary_and_plus_one():
    s = Scenario(StoragePolicy(100, 160, 160, 100, 10000)).begin().complete("next")
    s.begin(1, "next")
    assert (
        s.j.projection.retained_body_bytes + s.j.projection.reserved_remaining_bytes
        == 160
    )
    s = Scenario(StoragePolicy(100, 159, 159, 100, 10000)).begin().complete("next")
    with pytest.raises(HttpContractError, match="budget_exceeded"):
        s.begin(1, "next")


def test_unknown_charge_limits_next_attempt_even_after_stabilization():
    s = (
        Scenario(StoragePolicy(100, 199, 1000, 1000, 10000))
        .begin()
        .opened()
        .observed(20, state="partial")
        .received("unknown")
        .closed()
        .stabilized()
    )
    with pytest.raises(HttpContractError, match="cumulative_budget_exceeded"):
        s.begin()


def test_overlimit_discovery_is_preserved_and_blocks_new_work():
    s = Scenario().begin().opened().observed(1100, state="orphan")
    assert s.j.projection.retained_body_bytes == 1100
    assert s.result().classification == "inconsistent"


@pytest.mark.parametrize(
    "value", [True, False, -1, 0, 1.0, float("nan"), MAX_INT64 + 1]
)
def test_policy_integer_boundaries(value):
    with pytest.raises(HttpContractError):
        StoragePolicy(value, 1000, 1000, 1000, 10000)


def test_capacity_addition_overflow_rejected():
    s = (
        Scenario(StoragePolicy(MAX_INT64, MAX_INT64, MAX_INT64, MAX_INT64, MAX_INT64))
        .begin()
        .complete("next")
    )
    with pytest.raises(HttpContractError, match="integer_invalid"):
        s.begin(1, "next")


@pytest.mark.parametrize(
    "at", [datetime(2040, 1, 1), NOW.astimezone(timezone(timedelta(hours=9)))]
)
def test_noncanonical_utc_rejected(at):
    s = Scenario()
    with pytest.raises(HttpContractError, match="canonical_utc"):
        replace(s.root, observed_at=at)


@pytest.mark.parametrize(
    "data",
    [
        b'{"schema":1,"schema":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1.5}',
        b'{"x":' + b"9" * 5000 + b"}",
        b"{" + b" " * MAX_RECORD_BYTES + b"}",
    ],
)
def test_json_malformed_or_unbounded_rejected(data):
    with pytest.raises(HttpContractError):
        StoragePolicy.from_bytes(data)


def test_schema_unknown_fields_and_permission_injection():
    p = contract().policy.to_dict()
    for key, value in [("schema", "legacy-v2"), ("extra", "unexpected")]:
        with pytest.raises(HttpContractError):
            StoragePolicy.from_dict({**p, key: value})
    a = BodyAssessment("consistent", ())
    with pytest.raises(HttpContractError, match="permission"):
        BodyAssessment.from_dict({**a.to_dict(), "live_send_permitted": True})
    with pytest.raises(TypeError):
        BodyAssessment("consistent", (), live_send_permitted=True)


@pytest.mark.parametrize("reference", ["x\nvalue", "x\u202evalue", "x\x00value"])
def test_reference_control_characters(reference):
    with pytest.raises(HttpContractError, match="control"):
        replace(Scenario().root, evidence_ref=reference)


def test_expired_plan_audit_allowed_reservation_denied():
    s = Scenario()
    at = NOW + timedelta(hours=2)
    j = s.j.audit(evidence_ref="expired-audit", at=at, operation_id="audit")
    assert len(j.records) == 1
    s.begin()
    e = BodyEvent.from_dict(json.loads(s.j.records[0])["event"])
    empty = LiveBodyJournal(s.c, s.root)
    with pytest.raises(HttpContractError, match="outside_plan_window"):
        empty.append(replace(e, recorded_at=at))


def test_required_c2_journals_and_historical_heads():
    s = Scenario().begin().complete()
    assert (
        reconcile_live_bodies(s.j, account=None, plans=()).classification == "pending"
    )
    assert (
        reconcile_live_bodies(s.j, account=s.a, plans=()).classification
        == "inconsistent"
    )
    assert (
        reconcile_live_bodies(s.j, account=s.a, plans=(s.p, s.p)).classification
        == "inconsistent"
    )


def test_roundtrip_replay_idempotency_and_tamper():
    s = Scenario().begin().complete()
    data = s.j.to_bytes()
    loaded = LiveBodyJournal.from_bytes(
        data, expected_contract=s.c, expected_root=s.root
    )
    assert loaded == s.j
    assert loaded.projection == s.j.projection
    event = BodyEvent.from_dict(json.loads(s.j.records[-1])["event"])
    assert loaded.append(event) is loaded
    assert loaded.projection.cumulative_acquired_bytes == 60
    with pytest.raises(HttpContractError):
        LiveBodyJournal.from_bytes(
            data[:-1], expected_contract=s.c, expected_root=s.root
        )
    lines = list(s.j.records)
    raw = json.loads(lines[-1])
    raw["previous_hash"] = "d" * 64
    lines[-1] = canonical(raw)
    with pytest.raises(HttpContractError, match="chain"):
        LiveBodyJournal(s.c, s.root, tuple(lines))


def test_stability_requires_matching_closed_writer_and_order():
    s = Scenario().begin().opened().observed().closed()
    evidence = s.stable_evidence()
    for change in [
        {"writer_evidence_sha": "f" * 64},
        {"root_identity_sha": "e" * 64},
        {"object_id": "d" * 64},
    ]:
        with pytest.raises(HttpContractError, match="binding_mismatch"):
            s.j.stabilize(
                s.oid, replace(evidence, **change), at=s.tick(), operation_id="bad"
            )
    with pytest.raises(HttpContractError, match="order_invalid"):
        replace(evidence, rehashed_at=NOW)


def test_live_gate_and_legacy_duplicate_digest_contract_unchanged():
    assert ACCOUNT_LEDGER_SCHEMA == "historical-feasibility-account-rate-ledger-v2"
    assert not LiveAcquisitionGate(()).permitted
    s = Scenario().begin().complete()
    r = s.result()
    assert not r.live_send_permitted
    assert not r.live_acquisition_permitted
    assert not r.identity_verified
    assert not r.store_implemented
    with pytest.raises(HttpContractError, match="duplicate_stored_body"):
        BodyInventory(
            (
                BodyFileEvidence("a", 60, "committed", DIGEST),
                BodyFileEvidence("b", 60, "committed", DIGEST),
            )
        )


def test_value_roundtrips_and_inputs_are_immutable():
    s = Scenario().begin().complete()
    obj = s.j.object(s.oid)
    for value in (s.root, obj, obj.writer, obj.stability, obj.receipt, s.j.projection):
        assert type(value).from_bytes(value.to_bytes()) == value
    raw = obj.to_dict()
    raw["identity"]["binding"]["holder_id"] = "other"
    assert s.j.object(s.oid).identity.binding.holder_id == "holder"
    assert LiveBodyObject.from_bytes(obj.to_bytes()) == obj


def test_c3_has_no_filesystem_network_clock_or_delete_operations():
    # Inspect the imported module's source as a test, not an operation by C3.
    import ast
    import inspect

    import feasibility.live_body_contract as module

    tree = ast.parse(inspect.getsource(module))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not imported & {
        "os",
        "pathlib",
        "socket",
        "sqlite3",
        "requests",
        "httpx",
        "time",
    }
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not calls & {
        "resolve",
        "stat",
        "unlink",
        "rename",
        "getenv",
        "now",
        "utcnow",
    }
    assert not any(
        name in dir(LiveBodyJournal) for name in ("delete", "send", "reclaim", "gc")
    )


def test_second_authoritative_commit_rejected_by_replay_not_only_reserve():
    s = (
        Scenario()
        .begin()
        .opened()
        .observed()
        .received()
        .closed()
        .stabilized()
        .acquired()
    )
    old = s.j
    s.committed()
    first_event = BodyEvent.from_dict(json.loads(s.j.records[-1])["event"])
    s.j = old
    s.begin().complete()
    receipt = LiveBodyReceipt.from_dict(json.loads(first_event.payload_json))
    at = s.tick()
    receipt = replace(receipt, committed_at=at)
    event = replace(
        first_event, recorded_at=at, payload_json=canonical(receipt.to_dict())
    )
    with pytest.raises(HttpContractError, match="page_already_committed"):
        s.j.append(event)


@pytest.mark.parametrize("status", [429, 503])
def test_non200_complete_body_counts_but_not_authoritative_success(status):
    s = (
        Scenario()
        .begin()
        .opened()
        .observed(state="orphan")
        .received(status=status)
        .closed()
        .stabilized()
        .acquired()
    )
    assert s.j.projection.cumulative_acquired_bytes == 60
    with pytest.raises(HttpContractError, match="success_response_required"):
        s.committed()


def test_stale_writer_generation_does_not_imply_termination():
    s = Scenario().begin().opened().observed(state="partial").received("unknown")
    old_writer = s.j.object(s.oid).writer
    with pytest.raises(HttpContractError, match="reservation_pending"):
        s.begin()
    assert (
        s.a.projection.generation == 2
    )  # newer C2 slot exists in this artificial input
    assert s.j.projection.objects[0].writer == old_writer
    assert s.j.projection.unstable


def test_all_account_plans_require_body_journals_and_bindings():
    s = Scenario()
    other_raw = s.c.plan.content
    other_raw["artifact_id"] = "c3-other"
    other_raw["output_dir"] = str(HTTP_OUTPUT_ROOT / "c3-other")
    text = canonical(other_raw)
    other_c = replace(
        s.c, plan=FixedPlanSnapshot(text, hashlib.sha256(text.encode()).hexdigest())
    )
    other_scope = replace(s.scope, plan_sha=other_c.plan.plan_sha)
    a = LiveJournal.create(LIVE_ACCOUNT_SCHEMA, (s.scope, other_scope), recorded_at=NOW)
    p2 = LiveJournal.create(LIVE_PLAN_SCHEMA, (other_scope,), recorded_at=NOW)
    missing = reconcile_live_bodies(s.j, account=a, plans=(s.p, p2))
    assert missing.classification == "pending"
    assert "required_related_body_journal_missing" in missing.reasons
    other_root = replace(
        s.root,
        root_binding_sha=other_c.root_binding_sha,
        canonical_realpath=other_c.root,
    )
    other_j = LiveBodyJournal(other_c, other_root)
    assert (
        reconcile_live_bodies(
            s.j, account=a, plans=(s.p, p2), related_bodies=(other_j,)
        ).classification
        == "consistent"
    )
    wrong_c = replace(other_c, preflight_sha="f" * 64)
    wrong_j = LiveBodyJournal(
        wrong_c, replace(other_root, root_binding_sha=wrong_c.root_binding_sha)
    )
    assert (
        reconcile_live_bodies(
            s.j, account=a, plans=(s.p, p2), related_bodies=(wrong_j,)
        ).classification
        == "inconsistent"
    )


def test_body_unknown_size_no_zero_in_assessment():
    s = Scenario().begin().opened().observed(None, None, "partial")
    assert s.result().classification == "pending"
    assert s.j.projection.retained_body_bytes is None


def test_stable_receipt_roundtrip_and_legacy_schema_rejected():
    s = Scenario().begin().opened().observed(state="partial").closed().stabilized()
    q = s.quarantine_evidence()
    assert QuarantineEvidence.from_bytes(q.to_bytes()) == q
    s.j = s.j.quarantine_object(q)
    assert (
        LiveBodyJournal.from_bytes(
            s.j.to_bytes(), expected_contract=s.c, expected_root=s.root
        )
        == s.j
    )
    data = q.to_dict()
    data["schema"] = ACCOUNT_LEDGER_SCHEMA
    with pytest.raises(HttpContractError, match="schema_invalid"):
        QuarantineEvidence.from_dict(data)


def test_observation_cannot_erase_lower_bound_after_unknown_size():
    s = Scenario().begin().opened().observed(20).observed(None, None, "partial")
    with pytest.raises(HttpContractError, match="retained_size_regressed"):
        s.observed(19, state="partial")
    assert s.j.projection.retained_body_bytes is None
    assert s.j.projection.retained_observed_minimum_bytes == 20


def test_future_c2_recording_cannot_be_used_by_backdated_body_event():
    s = Scenario().begin().opened().observed().received().closed().stabilized()
    old_at = NOW + timedelta(seconds=s.n)
    hold = s.a.projection.holds[0].hold
    future = replace(hold, recorded_at=NOW + timedelta(seconds=s.n + 100))
    s.a = s.a.append(
        "hold_extended",
        recorded_at=future.recorded_at,
        transition_id="late-c2-record",
        hold=future.to_dict(),
    )
    with pytest.raises(HttpContractError, match="head_recorded_after_body_event"):
        s.j.acquired(
            s.oid,
            size=60,
            evidence_ref="completion",
            at=old_at,
            operation_id="backdated",
            account=s.a,
            plans=(s.p,),
        )


def test_snapshot_and_codec_use_no_legacy_plan_constructor(monkeypatch):
    c = contract()

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy constructor / filesystem path validator called")

    monkeypatch.setattr(HttpAcquisitionPlan, "__post_init__", forbidden)
    import feasibility.http_contract as legacy

    monkeypatch.setattr(legacy, "_output_path", forbidden)
    assert LiveBodyStorageContract.from_bytes(c.to_bytes()) == c


def test_old_c2_prefix_cannot_hide_generation_already_advanced_before_commit():
    s = (
        Scenario()
        .begin()
        .opened()
        .observed()
        .received()
        .closed()
        .stabilized()
        .acquired()
    )
    before = s.j
    s.committed()
    event = BodyEvent.from_dict(json.loads(s.j.records[-1])["event"])
    s.j = before
    s.begin()  # current C2 generation is now newer; old object remains uncommitted
    at = s.tick()
    receipt = LiveBodyReceipt.from_dict(json.loads(event.payload_json))
    event = replace(
        event,
        recorded_at=at,
        payload_json=canonical(replace(receipt, committed_at=at).to_dict()),
    )
    s.j = s.j.append(event)  # importing a claim is not attesting that it is consistent
    result = s.result()
    assert result.classification == "inconsistent"
    assert "c3_historical_generation_already_superseded" in result.reasons


def sent_without_writer(outcome=None):
    """Model a crash window: C2 advanced, but no C3 writer event was recorded."""
    s = Scenario().begin()
    at = s.tick()
    s.pair("attempt_sent", "slot_sent", at, sent_at=stamp(at))
    if outcome is not None:
        s.received(outcome)
    return s


def test_writer_evidence_not_required_for_presend_reservation():
    s = Scenario().begin()
    before = s.j.to_bytes()
    assert s.j.object(s.oid).writer.state == "not_started"
    assert s.a.projection.attempt(s.b.plan_sha, s.b.attempt_id).sent_at is None
    assert s.result().classification == "consistent"
    assert "writer_evidence_missing" not in s.result().reasons
    assert s.j.to_bytes() == before


@pytest.mark.parametrize(
    "outcome", [None, "response", "unknown"], ids=["sent", "complete-200", "unknown"]
)
def test_sent_without_writer_is_pending_and_next_reservation_rejected(outcome):
    s = sent_without_writer(outcome)
    assert s.j.object(s.oid).writer.state == "not_started"
    before = s.j.to_bytes()
    result = s.result()
    assert result.classification == "pending"
    assert "writer_evidence_missing" in result.reasons
    assert not result.live_send_permitted
    assert not result.live_acquisition_permitted
    if outcome is None:
        # C2 still has an open attempt. Prove C3 rejects at its own gate first.
        with pytest.raises(HttpContractError, match="c3_reservation_pending"):
            s.j.reserve(
                s.identity,
                at=s.tick(),
                operation_id="next-body",
                account=s.a,
                plans=(s.p,),
            )
    else:
        if outcome == "unknown":
            assert any(
                h.hold.release_mode == "manual" and h.released_at is None
                for h in s.a.projection.holds
            )
        # The fixture explicitly reviews/releases C2's hold and creates a new
        # valid C2 reservation. Missing C3 evidence must still block its body.
        with pytest.raises(HttpContractError, match="c3_reservation_pending"):
            s.begin()
        assert s.a.projection.attempt(s.b.plan_sha, s.b.attempt_id).state == "reserved"
    assert s.j.to_bytes() == before  # gate and reconciliation are nonmutating
    assert s.j.projection.unresolved_acquisition_charge == 100


def test_writer_missing_is_detected_from_one_sided_plan_send():
    s = Scenario().begin()
    at = s.tick()
    s.p = s.p.append(
        "attempt_sent",
        recorded_at=at,
        transition_id="one-sided-send",
        binding=s.b,
        sent_at=stamp(at),
    )
    result = s.result()
    assert result.classification == "pending"
    assert "writer_evidence_missing" in result.reasons


@pytest.mark.parametrize("state", ["open", "closed", "stable"])
def test_missing_writer_evidence_recovery_requires_close_and_measurement(state):
    s = sent_without_writer("response")
    assert "writer_evidence_missing" in s.result().reasons
    w = WriterEvidence(s.b, "recovered-writer", "open", s.tick(), 0)
    s.j = s.j.writer(s.oid, w, operation_id="recovered-open")
    assert "writer_evidence_missing" not in s.result().reasons
    if state in {"closed", "stable"}:
        s.closed()
    if state == "stable":
        s.stabilized(size=0, digest=hashlib.sha256(b"").hexdigest())
        assert s.result().classification == "consistent"
        assert not s.j.projection.unstable
        s.begin()  # no other constraints: the next body reservation is possible
        assert len(s.j.projection.objects) == 2
    else:
        assert s.j.projection.unstable
        assert s.result().classification == "pending"
        with pytest.raises(HttpContractError, match="c3_reservation_pending"):
            s.begin()


@pytest.mark.parametrize(
    "output_dir",
    [
        str(HTTP_OUTPUT_ROOT),
        "/tmp/x",
        "/var/tmp/x",
        "/etc/x",
        str(HTTP_OUTPUT_ROOT.parents[2]),
        "/Users/example/stock_range_trader",
        "/opt/outside/x",
        str(HTTP_OUTPUT_ROOT) + "-lookalike/c3-artificial",
        str(HTTP_OUTPUT_ROOT / "c3-artificial" / "nested"),
        str(HTTP_OUTPUT_ROOT / "different-artifact"),
    ],
)
def test_snapshot_rejects_output_outside_dedicated_plan_directory(output_dir):
    raw = contract().plan.content
    raw["output_dir"] = output_dir
    text = canonical(raw)
    # Rehash intentionally: validation must reject the boundary, not a stale hash.
    with pytest.raises(
        HttpContractError, match="output_dir_outside_dedicated_http_root"
    ):
        FixedPlanSnapshot(text, hashlib.sha256(text.encode()).hexdigest())


def test_snapshot_valid_dedicated_root_is_pure_even_when_constructed_directly(
    monkeypatch,
):
    from pathlib import Path

    c = contract()
    raw = c.plan.content
    raw["artifact_id"] = "another-dedicated-plan"
    raw["output_dir"] = str(HTTP_OUTPUT_ROOT / raw["artifact_id"])
    text = canonical(raw)

    def forbidden(*args, **kwargs):
        raise AssertionError("C3 snapshot performed filesystem lookup")

    with monkeypatch.context() as m:
        for method in ("exists", "resolve", "stat", "is_symlink"):
            m.setattr(Path, method, forbidden)
        direct = FixedPlanSnapshot(text, hashlib.sha256(text.encode()).hexdigest())
        assert direct.content["output_dir"] == raw["output_dir"]
        assert FixedPlanSnapshot.from_bytes(direct.to_bytes()) == direct


@pytest.mark.parametrize("artifact_id", ["../x", "/tmp/x", "nested/x", "", True])
def test_snapshot_rejects_invalid_dedicated_artifact_name(artifact_id):
    raw = contract().plan.content
    raw["artifact_id"] = artifact_id
    text = canonical(raw)
    with pytest.raises(HttpContractError, match="artifact_id_invalid"):
        FixedPlanSnapshot(text, hashlib.sha256(text.encode()).hexdigest())
