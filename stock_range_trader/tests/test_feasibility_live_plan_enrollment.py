"""Artificial, in-memory enrollment evidence; no activation or live acquisition."""

import builtins
import hashlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_feasibility_http_contract import approval as approval_fixture
from test_feasibility_live_preflight import NOW
from test_feasibility_live_preflight import plan as plan_fixture
from test_feasibility_live_preflight import preflight as preflight_fixture

from feasibility.http_contract import (
    ACCOUNT_LEDGER_SCHEMA,
    HTTP_OUTPUT_ROOT,
    HttpContractError,
)
from feasibility.live_http_evidence import (
    LIVE_ACCOUNT_SCHEMA,
    LIVE_ACCOUNT_SCHEMA_V1,
    LIVE_ACCOUNT_SCHEMA_V2,
    LIVE_PLAN_SCHEMA,
    MAX_EVENT_BYTES,
    AttemptBinding,
    LiveJournal,
    _canonical,
    capture_headers,
    check_body_generation,
    reconcile_live_journals,
    required_attempt_hold,
)
from feasibility.live_plan_enrollment import (
    authority_from_preflight,
    derive_enrollment_intent,
    propose_plan_enrollment,
)


def at(n=0):
    return NOW + timedelta(seconds=n)


def stamp(value):
    return value.isoformat(timespec="microseconds")


def sources(name="plan-a", **changes):
    p = plan_fixture(
        artifact_id=name, output_dir=str(HTTP_OUTPUT_ROOT / name), **changes
    )
    c = preflight_fixture(p)
    a = approval_fixture(p, valid_from=p.not_before, valid_until=p.expires_at)
    return dict(
        plan=p,
        preflight=c,
        approval=a,
        canonical_plan=_canonical(p.to_dict()),
        canonical_preflight=_canonical(c.to_dict()),
        canonical_approval=_canonical(a.to_dict()),
    )


def empty(src=None):
    src = src or sources()
    authority = authority_from_preflight(
        src["preflight"], canonical_preflight=src["canonical_preflight"]
    )
    return LiveJournal.create_account_v2(authority, recorded_at=at())


def enroll(
    account,
    plans=(),
    name="plan-a",
    operation="enroll-a",
    when=None,
    src=None,
    **changes,
):
    args = dict(
        expected_account_head=account.head_sha,
        operation_id=operation,
        at=when or at(),
        **(src or sources(name)),
    )
    args.update(changes)
    return propose_plan_enrollment(account, plans, **args)


def first():
    return enroll(empty())


def pair(p, a, b, pk, ak, n, op, **data):
    return (
        p.append(pk, recorded_at=at(n), transition_id=op, binding=b, **data),
        a.append(ak, recorded_at=at(n), transition_id=op, binding=b, **data),
    )


def progress(stage="response", status=200, retry=()):
    result = first()
    p, a = result.plan, result.account
    s = p.projection.scopes[0]
    b = AttemptBinding(
        s.plan_sha,
        s.preflight_sha,
        s.account_ref,
        s.approval_sha,
        "attempt-a",
        "slot-a",
        "holder-a",
        1,
    )
    p, a = pair(
        p,
        a,
        b,
        "attempt_reserved",
        "slot_reserved",
        1,
        "reserve",
        reserved_at=stamp(at(1)),
    )
    if stage == "reserved":
        return p, a, b
    p, a = pair(p, a, b, "attempt_sent", "slot_sent", 2, "send", sent_at=stamp(at(2)))
    if stage == "sent":
        return p, a, b
    if stage != "unknown":
        header = capture_headers(
            observation_id="header-a",
            status=status,
            header_block_complete=True,
            observed_at=at(3),
            retry_after_fields=retry,
        )
        p, a = pair(
            p,
            a,
            b,
            "response_headers_observed",
            "slot_headers_observed",
            3,
            "header",
            observation=header.to_dict(),
        )
        a = sync_hold(a, b, 3)
        if stage == "headers":
            return p, a, b
    outcome = "unknown" if stage == "unknown" else "response"
    p, a = pair(
        p,
        a,
        b,
        "outcome_unknown" if stage == "unknown" else "response_received",
        "slot_settled",
        4,
        "settle",
        outcome=outcome,
        status=None if stage == "unknown" else status,
        settled_at=stamp(at(4)),
    )
    a = sync_hold(a, b, 4)
    return p, a, b


