"""End-to-end artificial localhost trials; no market host or credential."""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from feasibility.acquisition import DAILY, MASTER, MASTER_TEXT_FIELDS
from feasibility.http_bodies import RawBodyStorageError
from feasibility.http_contract import (
    HTTP_OUTPUT_ROOT,
    AccountRatePolicy,
    BodyInventory,
    HttpAcquisitionPlan,
    HttpContractError,
    HttpLimits,
    OwnerApprovalClaim,
    RetryRules,
    assess_live_acquisition_gate,
)
from feasibility.http_runner import (
    LocalhostAcquisitionRunner,
    LocalhostRecoverySession,
    LocalhostTrialStopped,
)
from feasibility.http_storage import StorageError
from feasibility.http_transport import LocalhostHttpTransport

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value

    def tick(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def fixed(*, artifact: str = "artificial-localhost", kind="calendar_discovery"):
    daily = ("2025-03-04",) if kind != "calendar_discovery" else ()
    return HttpAcquisitionPlan(
        artifact_id=artifact,
        kind=kind,
        reference_date="2025-03-04",
        calendar_start="2025-03-03",
        calendar_end="2025-03-04",
        master_date="2025-03-04" if daily else None,
        daily_dates=daily,
        calendar_source_sha256="a" * 64 if daily else None,
        calendar_source_reference="urn:artificial:calendar" if daily else None,
        output_dir=str(HTTP_OUTPUT_ROOT / artifact),
        not_before=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=2),
        limits=HttpLimits(
            max_attempts=8,
            max_pages_total=8,
            max_pages_per_query=3,
            max_elapsed_seconds=3600,
            max_transfer_bytes=50_000,
            max_decoded_bytes=50_000,
            max_saved_bytes=50_000,
            max_page_transfer_bytes=10_000,
            max_page_decoded_bytes=10_000,
            max_page_saved_bytes=10_000,
        ),
        retry=RetryRules(
            max_attempts_per_page=3,
            min_interval_seconds=13,
            min_wait_after_429_seconds=120,
            min_wait_after_5xx_seconds=13,
            min_wait_after_network_error_seconds=13,
            timeout_seconds=5,
        ),
        account_ref="artificial-localhost-account",
    )


def claim(plan: HttpAcquisitionPlan) -> OwnerApprovalClaim:
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
        valid_until=NOW + timedelta(hours=1),
        approver_id="artificial-not-owner",
        approval_event_id="artificial-claim-event",
        evidence_reference="urn:artificial:scope-only",
    )


def policy() -> AccountRatePolicy:
    return AccountRatePolicy(13, 120, 13, 13, 5, 20)


def payload(rows, *, next_key=None) -> bytes:
    return json.dumps(
        {"data": rows, "pagination_key": next_key},
        separators=(",", ":"),
    ).encode()


@contextmanager
def localhost(responses, clock: Clock):
    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            calls.append(self.path)
            clock.tick(1)
            status, headers, body = responses.pop(0)
            if status is None:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.close_connection = True
                return
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield LocalhostHttpTransport(f"http://127.0.0.1:{server.server_port}"), calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


def create(tmp_path: Path, plan, transport, clock):
    return LocalhostAcquisitionRunner.create(
        tmp_path / plan.artifact_id,
        tmp_path / "account-rate.sqlite3",
        plan,
        claim(plan),
        policy(),
        transport,
        clock=clock.now,
        holder_id="artificial-holder",
    )


def resume(tmp_path: Path, plan, transport, clock):
    return LocalhostAcquisitionRunner.open(
        tmp_path / plan.artifact_id,
        tmp_path / "account-rate.sqlite3",
        plan,
        claim(plan),
        policy(),
        transport,
        clock=clock.now,
        holder_id="resumed-holder",
    )


def test_small_200_is_durable_and_resume_does_not_resend(tmp_path):
    clock = Clock()
    plan = fixed()
    response = payload([{"Date": "2025-03-03", "HolDiv": "1"}])
    with localhost([(200, {}, response)], clock) as (transport, calls):
        runner = create(tmp_path, plan, transport, clock)
        result = runner.run_one()
        assert result.completed and result.status_code == 200
        assert len(calls) == 1
        fresh = resume(tmp_path, plan, transport, clock)
        assert fresh.run_one().state == "completed"
        assert len(calls) == 1
        journal = fresh.account.load_journal(plan, claim(plan))
        inventory = fresh.bodies.inventory(journal)
        assert len(inventory.files) == 1
        assert inventory.files[0].state == "committed"
        link = fresh.account.body_commits()[0]
        assert link.body_sha256 == inventory.files[0].body_sha256
        assert link.saved_bytes == len(response)


