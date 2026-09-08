"""Artificial integer reducer only. No account, prices, reservations or trading."""

from datetime import UTC, datetime, timedelta

from delayed_replay.audit_errors import InvalidEvent
from delayed_replay.audit_models import EventCommand, StreamIdentity
from delayed_replay.serialization import JsonObject


class CounterReducer:
    identity = "synthetic-counter-v1"

    def __init__(self):
        self.calls = 0

    def __call__(self, state: dict, command: EventCommand) -> dict:
        self.calls += 1  # Test instrumentation, not an input to the transition.
        payload = command.payload.to_dict()
        if (
            command.event_type != "add"
            or set(payload) != {"delta"}
            or type(payload["delta"]) is not int
        ):
            raise InvalidEvent("synthetic_payload_invalid")
        state["counter"] += payload["delta"]
        state["applied"].append(command.event_id)
        return state


def initial() -> JsonObject:
    return JsonObject.from_value({"counter": 0, "applied": []})


def identity(stream_id="synthetic-stream", **changes) -> StreamIdentity:
    values = dict(
        stream_id=stream_id,
        purpose="synthetic_test",
        config_hash="a" * 64,
        protocol_hash="b" * 64,
        source_identity="synthetic-source-1",
        reducer_identity=CounterReducer.identity,
        initial_state=initial(),
    )
    values.update(changes)
    return StreamIdentity.create(**values)


def command(number=1, *, stream=None, **changes) -> EventCommand:
    stream = stream or identity()
    values = dict(
        stream_id=stream.stream_id,
        stream_identity_hash=stream.genesis_hash,
        config_hash=stream.config_hash,
        reducer_identity=stream.reducer_identity,
        event_id=f"operation-{number}",
        event_type="add",
        market_decision_at=datetime(2024, 1, 1, tzinfo=UTC),
        replayed_at=datetime(2024, 4, 1, tzinfo=UTC) + timedelta(seconds=number),
        payload=JsonObject.from_value({"delta": number}),
        input_snapshot_hashes=("c" * 64,),
    )
    values.update(changes)
    return EventCommand(**values)