def sync_hold(a, b, n):
    hold = required_attempt_hold(
        a.projection.scope(b.plan_sha), a.projection.attempt(b.plan_sha, b.attempt_id)
    )
    if hold is None:
        return a
    hold = replace(hold, recorded_at=at(n))
    kind = (
        "hold_extended"
        if any(h.hold.hold_id == hold.hold_id for h in a.projection.holds)
        else "hold_entered"
    )
    return a.append(
        kind, recorded_at=at(n), transition_id=f"hold-{n}", hold=hold.to_dict()
    )


def release(a, n):
    for state in a.projection.holds:
        a = a.append(
            "hold_released",
            recorded_at=at(n),
            transition_id="review-" + state.hold.hold_id,
            hold_id=state.hold.hold_id,
            expected_hold_sha256=state.hold.hold_version_sha256,
            clock_reference="clock-evidence",
            account_state_reference="account-evidence",
            manual_review_reference="owner-review",
            repair_reference=None,
        )
    return a


def test_empty_open_and_first_two_enrollments_historical_scope():
    a0 = empty()
    assert a0.projection.opened and a0.projection.scopes == ()
    assert a0.projection.enrollment_revision == a0.projection.generation == 0
    assert reconcile_live_journals(a0, ()).classification == "consistent"
    a = enroll(a0)
    b = enroll(a.account, (a.plan,), "plan-b", "enroll-b")
    assert b.account.projection.enrollment_revision == 2
    assert b.account.projection.generation == 0
    assert b.account.prefix(a0.head_sha).projection.scopes == ()
    old = b.account.prefix(a.account.head_sha)
    assert old.projection.scopes == a.account.projection.scopes
    assert reconcile_live_journals(old, (a.plan,)).classification == "consistent"
    assert (
        reconcile_live_journals(old, (a.plan, b.plan)).classification == "inconsistent"
    )
    assert (
        reconcile_live_journals(b.account, (a.plan,)).classification == "inconsistent"
    )
    assert (
        reconcile_live_journals(b.account, (a.plan, b.plan)).classification
        == "consistent"
    )
    assert b.receipt.prior_plan_heads == (
        (a.plan.projection.scopes[0].plan_sha, a.plan.head_sha),
    )
    assert a.receipt.event_parts == ("account_enrollment", "plan_opening")


def test_empty_cannot_reserve_and_double_open_rejected():
    a = empty()
    with pytest.raises(HttpContractError):
        a.append("slot_reserved", recorded_at=at(), transition_id="reserve")
    with pytest.raises(HttpContractError, match="duplicate_open"):
        a.append(
            "ledger_opened",
            recorded_at=at(),
            transition_id="open2",
            authority=a.projection.authority.to_dict(),
        )


def test_idempotent_retry_preserves_original_receipt_even_after_expiry():
    a = first()
    b = enroll(a.account, (a.plan,), "plan-b", "enroll-b")
    again = enroll(b.account, (a.plan, b.plan), when=at(9000))
    assert again.replayed and again.receipt == a.receipt
    assert again.account == b.account and again.plan == a.plan
    assert not again.live_send_permitted and not again.live_acquisition_permitted
    assert (
        not again.receipt.live_send_permitted
        and not again.receipt.live_acquisition_permitted
    )


@pytest.mark.parametrize(
    "name,op,error",
    [
        ("plan-b", "enroll-a", "intent_conflict"),
        ("plan-a", "different", "already_enrolled"),
        ("plan-b", "opened", "collision"),
    ],
)
def test_operation_and_plan_conflicts(name, op, error):
    a = first()
    with pytest.raises(HttpContractError, match=error):
        enroll(a.account, (a.plan,), name, op)


@pytest.mark.parametrize(
    "field", ["canonical_plan", "canonical_preflight", "canonical_approval"]
)
def test_modified_canonical_content_rejected(field):
    src = sources()
    src[field] += " "
    with pytest.raises(HttpContractError, match="content_mismatch"):
        enroll(empty(), src=src)


