"""Artificial-loopback orchestration for the historical feasibility contract.

The only transport accepted here is ``LocalhostHttpTransport``. This module
does not authorize J-Quants traffic, authenticate an owner/account, or open
the deliberately closed live-acquisition gate.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .acquisition import REQUIRED_FIELDS, ROW_KEYS, AcquisitionStopped, _validate_row
from .http_bodies import QuarantineRecord, RawBodyStorageError, RawBodyStore
from .http_contract import (
    AccountRatePolicy,
    BodyFileEvidence,
    EvidenceJournal,
    HttpAcquisitionPlan,
    HttpContractError,
    JournalEvent,
    OwnerApprovalClaim,
    PageRequest,
    budget_snapshot,
    check_approval_scope,
)
from .http_storage import (
    AccountLedgerStore,
    OpenSlotInspection,
    StorageError,
)
from .http_transport import (
    HttpTransportError,
    LocalhostHttpTransport,
)


class LocalhostTrialStopped(RuntimeError):
    """The artificial exercise cannot safely proceed without new evidence."""

    def __init__(self, reason: str, *, terminal: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.terminal = terminal


@dataclass(frozen=True)
class ArtificialAttemptResult:
    attempt_id: str | None
    status_code: int | None
    state: str
    completed: bool
    plan_head: str
    account_head: str


class LocalhostRecoverySession:
    """Explicit, network-free maintenance for an interrupted artificial run."""

    def __init__(
        self,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
        account: AccountLedgerStore,
        bodies: RawBodyStore,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if (
            type(plan) is not HttpAcquisitionPlan
            or type(approval) is not OwnerApprovalClaim
            or type(account) is not AccountLedgerStore
            or type(bodies) is not RawBodyStore
            or account.account_ref != plan.account_ref
            or bodies.plan_sha256 != plan.sha256
            or bodies.approval_sha256 != approval.sha256
            or not callable(clock)
        ):
            raise LocalhostTrialStopped("recovery_binding_invalid", terminal=True)
        self.plan = plan
        self.approval = approval
        self.account = account
        self.bodies = bodies
        self.clock = clock

    @classmethod
    def open(
        cls,
        root: str | Path,
        account_db: str | Path,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
        *,
        clock: Callable[[], datetime],
    ) -> LocalhostRecoverySession:
        """Open unresolved evidence without silently deciding it is usable."""

        if (
            type(plan) is not HttpAcquisitionPlan
            or type(approval) is not OwnerApprovalClaim
            or not callable(clock)
        ):
            raise LocalhostTrialStopped("recovery_binding_invalid", terminal=True)
        account = AccountLedgerStore(account_db, plan.account_ref)
        account.load()
        account.load_journal(plan, approval)
        bodies = RawBodyStore.open(
            root, plan_sha256=plan.sha256, approval_sha256=approval.sha256
        )
        return cls(plan, approval, account, bodies, clock=clock)

    def _now(self) -> datetime:
        return LocalhostAcquisitionRunner._read_clock(self.clock)

    def inspect_open_slot(self) -> OpenSlotInspection | None:
        return self.account.inspect_open_slot(self.plan, self.approval)

    def inspect_uncommitted(self) -> tuple[BodyFileEvidence, ...]:
        journal = self.account.load_journal(self.plan, self.approval)
        return self.bodies.uncommitted_files(journal)

    def reclaim_expired_slot(
        self, inspection: OpenSlotInspection, *, new_holder_id: str
    ) -> None:
        self.account.reclaim_open_slot(
            inspection,
            self.plan,
            self.approval,
            at=self._now(),
            new_holder_id=new_holder_id,
        )

    def quarantine_uncommitted(
        self, object_id: str, *, reason: str
    ) -> QuarantineRecord:
        return self.account.quarantine_uncommitted_body(
            self.plan,
            self.approval,
            self.bodies,
            object_id,
            reason=reason,
            at=self._now(),
        )

    def quarantine_records(self) -> tuple[QuarantineRecord, ...]:
        return self.bodies.quarantine_records()


class LocalhostAcquisitionRunner:
    """One attempt per call; waits/resume are explicit, never silently slept away.

    An injected UTC clock is used for synthetic journal events. Network
    duration and timeout enforcement in the transport use a monotonic clock.
    The two are kept distinct; no synthetic time is evidence of market timing.
    """

    def __init__(
        self,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
        policy: AccountRatePolicy,
        transport: LocalhostHttpTransport,
        account: AccountLedgerStore,
        bodies: RawBodyStore,
        *,
        clock: Callable[[], datetime],
        holder_id: str,
        related: tuple[tuple[HttpAcquisitionPlan, OwnerApprovalClaim], ...] = (),
    ) -> None:
        if (
            type(plan) is not HttpAcquisitionPlan
            or type(approval) is not OwnerApprovalClaim
            or type(policy) is not AccountRatePolicy
            or type(transport) is not LocalhostHttpTransport
            or type(account) is not AccountLedgerStore
            or type(bodies) is not RawBodyStore
        ):
            raise LocalhostTrialStopped("artificial_components_required", terminal=True)
        if not callable(clock) or type(holder_id) is not str or not holder_id:
            raise LocalhostTrialStopped("clock_or_holder_required", terminal=True)
        if type(related) is not tuple or any(
            type(pair) is not tuple
            or len(pair) != 2
            or type(pair[0]) is not HttpAcquisitionPlan
            or type(pair[1]) is not OwnerApprovalClaim
            for pair in related
        ):
            raise LocalhostTrialStopped("related_plan_inputs_invalid", terminal=True)
        if account.account_ref != plan.account_ref or bodies.plan_sha256 != plan.sha256:
            raise LocalhostTrialStopped("artificial_binding_mismatch", terminal=True)
        if bodies.approval_sha256 != approval.sha256 or not policy.covers(plan.retry):
            raise LocalhostTrialStopped("artificial_binding_mismatch", terminal=True)
        self.plan = plan
        self.approval = approval
        self.policy = policy
        self.transport = transport
        self.account = account
        self.bodies = bodies
        self.clock = clock
        self.holder_id = holder_id
        self.related = related

    @classmethod
    def create(
        cls,
        root: str | Path,
        account_db: str | Path,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
        policy: AccountRatePolicy,
        transport: LocalhostHttpTransport,
        *,
        clock: Callable[[], datetime],
        holder_id: str,
        related: tuple[tuple[HttpAcquisitionPlan, OwnerApprovalClaim], ...] = (),
    ) -> LocalhostAcquisitionRunner:
        if type(transport) is not LocalhostHttpTransport:
            raise LocalhostTrialStopped(
                "only_localhost_transport_allowed", terminal=True
            )
        now = cls._read_clock(clock)
        check_approval_scope(plan, approval, now=now)
        account = AccountLedgerStore(account_db, plan.account_ref)
        # Make validation of the account path and artificial scope precede
        # creation of any body root or local journal.
        bodies = RawBodyStore.create(
            root, plan_sha256=plan.sha256, approval_sha256=approval.sha256
        )
        account.initialize(policy, at=now)
        account.register_plan(plan, approval)
        return cls(
            plan,
            approval,
            policy,
            transport,
            account,
            bodies,
            clock=clock,
            holder_id=holder_id,
            related=related,
        )

    @classmethod
    def open(
        cls,
        root: str | Path,
        account_db: str | Path,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
        policy: AccountRatePolicy,
        transport: LocalhostHttpTransport,
        *,
        clock: Callable[[], datetime],
        holder_id: str,
        related: tuple[tuple[HttpAcquisitionPlan, OwnerApprovalClaim], ...] = (),
    ) -> LocalhostAcquisitionRunner:
        if type(transport) is not LocalhostHttpTransport:
            raise LocalhostTrialStopped(
                "only_localhost_transport_allowed", terminal=True
            )
        account = AccountLedgerStore(account_db, plan.account_ref)
        account.load()  # never recreate or truncate an existing ledger on resume
        bodies = RawBodyStore.open(
            root, plan_sha256=plan.sha256, approval_sha256=approval.sha256
        )
        runner = cls(
            plan,
            approval,
            policy,
            transport,
            account,
            bodies,
            clock=clock,
            holder_id=holder_id,
            related=related,
        )
        journal = account.load_journal(plan, approval)
        budget_snapshot(journal, bodies.inventory(journal), now=runner._now())
        return runner

    @staticmethod
    def _read_clock(clock: Callable[[], datetime]) -> datetime:
        if not callable(clock):
            raise LocalhostTrialStopped("utc_clock_required", terminal=True)
        value = clock()
        if type(value) is not datetime or value.tzinfo is None:
            raise LocalhostTrialStopped("utc_clock_required", terminal=True)
        return value.astimezone(UTC)

    def _now(self) -> datetime:
        return self._read_clock(self.clock)

    def _result(
        self, attempt_id: str | None, status_code: int | None, state: str
    ) -> ArtificialAttemptResult:
        journal = self.account.load_journal(self.plan, self.approval)
        ledger = self.account.load()
        return ArtificialAttemptResult(
            attempt_id,
            status_code,
            state,
            journal.completed,
            journal.head_hash,
            ledger.head_hash,
        )

    def _finish_if_ready(self, journal: EvidenceJournal) -> bool:
        inventory = self.bodies.inventory(journal)
        budget_snapshot(journal, inventory, now=self._now())
        if journal.completed:
            return True
        if journal._state.complete_prefix != len(self.plan.queries):
            return False
        if any(item.state != "committed" for item in inventory.files):
            raise LocalhostTrialStopped("orphan_or_partial_body_present", terminal=True)
        self.account.append_plan_event(
            self.plan,
            self.approval,
            JournalEvent("run_completed", self._now()),
            expected_head=journal.head_hash,
        )
        return True

    def _response_time(self, sent_at: datetime, elapsed_seconds: float) -> datetime:
        return max(self._now(), sent_at + timedelta(seconds=elapsed_seconds))

    def _effective_wait(self, status: int | None, observed: int | None) -> int:
        retry = self.plan.retry
        if status is None:
            outcome = "unknown"
            plan_wait = max(
                retry.min_interval_seconds,
                retry.min_wait_after_network_error_seconds,
                retry.min_wait_after_429_seconds,
            )
        elif status == 429:
            outcome = "429"
            plan_wait = max(
                retry.min_interval_seconds, retry.min_wait_after_429_seconds
            )
        elif 500 <= status < 600:
            outcome = "5xx"
            plan_wait = max(
                retry.min_interval_seconds, retry.min_wait_after_5xx_seconds
            )
        else:
            outcome = "response"
            plan_wait = retry.min_interval_seconds
        return max(self.policy.wait_after(outcome), plan_wait, observed or 0)

    def _stop(self, reason: str, *, at: datetime) -> None:
        journal = self.account.load_journal(self.plan, self.approval)
        if not journal._state.terminal_stop and not journal.completed:
            self.account.append_plan_event(
                self.plan,
                self.approval,
                JournalEvent("run_stopped", at, reason=reason, terminal=True),
                expected_head=journal.head_hash,
            )

    def _existing_row_keys(
        self, journal: EvidenceJournal, page: PageRequest
    ) -> set[tuple]:
        keys: set[tuple] = set()
        for old_page, _, digest in journal._state.pages.get(page.query.query_id, ()):
            attempts = [
                (attempt_id, attempt)
                for attempt_id, attempt in journal._state.attempts.items()
                if attempt.page == old_page and attempt.body_sha256 == digest
            ]
            if len(attempts) != 1:
                raise LocalhostTrialStopped(
                    "completed_body_identity_invalid", terminal=True
                )
            body = self.bodies.read_final(
                attempts[0][0], max_bytes=self.plan.limits.max_page_saved_bytes
            )
            payload = self._parse_payload(old_page, body, keys)
            if payload.get("pagination_key") is None:
                raise LocalhostTrialStopped(
                    "completed_page_chain_invalid", terminal=True
                )
        return keys

    @staticmethod
    def _parse_payload(page: PageRequest, body: bytes, seen: set[tuple]) -> dict:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise LocalhostTrialStopped(
                "invalid_json_response", terminal=True
            ) from None
        if type(payload) is not dict or type(payload.get("data")) is not list:
            raise LocalhostTrialStopped("unsupported_response_schema", terminal=True)
        key = payload.get("pagination_key")
        if key is not None and (type(key) is not str or not key):
            raise LocalhostTrialStopped("invalid_pagination_key", terminal=True)
        if key is not None and not payload["data"]:
            raise LocalhostTrialStopped("empty_page_with_continuation", terminal=True)
        for row in payload["data"]:
            if type(row) is not dict or not REQUIRED_FIELDS[page.query.endpoint] <= set(
                row
            ):
                raise LocalhostTrialStopped(
                    "unsupported_response_schema", terminal=True
                )
            try:
                _validate_row(page.query, row)
            except AcquisitionStopped as error:
                raise LocalhostTrialStopped(error.reason, terminal=True) from None
            row_key = tuple(row[field] for field in ROW_KEYS[page.query.endpoint])
            if row_key in seen:
                raise LocalhostTrialStopped("duplicate_row", terminal=True)
            seen.add(row_key)
        return payload

    def run_one(self) -> ArtificialAttemptResult:
        """Execute at most one artificial loopback HTTP attempt."""

        journal = self.account.load_journal(self.plan, self.approval)
        if self._finish_if_ready(journal):
            return self._result(None, None, "completed")
        snapshot = budget_snapshot(
            journal, self.bodies.inventory(journal), now=self._now()
        )
        if not snapshot.plan_allows_next_attempt:
            raise LocalhostTrialStopped(snapshot.blocking_reasons[0])
        token = self.account.reserve_slot(
            self.plan,
            self.approval,
            at=self._now(),
            holder_id=self.holder_id,
            inventory=self.bodies.inventory,
            related=self.related,
        )
        send_at = self._now()
        try:
            total_timeout = self.account.mark_sent(
                token,
                self.plan,
                self.approval,
                at=send_at,
                inventory=self.bodies.inventory,
                related=self.related,
            )
            self.account.assert_current(token)
            partial, handle = self.bodies.begin_partial(
                token.attempt_id, token.generation
            )
            # Opening and fsyncing the staged file can take time. A fresh
            # clock read prevents a synthetic time jump from starting HTTP
            # after the send window validated under SQLite has elapsed.
            elapsed_after_send_record = (self._now() - send_at).total_seconds()
            if elapsed_after_send_record < 0:
                handle.close()
                raise LocalhostTrialStopped("send_clock_regressed", terminal=True)
            remaining_timeout = total_timeout - elapsed_after_send_record
            if remaining_timeout <= 0:
                handle.close()
                raise LocalhostTrialStopped("deadline_before_http_send", terminal=True)
            self.account.assert_current(token)
        except (
            HttpContractError,
            LocalhostTrialStopped,
            RawBodyStorageError,
        ) as error:
            # The reservation may have reached the durable account ledger;
            # never forget it or silently release the slot.
            self.account.settle_unknown(
                token,
                self.plan,
                self.approval,
                at=max(self._now(), send_at),
                effective_wait_seconds=self._effective_wait(None, None),
                hold_reason="setup_after_reservation_failed",
            )
            raise LocalhostTrialStopped(str(error), terminal=True) from None
        try:
            with handle:
                observation = self.transport.fetch(
                    self.plan,
                    token.page,
                    sink=handle,
                    allowed_transfer_bytes=token.allowed_transfer_bytes,
                    allowed_decoded_bytes=token.allowed_decoded_bytes,
                    allowed_saved_bytes=token.allowed_saved_bytes,
                    connect_timeout_seconds=min(5.0, remaining_timeout),
                    read_timeout_seconds=min(5.0, remaining_timeout),
                    total_timeout_seconds=remaining_timeout,
                    now=self.clock,
                )
                handle.flush()
                os.fsync(handle.fileno())
        except HttpTransportError as error:
            at = self._response_time(send_at, error.elapsed_seconds)
            partial_size = partial.stat().st_size
            # Headers can be observed while the body is incomplete. The
            # immutable contract cannot represent "known status, incomplete
            # response"; never claim response_received from those headers.
            # If a status or Retry-After was observed, quarantine the shared
            # account so another plan cannot shorten an unrepresentable wait.
            hold = (
                "incomplete_http_response"
                if error.status_code is not None or partial_size > 0
                else None
            )
            self.account.settle_unknown(
                token,
                self.plan,
                self.approval,
                at=at,
                effective_wait_seconds=self._effective_wait(None, None),
                hold_reason=hold,
            )
            if hold is None and partial_size == 0:
                self.bodies.discard_partial(partial)
                return self._result(token.attempt_id, None, "unknown")
            self._stop("http_attempt_incomplete", at=at)
            raise LocalhostTrialStopped(error.reason, terminal=True) from None
        at = self._response_time(send_at, observation.elapsed_seconds)
        hold = "retry_after_unrepresentable" if observation.retry_after_issue else None
        try:
            self.account.settle_response(
                token,
                self.plan,
                self.approval,
                at=at,
                status=observation.status_code,
                transfer_bytes=observation.transfer_bytes,
                decoded_bytes=observation.decompressed_bytes,
                observed_retry_after_seconds=observation.observed_retry_after_seconds,
                effective_wait_seconds=self._effective_wait(
                    observation.status_code, observation.observed_retry_after_seconds
                ),
                hold_reason=hold,
            )
        except (StorageError, HttpContractError):
            self.account.settle_unknown(
                token,
                self.plan,
                self.approval,
                at=at,
                effective_wait_seconds=self._effective_wait(None, None),
                hold_reason="late_or_unrepresentable_http_response",
            )
            raise LocalhostTrialStopped(
                "response_cannot_be_recorded", terminal=True
            ) from None
        if hold is not None:
            self._stop(hold, at=at)
            raise LocalhostTrialStopped(hold, terminal=True)
        if observation.status_code != 200:
            self.bodies.discard_partial(partial)
            if observation.status_code not in (429, 500, 502, 503, 504):
                self._stop("nonretryable_http_status", at=at)
            return self._result(
                token.attempt_id, observation.status_code, "http_response"
            )
        try:
            body = self.bodies.read_partial(
                partial, max_bytes=token.allowed_saved_bytes
            )
            self.bodies.verify_partial(
                partial,
                expected_sha256=observation.body_sha256,
                expected_size=observation.persisted_bytes,
            )
            # The frozen BodyInventory contract permits one physical file per
            # digest. Until the storage contract supports content-addressed
            # sharing, refuse a second identical body *before* page commit;
            # otherwise resume would fail after a seemingly successful page.
            current_journal = self.account.load_journal(self.plan, self.approval)
            if observation.body_sha256 in current_journal._state.saved_bodies:
                raise LocalhostTrialStopped(
                    "duplicate_body_digest_unsupported", terminal=True
                )
            seen = self._existing_row_keys(current_journal, token.page)
            payload = self._parse_payload(token.page, body, seen)
            next_key = payload.get("pagination_key")
            self.account.commit_body(
                token,
                self.plan,
                self.approval,
                at=max(self._now(), at),
                object_id=f"{token.attempt_id}.json",
                body_sha256=observation.body_sha256,
                saved_bytes=observation.persisted_bytes,
                next_key=next_key,
                finalize=lambda: self.bodies.publish_partial(
                    partial,
                    attempt_id=token.attempt_id,
                    expected_sha256=observation.body_sha256,
                    expected_size=observation.persisted_bytes,
                ),
            )
        except (
            LocalhostTrialStopped,
            RawBodyStorageError,
            StorageError,
            HttpContractError,
        ) as error:
            self.account.account_hold("body_or_schema_commit_failed")
            self._stop("body_or_schema_commit_failed", at=max(self._now(), at))
            raise LocalhostTrialStopped(str(error), terminal=True) from None
        journal = self.account.load_journal(self.plan, self.approval)
        self._finish_if_ready(journal)
        return self._result(token.attempt_id, observation.status_code, "page_completed")
