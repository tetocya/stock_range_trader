"""Write-once raw-body files for the localhost historical-feasibility trial.

This is an artificial-input store, not an external receipt or a live-acquisition
permission.  Inventory is reconstructed from actual file bytes on every open.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from .http_contract import BodyFileEvidence, BodyInventory, EvidenceJournal
from .paths import create_exclusive_dir, require_existing_store_dir, write_new_file

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_PARTIAL = re.compile(
    r"(?P<attempt>[0-9a-f]{64})\.g(?P<generation>[1-9][0-9]*)\.partial\Z"
)
_FINAL = re.compile(r"(?P<attempt>[0-9a-f]{64})\.json\Z")
_BINDING = "artificial-body-binding.json"
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_RECEIPT_SUFFIX = ".receipt.json"


class RawBodyStorageError(ValueError):
    """A body path, byte stream, or local artificial binding is invalid."""


@dataclass(frozen=True)
class QuarantineRecord:
    """Immutable local evidence of a partial/orphan moved out of active input."""

    original_path: str
    quarantine_path: str
    object_id: str
    size: int
    body_sha256: str
    state: str
    reason: str
    quarantined_at: datetime
    journal_head: str
    plan_sha256: str
    approval_sha256: str

    def to_dict(self) -> dict:
        return {
            "original_path": self.original_path,
            "quarantine_path": self.quarantine_path,
            "object_id": self.object_id,
            "size": self.size,
            "body_sha256": self.body_sha256,
            "state": self.state,
            "reason": self.reason,
            "quarantined_at": self.quarantined_at.astimezone(UTC).isoformat(),
            "journal_head": self.journal_head,
            "plan_sha256": self.plan_sha256,
            "approval_sha256": self.approval_sha256,
        }


def _digest_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    size, digest = 0, hashlib.sha256()
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise RawBodyStorageError("body_not_regular_file")
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class RawBodyStore:
    """An exclusive artificial root with bounded partial and no-overwrite bodies."""

    root: Path
    plan_sha256: str
    approval_sha256: str

    @classmethod
    def create(
        cls, root: str | Path, *, plan_sha256: str, approval_sha256: str
    ) -> RawBodyStore:
        if not _HEX.fullmatch(plan_sha256) or not _HEX.fullmatch(approval_sha256):
            raise RawBodyStorageError("binding_hash_invalid")
        target = create_exclusive_dir(root)
        os.mkdir(target / "bodies")
        payload = {
            "mode": "localhost_artificial_only",
            "plan_sha256": plan_sha256,
            "approval_sha256": approval_sha256,
        }
        write_new_file(
            target / _BINDING,
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode(),
        )
        _sync_directory(target)
        return cls(target, plan_sha256, approval_sha256)

    @classmethod
    def open(
        cls, root: str | Path, *, plan_sha256: str, approval_sha256: str
    ) -> RawBodyStore:
        target = require_existing_store_dir(root)
        binding = target / _BINDING
        if binding.is_symlink() or not binding.is_file():
            raise RawBodyStorageError("binding_missing_or_not_regular")
        expected = {
            "mode": "localhost_artificial_only",
            "plan_sha256": plan_sha256,
            "approval_sha256": approval_sha256,
        }
        try:
            raw = binding.read_bytes()
            parsed = json.loads(raw)
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise RawBodyStorageError("binding_invalid") from None
        canonical = (
            json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        if parsed != expected or raw != canonical:
            raise RawBodyStorageError("binding_mismatch")
        store = cls(target, plan_sha256, approval_sha256)
        store._body_dir()
        store.quarantine_records()
        return store

    def _body_dir(self) -> Path:
        # Revalidate on each operation; directory-descriptor-relative I/O would
        # be needed to remove the remaining concurrent ancestor-swap race.
        root = require_existing_store_dir(self.root)
        body_dir = root / "bodies"
        if (
            body_dir.is_symlink()
            or not body_dir.is_dir()
            or body_dir.resolve() != body_dir
        ):
            raise RawBodyStorageError("body_directory_invalid")
        return body_dir

    def _quarantine_dir(self, *, create: bool = False) -> Path | None:
        root = require_existing_store_dir(self.root)
        directory = root / "quarantine"
        if create and not os.path.lexists(directory):
            os.mkdir(directory, 0o700)
            _sync_directory(root)
        if not os.path.lexists(directory):
            return None
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or directory.resolve() != directory
        ):
            raise RawBodyStorageError("quarantine_directory_invalid")
        return directory

    @staticmethod
    def _file_evidence(path: Path, journal: EvidenceJournal) -> BodyFileEvidence:
        if path.is_symlink() or not path.is_file():
            raise RawBodyStorageError("unexpected_body_entry")
        partial = _PARTIAL.fullmatch(path.name)
        final = _FINAL.fullmatch(path.name)
        if partial is None and final is None:
            raise RawBodyStorageError("unexpected_body_entry")
        size, digest = _digest_file(path)
        if final is not None and size == 0:
            raise RawBodyStorageError("empty_final_body")
        attempt = (
            journal._state.attempts.get(final.group("attempt"))
            if final is not None
            else None
        )
        state = (
            "partial"
            if partial is not None
            else "committed"
            if attempt is not None
            and attempt.state == "page_completed"
            and attempt.body_sha256 == digest
            else "orphan"
        )
        return BodyFileEvidence(path.name, size, state, digest)

    def quarantine_records(self) -> tuple[QuarantineRecord, ...]:
        """Re-read immutable receipts and actual bytes; never trust a listing."""

        directory = self._quarantine_dir()
        if directory is None:
            return ()
        entries = {path.name: path for path in directory.iterdir()}
        receipt_names = sorted(
            name for name in entries if name.endswith(_RECEIPT_SUFFIX)
        )
        records: list[QuarantineRecord] = []
        expected_entries: set[str] = set()
        for receipt_name in receipt_names:
            object_id = receipt_name[: -len(_RECEIPT_SUFFIX)]
            if (
                _PARTIAL.fullmatch(object_id) is None
                and _FINAL.fullmatch(object_id) is None
            ):
                raise RawBodyStorageError("quarantine_receipt_name_invalid")
            receipt = entries[receipt_name]
            if receipt.is_symlink() or not receipt.is_file():
                raise RawBodyStorageError("quarantine_receipt_invalid")
            fd = os.open(receipt, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise RawBodyStorageError("quarantine_receipt_invalid")
                raw = handle.read(8193)
            if len(raw) > 8192:
                raise RawBodyStorageError("quarantine_receipt_invalid")
            try:
                value = json.loads(raw)
                at = datetime.fromisoformat(value["quarantined_at"])
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                raise RawBodyStorageError("quarantine_receipt_invalid") from None
            if type(value) is not dict or set(value) != set(
                QuarantineRecord.__dataclass_fields__
            ):
                raise RawBodyStorageError("quarantine_receipt_invalid")
            if at.tzinfo is None or at.utcoffset() != UTC.utcoffset(at):
                raise RawBodyStorageError("quarantine_receipt_invalid")
            record = QuarantineRecord(
                **{**value, "quarantined_at": at},
            )
            if (
                record.object_id != object_id
                or record.original_path != f"bodies/{object_id}"
                or record.quarantine_path != f"quarantine/{object_id}"
                or record.plan_sha256 != self.plan_sha256
                or record.approval_sha256 != self.approval_sha256
                or record.state not in ("partial", "orphan")
                or (bool(_PARTIAL.fullmatch(object_id)) != (record.state == "partial"))
                or type(record.size) is not int
                or record.size < 0
                or type(record.body_sha256) is not str
                or _HEX.fullmatch(record.body_sha256) is None
                or type(record.journal_head) is not str
                or _HEX.fullmatch(record.journal_head) is None
                or type(record.reason) is not str
                or _REASON.fullmatch(record.reason) is None
                or raw
                != (
                    json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode()
            ):
                raise RawBodyStorageError("quarantine_receipt_invalid")
            body = entries.get(object_id)
            if body is None or body.is_symlink() or not body.is_file():
                raise RawBodyStorageError("quarantined_body_missing")
            size, digest = _digest_file(body)
            if (size, digest) != (record.size, record.body_sha256):
                raise RawBodyStorageError("quarantined_body_changed")
            if os.path.lexists(self._body_dir() / object_id):
                raise RawBodyStorageError("quarantine_source_still_active")
            expected_entries.update((receipt_name, object_id))
            records.append(record)
        if set(entries) != expected_entries:
            raise RawBodyStorageError("unreceipted_quarantine_entry")
        return tuple(records)

    def quarantine_uncommitted(
        self,
        journal: EvidenceJournal,
        object_id: str,
        *,
        reason: str,
        at: datetime,
    ) -> QuarantineRecord:
        """Move a verified partial/orphan, preserving its bytes and receipt."""

        if (
            type(journal) is not EvidenceJournal
            or journal.plan.sha256 != self.plan_sha256
            or journal.approval is None
            or journal.approval.sha256 != self.approval_sha256
            or type(object_id) is not str
            or (
                _PARTIAL.fullmatch(object_id) is None
                and _FINAL.fullmatch(object_id) is None
            )
            or type(reason) is not str
            or _REASON.fullmatch(reason) is None
            or type(at) is not datetime
            or at.tzinfo is None
        ):
            raise RawBodyStorageError("quarantine_input_invalid")
        self.quarantine_records()  # existing evidence must already be coherent
        source = self._body_dir() / object_id
        evidence = self._file_evidence(source, journal)
        if evidence.state == "committed":
            raise RawBodyStorageError("committed_body_cannot_be_quarantined")
        directory = self._quarantine_dir(create=True)
        target = directory / object_id
        receipt = directory / f"{object_id}{_RECEIPT_SUFFIX}"
        if os.path.lexists(target) or os.path.lexists(receipt):
            raise RawBodyStorageError("quarantine_target_already_exists")
        record = QuarantineRecord(
            f"bodies/{object_id}",
            f"quarantine/{object_id}",
            object_id,
            evidence.size,
            evidence.body_sha256,
            evidence.state,
            reason,
            at.astimezone(UTC),
            journal.head_hash,
            self.plan_sha256,
            self.approval_sha256,
        )
        # Link first, then write the receipt, then remove only the active link.
        # A crash at any intermediate point leaves bytes in at least one place
        # and causes a fail-closed inventory/receipt check on resume.
        os.link(source, target, follow_symlinks=False)
        _sync_directory(directory)
        if _digest_file(target) != (evidence.size, evidence.body_sha256):
            raise RawBodyStorageError("quarantined_body_changed")
        write_new_file(
            receipt,
            (
                json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode(),
        )
        _sync_directory(directory)
        os.unlink(source)
        _sync_directory(source.parent)
        return record

    def begin_partial(self, attempt_id: str, generation: int) -> tuple[Path, BinaryIO]:
        if (
            not _HEX.fullmatch(attempt_id)
            or type(generation) is not int
            or generation < 1
        ):
            raise RawBodyStorageError("partial_identity_invalid")
        path = self._body_dir() / f"{attempt_id}.g{generation}.partial"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            raise RawBodyStorageError("partial_already_exists") from None
        return path, os.fdopen(fd, "wb")

    def verify_partial(
        self, path: Path, *, expected_sha256: str, expected_size: int
    ) -> None:
        if path.parent != self._body_dir() or not _PARTIAL.fullmatch(path.name):
            raise RawBodyStorageError("partial_path_invalid")
        if path.is_symlink() or not path.is_file():
            raise RawBodyStorageError("partial_missing_or_not_regular")
        size, digest = _digest_file(path)
        if size <= 0 or size != expected_size or digest != expected_sha256:
            raise RawBodyStorageError("partial_hash_or_size_mismatch")

    def read_partial(self, path: Path, *, max_bytes: int) -> bytes:
        if (
            path.parent != self._body_dir()
            or not _PARTIAL.fullmatch(path.name)
            or type(max_bytes) is not int
            or max_bytes < 1
        ):
            raise RawBodyStorageError("partial_read_invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            raise RawBodyStorageError("partial_missing_or_not_regular") from None
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise RawBodyStorageError("body_not_regular_file")
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RawBodyStorageError("partial_exceeds_page_cap")
        return data

    def publish_partial(
        self, path: Path, *, attempt_id: str, expected_sha256: str, expected_size: int
    ) -> Path:
        """Atomically add a final file without overwriting an existing object."""

        self.verify_partial(
            path, expected_sha256=expected_sha256, expected_size=expected_size
        )
        if not _HEX.fullmatch(attempt_id) or not path.name.startswith(
            attempt_id + ".g"
        ):
            raise RawBodyStorageError("partial_attempt_mismatch")
        directory = self._body_dir()
        final = directory / f"{attempt_id}.json"
        # A hard link is atomic publication with O_EXCL-like no-overwrite
        # semantics on the same filesystem.  An interrupted DB commit leaves
        # this final file as an explicit orphan on resume, never as success.
        try:
            os.link(path, final, follow_symlinks=False)
        except FileExistsError:
            raise RawBodyStorageError("body_already_exists") from None
        _sync_directory(directory)
        os.unlink(path)
        _sync_directory(directory)
        return final

    def discard_partial(self, path: Path) -> None:
        """Remove only this run's own incomplete file after a known non-200 result."""

        if path.parent != self._body_dir() or not _PARTIAL.fullmatch(path.name):
            raise RawBodyStorageError("partial_path_invalid")
        if path.is_symlink() or not path.is_file():
            raise RawBodyStorageError("partial_missing_or_not_regular")
        path.unlink()
        _sync_directory(path.parent)

    def read_final(self, attempt_id: str, *, max_bytes: int) -> bytes:
        """Read a previously committed artificial body under a fixed size cap."""

        if (
            not _HEX.fullmatch(attempt_id)
            or type(max_bytes) is not int
            or max_bytes < 1
        ):
            raise RawBodyStorageError("body_read_identity_or_limit_invalid")
        path = self._body_dir() / f"{attempt_id}.json"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            raise RawBodyStorageError("committed_body_missing") from None
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise RawBodyStorageError("body_not_regular_file")
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RawBodyStorageError("committed_body_exceeds_page_cap")
        return data

    def inventory(self, journal: EvidenceJournal) -> BodyInventory:
        if journal.plan.sha256 != self.plan_sha256:
            raise RawBodyStorageError("inventory_plan_mismatch")
        self.quarantine_records()
        return BodyInventory(
            tuple(
                self._file_evidence(path, journal)
                for path in sorted(self._body_dir().iterdir())
            )
        )

    def uncommitted_files(
        self, journal: EvidenceJournal
    ) -> tuple[BodyFileEvidence, ...]:
        """List partial/orphan objects for an explicit maintenance decision."""

        if journal.plan.sha256 != self.plan_sha256:
            raise RawBodyStorageError("inventory_plan_mismatch")
        self.quarantine_records()
        files = (
            self._file_evidence(path, journal)
            for path in sorted(self._body_dir().iterdir())
        )
        return tuple(item for item in files if item.state != "committed")