def test_same_sha_modified_plan_rejected():
    src = sources()
    object.__setattr__(src["plan"], "account_ref", "other")
    with pytest.raises(HttpContractError, match="fixed_scope"):
        enroll(empty(), src=src)


@pytest.mark.parametrize("kind", ["account", "preflight", "policy", "clock"])
def test_authority_mismatch(kind):
    a = empty()
    auth = a.projection.authority
    if kind == "account":
        auth = replace(auth, account_ref="other")
    elif kind == "preflight":
        auth = replace(auth, preflight_sha="f" * 64)
    elif kind == "policy":
        auth = replace(
            auth,
            account_policy=replace(auth.account_policy, min_wait_after_429_seconds=200),
        )
    else:
        auth = replace(
            auth,
            clock_policy=replace(auth.clock_policy, max_reservation_to_send_seconds=6),
        )
    a = LiveJournal.create_account_v2(auth, recorded_at=at())
    with pytest.raises(HttpContractError, match="binding_mismatch"):
        enroll(a)


@pytest.mark.parametrize(
    "offset,accept", [(-1, False), (0, True), (3599, True), (3600, False)]
)
def test_approval_time_boundaries(offset, accept):
    src = sources()
    claim = replace(src["approval"], valid_from=at())
    src.update(approval=claim, canonical_approval=_canonical(claim.to_dict()))
    if accept:
        assert (
            enroll(
                empty(), src=src, when=at(offset)
            ).account.projection.enrollment_revision
            == 1
        )
    else:
        with pytest.raises(HttpContractError, match="validity_window"):
            enroll(empty(), src=src, when=at(offset))


@pytest.mark.parametrize("stage", ["reserved", "sent", "headers", "unknown"])
def test_inflight_and_unrecovered_unknown_reject(stage):
    p, a, _ = progress(stage)
    with pytest.raises(
        HttpContractError, match="in_flight|recovery_pending|reconciliation_required"
    ):
        enroll(a, (p,), "plan-b", "enroll-b", at(10))


@pytest.mark.parametrize(
    "status,retry", [(200, ()), (429, (b"600",)), (429, (b"malformed",)), (503, ())]
)
def test_hold_and_rate_history_preserved(status, retry):
    p, a, b = progress(status=status, retry=retry)
    before = a.projection
    result = enroll(a, (p,), "plan-b", "enroll-b", at(10))
    after = result.account.projection
    assert after.holds == before.holds
    assert after.attempts == before.attempts
    assert after.generation == before.generation == 1
    assert after.enrollment_revision == 2
    assert result.account.prefix(a.head_sha) == a
    check_body_generation(result.account, b)


def test_recovered_unknown_does_not_permanently_block():
    p, a, _ = progress("unknown")
    a = release(a, 130)
    result = enroll(a, (p,), "plan-b", "enroll-b", at(131))
    assert result.account.projection.holds == a.projection.holds
    assert result.account.projection.enrollment_revision == 2


def test_pre_send_reclaim_historical_reserved_is_legal():
    p, a, b = progress("reserved")
    evidence = dict(
        lease_expired_at=stamp(at(61)),
        observed_at=stamp(at(62)),
        clock_status="verified",
        previous_transport_termination_reference="terminated",
        operator_review_reference="review",
        process_restart_detected=False,
    )
    a = a.append(
        "slot_reclaimed",
        recorded_at=at(62),
        transition_id="reclaim",
        binding=b,
        evidence=evidence,
    )
    a = sync_hold(a, b, 62)
    assert p.projection.attempts[0].state == "reserved"
    assert reconcile_live_journals(a, (p,)).classification == "consistent"
    result = enroll(a, (p,), "plan-b", "enroll-b", at(63))
    assert result.account.projection.generation == a.projection.generation


