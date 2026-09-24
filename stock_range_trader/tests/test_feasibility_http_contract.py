"""Artificial-only tests: no HTTP transport, key, market data, or owner approval."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from feasibility.acquisition import (
    DAILY,
    AcquisitionStopped,
    DateQuery,
    OfflineFixtureTransport,
    run_offline_fixture_acquisition,
)
from feasibility.http_contract import (
    HTTP_OUTPUT_ROOT,
    JOURNAL_SCHEMA,
    MAX_WAIT_SECONDS,
    AccountRateEvent,
    AccountRateLedger,
    AccountRatePolicy,
    AccountSlotAssessment,
    ApprovalScopeCheck,
    BodyFileEvidence,
    BodyInventory,
    CalendarAnchor,
    CalendarDiscoveryEvidence,
    EvidenceJournal,
    ExternalReceiptClaim,
    HttpAcquisitionPlan,
    HttpContractError,
    HttpLimits,
    JournalEvent,
    LedgerReconciliation,
    LiveAcquisitionClosed,
    LiveAcquisitionGate,
    OwnerApprovalClaim,
    PageRequest,
    ReceiptAlignment,
    RetryRules,
    assess_account_slot,
    assess_live_acquisition_gate,
    budget_snapshot,
    calendar_anchor_reference,
    check_account_plan_consistency,
    check_approval_scope,
    check_receipt_alignment,
    commit_account_ledger,
    derive_calendar_anchor,
    make_attempt_id,
    reconcile_account_and_plan,
    require_live_acquisition_permission,
    validate_page_request,
    verify_calendar_anchor,
)

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
ACCOUNT = "artificial-account"
DIGEST = "a" * 64
BODY_DIGEST = "b" * 64
MICRO = timedelta(microseconds=1)
CLOSED_TAIL = (
    "account_identity_unverified",
    "account_shared_store_not_implemented",
    "owner_approval_authenticity_unverified",
    "independent_receipt_custody_unconfigured",
    "http_transport_not_implemented",
)


def seconds(value: float) -> timedelta:
    return timedelta(seconds=value)


def limits(**changes) -> HttpLimits:
    values = dict(
        max_attempts=8,
        max_pages_total=5,
        max_pages_per_query=2,
        max_elapsed_seconds=1200,
        max_transfer_bytes=1000,
        max_decoded_bytes=1200,
        max_saved_bytes=900,
        max_page_transfer_bytes=500,
        max_page_decoded_bytes=600,
        max_page_saved_bytes=450,
    )
    values.update(changes)
    return HttpLimits(**values)


def retry(**changes) -> RetryRules:
    values = dict(
        max_attempts_per_page=3,
        min_interval_seconds=13,
        min_wait_after_429_seconds=120,
        min_wait_after_5xx_seconds=13,
        min_wait_after_network_error_seconds=13,
        timeout_seconds=30,
    )
    values.update(changes)
    return RetryRules(**values)


def plan(**changes) -> HttpAcquisitionPlan:
    values = dict(
        artifact_id="artificial-census",
        kind="predeclared_daily",
        reference_date="2025-03-04",
        calendar_start="2025-03-03",
        calendar_end="2025-03-04",
        master_date="2025-03-04",
        daily_dates=("2025-03-04", "2025-03-03"),
        calendar_source_sha256=DIGEST,
        calendar_source_reference="urn:artificial:calendar-source",
        output_dir=str(HTTP_OUTPUT_ROOT / "artificial-census"),
        not_before=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        limits=limits(),
        retry=retry(),
        account_ref=ACCOUNT,
    )
    values.update(changes)
    return HttpAcquisitionPlan(**values)


def approval(fixed: HttpAcquisitionPlan, **changes) -> OwnerApprovalClaim:
    values = dict(
        plan_sha256=fixed.sha256,
        artifact_id=fixed.artifact_id,
        scope_sha256=fixed.scope_sha256,
        allowed_endpoints=fixed.allowed_endpoints,
        reference_date=fixed.reference_date,
        calendar_start=fixed.calendar_start,
        calendar_end=fixed.calendar_end,
        master_date=fixed.master_date,
        daily_dates=fixed.daily_dates,
        limits=fixed.limits,
        retry=fixed.retry,
        output_dir=fixed.output_dir,
        valid_from=NOW - timedelta(seconds=30),
        valid_until=NOW + timedelta(minutes=30),
        approver_id="artificial-owner-id",
        approval_event_id="artificial-event-id",
        evidence_reference="urn:artificial:approval",
    )
    values.update(changes)
    return OwnerApprovalClaim(**values)


def page(fixed: HttpAcquisitionPlan, index: int = 0) -> PageRequest:
    return PageRequest(fixed.queries[index], 0)


def journal_for(fixed, claim=None, data: bytes = b"") -> EvidenceJournal:
    return EvidenceJournal(fixed, data, approval=claim or approval(fixed))


def reserve(journal, request, number, at=NOW, **overrides) -> JournalEvent:
    transfer, decoded, saved = journal.next_reservation_allowances
    values = dict(
        attempt_id=make_attempt_id(journal.plan, request, number),
        page=request,
        attempt_number=number,
        approval_sha256=journal.approval.sha256,
        allowed_transfer_bytes=transfer,
        allowed_decoded_bytes=decoded,
        allowed_saved_bytes=saved,
    )
    values.update(overrides)
    return JournalEvent("attempt_reserved", at, **values)


def exchange(
    journal,
    request,
    number,
    at=NOW,
    *,
    status=200,
    transfer=10,
    decoded=20,
    retry_after=None,
    sent_at=None,
    received_at=None,
) -> EvidenceJournal:
    event = reserve(journal, request, number, at)
    journal = journal.append(event)
    sent_at = sent_at or at
    journal = journal.append(
        JournalEvent("attempt_sent", sent_at, attempt_id=event.attempt_id)
    )
    return journal.append(
        JournalEvent(
            "response_received",
            received_at or sent_at,
            attempt_id=event.attempt_id,
            status=status,
            transfer_bytes=transfer,
            decoded_bytes=decoded,
            retry_after_seconds=retry_after,
        )
    )


def last_attempt_id(journal) -> str:
    return [e for e in journal.events if e.kind == "attempt_reserved"][-1].attempt_id


def finish_page(journal, at=NOW, *, digest, saved=20, next_key=None):
    attempt_id = last_attempt_id(journal)
    journal = journal.append(
        JournalEvent(
            "body_saved",
            at,
            attempt_id=attempt_id,
            body_sha256=digest,
            saved_bytes=saved,
        )
    )
    return journal.append(
        JournalEvent("page_completed", at, attempt_id=attempt_id, next_key=next_key)
    )


def complete_pages(fixed, claim=None, *, start=NOW, spacing=13):
    journal = journal_for(fixed, claim)
    files, at = [], start
    for index, query in enumerate(fixed.queries):
        digest = f"{index + 1:064x}"
        journal = exchange(journal, PageRequest(query, 0), 1, at)
        journal = finish_page(journal, at, digest=digest)
        files.append(BodyFileEvidence(f"body-{index}", 20, "committed", digest))
        at += seconds(spacing)
    return journal, BodyInventory(tuple(files)), at


def receipt(journal, inventory, **changes) -> ExternalReceiptClaim:
    values = dict(
        artifact_id=journal.plan.artifact_id,
        plan_sha256=journal.plan.sha256,
        approval_sha256=journal.approval.sha256,
        approval_event_id=journal.approval.approval_event_id,
        ledger_event_count=journal.event_count,
        ledger_bytes=journal.byte_count,
        ledger_head_sha256=journal.head_hash,
        body_set_sha256=inventory.body_set_sha256,
        fixed_at=NOW + timedelta(minutes=10),
        issuer_id="artificial-issuer",
        receipt_id="artificial-receipt",
        external_reference="urn:artificial:external-anchor",
    )
    values.update(changes)
    return ExternalReceiptClaim(**values)


def canonical(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def forge(fixed, data: bytes, event: dict, *, schema: str = JOURNAL_SCHEMA) -> bytes:
    """Append a well-chained record *without* contract validation (tamper tests)."""

    lines = data.split(b"\n")[:-1]
    head = json.loads(lines[-1])["event_id"] if lines else "0" * 64
    content = {
        "schema": schema,
        "sequence": len(lines),
        "previous_hash": head,
        "plan_sha256": fixed.sha256,
        "event": event,
    }
    record = {
        **content,
        "event_id": hashlib.sha256(canonical(content).encode()).hexdigest(),
    }
    return data + (canonical(record) + "\n").encode()


# ------------------------------------------------------------------ fixed plan


def test_plan_canonical_hash_order_timezone_and_semantic_changes():
    original = plan()
    offset = timezone(timedelta(hours=9))
    same = plan(
        daily_dates=tuple(reversed(original.daily_dates)),
        not_before=original.not_before.astimezone(offset),
        expires_at=original.expires_at.astimezone(offset),
    )
    assert original.sha256 == same.sha256
    assert original.daily_dates == ("2025-03-03", "2025-03-04")
    assert plan(daily_dates=("2025-03-04",)).sha256 != original.sha256
    assert plan(limits=limits(max_attempts=9)).sha256 != original.sha256
    assert plan(retry=retry(min_interval_seconds=14)).sha256 != original.sha256
    assert plan(calendar_source_sha256="c" * 64).sha256 != original.sha256
    assert (
        plan(calendar_source_reference="urn:artificial:another-source").sha256
        != original.sha256
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"daily_dates": ("2025-03-04", "2025-03-04")}, "duplicate_daily_date"),
        ({"daily_dates": ("2025-03-05",)}, "daily_dates_outside_fixed_window"),
        ({"daily_dates": ("2025-03-03",)}, "daily_dates_outside_fixed_window"),
        ({"daily_dates": ("2025-3-04",)}, "daily_date_must_be_iso_date"),
        ({"not_before": NOW.replace(tzinfo=None)}, "not_before_must_be_aware"),
        (
            {
                "output_dir": "/Users/harimatakeuchi/stock_range_trader/.delayed_replay/x"
            },
            "output_dir_outside",
        ),
        ({"auth_reference": "OTHER_KEY"}, "unsupported_auth_reference"),
        ({"limits": limits(max_attempts=3)}, "budget_below_fixed_query_count"),
        (
            {"calendar_source_reference": None},
            "calendar_source_reference_must_be_nonempty_text",
        ),
    ],
)
def test_plan_rejects_invalid_scope(changes, reason):
    with pytest.raises(HttpContractError, match=reason):
        plan(**changes)


@pytest.mark.parametrize(
    "bad",
    [True, 3.0, float("nan"), -1, 0, "5"],
)
def test_integer_budget_rejects_bool_float_nan_and_nonpositive(bad):
    with pytest.raises(
        HttpContractError, match="max_attempts_must_be_positive_integer"
    ):
        limits(max_attempts=bad)


CALENDAR_ONLY = dict(kind="calendar_discovery", master_date=None, daily_dates=())


def test_calendar_methods_have_separate_fixed_scope():
    discovery = plan(
        **CALENDAR_ONLY, calendar_source_sha256=None, calendar_source_reference=None
    )
    anchored = plan(kind="calendar_anchored_daily")
    assert discovery.allowed_endpoints == ("/markets/calendar",)
    assert anchored.allowed_endpoints == ("/equities/master", "/equities/bars/daily")
    assert len(anchored.queries) == 3
    assert anchored.scope()["calendar_source_sha256"] == DIGEST
    with pytest.raises(HttpContractError, match="page_outside_fixed_plan"):
        validate_page_request(discovery, page(anchored))
    with pytest.raises(HttpContractError, match="approval_allowed_endpoints_mismatch"):
        check_approval_scope(
            discovery,
            replace(approval(discovery), allowed_endpoints=anchored.allowed_endpoints),
            now=NOW,
        )


@pytest.mark.parametrize(
    "source", [{"calendar_source_sha256": ""}, {"calendar_source_reference": ""}]
)
def test_calendar_discovery_rejects_empty_text_instead_of_a_second_hash(source):
    other = {"calendar_source_sha256": None, "calendar_source_reference": None}
    with pytest.raises(HttpContractError, match="calendar_discovery_must_be_calendar"):
        plan(**CALENDAR_ONLY, **{**other, **source})


# ---------------------------------------------------- High 1: approval binding


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"plan_sha256": "c" * 64}, "approval_plan_sha256_mismatch"),
        ({"scope_sha256": "c" * 64}, "approval_scope_sha256_mismatch"),
        ({"daily_dates": ("2025-03-04",)}, "approval_daily_dates_mismatch"),
        ({"limits": limits(max_saved_bytes=901)}, "approval_limits_mismatch"),
        ({"valid_until": NOW}, "approval_outside_validity_window"),
    ],
)
def test_approval_mismatch_or_expiry_rejected(change, reason):
    fixed = plan()
    with pytest.raises(HttpContractError, match=reason):
        check_approval_scope(fixed, approval(fixed, **change), now=NOW)


def test_approval_daily_dates_with_mixed_types_are_a_contract_error():
    with pytest.raises(HttpContractError, match="approval_daily_date_must_be_text"):
        approval(plan(), daily_dates=("2025-03-03", 1))


def test_approval_content_hash_is_identity_not_authenticity():
    fixed = plan()
    claim = approval(fixed)
    assert claim.sha256 == approval(fixed).sha256
    assert approval(fixed, approval_event_id="other-event").sha256 != claim.sha256
    assert approval(fixed, approver_id="someone-else").sha256 != claim.sha256
    check = check_approval_scope(fixed, claim, now=NOW)
    assert (check.approval_sha256, check.authenticity_verified) == (claim.sha256, False)
    with pytest.raises(TypeError):
        ApprovalScopeCheck(True, claim.sha256, authenticity_verified=True)


def test_every_reservation_records_and_requires_the_bound_approval():
    fixed = plan()
    claim = approval(fixed)
    other = approval(fixed, approval_event_id="substituted-event")
    journal = exchange(journal_for(fixed, claim), page(fixed), 1, NOW, status=429)
    assert journal.events[0].approval_sha256 == claim.sha256
    assert journal.approval_sha256 == claim.sha256
    with pytest.raises(HttpContractError, match="attempt_requires_approval_claim"):
        EvidenceJournal(fixed, journal.data)  # never inferred from the journal
    with pytest.raises(HttpContractError, match="attempt_approval_mismatch"):
        EvidenceJournal(fixed, journal.data, approval=other)
    with pytest.raises(HttpContractError, match="attempt_approval_mismatch"):
        journal.append(
            reserve(
                journal,
                page(fixed),
                2,
                NOW + seconds(120),
                approval_sha256=other.sha256,
            )
        )
    with pytest.raises(HttpContractError, match="attempt_requires_approval_claim"):
        EvidenceJournal(fixed).append(reserve(journal, page(fixed), 1))
    with pytest.raises(HttpContractError, match="approval_plan_sha256_mismatch"):
        EvidenceJournal(fixed, approval=approval(plan(limits=limits(max_attempts=9))))


@pytest.mark.parametrize(
    "at,allowed",
    [
        (NOW - MICRO, False),  # before valid_from
        (NOW, True),  # valid_from is inclusive
        (NOW + seconds(60) - MICRO, True),
        (NOW + seconds(60), False),  # valid_until is exclusive
    ],
)
def test_reservation_must_lie_inside_the_approval_window(at, allowed):
    fixed = plan()
    claim = approval(fixed, valid_from=NOW, valid_until=NOW + seconds(60))
    journal = journal_for(fixed, claim)
    event = reserve(journal, page(fixed), 1, at)
    if allowed:
        assert journal.append(event).event_count == 1
    else:
        with pytest.raises(
            HttpContractError, match="attempt_outside_approval_validity"
        ):
            journal.append(event)


def test_send_is_rechecked_against_approval_expiry():
    fixed = plan()
    claim = approval(fixed, valid_from=NOW, valid_until=NOW + seconds(60))
    journal = journal_for(fixed, claim).append(
        reserve(journal_for(fixed, claim), page(fixed), 1, NOW + seconds(59))
    )
    with pytest.raises(HttpContractError, match="send_outside_approval_validity"):
        journal.append(
            JournalEvent(
                "attempt_sent", NOW + seconds(60), attempt_id=last_attempt_id(journal)
            )
        )


def test_resume_after_approval_expiry_cannot_reserve():
    fixed = plan()
    claim = approval(fixed, valid_until=NOW + seconds(200))
    journal = exchange(journal_for(fixed, claim), page(fixed), 1, NOW, status=503)
    resumed = EvidenceJournal(fixed, journal.data, approval=claim)
    state = budget_snapshot(resumed, BodyInventory(()), now=NOW + seconds(250))
    assert not state.plan_allows_next_attempt
    assert "approval_expired" in state.blocking_reasons
    with pytest.raises(HttpContractError, match="attempt_outside_approval_validity"):
        resumed.append(reserve(resumed, page(fixed), 2, NOW + seconds(250)))


def test_forged_journal_with_attempt_outside_the_approval_is_refused():
    fixed = plan()
    claim = approval(fixed)
    late = NOW + timedelta(minutes=40)  # inside the plan, after the approval
    event = reserve(journal_for(fixed, claim), page(fixed), 1, late)
    data = forge(fixed, b"", event.to_dict())
    with pytest.raises(HttpContractError, match="attempt_outside_approval_validity"):
        EvidenceJournal(fixed, data, approval=claim)


def test_journal_without_approval_identifiers_is_not_upgraded():
    fixed = plan()
    event = reserve(journal_for(fixed), page(fixed), 1).to_dict()
    legacy = {
        k: v
        for k, v in event.items()
        if k
        not in (
            "approval_sha256",
            "allowed_transfer_bytes",
            "allowed_decoded_bytes",
            "allowed_saved_bytes",
            "retry_after_seconds",
        )
    }
    with pytest.raises(HttpContractError, match="journal_chain_or_plan_mismatch"):
        EvidenceJournal(
            fixed,
            forge(fixed, b"", legacy, schema="historical-feasibility-journal-v1"),
            approval=approval(fixed),
        )
    with pytest.raises(HttpContractError, match="journal_event_schema_invalid"):
        EvidenceJournal(fixed, forge(fixed, b"", legacy), approval=approval(fixed))


def test_receipt_binds_the_approval_and_rejects_missing_or_other_approvals():
    fixed = plan()
    claim = approval(fixed)
    journal, inventory, _ = complete_pages(fixed, claim)
    claim_receipt = receipt(journal, inventory)
    assert check_receipt_alignment(journal, inventory, claim_receipt).content_matches
    other = approval(fixed, approval_event_id="substituted-event")
    with pytest.raises(HttpContractError, match="receipt_approval_sha256_mismatch"):
        check_receipt_alignment(
            journal, inventory, replace(claim_receipt, approval_sha256=other.sha256)
        )
    with pytest.raises(HttpContractError, match="receipt_approval_event_id_mismatch"):
        check_receipt_alignment(
            journal, inventory, replace(claim_receipt, approval_event_id="other")
        )
    with pytest.raises(HttpContractError, match="approval_sha256_must_be_sha256"):
        replace(claim_receipt, approval_sha256="")
    with pytest.raises(TypeError):
        ExternalReceiptClaim(
            **{k: v for k, v in vars(claim_receipt).items() if k != "approval_sha256"}
        )
    with pytest.raises(HttpContractError, match="receipt_requires_approval_claim"):
        check_receipt_alignment(
            EvidenceJournal(fixed), BodyInventory(()), claim_receipt
        )


def test_claim_metadata_never_proves_authenticity_or_opens_gate():
    fixed = plan()
    claim = approval(fixed)
    journal = journal_for(fixed, claim)
    inventory = BodyInventory(())
    assert check_approval_scope(fixed, claim, now=NOW).authenticity_verified is False
    assert OwnerApprovalClaim.__dataclass_fields__.get("approved") is None
    gate = assess_live_acquisition_gate(
        fixed, claim, journal, inventory, receipt(journal, inventory), now=NOW
    )
    assert gate.reasons[-5:] == CLOSED_TAIL
    assert not gate.permitted
    assert not assess_live_acquisition_gate(
        fixed, None, journal, inventory, None, now=NOW
    ).permitted


# ------------------------------------------------ High 2: rate limit and waits


def after_response(status: int, *, retry_after=None, **retry_changes):
    fixed = plan(retry=retry(**retry_changes))
    journal = exchange(
        journal_for(fixed), page(fixed), 1, NOW, status=status, retry_after=retry_after
    )
    if status == 200:
        journal = finish_page(journal, NOW, digest=BODY_DIGEST)
    return fixed, journal


@pytest.mark.parametrize(
    "status,retry_after,changes,wait",
    [
        (200, None, {}, 13),  # plain interval
        (429, None, {}, 120),  # 429 wait
        (503, None, {"min_wait_after_5xx_seconds": 30}, 30),
        (429, 300, {}, 300),  # a longer server hint wins
        (429, 5, {}, 120),  # a shorter server hint never shortens the local wait
    ],
)
def test_reservation_respects_the_plan_wait_to_the_microsecond(
    status, retry_after, changes, wait
):
    fixed, journal = after_response(status, retry_after=retry_after, **changes)
    next_page = page(fixed, 1) if status == 200 else page(fixed)
    number = 1 if status == 200 else 2
    with pytest.raises(HttpContractError, match="attempt_before_rate_limit_wait"):
        journal.append(reserve(journal, next_page, number, NOW + seconds(wait) - MICRO))
    assert journal.append(reserve(journal, next_page, number, NOW + seconds(wait)))
    early = budget_snapshot(
        journal, inventory_of(journal), now=NOW + seconds(wait) - MICRO
    )
    assert early.earliest_next_attempt_at == NOW + seconds(wait)
    assert not early.plan_allows_next_attempt
    assert early.blocking_reasons == ("rate_limit_wait",)
    on_time = budget_snapshot(journal, inventory_of(journal), now=NOW + seconds(wait))
    assert on_time.plan_allows_next_attempt and on_time.blocking_reasons == ()
    assert (on_time.next_page, on_time.next_attempt_number) == (next_page, number)


def inventory_of(journal) -> BodyInventory:
    """Committed files exactly matching the journal's completed bodies."""

    files = []
    for event in journal.events:
        if event.kind == "body_saved":
            files.append(
                BodyFileEvidence(
                    f"body-{len(files)}",
                    event.saved_bytes,
                    "committed",
                    event.body_sha256,
                )
            )
    return BodyInventory(tuple(files))


