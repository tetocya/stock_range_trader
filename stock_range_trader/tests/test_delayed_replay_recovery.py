"""Real DB recovery, deliberate corruption and synchronized process termination."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from delayed_replay_audit_helpers import CounterReducer, command, identity, initial

from delayed_replay.audit_errors import IdentityMismatch, IntegrityError
from delayed_replay.event_store import EventStore
from delayed_replay.serialization import canonical_json


def populate(path, *, snapshots=(), stop=4):
    reducer = CounterReducer()
    with EventStore.create(
        path, identity(), initial(), snapshot_initial=0 in snapshots
    ) as store:
        for number in range(1, stop + 1):
            store.commit_event(
                command(number),
                store.read().head,
                reducer,
                save_snapshot=number in snapshots,
            )
        return store.read()


@pytest.mark.parametrize("snapshots", [(), (0,), (1,), (2,), (4,), (0, 2, 4)])
def test_full_replay_and_snapshot_tail_agree_without_mutating_storage(
    tmp_path, snapshots
):
    path = tmp_path / "audit.sqlite3"
    expected = populate(path, snapshots=snapshots)
    with sqlite3.connect(path) as raw:
        before = tuple(raw.iterdump())
    reducer = CounterReducer()
    with EventStore.resume(
        path, identity(), reducer, expected_head=expected.head
    ) as resumed:
        assert resumed.read() == expected
        recovered = resumed.recover(reducer)
        assert recovered.state == expected.current_state
        assert recovered.head == expected.head
    with sqlite3.connect(path) as raw:
        assert tuple(raw.iterdump()) == before
    assert reducer.calls == 2 * (4 + (4 - max(snapshots) if snapshots else 0))


@pytest.mark.parametrize("restart_at,snapshot_at", [(1, None), (2, 2), (3, 1)])
def test_continuous_and_restarted_logical_events_state_and_head_match(
    tmp_path, restart_at, snapshot_at
):
    reference = populate(
        tmp_path / "continuous.sqlite3",
        snapshots=(() if snapshot_at is None else (snapshot_at,)),
    )
    path = tmp_path / "restart.sqlite3"
    reducer = CounterReducer()
    store = EventStore.create(path, identity(), initial())
    try:
        for number in range(1, 5):
            store.commit_event(
                command(number),
                store.read().head,
                reducer,
                save_snapshot=number == snapshot_at,
            )
            if number == restart_at:
                store.close()
                store = EventStore.resume(path, identity(), reducer)
        actual = store.read()
        assert actual.events == reference.events
        assert actual.head == reference.head
        assert actual.current_state == reference.current_state
        assert actual.snapshots == reference.snapshots
    finally:
        store.close()


def corrupt_json(raw, table, column, sequence, mutate):
    value = json.loads(
        raw.execute(
            f"SELECT {column} FROM {table} WHERE sequence=?", (sequence,)
        ).fetchone()[0]
    )
    mutate(value)
    raw.execute(
        f"UPDATE {table} SET {column}=? WHERE sequence=?",
        (canonical_json(value), sequence),
    )


@pytest.mark.parametrize(
    "damage",
    [
        "payload",
        "event_hash",
        "previous_hash",
        "delete_middle",
        "sequence_index",
        "head",
        "current_state",
        "current_hash",
        "initial_state",
        "snapshot",
        "snapshot_state_hash",
        "snapshot_hash",
        "snapshot_index",
        "tail_event",
        "identity",
        "schema",
        "missing_command_schema",
    ],
)
def test_corruption_is_rejected_not_repaired_including_after_latest_snapshot(
    tmp_path, damage
):
    path = tmp_path / "audit.sqlite3"
    populate(path, snapshots=(0, 2))
    with sqlite3.connect(path) as raw:
        if damage == "payload":
            corrupt_json(
                raw,
                "events",
                "event_json",
                1,
                lambda v: v["command"]["payload"].update(delta=999),
            )
        elif damage == "event_hash":
            corrupt_json(
                raw, "events", "event_json", 1, lambda v: v.update(event_hash="f" * 64)
            )
        elif damage == "previous_hash":
            corrupt_json(
                raw,
                "events",
                "event_json",
                2,
                lambda v: v.update(previous_event_hash="f" * 64),
            )
        elif damage == "delete_middle":
            raw.execute("DELETE FROM events WHERE sequence=2")
        elif damage == "sequence_index":
            raw.execute("UPDATE events SET sequence=9 WHERE sequence=2")
        elif damage == "head":
            raw.execute("UPDATE streams SET head_hash=?", ("f" * 64,))
        elif damage == "current_state":
            raw.execute("UPDATE streams SET current_json=?", ('{"counter":999}',))
        elif damage == "current_hash":
            raw.execute("UPDATE streams SET current_hash=?", ("f" * 64,))
        elif damage == "initial_state":
            raw.execute("UPDATE streams SET initial_json='{}'")
        elif damage == "snapshot":
            corrupt_json(
                raw,
                "snapshots",
                "snapshot_json",
                2,
                lambda v: v["state"].update(counter=999),
            )
        elif damage == "snapshot_state_hash":
            corrupt_json(
                raw,
                "snapshots",
                "snapshot_json",
                2,
                lambda v: v.update(state_hash="f" * 64),
            )
        elif damage == "snapshot_hash":
            corrupt_json(
                raw,
                "snapshots",
                "snapshot_json",
                2,
                lambda v: v.update(snapshot_hash="f" * 64),
            )
        elif damage == "snapshot_index":
            raw.execute("UPDATE snapshots SET sequence=3 WHERE sequence=2")
        elif damage == "tail_event":
            corrupt_json(
                raw,
                "events",
                "event_json",
                4,
                lambda v: v["command"]["payload"].update(delta=999),
            )
        elif damage == "identity":
            raw.execute(
                "UPDATE streams SET identity_json=?",
                (
                    canonical_json(
                        identity(source_identity="tampered-source").to_dict()
                    ),
                ),
            )
        elif damage == "schema":
            raw.execute("PRAGMA user_version=2")
        elif damage == "missing_command_schema":
            corrupt_json(
                raw,
                "events",
                "event_json",
                1,
                lambda v: v["command"].pop("schema_version"),
            )
    with sqlite3.connect(path) as raw:
        damaged = tuple(raw.iterdump())
    with pytest.raises((IntegrityError, IdentityMismatch)):
        EventStore.resume(path, identity(), CounterReducer())
    with sqlite3.connect(path) as raw:
        assert tuple(raw.iterdump()) == damaged


def test_same_identity_but_changed_reducer_semantics_fail_full_replay(tmp_path):
    class Changed(CounterReducer):
        def __call__(self, state, cmd):
            result = super().__call__(state, cmd)
            result["counter"] += 1
            return result

    path = tmp_path / "audit.sqlite3"
    populate(path, snapshots=(2,))
    with pytest.raises(IntegrityError, match="replayed_after_state_mismatch"):
        EventStore.resume(path, identity(), Changed())


def test_external_head_detects_coherent_old_backup_but_no_anchor_cannot(tmp_path):
    path, old = tmp_path / "current.sqlite3", tmp_path / "old.sqlite3"
    prefix = populate(path, stop=2)
    shutil.copyfile(path, old)
    with EventStore.resume(path, identity(), CounterReducer()) as store:
        receipt = store.commit_event(command(3), store.read().head, CounterReducer())
    with pytest.raises(IntegrityError, match="external_head_mismatch"):
        EventStore.resume(old, identity(), CounterReducer(), expected_head=receipt.head)
    with EventStore.resume(old, identity(), CounterReducer()) as resumed:
        assert (
            resumed.read().head == prefix.head
        )  # Documented limit, not corruption repair.


@pytest.mark.parametrize(
    "point", ["after_state_update", "after_snapshot_insert", "after_commit"]
)
def test_subprocess_termination_and_independent_process_recovery(tmp_path, point):
    path = tmp_path / "audit.sqlite3"
    EventStore.create(path, identity(), initial(), snapshot_initial=True).close()
    worker = Path(__file__).with_name("delayed_replay_crash_worker.py")
    environment = dict(
        os.environ,
        PYTHONPATH=str(Path(__file__).parents[1]),
        RUN_LIVE_JQUANTS_TESTS="0",
        RUN_LIVE_YFINANCE_TESTS="0",
    )
    crashed = subprocess.run(
        [sys.executable, str(worker), "crash", str(path), point],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert crashed.returncode == 86, crashed.stderr
    recovered = subprocess.run(
        [sys.executable, str(worker), "recover", str(path), point],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert recovered.returncode == 0, recovered.stderr
    result = json.loads(recovered.stdout)
    committed = point == "after_commit"
    assert result["events"] == int(committed)
    assert result["counter"] == int(committed)
    assert result["snapshots"] == 1 + int(committed)
    with EventStore.resume(path, identity(), CounterReducer()) as store:
        assert store.read().head.event_hash == result["head_hash"]
