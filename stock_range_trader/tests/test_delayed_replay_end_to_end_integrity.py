"""Boundary, missing-input and report connections, all explicitly synthetic."""

import json
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from delayed_replay_e2e_helpers import artifact_bytes, forbidden, run, timeline
from delayed_replay_e2e_helpers import baseline as baseline
from delayed_replay_e2e_helpers import network_guard as network_guard
from delayed_replay_stage4_helpers import WALL, fixture_plan, reprice
from test_delayed_replay_stage5_judge import NOW, checkpoint

from delayed_replay.checkpoint_report import (
    CHECKPOINTS_FILENAME,
    RESULT_FILENAME,
    write_checkpoint_results,
)
from delayed_replay.protocol_judge import ProtocolJudge
from delayed_replay.replay_engine import ReplayEngine

pytestmark = pytest.mark.usefixtures("network_guard")


@pytest.mark.parametrize(
    "one,three,label,reason",
    [
        (
            checkpoint(1, "188000"),
            checkpoint(3, None, value_state="unavailable_external"),
            "FAIL",
            "early_downside_gate_breached",
        ),
        (
            checkpoint(1, "188000"),
            checkpoint(3, "200001", count=(2, 7)),
            "FAIL",
            "early_downside_gate_breached",
        ),
        (
            checkpoint(1, "188000"),
            checkpoint(
                3, None, value_state="unavailable_external", sample_state="pending"
            ),
            "FAIL",
            "early_downside_gate_breached",
        ),
        (
            checkpoint(1, "188000"),
            checkpoint(
                3,
                None,
                value_state="unavailable_external",
                sample_state="unavailable_external",
            ),
            "FAIL",
            "early_downside_gate_breached",
        ),
        (
            checkpoint(1, None, value_state="unavailable_external"),
            checkpoint(3, "220000"),
            "INCONCLUSIVE",
            "one_month_return_unavailable",
        ),
        (
            checkpoint(1, "190000"),
            checkpoint(3, None, value_state="unavailable_external"),
            "INCONCLUSIVE",
            "three_month_return_unavailable",
        ),
        (
            checkpoint(1, "188000"),
            checkpoint(3, None, value_state="invalid"),
            "INVALID",
            "invalid_evidence",
        ),
    ],
)
def test_typed_evidence_judge_report_precedence(tmp_path, one, three, label, reason):
    # Deliberately NOT real-ledger E2E; keep unit evidence and replay claims separate.
    result = ProtocolJudge().evaluate(one, three, now=NOW)
    out = write_checkpoint_results(one, three, result, tmp_path / "typed-only")
    saved = json.loads((out / RESULT_FILENAME).read_text())
    assert saved["label"] == label and saved["reason_codes"] == [reason]
    assert saved["evidence_kind"] == "synthetic"
    assert saved["registration_status"] == "draft_not_registered"
    assert saved["checkpoints"] == [one.to_dict(), three.to_dict()]


def test_report_failure_and_collision_leave_real_ledger_unchanged(baseline, tmp_path):
    engine = ReplayEngine.resume(baseline.path, baseline.plan)
    try:
        before = engine.store.read()
        existing = artifact_bytes(baseline.bundle.parent)

        def fail(name):
            if name == CHECKPOINTS_FILENAME:
                raise RuntimeError("temporary_write_failure")

        with (
            patch.object(ReplayEngine, "run", forbidden),
            patch.object(ReplayEngine, "advance", forbidden),
            patch.object(
                type(baseline.plan.monthly.evaluator), "evaluate_validation", forbidden
            ),
            patch.object(type(baseline.plan.monthly.selector), "select", forbidden),
            patch.object(type(engine.store), "commit_event", forbidden),
        ):
            with pytest.raises(RuntimeError, match="temporary_write_failure"):
                write_checkpoint_results(
                    *baseline.pair, baseline.result, tmp_path / "failed", fault=fail
                )
            assert not list(tmp_path.iterdir())
            with pytest.raises(FileExistsError):
                write_checkpoint_results(
                    *baseline.pair, baseline.result, baseline.bundle
                )
        assert engine.store.read() == before
        assert artifact_bytes(baseline.bundle.parent) == existing
    finally:
        engine.store.close()


def test_in_period_failed_gate_stays_pending_in_report(tmp_path):
    from datetime import UTC, datetime

    now = datetime(2024, 3, 1, tzinfo=UTC)
    one = checkpoint(1, "188000")
    one = replace(
        one,
        valuation=replace(one.valuation, observed_at=now, finalized_at=now),
        samples=replace(one.samples, observed_at=now, finalized_at=now),
    )
    three = checkpoint(3, None, value_state="pending", sample_state="pending")
    three = replace(
        three,
        valuation=replace(three.valuation, observed_at=None),
        samples=replace(three.samples, observed_at=None),
    )
    result = ProtocolJudge().evaluate(one, three, now=now)
    out = write_checkpoint_results(one, three, result, tmp_path / "pending")
    saved = json.loads((out / RESULT_FILENAME).read_text())
    assert saved["label"] == "PENDING" and saved["outcome"] == "N/A"
    assert saved["one_month_gate"] == "failed"
    assert saved["reason_codes"] == ["three_month_period_not_complete"]


