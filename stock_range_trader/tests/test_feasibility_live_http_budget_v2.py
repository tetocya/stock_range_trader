"""Saved B, capacity and arithmetic counterexamples; no market data."""

import hashlib
import json
from dataclasses import replace

import pytest
from test_feasibility_live_http_admission_history import LIMITS, History, verified
from test_feasibility_live_http_budget import Fixture

from feasibility import http_contract as hc
from feasibility import live_body_contract as c3
from feasibility import live_http_admission_history as h
from feasibility import live_http_budget as v1
from feasibility import live_http_budget_v2 as v2
from feasibility import live_store as store


def saved(f):
    return v2.saved_compatibility_v2(
        f.j, account=f.a, plans=(f.p,), originals=tuple(f.measurements)
    )


@pytest.mark.parametrize("size", [0, 60])
def test_B_quarantine_duplicate_digest_nonzero_and_zero(size):
    f = History().begin().settle(size=size).begin().settle(status=429, size=size)
    assert saved(f).compatible
    assert saved(f).saved_total == 0
    assert f.j.projection.retained_body_bytes == 2 * size


def test_C_success_success_digest_duplicate_is_rejected():
    f = (
        History()
        .begin()
        .settle(status=200, size=10, next_cursor="second")
        .begin(1, "second")
        .settle(status=200, size=10)
    )
    assert not saved(f).compatible
    assert "successful_digest_duplicate" in saved(f).reasons
    assert f.j.projection.retained_body_bytes == 20
    f.clock()
    with pytest.raises(hc.HttpContractError, match="successful_digest_duplicate"):
        h.build_admission_v2(f.pool(), limits=LIMITS, cut=f.cut(), page=f.identity.page)


def test_D_committed_quarantine_same_digest_counts_objects_independently():
    f = History().begin().settle(size=10).begin().settle(status=200, size=10)
    result = saved(f)
    assert result.compatible and result.saved_total == 10
    assert f.j.projection.retained_body_bytes == 20
    assert f.j.projection.cumulative_acquired_bytes == 20
    assert verified(f).historical_admissions_valid


def test_digest_size_conflict_rejected_even_for_quarantines():
    f = History().begin().settle(size=1).begin().settle(size=2)
    assert "final_digest_size_conflict" in saved(f).reasons


@pytest.mark.parametrize(
    "partial,overrun", [(None, False), (0, False), (500, False), (501, True)]
)
def test_I_unknown_partial_single_charge_and_sticky_plus_one(partial, overrun):
    f = History().begin().settle(outcome="unknown", partial=partial)
    f.release()
    f.clock()
    pool = f.pool()
    history, state = h._evaluate(pool, LIMITS, True, h.ReplayStatistics())
    assert history.historical_admissions_valid
    index, _, prior, originals, allowances, _ = state
    _, assessment, _, _ = h._core(
        pool, index, f.cut(), f.identity.page, originals, allowances, prior
    )
    assert assessment.budget.charged_transfer == 500
    assert assessment.budget.charged_decoded == 600
    assert assessment.budget.known_transfer == assessment.budget.known_decoded == 0
    assert ("sticky_overrun" in assessment.reasons) == overrun


@pytest.mark.parametrize(
    "stage", ["open", "unknown", "closed", "partial", "uncommitted200", "unknown_size"]
)
def test_L_nonfinal_body_states_fail_closed(stage):
    f = (
        Fixture()
        .begin()
        .opened()
        .received(status=200 if stage == "uncommitted200" else 503)
    )
    if stage == "unknown":
        w = replace(f.j.object(f.oid).writer, state="unknown", observed_at=f.tick())
        f.j = f.j.writer(f.oid, w, operation_id="writer-unknown")
    else:
        f.observed(
            size=None if stage == "unknown_size" else 0,
            digest=hashlib.sha256(b"").hexdigest(),
            state="partial",
        )
    if stage in {"closed", "partial", "uncommitted200"}:
        f.closed()
    if stage in {"partial", "uncommitted200"}:
        f.stabilized()
    f.measurement(transfer=0, decoded=0, sink=0)
    result = saved(f)
    assert not result.compatible


def test_untouched_with_sent_evidence_is_not_zero_byte_proof():
    f = Fixture().begin()
    at = f.tick()
    f.pair(
        "attempt_sent", "slot_sent", at, sent_at=at.isoformat(timespec="microseconds")
    )
    result = saved(f)
    assert not result.compatible and "untouched_send_evidence" in result.reasons


@pytest.mark.parametrize("budget,expected", [(200, True), (199, False)])
def test_G_H_stopped_other_plan_capacity_and_exact_account_boundary(budget, expected):
    f = History()
    contract = f.c
    empty = c3._projection({})
    other = replace(
        empty,
        retained_body_bytes=100,
        retained_observed_minimum_bytes=100,
        cumulative_acquired_bytes=100,
    )
    other_sha = "f" * 64
    result = v2.assess_capacity_v2(
        {contract.plan.plan_sha: contract, other_sha: contract},
        {contract.plan.plan_sha: empty, other_sha: other},
        target_plan_sha=contract.plan.plan_sha,
        store_policy=store.StorePolicy(4, 32768, budget, budget),
    )
    assert result.satisfied is expected
    assert result.retained_with_reservation == result.acquired_with_reservation == 200


