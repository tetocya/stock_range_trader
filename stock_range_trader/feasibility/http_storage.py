"""Atomic local evidence store for artificial historical HTTP exercises.

The store serializes account and plan journals in one SQLite transaction. It is
not an authorization source: owner authenticity, real account identity, and the
live acquisition gate remain outside this module and unverified.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from .http_contract import (
    AccountRateEvent,
    AccountRateLedger,
    AccountRatePolicy,
    BodyInventory,
    EvidenceJournal,
    HttpAcquisitionPlan,
    HttpContractError,
    JournalEvent,
    OwnerApprovalClaim,
    PageRequest,
    assess_account_slot,
    budget_snapshot,
    check_approval_scope,
    commit_account_ledger,
    make_attempt_id,
    reconcile_account_and_plan,
)
from .paths import UnsafeOutputPath, require_existing_store_dir

_T = TypeVar("_T")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.sqlite3")
_OBJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class StorageError(HttpContractError):
    """Storage is unavailable, stale, corrupt, or not safely writable."""


@dataclass(frozen=True)
class SlotToken:
    """A local fencing token, not a network-send authorization."""

    account_ref: str
    plan_sha256: str
    attempt_id: str
    holder_id: str
    generation: int
    page: PageRequest
    allowed_transfer_bytes: int
    allowed_decoded_bytes: int
    allowed_saved_bytes: int


@dataclass(frozen=True)
class BodyCommit:
    """A persisted link from a fenced slot to an externally verified body."""

    plan_sha256: str
    attempt_id: str
    generation: int
    object_id: str
    body_sha256: str
    saved_bytes: int


class AccountLedgerStore:
    """SQLite-backed account+plan journals, scoped to a trusted output tree.

    ``BEGIN IMMEDIATE`` serializes cross-process writers. Every operation
    reopens and verifies the full hash chain. The database is the authoritative
    local evidence; the hash chain is not an externally signed receipt.
    """

    def __init__(self, path: str | Path, account_ref: str) -> None:
        raw = Path(path)
        if not raw.is_absolute() or ".." in raw.parts or not _NAME.fullmatch(raw.name):
            raise StorageError("unsafe_account_store_path")
        try:
            parent = require_existing_store_dir(raw.parent)
        except UnsafeOutputPath as error:
            raise StorageError(f"unsafe_account_store_path:{error}") from None
        self.path = parent / raw.name
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise StorageError("account_store_not_regular_file")
        if type(account_ref) is not str or not account_ref:
            raise StorageError("account_ref_required")
        self.account_ref = account_ref

    def _connection(self) -> sqlite3.Connection:
        if self.path.is_symlink() or not self.path.is_file():
            raise StorageError("account_store_not_regular_file")
        try:
            db = sqlite3.connect(
                self.path.as_uri() + "?mode=rw",
                uri=True,
                timeout=10,
                isolation_level=None,
            )
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except sqlite3.Error as error:
            raise StorageError(
                f"account_store_open_failed:{type(error).__name__}"
            ) from None

    @contextmanager
    def _transaction(self):
        db = self._connection()
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StorageError("account_store_corrupt")
            yield db
            db.commit()
        except sqlite3.Error as error:
            db.rollback()
            raise StorageError(
                f"account_store_sqlite_error:{type(error).__name__}"
            ) from None
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(
        self, policy: AccountRatePolicy, *, at: datetime
    ) -> AccountRateLedger:
        """Create the database exclusively, or verify an existing same-policy one."""

        if type(policy) is not AccountRatePolicy:
            raise StorageError("account_policy_required")
        if not self.path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.path, flags, 0o600)
            except FileExistsError:
                pass  # another process created it; verify it below
            else:
                os.close(descriptor)
        with self._transaction() as db:
            schema = """
                CREATE TABLE IF NOT EXISTS account_ledgers (
                    account_ref TEXT PRIMARY KEY,
                    data BLOB NOT NULL,
                    head TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    active_slot TEXT,
                    active_holder TEXT,
                    active_generation INTEGER,
                    hold_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS plan_journals (
                    account_ref TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL,
                    plan_json BLOB NOT NULL,
                    approval_json BLOB NOT NULL,
                    data BLOB NOT NULL,
                    head TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    PRIMARY KEY (account_ref, plan_sha256),
                    FOREIGN KEY (account_ref) REFERENCES account_ledgers(account_ref)
                );
                CREATE TABLE IF NOT EXISTS body_commits (
                    account_ref TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    object_id TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    saved_bytes INTEGER NOT NULL,
                    PRIMARY KEY (account_ref, object_id)
                );
                """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            row = db.execute(
                "SELECT * FROM account_ledgers WHERE account_ref=?", (self.account_ref,)
            ).fetchone()
            if row is None:
                ledger = AccountRateLedger(self.account_ref).append(
                    AccountRateEvent("ledger_opened", at, policy=policy)
                )
                db.execute(
                    "INSERT INTO account_ledgers VALUES (?, ?, ?, ?, 0, NULL, NULL, NULL, NULL)",
                    (
                        self.account_ref,
                        ledger.data,
                        ledger.head_hash,
                        ledger.event_count,
                    ),
                )
            else:
                ledger = self._read_account(db)[0]
                if ledger.policy != policy:
                    raise StorageError("account_policy_mismatch")
        return ledger

    def _read_account(
        self, db: sqlite3.Connection
    ) -> tuple[AccountRateLedger, sqlite3.Row]:
        row = db.execute(
            "SELECT * FROM account_ledgers WHERE account_ref=?", (self.account_ref,)
        ).fetchone()
        if row is None:
            raise StorageError("account_ledger_not_initialized")
        try:
            ledger = AccountRateLedger(self.account_ref, bytes(row["data"]))
        except (HttpContractError, TypeError, ValueError):
            raise StorageError("account_ledger_corrupt") from None
        if ledger.head_hash != row["head"] or ledger.event_count != row["version"]:
            raise StorageError("account_ledger_metadata_mismatch")
        state = ledger._state
        active = state.open_slot
        if active is None:
            if any(
                row[k] is not None
                for k in ("active_slot", "active_holder", "active_generation")
            ):
                raise StorageError("account_fence_metadata_mismatch")
        elif (
            row["active_slot"] != active
            or row["active_holder"] != state.slots[active].holder_id
            or row["active_generation"] != row["generation"]
        ):
            raise StorageError("account_fence_metadata_mismatch")
        if type(row["generation"]) is not int or row["generation"] < 0:
            raise StorageError("account_generation_invalid")
        hold = row["hold_reason"]
        if hold is not None and (
            type(hold) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", hold) is None
        ):
            raise StorageError("account_hold_reason_invalid")
        return ledger, row

    def load(self) -> AccountRateLedger:
        """Re-read and verify persisted account evidence after process restart."""

        with self._transaction() as db:
            return self._read_account(db)[0]

    def hold_reason(self) -> str | None:
        """Read durable quarantine state; it is never a send permission."""

        with self._transaction() as db:
            return self._read_account(db)[1]["hold_reason"]

    @staticmethod
    def _check_hold_reason(reason: str) -> None:
        if (
            type(reason) is not str
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason) is None
        ):
            raise StorageError("account_hold_reason_invalid")

    def _hold_in_transaction(self, db: sqlite3.Connection, reason: str) -> None:
        self._check_hold_reason(reason)
        current = self._read_account(db)[1]["hold_reason"]
        if current is not None and current != reason:
            raise StorageError("account_already_held_for_different_reason")
        db.execute(
            "UPDATE account_ledgers SET hold_reason=? WHERE account_ref=?",
            (reason, self.account_ref),
        )

    def account_hold(self, reason: str) -> None:
        """Fail closed across plans/restarts; no automatic release is provided."""

        with self._transaction() as db:
            self._hold_in_transaction(db, reason)

    @staticmethod
    def _json(value: object) -> bytes:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    def register_plan(
        self, plan: HttpAcquisitionPlan, approval: OwnerApprovalClaim
    ) -> EvidenceJournal:
        """Register immutable plan/claim content, never an authenticity claim."""

        if (
            type(plan) is not HttpAcquisitionPlan
            or plan.account_ref != self.account_ref
        ):
            raise StorageError("plan_account_mismatch")
        journal = EvidenceJournal(plan, approval=approval)
        plan_blob = self._json(plan.to_dict())
        approval_blob = self._json(approval.to_dict())
        with self._transaction() as db:
            self._read_account(db)
            row = db.execute(
                "SELECT * FROM plan_journals WHERE account_ref=? AND plan_sha256=?",
                (self.account_ref, plan.sha256),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO plan_journals VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.account_ref,
                        plan.sha256,
                        plan_blob,
                        approval_blob,
                        journal.data,
                        journal.head_hash,
                        journal.event_count,
                    ),
                )
            else:
                journal = self._read_plan(db, plan, approval)
        return journal

    def _read_plan_row(
        self,
        row: sqlite3.Row,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
    ) -> EvidenceJournal:
        if (
            row["account_ref"] != self.account_ref
            or row["plan_sha256"] != plan.sha256
            or bytes(row["plan_json"]) != self._json(plan.to_dict())
            or bytes(row["approval_json"]) != self._json(approval.to_dict())
        ):
            raise StorageError("plan_or_approval_content_mismatch")
        try:
            journal = EvidenceJournal(plan, bytes(row["data"]), approval=approval)
        except (HttpContractError, TypeError, ValueError):
            raise StorageError("plan_journal_corrupt") from None
        if journal.head_hash != row["head"] or journal.event_count != row["version"]:
            raise StorageError("plan_journal_metadata_mismatch")
        return journal

    def _read_plan(
        self,
        db: sqlite3.Connection,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
    ) -> EvidenceJournal:
        row = db.execute(
            "SELECT * FROM plan_journals WHERE account_ref=? AND plan_sha256=?",
            (self.account_ref, plan.sha256),
        ).fetchone()
        if row is None:
            raise StorageError("plan_journal_not_registered")
        journal = self._read_plan_row(row, plan, approval)
        self._check_body_links(db, journal)
        return journal

    def _check_body_links(
        self, db: sqlite3.Connection, journal: EvidenceJournal
    ) -> None:
        expected = {
            attempt_id: (
                attempt.body_sha256,
                journal._state.saved_bodies[attempt.body_sha256],
            )
            for attempt_id, attempt in journal._state.attempts.items()
            if attempt.state == "page_completed"
        }
        rows = db.execute(
            "SELECT attempt_id, object_id, body_sha256, saved_bytes "
            "FROM body_commits WHERE account_ref=? AND plan_sha256=?",
            (self.account_ref, journal.plan.sha256),
        ).fetchall()
        if len(rows) != len(expected) or {row["attempt_id"] for row in rows} != set(
            expected
        ):
            raise StorageError("body_commit_journal_links_mismatch")
        for row in rows:
            if (
                (row["body_sha256"], row["saved_bytes"]) != expected[row["attempt_id"]]
                or type(row["object_id"]) is not str
                or _OBJECT_ID.fullmatch(row["object_id"]) is None
                or row["object_id"] != f"{row['attempt_id']}.json"
            ):
                raise StorageError("body_commit_journal_links_mismatch")

    def load_journal(
        self, plan: HttpAcquisitionPlan, approval: OwnerApprovalClaim
    ) -> EvidenceJournal:
        with self._transaction() as db:
            return self._read_plan(db, plan, approval)

    def body_commits(self) -> tuple[BodyCommit, ...]:
        """Return links, not proof that the referenced files still exist."""

        with self._transaction() as db:
            ledger, account_row = self._read_account(db)
            rows = db.execute(
                "SELECT plan_sha256, attempt_id, generation, object_id, "
                "body_sha256, saved_bytes FROM body_commits "
                "WHERE account_ref=? ORDER BY object_id",
                (self.account_ref,),
            ).fetchall()
            result = []
            for row in rows:
                item = BodyCommit(*row)
                slot = ledger._state.slots.get(item.attempt_id)
                if (
                    slot is None
                    or slot.plan_sha256 != item.plan_sha256
                    or slot.outcome != "response"
                    or type(item.generation) is not int
                    or not 0 < item.generation <= account_row["generation"]
                    or type(item.saved_bytes) is not int
                    or item.saved_bytes <= 0
                    or type(item.object_id) is not str
                    or _OBJECT_ID.fullmatch(item.object_id) is None
                    or item.object_id != f"{item.attempt_id}.json"
                    or type(item.body_sha256) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", item.body_sha256) is None
                ):
                    raise StorageError("body_commit_metadata_invalid")
                result.append(item)
            return tuple(result)

    def _write_account(
        self,
        db: sqlite3.Connection,
        old: AccountRateLedger,
        updated: AccountRateLedger,
        *,
        generation: int,
        active_slot: str | None,
        active_holder: str | None,
        active_generation: int | None,
    ) -> None:
        commit_account_ledger(self.account_ref, old.data, updated.data)
        count = db.execute(
            "UPDATE account_ledgers SET data=?, head=?, version=?, generation=?, "
            "active_slot=?, active_holder=?, active_generation=? "
            "WHERE account_ref=? AND head=? AND version=?",
            (
                updated.data,
                updated.head_hash,
                updated.event_count,
                generation,
                active_slot,
                active_holder,
                active_generation,
                self.account_ref,
                old.head_hash,
                old.event_count,
            ),
        ).rowcount
        if count != 1:
            raise StorageError("account_compare_and_append_stale")

    def _write_plan(
        self,
        db: sqlite3.Connection,
        old: EvidenceJournal,
        updated: EvidenceJournal,
    ) -> None:
        if updated.event_count != old.event_count + 1 or not updated.data.startswith(
            old.data
        ):
            raise StorageError("plan_compare_and_append_invalid")
        count = db.execute(
            "UPDATE plan_journals SET data=?, head=?, version=? "
            "WHERE account_ref=? AND plan_sha256=? AND head=? AND version=?",
            (
                updated.data,
                updated.head_hash,
                updated.event_count,
                self.account_ref,
                old.plan.sha256,
                old.head_hash,
                old.event_count,
            ),
        ).rowcount
        if count != 1:
            raise StorageError("plan_compare_and_append_stale")

    def _related_journals(
        self,
        db: sqlite3.Connection,
        current: tuple[HttpAcquisitionPlan, OwnerApprovalClaim],
        related: tuple[tuple[HttpAcquisitionPlan, OwnerApprovalClaim], ...],
    ) -> tuple[EvidenceJournal, ...]:
        pairs = (current, *related)
        by_hash = {plan.sha256: (plan, approval) for plan, approval in pairs}
        registered = {
            row[0]
            for row in db.execute(
                "SELECT plan_sha256 FROM plan_journals WHERE account_ref=?",
                (self.account_ref,),
            )
        }
        if len(by_hash) != len(pairs) or set(by_hash) != registered:
            raise StorageError("related_plan_journals_incomplete_or_duplicate")
        return tuple(self._read_plan(db, *by_hash[sha]) for sha in sorted(registered))

    def reserve_slot(
        self,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
        *,
        at: datetime,
        holder_id: str,
        inventory: Callable[[EvidenceJournal], BodyInventory],
        related: tuple[tuple[HttpAcquisitionPlan, OwnerApprovalClaim], ...] = (),
        expected_account_head: str | None = None,
        expected_plan_head: str | None = None,
    ) -> SlotToken:
        """Read raw current evidence and atomically reserve both journal sides."""

        if not callable(inventory):
            raise StorageError("fresh_inventory_loader_required")
        with self._transaction() as db:
            ledger, row = self._read_account(db)
            if row["hold_reason"] is not None:
                raise StorageError("account_quarantined:" + row["hold_reason"])
            journals = self._related_journals(db, (plan, approval), related)
            journal = next(j for j in journals if j.plan.sha256 == plan.sha256)
            if (
                expected_account_head is not None
                and ledger.head_hash != expected_account_head
            ):
                raise StorageError("account_compare_and_append_stale")
            if (
                expected_plan_head is not None
                and journal.head_hash != expected_plan_head
            ):
                raise StorageError("plan_compare_and_append_stale")
            # A 200 response stays open in the plan until its body/page is
            # committed. Do not let a different plan consume the account slot.
            if any(j._state.open_attempt is not None for j in journals):
                raise StorageError("related_plan_attempt_unfinished")
            check_approval_scope(plan, approval, now=at)
            # The callback must inspect only body files. It receives the fresh
            # journal loaded inside this transaction; a nested store read would
            # contend on our own BEGIN IMMEDIATE lock.
            snapshot = budget_snapshot(journal, inventory(journal), now=at)
            assessment = assess_account_slot(
                ledger, plan, now=at, plan_journals=journals
            )
            if not snapshot.plan_allows_next_attempt:
                raise StorageError(
                    "plan_reservation_blocked:" + snapshot.blocking_reasons[0]
                )
            if not assessment.account_allows_next_attempt:
                raise StorageError(
                    "account_reservation_blocked:" + assessment.blocking_reasons[0]
                )
            page = snapshot.next_page
            number = snapshot.next_attempt_number
            if page is None or number is None:
                raise StorageError("next_attempt_unavailable")
            attempt_id = make_attempt_id(plan, page, number)
            account_event = AccountRateEvent(
                "slot_reserved",
                at,
                slot_id=attempt_id,
                plan_sha256=plan.sha256,
                holder_id=holder_id,
            )
            plan_event = JournalEvent(
                "attempt_reserved",
                at,
                attempt_id=attempt_id,
                page=page,
                attempt_number=number,
                approval_sha256=approval.sha256,
                allowed_transfer_bytes=snapshot.next_allowed_transfer_bytes,
                allowed_decoded_bytes=snapshot.next_allowed_decoded_bytes,
                allowed_saved_bytes=snapshot.next_allowed_saved_bytes,
            )
            updated_account = ledger.append(account_event)
            updated_plan = journal.append(plan_event)
            generation = row["generation"] + 1
            self._write_account(
                db,
                ledger,
                updated_account,
                generation=generation,
                active_slot=attempt_id,
                active_holder=holder_id,
                active_generation=generation,
            )
            self._write_plan(db, journal, updated_plan)
            return SlotToken(
                self.account_ref,
                plan.sha256,
                attempt_id,
                holder_id,
                generation,
                page,
                snapshot.next_allowed_transfer_bytes,
                snapshot.next_allowed_decoded_bytes,
                snapshot.next_allowed_saved_bytes,
            )

    def _current(
        self,
        ledger: AccountRateLedger,
        row: sqlite3.Row,
        token: SlotToken,
        *,
        require_open: bool,
    ) -> None:
        if type(token) is not SlotToken or token.account_ref != self.account_ref:
            raise StorageError("invalid_slot_token")
        slot = ledger._state.slots.get(token.attempt_id)
        if (
            slot is None
            or slot.plan_sha256 != token.plan_sha256
            or slot.holder_id != token.holder_id
            or row["generation"] != token.generation
        ):
            raise StorageError("stale_slot_generation")
        if require_open and (
            row["active_slot"] != token.attempt_id
            or row["active_holder"] != token.holder_id
            or row["active_generation"] != token.generation
        ):
            raise StorageError("slot_not_current_owner")
        if slot.reclaimed_by is not None:
            raise StorageError("stale_slot_generation")

    @staticmethod
    def _check_token_plan(token: SlotToken, journal: EvidenceJournal) -> None:
        attempt = journal._state.attempts.get(token.attempt_id)
        if attempt is None or (
            token.plan_sha256 != journal.plan.sha256
            or token.page != attempt.page
            or (
                token.allowed_transfer_bytes,
                token.allowed_decoded_bytes,
                token.allowed_saved_bytes,
            )
            != attempt.allowed
        ):
            raise StorageError("slot_token_plan_evidence_mismatch")

    def assert_current(self, token: SlotToken, *, require_open: bool = True) -> None:
        with self._transaction() as db:
            ledger, row = self._read_account(db)
            if row["hold_reason"] is not None:
                raise StorageError("account_quarantined:" + row["hold_reason"])
            self._current(ledger, row, token, require_open=require_open)

    def mark_sent(
        self,
        token: SlotToken,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
        *,
        at: datetime,
        inventory: Callable[[EvidenceJournal], BodyInventory],
        related: tuple[tuple[HttpAcquisitionPlan, OwnerApprovalClaim], ...] = (),
    ) -> float:
        """Recheck fresh evidence and atomically record a localhost send.

        The returned timeout is the remaining limit at ``at``. It is not
        network permission: the caller must still check its fencing token and
        start the artificial request within that window.
        """

        if not callable(inventory):
            raise StorageError("fresh_inventory_loader_required")

        with self._transaction() as db:
            ledger, row = self._read_account(db)
            if row["hold_reason"] is not None:
                raise StorageError("account_quarantined:" + row["hold_reason"])
            self._current(ledger, row, token, require_open=True)
            journals = self._related_journals(db, (plan, approval), related)
            journal = next(j for j in journals if j.plan.sha256 == plan.sha256)
            if token.plan_sha256 != plan.sha256:
                raise StorageError("slot_plan_mismatch")
            self._check_token_plan(token, journal)
            check_approval_scope(plan, approval, now=at)
            snapshot = budget_snapshot(journal, inventory(journal), now=at)
            expected_blocks = {
                "unsettled_attempt",
                "attempt_budget_exhausted",
                "page_retry_budget_exhausted",
            }
            unexpected = [
                reason
                for reason in snapshot.blocking_reasons
                if reason not in expected_blocks
            ]
            if unexpected:
                raise StorageError("plan_send_blocked:" + unexpected[0])
            attempt = journal._state.attempts[token.attempt_id]
            if (
                journal._state.open_attempt != token.attempt_id
                or attempt.state != "reserved"
                or snapshot.status != "open"
                or snapshot.unsettled_attempts != 1
                or snapshot.remaining_transfer_bytes < token.allowed_transfer_bytes
                or snapshot.remaining_decoded_bytes < token.allowed_decoded_bytes
                or snapshot.remaining_saved_bytes < token.allowed_saved_bytes
            ):
                raise StorageError("plan_send_evidence_changed")
            for related_journal in journals:
                reconciliation = reconcile_account_and_plan(ledger, related_journal)
                if related_journal.plan.sha256 == plan.sha256:
                    if reconciliation.status != "pending" or reconciliation.issues != (
                        "pending_settlement_on_both_ledgers",
                    ):
                        raise StorageError("current_plan_send_reconciliation_failed")
                elif reconciliation.status != "consistent":
                    raise StorageError("related_plan_send_reconciliation_failed")
            assessment = assess_account_slot(
                ledger, plan, now=at, plan_journals=journals
            )
            expected_account_blocks = {
                "account_slot_in_use",
                "account_ledger_pending:pending_settlement_on_both_ledgers",
            }
            if set(assessment.blocking_reasons) != expected_account_blocks:
                raise StorageError(
                    "account_send_blocked:"
                    + (
                        next(
                            (
                                reason
                                for reason in assessment.blocking_reasons
                                if reason not in expected_account_blocks
                            ),
                            "current_slot_unavailable",
                        )
                    )
                )
            slot = ledger._state.slots[token.attempt_id]
            deadline = journal._state.deadline()
            limits = [
                float(plan.retry.timeout_seconds),
                float(ledger.policy.request_timeout_seconds),
                (ledger._state.lease_end(slot) - at).total_seconds(),
                (plan.expires_at - at).total_seconds(),
                (approval.valid_until - at).total_seconds(),
            ]
            if deadline is not None:
                limits.append((deadline - at).total_seconds())
            remaining_timeout = min(limits)
            if remaining_timeout <= 0:
                raise StorageError("send_deadline_expired")
            updated_plan = journal.append(
                JournalEvent("attempt_sent", at, attempt_id=token.attempt_id)
            )
            updated_account = ledger.append(
                AccountRateEvent(
                    "slot_sent", at, slot_id=token.attempt_id, holder_id=token.holder_id
                )
            )
            self._write_plan(db, journal, updated_plan)
            self._write_account(
                db,
                ledger,
                updated_account,
                generation=row["generation"],
                active_slot=token.attempt_id,
                active_holder=token.holder_id,
                active_generation=token.generation,
            )
            return remaining_timeout

    def settle_response(
        self,
        token: SlotToken,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
        *,
        at: datetime,
        status: int,
        transfer_bytes: int,
        decoded_bytes: int,
        observed_retry_after_seconds: int | None,
        effective_wait_seconds: int,
        hold_reason: str | None = None,
    ) -> None:
        """Record the observed response on both sides, never inferring success."""

        with self._transaction() as db:
            ledger, row = self._read_account(db)
            self._current(ledger, row, token, require_open=True)
            journal = self._read_plan(db, plan, approval)
            if token.plan_sha256 != plan.sha256:
                raise StorageError("slot_plan_mismatch")
            self._check_token_plan(token, journal)
            updated_plan = journal.append(
                JournalEvent(
                    "response_received",
                    at,
                    attempt_id=token.attempt_id,
                    status=status,
                    transfer_bytes=transfer_bytes,
                    decoded_bytes=decoded_bytes,
                    retry_after_seconds=observed_retry_after_seconds,
                )
            )
            outcome = (
                "429" if status == 429 else "5xx" if 500 <= status < 600 else "response"
            )
            updated_account = ledger.append(
                AccountRateEvent(
                    "slot_settled",
                    at,
                    slot_id=token.attempt_id,
                    holder_id=token.holder_id,
                    outcome=outcome,
                    observed_retry_after_seconds=observed_retry_after_seconds,
                    effective_wait_seconds=effective_wait_seconds,
                )
            )
            result = reconcile_account_and_plan(updated_account, updated_plan)
            if result.status != "consistent":
                raise StorageError("journal_reconciliation_failed:" + result.issues[0])
            self._write_plan(db, journal, updated_plan)
            self._write_account(
                db,
                ledger,
                updated_account,
                generation=row["generation"],
                active_slot=None,
                active_holder=None,
                active_generation=None,
            )
            if hold_reason is not None:
                self._hold_in_transaction(db, hold_reason)

    def settle_unknown(
        self,
        token: SlotToken,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
        *,
        at: datetime,
        effective_wait_seconds: int,
        hold_reason: str | None = None,
    ) -> None:
        """Preserve uncertainty after network failure or timeout on both sides."""

        with self._transaction() as db:
            ledger, row = self._read_account(db)
            self._current(ledger, row, token, require_open=True)
            journal = self._read_plan(db, plan, approval)
            if token.plan_sha256 != plan.sha256:
                raise StorageError("slot_plan_mismatch")
            self._check_token_plan(token, journal)
            updated_plan = journal.append(
                JournalEvent("outcome_unknown", at, attempt_id=token.attempt_id)
            )
            updated_account = ledger.append(
                AccountRateEvent(
                    "slot_settled",
                    at,
                    slot_id=token.attempt_id,
                    holder_id=token.holder_id,
                    outcome="unknown",
                    effective_wait_seconds=effective_wait_seconds,
                )
            )
            result = reconcile_account_and_plan(updated_account, updated_plan)
            if result.status != "consistent":
                raise StorageError("journal_reconciliation_failed:" + result.issues[0])
            self._write_plan(db, journal, updated_plan)
            self._write_account(
                db,
                ledger,
                updated_account,
                generation=row["generation"],
                active_slot=None,
                active_holder=None,
                active_generation=None,
            )
            if hold_reason is not None:
                self._hold_in_transaction(db, hold_reason)

    def reclaim(
        self,
        token: SlotToken,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
        *,
        at: datetime,
        new_holder_id: str,
        effective_wait_seconds: int,
    ) -> None:
        """Reclaim an expired open slot as unknown and advance the fence."""

        with self._transaction() as db:
            ledger, row = self._read_account(db)
            self._current(ledger, row, token, require_open=True)
            journal = self._read_plan(db, plan, approval)
            if token.plan_sha256 != plan.sha256:
                raise StorageError("slot_plan_mismatch")
            self._check_token_plan(token, journal)
            slot = ledger._state.slots[token.attempt_id]
            lease_end = ledger._state.lease_end(slot)
            updated_plan = journal.append(
                JournalEvent("outcome_unknown", at, attempt_id=token.attempt_id)
            )
            updated_account = ledger.append(
                AccountRateEvent(
                    "slot_reclaimed",
                    at,
                    slot_id=token.attempt_id,
                    holder_id=new_holder_id,
                    previous_holder_id=token.holder_id,
                    lease_expired_at=lease_end,
                    reclaim_reason="lease_expired",
                    effective_wait_seconds=effective_wait_seconds,
                )
            )
            result = reconcile_account_and_plan(updated_account, updated_plan)
            if result.status != "consistent":
                raise StorageError("journal_reconciliation_failed:" + result.issues[0])
            self._write_plan(db, journal, updated_plan)
            self._write_account(
                db,
                ledger,
                updated_account,
                generation=row["generation"] + 1,
                active_slot=None,
                active_holder=None,
                active_generation=None,
            )

    def commit_body(
        self,
        token: SlotToken,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
        *,
        at: datetime,
        object_id: str,
        body_sha256: str,
        saved_bytes: int,
        next_key: str | None,
        finalize: Callable[[], _T],
    ) -> _T:
        """Fence final rename and append body/page evidence in one transaction.

        ``finalize`` should only fsync/rename a verified staged body. A crash
        after rename and before SQLite commit leaves an orphan to inventory;
        neither that file nor a partial is ever silently marked committed.
        """

        if (
            not callable(finalize)
            or type(object_id) is not str
            or _OBJECT_ID.fullmatch(object_id) is None
            or object_id != f"{token.attempt_id}.json"
        ):
            raise StorageError("body_finalize_or_id_invalid")
        with self._transaction() as db:
            ledger, row = self._read_account(db)
            self._current(ledger, row, token, require_open=False)
            if token.plan_sha256 != plan.sha256:
                raise StorageError("slot_plan_mismatch")
            slot = ledger._state.slots[token.attempt_id]
            if (
                slot.outcome != "response"
                or row["generation"] != token.generation
                or at > ledger._state.lease_end(slot)
            ):
                raise StorageError("body_commit_requires_current_success")
            journal = self._read_plan(db, plan, approval)
            self._check_token_plan(token, journal)
            updated = journal.append(
                JournalEvent(
                    "body_saved",
                    at,
                    attempt_id=token.attempt_id,
                    body_sha256=body_sha256,
                    saved_bytes=saved_bytes,
                )
            ).append(
                JournalEvent(
                    "page_completed",
                    at,
                    attempt_id=token.attempt_id,
                    next_key=next_key,
                )
            )
            existing = db.execute(
                "SELECT 1 FROM body_commits WHERE account_ref=? AND object_id=?",
                (self.account_ref, object_id),
            ).fetchone()
            if existing is not None:
                raise StorageError("body_object_id_already_committed")
            value = finalize()
            db.execute(
                "INSERT INTO body_commits VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self.account_ref,
                    plan.sha256,
                    token.attempt_id,
                    token.generation,
                    object_id,
                    body_sha256,
                    saved_bytes,
                ),
            )
            # A completed page requires two events; both occur under one transaction.
            self._write_plan_many(db, journal, updated)
            return value

    def _write_plan_many(
        self,
        db: sqlite3.Connection,
        old: EvidenceJournal,
        updated: EvidenceJournal,
    ) -> None:
        if updated.event_count <= old.event_count or not updated.data.startswith(
            old.data
        ):
            raise StorageError("plan_compare_and_append_invalid")
        count = db.execute(
            "UPDATE plan_journals SET data=?, head=?, version=? "
            "WHERE account_ref=? AND plan_sha256=? AND head=? AND version=?",
            (
                updated.data,
                updated.head_hash,
                updated.event_count,
                self.account_ref,
                old.plan.sha256,
                old.head_hash,
                old.event_count,
            ),
        ).rowcount
        if count != 1:
            raise StorageError("plan_compare_and_append_stale")

    def append_plan_event(
        self,
        plan: HttpAcquisitionPlan,
        approval: OwnerApprovalClaim,
        event: JournalEvent,
        *,
        expected_head: str | None = None,
    ) -> EvidenceJournal:
        """CAS append of bookkeeping only; attempt lifecycle uses paired APIs."""

        if type(event) is not JournalEvent or event.kind not in (
            "run_completed",
            "run_stopped",
        ):
            raise StorageError("plan_event_requires_paired_slot_api")
        with self._transaction() as db:
            journal = self._read_plan(db, plan, approval)
            if expected_head is not None and journal.head_hash != expected_head:
                raise StorageError("plan_compare_and_append_stale")
            updated = journal.append(event)
            self._write_plan(db, journal, updated)
            return updated
