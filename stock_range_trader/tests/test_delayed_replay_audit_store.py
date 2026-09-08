"""Real SQLite transactions: idempotency, concurrency, faults and isolation."""

import sqlite3
from dataclasses import replace

import pytest
from delayed_replay_audit_helpers import CounterReducer, command, identity, initial

from delayed_replay.audit_errors import (
    HeadConflict,
    IdempotencyConflict,
    IdentityMismatch,
    IntegrityError,
    InvalidEvent,
    StoreBusy,
)
from delayed_replay.event_store import EventStore
from delayed_replay.serialization import JsonObject


def test_empty_stream_append_same_market_time_and_real_db_constraints(tmp_path):
    path = tmp_path / "audit.sqlite3"
    reducer = CounterReducer()
    with EventStore.create(path, identity(), initial(), snapshot_initial=True) as store:
        empty = store.read()
        assert empty.head.sequence == 0
        assert empty.head.event_hash == identity().genesis_hash
        assert empty.current_state == initial()
        assert len(empty.snapshots) == 1
        for number in range(1, 4):
            prior = store.read()
            receipt = store.commit_event(
                command(number), prior.head, reducer, save_snapshot=True
            )
            assert receipt.event.sequence == number
            assert receipt.event.previous_event_hash == prior.head.event_hash
            assert receipt.event.before_state_hash == prior.current_state.sha256
            assert receipt.event.after_state_hash == store.read().current_state.sha256
        assert store.read().current_state.to_dict()["counter"] == 6
        assert reducer.calls == 3
        with sqlite3.connect(path) as raw:
            assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert raw.execute("PRAGMA synchronous").fetchone()[0] == 2
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute("INSERT INTO events SELECT * FROM events LIMIT 1")
        assert store.recover(reducer).state == store.read().current_state


def test_retry_returns_original_receipt_before_stale_head_and_without_reducer(tmp_path):
    path = tmp_path / "audit.sqlite3"
    reducer = CounterReducer()
    with EventStore.create(path, identity(), initial()) as store:
        old_head = store.read().head
        receipt = store.commit_event(command(), old_head, reducer)
        assert reducer.calls == 1
        assert (
            store.commit_event(command(), old_head, reducer, save_snapshot=True)
            == receipt
        )
        assert reducer.calls == 1
        assert store.read().snapshots == ()  # Retry cannot change snapshot history.
        assert store.lookup("operation-1") == receipt
        assert store.lookup("unknown") is None
        with pytest.raises(IdempotencyConflict):
            store.commit_event(
                command(payload=JsonObject.from_value({"delta": 999})),
                old_head,
                reducer,
            )
        with pytest.raises(IdempotencyConflict):
            store.commit_event(
                replace(command(), replayed_at=command(2).replayed_at),
                old_head,
                reducer,
            )
        with pytest.raises(HeadConflict):
            store.commit_event(command(2), old_head, reducer)
        assert store.read().head == receipt.head
    with EventStore.resume(path, identity(), reducer) as resumed:
        calls_after_recovery = reducer.calls
        assert resumed.commit_event(command(), old_head, reducer) == receipt
        assert reducer.calls == calls_after_recovery


def test_two_connections_same_head_and_bounded_busy(tmp_path):
    path = tmp_path / "audit.sqlite3"
    reducer = CounterReducer()
    with (
        EventStore.create(path, identity(), initial()) as first,
        EventStore.resume(path, identity(), reducer, busy_timeout_ms=10) as second,
    ):
        common_head = first.read().head
        first.commit_event(command(), common_head, reducer)
        with pytest.raises(HeadConflict):
            second.commit_event(command(2), common_head, reducer)
        assert len(second.read().events) == 1
        current_head = second.read().head
        first._connection.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(StoreBusy, match="^store_busy$"):
                second.commit_event(command(2), current_head, reducer)
        finally:
            first._connection.rollback()
        second.commit_event(command(2), current_head, reducer)
        assert first.read().head.sequence == 2


def test_retry_canonical_meaning_distinguishes_bool_from_integer(tmp_path):
    reducer = CounterReducer()
    with EventStore.create(tmp_path / "audit.sqlite3", identity(), initial()) as store:
        head = store.read().head
        receipt = store.commit_event(command(), head, reducer)
        with pytest.raises(IdempotencyConflict):
            store.commit_event(
                command(payload=JsonObject.from_value({"delta": True})), head, reducer
            )
        assert reducer.calls == 1
        assert store.read().head == receipt.head


@pytest.mark.parametrize(
    "point",
    [
        "before_event_insert",
        "after_event_insert",
        "after_state_update",
        "after_snapshot_insert",
        "after_commit",
    ],
)
def test_transaction_fault_points_are_atomic_and_response_loss_is_idempotent(
    tmp_path, point
):
    reducer = CounterReducer()
    path = tmp_path / "audit.sqlite3"

    def fail(at):
        if at == point:
            raise RuntimeError("injected-test-failure")

    with EventStore.create(path, identity(), initial(), snapshot_initial=True) as store:
        before = store.read()
        with pytest.raises(RuntimeError, match="injected"):
            store.commit_event(
                command(), before.head, reducer, save_snapshot=True, _fault_hook=fail
            )
        after = store.read()
        if point == "after_commit":
            assert after.head.sequence == 1
            assert len(after.snapshots) == 2
            calls = reducer.calls
            receipt = store.commit_event(command(), before.head, reducer)
            assert reducer.calls == calls
            assert receipt.head == after.head
        else:
            assert after == before
        assert initial().to_dict() == {"counter": 0, "applied": []}
    with EventStore.resume(path, identity(), reducer) as resumed:
        assert resumed.read() == after


