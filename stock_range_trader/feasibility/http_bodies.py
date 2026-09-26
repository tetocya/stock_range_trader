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


class RawBodyStorageError(ValueError):
    """A body path, byte stream, or local artificial binding is invalid."""


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
        files = []
        for path in sorted(self._body_dir().iterdir()):
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
            files.append(BodyFileEvidence(path.name, size, state, digest))
        return BodyInventory(tuple(files))