def test_generation_next_request_after_enrollment():
    p, a, b = progress()
    a = release(a, 130)
    result = enroll(a, (p,), "plan-b", "enroll-b", at(131))
    scope = result.plan.projection.scopes[0]
    next_binding = replace(
        b,
        plan_sha=scope.plan_sha,
        approval_sha=scope.approval_sha,
        attempt_id="attempt-b",
        slot_id="slot-b",
        generation=2,
    )
    pp, aa = pair(
        result.plan,
        result.account,
        next_binding,
        "attempt_reserved",
        "slot_reserved",
        132,
        "reserve-b",
        reserved_at=stamp(at(132)),
    )
    assert aa.projection.generation == 2 and aa.projection.enrollment_revision == 2
    assert reconcile_live_journals(aa, (p, pp)).classification == "consistent"


def test_missing_pairs_rejected():
    a = first()
    assert (
        "plan_enrollment_missing_journal"
        in reconcile_live_journals(a.account, ()).reasons
    )
    assert (
        "plan_journal_not_enrolled"
        in reconcile_live_journals(empty(), (a.plan,)).reasons
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ("transition", "transition_mismatch"),
        ("time", "time_mismatch"),
        ("scope", "binding_mismatch"),
    ],
)
def test_opening_pair_mismatch(change, reason):
    a = first()
    scope = a.plan.projection.scopes[0]
    wrong = LiveJournal.create(
        LIVE_PLAN_SCHEMA,
        (replace(scope, approval_sha="f" * 64) if change == "scope" else scope,),
        recorded_at=at(1) if change == "time" else at(),
        transition_id="wrong" if change == "transition" else "enroll-a",
    )
    result = reconcile_live_journals(a.account, (wrong,))
    assert result.classification == "inconsistent"
    assert f"plan_enrollment_{reason}" in result.reasons
    assert "plan_enrollment_initial_head_mismatch" in result.reasons


def append_enrollment(a, scope, initial, prior, op="manual", **extra):
    return a.append(
        "plan_enrolled",
        recorded_at=at(200),
        transition_id=op,
        scope=scope.to_dict(),
        plan_journal_initial_head=initial,
        prior_plan_heads=prior,
        **extra,
    )


@pytest.mark.parametrize("kind", ["omit", "extra", "duplicate", "reorder"])
def test_prior_plan_set_shape_rejected(kind):
    a = first()
    b = enroll(a.account, (a.plan,), "plan-b", "enroll-b")
    intent = derive_enrollment_intent(**sources("plan-c"), at=at(200))
    prior = sorted(
        [
            dict(plan_sha=p.projection.scopes[0].plan_sha, head=p.head_sha)
            for p in (a.plan, b.plan)
        ],
        key=lambda x: x["plan_sha"],
    )
    if kind == "omit":
        prior.pop()
    if kind == "extra":
        prior.append(dict(plan_sha="f" * 64, head="e" * 64))
    if kind == "duplicate":
        prior.append(prior[0])
    if kind == "reorder":
        prior.reverse()
    with pytest.raises(HttpContractError, match="prior_heads_mismatch"):
        append_enrollment(b.account, intent.scope, "a" * 64, prior)


def test_wrong_prior_head_and_stale_account_rejected():
    a = first()
    src = sources("plan-b")
    intent = derive_enrollment_intent(**src, at=at(200))
    p = LiveJournal.create(
        LIVE_PLAN_SCHEMA, (intent.scope,), recorded_at=at(200), transition_id="manual"
    )
    bad = append_enrollment(
        a.account,
        intent.scope,
        p.head_sha,
        [dict(plan_sha=a.plan.projection.scopes[0].plan_sha, head="f" * 64)],
    )
    assert (
        "plan_enrollment_prior_heads_mismatch"
        in reconcile_live_journals(bad, (a.plan, p)).reasons
    )
    with pytest.raises(HttpContractError, match="head_stale"):
        enroll(empty(), expected_account_head=a.account.head_sha)


def test_inconsistent_and_pending_prior_evidence_rejected():
    p, a, b = progress("reserved")
    p = p.append(
        "attempt_sent",
        recorded_at=at(2),
        transition_id="send",
        binding=b,
        sent_at=stamp(at(2)),
    )
    with pytest.raises(HttpContractError, match="reconciliation_required"):
        enroll(a, (p,), "plan-b", "enroll-b", at(5))