def test_send_rechecks_approval_after_reserved_clock_jump(tmp_path, monkeypatch):
    clock = Clock()
    plan = fixed()
    with localhost(
        [(200, {}, payload([{"Date": "2025-03-03", "HolDiv": "1"}]))], clock
    ) as (
        transport,
        calls,
    ):
        runner = create(tmp_path, plan, transport, clock)
        original = runner.account.reserve_slot

        def reserve_then_expire(*args, **kwargs):
            token = original(*args, **kwargs)
            clock.tick(3600)
            return token

        monkeypatch.setattr(runner.account, "reserve_slot", reserve_then_expire)
        with pytest.raises(
            LocalhostTrialStopped, match="approval_outside_validity_window"
        ):
            runner.run_one()
        assert not calls
        assert runner.account.hold_reason() == "setup_after_reservation_failed"
        journal = runner.account.load_journal(plan, claim(plan))
        assert (
            journal._state.attempts[next(iter(journal._state.attempts))].state
            == "unknown"
        )


@pytest.mark.parametrize(
    ("clock_jump", "reason"),
    ((6, "deadline_before_http_send"), (-1, "send_clock_regressed")),
)
def test_no_request_after_file_setup_clock_change(
    tmp_path, monkeypatch, clock_jump, reason
):
    clock = Clock()
    plan = fixed()
    with localhost(
        [(200, {}, payload([{"Date": "2025-03-03", "HolDiv": "1"}]))], clock
    ) as (
        transport,
        calls,
    ):
        runner = create(tmp_path, plan, transport, clock)
        original = type(runner.bodies).begin_partial

        def begin_then_advance(body_store, *args, **kwargs):
            value = original(body_store, *args, **kwargs)
            clock.tick(clock_jump)  # request timeout is five seconds
            return value

        monkeypatch.setattr(type(runner.bodies), "begin_partial", begin_then_advance)
        with pytest.raises(LocalhostTrialStopped, match=reason):
            runner.run_one()
        assert not calls
        assert runner.account.hold_reason() == "setup_after_reservation_failed"


def test_two_pages_resume_keeps_pagination_and_account_wait(tmp_path):
    clock = Clock()
    plan = fixed()
    responses = [
        (200, {}, payload([{"Date": "2025-03-03", "HolDiv": "1"}], next_key="p2")),
        (200, {}, payload([{"Date": "2025-03-04", "HolDiv": "1"}])),
    ]
    with localhost(responses, clock) as (transport, calls):
        runner = create(tmp_path, plan, transport, clock)
        assert not runner.run_one().completed
        with pytest.raises(LocalhostTrialStopped, match="rate_limit_wait"):
            runner.run_one()
        clock.tick(13)
        resumed = resume(tmp_path, plan, transport, clock)
        assert resumed.run_one().completed
        assert len(calls) == 2
        assert "pagination_key=p2" in calls[1]
        assert (
            len(
                resumed.bodies.inventory(
                    resumed.account.load_journal(plan, claim(plan))
                ).files
            )
            == 2
        )


def test_calendar_master_daily_are_sequential_and_no_future_query(tmp_path):
    clock = Clock()
    plan = fixed(kind="predeclared_daily")
    master = {"Date": "2025-03-04", "Code": "12340"}
    master.update({name: "x" for name in MASTER_TEXT_FIELDS})
    daily = {"Date": "2025-03-04", "Code": "12340"}
    daily.update(
        {
            name: 1
            for name in (
                "O",
                "H",
                "L",
                "C",
                "AdjO",
                "AdjH",
                "AdjL",
                "AdjC",
                "AdjFactor",
                "Vo",
                "Va",
                "AdjVo",
            )
        }
    )
    responses = [
        (200, {}, payload([{"Date": "2025-03-04", "HolDiv": "1"}])),
        (200, {}, payload([master])),
        (200, {}, payload([daily])),
    ]
    with localhost(responses, clock) as (transport, calls):
        runner = create(tmp_path, plan, transport, clock)
        assert not runner.run_one().completed
        clock.tick(13)
        assert not runner.run_one().completed
        clock.tick(13)
        assert runner.run_one().completed
        assert len(calls) == 3
        assert calls[0].startswith("/markets/calendar?")
        assert calls[1].startswith(MASTER + "?")
        assert calls[2].startswith(DAILY + "?")


