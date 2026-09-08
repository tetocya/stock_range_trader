"""Subprocess fault worker: os._exit is a process crash, not a power-loss test."""

import os
import sys
from pathlib import Path

from delayed_replay_audit_helpers import CounterReducer, command, identity

from delayed_replay.event_store import EventStore
from delayed_replay.serialization import canonical_json


def main():
    operation, database, point = sys.argv[1:]
    reducer = CounterReducer()
    with EventStore.resume(Path(database), identity(), reducer) as store:
        if operation == "crash":

            def crash(at):
                if at == point:
                    os._exit(86)

            store.commit_event(
                command(),
                store.read().head,
                reducer,
                save_snapshot=True,
                _fault_hook=crash,
            )
            raise AssertionError("fault point was not reached")
        if operation != "recover":
            raise AssertionError("unknown worker operation")
        before_retry = store.read()
        calls = reducer.calls
        if before_retry.events:
            store.commit_event(command(), before_retry.head, reducer)
            assert reducer.calls == calls
        print(
            canonical_json(
                {
                    "counter": before_retry.current_state.to_dict()["counter"],
                    "events": len(before_retry.events),
                    "snapshots": len(before_retry.snapshots),
                    "head_hash": before_retry.head.event_hash,
                }
            )
        )


if __name__ == "__main__":
    main()