@pytest.mark.parametrize(
    "schema", [LIVE_ACCOUNT_SCHEMA_V1, ACCOUNT_LEDGER_SCHEMA, "unknown"]
)
def test_no_schema_fallback(schema):
    a = empty()
    with pytest.raises(HttpContractError, match="schema_mismatch"):
        LiveJournal.from_bytes(a.to_bytes(), expected_schema=schema)
    assert (
        LiveJournal.from_bytes(a.to_bytes(), expected_schema=LIVE_ACCOUNT_SCHEMA_V2)
        == a
    )


def test_v1_unmodified_and_no_enrollment_or_migration():
    intent = derive_enrollment_intent(**sources(), at=at())
    assert LIVE_ACCOUNT_SCHEMA == LIVE_ACCOUNT_SCHEMA_V1
    a = LiveJournal.create(LIVE_ACCOUNT_SCHEMA, (intent.scope,), recorded_at=at())
    assert type(a.projection).__name__ == "LiveProjection"
    assert (
        LiveJournal.from_bytes(a.to_bytes(), expected_schema=LIVE_ACCOUNT_SCHEMA) == a
    )
    with pytest.raises(HttpContractError, match="live_event_unknown"):
        append_enrollment(a, intent.scope, "a" * 64, [])
    with pytest.raises(HttpContractError, match="schema_mismatch"):
        LiveJournal.from_bytes(a.to_bytes(), expected_schema=LIVE_ACCOUNT_SCHEMA_V2)
    with pytest.raises(HttpContractError, match="v2_required"):
        enroll(a)


def test_record_overflow_and_canonical_tamper():
    a = first()
    with pytest.raises(HttpContractError, match="size_invalid"):
        a.account.append(
            "plan_enrolled",
            recorded_at=at(),
            transition_id="large",
            padding="x" * MAX_EVENT_BYTES,
        )
    with pytest.raises(HttpContractError):
        LiveJournal.from_bytes(
            a.account.to_bytes().replace(b'"sequence":0', b'"sequence": 0'),
            expected_schema=LIVE_ACCOUNT_SCHEMA_V2,
        )
    with pytest.raises(HttpContractError):
        a.account.append(
            "plan_enrolled", recorded_at=at(), transition_id="enroll-a", malformed=True
        )