def test_identical_bodies_across_queries_stop_before_second_page_commit(tmp_path):
    clock = Clock()
    plan = fixed(kind="predeclared_daily")
    empty = payload([])
    with localhost([(200, {}, empty), (200, {}, empty)], clock) as (
        transport,
        calls,
    ):
        runner = create(tmp_path, plan, transport, clock)
        assert not runner.run_one().completed
        clock.tick(13)
        with pytest.raises(
            LocalhostTrialStopped, match="duplicate_body_digest_unsupported"
        ):
            runner.run_one()
        journal = runner.account.load_journal(plan, claim(plan))
        assert journal._state.completed_pages == 1
        assert journal._state.terminal_stop
        assert len(runner.account.body_commits()) == 1
        assert runner.account.hold_reason() == "body_or_schema_commit_failed"
        assert len(calls) == 2


def test_429_retry_after_is_persisted_and_blocks_other_plan(tmp_path):
    clock = Clock()
    plan = fixed()
    responses = [
        (429, {"Retry-After": "600"}, b"too many requests"),
        (200, {}, payload([{"Date": "2025-03-03", "HolDiv": "1"}])),
    ]
    with localhost(responses, clock) as (transport, calls):
        runner = create(tmp_path, plan, transport, clock)
        assert runner.run_one().status_code == 429
        journal = runner.account.load_journal(plan, claim(plan))
        assert journal.events[-1].retry_after_seconds == 600
        slot = runner.account.load()._state.slots[journal.events[0].attempt_id]
        assert slot.observed_retry_after == 600
        assert slot.effective_wait >= 600
        clock.tick(599)
        with pytest.raises(LocalhostTrialStopped, match="rate_limit_wait"):
            runner.run_one()
        clock.tick(1)
        assert runner.run_one().completed
        assert len(calls) == 2


def test_malformed_retry_after_holds_account_and_never_opens_live_gate(tmp_path):
    clock = Clock()
    plan = fixed()
    with localhost([(429, {"Retry-After": "not-a-date"}, b"limited")], clock) as (
        transport,
        calls,
    ):
        runner = create(tmp_path, plan, transport, clock)
        with pytest.raises(LocalhostTrialStopped, match="retry_after_unrepresentable"):
            runner.run_one()
        assert len(calls) == 1
        assert runner.account.hold_reason() == "retry_after_unrepresentable"
        journal = runner.account.load_journal(plan, claim(plan))
        assert journal._state.terminal_stop
        assert not assess_live_acquisition_gate(
            plan, claim(plan), journal, BodyInventory(()), None, now=clock.now()
        ).permitted


def test_incomplete_429_preserves_uncertainty_and_quarantines_account(tmp_path):
    clock = Clock()
    plan = fixed()
    with localhost(
        [
            (
                429,
                {"Retry-After": "600", "Content-Length": "20"},
                b"short",
            )
        ],
        clock,
    ) as (transport, calls):
        runner = create(tmp_path, plan, transport, clock)
        with pytest.raises(LocalhostTrialStopped, match="content_length_mismatch"):
            runner.run_one()
        assert len(calls) == 1
        assert runner.account.hold_reason() == "incomplete_http_response"
        journal = runner.account.load_journal(plan, claim(plan))
        assert journal._state.unknown == 1
        assert journal._state.terminal_stop
        slot = runner.account.load()._state.slots[journal.events[0].attempt_id]
        assert slot.outcome == "unknown"
        # The frozen contract cannot encode an observed header on an
        # incomplete HTTP response; never treat this as a complete 429.
        assert slot.observed_retry_after is None
        assert not runner.account.body_commits()


def test_503_and_unknown_use_separate_conservative_waits(tmp_path):
    clock = Clock()
    plan = fixed()
    responses = [
        (503, {}, b"temporarily unavailable"),
        (None, {}, b""),
        (200, {}, payload([{"Date": "2025-03-03", "HolDiv": "1"}])),
    ]
    with localhost(responses, clock) as (transport, calls):
        runner = create(tmp_path, plan, transport, clock)
        assert runner.run_one().status_code == 503
        clock.tick(13)
        assert runner.run_one().state == "unknown"
        journal = runner.account.load_journal(plan, claim(plan))
        assert journal._state.unknown == 1
        assert runner.account.hold_reason() is None
        clock.tick(119)
        with pytest.raises(LocalhostTrialStopped, match="rate_limit_wait"):
            runner.run_one()
        clock.tick(1)
        assert runner.run_one().completed
        assert len(calls) == 3