def test_next_open_positive_control_keeps_previous_quantity_and_reservation(
    baseline, tmp_path
):
    order_id, order = next(
        (k, o)
        for k, o in baseline.state["account"]["orders"].items()
        if o["status"] == "filled" and o["request"]["side"] == "BUY"
    )
    req = order["request"]
    plan = baseline.plan
    original = plan.market.open_snapshots[0]
    changed = replace(
        plan,
        market=replace(
            plan.market,
            open_snapshots=(
                replace(
                    original,
                    evidence=tuple(
                        replace(e, open_price="10000")
                        if e.instrument_id == req["instrument_id"]
                        and e.session == req["target_session"]
                        else e
                        for e in original.evidence
                    ),
                ),
            ),
        ),
    )
    actual = run(tmp_path / "open-change", changed)

    def accepted(records):
        return next(
            s["account"]["orders"][order_id]
            for _, s in timeline(records)
            if order_id in s["account"]["orders"]
        )

    assert accepted(actual.records) == accepted(baseline.records)
    assert actual.state["account"]["orders"][order_id]["status"] == "rejected"
    assert actual.state["epochs"] == baseline.state["epochs"]
    assert actual.pair[1].equity != baseline.pair[1].equity


def test_real_no_candidate_month_continues_exits_and_checkpoint(tmp_path):
    # Reuse Stage 4's explicit fixture variant; selection still runs actual scores.
    plan = fixture_plan()
    snapshot = plan.market.snapshots[0]
    bars = tuple(
        replace(b, raw_ohlcv=(50.0, 51.0, 49.0, 50.0, 100000.0))
        if b.symbol == "A" and b.session_date.month == 7
        else b
        for b in snapshot.observations
        if b.session_date != date(2024, 7, 31)
    )
    opens = tuple(
        replace(e, open_price="50")
        if e.instrument_id == "A" and e.session.startswith("2024-07")
        else e
        for e in plan.market.open_snapshots[0].evidence
        if e.session != "2024-07-31"
    )
    plan = replace(
        plan,
        calendar=replace(
            plan.calendar,
            sessions=tuple(
                s for s in plan.calendar.sessions if s.day != date(2024, 7, 31)
            ),
        ),
        monthly=replace(
            plan.monthly,
            selector=replace(
                plan.monthly.selector,
                policy=replace(
                    plan.monthly.selector.policy, minimum_finite_sharpe_count=2
                ),
            ),
        ),
        market=replace(
            plan.market,
            snapshots=(reprice(snapshot, observations=bars),),
            open_snapshots=(replace(plan.market.open_snapshots[0], evidence=opens),),
        ),
    )
    actual = run(tmp_path / "no-candidate", plan)
    assert actual.state["epochs"]["2024-08-01"]["status"] == "no_eligible_candidate"
    orders = actual.state["account"]["orders"]
    assert not any(
        k.startswith("2024-08") and o["request"]["side"] == "BUY"
        for k, o in orders.items()
    )
    carried = [
        o
        for o in orders.values()
        if o["request"]["side"] == "BUY"
        and o["request"]["target_session"] == "2024-08-01"
    ]
    assert carried and all(o["status"] == "filled" for o in carried)
    assert any(
        k.startswith("2024-08")
        and o["status"] == "filled"
        and o["request"]["side"] == "SELL"
        for k, o in orders.items()
    )
    states = list(timeline(actual.records))
    for (_, before), (_, after) in zip(states, states[1:], strict=False):
        if after["visible"]["phase"] == "select":
            assert after["account"] == before["account"]
            assert after["position_states"] == before["position_states"]
    assert actual.result.to_dict()["label"] == "INCONCLUSIVE"


def test_fixed_snapshot_wait_resumes_without_partial_mark(baseline, tmp_path):
    plan = baseline.plan
    snapshot = plan.market.snapshots[0]
    late = tuple(
        b
        for b in snapshot.observations
        if b.symbol == "A" and b.session_date == date(2024, 6, 24)
    )
    changed = replace(
        plan,
        market=replace(
            plan.market,
            snapshots=(
                reprice(
                    snapshot,
                    observations=tuple(
                        b for b in snapshot.observations if b not in late
                    ),
                ),
                reprice(
                    snapshot, observations=late, fetched_at=WALL + timedelta(days=1)
                ),
            ),
        ),
    )
    root = tmp_path / "waiting"
    root.mkdir()
    engine = ReplayEngine.create(root / "replay.sqlite", changed)
    try:
        waiting = engine.run(WALL).to_dict()
        assert waiting["cursor"] == {"index": 4, "phase": "mark"}
        assert waiting["account"]["valuation"]["session"] != "2024-06-24"
        engine.advance(WALL)
        assert engine.state.to_dict()["account"] == waiting["account"]
    finally:
        engine.store.close()
    engine = ReplayEngine.resume(root / "replay.sqlite", changed)
    try:
        engine.run(WALL + timedelta(days=1))
        # Evidence collection must use a wall time at/after this resumed replay.
        from delayed_replay_e2e_helpers import policies

        from delayed_replay.checkpoint_models import CheckpointSchedule
        from delayed_replay.checkpoints import LedgerCheckpointSource

        records = engine.store.read()
        pair = LedgerCheckpointSource(records, records.head).collect(
            CheckpointSchedule(plan.policy.run_start, plan.calendar),
            policies(),
            now=WALL + timedelta(days=1),
        )
        assert [(c.equity, c.completed_trades) for c in pair] == [
            (c.equity, c.completed_trades) for c in baseline.pair
        ]
        marks = [
            e
            for e in records.events
            if e.command.payload.to_dict()["phase"] == "mark"
            and e.command.payload.to_dict()["action"] == "commit"
        ]
        assert len(marks) == 21
        result = ProtocolJudge().evaluate(*pair, now=WALL + timedelta(days=1))
        out = write_checkpoint_results(*pair, result, root / "result")
        assert (
            json.loads((out / RESULT_FILENAME).read_text())["label"] == "INCONCLUSIVE"
        )
    finally:
        engine.store.close()
