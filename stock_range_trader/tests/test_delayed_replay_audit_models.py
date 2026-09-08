"""Strict persistent serialization and immutable audit types."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, tzinfo

import pytest
from delayed_replay_audit_helpers import command, identity, initial

from delayed_replay.audit_errors import InvalidEvent
from delayed_replay.audit_models import (
    AuditEvent,
    EventCommand,
    Head,
    StateSnapshot,
    StreamIdentity,
)
from delayed_replay.serialization import (
    JsonObject,
    canonical_json,
    decimal_text,
    digest,
    parse_json,
)


@pytest.mark.parametrize(
    "value",
    [
        0.0,
        1.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        object(),
        {1: "bad"},
        (1, 2),
        {1, 2},
        2**63,
        -(2**63) - 1,
    ],
)
def test_unsupported_json_values_rejected_recursively(value):
    with pytest.raises(InvalidEvent):
        JsonObject.from_value({"nested": [value]})


@pytest.mark.parametrize(
    "encoded",
    [
        '{"a":1,"a":2}',
        '{"a":NaN}',
        '{"a":1.0}',
        '{"a": 1}',
        "[]",
        '{"x":Infinity}',
        '{"b":1,"a":2}',
    ],
)
def test_noncanonical_or_invalid_serialized_inputs_rejected(encoded):
    with pytest.raises(InvalidEvent):
        JsonObject(encoded)


def test_json_key_order_null_boolean_array_and_exact_decimal_contract():
    assert digest({"b": None, "a": [True, 3, "日本"]}) == digest(
        {"a": [True, 3, "日本"], "b": None}
    )
    assert digest({"a": [1, 2]}) != digest({"a": [2, 1]})
    assert parse_json(canonical_json({"flag": True}))["flag"] is True
    assert decimal_text("200000.000") == "200000"
    assert decimal_text("-0.00") == "0"
    assert (
        decimal_text("1.234567890123456789012345678901")
        == "1.234567890123456789012345678901"
    )
    for value in (0.1, "NaN", "Infinity", "1e9999"):
        with pytest.raises(InvalidEvent):
            decimal_text(value)


def test_event_snapshot_and_state_are_detached_from_inputs_and_exports():
    source = {"nested": [{"value": 1}]}
    state = JsonObject.from_value(source)
    source["nested"][0]["value"] = 999
    stream = identity(initial_state=state)
    cmd = command(stream=stream, payload=state)
    head = Head(0, stream.genesis_hash)
    event = AuditEvent.create(cmd, head, state, state)
    snap = StateSnapshot.create(stream, head, state)
    event_hash, snapshot_hash, state_hash = (
        event.event_hash,
        snap.snapshot_hash,
        state.sha256,
    )
    for exported in (
        state.to_dict(),
        event.command.payload.to_dict(),
        snap.state.to_dict(),
    ):
        exported["nested"][0]["value"] = 500
    event.to_dict()["command"]["payload"]["nested"].clear()
    snap.to_dict()["state"]["nested"].clear()
    assert state.to_dict() == {"nested": [{"value": 1}]}
    assert (event.event_hash, snap.snapshot_hash, state.sha256) == (
        event_hash,
        snapshot_hash,
        state_hash,
    )
    assert AuditEvent.from_dict(event.to_dict()) == event
    assert StateSnapshot.from_dict(snap.to_dict()) == snap
    with pytest.raises(FrozenInstanceError):
        state.encoded = "{}"


@pytest.mark.parametrize("sequence", [True, False, -1, 1.0, "1", None, 2**63])
def test_sequence_is_not_bool_or_other_type(sequence):
    with pytest.raises(InvalidEvent):
        Head(sequence, "a" * 64)


def test_event_snapshot_and_identity_recalculate_supplied_hashes():
    stream = identity()
    event = AuditEvent.create(
        command(), Head(0, stream.genesis_hash), initial(), initial()
    )
    snap = StateSnapshot.create(stream, Head(0, stream.genesis_hash), initial())
    for record, changes in (
        (stream, {"genesis_hash": "0" * 64}),
        (event, {"event_hash": "0" * 64}),
        (event, {"after_state_hash": "f" * 64}),
        (snap, {"snapshot_hash": "0" * 64}),
        (snap, {"state": JsonObject.from_value({"changed": True})}),
    ):
        with pytest.raises(InvalidEvent):
            replace(record, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "2"},
        {"event_id": ""},
        {"config_hash": "bad"},
        {"payload": {}},
        {"input_snapshot_hashes": ["a" * 64]},
        {"market_decision_at": datetime(2024, 1, 1)},
        {"market_decision_at": True},
        {"correction_of": ""},
    ],
)
def test_bad_commands_rejected(changes):
    with pytest.raises(InvalidEvent):
        command(**changes)


@pytest.mark.parametrize(
    "field", ["storage_schema_version", "event_schema_version", "state_schema_version"]
)
def test_unknown_identity_schema_rejected(field):
    with pytest.raises(InvalidEvent):
        replace(identity(), **{field: "2"})


def test_all_record_fields_required_on_read_and_unknown_fields_rejected():
    stream = identity()
    cmd = command()
    event = AuditEvent.create(cmd, Head(0, stream.genesis_hash), initial(), initial())
    snap = StateSnapshot.create(stream, Head(0, stream.genesis_hash), initial())
    for record, cls in (
        (stream, StreamIdentity),
        (cmd, EventCommand),
        (event, AuditEvent),
        (snap, StateSnapshot),
    ):
        encoded = record.to_dict()
        for key in encoded:
            damaged = dict(encoded)
            damaged.pop(key)
            with pytest.raises(InvalidEvent):
                cls.from_dict(damaged)
        with pytest.raises(InvalidEvent):
            cls.from_dict(dict(encoded, unknown=True))


def test_command_semantic_hash_includes_clock_refs_identity_and_correction():
    cmd = command()
    original = digest(cmd.to_dict())
    for changed in (
        replace(cmd, input_snapshot_hashes=("d" * 64,)),
        replace(cmd, correction_of="prior-operation"),
        replace(cmd, config_hash="f" * 64),
        command(2),
    ):
        assert digest(changed.to_dict()) != original


def test_command_does_not_retain_mutable_timezone_input():
    class MutableTimezone(tzinfo):
        offset = 9

        def utcoffset(self, dt):
            return timedelta(hours=self.offset)

        def dst(self, dt):
            return timedelta(0)

    zone = MutableTimezone()
    original = command(market_decision_at=datetime(2024, 1, 1, tzinfo=zone))
    before = original.to_dict()
    zone.offset = 8
    assert original.to_dict() == before
