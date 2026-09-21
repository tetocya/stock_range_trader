"""Consistent, read-only SQLite and immutable external evidence snapshots."""

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from delayed_replay.audit_models import StreamIdentity
from delayed_replay.event_store import EventStore
from delayed_replay.serialization import JsonObject, parse_json


class ObservationError(ValueError):
    """Fixed diagnostic codes only; never expose credentials or filesystem paths."""


class EvidenceFiles:
    def __init__(self):
        self._files = {}
        self._missing = set()

    def read(self, path):
        path = Path(path).absolute()
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise ObservationError("symlink_input_refused")
        try:
            before = path.stat()
            body = path.read_bytes()
            after = path.stat()
        except FileNotFoundError:
            self._missing.add(path)
            raise ObservationError("missing_input") from None
        except OSError:
            raise ObservationError("unreadable_input") from None
        signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != signature:
            raise ObservationError("input_changed_during_read")
        item = (signature, hashlib.sha256(body).hexdigest())
        if path in self._files and self._files[path] != item:
            raise ObservationError("input_changed_during_read")
        self._files[path] = item
        return body

    def json(self, path, expected_hash=None):
        body = self.read(path)
        try:
            value = JsonObject(parse_json_text(body))
        except (UnicodeError, ValueError):
            raise ObservationError("corrupt_json_evidence") from None
        if expected_hash is not None and value.sha256 != expected_hash:
            raise ObservationError("evidence_hash_mismatch")
        return value

    def verify(self):
        if any(p.exists() for p in self._missing):
            raise ObservationError("missing_input_appeared_during_read")
        for path in tuple(self._files):
            self.read(path)

    @property
    def hashes(self):
        # Opaque IDs, not local usernames or directory names.
        return sorted({item[1] for item in self._files.values()})


def parse_json_text(body):
    text = body.decode("utf-8")
    parse_json(text)
    return text


def _authorize(action, arg1, arg2, *_):
    if action in (
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_TRANSACTION,
    ):
        return sqlite3.SQLITE_OK
    if (
        action == sqlite3.SQLITE_PRAGMA
        and arg1 in ("user_version", "journal_mode")
        and arg2 is None
    ):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


@contextmanager
def read_database(path, files):
    """Shared readonly transaction; reject active journals before connecting."""
    path = Path(path).absolute()
    body = files.read(path)
    if len(body) < 100 or body[:16] != b"SQLite format 3\x00":
        raise ObservationError("invalid_database_header")
    sidecars = [Path(str(path) + s) for s in ("-wal", "-shm", "-journal")]
    if body[18:20] != b"\x01\x01" or any(p.exists() for p in sidecars):
        raise ObservationError("stopped_consistent_non_wal_snapshot_required")
    connection = None
    try:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=1
        )
        connection.execute("PRAGMA query_only=ON")
        connection.set_authorizer(_authorize)
        connection.execute("BEGIN")
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise ObservationError("unsupported_journal_mode")
        yield connection
        files.verify()
        connection.execute("COMMIT")
        files.verify()
        if any(p.exists() for p in sidecars):
            raise ObservationError("concurrent_writer_detected")
    except sqlite3.Error:
        raise ObservationError("database_read_or_schema_failure") from None
    finally:
        if connection is not None:
            connection.close()


@contextmanager
def read_account(path, files):
    """Reuse EventStore._load, never recovery/reducer/migration/checkpoint."""
    with read_database(path, files) as connection:
        identities = connection.execute("SELECT identity_json FROM streams").fetchall()
        if len(identities) != 1:
            raise ObservationError("exactly_one_stream_required")
        identity = StreamIdentity.from_dict(parse_json(identities[0][0]))
        yield EventStore(connection, identity)._load()
