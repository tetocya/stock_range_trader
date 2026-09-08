"""Single local SQLite audit DB, explicit transactions and bounded writer waits."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .audit_errors import (
    HeadConflict,
    IdempotencyConflict,
    IdentityMismatch,
    IntegrityError,
    InvalidEvent,
    StoreBusy,
)
from .audit_models import (
    AuditEvent,
    CommitReceipt,
    EventCommand,
    Head,
    StateSnapshot,
    StreamIdentity,
)
from .recovery import (
    RecoveredState,
    Reducer,
    StoredRecords,
    recover_records,
    reduce_state,
    require_command_identity,
    require_reducer,
    verify_chain,
)
from .serialization import JsonObject, canonical_json, parse_json, require_text

_DDL = (
    "CREATE TABLE streams (stream_id TEXT PRIMARY KEY, identity_json TEXT NOT NULL, initial_json TEXT NOT NULL, head_sequence INTEGER NOT NULL CHECK(head_sequence >= 0), head_hash TEXT NOT NULL, current_json TEXT NOT NULL, current_hash TEXT NOT NULL)",
    "CREATE TABLE events (stream_id TEXT NOT NULL REFERENCES streams(stream_id), sequence INTEGER NOT NULL CHECK(sequence > 0), event_id TEXT NOT NULL, event_json TEXT NOT NULL, PRIMARY KEY(stream_id, sequence), UNIQUE(stream_id, event_id))",
    "CREATE TABLE snapshots (stream_id TEXT NOT NULL REFERENCES streams(stream_id), sequence INTEGER NOT NULL CHECK(sequence >= 0), snapshot_json TEXT NOT NULL, PRIMARY KEY(stream_id, sequence))",
)


class EventStore:
    """Create/resume are separate. No event UPDATE/DELETE or automatic repair API.

    Use one logical writer on a local filesystem. `_fault_hook` is a bounded test
    seam invoked inside the real transaction (and once just after COMMIT).
    """

    def __init__(self, connection: sqlite3.Connection, identity: StreamIdentity):
        self._connection = connection
        self.identity = identity

    @staticmethod
    def _connect(
        path: Path, *, create: bool, busy_timeout_ms: int
    ) -> sqlite3.Connection:
        if type(busy_timeout_ms) is not int or not 0 <= busy_timeout_ms <= 60_000:
            raise InvalidEvent("invalid_busy_timeout")
        path = Path(path)
        # mode=rw never creates a missing database on resume.
        uri = path.absolute().as_uri() + ("?mode=rwc" if create else "?mode=rw")
        connection = None
        try:
            connection = sqlite3.connect(
                uri, uri=True, timeout=busy_timeout_ms / 1000, isolation_level=None
            )
            connection.execute("PRAGMA foreign_keys=ON")
            # DELETE journal + FULL synchronous are deliberate local-FS settings.
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            connection.execute("PRAGMA synchronous=FULL")
            if (
                mode != "delete"
                or connection.execute("PRAGMA synchronous").fetchone()[0] != 2
            ):
                raise IntegrityError("unsupported_durability_settings")
            return connection
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            EventStore._database_error(exc)
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    @staticmethod
    def _database_error(exc: sqlite3.Error) -> None:
        if getattr(exc, "sqlite_errorcode", 0) & 255 in (
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        ):
            raise StoreBusy("store_busy") from None
        raise IntegrityError("database_operation_failed") from None

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        try:
            self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield
            self._connection.execute("COMMIT")
        except BaseException as exc:
            if self._connection.in_transaction:
                self._connection.rollback()
            if isinstance(exc, sqlite3.Error):
                self._database_error(exc)
            raise

    @classmethod
    def create(
        cls,
        path: Path,
        identity: StreamIdentity,
        initial_state: JsonObject,
        *,
        snapshot_initial: bool = False,
        busy_timeout_ms: int = 1000,
    ) -> "EventStore":
        if (
            type(identity) is not StreamIdentity
            or type(initial_state) is not JsonObject
        ):
            raise InvalidEvent("stream_types_invalid")
        if type(snapshot_initial) is not bool:
            raise InvalidEvent("snapshot_flag_invalid")
        if identity.initial_state_hash != initial_state.sha256:
            raise IdentityMismatch("initial_state_identity_mismatch")
        new_file = not Path(path).exists()
        store = cls(
            cls._connect(path, create=True, busy_timeout_ms=busy_timeout_ms), identity
        )
        try:
            with store._transaction(write=True):
                if new_file:
                    for statement in _DDL:
                        store._connection.execute(statement)
                    store._connection.execute("PRAGMA user_version=1")
                store._check_schema()
                if store._connection.execute(
                    "SELECT 1 FROM streams WHERE stream_id=?", (identity.stream_id,)
                ).fetchone():
                    raise IdentityMismatch("stream_already_exists")
                store._connection.execute(
                    "INSERT INTO streams VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        identity.stream_id,
                        canonical_json(identity.to_dict()),
                        initial_state.encoded,
                        0,
                        identity.genesis_hash,
                        initial_state.encoded,
                        initial_state.sha256,
                    ),
                )
                if snapshot_initial:
                    store._insert_snapshot(
                        StateSnapshot.create(
                            identity, Head(0, identity.genesis_hash), initial_state
                        )
                    )
            return store
        except BaseException:
            store.close()
            raise

    @classmethod
    def resume(
        cls,
        path: Path,
        identity: StreamIdentity,
        reducer: Reducer,
        *,
        expected_head: Head | None = None,
        busy_timeout_ms: int = 1000,
    ) -> "EventStore":
        if type(identity) is not StreamIdentity:
            raise InvalidEvent("identity_required")
        store = cls(
            cls._connect(path, create=False, busy_timeout_ms=busy_timeout_ms), identity
        )
        try:
            store.recover(reducer, expected_head=expected_head)
            return store
        except BaseException:
            store.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _check_schema(self) -> None:
        if self._connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise IdentityMismatch("storage_schema_mismatch")

    def _load(self) -> StoredRecords:
        """Caller holds a transaction so all tables come from one read view."""
        self._check_schema()
        row = self._connection.execute(
            "SELECT identity_json, initial_json, head_sequence, head_hash, current_json, current_hash FROM streams WHERE stream_id=?",
            (self.identity.stream_id,),
        ).fetchone()
        if row is None:
            raise IdentityMismatch("stream_missing")
        try:
            identity = StreamIdentity.from_dict(parse_json(row[0]))
            if identity != self.identity:
                raise IdentityMismatch("stream_identity_mismatch")
            initial = JsonObject(row[1])
            head = Head(row[2], row[3])
            current = JsonObject(row[4])
            if current.sha256 != row[5]:
                raise IntegrityError("current_state_hash_mismatch")
            events = []
            for sequence, event_id, encoded in self._connection.execute(
                "SELECT sequence, event_id, event_json FROM events WHERE stream_id=? ORDER BY sequence",
                (identity.stream_id,),
            ):
                event = AuditEvent.from_dict(parse_json(encoded))
                if event.sequence != sequence or event.command.event_id != event_id:
                    raise IntegrityError("event_index_mismatch")
                events.append(event)
            snapshots = []
            for sequence, encoded in self._connection.execute(
                "SELECT sequence, snapshot_json FROM snapshots WHERE stream_id=? ORDER BY sequence",
                (identity.stream_id,),
            ):
                snapshot = StateSnapshot.from_dict(parse_json(encoded))
                if snapshot.sequence != sequence:
                    raise IntegrityError("snapshot_index_mismatch")
                snapshots.append(snapshot)
            records = StoredRecords(
                identity, initial, tuple(events), tuple(snapshots), head, current
            )
            verify_chain(records)
            return records
        except (InvalidEvent, TypeError, KeyError, OverflowError):
            raise IntegrityError("invalid_stored_record") from None

    def read(self) -> StoredRecords:
        """Structurally verified records; resume/recover also replay the reducer."""
        with self._transaction(write=False):
            return self._load()

    def lookup(self, event_id: str) -> CommitReceipt | None:
        require_text(event_id)
        for event in self.read().events:
            if event.command.event_id == event_id:
                return CommitReceipt(event)
        return None

    def recover(
        self, reducer: Reducer, *, expected_head: Head | None = None
    ) -> RecoveredState:
        with self._transaction(write=False):
            return recover_records(self._load(), reducer, expected_head=expected_head)

    def _insert_snapshot(self, snapshot: StateSnapshot) -> None:
        self._connection.execute(
            "INSERT INTO snapshots VALUES (?, ?, ?)",
            (
                self.identity.stream_id,
                snapshot.sequence,
                canonical_json(snapshot.to_dict()),
            ),
        )

    def commit_event(
        self,
        command: EventCommand,
        expected_head: Head,
        reducer: Reducer,
        *,
        save_snapshot: bool = False,
        _fault_hook: Callable[[str], None] | None = None,
    ) -> CommitReceipt:
        if (
            type(command) is not EventCommand
            or type(expected_head) is not Head
            or type(save_snapshot) is not bool
        ):
            raise InvalidEvent("commit_arguments_invalid")
        require_command_identity(command, self.identity)
        require_reducer(reducer, self.identity)

        def fault(point: str) -> None:
            if _fault_hook is not None:
                _fault_hook(point)

        with self._transaction(write=True):
            records = self._load()
            for event in records.events:
                if event.command.event_id == command.event_id:
                    # Python dict equality conflates True and 1; command
                    # meaning is defined by the typed canonical JSON instead.
                    if canonical_json(event.command.to_dict()) != canonical_json(
                        command.to_dict()
                    ):
                        raise IdempotencyConflict("event_id_content_conflict")
                    return CommitReceipt(event)
            if expected_head != records.head:
                raise HeadConflict("stale_expected_head")
            if command.correction_of is not None and not any(
                event.command.event_id == command.correction_of
                for event in records.events
            ):
                raise InvalidEvent("correction_target_missing")
            if records.events:
                previous = records.events[-1].command
                if (
                    command.market_decision_at < previous.market_decision_at
                    or command.replayed_at < previous.replayed_at
                ):
                    raise InvalidEvent("clock_reversal")
            after = reduce_state(reducer, records.current_state, command)
            event = AuditEvent.create(
                command, records.head, records.current_state, after
            )
            fault("before_event_insert")
            self._connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?)",
                (
                    self.identity.stream_id,
                    event.sequence,
                    command.event_id,
                    canonical_json(event.to_dict()),
                ),
            )
            fault("after_event_insert")
            self._connection.execute(
                "UPDATE streams SET head_sequence=?, head_hash=?, current_json=?, current_hash=? WHERE stream_id=?",
                (
                    event.sequence,
                    event.event_hash,
                    after.encoded,
                    after.sha256,
                    self.identity.stream_id,
                ),
            )
            fault("after_state_update")
            if save_snapshot:
                self._insert_snapshot(
                    StateSnapshot.create(
                        self.identity, Head(event.sequence, event.event_hash), after
                    )
                )
                fault("after_snapshot_insert")
        fault("after_commit")
        return CommitReceipt(event)