def test_unknown_outcome_counts_and_waits_like_a_network_failure():
    fixed = plan(retry=retry(min_wait_after_network_error_seconds=60))
    journal = journal_for(fixed)
    first = reserve(journal, page(fixed), 1, NOW)
    journal = journal.append(first).append(
        JournalEvent("outcome_unknown", NOW + seconds(5), attempt_id=first.attempt_id)
    )
    with pytest.raises(HttpContractError, match="attempt_before_rate_limit_wait"):
        journal.append(reserve(journal, page(fixed), 2, NOW + seconds(65) - MICRO))
    state = budget_snapshot(journal, BodyInventory(()), now=NOW + seconds(6))
    assert (state.reserved_attempts, state.unknown_outcomes) == (1, 1)
    assert state.earliest_next_attempt_at == NOW + seconds(65)
    assert state.remaining_attempts == fixed.limits.max_attempts - 1


def test_resume_keeps_the_earliest_next_attempt_time():
    fixed, journal = after_response(429)
    resumed = EvidenceJournal(fixed, journal.data, approval=approval(fixed))
    state = budget_snapshot(resumed, BodyInventory(()), now=NOW + seconds(30))
    assert state.earliest_next_attempt_at == NOW + seconds(120)
    assert "rate_limit_wait" in state.blocking_reasons


