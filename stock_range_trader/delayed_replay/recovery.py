"""Full deterministic verification; snapshots never bypass the event history."""

from dataclasses import dataclass
from typing import Protocol

from .audit_errors import IdentityMismatch, IntegrityError, InvalidEvent
from .audit_models import AuditEvent, EventCommand, Head, StateSnapshot, StreamIdentity
from .serialization import JsonObject


class Reducer(Protocol):
    """Pure transition, no I/O, random values, wall-clock reads or external state."""

    identity: str

    def __call__(self, state: dict, command: EventCommand) -> dict: ...


@dataclass(frozen=True, slots=True)
class StoredRecords:
    identity: StreamIdentity
    initial_state: JsonObject
    events: tuple[AuditEvent, ...]
    snapshots: tuple[StateSnapshot, ...]
    head: Head
    current_state: JsonObject

    def __post_init__(self) -> None:
        if (
            type(self.identity) is not StreamIdentity
            or type(self.initial_state) is not JsonObject
            or type(self.current_state) is not JsonObject
            or type(self.head) is not Head
            or type(self.events) is not tuple
            or any(type(event) is not AuditEvent for event in self.events)
            or type(self.snapshots) is not tuple
            or any(type(snapshot) is not StateSnapshot for snapshot in self.snapshots)
        ):
            raise InvalidEvent("immutable_records_required")


@dataclass(frozen=True, slots=True)
class RecoveredState:
    head: Head
    state: JsonObject

    def __post_init__(self) -> None:
        if type(self.head) is not Head or type(self.state) is not JsonObject:
            raise InvalidEvent("immutable_recovery_required")


def require_command_identity(command: EventCommand, identity: StreamIdentity) -> None:
    if (
        command.stream_id != identity.stream_id
        or command.stream_identity_hash != identity.genesis_hash
        or command.config_hash != identity.config_hash
        or command.reducer_identity != identity.reducer_identity
        or command.schema_version != identity.event_schema_version
    ):
        raise IdentityMismatch("command_identity_mismatch")


def require_reducer(reducer: Reducer, identity: StreamIdentity) -> None:
    if (
        not callable(reducer)
        or getattr(reducer, "identity", None) != identity.reducer_identity
    ):
        raise IdentityMismatch("reducer_identity_mismatch")


def reduce_state(
    reducer: Reducer, state: JsonObject, command: EventCommand
) -> JsonObject:
    # Even an accidentally mutating reducer receives its own detached input.
    try:
        return JsonObject.from_value(reducer(state.to_dict(), command))
    except Exception:
        raise InvalidEvent("reducer_rejected_transition") from None


def verify_chain(records: StoredRecords) -> None:
    identity = records.identity
    if records.initial_state.sha256 != identity.initial_state_hash:
        raise IntegrityError("initial_state_mismatch")
    head = Head(0, identity.genesis_hash)
    state_hash = identity.initial_state_hash
    history = {0: (head.event_hash, state_hash)}
    known_ids: set[str] = set()
    previous_clock = None
    for event in records.events:
        require_command_identity(event.command, identity)
        if (
            event.sequence != head.sequence + 1
            or event.previous_event_hash != head.event_hash
        ):
            raise IntegrityError("event_chain_mismatch")
        if event.before_state_hash != state_hash:
            raise IntegrityError("state_chain_mismatch")
        command = event.command
        if command.event_id in known_ids:
            raise IntegrityError("duplicate_event_id")
        if command.correction_of is not None and command.correction_of not in known_ids:
            raise IntegrityError("invalid_correction_reference")
        if previous_clock is not None:
            if (
                command.market_decision_at < previous_clock.market_decision_at
                or command.replayed_at < previous_clock.replayed_at
            ):
                raise IntegrityError("clock_reversal")
        previous_clock = command.clock
        known_ids.add(command.event_id)
        head = Head(event.sequence, event.event_hash)
        state_hash = event.after_state_hash
        history[event.sequence] = (event.event_hash, state_hash)
    if records.head != head or records.current_state.sha256 != state_hash:
        raise IntegrityError("stored_head_state_mismatch")
    seen_sequences: set[int] = set()
    for snapshot in records.snapshots:
        if snapshot.identity != identity:
            raise IdentityMismatch("snapshot_identity_mismatch")
        if snapshot.sequence in seen_sequences or history.get(snapshot.sequence) != (
            snapshot.event_hash,
            snapshot.state_hash,
        ):
            raise IntegrityError("snapshot_history_mismatch")
        seen_sequences.add(snapshot.sequence)


def recover_records(
    records: StoredRecords, reducer: Reducer, *, expected_head: Head | None = None
) -> RecoveredState:
    """Verify all events, all snapshots, and a second replay from latest snapshot."""
    require_reducer(reducer, records.identity)
    verify_chain(records)
    if expected_head is not None:
        if type(expected_head) is not Head:
            raise InvalidEvent("head_required")
        if expected_head != records.head:
            raise IntegrityError("external_head_mismatch")
    snapshots = {snapshot.sequence: snapshot for snapshot in records.snapshots}
    state = records.initial_state
    if 0 in snapshots and snapshots[0].state != state:
        raise IntegrityError("genesis_snapshot_mismatch")
    try:
        for event in records.events:
            if event.before_state_hash != state.sha256:
                raise IntegrityError("replayed_before_state_mismatch")
            state = reduce_state(reducer, state, event.command)
            if state.sha256 != event.after_state_hash:
                raise IntegrityError("replayed_after_state_mismatch")
            if event.sequence in snapshots and snapshots[event.sequence].state != state:
                raise IntegrityError("replayed_snapshot_mismatch")
        if snapshots:
            latest = snapshots[max(snapshots)]
            tail_state = latest.state
            for event in records.events[latest.sequence :]:
                if tail_state.sha256 != event.before_state_hash:
                    raise IntegrityError("snapshot_tail_before_mismatch")
                tail_state = reduce_state(reducer, tail_state, event.command)
                if tail_state.sha256 != event.after_state_hash:
                    raise IntegrityError("snapshot_tail_after_mismatch")
            if tail_state != state:
                raise IntegrityError("snapshot_tail_mismatch")
    except InvalidEvent:
        raise IntegrityError("replay_transition_failed") from None
    if state != records.current_state:
        raise IntegrityError("replayed_current_state_mismatch")
    return RecoveredState(records.head, state)