def test_pure_builder_never_reads_files_or_clock_or_credentials(monkeypatch):
    src = sources()
    a = empty(src)

    def forbidden(*args, **kwargs):
        raise AssertionError("I/O forbidden")

    import os
    import socket
    import time

    for name in ("exists", "resolve", "stat", "open"):
        monkeypatch.setattr(Path, name, forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(time, "time", forbidden)
    result = enroll(a, src=src)
    assert result.account.projection.enrollment_revision == 1
    assert not result.live_send_permitted


def test_v1_base_golden_bytes_hash_replay_and_projection():
    # Obtained independently by executing the fixed base's C2 module in memory:
    # d20cd70095fe8af0fa8b2c091b558c22c260b6b5. No baseline code runs in this test.
    from test_feasibility_live_http_evidence import sent

    p, a, _ = sent()
    assert (
        hashlib.sha256(a.to_bytes()).hexdigest()
        == "9d3d23c02b144c68e7cec03d4bdcaa1035c2a572c650e0d5b0497ed1fb9d7151"
    )
    assert (
        a.head_sha == "e73589544516872374799f8c8f98138727b14d378f2e74366cd55f1e0594ccf8"
    )
    assert (
        hashlib.sha256(p.to_bytes()).hexdigest()
        == "7cc8850e8f695534f63e2f7b281914039f2cd22a209df6e04acaf7000643cad5"
    )
    assert (
        p.head_sha == "1966198efa5aaca412b611fe2b6aec6ceabcf65a8d34267a0274fe8a7d2a9fe2"
    )
    for journal in (a, p):
        decoded = LiveJournal.from_bytes(
            journal.to_bytes(), expected_schema=journal.schema
        )
        assert decoded.projection == journal.projection
        assert decoded.to_bytes() == journal.to_bytes()
        assert decoded.projection.generation == 1
    assert reconcile_live_journals(a, (p,)).classification == "consistent"


def test_v2_reload_each_prefix_and_enrollment_revision():
    p, a, _ = progress()
    after = enroll(a, (p,), "plan-b", "enroll-b", at(10))
    loaded = LiveJournal.from_bytes(
        after.account.to_bytes(), expected_schema=LIVE_ACCOUNT_SCHEMA_V2
    )
    assert loaded == after.account
    assert loaded.projection == after.account.projection
    assert loaded.prefix(a.head_sha).projection.enrollment_revision == 1
    assert loaded.projection.enrollment_revision == 2
    assert (
        len(
            loaded.prefix(
                loaded.projection.enrollments[0].previous_account_head
            ).projection.scopes
        )
        == 0
    )
    assert (
        reconcile_live_journals(loaded, (p, after.plan)).classification == "consistent"
    )


def test_policy_must_cover_plan_retry():
    src = sources()
    p = replace(
        src["plan"], retry=replace(src["plan"].retry, min_wait_after_429_seconds=300)
    )
    claim = approval_fixture(p, valid_from=p.not_before, valid_until=p.expires_at)
    src.update(
        plan=p,
        approval=claim,
        canonical_plan=_canonical(p.to_dict()),
        canonical_approval=_canonical(claim.to_dict()),
    )
    with pytest.raises(HttpContractError, match="weaker_than_plan"):
        enroll(empty(), src=src)


def test_approval_scope_content_mismatch_not_just_hash():
    src = sources()
    claim = replace(src["approval"], reference_date="2025-03-03")
    src.update(approval=claim, canonical_approval=_canonical(claim.to_dict()))
    with pytest.raises(HttpContractError, match="approval_reference_date_mismatch"):
        enroll(empty(), src=src)


def test_swapped_initial_head_fails_closed():
    a = first()
    intent = derive_enrollment_intent(**sources("plan-b"), at=at(200))
    p = LiveJournal.create(
        LIVE_PLAN_SCHEMA, (intent.scope,), recorded_at=at(200), transition_id="manual"
    )
    bad = append_enrollment(
        a.account,
        intent.scope,
        a.plan.head_sha,
        [dict(plan_sha=a.plan.projection.scopes[0].plan_sha, head=a.plan.head_sha)],
    )
    assert (
        "plan_enrollment_initial_head_mismatch"
        in reconcile_live_journals(bad, (a.plan, p)).reasons
    )


def test_historical_prior_head_cannot_omit_progressed_attempt():
    initial = first()
    p, a, _ = progress()
    intent = derive_enrollment_intent(**sources("plan-b"), at=at(200))
    pb = LiveJournal.create(
        LIVE_PLAN_SCHEMA, (intent.scope,), recorded_at=at(200), transition_id="manual"
    )
    bad = append_enrollment(
        a,
        intent.scope,
        pb.head_sha,
        [dict(plan_sha=p.projection.scopes[0].plan_sha, head=initial.plan.head_sha)],
    )
    assert (
        "plan_enrollment_prior_heads_mismatch"
        in reconcile_live_journals(bad, (p, pb)).reasons
    )


def test_actual_prior_head_payload_overflow_is_not_summarized():
    a = empty()
    before = a.to_bytes()
    prior = [dict(plan_sha=f"{i:064x}", head="a" * 64) for i in range(300)]
    intent = derive_enrollment_intent(**sources(), at=at(200))
    with pytest.raises(HttpContractError, match="record_size_invalid"):
        append_enrollment(a, intent.scope, "a" * 64, prior)
    assert a.to_bytes() == before


def test_enrollment_payload_cannot_shorten_hold_or_change_generation():
    p, a, _ = progress(status=429, retry=(b"600",))
    intent = derive_enrollment_intent(**sources("plan-b"), at=at(200))
    before = a.to_bytes()
    with pytest.raises(HttpContractError):
        append_enrollment(
            a,
            intent.scope,
            "a" * 64,
            [dict(plan_sha=p.projection.scopes[0].plan_sha, head=p.head_sha)],
            holds=[],
            generation=0,
        )
    assert a.to_bytes() == before


def test_all_three_schema_names_are_distinct():
    assert (
        len({LIVE_ACCOUNT_SCHEMA_V1, LIVE_ACCOUNT_SCHEMA_V2, ACCOUNT_LEDGER_SCHEMA})
        == 3
    )