@pytest.mark.parametrize(
    "plan_changes,claim_changes,reason",
    [
        ({}, {"valid_until": NOW + seconds(100)},
         "approval_expires_before_next_allowed_attempt"),
        ({"expires_at": NOW + seconds(110)}, {"valid_until": NOW + seconds(100)},
         "plan_expires_before_next_allowed_attempt"),
        ({"limits": limits(max_elapsed_seconds=100)}, {},
         "deadline_before_next_allowed_attempt"),
    ],
)  # fmt: skip
def test_expiry_during_a_retry_wait_blocks_the_next_attempt(
    plan_changes, claim_changes, reason
):
    fixed = plan(**plan_changes)
    claim = approval(fixed, **claim_changes)
    journal = exchange(journal_for(fixed, claim), page(fixed), 1, NOW, status=429)
    state = budget_snapshot(journal, BodyInventory(()), now=NOW + seconds(1))
    assert reason in state.blocking_reasons and not state.plan_allows_next_attempt


def test_clock_anomalies_are_rejected():
    fixed = plan()
    journal = journal_for(fixed).append(reserve(journal_for(fixed), page(fixed), 1))
    with pytest.raises(HttpContractError, match="journal_clock_regressed"):
        journal.append(
            JournalEvent(
                "attempt_sent", NOW - MICRO, attempt_id=last_attempt_id(journal)
            )
        )
    with pytest.raises(HttpContractError, match="resume_clock_regressed"):
        budget_snapshot(journal, BodyInventory(()), now=NOW - MICRO)
    with pytest.raises(HttpContractError, match="now_must_be_aware_datetime"):
        budget_snapshot(journal, BodyInventory(()), now=NOW.replace(tzinfo=None))
    with pytest.raises(HttpContractError, match="event_at_must_be_aware_datetime"):
        JournalEvent("run_completed", NOW.replace(tzinfo=None))


def test_send_and_response_are_bounded_by_deadline_and_timeout():
    fixed = plan(limits=limits(max_elapsed_seconds=20))
    journal = journal_for(fixed).append(reserve(journal_for(fixed), page(fixed), 1))
    attempt_id = last_attempt_id(journal)
    with pytest.raises(HttpContractError, match="send_after_cumulative_deadline"):
        journal.append(
            JournalEvent("attempt_sent", NOW + seconds(20), attempt_id=attempt_id)
        )
    sent = journal.append(
        JournalEvent("attempt_sent", NOW + seconds(19), attempt_id=attempt_id)
    )

    def respond(at):
        return JournalEvent(
            "response_received",
            at,
            attempt_id=attempt_id,
            status=200,
            transfer_bytes=10,
            decoded_bytes=20,
        )

    with pytest.raises(HttpContractError, match="response_after_cumulative_deadline"):
        sent.append(respond(NOW + seconds(20) + MICRO))
    assert sent.append(respond(NOW + seconds(20)))
    slow = plan(retry=retry(timeout_seconds=30))
    journal = journal_for(slow).append(reserve(journal_for(slow), page(slow), 1))
    journal = journal.append(
        JournalEvent("attempt_sent", NOW, attempt_id=last_attempt_id(journal))
    )
    with pytest.raises(HttpContractError, match="response_after_timeout"):
        journal.append(
            replace(
                respond(NOW + seconds(30) + MICRO), attempt_id=last_attempt_id(journal)
            )
        )


def test_journal_reserved_sent_429_retry_unknown_counts_survive_resume():
    fixed = plan()
    claim = approval(fixed)
    request = page(fixed)
    journal = exchange(journal_for(fixed, claim), request, 1, NOW, status=429)
    retry_at = NOW + seconds(120)
    second = reserve(journal, request, 2, retry_at)
    journal = journal.append(second).append(
        JournalEvent("attempt_sent", retry_at, attempt_id=second.attempt_id)
    )
    journal = journal.append(
        JournalEvent("outcome_unknown", retry_at, attempt_id=second.attempt_id)
    )
    resumed = EvidenceJournal(fixed, journal.data, approval=claim)
    state = budget_snapshot(resumed, BodyInventory(()), now=retry_at + seconds(13))
    assert (state.reserved_attempts, state.sent_attempts, state.received_responses) == (
        2,
        2,
        1,
    )
    assert (state.retry_attempts, state.http_429_responses, state.unknown_outcomes) == (
        1,
        1,
        1,
    )
    assert state.remaining_attempts == fixed.limits.max_attempts - 2
    assert state.remaining_transfer_bytes == fixed.limits.max_transfer_bytes - 510
    assert state.remaining_decoded_bytes == fixed.limits.max_decoded_bytes - 620
    assert state.elapsed_seconds == 133
    assert state.plan_allows_next_attempt
    assert (state.next_page, state.next_attempt_number) == (request, 3)
    with pytest.raises(HttpContractError, match="journal_chain_or_plan_mismatch"):
        EvidenceJournal(plan(limits=limits(max_attempts=9)), journal.data)


def test_unsettled_or_unknown_attempt_consumes_budget_and_deadline_is_fixed():
    fixed = plan(limits=limits(max_elapsed_seconds=20))
    request = page(fixed)
    first = reserve(journal_for(fixed), request, 1)
    journal = journal_for(fixed).append(first)
    state = budget_snapshot(journal, BodyInventory(()), now=NOW + seconds(5))
    assert state.unsettled_attempts == 1
    assert "unsettled_attempt" in state.blocking_reasons
    journal = journal.append(
        JournalEvent("outcome_unknown", NOW, attempt_id=first.attempt_id)
    )
    assert (
        budget_snapshot(
            journal, BodyInventory(()), now=NOW + seconds(19)
        ).remaining_seconds
        == 1
    )
    late = budget_snapshot(journal, BodyInventory(()), now=NOW + seconds(20))
    assert "time_budget_exhausted" in late.blocking_reasons
    with pytest.raises(HttpContractError, match="attempt_after_cumulative_deadline"):
        journal.append(reserve(journal, request, 2, NOW + seconds(20)))


def test_global_sequential_query_and_nonretryable_response_contract():
    fixed = plan()
    first_page = page(fixed)
    second_query_page = page(fixed, 1)
    journal = journal_for(fixed)
    first = reserve(journal, first_page, 1)
    journal = journal.append(first)
    with pytest.raises(HttpContractError, match="prior_attempt_unsettled"):
        journal.append(reserve(journal, second_query_page, 1, NOW + seconds(13)))
    journal = journal.append(
        JournalEvent("attempt_sent", NOW, attempt_id=first.attempt_id)
    )
    journal = journal.append(
        JournalEvent(
            "response_received",
            NOW,
            attempt_id=first.attempt_id,
            status=403,
            transfer_bytes=0,
            decoded_bytes=0,
        )
    )
    with pytest.raises(HttpContractError, match="previous_attempt_not_retryable"):
        journal.append(reserve(journal, first_page, 2, NOW + seconds(13)))
    with pytest.raises(HttpContractError, match="prior_query_incomplete"):
        journal.append(reserve(journal, second_query_page, 1, NOW + seconds(13)))
    state = budget_snapshot(journal, BodyInventory(()), now=NOW + seconds(13))
    assert "previous_attempt_not_retryable" in state.blocking_reasons


# --------------------------------------------- Medium 4: per-attempt allowances


def byte_plan(**changes) -> HttpAcquisitionPlan:
    values = dict(
        max_transfer_bytes=1000,
        max_page_transfer_bytes=500,
        max_decoded_bytes=10000,
        max_page_decoded_bytes=5000,
        max_saved_bytes=10000,
        max_page_saved_bytes=5000,
    )
    values.update(changes)
    return plan(limits=limits(**values))


def pages_with(fixed, transfers, *, saved=20, decoded=20):
    journal, at = journal_for(fixed), NOW
    for index, transfer in enumerate(transfers):
        journal = exchange(
            journal, page(fixed, index), 1, at, transfer=transfer, decoded=decoded
        )
        journal = finish_page(journal, at, digest=f"{index + 1:064x}", saved=saved)
        at += seconds(13)
    return journal, at


def test_allowance_is_the_smaller_of_page_cap_and_remaining_budget():
    fixed = byte_plan()
    journal, at = pages_with(fixed, [500, 499])  # 1 byte of transfer budget left
    assert journal.next_reservation_allowances[0] == 1
    state = budget_snapshot(journal, inventory_of(journal), now=at)
    assert state.next_allowed_transfer_bytes == 1 and state.plan_allows_next_attempt
    with pytest.raises(HttpContractError, match="reserved_allowance_invalid"):
        journal.append(
            reserve(journal, page(fixed, 2), 1, at, allowed_transfer_bytes=500)
        )
    over = exchange(journal, page(fixed, 2), 1, at, transfer=2)  # beyond the 1 byte
    with pytest.raises(HttpContractError, match="body_save_order_invalid"):
        finish_page(over, at, digest=DIGEST)
    blocked = budget_snapshot(over, inventory_of(over), now=at + seconds(13))
    assert "budget_overrun_recorded" in blocked.blocking_reasons
    assert over.next_reservation_allowances[0] < 0  # the overrun is on record
    with pytest.raises(HttpContractError, match="previous_page_budget_overrun"):
        over.append(
            reserve(over, page(fixed, 2), 2, at + seconds(13), allowed_transfer_bytes=1)
        )


def test_remaining_below_page_cap_exact_fill_and_zero():
    fixed = byte_plan()
    journal, at = pages_with(fixed, [500, 200])  # 300 left, page cap 500
    assert journal.next_reservation_allowances[0] == 300
    journal = exchange(journal, page(fixed, 2), 1, at, transfer=300)  # exactly the cap
    journal = finish_page(journal, at, digest=DIGEST)
    assert journal.next_reservation_allowances[0] == 0
    state = budget_snapshot(journal, inventory_of(journal), now=at + seconds(13))
    assert state.next_allowed_transfer_bytes == 0
    assert "transfer_budget_exhausted" in state.blocking_reasons
    with pytest.raises(HttpContractError, match="transfer_budget_exhausted"):
        journal.append(
            reserve(
                journal,
                page(fixed, 3),
                1,
                at + seconds(13),
                allowed_transfer_bytes=1,
            )
        )


def test_decoded_and_saved_allowances_are_separate_budgets():
    decoded_plan = byte_plan(max_decoded_bytes=100, max_page_decoded_bytes=60)
    journal, at = pages_with(decoded_plan, [10], decoded=60)
    assert journal.next_reservation_allowances[1] == 40
    over = exchange(journal, page(decoded_plan, 1), 1, at, transfer=10, decoded=41)
    assert budget_snapshot(over, inventory_of(over), now=at).blocking_reasons[:1] == (
        "budget_overrun_recorded",
    )
    saved_plan = byte_plan(max_saved_bytes=50, max_page_saved_bytes=40)
    journal, at = pages_with(saved_plan, [10], saved=40)
    assert journal.next_reservation_allowances[2] == 10
    journal = exchange(journal, page(saved_plan, 1), 1, at)
    with pytest.raises(
        HttpContractError, match="saved_bytes_exceed_reserved_allowance"
    ):
        finish_page(journal, at, digest=DIGEST, saved=11)
    assert finish_page(journal, at, digest=DIGEST, saved=10)


