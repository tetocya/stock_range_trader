"""Artificial-only tests: no HTTP transport, key, market data, or owner approval."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from feasibility.acquisition import (
    AcquisitionStopped,
    OfflineFixtureTransport,
    run_offline_fixture_acquisition,
)
from feasibility.http_contract import (
    HTTP_OUTPUT_ROOT,
    BodyFileEvidence,
    BodyInventory,
    EvidenceJournal,
    ExternalReceiptClaim,
    HttpAcquisitionPlan,
    HttpContractError,
    HttpLimits,
    JournalEvent,
    OwnerApprovalClaim,
    PageRequest,
    RetryRules,
    assess_live_acquisition_gate,
    budget_snapshot,
    check_approval_scope,
    check_receipt_alignment,
    make_attempt_id,
    validate_page_request,
)

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
DIGEST = "a" * 64
BODY_DIGEST = "b" * 64


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


def reserve(
    fixed: HttpAcquisitionPlan,
    request: PageRequest,
    attempt_number: int,
    at: datetime = NOW,
) -> JournalEvent:
    return JournalEvent(
        "attempt_reserved",
        at,
        attempt_id=make_attempt_id(fixed, request, attempt_number),
        page=request,
        attempt_number=attempt_number,
    )


def response(
    journal: EvidenceJournal, request: PageRequest, *, status: int = 200
) -> EvidenceJournal:
    number = 1 + sum(
        event.kind == "attempt_reserved" and event.page == request
        for event in journal.events
    )
    fixed = journal.plan
    attempt = reserve(fixed, request, number, NOW + timedelta(seconds=number))
    journal = journal.append(attempt)
    journal = journal.append(
        JournalEvent("attempt_sent", attempt.at, attempt_id=attempt.attempt_id)
    )
    return journal.append(
        JournalEvent(
            "response_received",
            attempt.at,
            attempt_id=attempt.attempt_id,
            status=status,
            transfer_bytes=10,
            decoded_bytes=20,
        )
    )


def receipt(
    journal: EvidenceJournal, inventory: BodyInventory, **changes
) -> ExternalReceiptClaim:
    values = dict(
        artifact_id=journal.plan.artifact_id,
        plan_sha256=journal.plan.sha256,
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


def test_calendar_methods_have_separate_fixed_scope():
    discovery = plan(
        kind="calendar_discovery",
        master_date=None,
        daily_dates=(),
        calendar_source_sha256=None,
        calendar_source_reference=None,
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


def test_claim_metadata_never_proves_authenticity_or_opens_gate():
    fixed = plan()
    claim = approval(fixed)
    journal = EvidenceJournal(fixed)
    inventory = BodyInventory(())
    assert check_approval_scope(fixed, claim, now=NOW).authenticity_verified is False
    assert OwnerApprovalClaim.__dataclass_fields__.get("approved") is None
    assert assess_live_acquisition_gate(
        fixed, claim, journal, inventory, receipt(journal, inventory), now=NOW
    ).reasons[-3:] == (
        "owner_approval_authenticity_unverified",
        "independent_receipt_custody_unconfigured",
        "http_transport_not_implemented",
    )
    assert not assess_live_acquisition_gate(
        fixed, None, journal, inventory, None, now=NOW
    ).permitted


def test_journal_reserved_sent_429_retry_unknown_counts_survive_resume():
    fixed = plan()
    request = page(fixed)
    journal = response(EvidenceJournal(fixed), request, status=429)
    second = reserve(fixed, request, 2, NOW + timedelta(seconds=13))
    journal = journal.append(second).append(
        JournalEvent("attempt_sent", second.at, attempt_id=second.attempt_id)
    )
    journal = journal.append(
        JournalEvent("outcome_unknown", second.at, attempt_id=second.attempt_id)
    )
    resumed = EvidenceJournal(fixed, journal.data)
    state = budget_snapshot(resumed, BodyInventory(()), now=NOW + timedelta(seconds=20))
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
    assert state.elapsed_seconds == 19
    assert state.can_reserve_next_attempt
    with pytest.raises(HttpContractError, match="journal_chain_or_plan_mismatch"):
        EvidenceJournal(plan(limits=limits(max_attempts=9)), journal.data)


def test_unsettled_or_unknown_attempt_consumes_budget_and_deadline_is_fixed():
    fixed = plan(limits=limits(max_elapsed_seconds=20))
    request = page(fixed)
    first = reserve(fixed, request, 1)
    journal = EvidenceJournal(fixed).append(first)
    state = budget_snapshot(journal, BodyInventory(()), now=NOW + timedelta(seconds=5))
    assert state.unsettled_attempts == 1
    assert not state.can_reserve_next_attempt
    journal = journal.append(
        JournalEvent("outcome_unknown", NOW, attempt_id=first.attempt_id)
    )
    assert (
        budget_snapshot(
            journal, BodyInventory(()), now=NOW + timedelta(seconds=19)
        ).remaining_seconds
        == 1
    )
    assert not budget_snapshot(
        journal, BodyInventory(()), now=NOW + timedelta(seconds=20)
    ).can_reserve_next_attempt
    with pytest.raises(HttpContractError, match="attempt_after_cumulative_deadline"):
        journal.append(reserve(fixed, request, 2, NOW + timedelta(seconds=20)))


def test_body_inventory_counts_orphan_and_partial_without_reset():
    fixed = plan(limits=limits(max_saved_bytes=25, max_page_saved_bytes=25))
    inventory = BodyInventory(
        (
            BodyFileEvidence("orphan-1", 20, "orphan", BODY_DIGEST),
            BodyFileEvidence("partial-1", 5, "partial"),
        )
    )
    state = budget_snapshot(EvidenceJournal(fixed), inventory, now=NOW)
    assert state.orphan_files == state.partial_files == 1
    assert state.remaining_saved_bytes == 0
    assert not state.can_reserve_next_attempt
    under_budget = budget_snapshot(
        EvidenceJournal(plan()),
        BodyInventory((BodyFileEvidence("orphan-only", 1, "orphan", BODY_DIGEST),)),
        now=NOW,
    )
    assert under_budget.remaining_saved_bytes > 0
    assert not under_budget.can_reserve_next_attempt


def test_completed_page_body_and_receipt_alignment_then_tamper_rejection():
    fixed = plan()
    request = page(fixed)
    journal = response(EvidenceJournal(fixed), request)
    attempt_id = journal.events[0].attempt_id
    journal = journal.append(
        JournalEvent(
            "body_saved",
            NOW + timedelta(seconds=2),
            attempt_id=attempt_id,
            body_sha256=BODY_DIGEST,
            saved_bytes=20,
        )
    )
    journal = journal.append(
        JournalEvent(
            "page_completed", NOW + timedelta(seconds=2), attempt_id=attempt_id
        )
    )
    inventory = BodyInventory(
        (BodyFileEvidence("body-1", 20, "committed", BODY_DIGEST),)
    )
    assert (
        budget_snapshot(
            journal, inventory, now=NOW + timedelta(seconds=10)
        ).completed_pages
        == 1
    )
    claim = receipt(journal, inventory)
    assert check_receipt_alignment(journal, inventory, claim).content_matches
    assert not check_receipt_alignment(
        journal, inventory, claim
    ).independent_custody_verified
    with pytest.raises(HttpContractError, match="receipt_ledger_head_sha256_mismatch"):
        check_receipt_alignment(
            journal, inventory, replace(claim, ledger_head_sha256=DIGEST)
        )
    with pytest.raises(HttpContractError, match="receipt_body_set_sha256_mismatch"):
        check_receipt_alignment(
            journal, inventory, replace(claim, body_set_sha256=DIGEST)
        )
    with pytest.raises(HttpContractError, match="completed_page_body_not_committed"):
        budget_snapshot(
            journal,
            BodyInventory((BodyFileEvidence("body-1", 20, "orphan", BODY_DIGEST),)),
            now=NOW,
        )


def test_journal_detects_line_deletion_and_noncanonical_or_modified_record():
    fixed = plan()
    journal = response(EvidenceJournal(fixed), page(fixed), status=429)
    with pytest.raises(HttpContractError, match="journal_incomplete_final_line"):
        EvidenceJournal(fixed, journal.data[:-1])
    lines = journal.data.splitlines(keepends=True)
    with pytest.raises(HttpContractError, match="journal_chain_or_plan_mismatch"):
        EvidenceJournal(fixed, b"".join(lines[1:]))
    record = json.loads(lines[0])
    record["event"]["status"] = 200
    with pytest.raises(
        HttpContractError, match="journal_record_noncanonical|journal_chain"
    ):
        EvidenceJournal(
            fixed, (json.dumps(record) + "\n").encode() + b"".join(lines[1:])
        )
    # A cleanly removed *tail* cannot be detected without an independently fixed receipt.
    truncated = EvidenceJournal(fixed, b"".join(lines[:-1]))
    assert truncated.event_count == journal.event_count - 1
    with pytest.raises(HttpContractError, match="receipt_ledger_event_count_mismatch"):
        check_receipt_alignment(
            truncated, BodyInventory(()), receipt(journal, BodyInventory(()))
        )


def test_journal_rejects_nonfinite_json_as_contract_error():
    fixed = plan()
    journal = EvidenceJournal(fixed).append(reserve(fixed, page(fixed), 1))
    record = json.loads(journal.data)
    record["event"]["status"] = float("nan")
    malformed = (json.dumps(record) + "\n").encode("utf-8")
    with pytest.raises(HttpContractError, match="journal_record_noncanonical"):
        EvidenceJournal(fixed, malformed)


def test_complete_journal_is_idempotent_and_rejects_a_second_attempt():
    fixed = plan()
    journal = EvidenceJournal(fixed)
    files = []
    for index, query in enumerate(fixed.queries):
        request = PageRequest(query, 0)
        at = NOW + timedelta(seconds=13 * index)
        attempt = reserve(fixed, request, 1, at)
        digest = f"{index + 1:064x}"
        journal = journal.append(attempt)
        journal = journal.append(
            JournalEvent("attempt_sent", at, attempt_id=attempt.attempt_id)
        )
        journal = journal.append(
            JournalEvent(
                "response_received",
                at,
                attempt_id=attempt.attempt_id,
                status=200,
                transfer_bytes=10,
                decoded_bytes=20,
            )
        )
        journal = journal.append(
            JournalEvent(
                "body_saved",
                at,
                attempt_id=attempt.attempt_id,
                body_sha256=digest,
                saved_bytes=20,
            )
        )
        journal = journal.append(
            JournalEvent("page_completed", at, attempt_id=attempt.attempt_id)
        )
        files.append(BodyFileEvidence(f"body-{index}", 20, "committed", digest))
    journal = journal.append(JournalEvent("run_completed", NOW + timedelta(seconds=60)))
    resumed = EvidenceJournal(fixed, journal.data)
    state = budget_snapshot(
        resumed, BodyInventory(tuple(files)), now=NOW + timedelta(seconds=61)
    )
    assert resumed.completed
    assert state.status == "completed"
    assert state.completed_pages == len(fixed.queries)
    assert not state.can_reserve_next_attempt
    with pytest.raises(HttpContractError, match="journal_event_after_terminal_state"):
        resumed.append(reserve(fixed, page(fixed), 2, NOW + timedelta(seconds=62)))


def test_global_sequential_query_and_nonretryable_response_contract():
    fixed = plan()
    first_page = page(fixed)
    second_query_page = page(fixed, 1)
    journal = EvidenceJournal(fixed)
    first = reserve(fixed, first_page, 1)
    journal = journal.append(first)
    with pytest.raises(HttpContractError, match="prior_attempt_unsettled"):
        journal.append(
            reserve(fixed, second_query_page, 1, NOW + timedelta(seconds=13))
        )
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
        journal.append(reserve(fixed, first_page, 2, NOW + timedelta(seconds=13)))
    with pytest.raises(HttpContractError, match="prior_query_incomplete"):
        journal.append(
            reserve(fixed, second_query_page, 1, NOW + timedelta(seconds=13))
        )


def test_page_size_overrun_is_recordable_but_blocks_another_attempt():
    fixed = plan(limits=limits(max_page_transfer_bytes=10))
    request = page(fixed)
    first = reserve(fixed, request, 1)
    journal = EvidenceJournal(fixed).append(first)
    journal = journal.append(
        JournalEvent("attempt_sent", NOW, attempt_id=first.attempt_id)
    )
    journal = journal.append(
        JournalEvent(
            "response_received",
            NOW,
            attempt_id=first.attempt_id,
            status=429,
            transfer_bytes=11,
            decoded_bytes=20,
        )
    )
    assert budget_snapshot(journal, BodyInventory(()), now=NOW).received_responses == 1
    with pytest.raises(HttpContractError, match="previous_page_budget_overrun"):
        journal.append(reserve(fixed, request, 2, NOW + timedelta(seconds=13)))


@pytest.mark.parametrize(
    "limit_override",
    [
        {"max_transfer_bytes": 35, "max_page_transfer_bytes": 10},
        {"max_decoded_bytes": 75, "max_page_decoded_bytes": 20},
        {"max_saved_bytes": 75, "max_page_saved_bytes": 20},
    ],
)
def test_cumulative_byte_overrun_cannot_be_recorded_as_completed(limit_override):
    fixed = plan(limits=limits(**limit_override))
    journal = EvidenceJournal(fixed)
    files = []
    for index, query in enumerate(fixed.queries):
        request = PageRequest(query, 0)
        at = NOW + timedelta(seconds=13 * index)
        attempt = reserve(fixed, request, 1, at)
        digest = f"{index + 1:064x}"
        journal = journal.append(attempt)
        journal = journal.append(
            JournalEvent("attempt_sent", at, attempt_id=attempt.attempt_id)
        )
        journal = journal.append(
            JournalEvent(
                "response_received",
                at,
                attempt_id=attempt.attempt_id,
                status=200,
                transfer_bytes=10,
                decoded_bytes=20,
            )
        )
        journal = journal.append(
            JournalEvent(
                "body_saved",
                at,
                attempt_id=attempt.attempt_id,
                body_sha256=digest,
                saved_bytes=20,
            )
        )
        journal = journal.append(
            JournalEvent("page_completed", at, attempt_id=attempt.attempt_id)
        )
        files.append(BodyFileEvidence(f"body-{index}", 20, "committed", digest))
    assert not budget_snapshot(
        journal, BodyInventory(tuple(files)), now=NOW + timedelta(seconds=50)
    ).can_reserve_next_attempt
    with pytest.raises(HttpContractError, match="run_completed_after_budget_overrun"):
        journal.append(JournalEvent("run_completed", NOW + timedelta(seconds=60)))


def test_page_continuation_requires_exact_key_and_rejects_loop():
    fixed = plan()
    request = page(fixed)
    journal = response(EvidenceJournal(fixed), request)
    attempt_id = journal.events[0].attempt_id
    journal = journal.append(
        JournalEvent(
            "body_saved",
            NOW + timedelta(seconds=2),
            attempt_id=attempt_id,
            body_sha256=BODY_DIGEST,
            saved_bytes=20,
        )
    )
    journal = journal.append(
        JournalEvent(
            "page_completed",
            NOW + timedelta(seconds=2),
            attempt_id=attempt_id,
            next_key="page-two",
        )
    )
    with pytest.raises(HttpContractError, match="pagination_chain_invalid"):
        journal.append(
            reserve(
                fixed,
                PageRequest(request.query, 1, "wrong-key"),
                1,
                NOW + timedelta(seconds=13),
            )
        )
    second = PageRequest(request.query, 1, "page-two")
    journal = journal.append(reserve(fixed, second, 1, NOW + timedelta(seconds=13)))
    second_id = journal.events[-1].attempt_id
    journal = journal.append(
        JournalEvent("attempt_sent", NOW + timedelta(seconds=13), attempt_id=second_id)
    )
    journal = journal.append(
        JournalEvent(
            "response_received",
            NOW + timedelta(seconds=13),
            attempt_id=second_id,
            status=200,
            transfer_bytes=10,
            decoded_bytes=20,
        )
    )
    journal = journal.append(
        JournalEvent(
            "body_saved",
            NOW + timedelta(seconds=13),
            attempt_id=second_id,
            body_sha256=DIGEST,
            saved_bytes=20,
        )
    )
    with pytest.raises(HttpContractError, match="pagination_key_loop"):
        journal.append(
            JournalEvent(
                "page_completed",
                NOW + timedelta(seconds=13),
                attempt_id=second_id,
                next_key="page-two",
            )
        )


def test_offline_entry_still_rejects_nonfixture_before_creating_store(tmp_path):
    output = tmp_path / "not-created"
    with pytest.raises(AcquisitionStopped, match="only_offline_fixture"):
        run_offline_fixture_acquisition(
            output, plan(), object(), clock=lambda: NOW, sleep=lambda _: None
        )
    assert not output.exists()
    assert OfflineFixtureTransport.__name__ == "OfflineFixtureTransport"
