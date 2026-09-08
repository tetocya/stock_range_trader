"""Immutable audit records, separate from Stage 1 price snapshots."""

from dataclasses import dataclass
from datetime import datetime

from .audit_errors import InvalidEvent
from .clock import ReplayClock
from .serialization import (
    JsonObject,
    digest,
    parse_time,
    require_fields,
    require_hash,
    require_sequence,
    require_text,
    time_text,
)


@dataclass(frozen=True, slots=True)
class StreamIdentity:
    stream_id: str
    purpose: str
    config_hash: str
    protocol_hash: str
    source_identity: str
    reducer_identity: str
    initial_state_hash: str
    genesis_hash: str
    storage_schema_version: str = "1"
    event_schema_version: str = "1"
    state_schema_version: str = "1"

    def __post_init__(self) -> None:
        for value in (self.stream_id, self.source_identity, self.reducer_identity):
            require_text(value)
        if self.purpose not in ("synthetic_test", "draft_audit"):
            raise InvalidEvent("unsupported_stream_purpose")
        for value in (
            self.config_hash,
            self.protocol_hash,
            self.initial_state_hash,
            self.genesis_hash,
        ):
            require_hash(value)
        if any(
            value != "1"
            for value in (
                self.storage_schema_version,
                self.event_schema_version,
                self.state_schema_version,
            )
        ):
            raise InvalidEvent("unsupported_schema")
        if digest(self.to_dict(include_hash=False)) != self.genesis_hash:
            raise InvalidEvent("genesis_hash_mismatch")

    def to_dict(self, *, include_hash: bool = True) -> dict:
        result = {name: getattr(self, name) for name in self.__dataclass_fields__}
        if not include_hash:
            result.pop("genesis_hash")
        return result

    @classmethod
    def create(
        cls,
        *,
        stream_id: str,
        purpose: str,
        config_hash: str,
        protocol_hash: str,
        source_identity: str,
        reducer_identity: str,
        initial_state: JsonObject,
    ) -> "StreamIdentity":
        if type(initial_state) is not JsonObject:
            raise InvalidEvent("state_object_required")
        values = dict(
            stream_id=stream_id,
            purpose=purpose,
            config_hash=config_hash,
            protocol_hash=protocol_hash,
            source_identity=source_identity,
            reducer_identity=reducer_identity,
            initial_state_hash=initial_state.sha256,
            storage_schema_version="1",
            event_schema_version="1",
            state_schema_version="1",
        )
        return cls(**values, genesis_hash=digest(values))

    @classmethod
    def from_dict(cls, value: dict) -> "StreamIdentity":
        require_fields(value, cls)
        return cls(**value)


@dataclass(frozen=True, slots=True)
class Head:
    sequence: int
    event_hash: str

    def __post_init__(self) -> None:
        require_sequence(self.sequence, zero=True)
        require_hash(self.event_hash)