def test_unknown_outcomes_keep_reserving_a_full_page():
    fixed = byte_plan()
    journal = journal_for(fixed)
    at = NOW
    for number in (1, 2):
        event = reserve(journal, page(fixed), number, at)
        assert event.allowed_transfer_bytes == 500
        journal = journal.append(event).append(
            JournalEvent("outcome_unknown", at, attempt_id=event.attempt_id)
        )
        at += seconds(13)
    assert journal.next_reservation_allowances[0] == 0
    state = budget_snapshot(journal, BodyInventory(()), now=at)
    assert state.remaining_transfer_bytes == 0
    assert "transfer_budget_exhausted" in state.blocking_reasons


def test_page_size_overrun_is_recordable_but_blocks_another_attempt():
    fixed = plan(limits=limits(max_page_transfer_bytes=10))
    journal = exchange(journal_for(fixed), page(fixed), 1, NOW, status=429, transfer=11)
    assert budget_snapshot(journal, BodyInventory(()), now=NOW).received_responses == 1
    with pytest.raises(HttpContractError, match="previous_page_budget_overrun"):
        journal.append(reserve(journal, page(fixed), 2, NOW + seconds(120)))
    with pytest.raises(HttpContractError, match="run_completed_with_missing_page"):
        journal.append(JournalEvent("run_completed", NOW + seconds(120)))


# ------------------------------------------ Medium 5: inventory and receipt set


def completed_evidence():
    fixed = plan()
    journal, inventory, at = complete_pages(fixed)
    return fixed, journal, inventory, at


def test_normal_completed_body_set_aligns_with_the_receipt():
    fixed, journal, inventory, at = completed_evidence()
    claim = receipt(journal, inventory)
    alignment = check_receipt_alignment(journal, inventory, claim)
    assert alignment.content_matches and not alignment.independent_custody_verified
    with pytest.raises(TypeError):
        ReceiptAlignment(True, independent_custody_verified=True)
    assert budget_snapshot(journal, inventory, now=at).completed_pages == 4


@pytest.mark.parametrize(
    "change",
    ["add_partial", "add_orphan", "committed_to_orphan", "remove", "resize", "rehash"],
)
def test_inventory_changes_are_detected_against_receipt_and_journal(change):
    fixed, journal, inventory, _ = completed_evidence()
    claim = receipt(journal, inventory)
    files = list(inventory.files)
    if change == "add_partial":
        files.append(BodyFileEvidence("tmp-1", 7, "partial", "c" * 64))
    elif change == "add_orphan":
        files.append(BodyFileEvidence("stray-1", 7, "orphan", "c" * 64))
    elif change == "committed_to_orphan":
        files[0] = replace(files[0], state="orphan")
    elif change == "remove":
        files.pop()
    elif change == "resize":
        files[0] = replace(files[0], size=21)
    else:
        files[0] = replace(files[0], body_sha256="c" * 64)
    changed = BodyInventory(tuple(files))
    assert changed.body_set_sha256 != inventory.body_set_sha256
    with pytest.raises(HttpContractError):
        check_receipt_alignment(journal, changed, claim)


def test_body_state_rules_against_the_journal():
    fixed, journal, inventory, _ = completed_evidence()
    first = inventory.files[0]
    with pytest.raises(HttpContractError, match="completed_page_body_not_committed"):
        budget_snapshot(
            journal,
            BodyInventory((replace(first, state="orphan"), *inventory.files[1:])),
            now=NOW + seconds(60),
        )
    with pytest.raises(HttpContractError, match="recorded_body_declared_partial"):
        budget_snapshot(
            journal,
            BodyInventory((replace(first, state="partial"), *inventory.files[1:])),
            now=NOW + seconds(60),
        )
    with pytest.raises(HttpContractError, match="duplicate_stored_body"):
        BodyInventory((first, replace(first, object_id="copy", state="orphan")))
    with pytest.raises(HttpContractError, match="unrecorded_committed_body"):
        budget_snapshot(
            journal,
            BodyInventory(
                (*inventory.files, BodyFileEvidence("x", 5, "committed", DIGEST))
            ),
            now=NOW + seconds(60),
        )


def test_saved_body_without_page_completion_cannot_be_declared_committed():
    fixed = plan()
    journal = exchange(journal_for(fixed), page(fixed), 1, NOW)
    journal = journal.append(
        JournalEvent(
            "body_saved",
            NOW,
            attempt_id=last_attempt_id(journal),
            body_sha256=BODY_DIGEST,
            saved_bytes=20,
        )
    )
    committed = BodyInventory((BodyFileEvidence("b", 20, "committed", BODY_DIGEST),))
    with pytest.raises(
        HttpContractError, match="committed_body_without_completed_page"
    ):
        check_receipt_alignment(journal, committed, receipt(journal, committed))
    orphan = BodyInventory((BodyFileEvidence("b", 20, "orphan", BODY_DIGEST),))
    state = budget_snapshot(journal, orphan, now=NOW + seconds(1))
    assert {"unsettled_attempt", "orphan_or_partial_body_present"} <= set(
        state.blocking_reasons
    )


def test_body_inventory_counts_orphan_and_partial_without_reset():
    fixed = plan(limits=limits(max_saved_bytes=25, max_page_saved_bytes=25))
    inventory = BodyInventory(
        (
            BodyFileEvidence("orphan-1", 20, "orphan", BODY_DIGEST),
            BodyFileEvidence("partial-1", 5, "partial", DIGEST),
        )
    )
    state = budget_snapshot(journal_for(fixed), inventory, now=NOW)
    assert state.orphan_files == state.partial_files == 1
    assert state.remaining_saved_bytes == 0
    assert not state.plan_allows_next_attempt
    under_budget = budget_snapshot(
        journal_for(plan()),
        BodyInventory((BodyFileEvidence("orphan-only", 1, "orphan", BODY_DIGEST),)),
        now=NOW,
    )
    assert under_budget.remaining_saved_bytes > 0
    assert "orphan_or_partial_body_present" in under_budget.blocking_reasons


def test_completed_page_body_and_receipt_tamper_rejection():
    fixed, journal, inventory, _ = completed_evidence()
    claim = receipt(journal, inventory)
    with pytest.raises(HttpContractError, match="receipt_ledger_head_sha256_mismatch"):
        check_receipt_alignment(
            journal, inventory, replace(claim, ledger_head_sha256=DIGEST)
        )
    with pytest.raises(HttpContractError, match="receipt_body_set_sha256_mismatch"):
        check_receipt_alignment(
            journal, inventory, replace(claim, body_set_sha256=DIGEST)
        )
    with pytest.raises(HttpContractError, match="receipt_plan_sha256_mismatch"):
        check_receipt_alignment(journal, inventory, replace(claim, plan_sha256=DIGEST))


# ------------------------------------------------ journal integrity and bookkeeping


def test_journal_detects_line_deletion_and_noncanonical_or_modified_record():
    fixed = plan()
    claim = approval(fixed)
    journal = exchange(journal_for(fixed, claim), page(fixed), 1, NOW, status=429)
    with pytest.raises(HttpContractError, match="journal_incomplete_final_line"):
        EvidenceJournal(fixed, journal.data[:-1], approval=claim)
    lines = journal.data.splitlines(keepends=True)
    with pytest.raises(HttpContractError, match="journal_chain_or_plan_mismatch"):
        EvidenceJournal(fixed, b"".join(lines[1:]), approval=claim)
    record = json.loads(lines[0])
    record["event"]["attempt_number"] = 2
    with pytest.raises(
        HttpContractError, match="journal_record_noncanonical|journal_chain"
    ):
        EvidenceJournal(
            fixed,
            (json.dumps(record) + "\n").encode() + b"".join(lines[1:]),
            approval=claim,
        )
    # A cleanly removed *tail* cannot be detected without an independently fixed receipt.
    truncated = EvidenceJournal(fixed, b"".join(lines[:-1]), approval=claim)
    assert truncated.event_count == journal.event_count - 1
    with pytest.raises(HttpContractError, match="receipt_ledger_event_count_mismatch"):
        check_receipt_alignment(
            truncated, BodyInventory(()), receipt(journal, BodyInventory(()))
        )


def test_journal_rejects_nonfinite_json_as_contract_error():
    fixed = plan()
    journal = journal_for(fixed).append(reserve(journal_for(fixed), page(fixed), 1))
    record = json.loads(journal.data)
    record["event"]["status"] = float("nan")
    malformed = (json.dumps(record) + "\n").encode("utf-8")
    with pytest.raises(HttpContractError, match="journal_record_noncanonical"):
        EvidenceJournal(fixed, malformed, approval=approval(fixed))


def test_complete_journal_is_idempotent_and_rejects_a_second_attempt():
    fixed = plan()
    claim = approval(fixed)
    journal, inventory, at = complete_pages(fixed, claim)
    journal = journal.append(JournalEvent("run_completed", at))
    resumed = EvidenceJournal(fixed, journal.data, approval=claim)
    state = budget_snapshot(resumed, inventory, now=at + seconds(1))
    assert resumed.completed
    assert state.status == "completed"
    assert state.blocking_reasons == ("run_completed",)
    assert state.completed_pages == len(fixed.queries)
    assert not state.plan_allows_next_attempt
    with pytest.raises(HttpContractError, match="journal_event_after_terminal_state"):
        resumed.append(reserve(resumed, page(fixed), 2, at + seconds(62)))


def test_completion_may_be_recorded_after_the_deadline_but_attempts_may_not():
    fixed = plan(limits=limits(max_elapsed_seconds=60))
    journal, inventory, at = complete_pages(fixed)  # last reservation at +39s
    finished = journal.append(JournalEvent("run_completed", NOW + seconds(100)))
    assert finished.completed
    incomplete = journal_for(fixed)
    incomplete = exchange(incomplete, page(fixed), 1, NOW, status=503)
    with pytest.raises(HttpContractError, match="attempt_after_cumulative_deadline"):
        incomplete.append(reserve(incomplete, page(fixed), 2, NOW + seconds(60)))


def test_page_continuation_requires_exact_key_and_rejects_loop_or_repeated_body():
    fixed = plan()
    request = page(fixed)
    journal = exchange(journal_for(fixed), request, 1, NOW)
    journal = finish_page(journal, NOW, digest=BODY_DIGEST, next_key="page-two")
    with pytest.raises(HttpContractError, match="pagination_chain_invalid"):
        journal.append(
            reserve(
                journal,
                PageRequest(request.query, 1, "wrong-key"),
                1,
                NOW + seconds(13),
            )
        )
    second = PageRequest(request.query, 1, "page-two")
    journal = exchange(journal, second, 1, NOW + seconds(13))
    with pytest.raises(HttpContractError, match="repeated_page_body"):
        finish_page(journal, NOW + seconds(13), digest=BODY_DIGEST)
    with pytest.raises(HttpContractError, match="pagination_key_loop"):
        finish_page(journal, NOW + seconds(13), digest=DIGEST, next_key="page-two")