@pytest.mark.parametrize(
    "field", ["retained_body_budget", "reserved_capacity_budget", "cumulative_budget"]
)
def test_L2C8_plan_limits_apply_even_when_account_has_room(field):
    f = History()
    base = f.c
    projection = replace(
        c3._projection({}),
        retained_body_bytes=1,
        retained_observed_minimum_bytes=1,
        reserved_remaining_bytes=1,
        cumulative_acquired_bytes=1,
    )
    exact = 102 if field == "retained_body_budget" else 101
    for bound, expected in ((exact, True), (exact - 1, False)):
        contract = replace(base, policy=replace(base.policy, **{field: bound}))
        result = v2.assess_capacity_v2(
            {base.plan.plan_sha: contract},
            {base.plan.plan_sha: projection},
            target_plan_sha=base.plan.plan_sha,
            store_policy=store.StorePolicy(4, 32768, 10000, 10000),
        )
        assert result.satisfied is expected


def test_other_plan_policy_not_only_target_and_unknown_size():
    f = History()
    sha = f.c.plan.plan_sha
    p = replace(c3._projection({}), retained_body_bytes=1001)
    result = v2.assess_capacity_v2(
        {sha: f.c, "f" * 64: f.c},
        {sha: c3._projection({}), "f" * 64: p},
        target_plan_sha=sha,
        store_policy=store.StorePolicy(4, 32768, 10000, 10000),
    )
    assert not result.satisfied and "plan_retained_capacity" in result.reasons
    with pytest.raises(hc.HttpContractError, match="unknown_retained_size"):
        v2.assess_capacity_v2(
            {sha: f.c},
            {sha: replace(p, retained_body_bytes=None)},
            target_plan_sha=sha,
            store_policy=f.policy,
        )


def test_int64_capacity_overflow_rejected():
    f = History()
    sha = f.c.plan.plan_sha
    p = replace(c3._projection({}), retained_body_bytes=c3.MAX_INT64)
    with pytest.raises(hc.HttpContractError, match="integer"):
        v2.assess_capacity_v2(
            {sha: f.c}, {sha: p}, target_plan_sha=sha, store_policy=f.policy
        )


def test_M_v1_literal_policy_and_unknown_version_isolation():
    assert (
        v1.DEFAULT_POLICY.to_bytes()
        == b'{"rule":"legacy-http-full-page-unknown-no-saved-charge","schema":"historical-feasibility-http-budget-policy-v1"}'
    )
    assert (
        v1.DEFAULT_POLICY.sha256
        == "37dbd690b8207d7e78407206cb91654b38e1d31c1132c21443227f6f85ceb03a"
    )
    with pytest.raises(hc.HttpContractError, match="schema"):
        v1.BudgetPolicy.from_bytes(v2.BudgetPolicyV2().to_bytes())
    with pytest.raises(hc.HttpContractError, match="schema"):
        v2.BudgetPolicyV2.from_bytes(v1.DEFAULT_POLICY.to_bytes())
    with pytest.raises(hc.HttpContractError, match="core_v2_required"):
        h.derive_slot_id_v2(object(), "operation")


def test_K_rehashing_quarantine_with_later_writer_does_not_make_it_legal():
    f = History().begin().settle(early_quarantine=True)
    obj = f.j.projection.objects[0]
    records = []
    previous = c3.ZERO_HASH
    for line in f.j.records:
        raw = json.loads(line)
        raw["previous_hash"] = previous
        if raw["event"]["kind"] == "quarantined":
            payload = json.loads(raw["event"]["payload_json"])
            payload["writer"] = obj.writer.to_dict()
            payload["object_version"] = obj.version
            payload["digest_class"] = "final"
            raw["event"]["payload_json"] = c3._canonical(payload)
        raw["event_hash"] = c3._hash(
            {k: v for k, v in raw.items() if k != "event_hash"}
        )
        previous = raw["event_hash"]
        records.append(c3._canonical(raw))
    with pytest.raises(hc.HttpContractError, match="quarantine_evidence_mismatch"):
        c3.LiveBodyJournal(f.c, f.root, tuple(records))


def test_same_attempt_second_object_and_direct_reserved_quarantine_rejected():
    f = History().begin()
    identity = replace(
        f.identity, page=replace(f.identity.page, page_index=1, cursor="next")
    )
    with pytest.raises(hc.HttpContractError, match="attempt_already_has_object"):
        f.j.reserve(
            identity,
            at=f.tick(),
            operation_id="second-object",
            account=f.a,
            plans=(f.p,),
        )
    obj = f.j.object(f.oid)
    # Invalid raw event, not a positive fixture built through quarantine API.
    q = c3.QuarantineEvidence(
        "bad-q",
        f.c.sha256,
        f.identity,
        obj.version,
        f.root.sha256,
        f.c.layout()["staging"] + "/" + f.oid,
        f.c.layout()["quarantine"] + "/" + f.oid,
        0,
        None,
        "provisional",
        "partial",
        "reason",
        "ref",
        obj.writer,
        f.tick(),
    )
    with pytest.raises(
        hc.HttpContractError, match="quarantine_requires_partial_or_orphan"
    ):
        f.j.quarantine_object(q)


def test_v2_unknown_multiplication_overflow_is_not_wrapped():
    f = History()
    with pytest.raises(hc.HttpContractError, match="integer"):
        v1.calculate_allowances(f.src["plan"].limits, 0, 0, c3.MAX_INT64, 0)