def test_invalid_200_schema_is_not_committed_or_retried(tmp_path):
    clock = Clock()
    plan = fixed()
    with localhost([(200, {}, b'{"data":"not a list"}')], clock) as (transport, calls):
        runner = create(tmp_path, plan, transport, clock)
        with pytest.raises(LocalhostTrialStopped, match="unsupported_response_schema"):
            runner.run_one()
        assert runner.account.hold_reason() == "body_or_schema_commit_failed"
        journal = runner.account.load_journal(plan, claim(plan))
        assert journal._state.terminal_stop
        assert not runner.account.body_commits()
        assert runner.bodies.inventory(journal).files[0].state == "partial"
        with pytest.raises(LocalhostTrialStopped, match="terminal_stop_recorded"):
            resume(tmp_path, plan, transport, clock).run_one()
        assert len(calls) == 1


def test_partial_and_tampered_committed_body_fail_closed_on_resume(tmp_path):
    clock = Clock()
    plan = fixed()
    response = payload([{"Date": "2025-03-03", "HolDiv": "1"}])
    with localhost([(200, {}, response)], clock) as (transport, _):
        runner = create(tmp_path, plan, transport, clock)
        path, handle = runner.bodies.begin_partial("a" * 64, 1)
        with handle:
            handle.write(b"partial")
        with pytest.raises(
            LocalhostTrialStopped, match="orphan_or_partial_body_present"
        ):
            runner.run_one()
        path.unlink()  # only this artificial fixture file
        assert runner.run_one().completed
        body = next((tmp_path / plan.artifact_id / "bodies").glob("*.json"))
        body.write_bytes(b"tampered")  # only this artificial fixture file
        with pytest.raises(Exception, match="saved_body_missing_or_size_mismatch"):
            resume(tmp_path, plan, transport, clock)


def test_recovery_reclaims_partial_then_explicit_resume_without_auto_send(tmp_path):
    clock = Clock()
    plan = fixed()
    response = payload([{"Date": "2025-03-03", "HolDiv": "1"}])
    with localhost([(200, {}, response)], clock) as (transport, calls):
        runner = create(tmp_path, plan, transport, clock)
        token = runner.account.reserve_slot(
            plan,
            claim(plan),
            at=clock.now(),
            holder_id="crashed-holder",
            inventory=runner.bodies.inventory,
        )
        partial, handle = runner.bodies.begin_partial(
            token.attempt_id, token.generation
        )
        with handle:
            handle.write(b"partial-evidence")
        recovery = LocalhostRecoverySession.open(
            tmp_path / plan.artifact_id,
            tmp_path / "account-rate.sqlite3",
            plan,
            claim(plan),
            clock=clock.now,
        )
        inspected = recovery.inspect_open_slot()
        assert inspected is not None and inspected.state == "reserved"
        assert inspected.attempt_id == token.attempt_id
        assert [item.object_id for item in recovery.inspect_uncommitted()] == [
            partial.name
        ]
        assert len(calls) == 0
        with pytest.raises(
            StorageError, match="open_slot_reclaim_required_before_quarantine"
        ):
            recovery.quarantine_uncommitted(
                partial.name, reason="cannot_bypass_open_slot"
            )
        assert partial.read_bytes() == b"partial-evidence"
        clock.tick(policy().slot_lease_seconds - 1)
        with pytest.raises(
            HttpContractError, match="account_slot_reclaim_before_lease_expiry"
        ):
            recovery.reclaim_expired_slot(inspected, new_holder_id="recovery-holder")
        clock.tick(1)
        recovery.reclaim_expired_slot(inspected, new_holder_id="recovery-holder")
        assert recovery.inspect_open_slot() is None
        record = recovery.quarantine_uncommitted(
            partial.name, reason="crash_partial_reviewed"
        )
        assert record.state == "partial"
        assert record.original_path == f"bodies/{partial.name}"
        assert record.size == len(b"partial-evidence")
        assert record.body_sha256 == hashlib.sha256(b"partial-evidence").hexdigest()
        assert record.quarantined_at == clock.now()
        assert recovery.quarantine_records() == (record,)
        assert recovery.inspect_uncommitted() == ()
        assert not partial.exists()
        quarantined = tmp_path / plan.artifact_id / record.quarantine_path
        assert quarantined.read_bytes() == b"partial-evidence"
        with pytest.raises(RawBodyStorageError, match="partial_path_invalid"):
            runner.bodies.publish_partial(
                quarantined,
                attempt_id=token.attempt_id,
                expected_sha256=record.body_sha256,
                expected_size=record.size,
            )
        assert len(calls) == 0
        reopened = resume(tmp_path, plan, transport, clock)
        with pytest.raises(LocalhostTrialStopped, match="rate_limit_wait"):
            reopened.run_one()
        clock.tick(120)
        assert reopened.run_one().completed
        assert len(calls) == 1
        assert recovery.quarantine_records() == (record,)