def test_identical_bodies_of_different_queries_are_stored_once():
    fixed = plan()
    journal = exchange(journal_for(fixed), page(fixed), 1, NOW)
    journal = finish_page(journal, NOW, digest=BODY_DIGEST)
    journal = exchange(journal, page(fixed, 1), 1, NOW + seconds(13))
    journal = finish_page(journal, NOW + seconds(13), digest=BODY_DIGEST)
    inventory = BodyInventory((BodyFileEvidence("b", 20, "committed", BODY_DIGEST),))
    state = budget_snapshot(journal, inventory, now=NOW + seconds(26))
    assert state.completed_pages == 2 and state.remaining_saved_bytes == 880


def test_incremental_append_equals_full_reopen():
    fixed = plan()
    claim = approval(fixed)
    journal = exchange(journal_for(fixed, claim), page(fixed), 1, NOW, status=429)
    second = reserve(journal, page(fixed), 2, NOW + seconds(120))
    journal = journal.append(second).append(
        JournalEvent(
            "outcome_unknown", NOW + seconds(121), attempt_id=second.attempt_id
        )
    )
    journal = exchange(journal, page(fixed), 3, NOW + seconds(134))
    journal = finish_page(journal, NOW + seconds(134), digest=BODY_DIGEST)
    reopened = EvidenceJournal(fixed, journal.data, approval=claim)
    inventory = inventory_of(journal)
    later = NOW + seconds(200)
    assert reopened.data == journal.data
    assert reopened.head_hash == journal.head_hash
    assert reopened.byte_count == journal.byte_count == len(journal.data)
    assert reopened.events == journal.events
    assert budget_snapshot(reopened, inventory, now=later) == budget_snapshot(
        journal, inventory, now=later
    )


# --------------------------------------------- Medium 3: calendar anchor evidence

CAL_START, CAL_END = "2025-02-24", "2025-03-04"  # Monday .. Tuesday


def calendar_body(*, drop=None, holiday=None) -> bytes:
    rows, day = [], date(2025, 2, 24)
    while day <= date(2025, 3, 4):
        text = day.isoformat()
        if text != drop:
            session = day.weekday() < 5 and text != holiday
            rows.append({"Date": text, "HolDiv": "1" if session else "0"})
        day += timedelta(days=1)
    return json.dumps({"data": rows}).encode()


def discovery_evidence(
    body=None, *, complete=True, artifact="artificial-calendar", bodies=None
) -> CalendarDiscoveryEvidence:
    fixed = plan(
        **CALENDAR_ONLY,
        artifact_id=artifact,
        calendar_start=CAL_START,
        calendar_end=CAL_END,
        calendar_source_sha256=None,
        calendar_source_reference=None,
        output_dir=str(HTTP_OUTPUT_ROOT / artifact),
        limits=limits(
            max_transfer_bytes=5000,
            max_page_transfer_bytes=2000,
            max_decoded_bytes=5000,
            max_page_decoded_bytes=2000,
            max_saved_bytes=5000,
            max_page_saved_bytes=2000,
        ),
    )
    claim = approval(fixed)
    body = body or calendar_body()
    digest = hashlib.sha256(body).hexdigest()
    journal = exchange(
        journal_for(fixed, claim),
        page(fixed),
        1,
        NOW,
        transfer=len(body),
        decoded=len(body),
    )
    journal = finish_page(journal, NOW, digest=digest, saved=len(body))
    if complete:
        journal = journal.append(JournalEvent("run_completed", NOW))
    inventory = BodyInventory(
        (BodyFileEvidence("calendar-0", len(body), "committed", digest),)
    )
    return CalendarDiscoveryEvidence(
        fixed,
        claim,
        journal.data,
        inventory,
        receipt(journal, inventory),
        bodies if bodies is not None else (body,),
    )


def anchored_plan(evidence, **changes) -> HttpAcquisitionPlan:
    values = dict(
        artifact_id="artificial-daily",
        kind="calendar_anchored_daily",
        calendar_start="2025-03-03",
        calendar_end="2025-03-04",
        daily_dates=("2025-03-03", "2025-03-04"),
        calendar_source_sha256=derive_calendar_anchor(evidence).anchor_sha256,
        calendar_source_reference=calendar_anchor_reference("artificial-calendar"),
        output_dir=str(HTTP_OUTPUT_ROOT / "artificial-daily"),
    )
    values.update(changes)
    return plan(**values)


def test_anchor_is_derived_from_a_completed_discovery_and_verified():
    evidence = discovery_evidence()
    anchor = derive_calendar_anchor(evidence)
    assert anchor.sessions == (
        "2025-02-24", "2025-02-25", "2025-02-26", "2025-02-27", "2025-02-28",
        "2025-03-03", "2025-03-04",
    )  # fmt: skip
    daily = anchored_plan(evidence)
    assert verify_calendar_anchor(daily, evidence) == anchor
    assert not anchor.independent_custody_verified
    with pytest.raises(TypeError):
        CalendarAnchor("a", DIGEST, CAL_START, CAL_END, (), DIGEST, True)
    claim = approval(daily)
    journal = journal_for(daily, claim)
    inventory = BodyInventory(())
    verified = assess_live_acquisition_gate(
        daily, claim, journal, inventory, receipt(journal, inventory), now=NOW,
        calendar_evidence=evidence,
    )  # fmt: skip
    declared = assess_live_acquisition_gate(
        daily, claim, journal, inventory, receipt(journal, inventory), now=NOW
    )
    assert not any(r.startswith("calendar_anchor") for r in verified.reasons)
    assert "calendar_anchor_declared_unverified" in declared.reasons
    assert not verified.permitted and not declared.permitted


@pytest.mark.parametrize(
    "build,reason",
    [
        (lambda ev: anchored_plan(ev, calendar_source_sha256=DIGEST),
         "calendar_anchor_hash_mismatch"),
        (lambda ev: anchored_plan(ev, calendar_source_reference="anything"),
         "calendar_anchor_reference_mismatch"),
        (lambda ev: anchored_plan(ev, calendar_start="2025-03-01",
                                  daily_dates=("2025-03-01", "2025-03-04")),
         "daily_dates_include_non_sessions"),
        (lambda ev: anchored_plan(ev, reference_date="2025-03-02", master_date="2025-03-02",
                                  calendar_start="2025-03-01", calendar_end="2025-03-02",
                                  daily_dates=("2025-03-02",)),
         "reference_date_not_a_session"),
        (lambda ev: anchored_plan(ev, calendar_start="2025-02-20"),
         "anchored_window_outside_calendar_evidence"),
        (lambda ev: anchored_plan(ev, artifact_id="artificial-calendar",
                                  output_dir=str(HTTP_OUTPUT_ROOT / "artificial-calendar")),
         "anchored_plan_reuses_discovery_artifact"),
    ],
)  # fmt: skip
def test_anchored_plan_mismatches_are_rejected(build, reason):
    evidence = discovery_evidence()
    with pytest.raises(HttpContractError, match=reason):
        verify_calendar_anchor(build(evidence), evidence)


def test_anchor_from_another_or_altered_calendar_is_rejected():
    daily = anchored_plan(discovery_evidence())
    with pytest.raises(HttpContractError, match="calendar_anchor_hash_mismatch"):
        verify_calendar_anchor(
            daily, discovery_evidence(calendar_body(holiday="2025-03-03"))
        )
    with pytest.raises(HttpContractError, match="calendar_anchor_reference_mismatch"):
        verify_calendar_anchor(
            daily, discovery_evidence(artifact="artificial-calendar-b")
        )


@pytest.mark.parametrize(
    "evidence,reason",
    [
        (lambda: discovery_evidence(complete=False), "calendar_evidence_run_incomplete"),
        (lambda: discovery_evidence(bodies=(calendar_body(holiday="2025-03-03"),)),
         "calendar_body_set_mismatch"),
        (lambda: discovery_evidence(bodies=()), "calendar_body_set_mismatch"),
        (lambda: discovery_evidence(calendar_body(drop="2025-03-01")),
         "calendar_rows_incomplete"),
        (lambda: replace(discovery_evidence(), plan=plan()),
         "calendar_evidence_plan_not_discovery"),
    ],
)  # fmt: skip
def test_incomplete_tampered_or_foreign_calendar_evidence_is_rejected(evidence, reason):
    with pytest.raises(HttpContractError, match=reason):
        derive_calendar_anchor(evidence())


def test_predeclared_source_stays_declared_and_unverified():
    fixed = plan()
    claim = approval(fixed)
    journal = journal_for(fixed, claim)
    gate = assess_live_acquisition_gate(
        fixed, claim, journal, BodyInventory(()), None, now=NOW
    )
    assert "calendar_source_declared_unverified" in gate.reasons


# ------------------------------------------ gate result objects and entry point


def test_gate_reports_inconsistent_evidence_as_reasons_not_exceptions():
    fixed, journal, inventory, at = completed_evidence()
    claim = journal.approval
    gate = assess_live_acquisition_gate(
        fixed, claim, journal, BodyInventory(()), receipt(journal, inventory), now=at
    )
    assert "budget_state_invalid:saved_body_missing_or_size_mismatch" in gate.reasons
    assert (
        "external_receipt_content_invalid:saved_body_missing_or_size_mismatch"
        in gate.reasons
    )
    other = approval(fixed, approval_event_id="substituted-event")
    swapped = assess_live_acquisition_gate(
        fixed, other, journal, inventory, None, now=at
    )
    assert "journal_invalid:attempt_approval_mismatch" in swapped.reasons
    assert gate.reasons[-5:] == swapped.reasons[-5:] == CLOSED_TAIL


def test_result_objects_cannot_be_constructed_as_permission():
    with pytest.raises(TypeError):
        LiveAcquisitionGate(reasons=(), permitted=True)
    assert LiveAcquisitionGate(reasons=()).permitted is False


def test_entry_point_takes_raw_evidence_and_always_closes():
    fixed = plan()
    claim = approval(fixed)
    journal = journal_for(fixed, claim)
    inventory = BodyInventory(())
    fake_permission = LiveAcquisitionGate(reasons=())
    for bad in (
        dict(approval=fake_permission),
        dict(approval=check_approval_scope(fixed, claim, now=NOW)),
        dict(receipt=ReceiptAlignment(True)),
    ):
        arguments = dict(approval=claim, receipt=receipt(journal, inventory)) | bad
        with pytest.raises(HttpContractError, match="raw_evidence_required"):
            require_live_acquisition_permission(
                fixed,
                arguments["approval"],
                journal,
                inventory,
                arguments["receipt"],
                now=NOW,
            )
    with pytest.raises(LiveAcquisitionClosed) as closed:
        require_live_acquisition_permission(
            fixed, claim, journal, inventory, receipt(journal, inventory), now=NOW
        )
    assert closed.value.reasons[-5:] == CLOSED_TAIL


def test_offline_entry_still_rejects_nonfixture_before_creating_store(tmp_path):
    output = tmp_path / "not-created"
    with pytest.raises(AcquisitionStopped, match="only_offline_fixture"):
        run_offline_fixture_acquisition(
            output, plan(), object(), clock=lambda: NOW, sleep=lambda _: None
        )
    assert not output.exists()
    assert OfflineFixtureTransport.__name__ == "OfflineFixtureTransport"


# ------------------------------------------- round 3, High 1: fixed plan scope

OUTSIDE = DateQuery(DAILY, market_date="2025-02-03")


