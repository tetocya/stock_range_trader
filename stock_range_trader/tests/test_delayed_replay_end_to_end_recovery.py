"""Transaction/cursor cuts through the real runner and final Stage 5 exports."""

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from delayed_replay_e2e_helpers import (
    artifact_bytes,
    checkpoints,
    export,
    forbidden,
)
from delayed_replay_e2e_helpers import baseline as baseline
from delayed_replay_e2e_helpers import network_guard as network_guard
from delayed_replay_stage4_helpers import WALL

from delayed_replay.audit_errors import InvalidEvent
from delayed_replay.checkpoint_report import write_registration_candidate
from delayed_replay.event_store import EventStore
from delayed_replay.replay_engine import ReplayEngine
from delayed_replay.replay_state import ReplayReducer

pytestmark = pytest.mark.usefixtures("network_guard")


def seed(baseline, path, count):
    records = baseline.records
    store = EventStore.create(
        path, records.identity, records.initial_state, snapshot_initial=True
    )
    try:
        for event in records.events[:count]:
            store.commit_event(
                event.command,
                store.read().head,
                ReplayReducer(),
                save_snapshot=event.command.payload.to_dict()["phase"] == "select",
            )
    finally:
        store.close()


@pytest.mark.parametrize("phase", ["select", "open", "mark", "decide"])
def test_committed_phase_lost_receipt_resume_to_identical_artifacts(
    baseline, tmp_path, phase
):
    root = tmp_path / phase
    root.mkdir()
    write_registration_candidate(baseline.candidate, root / "candidate")
    cut = next(
        i
        for i, e in enumerate(baseline.records.events)
        if e.command.payload.to_dict()["index"] == 14
        and e.command.payload.to_dict()["phase"] == phase
    )
    seed(baseline, root / "replay.sqlite", cut)
    engine = ReplayEngine.resume(root / "replay.sqlite", baseline.plan)

    def fault(where):
        if where == "after_commit":
            raise RuntimeError("lost_receipt")

    try:
        with pytest.raises(RuntimeError, match="lost_receipt"):
            engine.advance(WALL, fault=fault)
        committed = engine.store.read()
        assert committed.head.sequence == cut + 1
    finally:
        engine.store.close()
    with (
        patch.object(
            type(baseline.plan.monthly.evaluator), "evaluate_validation", forbidden
        ),
        patch.object(type(baseline.plan.monthly.selector), "select", forbidden),
    ):
        engine = ReplayEngine.resume(
            root / "replay.sqlite", baseline.plan, expected_head=committed.head
        )
        try:
            # Exact retransmission after commit-before-receipt: no extra event/effect.
            engine.commit(committed.events[-1].command)
            assert engine.store.read() == committed
            engine.run(WALL)
            actual = export(engine, root, baseline.candidate)
            assert actual.state == baseline.state
            assert actual.records.head == baseline.records.head
            assert actual.pair == baseline.pair
            assert artifact_bytes(root) == artifact_bytes(baseline.bundle.parent)
        finally:
            engine.store.close()


def test_open_batch_fault_rolls_back_account_and_cursor(baseline, tmp_path):
    # Find a real batch with at least two pending fills, not fabricated orders.
    from delayed_replay_e2e_helpers import timeline

    from delayed_replay import account_reducer

    cut = next(
        i + 1
        for i, (_, s) in enumerate(timeline(baseline.records))
        if s["cursor"]["phase"] == "open"
        and sum(o["status"] == "pending" for o in s["account"]["orders"].values()) >= 2
    )
    root = tmp_path / "batch"
    root.mkdir()
    seed(baseline, root / "replay.sqlite", cut)
    engine = ReplayEngine.resume(root / "replay.sqlite", baseline.plan)
    before = engine.store.read()
    original, calls = account_reducer.evaluate_fill, []

    def interrupted(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if len(calls) == 2:
            raise RuntimeError("inside_open_batch")
        return result

    try:
        with (
            patch.object(account_reducer, "evaluate_fill", interrupted),
            pytest.raises(InvalidEvent, match="reducer_rejected_transition"),
        ):
            engine.advance(WALL)
        assert len(calls) == 2 and calls[0].filled
        assert engine.store.read() == before
    finally:
        engine.store.close()
    engine = ReplayEngine.resume(
        root / "replay.sqlite", baseline.plan, expected_head=before.head
    )
    try:
        engine.run(WALL)
        actual = export(engine, root, baseline.candidate)
        assert actual.state == baseline.state and actual.pair == baseline.pair
    finally:
        engine.store.close()


def test_one_month_frozen_evidence_survives_real_suffix_run(baseline, tmp_path):
    root = tmp_path / "one-month"
    root.mkdir()
    seed(baseline, root / "replay.sqlite", 42)
    engine = ReplayEngine.resume(root / "replay.sqlite", baseline.plan)
    one = checkpoints(baseline.plan, engine.store.read())[0]
    assert one.valuation.status == "available" and one.equity == "227859"
    engine.store.close()
    engine = ReplayEngine.resume(root / "replay.sqlite", baseline.plan)
    try:
        engine.run(WALL)
        actual = export(engine, root, baseline.candidate, previous={1: one})
        assert actual.pair[0] == one
        assert actual.pair[1] == baseline.pair[1]
        assert actual.state == baseline.state
        assert actual.result.to_dict()["label"] == "INCONCLUSIVE"
        # Earlier frozen provenance intentionally differs from full-head evidence.
        assert actual.pair[0].sha256 != baseline.pair[0].sha256
    finally:
        engine.store.close()


def test_different_event_id_same_business_operation_no_double_effect(
    baseline, tmp_path
):
    root = tmp_path / "retry"
    root.mkdir()
    seed(baseline, root / "replay.sqlite", 86)  # August Open committed.
    engine = ReplayEngine.resume(root / "replay.sqlite", baseline.plan)
    try:
        before = engine.store.read()
        command = before.events[-1].command
        assert command.payload.to_dict()["phase"] == "open"
        engine.commit(replace(command, event_id="alternate-business-retry"))
        assert engine.state.value == before.current_state
        assert engine.store.read().head.sequence == before.head.sequence + 1
        engine.run(WALL)
        actual = export(engine, root, baseline.candidate)
        assert actual.state == baseline.state
        assert [(c.equity, c.completed_trades) for c in actual.pair] == [
            (c.equity, c.completed_trades) for c in baseline.pair
        ]
        assert actual.result.to_dict()["label"] == baseline.result.to_dict()["label"]
    finally:
        engine.store.close()


def test_abrupt_subprocess_restart_without_reselection(baseline, tmp_path):
    worker = Path(__file__).with_name("delayed_replay_e2e_helpers.py")
    root = tmp_path / "subprocess"
    env = {
        **os.environ,
        "PYTHONPATH": str(worker.parent.parent),
        "RUN_LIVE_JQUANTS_TESTS": "0",
        "RUN_LIVE_YFINANCE_TESTS": "0",
    }
    for mode, expected in (("cut", 23), ("resume", 0)):
        result = subprocess.run(
            [sys.executable, str(worker), str(root), mode],
            cwd=worker.parent.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == expected, result.stdout + result.stderr
    engine = ReplayEngine.resume(root / "replay.sqlite", baseline.plan)
    try:
        assert engine.state.to_dict() == baseline.state
        assert engine.store.read().head == baseline.records.head
    finally:
        engine.store.close()
    assert artifact_bytes(root) == artifact_bytes(baseline.bundle.parent)
