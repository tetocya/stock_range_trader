"""C2 contract-only fixtures: immutable values and bytes, no HTTP or files."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from feasibility.http_contract import (
    ACCOUNT_LEDGER_SCHEMA,
    JOURNAL_SCHEMA,
    AccountRateEvent,
    AccountRateLedger,
    AccountRatePolicy,
    HttpContractError,
    LiveAcquisitionGate,
    RetryRules,
)
from feasibility.live_http_evidence import (
    LIVE_ACCOUNT_SCHEMA,
    LIVE_HEADER_SCHEMA,
    LIVE_PLAN_SCHEMA,
    MAX_EVENT_BYTES,
    AttemptBinding,
    HeaderObservation,
    LiveHold,
    LiveJournal,
    LivePlanScope,
    LiveReconciliation,
    assess_live_restrictions,
    capture_headers,
    check_body_generation,
    reconcile_live_journals,
    required_attempt_hold,
)
from feasibility.live_preflight import LiveClockPolicy

NOW = datetime(2026, 9, 29, 0, tzinfo=UTC)
ACCOUNT = "artificial-c2-account"
MICRO = timedelta(microseconds=1)


def t(seconds: int = 0) -> datetime:
    return NOW + timedelta(seconds=seconds)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def scope(**changes) -> LivePlanScope:
    values = dict(
        plan_sha="a" * 64,
        preflight_sha="b" * 64,
        account_ref=ACCOUNT,
        approval_sha="c" * 64,
        not_before=t(-60),
        expires_at=t(3600),
        retry=RetryRules(
            max_attempts_per_page=3,
            min_interval_seconds=13,
            min_wait_after_429_seconds=120,
            min_wait_after_5xx_seconds=13,
            min_wait_after_network_error_seconds=13,
            timeout_seconds=30,
        ),
        account_policy=AccountRatePolicy(
            min_interval_seconds=13,
            min_wait_after_429_seconds=120,
            min_wait_after_5xx_seconds=13,
            min_wait_after_network_error_seconds=13,
            request_timeout_seconds=30,
            slot_lease_seconds=60,
        ),
        clock_policy=LiveClockPolicy(5, 10),
    )
    values.update(changes)
    return LivePlanScope(**values)


def binding(s: LivePlanScope | None = None, **changes) -> AttemptBinding:
    s = s or scope()
    values = dict(
        plan_sha=s.plan_sha,
        preflight_sha=s.preflight_sha,
        account_ref=s.account_ref,
        approval_sha=s.approval_sha,
        attempt_id="attempt-a",
        slot_id="slot-a",
        holder_id="holder-a",
        generation=1,
    )
    values.update(changes)
    return AttemptBinding(**values)


def opened(s: LivePlanScope | None = None, *others):
    s = s or scope()
    return (
        LiveJournal.create(LIVE_PLAN_SCHEMA, (s,), recorded_at=t()),
        LiveJournal.create(LIVE_ACCOUNT_SCHEMA, (s, *others), recorded_at=t()),
        binding(s),
    )


def paired(plan, account, b, plan_kind, account_kind, at, transition, **data):
    return (
        plan.append(
            plan_kind, recorded_at=at, transition_id=transition, binding=b, **data
        ),
        account.append(
            account_kind, recorded_at=at, transition_id=transition, binding=b, **data
        ),
    )


def reserved(s: LivePlanScope | None = None, *others):
    p, a, b = opened(s, *others)
    p, a = paired(
        p,
        a,
        b,
        "attempt_reserved",
        "slot_reserved",
        t(1),
        "reserve-a",
        reserved_at=stamp(t(1)),
    )
    return p, a, b


def sent(s: LivePlanScope | None = None, *others):
    p, a, b = reserved(s, *others)
    p, a = paired(
        p, a, b, "attempt_sent", "slot_sent", t(2), "send-a", sent_at=stamp(t(2))
    )
    return p, a, b


def observation(**changes) -> HeaderObservation:
    values = dict(
        observation_id="header-a",
        status=429,
        header_block_complete=True,
        observed_at=t(5),
        retry_after_fields=(b"600",),
    )
    values.update(changes)
    return capture_headers(**values)


def hold_sync(account, b, at, transition):
    projection = account.projection
    hold = required_attempt_hold(
        projection.scope(b.plan_sha), projection.attempt(b.plan_sha, b.attempt_id)
    )
    hold = replace(hold, recorded_at=at)
    kind = (
        "hold_extended"
        if any(h.hold.hold_id == hold.hold_id for h in projection.holds)
        else "hold_entered"
    )
    return account.append(
        kind, recorded_at=at, transition_id=transition, hold=hold.to_dict()
    )


def headers(header=None, s=None, *others):
    p, a, b = sent(s, *others)
    h = header or observation()
    p, a = paired(
        p,
        a,
        b,
        "response_headers_observed",
        "slot_headers_observed",
        h.observed_at,
        "headers-a",
        observation=h.to_dict(),
    )
    a = hold_sync(a, b, h.observed_at, "header-hold-a")
    return p, a, b


def settled(header=None, *, outcome="response", s=None, others=()):
    p, a, b = headers(header, s, *others)
    h = p.projection.attempt(b.plan_sha, b.attempt_id).header
    p, a = paired(
        p,
        a,
        b,
        "response_received" if outcome == "response" else "outcome_unknown",
        "slot_settled",
        t(6),
        "settle-a",
        settled_at=stamp(t(6)),
        outcome=outcome,
        status=h.status if outcome == "response" else None,
    )
    a = hold_sync(a, b, t(6), "settle-hold-a")
    return p, a, b


def release(account, *, at=None, hold_id=None, manual=None, repair=None):
    at = t(606) if at is None else at
    h = account.projection.holds[0].hold
    return account.append(
        "hold_released",
        recorded_at=at,
        transition_id="release-" + str(len(account.records)),
        hold_id=hold_id or h.hold_id,
        clock_reference="urn:artificial:clock-review",
        account_state_reference="urn:artificial:account-review",
        manual_review_reference=manual,
        repair_reference=repair,
    )


def reclaim(account, b, *, at=None, **changes):
    at = t(61) if at is None else at
    evidence = dict(
        lease_expired_at=stamp(t(61)),
        observed_at=stamp(at),
        clock_status="verified",
        previous_transport_termination_reference="urn:artificial:terminated",
        operator_review_reference="urn:artificial:manual-review",
        process_restart_detected=True,
    )
    evidence.update(changes)
    return account.append(
        "slot_reclaimed",
        recorded_at=at,
        transition_id="reclaim-a",
        binding=b,
        evidence=evidence,
    )


def test_complete_429_600_requires_explicit_release_even_at_deadline():
    p, a, b = settled()
    assert reconcile_live_journals(a, (p,)).classification == "consistent"
    early = assess_live_restrictions(a, (p,), plan_sha=b.plan_sha, at=t(606) - MICRO)
    assert early.known_not_before == t(606)
    assert "live_known_wait_not_elapsed" in early.reasons
    assert not early.rule_release_candidates
    ready = assess_live_restrictions(a, (p,), plan_sha=b.plan_sha, at=t(606))
    assert ready.rule_release_candidates == (a.projection.holds[0].hold.hold_id,)
    assert "live_rule_hold_active" in ready.reasons
    with pytest.raises(HttpContractError, match="live_hold_known_wait_not_elapsed"):
        release(a, at=t(606) - MICRO)
    released = release(a)
    assert not assess_live_restrictions(
        released, (p,), plan_sha=b.plan_sha, at=t(606)
    ).reasons
    assert not ready.live_send_permitted
    assert a.projection.holds[0].released_at is None  # immutable predecessor
    assert released.projection.holds[0].released_at == t(606)


@pytest.mark.parametrize("status", [200, 429, 503])
@pytest.mark.parametrize("complete", [False, True])
def test_headers_then_body_timeout_keeps_evidence_and_manual_wait(status, complete):
    h = observation(status=status, header_block_complete=complete)
    p, a, b = settled(h, outcome="unknown")
    assert p.projection.attempt(b.plan_sha, b.attempt_id).header == h
    assert reconcile_live_journals(a, (p,)).classification == "consistent"
    hold = a.projection.holds[0].hold
    assert hold.release_mode == "manual"
    assert hold.indefinite is (not complete)
    assert hold.not_before == t(606)
    for at in (t(126), t(1000)):
        assessment = assess_live_restrictions(a, (p,), plan_sha=b.plan_sha, at=at)
        assert "live_manual_hold_active" in assessment.reasons
        assert not assessment.rule_release_candidates
    with pytest.raises(HttpContractError, match="live_hold_known_wait_not_elapsed"):
        release(a, at=t(126), manual="urn:manual:review")
    with pytest.raises(HttpContractError, match="reference_invalid"):
        release(a)
    assert release(a, manual="urn:manual:review").projection.holds[0].released_at == t(
        606
    )


def test_sent_unknown_without_headers_is_manual_and_uses_unknown_lower_bound():
    p, a, b = sent()
    p, a = paired(
        p,
        a,
        b,
        "outcome_unknown",
        "slot_settled",
        t(6),
        "unknown-a",
        outcome="unknown",
        status=None,
        settled_at=stamp(t(6)),
    )
    a = hold_sync(a, b, t(6), "unknown-hold")
    assert reconcile_live_journals(a, (p,)).classification == "consistent"
    assert a.projection.holds[0].hold.not_before == t(126)
    assert a.projection.holds[0].hold.release_mode == "manual"


@pytest.mark.parametrize(
    "fields,kind",
    [
        ((b"nonsense",), "malformed"),
        ((b"86401",), "out_of_range"),
        ((b"9" * 1024,), "out_of_range"),
        ((b"600", b"900"), "duplicate_field"),
        ((b"600", b"\x00"), "capture_rejected"),
        ((b"9" * 5000,), "capture_rejected"),
    ],
)
def test_abnormal_retry_after_is_indefinite_without_clipping(fields, kind):
    h = observation(retry_after_fields=fields)
    assert h.parsed.kind == kind
    p, a, b = settled(h)
    hold = a.projection.holds[0].hold
    assert hold.indefinite and hold.release_mode == "manual"
    assert not assess_live_restrictions(
        a, (p,), plan_sha=b.plan_sha, at=t(3500)
    ).rule_release_candidates
    if fields[0] == b"600":
        assert hold.not_before >= t(606)


def test_partial_malformed_and_absent_vs_undetermined():
    h = observation(header_block_complete=False, retry_after_fields=(b"bad",))
    p, a, _ = settled(h, outcome="unknown")
    assert h.parsed.kind == "malformed"
    assert a.projection.holds[0].hold.indefinite
    assert reconcile_live_journals(a, (p,)).classification == "consistent"
    incomplete = observation(header_block_complete=False, retry_after_fields=())
    assert incomplete.retry_after_presence == "undetermined"
    assert incomplete.parsed.kind == "incomplete"
    complete = observation(retry_after_fields=())
    assert complete.retry_after_presence == "absent"
    with pytest.raises(
        HttpContractError, match="incomplete_headers_cannot_mean_absent"
    ):
        replace(incomplete, retry_after_presence="absent")


@pytest.mark.parametrize(
    "raw",
    [
        "Sun, 06 Nov 1994 08:59:37 GMT",
        "Sunday, 06-Nov-94 08:59:37 GMT",
        "Sun Nov  6 08:59:37 1994",
    ],
)
def test_http_date_variants_and_reopen_use_fixed_observation_time(raw):
    observed = datetime(1994, 11, 6, 8, 49, 37, tzinfo=UTC)
    h = observation(observed_at=observed, retry_after_fields=(raw,))
    assert h.parsed.kind == "http_date"
    assert h.known_deadline(observed) == observed + timedelta(seconds=600)
    assert HeaderObservation.from_dict(h.to_dict()) == h


def test_past_http_date_retains_evidence_and_rule_wait():
    h = observation(retry_after_fields=("Sunday, 06-Nov-94 08:59:37 GMT",))
    assert h.parsed.kind == "past_http_date" and h.parsed.delay_seconds == 0
    assert h.parsed.http_date.startswith("1994-")
    _, a, _ = settled(h)
    assert a.projection.holds[0].hold.not_before == t(126)


@pytest.mark.parametrize(
    "raw,kind",
    [
        ("Wed, 30 Sep 2026 00:00:05 GMT", "http_date"),
        ("Thu, 01 Oct 2026 00:00:05 GMT", "out_of_range"),
        ("Wed, 29 Sep 2026 00:00:05 GMT", "malformed"),
        ("Tue, 29 Sep 2026 00:00:05 PST", "malformed"),
        ("Tue, 29 Sep 2026 00:00:99 GMT", "malformed"),
    ],
)
def test_http_date_boundaries_and_invalid_values(raw, kind):
    assert observation(retry_after_fields=(raw,)).parsed.kind == kind


@pytest.mark.parametrize("status", [True, False, 199, 600, -1, 429.0, "429"])
def test_status_must_be_final_integer(status):
    with pytest.raises(HttpContractError, match="status_invalid"):
        observation(status=status)


@pytest.mark.parametrize(
    "fields",
    [
        (b"\x7f",),
        (b"\r\n",),
        ("日本語",),
        (b"\xff",),
        (None,),
        tuple(b"600" for _ in range(5)),
    ],
)
def test_raw_special_inputs_have_bounded_diagnostics(fields):
    h = observation(retry_after_fields=fields)
    assert h.parsed.kind == "capture_rejected"
    assert 1 <= len(h.capture_issues) <= 5
    assert all(len(c.prefix_hex) <= 128 for c in h.capture_issues)
    assert HeaderObservation.from_dict(h.to_dict()) == h


def test_raw_octet_boundary_and_delta_bounds():
    assert (
        observation(retry_after_fields=(b" " * 1021 + b"600",)).parsed.delay_seconds
        == 600
    )
    h = observation(retry_after_fields=(b"X" * 1025,))
    assert h.capture_issues[0].input_length == 1025
    assert len(h.capture_issues[0].prefix_hex) == 128
    assert observation(retry_after_fields=(b"86400",)).parsed.delay_seconds == 86400
    assert (
        observation(retry_after_fields=(b"0" * 1000 + b"600",)).parsed.delay_seconds
        == 600
    )


def test_partial_snapshot_cannot_be_upgraded_or_settled_as_complete():
    p, a, b = headers(observation(header_block_complete=False))
    with pytest.raises(HttpContractError, match="live_header_state_invalid"):
        a.append(
            "slot_headers_observed",
            recorded_at=t(6),
            transition_id="new-header",
            binding=b,
            observation=observation(observation_id="new", observed_at=t(6)).to_dict(),
        )
    with pytest.raises(
        HttpContractError, match="complete_response_requires_complete_header_evidence"
    ):
        p.append(
            "response_received",
            recorded_at=t(6),
            transition_id="result",
            binding=b,
            outcome="response",
            status=429,
            settled_at=stamp(t(6)),
        )


def test_reopen_header_evidence_and_caller_mutations_are_isolated():
    p, a, b = headers()
    restored = LiveJournal.from_bytes(a.to_bytes(), expected_schema=LIVE_ACCOUNT_SCHEMA)
    assert restored == a and restored.projection == a.projection
    assert restored.head_sha == a.head_sha
    payload = observation().to_dict()
    payload["raw_retry_after"][0] = "1"
    assert restored.projection.attempt(
        b.plan_sha, b.attempt_id
    ).header.raw_retry_after == ("600",)
    with pytest.raises(FrozenInstanceError):
        restored.schema = "changed"
    assert reconcile_live_journals(restored, (p,)).classification == "consistent"


def test_header_raw_or_parsed_tamper_is_rejected_on_reconstruction():
    for key, value in (
        ("raw_retry_after", ["1"]),
        ("parsed", {"kind": "delta_seconds", "delay_seconds": 1, "http_date": None}),
    ):
        raw = observation().to_dict()
        raw[key] = value
        with pytest.raises(HttpContractError, match="parsed_evidence_mismatch"):
            HeaderObservation.from_dict(raw)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"status": 200}, "header_status_mismatch"),
        ({"retry_after_fields": (b"601",)}, "retry_after_evidence_mismatch"),
        ({"observation_id": "other"}, "header_observation_mismatch"),
    ],
)
def test_reconciliation_header_mismatch(change, reason):
    p, a, b = sent()
    p = p.append(
        "response_headers_observed",
        recorded_at=t(5),
        transition_id="headers-a",
        binding=b,
        observation=observation().to_dict(),
    )
    a = a.append(
        "slot_headers_observed",
        recorded_at=t(5),
        transition_id="headers-a",
        binding=b,
        observation=observation(**change).to_dict(),
    )
    a = hold_sync(a, b, t(5), "hold")
    result = reconcile_live_journals(a, (p,))
    assert result.classification == "inconsistent" and reason in result.reasons


def test_pending_header_pair_vs_peer_progressed_unknown():
    p, a, b = sent()
    p = p.append(
        "response_headers_observed",
        recorded_at=t(5),
        transition_id="headers-a",
        binding=b,
        observation=observation().to_dict(),
    )
    result = reconcile_live_journals(a, (p,))
    assert (
        result.classification == "pending" and "pending_header_pair" in result.reasons
    )
    a = a.append(
        "slot_settled",
        recorded_at=t(6),
        transition_id="unknown",
        binding=b,
        outcome="unknown",
        status=None,
        settled_at=stamp(t(6)),
    )
    assert reconcile_live_journals(a, (p,)).classification == "inconsistent"


def test_different_recording_times_are_not_http_observation_time_mismatch():
    p, a, b = sent()
    h = observation().to_dict()
    p = p.append(
        "response_headers_observed",
        recorded_at=t(5),
        transition_id="headers-a",
        binding=b,
        observation=h,
    )
    a = a.append(
        "slot_headers_observed",
        recorded_at=t(9),
        transition_id="headers-a",
        binding=b,
        observation=h,
    )
    a = hold_sync(a, b, t(9), "hold")
    assert reconcile_live_journals(a, (p,)).classification == "consistent"


def test_holder_and_generation_mismatch_do_not_match_attempts():
    for modified in (
        replace(binding(), holder_id="other"),
        replace(binding(), generation=2),
    ):
        p, a, _ = opened()
        p = p.append(
            "attempt_reserved",
            recorded_at=t(1),
            transition_id="reserve-a",
            binding=modified,
            reserved_at=stamp(t(1)),
        )
        a = a.append(
            "slot_reserved",
            recorded_at=t(1),
            transition_id="reserve-a",
            binding=binding(),
            reserved_at=stamp(t(1)),
        )
        assert "header_binding_mismatch" in reconcile_live_journals(a, (p,)).reasons


def test_header_and_settlement_causal_time_violations():
    p, _, b = sent()
    with pytest.raises(HttpContractError, match="header_before_send"):
        p.append(
            "response_headers_observed",
            recorded_at=t(5),
            transition_id="header",
            binding=b,
            observation=observation(observed_at=t(1)).to_dict(),
        )
    p, _, b = headers()
    with pytest.raises(HttpContractError, match="settlement_before_header_evidence"):
        p.append(
            "outcome_unknown",
            recorded_at=t(6),
            transition_id="unknown",
            binding=b,
            outcome="unknown",
            status=None,
            settled_at=stamp(t(4)),
        )


def test_reserved_never_sent_is_not_unknown_and_pre_send_reclaim_is_distinct():
    p, a, b = reserved()
    for journal, kind in ((p, "outcome_unknown"), (a, "slot_settled")):
        with pytest.raises(
            HttpContractError, match="live_unknown_requires_sent_attempt"
        ):
            journal.append(
                kind,
                recorded_at=t(6),
                transition_id="unknown",
                binding=b,
                outcome="unknown",
                status=None,
                settled_at=stamp(t(6)),
            )
    a = reclaim(a, b)
    a = hold_sync(a, b, t(61), "reclaim-hold")
    assert a.projection.attempt(b.plan_sha, b.attempt_id).outcome == "pre_send_reclaim"
    assert p.projection.attempt(b.plan_sha, b.attempt_id).outcome is None
    assert reconcile_live_journals(a, (p,)).classification == "consistent"


def test_generation_reclaim_replay_and_stale_callbacks_cannot_progress():
    p, a, b = sent()
    with pytest.raises(HttpContractError, match="manual_evidence_required"):
        reclaim(a, b, at=t(60))
    a = reclaim(a, b)
    a = hold_sync(a, b, t(61), "reclaim-hold")
    assert a.projection.generation == 2
    for kind, data in (
        (
            "slot_headers_observed",
            {"observation": observation(observed_at=t(62)).to_dict()},
        ),
        (
            "slot_settled",
            {"outcome": "unknown", "status": None, "settled_at": stamp(t(62))},
        ),
    ):
        with pytest.raises(
            HttpContractError, match="stale_generation_manual_hold_review_required"
        ):
            a.append(kind, recorded_at=t(62), transition_id="old", binding=b, **data)
    with pytest.raises(
        HttpContractError, match="stale_generation_manual_hold_review_required"
    ):
        check_body_generation(a, b)
    p = p.append(
        "outcome_unknown",
        recorded_at=t(62),
        transition_id="reclaim-a",
        binding=b,
        outcome="unknown",
        status=None,
        settled_at=stamp(t(61)),
    )
    assert reconcile_live_journals(a, (p,)).classification == "consistent"
    a = release(a, at=t(181), manual="urn:review")
    for generation in (1, 2):
        with pytest.raises(HttpContractError, match="live_generation_not_next"):
            a.append(
                "slot_reserved",
                recorded_at=t(181),
                transition_id="new-reserve",
                binding=replace(b, generation=generation, attempt_id="b", slot_id="b"),
                reserved_at=stamp(t(181)),
            )
    a = a.append(
        "slot_reserved",
        recorded_at=t(181),
        transition_id="new-reserve",
        binding=replace(b, generation=3, attempt_id="b", slot_id="b"),
        reserved_at=stamp(t(181)),
    )
    assert (
        LiveJournal.from_bytes(
            a.to_bytes(), expected_schema=LIVE_ACCOUNT_SCHEMA
        ).projection.generation
        == 3
    )


def test_missing_and_tampered_hold_projection_are_detected():
    p, a, b = sent()
    p, a = paired(
        p,
        a,
        b,
        "response_headers_observed",
        "slot_headers_observed",
        t(5),
        "headers-a",
        observation=observation().to_dict(),
    )
    assert "hold_projection_mismatch" in reconcile_live_journals(a, (p,)).reasons
    a = hold_sync(a, b, t(5), "hold")
    claimed = replace(a.projection, generation=99, holds=())
    result = reconcile_live_journals(a, (p,), claimed_projection=claimed)
    assert {"generation_projection_mismatch", "hold_projection_mismatch"} <= set(
        result.reasons
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"not_before": t(605)},
        {"release_mode": "rule_based"},
        {"indefinite": False},
        {"source_refs": ("new",)},
    ],
)
def test_hold_extensions_cannot_weaken_any_constraint(changes):
    _, a, _ = settled(observation(header_block_complete=False), outcome="unknown")
    hold = a.projection.holds[0].hold
    with pytest.raises(HttpContractError):
        a.append(
            "hold_extended",
            recorded_at=t(7),
            transition_id="extend",
            hold=replace(hold, recorded_at=t(7), **changes).to_dict(),
        )


def test_hold_extension_strengthens_deadline_mode_and_indefiniteness():
    _, a, _ = settled()
    h = a.projection.holds[0].hold
    a = a.append(
        "hold_extended",
        recorded_at=t(7),
        transition_id="extend",
        hold=replace(
            h,
            recorded_at=t(7),
            not_before=t(906),
            release_mode="manual",
            indefinite=True,
        ).to_dict(),
    )
    with pytest.raises(HttpContractError, match="known_wait_not_elapsed"):
        release(a, at=t(606), manual="urn:review")
    assert release(a, at=t(906), manual="urn:review").projection.holds[
        0
    ].released_at == t(906)


def test_additional_account_hold_and_other_plan_share_longest_wait():
    other = scope(plan_sha="d" * 64)
    p, a, b = settled(others=(other,))
    other_p = LiveJournal.create(LIVE_PLAN_SCHEMA, (other,), recorded_at=t())
    hold = LiveHold(
        "clock-hold",
        "clock_anomaly",
        ("urn:clock:anomaly",),
        t(900),
        "manual",
        True,
        other.policy_sha,
        t(7),
    )
    a = a.append(
        "hold_entered",
        recorded_at=t(7),
        transition_id="clock-hold",
        hold=hold.to_dict(),
    )
    assessment = assess_live_restrictions(
        a, (p, other_p), plan_sha=other.plan_sha, at=t(606)
    )
    assert assessment.known_not_before == t(900)
    assert "live_manual_hold_active" in assessment.reasons
    assert not assessment.rule_release_candidates
    a = release(a, at=t(606))
    with pytest.raises(HttpContractError, match="live_account_hold_active"):
        a.append(
            "slot_reserved",
            recorded_at=t(900),
            transition_id="other",
            binding=binding(other, generation=2, slot_id="b", attempt_id="b"),
            reserved_at=stamp(t(900)),
        )
    a = release(a, at=t(900), hold_id="clock-hold", manual="urn:clock:review")
    assert not assess_live_restrictions(
        a, (p, other_p), plan_sha=other.plan_sha, at=t(900)
    ).reasons


def test_account_rule_and_plan_rule_never_shorten_retry_after():
    s = scope()
    s = replace(
        s, account_policy=replace(s.account_policy, min_wait_after_429_seconds=900)
    )
    _, a, _ = settled(s=s)
    assert a.projection.holds[0].hold.not_before == t(906)
    s = replace(s, retry=replace(s.retry, min_wait_after_429_seconds=900))
    _, a, _ = settled(s=s)
    assert a.projection.holds[0].hold.not_before == t(906)


def test_ledger_inconsistency_hold_requires_repair_reference():
    p, a, _ = opened()
    h = LiveHold(
        "inconsistency",
        "ledger_inconsistency",
        ("urn:incident",),
        None,
        "manual",
        True,
        scope().policy_sha,
        t(1),
    )
    a = a.append(
        "hold_entered", recorded_at=t(1), transition_id="hold", hold=h.to_dict()
    )
    with pytest.raises(HttpContractError, match="reference_invalid"):
        release(a, at=t(2), manual="urn:review")
    a = release(a, at=t(2), manual="urn:review", repair="urn:verified:repair")
    assert not assess_live_restrictions(
        a, (p,), plan_sha=scope().plan_sha, at=t(2)
    ).live_send_permitted


def test_unrelated_or_missing_plan_journals_cannot_prove_consistency():
    p, a, _ = settled()
    assert (
        "related_plan_journals_incomplete_or_unrelated"
        in reconcile_live_journals(a, ()).reasons
    )
    other = LiveJournal.create(
        LIVE_PLAN_SCHEMA, (scope(plan_sha="d" * 64),), recorded_at=t()
    )
    assert reconcile_live_journals(a, (p, other)).classification == "inconsistent"
    assert "duplicate_plan_journal" in reconcile_live_journals(a, (p, p)).reasons


def test_audit_after_expiry_is_allowed_but_new_sends_are_not():
    s = scope(expires_at=t(3))
    p, a, b = settled(outcome="unknown", s=s)
    assert reconcile_live_journals(a, (p,)).classification == "consistent"
    assert (
        "live_plan_expired_for_send"
        in assess_live_restrictions(a, (p,), plan_sha=b.plan_sha, at=t(606)).reasons
    )
    p, _, b = reserved(s)
    with pytest.raises(HttpContractError, match="live_plan_expired_for_send"):
        p.append(
            "attempt_sent",
            recorded_at=t(4),
            transition_id="send",
            binding=b,
            sent_at=stamp(t(4)),
        )


def test_send_time_guard_preserves_c1_and_utc_normalization():
    p, _, b = reserved()
    with pytest.raises(HttpContractError, match="reservation_to_send_bound"):
        p.append(
            "attempt_sent",
            recorded_at=t(7),
            transition_id="send",
            binding=b,
            sent_at=stamp(t(7)),
        )
    p = p.append(
        "attempt_sent",
        recorded_at=t(6).astimezone(timezone(timedelta(hours=9))),
        transition_id="send",
        binding=b,
        sent_at=stamp(t(6)),
    )
    assert p.projection.attempt(b.plan_sha, b.attempt_id).sent_at == t(6)


def test_schema_separation_and_legacy_account_bytes_unchanged():
    p, a, _ = opened()
    assert JOURNAL_SCHEMA == "historical-feasibility-journal-v2"
    assert ACCOUNT_LEDGER_SCHEMA == "historical-feasibility-account-rate-ledger-v2"
    assert observation().to_dict()["schema"] == LIVE_HEADER_SCHEMA
    legacy = AccountRateLedger(ACCOUNT).append(
        AccountRateEvent("ledger_opened", t(), policy=scope().account_policy)
    )
    old_bytes, old_hash = legacy.data, legacy.head_hash
    with pytest.raises(HttpContractError):
        AccountRateLedger(ACCOUNT, a.to_bytes())
    with pytest.raises(HttpContractError):
        LiveJournal.from_bytes(old_bytes, expected_schema=LIVE_ACCOUNT_SCHEMA)
    assert AccountRateLedger(ACCOUNT, old_bytes).head_hash == old_hash
    assert legacy.data == old_bytes
    for schema in (ACCOUNT_LEDGER_SCHEMA, JOURNAL_SCHEMA):
        with pytest.raises(HttpContractError, match="schema_mismatch"):
            LiveJournal.create(schema, (scope(),), recorded_at=t())
    with pytest.raises(HttpContractError, match="schema_mismatch"):
        LiveJournal.from_bytes(p.to_bytes(), expected_schema=LIVE_ACCOUNT_SCHEMA)


def rehash_last(journal, transform):
    record = json.loads(journal.records[-1])
    transform(record)
    content = {k: v for k, v in record.items() if k != "event_hash"}

    def canonical(value):
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    record["event_hash"] = hashlib.sha256(canonical(content).encode()).hexdigest()
    return ("\n".join((*journal.records[:-1], canonical(record))) + "\n").encode()


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda r: r.update(extra=1), "fields_invalid"),
        (lambda r: r["event"].update(extra=1), "fields_invalid"),
        (lambda r: r["event"].update(kind="automatic_reclaim"), "event_unknown"),
        (lambda r: r.update(sequence=True), "integer_invalid"),
        (lambda r: r.update(sequence=1 << 63), "integer_invalid"),
        (lambda r: r["event"]["binding"].update(generation=True), "integer_invalid"),
        (lambda r: r["event"]["data"]["observation"].update(extra=1), "fields_invalid"),
    ],
)
def test_rehashed_malformed_events_still_fail_schema_or_replay(mutation, reason):
    p, _, _ = headers()
    with pytest.raises(HttpContractError, match=reason):
        LiveJournal.from_bytes(
            rehash_last(p, mutation), expected_schema=LIVE_PLAN_SCHEMA
        )


def test_json_duplicate_key_nonfinite_size_tail_and_hash_tamper():
    p, _, _ = headers()
    data = p.to_bytes()
    for changed, reason in (
        (
            data.replace(b'"sequence":0', b'"sequence":0,"sequence":0', 1),
            "duplicate_json_key",
        ),
        (data.replace(b'"sequence":0', b'"sequence":NaN', 1), "nonfinite_json"),
        (data.replace(b'"600"', b'"601"'), "hash_mismatch"),
        (data[:-1], "incomplete_tail"),
        (b"X" * (MAX_EVENT_BYTES + 1) + b"\n", "record_size_invalid"),
        (b'{"sequence":' + b"9" * 5000 + b"}\n", "integer_invalid"),
    ):
        with pytest.raises(HttpContractError, match=reason):
            LiveJournal.from_bytes(changed, expected_schema=LIVE_PLAN_SCHEMA)


def test_informational_results_cannot_enable_live_or_identity_permissions():
    p, a, b = settled()
    result = reconcile_live_journals(a, (p,))
    assert result.classification == "consistent"
    assert not any(
        (
            result.live_send_permitted,
            result.authenticated_account_verified,
            result.owner_approval_verified,
            result.store_implemented,
        )
    )
    with pytest.raises(TypeError):
        LiveReconciliation("consistent", (), live_send_permitted=True)
    assert not assess_live_restrictions(
        a, (p,), plan_sha=b.plan_sha, at=t(1000)
    ).live_send_permitted
    gate = LiveAcquisitionGate(reasons=("http_transport_not_implemented",))
    assert not gate.permitted


def test_obsolete_date_50_year_rule_uses_fixed_full_timestamp():
    h = observation(
        observed_at=datetime(2044, 1, 1, tzinfo=UTC),
        retry_after_fields=("Sunday, 06-Nov-94 08:59:37 GMT",),
    )
    assert h.parsed.kind == "past_http_date"
    assert h.parsed.http_date.startswith("1994-")


def test_future_release_cannot_authorize_backdated_control_events():
    p, a, b = settled(outcome="unknown")
    a = release(a, at=t(1000), manual="urn:review")
    result = assess_live_restrictions(a, (p,), plan_sha=b.plan_sha, at=t(700))
    assert "live_assessment_clock_regressed" in result.reasons
    with pytest.raises(
        HttpContractError, match="live_control_time_before_recorded_evidence"
    ):
        a.append(
            "slot_reserved",
            recorded_at=t(1001),
            transition_id="backdated",
            binding=replace(b, generation=2, attempt_id="b", slot_id="b"),
            reserved_at=stamp(t(700)),
        )


def test_rehashed_raw_tamper_cannot_fake_derived_parse():
    p, _, _ = headers()
    data = rehash_last(
        p,
        lambda r: r["event"]["data"]["observation"]["raw_retry_after"].__setitem__(
            0, "1"
        ),
    )
    with pytest.raises(HttpContractError, match="parsed_evidence_mismatch"):
        LiveJournal.from_bytes(data, expected_schema=LIVE_PLAN_SCHEMA)


def test_complete_response_without_headers_and_unknown_status_claim_rejected():
    p, _, b = sent()
    with pytest.raises(
        HttpContractError, match="complete_response_requires_complete_header_evidence"
    ):
        p.append(
            "response_received",
            recorded_at=t(6),
            transition_id="result",
            binding=b,
            outcome="response",
            status=200,
            settled_at=stamp(t(6)),
        )
    with pytest.raises(HttpContractError, match="live_unknown_final_status_forbidden"):
        p.append(
            "outcome_unknown",
            recorded_at=t(6),
            transition_id="unknown",
            binding=b,
            outcome="unknown",
            status=200,
            settled_at=stamp(t(6)),
        )


def test_missing_hold_blocks_next_account_reservation():
    p, a, b = sent()
    p, a = paired(
        p,
        a,
        b,
        "outcome_unknown",
        "slot_settled",
        t(6),
        "unknown-a",
        outcome="unknown",
        status=None,
        settled_at=stamp(t(6)),
    )
    with pytest.raises(HttpContractError, match="hold_projection_mismatch"):
        a.append(
            "slot_reserved",
            recorded_at=t(1000),
            transition_id="next",
            binding=replace(b, generation=2, attempt_id="b", slot_id="b"),
            reserved_at=stamp(t(1000)),
        )


def test_body_generation_check_rejects_previous_generation_after_new_reservation():
    _, a, b = settled()
    check_body_generation(a, b)
    a = release(a)
    a = a.append(
        "slot_reserved",
        recorded_at=t(606),
        transition_id="next",
        binding=replace(b, generation=2, attempt_id="b", slot_id="b"),
        reserved_at=stamp(t(606)),
    )
    with pytest.raises(
        HttpContractError, match="stale_generation_manual_hold_review_required"
    ):
        check_body_generation(a, b)


def test_header_schema_mismatch_and_unknown_capture_fields_are_rejected():
    raw = observation().to_dict()
    raw["schema"] = JOURNAL_SCHEMA
    with pytest.raises(HttpContractError, match="live_header_schema_mismatch"):
        HeaderObservation.from_dict(raw)
    raw = observation(retry_after_fields=(b"\xff",)).to_dict()
    raw["capture_issues"][0]["extra"] = True
    with pytest.raises(HttpContractError, match="fields_invalid"):
        HeaderObservation.from_dict(raw)


def test_open_and_header_bytes_do_not_depend_on_current_clock_or_mutable_inputs():
    p, _, b = sent()
    raw = observation().to_dict()
    before = p.to_bytes()
    updated = p.append(
        "response_headers_observed",
        recorded_at=t(5),
        transition_id="header",
        binding=b,
        observation=raw,
    )
    stored = updated.to_bytes()
    raw["status"] = 200
    raw["raw_retry_after"].clear()
    assert p.to_bytes() == before and updated.to_bytes() == stored
    assert (
        LiveJournal.from_bytes(stored, expected_schema=LIVE_PLAN_SCHEMA).to_bytes()
        == stored
    )


@pytest.mark.parametrize("sources", [[[]], [{}], [True], ["same", "same"]])
def test_hold_sources_reject_invalid_types_and_duplicates_without_type_error(sources):
    _, account, _ = settled()
    raw = account.projection.holds[0].hold.to_dict()
    raw["source_refs"] = sources
    with pytest.raises(HttpContractError):
        LiveHold.from_dict(raw)