def test_recovery_session_cannot_expose_success_or_body_capabilities(tmp_path):
    """A sent slot's inspection must not grant normal execution authority."""

    clock = Clock()
    plan = fixed()
    with localhost([], clock) as (transport, calls):
        runner = create(tmp_path, plan, transport, clock)
        token = runner.account.reserve_slot(
            plan,
            claim(plan),
            at=clock.now(),
            holder_id="crashed-holder",
            inventory=runner.bodies.inventory,
        )
        runner.account.mark_sent(
            token,
            plan,
            claim(plan),
            at=clock.now(),
            inventory=runner.bodies.inventory,
        )
        recovery = LocalhostRecoverySession.open(
            tmp_path / plan.artifact_id,
            tmp_path / "account-rate.sqlite3",
            plan,
            claim(plan),
            clock=clock.now,
        )
        inspected = recovery.inspect_open_slot()
        assert inspected is not None and inspected.state == "sent"
        assert not hasattr(inspected, "token")
        assert not hasattr(recovery, "account")
        assert not hasattr(recovery, "bodies")
        assert not hasattr(recovery, "__dict__")
        for operation in (
            "reserve_slot",
            "mark_sent",
            "settle_response",
            "settle_unknown",
            "commit_body",
            "begin_partial",
            "publish_partial",
            "run_one",
        ):
            assert not hasattr(recovery, operation)
        assert len(calls) == 0
        assert runner.account.load().head_hash == inspected.account_head
        assert runner.account.load_journal(plan, claim(plan)).head_hash == (
            inspected.plan_head
        )
        assert runner.account.body_commits() == ()


def test_orphan_quarantine_preserves_receipt_and_blocks_tampering(tmp_path):
    clock = Clock()
    plan = fixed()
    with localhost([], clock) as (transport, calls):
        runner = create(tmp_path, plan, transport, clock)
        attempt_id = "a" * 64
        partial, handle = runner.bodies.begin_partial(attempt_id, 1)
        orphan_bytes = b'{"data":[]}'
        with handle:
            handle.write(orphan_bytes)
        orphan = runner.bodies.publish_partial(
            partial,
            attempt_id=attempt_id,
            expected_sha256=hashlib.sha256(orphan_bytes).hexdigest(),
            expected_size=len(orphan_bytes),
        )
        with pytest.raises(
            LocalhostTrialStopped, match="orphan_or_partial_body_present"
        ):
            runner.run_one()
        recovery = LocalhostRecoverySession.open(
            tmp_path / plan.artifact_id,
            tmp_path / "account-rate.sqlite3",
            plan,
            claim(plan),
            clock=clock.now,
        )
        assert [item.object_id for item in recovery.inspect_uncommitted()] == [
            orphan.name
        ]
        record = recovery.quarantine_uncommitted(orphan.name, reason="orphan_reviewed")
        assert record.state == "orphan"
        assert record.size == len(orphan_bytes)
        assert record.body_sha256 == hashlib.sha256(orphan_bytes).hexdigest()
        assert recovery.quarantine_records() == (record,)
        assert recovery.inspect_uncommitted() == ()
        assert not orphan.exists()
        assert len(calls) == 0
        with pytest.raises(RawBodyStorageError, match="committed_body_missing"):
            runner.bodies.read_final(attempt_id, max_bytes=100)
        quarantined = tmp_path / plan.artifact_id / record.quarantine_path
        quarantined.write_bytes(b"tampered artificial quarantine")
        with pytest.raises(RawBodyStorageError, match="quarantined_body_changed"):
            LocalhostRecoverySession.open(
                tmp_path / plan.artifact_id,
                tmp_path / "account-rate.sqlite3",
                plan,
                claim(plan),
                clock=clock.now,
            )


def test_recovery_refuses_to_quarantine_committed_body(tmp_path):
    clock = Clock()
    plan = fixed()
    response = payload([{"Date": "2025-03-03", "HolDiv": "1"}])
    with localhost([(200, {}, response)], clock) as (transport, calls):
        runner = create(tmp_path, plan, transport, clock)
        assert runner.run_one().completed
        committed = runner.account.body_commits()[0]
        recovery = LocalhostRecoverySession.open(
            tmp_path / plan.artifact_id,
            tmp_path / "account-rate.sqlite3",
            plan,
            claim(plan),
            clock=clock.now,
        )
        with pytest.raises(
            RawBodyStorageError, match="committed_body_cannot_be_quarantined"
        ):
            recovery.quarantine_uncommitted(
                committed.object_id, reason="must_not_promote_or_remove"
            )
        assert (
            runner.bodies.read_final(
                committed.attempt_id, max_bytes=plan.limits.max_page_saved_bytes
            )
            == response
        )
        assert len(calls) == 1