@dataclass(frozen=True, slots=True)
class EventCommand:
    stream_id: str
    stream_identity_hash: str
    config_hash: str
    reducer_identity: str
    event_id: str
    event_type: str
    market_decision_at: datetime
    replayed_at: datetime
    payload: JsonObject
    input_snapshot_hashes: tuple[str, ...]
    correction_of: str | None = None
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for value in (
            self.stream_id,
            self.reducer_identity,
            self.event_id,
            self.event_type,
        ):
            require_text(value)
        for value in (self.stream_identity_hash, self.config_hash):
            require_hash(value)
        if self.schema_version != "1":
            raise InvalidEvent("unsupported_schema")
        # Do not retain a caller-supplied, potentially mutable tzinfo object.
        object.__setattr__(
            self, "market_decision_at", parse_time(time_text(self.market_decision_at))
        )
        object.__setattr__(self, "replayed_at", parse_time(time_text(self.replayed_at)))
        if self.market_decision_at > self.replayed_at:
            raise InvalidEvent("market_after_replay")
        if (
            type(self.payload) is not JsonObject
            or type(self.input_snapshot_hashes) is not tuple
        ):
            raise InvalidEvent("immutable_command_required")
        for value in self.input_snapshot_hashes:
            require_hash(value)
        if self.correction_of is not None:
            require_text(self.correction_of)

    @property
    def clock(self) -> ReplayClock:
        return ReplayClock(self.market_decision_at, self.replayed_at)

    def to_dict(self) -> dict:
        result = {name: getattr(self, name) for name in self.__dataclass_fields__}
        result.update(
            payload=self.payload.to_dict(),
            market_decision_at=time_text(self.market_decision_at),
            replayed_at=time_text(self.replayed_at),
            input_snapshot_hashes=list(self.input_snapshot_hashes),
        )
        return result

    @classmethod
    def from_dict(cls, value: dict) -> "EventCommand":
        require_fields(value, cls)
        copied = dict(value)
        copied["payload"] = JsonObject.from_value(copied["payload"])
        if type(copied["input_snapshot_hashes"]) is not list:
            raise InvalidEvent("invalid_input_references")
        copied["input_snapshot_hashes"] = tuple(copied["input_snapshot_hashes"])
        for name in ("market_decision_at", "replayed_at"):
            copied[name] = parse_time(copied[name])
        return cls(**copied)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    command: EventCommand
    sequence: int
    previous_event_hash: str
    before_state_hash: str
    after_state_hash: str
    event_hash: str

    def __post_init__(self) -> None:
        if type(self.command) is not EventCommand:
            raise InvalidEvent("command_required")
        require_sequence(self.sequence)
        for value in (
            self.previous_event_hash,
            self.before_state_hash,
            self.after_state_hash,
            self.event_hash,
        ):
            require_hash(value)
        if digest(self.to_dict(include_hash=False)) != self.event_hash:
            raise InvalidEvent("event_hash_mismatch")

    def to_dict(self, *, include_hash: bool = True) -> dict:
        result = dict(
            command=self.command.to_dict(),
            sequence=self.sequence,
            previous_event_hash=self.previous_event_hash,
            before_state_hash=self.before_state_hash,
            after_state_hash=self.after_state_hash,
        )
        if include_hash:
            result["event_hash"] = self.event_hash
        return result

    @classmethod
    def create(
        cls, command: EventCommand, head: Head, before: JsonObject, after: JsonObject
    ) -> "AuditEvent":
        values = dict(
            command=command.to_dict(),
            sequence=head.sequence + 1,
            previous_event_hash=head.event_hash,
            before_state_hash=before.sha256,
            after_state_hash=after.sha256,
        )
        return cls.from_dict(dict(values, event_hash=digest(values)))

    @classmethod
    def from_dict(cls, value: dict) -> "AuditEvent":
        require_fields(value, cls)
        copied = dict(value)
        copied["command"] = EventCommand.from_dict(copied["command"])
        return cls(**copied)


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    identity: StreamIdentity
    sequence: int
    event_hash: str
    state: JsonObject
    state_hash: str
    snapshot_hash: str
    state_schema_version: str = "1"

    def __post_init__(self) -> None:
        if (
            type(self.identity) is not StreamIdentity
            or type(self.state) is not JsonObject
        ):
            raise InvalidEvent("snapshot_types_invalid")
        require_sequence(self.sequence, zero=True)
        for value in (self.event_hash, self.state_hash, self.snapshot_hash):
            require_hash(value)
        if self.state_schema_version != self.identity.state_schema_version:
            raise InvalidEvent("unsupported_schema")
        if (
            self.state_hash != self.state.sha256
            or digest(self.to_dict(include_hash=False)) != self.snapshot_hash
        ):
            raise InvalidEvent("snapshot_hash_mismatch")

    def to_dict(self, *, include_hash: bool = True) -> dict:
        result = dict(
            identity=self.identity.to_dict(),
            sequence=self.sequence,
            event_hash=self.event_hash,
            state=self.state.to_dict(),
            state_hash=self.state_hash,
            state_schema_version=self.state_schema_version,
        )
        if include_hash:
            result["snapshot_hash"] = self.snapshot_hash
        return result

    @classmethod
    def create(
        cls, identity: StreamIdentity, head: Head, state: JsonObject
    ) -> "StateSnapshot":
        values = dict(
            identity=identity.to_dict(),
            sequence=head.sequence,
            event_hash=head.event_hash,
            state=state.to_dict(),
            state_hash=state.sha256,
            state_schema_version=identity.state_schema_version,
        )
        return cls.from_dict(dict(values, snapshot_hash=digest(values)))

    @classmethod
    def from_dict(cls, value: dict) -> "StateSnapshot":
        require_fields(value, cls)
        copied = dict(value)
        copied["identity"] = StreamIdentity.from_dict(copied["identity"])
        copied["state"] = JsonObject.from_value(copied["state"])
        return cls(**copied)


@dataclass(frozen=True, slots=True)
class CommitReceipt:
    event: AuditEvent

    def __post_init__(self) -> None:
        if type(self.event) is not AuditEvent:
            raise InvalidEvent("event_required")

    @property
    def head(self) -> Head:
        return Head(self.event.sequence, self.event.event_hash)