def test_corrections_append_from_current_state_and_cannot_cross_streams(tmp_path):
    path = tmp_path / "audit.sqlite3"
    reducer = CounterReducer()
    other_identity = identity("other")
    with (
        EventStore.create(path, identity(), initial()) as store,
        EventStore.create(path, other_identity, initial()) as other,
    ):
        other.commit_event(
            command(stream=other_identity, event_id="other-only"),
            other.read().head,
            reducer,
        )
        first = store.commit_event(command(), store.read().head, reducer)
        store.commit_event(command(2), first.head, reducer)
        before = store.read()
        for target in ("missing", "other-only"):
            with pytest.raises(InvalidEvent, match="correction_target_missing"):
                store.commit_event(
                    command(3, correction_of=target), before.head, reducer
                )
        corrected = command(
            3, correction_of="operation-1", payload=JsonObject.from_value({"delta": -1})
        )
        receipt = store.commit_event(corrected, before.head, reducer)
        assert receipt.event.before_state_hash == before.current_state.sha256
        assert store.read().current_state.to_dict()["counter"] == 2
        assert store.read().events[0] == first.event
        assert store.commit_event(corrected, before.head, reducer) == receipt
        assert other.read().current_state.to_dict()["counter"] == 1


def test_state_exports_mutating_reducer_and_separate_runs_are_isolated(tmp_path):
    source = {"counter": 0, "applied": []}
    frozen = JsonObject.from_value(source)
    reducer = CounterReducer()
    with (
        EventStore.create(tmp_path / "one.sqlite3", identity(), frozen) as first,
        EventStore.create(tmp_path / "two.sqlite3", identity(), frozen) as second,
    ):
        source["applied"].append("outside")
        first.commit_event(command(), first.read().head, reducer, save_snapshot=True)
        records = first.read()
        records.current_state.to_dict()["applied"].append("outside")
        records.events[0].command.payload.to_dict()["delta"] = 999
        records.snapshots[0].state.to_dict()["counter"] = 999
        assert first.read() == records
        assert second.read().current_state == initial()
        assert frozen == initial()


def test_resume_missing_file_or_stream_never_creates_empty_stream(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(IntegrityError):
        EventStore.resume(missing, identity(), CounterReducer())
    assert not missing.exists()
    with EventStore.create(
        tmp_path / "existing.sqlite3", identity(), initial()
    ) as store:
        with pytest.raises(IdentityMismatch, match="stream_missing"):
            EventStore.resume(
                tmp_path / "existing.sqlite3", identity("other"), CounterReducer()
            )
        assert store.read().head.sequence == 0
        with pytest.raises(IdentityMismatch, match="stream_already_exists"):
            EventStore.create(tmp_path / "existing.sqlite3", identity(), initial())


@pytest.mark.parametrize(
    "changes",
    [
        {"config_hash": "d" * 64},
        {"protocol_hash": "e" * 64},
        {"source_identity": "another-source"},
        {"reducer_identity": "counter-v2"},
    ],
)
def test_resume_identity_mismatch_rejected(tmp_path, changes):
    path = tmp_path / "audit.sqlite3"
    EventStore.create(path, identity(), initial()).close()
    with pytest.raises(IdentityMismatch):
        EventStore.resume(path, identity(**changes), CounterReducer())


def test_reject_invalid_command_identity_clocks_bool_quantity_and_reducer(tmp_path):
    reducer = CounterReducer()
    with EventStore.create(tmp_path / "audit.sqlite3", identity(), initial()) as store:
        first = store.commit_event(command(2), store.read().head, reducer)
        for bad in (
            command(stream=identity("other")),
            command(config_hash="d" * 64),
            command(reducer_identity="v2"),
        ):
            with pytest.raises(IdentityMismatch):
                store.commit_event(bad, first.head, reducer)
        for bad in (
            command(1),
            command(
                3, market_decision_at=command().market_decision_at.replace(year=2023)
            ),
        ):
            with pytest.raises(InvalidEvent, match="clock_reversal"):
                store.commit_event(bad, first.head, reducer)
        with pytest.raises(InvalidEvent, match="reducer_rejected_transition"):
            store.commit_event(
                command(3, payload=JsonObject.from_value({"delta": True})),
                first.head,
                reducer,
            )
        with pytest.raises(IdentityMismatch):
            store.commit_event(command(3), first.head, lambda state, cmd: state)
        assert store.read().head == first.head


def test_failed_reducer_does_not_persist_mutated_input_or_exception_text(tmp_path):
    class Broken(CounterReducer):
        def __call__(self, state, cmd):
            state["counter"] = 999
            raise RuntimeError("secret-fake-key-and-private-path")

    path = tmp_path / "audit.sqlite3"
    with EventStore.create(path, identity(), initial()) as store:
        before = store.read()
        with pytest.raises(InvalidEvent, match="^reducer_rejected_transition$"):
            store.commit_event(command(), before.head, Broken())
        assert store.read() == before
    assert b"secret-fake-key" not in path.read_bytes()