def test_plan_scope_cannot_be_widened_through_public_or_input_objects():
    with pytest.raises(HttpContractError, match="daily_dates_must_be_tuple"):
        plan(daily_dates=["2025-03-04", "2025-03-03"])
    fixed = plan()
    with pytest.raises(TypeError):
        fixed.query_positions[OUTSIDE] = 0  # read-only view
    for name, value in (
        ("daily_dates", ("2025-02-03", "2025-03-04")),
        ("_queries", fixed.queries + (OUTSIDE,)),
        ("_sha256", DIGEST),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(fixed, name, value)
    assert OUTSIDE not in fixed.query_positions
    assert fixed.queries == plan().queries and fixed.sha256 == plan().sha256


def test_reservation_outside_the_fixed_dates_is_refused_without_side_effects():
    fixed = plan()
    journal = exchange(journal_for(fixed), page(fixed), 1, NOW, status=503)
    before = (journal.data, budget_snapshot(journal, BodyInventory(()), now=NOW))
    with pytest.raises(HttpContractError, match="page_outside_fixed_plan"):
        journal.append(
            reserve(
                journal,
                PageRequest(OUTSIDE, 0),
                1,
                NOW + seconds(13),
                attempt_id=DIGEST,
            )
        )
    assert (
        journal.data,
        budget_snapshot(journal, BodyInventory(()), now=NOW),
    ) == before


@pytest.mark.parametrize(
    "name,value",
    [
        ("_queries", lambda p: p.queries + (OUTSIDE,)),
        ("_positions", lambda p: {**p.query_positions, OUTSIDE: len(p.queries)}),
        ("_sha256", lambda p: DIGEST),
        ("daily_dates", lambda p: ("2025-02-03", *p.daily_dates)),
    ],
)
def test_tampered_derived_scope_under_the_same_hash_is_detected(name, value):
    fixed = plan()
    object.__setattr__(fixed, name, value(fixed))  # deliberate in-process tamper
    with pytest.raises(HttpContractError, match="plan_fixed_scope_inconsistent"):
        EvidenceJournal(fixed, approval=None)
    with pytest.raises(HttpContractError, match="plan_fixed_scope_inconsistent"):
        EvidenceJournal(fixed, approval=approval(plan()))


def test_journal_with_an_out_of_plan_reservation_is_refused_on_reopen():
    fixed = plan()
    event = JournalEvent(
        "attempt_reserved",
        NOW,
        attempt_id=DIGEST,
        page=PageRequest(OUTSIDE, 0),
        attempt_number=1,
        approval_sha256=approval(fixed).sha256,
        allowed_transfer_bytes=500,
        allowed_decoded_bytes=600,
        allowed_saved_bytes=450,
    )
    data = forge(fixed, b"", event.to_dict())
    with pytest.raises(HttpContractError, match="page_outside_fixed_plan"):
        EvidenceJournal(fixed, data, approval=approval(fixed))


def test_normal_plan_runs_to_completion_and_resumes():
    fixed = plan()
    claim = approval(fixed)
    journal, inventory, at = complete_pages(fixed, claim)
    journal = journal.append(JournalEvent("run_completed", at))
    resumed = EvidenceJournal(fixed, journal.data, approval=claim)
    assert resumed.completed and resumed.data == journal.data
    assert budget_snapshot(resumed, inventory, now=at).completed_pages == 4
    assert plan(account_ref="another-account").sha256 != fixed.sha256


# ------------------------------------------ round 3, High 2: account rate ledger

PLAN_B = dict(
    artifact_id="artificial-other",
    output_dir=str(HTTP_OUTPUT_ROOT / "artificial-other"),
)


def account_policy(**changes) -> AccountRatePolicy:
    values = dict(
        min_interval_seconds=13,
        min_wait_after_429_seconds=120,
        min_wait_after_5xx_seconds=13,
        min_wait_after_network_error_seconds=13,
        request_timeout_seconds=30,
        slot_lease_seconds=60,
    )
    values.update(changes)
    return AccountRatePolicy(**values)


def open_account(ref=ACCOUNT, **changes) -> AccountRateLedger:
    opened = AccountRateEvent(
        "ledger_opened", NOW - seconds(60), policy=account_policy(**changes)
    )
    return AccountRateLedger(ref).append(opened)


def slot_event(kind, at, slot_id, *, plan_sha=None, holder="runner-a", **extra):
    return AccountRateEvent(
        kind, at, slot_id=slot_id, plan_sha256=plan_sha, holder_id=holder, **extra
    )


def outcome_of(status: int) -> str:
    return "429" if status == 429 else "5xx" if 500 <= status < 600 else "response"


def required_wait(fixed, outcome, retry_after=None) -> int:
    rule = fixed.retry
    specific = {
        "response": 0,
        "429": rule.min_wait_after_429_seconds,
        "5xx": rule.min_wait_after_5xx_seconds,
        "unknown": rule.min_wait_after_network_error_seconds,
    }[outcome]
    return max(
        rule.min_interval_seconds,
        specific,
        account_policy().wait_after(outcome),
        retry_after or 0,
    )


def settle(ledger, fixed, slot_id, at, outcome, *, observed=None, extra=0, **kw):
    return ledger.append(
        slot_event(
            "slot_settled",
            at,
            slot_id,
            outcome=outcome,
            observed_retry_after_seconds=observed,
            effective_wait_seconds=required_wait(fixed, outcome, observed) + extra,
            **kw,
        )
    )


def paired_until_send(journal, ledger, request, number=1, at=NOW, *, holder="runner-a"):
    """Protocol steps 1-4: account reserve, plan reserve, plan sent, account sent."""

    event = reserve(journal, request, number, at)
    ledger = ledger.append(
        slot_event(
            "slot_reserved",
            at,
            event.attempt_id,
            plan_sha=journal.plan.sha256,
            holder=holder,
        )
    )
    journal = journal.append(event)
    journal = journal.append(
        JournalEvent("attempt_sent", at, attempt_id=event.attempt_id)
    )
    ledger = ledger.append(slot_event("slot_sent", at, event.attempt_id, holder=holder))
    return journal, ledger


def plan_response(journal, at, status, *, retry_after=None, transfer=10, decoded=20):
    return journal.append(
        JournalEvent(
            "response_received",
            at,
            attempt_id=last_attempt_id(journal),
            status=status,
            transfer_bytes=transfer,
            decoded_bytes=decoded,
            retry_after_seconds=retry_after,
        )
    )


def paired_exchange(
    journal, ledger, request, number=1, at=NOW, *, status=200, retry_after=None,
    settle_at=None, extra=0, holder="runner-a", **response,
):  # fmt: skip
    """All protocol steps: the account copies the plan's observed result."""

    journal, ledger = paired_until_send(
        journal, ledger, request, number, at, holder=holder
    )
    journal = plan_response(journal, at, status, retry_after=retry_after, **response)
    ledger = settle(
        ledger,
        journal.plan,
        last_attempt_id(journal),
        settle_at or at,
        outcome_of(status),
        observed=retry_after,
        extra=extra,
        holder=holder,
    )
    return journal, ledger


def run_plan_a(status=200, *, retry_after=None, extra=0):
    fixed = plan()
    return paired_exchange(
        journal_for(fixed),
        open_account(),
        page(fixed),
        status=status,
        retry_after=retry_after,
        extra=extra,
    )


def account_view(ledger, fixed, at, *journals):
    return assess_account_slot(ledger, fixed, now=at, plan_journals=journals)


@pytest.mark.parametrize("status,wait", [(200, 13), (429, 120), (503, 13)])
def test_plan_b_inherits_plan_a_waits_on_the_same_account(status, wait):
    journal_a, ledger = run_plan_a(status)
    fixed_b = plan(**PLAN_B)
    journal_b = journal_for(fixed_b)
    # Plan B's own journal is empty: plan-local rules alone would allow it ...
    assert budget_snapshot(
        journal_b, BodyInventory(()), now=NOW + seconds(1)
    ).plan_allows_next_attempt
    # ... but the shared account state does not.
    early = account_view(ledger, fixed_b, NOW + seconds(1), journal_a)
    assert not early.account_allows_next_attempt
    assert early.blocking_reasons == ("account_rate_limit_wait",)
    assert early.earliest_next_slot_at == NOW + seconds(wait)
    edge = account_view(ledger, fixed_b, NOW + seconds(wait) - MICRO, journal_a)
    assert not edge.account_allows_next_attempt
    assert account_view(
        ledger, fixed_b, NOW + seconds(wait), journal_a
    ).account_allows_next_attempt
    attempt = reserve(journal_b, page(fixed_b), 1, NOW + seconds(wait) - MICRO)
    with pytest.raises(HttpContractError, match="account_rate_limit_wait"):
        ledger.append(
            slot_event(
                "slot_reserved",
                NOW + seconds(wait) - MICRO,
                attempt.attempt_id,
                plan_sha=fixed_b.sha256,
            )
        )
    assert ledger.append(
        slot_event(
            "slot_reserved",
            NOW + seconds(wait),
            attempt.attempt_id,
            plan_sha=fixed_b.sha256,
        )
    )


def test_calendar_plan_completion_then_daily_plan_waits_on_the_account():
    evidence = discovery_evidence()
    body = calendar_body()
    calendar, ledger = paired_exchange(
        journal_for(evidence.plan, evidence.approval),
        open_account(),
        page(evidence.plan),
        transfer=len(body),
        decoded=len(body),
    )
    calendar = finish_page(
        calendar, NOW, digest=hashlib.sha256(body).hexdigest(), saved=len(body)
    )
    calendar = calendar.append(JournalEvent("run_completed", NOW))
    daily = anchored_plan(evidence)
    assert not account_view(
        ledger, daily, NOW + seconds(13) - MICRO, calendar
    ).account_allows_next_attempt
    assert account_view(
        ledger, daily, NOW + seconds(13), calendar
    ).account_allows_next_attempt
    missing = account_view(ledger, daily, NOW + seconds(13))
    assert missing.blocking_reasons == ("account_plan_journal_missing",)


def test_other_account_is_separate_and_mismatched_ledgers_are_refused():
    journal_a, ledger = run_plan_a(429)
    other = plan(**PLAN_B, account_ref="other-account")
    own = open_account("other-account")
    assert account_view(own, other, NOW + seconds(1)).account_allows_next_attempt
    assert (
        "account_ref_mismatch"
        in account_view(ledger, other, NOW + seconds(1), journal_a).blocking_reasons
    )
    with pytest.raises(HttpContractError, match="account_ledger_account_mismatch"):
        AccountRateLedger("other-account", ledger.data)
    assessment = account_view(own, other, NOW + seconds(1))
    assert assessment.account_identity_verified is False
    with pytest.raises(TypeError):
        AccountSlotAssessment(ACCOUNT, NOW, (), account_identity_verified=True)


def test_concurrent_reservations_cannot_share_a_send_slot():
    base = open_account()
    first, second = plan(), plan(**PLAN_B)
    slot_a = reserve(journal_for(first), page(first), 1).attempt_id
    slot_b = reserve(journal_for(second), page(second), 1).attempt_id
    proposal_a = base.append(
        slot_event("slot_reserved", NOW, slot_a, plan_sha=first.sha256)
    )
    proposal_b = base.append(
        slot_event(
            "slot_reserved", NOW, slot_b, plan_sha=second.sha256, holder="runner-b"
        )
    )
    store = commit_account_ledger(ACCOUNT, base.data, proposal_a.data)  # A wins
    with pytest.raises(HttpContractError, match="stale_or_conflicting_commit"):
        commit_account_ledger(ACCOUNT, store.data, proposal_b.data)
    with pytest.raises(HttpContractError, match="account_slot_in_use"):
        store.append(
            slot_event(
                "slot_reserved", NOW, slot_b, plan_sha=second.sha256, holder="runner-b"
            )
        )
    two_records = proposal_a.append(slot_event("slot_sent", NOW, slot_a))
    with pytest.raises(HttpContractError, match="stale_or_conflicting_commit"):
        commit_account_ledger(ACCOUNT, base.data, two_records.data)


def test_unknown_outcome_across_a_plan_switch_is_reclaimed_only_after_the_lease():
    first, second = plan(), plan(**PLAN_B)
    journal_a, ledger = paired_until_send(
        journal_for(first), open_account(), page(first), at=NOW
    )
    slot_a = last_attempt_id(journal_a)
    # runner-a stops after sending; plan B (runner-b) must not use the account
    stuck = account_view(ledger, second, NOW + seconds(30), journal_a)
    assert stuck.blocking_reasons == (
        "account_ledger_pending:pending_settlement_on_both_ledgers",
        "account_slot_in_use",
    )
    reclaim = slot_event(
        "slot_settled", NOW + seconds(60) - MICRO, slot_a, holder="runner-b",
        outcome="unknown", effective_wait_seconds=13,
    )  # fmt: skip
    with pytest.raises(HttpContractError, match="reclaim_before_lease_expiry"):
        ledger.append(reclaim)
    with pytest.raises(HttpContractError, match="account_slot_holder_mismatch"):
        ledger.append(replace(reclaim, at=NOW + seconds(60), outcome="response"))
    ledger = ledger.append(replace(reclaim, at=NOW + seconds(60)))
    # The plan side still shows the attempt as sent: recover it as unknown first.
    assert (
        "account_ledger_pending:pending_plan_settlement"
        in account_view(ledger, second, NOW + seconds(80), journal_a).blocking_reasons
    )
    journal_a = journal_a.append(
        JournalEvent("outcome_unknown", NOW + seconds(61), attempt_id=slot_a)
    )
    for current_ledger, current_journal in (
        (ledger, journal_a),
        (
            AccountRateLedger(ACCOUNT, ledger.data),
            EvidenceJournal(first, journal_a.data, approval=journal_a.approval),
        ),  # resumed
    ):
        state = account_view(
            current_ledger, second, NOW + seconds(73) - MICRO, current_journal
        )
        assert state.blocking_reasons == ("account_rate_limit_wait",)
        assert state.earliest_next_slot_at == NOW + seconds(73)
        assert account_view(
            current_ledger, second, NOW + seconds(73), current_journal
        ).account_allows_next_attempt


def test_send_must_fit_inside_the_slot_lease():
    fixed = plan()
    slot_id = reserve(journal_for(fixed), page(fixed), 1).attempt_id
    ledger = open_account().append(
        slot_event("slot_reserved", NOW, slot_id, plan_sha=fixed.sha256)
    )
    with pytest.raises(HttpContractError, match="lease_too_short_for_send"):
        ledger.append(slot_event("slot_sent", NOW + seconds(30) + MICRO, slot_id))
    assert ledger.append(slot_event("slot_sent", NOW + seconds(30), slot_id))


def test_account_policy_must_cover_the_plan_rules():
    journal_a, ledger = run_plan_a()
    strict = plan(**PLAN_B, retry=retry(min_wait_after_429_seconds=300))
    assert (
        "account_policy_weaker_than_plan"
        in account_view(ledger, strict, NOW + seconds(60), journal_a).blocking_reasons
    )
    with pytest.raises(HttpContractError, match="slot_lease_shorter_than_request"):
        account_policy(slot_lease_seconds=10)


def gate_for(journal, ledger, now, related=()):
    inventory = BodyInventory(())
    return assess_live_acquisition_gate(
        journal.plan,
        journal.approval,
        journal,
        inventory,
        None,
        now=now,
        account_ledger=ledger,
        related_journals=related,
    ).reasons


def test_gate_requires_consistent_account_state_and_stays_closed():
    journal, ledger = run_plan_a(429)
    later = NOW + seconds(200)
    assert "account_rate_state_missing" in gate_for(journal, None, later)
    tampered = AccountRateLedger.__new__(AccountRateLedger)
    tampered._init(ACCOUNT, (ledger.data.replace(b'"429"', b'"5xx"', 1),), "0", 0, None)
    assert any(
        r.startswith("account_rate_state_invalid:")
        for r in gate_for(journal, tampered, later)
    )
    stale = AccountRateLedger(ACCOUNT, b"".join(ledger._lines[:3]))  # before settle
    reasons = gate_for(journal, stale, later)
    assert "account_rate_blocked:account_slot_in_use" in reasons
    assert (
        "account_rate_blocked:account_ledger_pending:pending_account_settlement"
        in reasons
    )
    assert (
        "account_rate_blocked:account_ledger_inconsistent:"
        "account_ledger_missing_plan_attempt"
    ) in gate_for(journal, open_account(), later)
    early = gate_for(journal, ledger, NOW + seconds(1))
    assert "account_rate_blocked:account_rate_limit_wait" in early
    settled = gate_for(journal, ledger, later)
    assert not any(r.startswith("account_rate_") for r in settled)
    assert settled[-5:] == CLOSED_TAIL
    # Plan B's gate needs plan A's journal: the account ledger alone is not enough.
    fixed_b = plan(**PLAN_B)
    b_journal = journal_for(fixed_b)
    assert "account_rate_blocked:account_plan_journal_missing" in gate_for(
        b_journal, ledger, later
    )
    assert not any(
        r.startswith("account_rate_")
        for r in gate_for(b_journal, ledger, later, (journal,))
    )
    with pytest.raises(HttpContractError, match="raw_evidence_required"):
        require_live_acquisition_permission(
            journal.plan,
            journal.approval,
            journal,
            BodyInventory(()),
            None,
            now=later,
            account_ledger=account_view(ledger, journal.plan, later, journal),
        )
    with pytest.raises(HttpContractError, match="raw_evidence_required"):
        require_live_acquisition_permission(
            fixed_b,
            b_journal.approval,
            b_journal,
            BodyInventory(()),
            None,
            now=later,
            account_ledger=ledger,
            related_journals=[journal],
        )


def test_plan_snapshot_flag_is_named_plan_only():
    state = budget_snapshot(journal_for(plan()), BodyInventory(()), now=NOW)
    assert state.plan_allows_next_attempt
    assert not hasattr(state, "can_reserve_next_attempt")


# --------------------------------- round 4, High: results and Retry-After bound


def after_plan_response(status, *, retry_after=None, fixed=None):
    fixed = fixed or plan()
    journal, ledger = paired_until_send(journal_for(fixed), open_account(), page(fixed))
    return plan_response(journal, NOW, status, retry_after=retry_after), ledger


@pytest.mark.parametrize(
    "status,account_outcome",
    [(429, "unknown"), (429, "response"), (200, "unknown"), (503, "response")],
)
def test_known_plan_result_must_match_the_account_outcome(status, account_outcome):
    journal, ledger = after_plan_response(status)
    ledger = settle(
        ledger, journal.plan, last_attempt_id(journal), NOW, account_outcome
    )
    result = reconcile_account_and_plan(ledger, journal)
    assert result.status == "inconsistent"
    assert result.issues == ("account_slot_outcome_mismatch",)
    blocked = account_view(ledger, plan(**PLAN_B), NOW + seconds(600), journal)
    assert (
        "account_ledger_inconsistent:account_slot_outcome_mismatch"
        in blocked.blocking_reasons
    )


@pytest.mark.parametrize("status", [200, 429, 503])
def test_matching_results_on_both_ledgers_are_consistent(status):
    journal, ledger = after_plan_response(status)
    ledger = settle(
        ledger, journal.plan, last_attempt_id(journal), NOW, outcome_of(status)
    )
    assert reconcile_account_and_plan(ledger, journal) == LedgerReconciliation(
        journal.plan.sha256, "consistent", ()
    )
    check_account_plan_consistency(ledger, journal)


def test_plan_retry_after_must_be_carried_to_the_account():
    journal, ledger = after_plan_response(429, retry_after=600)
    slot_id = last_attempt_id(journal)

    def settled(observed, effective):
        return ledger.append(
            slot_event(
                "slot_settled",
                NOW,
                slot_id,
                outcome="429",
                observed_retry_after_seconds=observed,
                effective_wait_seconds=effective,
            )
        )

    for observed in (None, 300):
        result = reconcile_account_and_plan(settled(observed, 600), journal)
        assert result.issues == ("account_retry_after_mismatch",)
    with pytest.raises(HttpContractError, match="account_effective_wait_below_rule"):
        settled(600, 599)
    assert reconcile_account_and_plan(settled(600, 600), journal).status == "consistent"


def test_known_retry_after_is_never_replaced_by_a_shorter_wait_for_plan_b():
    journal, ledger = after_plan_response(429, retry_after=600)
    unknown = settle(ledger, journal.plan, last_attempt_id(journal), NOW, "unknown")
    fixed_b = plan(**PLAN_B)
    at = NOW + seconds(121)
    assert not account_view(unknown, fixed_b, at, journal).account_allows_next_attempt
    assert (
        "account_plan_journal_missing"
        in account_view(unknown, fixed_b, at).blocking_reasons
    )
    carried = settle(
        ledger, journal.plan, last_attempt_id(journal), NOW, "429", observed=600
    )
    assert not account_view(carried, fixed_b, at, journal).account_allows_next_attempt
    assert account_view(
        carried, fixed_b, NOW + seconds(600), journal
    ).account_allows_next_attempt


def test_account_may_apply_a_longer_wait_without_changing_the_observed_value():
    journal, ledger = run_plan_a(429, retry_after=600, extra=300)
    assert reconcile_account_and_plan(ledger, journal).status == "consistent"
    settled = json.loads(ledger._lines[-1])["event"]
    assert (
        settled["observed_retry_after_seconds"],
        settled["effective_wait_seconds"],
    ) == (
        600,
        900,
    )
    view = account_view(ledger, plan(**PLAN_B), NOW + seconds(600), journal)
    assert view.earliest_next_slot_at == NOW + seconds(900)
    assert view.blocking_reasons == ("account_rate_limit_wait",)


def test_account_wait_below_the_plan_rule_is_inconsistent():
    strict = plan(retry=retry(min_wait_after_5xx_seconds=60))
    journal, ledger = after_plan_response(503, fixed=strict)
    ledger = ledger.append(
        slot_event(
            "slot_settled",
            NOW,
            last_attempt_id(journal),
            outcome="5xx",
            effective_wait_seconds=13,  # the account rule alone would allow this
        )
    )
    assert reconcile_account_and_plan(ledger, journal).issues == (
        "account_effective_wait_below_plan_rule",
    )


# ----------------------------------- round 4, Medium: causal order of the times


def custom_timeline(*, account_reserved, plan_reserved, plan_sent, account_sent,
                    response, settled, status=200):  # fmt: skip
    fixed = plan()
    journal = journal_for(fixed)
    event = reserve(journal, page(fixed), 1, NOW + seconds(plan_reserved))
    ledger = open_account().append(
        slot_event(
            "slot_reserved",
            NOW + seconds(account_reserved),
            event.attempt_id,
            plan_sha=fixed.sha256,
        )
    )
    journal = journal.append(event).append(
        JournalEvent(
            "attempt_sent", NOW + seconds(plan_sent), attempt_id=event.attempt_id
        )
    )
    ledger = ledger.append(
        slot_event("slot_sent", NOW + seconds(account_sent), event.attempt_id)
    )
    journal = plan_response(journal, NOW + seconds(response), status)
    ledger = settle(
        ledger, fixed, event.attempt_id, NOW + seconds(settled), outcome_of(status)
    )
    return journal, ledger


def test_different_append_times_with_a_causal_order_are_accepted():
    journal, ledger = custom_timeline(
        account_reserved=-2, plan_reserved=0, plan_sent=1, account_sent=1.5,
        response=3, settled=10,
    )  # fmt: skip
    assert reconcile_account_and_plan(ledger, journal).status == "consistent"
    view = account_view(ledger, plan(**PLAN_B), NOW + seconds(23) - MICRO, journal)
    assert view.earliest_next_slot_at == NOW + seconds(23)


@pytest.mark.parametrize(
    "timeline,issue",
    [
        (dict(account_reserved=0, plan_reserved=0, plan_sent=0, account_sent=5,
              response=3, settled=10), "account_send_after_plan_response"),
        (dict(account_reserved=0, plan_reserved=0, plan_sent=2, account_sent=1,
              response=3, settled=10), "account_sent_before_plan_send"),
        (dict(account_reserved=0, plan_reserved=0, plan_sent=0, account_sent=0,
              response=3, settled=2), "account_settled_before_plan_result"),
        (dict(account_reserved=1, plan_reserved=0, plan_sent=1, account_sent=1,
              response=3, settled=10), "account_slot_reserved_after_plan_attempt"),
    ],
)  # fmt: skip
def test_impossible_time_orders_are_refused(timeline, issue):
    try:
        journal, ledger = custom_timeline(**timeline)
    except HttpContractError:
        pytest.fail("the timeline itself must be recordable")
    result = reconcile_account_and_plan(ledger, journal)
    assert result.status == "inconsistent" and issue in result.issues


def test_plan_result_after_the_lease_is_inconsistent():
    fixed = plan()
    journal, ledger = paired_until_send(journal_for(fixed), open_account(), page(fixed))
    journal = plan_response(journal, NOW + seconds(30), 200)  # inside the plan timeout
    ledger = settle(
        ledger, fixed, last_attempt_id(journal), NOW + seconds(30), "response"
    )
    assert reconcile_account_and_plan(ledger, journal).status == "consistent"
    late = open_account(request_timeout_seconds=20, slot_lease_seconds=20)
    journal2, late = paired_until_send(journal_for(fixed), late, page(fixed))
    journal2 = plan_response(journal2, NOW + seconds(20) + MICRO, 200)
    result = reconcile_account_and_plan(late, journal2)
    assert "plan_result_after_account_lease" in result.issues


# ----------------------------------------- round 4: one-sided crash states


def test_crash_after_account_reservation_only():
    fixed = plan()
    event = reserve(journal_for(fixed), page(fixed), 1)
    ledger = open_account().append(
        slot_event("slot_reserved", NOW, event.attempt_id, plan_sha=fixed.sha256)
    )
    journal = journal_for(fixed)
    assert reconcile_account_and_plan(ledger, journal).issues == (
        "pending_plan_attempt_record",
    )
    # Recovery: the attempt never reached the plan and was never sent.
    ledger = settle(ledger, fixed, event.attempt_id, NOW + seconds(1), "unknown")
    assert reconcile_account_and_plan(ledger, journal).status == "consistent"


def test_crash_after_sending_is_recovered_as_unknown_on_both_sides():
    fixed = plan()
    journal, ledger = paired_until_send(journal_for(fixed), open_account(), page(fixed))
    assert reconcile_account_and_plan(ledger, journal).issues == (
        "pending_settlement_on_both_ledgers",
    )
    slot_id = last_attempt_id(journal)
    journal = journal.append(
        JournalEvent("outcome_unknown", NOW + seconds(5), attempt_id=slot_id)
    )
    assert reconcile_account_and_plan(ledger, journal).issues == (
        "pending_account_settlement",
    )
    ledger = settle(ledger, fixed, slot_id, NOW + seconds(6), "unknown")
    assert reconcile_account_and_plan(ledger, journal).status == "consistent"


def test_crash_after_the_plan_result_is_recovered_only_by_copying_it():
    journal, ledger = after_plan_response(429, retry_after=600)
    slot_id = last_attempt_id(journal)
    pending = reconcile_account_and_plan(ledger, journal)
    assert (pending.status, pending.issues) == (
        "pending",
        ("pending_account_settlement",),
    )
    assert (
        "account_ledger_pending:pending_account_settlement"
        in account_view(
            ledger, plan(**PLAN_B), NOW + seconds(900), journal
        ).blocking_reasons
    )
    guessed = settle(ledger, journal.plan, slot_id, NOW + seconds(1), "unknown")
    assert reconcile_account_and_plan(guessed, journal).status == "inconsistent"
    copied = settle(
        ledger, journal.plan, slot_id, NOW + seconds(1), "429", observed=600
    )
    assert reconcile_account_and_plan(copied, journal).status == "consistent"


@pytest.mark.parametrize(
    "plan_side,issue",
    [
        ("open", "account_result_without_plan_result"),
        ("unknown", "account_result_contradicts_plan_unknown"),
    ],
)
def test_account_result_without_the_plan_result_is_never_repaired(plan_side, issue):
    fixed = plan()
    journal, ledger = paired_until_send(journal_for(fixed), open_account(), page(fixed))
    slot_id = last_attempt_id(journal)
    if plan_side == "unknown":
        journal = journal.append(
            JournalEvent("outcome_unknown", NOW, attempt_id=slot_id)
        )
    ledger = settle(ledger, fixed, slot_id, NOW + seconds(1), "429")
    result = reconcile_account_and_plan(ledger, journal)
    assert (result.status, result.issues) == ("inconsistent", (issue,))


def test_account_send_without_a_plan_attempt_is_inconsistent():
    fixed = plan()
    event = reserve(journal_for(fixed), page(fixed), 1)
    ledger = open_account().append(
        slot_event("slot_reserved", NOW, event.attempt_id, plan_sha=fixed.sha256)
    )
    ledger = ledger.append(slot_event("slot_sent", NOW, event.attempt_id))
    assert reconcile_account_and_plan(ledger, journal_for(fixed)).issues == (
        "account_sent_without_plan_attempt",
    )


def test_refusals_leave_both_ledgers_unchanged_and_resume_is_consistent():
    journal, ledger = after_plan_response(429, retry_after=600)
    before = (journal.data, ledger.data)
    with pytest.raises(HttpContractError, match="account_effective_wait_below_rule"):
        ledger.append(
            slot_event(
                "slot_settled",
                NOW,
                last_attempt_id(journal),
                outcome="429",
                observed_retry_after_seconds=600,
                effective_wait_seconds=120,
            )
        )
    reconcile_account_and_plan(ledger, journal)
    assert (journal.data, ledger.data) == before
    journal, ledger = run_plan_a(429, retry_after=600)
    resumed_journal = EvidenceJournal(
        journal.plan, journal.data, approval=journal.approval
    )
    resumed_ledger = AccountRateLedger(ACCOUNT, ledger.data)
    assert (
        reconcile_account_and_plan(resumed_ledger, resumed_journal).status
        == "consistent"
    )
    assert account_view(
        resumed_ledger, plan(**PLAN_B), NOW + seconds(600), resumed_journal
    ).account_allows_next_attempt


# ---------------------------------------- round 3, Medium: bounded wait values


@pytest.mark.parametrize(
    "value,reason",
    [
        (10**400, "retry_after_seconds_out_of_range"),
        (MAX_WAIT_SECONDS + 1, "retry_after_seconds_out_of_range"),
        (-1, "retry_after_seconds_must_be_nonnegative_integer"),
        (float("inf"), "retry_after_seconds_must_be_nonnegative_integer"),
        (float("nan"), "retry_after_seconds_must_be_nonnegative_integer"),
        (True, "retry_after_seconds_must_be_nonnegative_integer"),
        ("120", "retry_after_seconds_must_be_nonnegative_integer"),
    ],
)
def test_invalid_retry_after_is_refused_before_recording(value, reason):
    fixed = plan()
    with pytest.raises(HttpContractError, match=reason):
        exchange(journal_for(fixed), page(fixed), 1, NOW, status=429, retry_after=value)
    with pytest.raises(HttpContractError, match=reason):
        slot_event(
            "slot_settled",
            NOW,
            DIGEST,
            outcome="429",
            observed_retry_after_seconds=value,
            effective_wait_seconds=120,
        )


def test_largest_retry_after_sets_an_exact_next_time():
    fixed = plan(expires_at=NOW + timedelta(days=3))
    journal = exchange(
        journal_for(fixed, approval(fixed, valid_until=NOW + timedelta(days=2))),
        page(fixed),
        1,
        NOW,
        status=429,
        retry_after=MAX_WAIT_SECONDS,
    )
    state = budget_snapshot(journal, BodyInventory(()), now=NOW + seconds(1))
    assert state.earliest_next_attempt_at == NOW + seconds(MAX_WAIT_SECONDS)


def test_tampered_journal_with_a_huge_retry_after_is_a_contract_error():
    fixed = plan()
    claim = approval(fixed)
    journal = journal_for(fixed, claim).append(
        reserve(journal_for(fixed, claim), page(fixed), 1)
    )
    journal = journal.append(
        JournalEvent("attempt_sent", NOW, attempt_id=last_attempt_id(journal))
    )
    response = JournalEvent(
        "response_received",
        NOW,
        attempt_id=last_attempt_id(journal),
        status=429,
        transfer_bytes=10,
        decoded_bytes=20,
    ).to_dict()
    response["retry_after_seconds"] = 10**400
    with pytest.raises(HttpContractError, match="retry_after_seconds_out_of_range"):
        EvidenceJournal(fixed, forge(fixed, journal.data, response), approval=claim)


@pytest.mark.parametrize(
    "build,reason",
    [
        (lambda: limits(max_elapsed_seconds=10**400), "max_elapsed_seconds_out_of_range"),
        (lambda: retry(min_wait_after_429_seconds=10**400),
         "min_wait_after_429_seconds_out_of_range"),
        (lambda: retry(timeout_seconds=MAX_WAIT_SECONDS + 1), "timeout_seconds_out_of_range"),
        (lambda: plan(expires_at=datetime(9999, 12, 31, tzinfo=UTC)),
         "plan_time_window_unrepresentable"),
        (lambda: account_policy(slot_lease_seconds=10**400), "slot_lease_seconds_out_of_range"),
    ],
)  # fmt: skip
def test_unrepresentable_durations_and_windows_are_refused(build, reason):
    with pytest.raises(HttpContractError, match=reason):
        build()


def test_unrepresentable_next_time_is_a_reason_not_an_exception():
    fixed = plan()
    journal = journal_for(fixed).append(reserve(journal_for(fixed), page(fixed), 1))
    far = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)  # + 13 s overflows
    journal = journal.append(
        JournalEvent("outcome_unknown", far, attempt_id=last_attempt_id(journal))
    )
    state = budget_snapshot(journal, BodyInventory(()), now=far)
    assert "next_attempt_time_unrepresentable" in state.blocking_reasons
    assert state.earliest_next_attempt_at is None


def test_stale_runner_cannot_settle_a_reclaimed_slot_and_its_late_result_blocks():
    first = plan()
    journal, ledger = paired_until_send(journal_for(first), open_account(), page(first))
    slot_id = last_attempt_id(journal)
    ledger = ledger.append(
        slot_event(
            "slot_settled",
            NOW + seconds(60),
            slot_id,
            holder="runner-b",
            outcome="unknown",
            effective_wait_seconds=13,
        )
    )
    with pytest.raises(HttpContractError, match="account_slot_not_open"):
        settle(ledger, first, slot_id, NOW + seconds(61), "response")
    late = plan_response(journal, NOW + seconds(30), 200)  # runner-a comes back
    result = reconcile_account_and_plan(ledger, late)
    assert result.status == "inconsistent"
    assert "account_slot_outcome_mismatch" in result.issues
    assert not account_view(
        ledger, plan(**PLAN_B), NOW + seconds(300), late
    ).account_allows_next_attempt
