"""Artificial-only persistence and fencing tests; never contact a market API."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context

import pytest

from feasibility.http_contract import (
    HTTP_OUTPUT_ROOT,
    AccountRatePolicy,
    BodyFileEvidence,
    BodyInventory,
    HttpAcquisitionPlan,
    HttpContractError,
    HttpLimits,
    JournalEvent,
    OwnerApprovalClaim,
    RetryRules,
)
from feasibility.http_storage import AccountLedgerStore, StorageError

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
ACCOUNT = "artificial-storage-account"


def later(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


def fixed(artifact: str = "artificial-storage") -> HttpAcquisitionPlan:
    limits = HttpLimits(
        max_attempts=4,
        max_pages_total=4,
        max_pages_per_query=2,
        max_elapsed_seconds=1200,
        max_transfer_bytes=1000,
        max_decoded_bytes=1000,
        max_saved_bytes=1000,
        max_page_transfer_bytes=500,
        max_page_decoded_bytes=500,
        max_page_saved_bytes=500,
    )
    retry = RetryRules(
        max_attempts_per_page=2,
        min_interval_seconds=13,
        min_wait_after_429_seconds=120,
        min_wait_after_5xx_seconds=13,
        min_wait_after_network_error_seconds=13,
        timeout_seconds=30,
    )
    return HttpAcquisitionPlan(
        artifact_id=artifact,
        kind="calendar_discovery",
        reference_date="2025-03-04",
        calendar_start="2025-03-03",
        calendar_end="2025-03-04",
        master_date=None,
        daily_dates=(),
        calendar_source_sha256=None,
        calendar_source_reference=None,
        output_dir=str(HTTP_OUTPUT_ROOT / artifact),
        not_before=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        limits=limits,
        retry=retry,
        account_ref=ACCOUNT,
    )


def approval(plan: HttpAcquisitionPlan) -> OwnerApprovalClaim:
    return OwnerApprovalClaim(
        plan_sha256=plan.sha256,
        artifact_id=plan.artifact_id,
        scope_sha256=plan.scope_sha256,
        allowed_endpoints=plan.allowed_endpoints,
        reference_date=plan.reference_date,
        calendar_start=plan.calendar_start,
        calendar_end=plan.calendar_end,
        master_date=plan.master_date,
        daily_dates=plan.daily_dates,
        limits=plan.limits,
        retry=plan.retry,
        output_dir=plan.output_dir,
        valid_from=NOW - timedelta(seconds=30),
        valid_until=NOW + timedelta(minutes=30),
        approver_id="artificial-approver",
        approval_event_id="artificial-approval-event",
        evidence_reference="urn:artificial:storage-approval",
    )


def policy() -> AccountRatePolicy:
    return AccountRatePolicy(13, 120, 13, 13, 30, 60)


def prepared(tmp_path, *, artifact="artificial-storage"):
    plan = fixed(artifact)
    claim = approval(plan)
    store = AccountLedgerStore(tmp_path / "account-rate.sqlite3", ACCOUNT)
    store.initialize(policy(), at=NOW - timedelta(minutes=1))
    store.register_plan(plan, claim)
    return store, plan, claim


def reserve(store, plan, claim, *, at=NOW, holder="holder-a", **kwargs):
    return store.reserve_slot(
        plan,
        claim,
        at=at,
        holder_id=holder,
        inventory=lambda _journal: BodyInventory(()),
        **kwargs,
    )


def test_register_and_reload_full_hash_chains(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    reopened = AccountLedgerStore(store.path, ACCOUNT)
    assert reopened.load().head_hash == store.load().head_hash
    assert reopened.load_journal(plan, claim).events[-1].attempt_id == token.attempt_id
    assert token.generation == 1
    with pytest.raises(StorageError, match="stale_slot_generation"):
        reopened.assert_current(
            type(token)(
                token.account_ref,
                token.plan_sha256,
                token.attempt_id,
                "wrong-holder",
                token.generation,
                token.page,
                token.allowed_transfer_bytes,
                token.allowed_decoded_bytes,
                token.allowed_saved_bytes,
            )
        )


def test_forged_token_allowance_cannot_start_send(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    forged = replace(token, allowed_transfer_bytes=token.allowed_transfer_bytes + 1)
    with pytest.raises(StorageError, match="slot_token_plan_evidence_mismatch"):
        store.mark_sent(
            forged,
            plan,
            claim,
            at=later(1),
            inventory=lambda _journal: BodyInventory(()),
        )
    assert store.load_journal(plan, claim).events[-1].kind == "attempt_reserved"


def test_two_connections_reserve_only_once_and_cas_rejects_stale(tmp_path):
    store, plan, claim = prepared(tmp_path)
    stale_head = store.load().head_hash
    second = AccountLedgerStore(store.path, ACCOUNT)

    def attempt(holder):
        return reserve(
            AccountLedgerStore(store.path, ACCOUNT), plan, claim, holder=holder
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda h: _try(attempt, h), ("holder-a", "holder-b")))
    assert sum(isinstance(value, StorageError) for value in outcomes) == 1
    assert sum(not isinstance(value, StorageError) for value in outcomes) == 1
    assert store.load().event_count == 2  # opened plus exactly one slot
    assert store.load_journal(plan, claim).event_count == 1
    with pytest.raises(StorageError, match="account_compare_and_append_stale"):
        reserve(second, plan, claim, expected_account_head=stale_head)


def test_reservation_reloads_fresh_journal_under_lock(tmp_path):
    store, plan, claim = prepared(tmp_path)
    seen = []

    def inspect(journal):
        seen.append((journal.head_hash, journal.event_count))
        return BodyInventory(())

    initial = store.load_journal(plan, claim)
    store.reserve_slot(plan, claim, at=NOW, holder_id="holder-a", inventory=inspect)
    assert seen == [(initial.head_hash, 0)]


def test_send_rechecks_approval_and_preserves_unsent_reservation(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    account_head = store.load().head_hash
    plan_head = store.load_journal(plan, claim).head_hash
    with pytest.raises(HttpContractError, match="approval_outside_validity_window"):
        store.mark_sent(
            token,
            plan,
            claim,
            at=claim.valid_until,
            inventory=lambda _journal: BodyInventory(()),
        )
    assert store.load().head_hash == account_head
    assert store.load_journal(plan, claim).head_hash == plan_head
    assert (
        store.load_journal(plan, claim)._state.attempts[token.attempt_id].state
        == "reserved"
    )


def test_send_rechecks_fresh_body_inventory_before_any_send_record(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    account_head = store.load().head_hash
    plan_head = store.load_journal(plan, claim).head_hash
    orphan = BodyInventory((BodyFileEvidence("orphan", 1, "orphan", "a" * 64),))
    with pytest.raises(
        StorageError, match="plan_send_blocked:orphan_or_partial_body_present"
    ):
        store.mark_sent(
            token,
            plan,
            claim,
            at=later(1),
            inventory=lambda _journal: orphan,
        )
    assert store.load().head_hash == account_head
    assert store.load_journal(plan, claim).head_hash == plan_head


def test_send_requires_every_registered_related_plan(tmp_path):
    store, plan_a, claim_a = prepared(tmp_path)
    token = reserve(store, plan_a, claim_a)
    plan_b = fixed("artificial-send-related")
    claim_b = approval(plan_b)
    store.register_plan(plan_b, claim_b)
    with pytest.raises(StorageError, match="related_plan_journals_incomplete"):
        store.mark_sent(
            token,
            plan_a,
            claim_a,
            at=later(1),
            inventory=lambda _journal: BodyInventory(()),
        )
    timeout = store.mark_sent(
        token,
        plan_a,
        claim_a,
        at=later(1),
        inventory=lambda _journal: BodyInventory(()),
        related=((plan_b, claim_b),),
    )
    assert timeout == 30


def test_missing_related_plan_is_not_treated_as_verified(tmp_path):
    store, plan_a, claim_a = prepared(tmp_path)
    plan_b = fixed("artificial-second")
    claim_b = approval(plan_b)
    store.register_plan(plan_b, claim_b)
    with pytest.raises(StorageError, match="related_plan_journals_incomplete"):
        reserve(store, plan_a, claim_a)
    token = reserve(store, plan_a, claim_a, related=((plan_b, claim_b),))
    assert token.plan_sha256 == plan_a.sha256


def test_paired_reservation_rolls_back_if_second_write_fails(tmp_path, monkeypatch):
    store, plan, claim = prepared(tmp_path)
    account_head = store.load().head_hash
    plan_head = store.load_journal(plan, claim).head_hash

    def fail(*_args):
        raise RuntimeError("artificial crash before plan commit")

    monkeypatch.setattr(store, "_write_plan", fail)
    with pytest.raises(RuntimeError, match="artificial crash"):
        reserve(store, plan, claim)
    reopened = AccountLedgerStore(store.path, ACCOUNT)
    assert reopened.load().head_hash == account_head
    assert reopened.load_journal(plan, claim).head_hash == plan_head


def _exit_during_transaction(path):
    # Abrupt child death tests SQLite's rollback journal, not a Python exception.
    import os

    with sqlite3.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE account_ledgers SET data=? WHERE account_ref=?",
            (b"corrupt\n", ACCOUNT),
        )
        os._exit(0)


def test_abrupt_process_exit_rolls_back_uncommitted_account_change(tmp_path):
    store, _plan, _claim = prepared(tmp_path)
    before = store.load().head_hash
    child = get_context("spawn").Process(
        target=_exit_during_transaction, args=(str(store.path),)
    )
    child.start()
    child.join(timeout=10)
    if child.is_alive():
        child.kill()
        child.join(timeout=10)
        pytest.fail("artificial child did not exit")
    assert child.exitcode == 0
    assert AccountLedgerStore(store.path, ACCOUNT).load().head_hash == before


def _try(call, arg):
    try:
        return call(arg)
    except StorageError as error:
        return error


def test_response_and_body_are_fenced_and_next_slot_waits_for_page(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    store.mark_sent(
        token, plan, claim, at=later(1), inventory=lambda _journal: BodyInventory(())
    )
    store.settle_response(
        token,
        plan,
        claim,
        at=later(2),
        status=200,
        transfer_bytes=10,
        decoded_bytes=10,
        observed_retry_after_seconds=None,
        effective_wait_seconds=13,
    )
    with pytest.raises(StorageError, match="related_plan_attempt_unfinished"):
        reserve(store, plan, claim, at=later(15))
    published = []
    store.commit_body(
        token,
        plan,
        claim,
        at=later(3),
        object_id=f"{token.attempt_id}.json",
        body_sha256="b" * 64,
        saved_bytes=10,
        next_key=None,
        finalize=lambda: published.append("atomic-publish"),
    )
    assert published == ["atomic-publish"]
    assert store.load_journal(plan, claim).events[-1].kind == "page_completed"
    assert store.load().event_count == 4
    assert store.body_commits()[0].body_sha256 == "b" * 64
    # The single Calendar query is finished: no extra page or slot is invented.
    with pytest.raises(StorageError, match="plan_reservation_blocked"):
        store.reserve_slot(
            plan,
            claim,
            at=later(15),
            holder_id="holder-b",
            inventory=lambda _journal: BodyInventory(
                (
                    BodyFileEvidence(
                        f"{token.attempt_id}.json", 10, "committed", "b" * 64
                    ),
                )
            ),
        )


def test_unknown_after_reclaim_fences_old_holder_and_survives_restart(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    store.mark_sent(
        token, plan, claim, at=later(1), inventory=lambda _journal: BodyInventory(())
    )
    store.reclaim(
        token,
        plan,
        claim,
        at=later(60),
        new_holder_id="holder-b",
        effective_wait_seconds=120,
    )
    restarted = AccountLedgerStore(store.path, ACCOUNT)
    assert restarted.load()._state.slots[token.attempt_id].outcome == "unknown"
    assert restarted.load_journal(plan, claim).events[-1].kind == "outcome_unknown"
    with pytest.raises(StorageError, match="stale_slot_generation"):
        restarted.assert_current(token)
    with pytest.raises(StorageError, match="stale_slot_generation"):
        restarted.settle_response(
            token,
            plan,
            claim,
            at=later(61),
            status=200,
            transfer_bytes=1,
            decoded_bytes=1,
            observed_retry_after_seconds=None,
            effective_wait_seconds=13,
        )
    with pytest.raises(StorageError, match="reservation_blocked"):
        reserve(restarted, plan, claim, at=later(179), holder="holder-c")
    retry = reserve(restarted, plan, claim, at=later(180), holder="holder-c")
    assert retry.generation > token.generation


def _exit_with_open_slot(path, artifact, sent):
    import os

    plan = fixed(artifact)
    claim = approval(plan)
    store = AccountLedgerStore(path, ACCOUNT)
    token = reserve(store, plan, claim)
    if sent:
        store.mark_sent(
            token,
            plan,
            claim,
            at=later(1),
            inventory=lambda _journal: BodyInventory(()),
        )
    os._exit(0)


@pytest.mark.parametrize("sent", [False, True])
def test_public_recovery_inspects_and_reclaims_crashed_process(tmp_path, sent):
    store, plan, claim = prepared(tmp_path)
    child = get_context("spawn").Process(
        target=_exit_with_open_slot,
        args=(str(store.path), plan.artifact_id, sent),
    )
    child.start()
    child.join(timeout=10)
    if child.is_alive():
        child.kill()
        child.join(timeout=10)
        pytest.fail("artificial child did not exit")
    assert child.exitcode == 0

    reopened = AccountLedgerStore(store.path, ACCOUNT)
    inspected = reopened.inspect_open_slot(plan, claim)
    assert inspected is not None
    assert inspected.account_ref == ACCOUNT
    assert inspected.plan_sha256 == plan.sha256
    assert not hasattr(inspected, "token")
    assert inspected.holder_id == "holder-a"
    assert inspected.generation == 1
    assert inspected.reserved_at == NOW
    assert inspected.sent_at == (later(1) if sent else None)
    assert inspected.lease_expires_at == later(60)
    assert inspected.state == ("sent" if sent else "reserved")
    before_account = reopened.load().head_hash
    before_plan = reopened.load_journal(plan, claim).head_hash
    with pytest.raises(
        HttpContractError, match="account_slot_reclaim_before_lease_expiry"
    ):
        reopened.reclaim_open_slot(
            inspected, plan, claim, at=later(59), new_holder_id="holder-b"
        )
    assert reopened.load().head_hash == before_account
    assert reopened.load_journal(plan, claim).head_hash == before_plan

    other_connection = AccountLedgerStore(store.path, ACCOUNT)
    other_connection.reclaim_open_slot(
        inspected, plan, claim, at=later(60), new_holder_id="holder-b"
    )
    assert reopened.inspect_open_slot(plan, claim) is None
    slot = reopened.load()._state.slots[inspected.attempt_id]
    assert slot.outcome == "unknown"
    assert slot.effective_wait == 120
    assert reopened.load_journal(plan, claim)._state.unknown == 1
    with pytest.raises(StorageError, match="open_slot_inspection_stale"):
        reopened.reclaim_open_slot(
            inspected, plan, claim, at=later(61), new_holder_id="holder-c"
        )
    with pytest.raises(StorageError, match="reservation_blocked"):
        reserve(reopened, plan, claim, at=later(179), holder="holder-c")
    retry = reserve(reopened, plan, claim, at=later(180), holder="holder-c")
    assert retry.generation > inspected.generation


def test_recovery_rejects_stale_inspection_and_wrong_holder(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    inspected = store.inspect_open_slot(plan, claim)
    assert inspected is not None
    with pytest.raises(StorageError, match="open_slot_inspection_stale"):
        store.reclaim_open_slot(
            replace(inspected, holder_id="forged-holder"),
            plan,
            claim,
            at=later(60),
            new_holder_id="holder-b",
        )
    AccountLedgerStore(store.path, ACCOUNT).mark_sent(
        token,
        plan,
        claim,
        at=later(1),
        inventory=lambda _journal: BodyInventory(()),
    )
    with pytest.raises(StorageError, match="open_slot_inspection_stale"):
        store.reclaim_open_slot(
            inspected, plan, claim, at=later(60), new_holder_id="holder-b"
        )
    current = store.inspect_open_slot(plan, claim)
    assert current.state == "sent"
    with pytest.raises(HttpContractError, match="account_reclaim_by_the_holder_itself"):
        store.reclaim_open_slot(
            current, plan, claim, at=later(60), new_holder_id="holder-a"
        )
    store.reclaim_open_slot(
        current, plan, claim, at=later(60), new_holder_id="holder-b"
    )
    with pytest.raises(StorageError, match="stale_slot_generation"):
        store.mark_sent(
            token,
            plan,
            claim,
            at=later(61),
            inventory=lambda _journal: BodyInventory(()),
        )
    with pytest.raises(StorageError, match="stale_slot_generation"):
        store.settle_unknown(
            token, plan, claim, at=later(61), effective_wait_seconds=120
        )
    with pytest.raises(StorageError, match="stale_slot_generation"):
        store.commit_body(
            token,
            plan,
            claim,
            at=later(61),
            object_id=f"{token.attempt_id}.json",
            body_sha256="b" * 64,
            saved_bytes=1,
            next_key=None,
            finalize=lambda: pytest.fail("stale finalizer called"),
        )


def test_recovery_rejects_inspection_after_other_connection_settles(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    inspected = store.inspect_open_slot(plan, claim)
    assert inspected is not None
    other_connection = AccountLedgerStore(store.path, ACCOUNT)
    other_connection.settle_unknown(
        token, plan, claim, at=later(1), effective_wait_seconds=120
    )
    account_head = store.load().head_hash
    plan_head = store.load_journal(plan, claim).head_hash
    with pytest.raises(StorageError, match="open_slot_inspection_stale"):
        store.reclaim_open_slot(
            inspected, plan, claim, at=later(60), new_holder_id="holder-b"
        )
    assert store.load().head_hash == account_head
    assert store.load_journal(plan, claim).head_hash == plan_head


def test_429_with_unrepresentable_hint_quarantines_atomically(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    store.mark_sent(
        token, plan, claim, at=later(1), inventory=lambda _journal: BodyInventory(())
    )
    store.settle_response(
        token,
        plan,
        claim,
        at=later(2),
        status=429,
        transfer_bytes=10,
        decoded_bytes=10,
        observed_retry_after_seconds=None,
        effective_wait_seconds=120,
        hold_reason="retry_after_unrepresentable",
    )
    reopened = AccountLedgerStore(store.path, ACCOUNT)
    assert reopened.hold_reason() == "retry_after_unrepresentable"
    assert reopened.load()._state.slots[token.attempt_id].outcome == "429"
    with pytest.raises(StorageError, match="account_quarantined"):
        reserve(reopened, plan, claim, at=later(1000))
    with pytest.raises(StorageError, match="account_already_held"):
        reopened.account_hold("different_reason")
    assert reopened.hold_reason() == "retry_after_unrepresentable"


def test_timeout_unknown_and_hold_are_persisted_together(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    store.mark_sent(
        token, plan, claim, at=later(1), inventory=lambda _journal: BodyInventory(())
    )
    store.settle_unknown(
        token,
        plan,
        claim,
        at=later(31),
        effective_wait_seconds=120,
        hold_reason="response_after_contract_timeout",
    )
    reopened = AccountLedgerStore(store.path, ACCOUNT)
    assert reopened.hold_reason() == "response_after_contract_timeout"
    assert reopened.load()._state.slots[token.attempt_id].outcome == "unknown"
    assert reopened.load_journal(plan, claim).events[-1].kind == "outcome_unknown"


def test_corrupt_account_or_plan_blob_refused_without_repair(tmp_path):
    store, plan, claim = prepared(tmp_path)
    original = store.load().data
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE account_ledgers SET data=? WHERE account_ref=?",
            (b"corrupt\n", ACCOUNT),
        )
    with pytest.raises(StorageError, match="account_ledger_corrupt"):
        store.load()
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE account_ledgers SET data=? WHERE account_ref=?", (original, ACCOUNT)
        )
        db.execute(
            "UPDATE plan_journals SET data=? WHERE account_ref=? AND plan_sha256=?",
            (b"bad\n", ACCOUNT, plan.sha256),
        )
    with pytest.raises(StorageError, match="plan_journal_corrupt"):
        store.load_journal(plan, claim)


def test_failed_finalize_rolls_back_body_metadata_and_journal(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    store.mark_sent(
        token, plan, claim, at=later(1), inventory=lambda _journal: BodyInventory(())
    )
    store.settle_response(
        token,
        plan,
        claim,
        at=later(2),
        status=200,
        transfer_bytes=10,
        decoded_bytes=10,
        observed_retry_after_seconds=None,
        effective_wait_seconds=13,
    )
    head = store.load_journal(plan, claim).head_hash

    def failed():
        raise RuntimeError("artificial rename failure")

    with pytest.raises(RuntimeError, match="artificial rename failure"):
        store.commit_body(
            token,
            plan,
            claim,
            at=later(3),
            object_id=f"{token.attempt_id}.json",
            body_sha256="b" * 64,
            saved_bytes=10,
            next_key=None,
            finalize=failed,
        )
    assert store.load_journal(plan, claim).head_hash == head
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM body_commits").fetchone()[0] == 0
    with pytest.raises(StorageError, match="body_finalize_or_id_invalid"):
        store.commit_body(
            token,
            plan,
            claim,
            at=later(3),
            object_id="../escape",
            body_sha256="b" * 64,
            saved_bytes=10,
            next_key=None,
            finalize=lambda: pytest.fail("unsafe path reached finalizer"),
        )


def test_body_link_tamper_is_refused_on_journal_reload(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    store.mark_sent(
        token, plan, claim, at=later(1), inventory=lambda _journal: BodyInventory(())
    )
    store.settle_response(
        token,
        plan,
        claim,
        at=later(2),
        status=200,
        transfer_bytes=10,
        decoded_bytes=10,
        observed_retry_after_seconds=None,
        effective_wait_seconds=13,
    )
    store.commit_body(
        token,
        plan,
        claim,
        at=later(3),
        object_id=f"{token.attempt_id}.json",
        body_sha256="b" * 64,
        saved_bytes=10,
        next_key=None,
        finalize=lambda: None,
    )
    assert store.load_journal(plan, claim).events[-1].kind == "page_completed"
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE body_commits SET saved_bytes=11 WHERE account_ref=?",
            (ACCOUNT,),
        )
    with pytest.raises(StorageError, match="body_commit_journal_links_mismatch"):
        AccountLedgerStore(store.path, ACCOUNT).load_journal(plan, claim)


def test_body_object_id_must_name_its_attempt_and_reload_rejects_alias(tmp_path):
    store, plan, claim = prepared(tmp_path)
    token = reserve(store, plan, claim)
    store.mark_sent(
        token, plan, claim, at=later(1), inventory=lambda _journal: BodyInventory(())
    )
    store.settle_response(
        token,
        plan,
        claim,
        at=later(2),
        status=200,
        transfer_bytes=10,
        decoded_bytes=10,
        observed_retry_after_seconds=None,
        effective_wait_seconds=13,
    )
    with pytest.raises(StorageError, match="body_finalize_or_id_invalid"):
        store.commit_body(
            token,
            plan,
            claim,
            at=later(3),
            object_id="body-1",
            body_sha256="b" * 64,
            saved_bytes=10,
            next_key=None,
            finalize=lambda: pytest.fail("alias should not be published"),
        )
    store.commit_body(
        token,
        plan,
        claim,
        at=later(3),
        object_id=f"{token.attempt_id}.json",
        body_sha256="b" * 64,
        saved_bytes=10,
        next_key=None,
        finalize=lambda: None,
    )
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE body_commits SET object_id=? WHERE account_ref=?",
            ("body-1", ACCOUNT),
        )
    with pytest.raises(StorageError, match="body_commit_journal_links_mismatch"):
        store.load_journal(plan, claim)
    with pytest.raises(StorageError, match="body_commit_metadata_invalid"):
        store.body_commits()


def test_plan_terminal_event_uses_compare_and_append(tmp_path):
    store, plan, claim = prepared(tmp_path)
    journal = store.load_journal(plan, claim)
    stopped = store.append_plan_event(
        plan,
        claim,
        JournalEvent("run_stopped", NOW, reason="artificial_stop", terminal=True),
        expected_head=journal.head_hash,
    )
    assert stopped.event_count == 1
    with pytest.raises(StorageError, match="plan_compare_and_append_stale"):
        store.append_plan_event(
            plan,
            claim,
            JournalEvent(
                "run_stopped", later(1), reason="artificial_stop", terminal=True
            ),
            expected_head=journal.head_hash,
        )
