"""Multi-month continuity and actual SQLite crash recovery."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from delayed_replay_stage4_helpers import WALL, fixture_plan, reprice

from delayed_replay.audit_errors import IdentityMismatch, IntegrityError
from delayed_replay.event_store import EventStore
from delayed_replay.replay_engine import ReplayEngine
from delayed_replay.replay_state import ReplayReducer
from delayed_replay.validation import ReplayContractError


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    plan = fixture_plan()
    path = tmp_path_factory.mktemp("stage4") / "replay.sqlite"
    engine = ReplayEngine.create(path, plan)
    try:
        final = engine.run(WALL).to_dict()
        records = engine.store.read()
        timeline, state = [], records.initial_state.to_dict()
        for event in records.events:
            state = ReplayReducer()(state, event.command)
            timeline.append(state)
        assert state == final
        return plan, final, records, timeline, path
    finally:
        engine.store.close()


def test_selection_changes_and_old_positions_coexist(replay):
    plan, final, records, timeline, _ = replay
    assert [e["candidate_id"] for e in final["epochs"].values()] == [
        "slow",
        "slow",
        "fast",
    ]
    assert len(final["decisions"]) == len(plan.sessions) == 21
    fills = [o for o in final["account"]["orders"].values() if o["status"] == "filled"]
    assert {o["request"]["side"] for o in fills} == {"BUY", "SELL"}
    assert len(fills) >= 10
    assert len(records.events) == 21 * 6
    assert any(
        o["position"]["entry_order_id"][:7] != o["exit_order_id"][:7]
        for o in final["account"]["episodes"].values()
    )
    august = next(s for s in timeline if s["cursor"] == {"index": 14, "phase": "open"})
    assert august["epochs"]["2024-08-01"]["candidate_id"] == "fast"
    assert any(
        p["candidate_id"] == "slow" for p in august["account"]["positions"].values()
    )
    assert any(
        o["request"]["candidate_id"] == "fast" and o["request"]["side"] == "BUY"
        for o in fills
    )


def test_close_next_open_holiday_month_boundary_and_finish(replay):
    plan, final, records, timeline, _ = replay
    epoch = final["epochs"]["2024-06-01"]
    assert epoch["boundary"] == "2024-06-01"
    assert epoch["executed_at"].startswith("2024-06-02T23:")
    assert epoch["validation_start"] == "2024-05-01"
    assert not timeline[1]["account"]["orders"]
    for order in final["account"]["orders"].values():
        req = order["request"]
        assert req["reference_session"] < req["target_session"] < "2024-09-01"
        if order["status"] == "filled":
            assert order["fill"]["session"] == req["target_session"]
    assert final["account"]["positions"]
    assert not any(
        o["status"] == "pending" for o in final["account"]["orders"].values()
    )
    assert any(d["reason"] == "no_next_bar" for d in final["decisions"]["2024-08-30"])
    assert final["account"]["valuation"]["session"] == "2024-08-30"
    assert plan.calendar.sessions[-1].day == date(2024, 9, 2)
    assert all(
        e.command.market_decision_at.date() < date(2024, 9, 1) for e in records.events
    )


def test_holding_streak_high_water_and_proceeds_order(replay):
    _, _, _, timeline, _ = replay
    holds_seen = 0
    for before, after in zip(timeline, timeline[1:], strict=False):
        if after["visible"]["phase"] == "select":
            assert after["account"] == before["account"]
            assert after["position_states"] == before["position_states"]
        if (
            after["visible"]["phase"] == "prepare"
            and before["account"]["proceeds_holds"]
        ):
            holds_seen += 1
            assert not after["account"]["proceeds_holds"]
            assert after["account"]["cash"] == before["account"]["cash"]
            assert before["account"]["valuation"]["complete"]
        if after["visible"]["phase"] == "decide":
            for symbol, meta in after["position_states"].items():
                assert (
                    meta["holding_sessions"]
                    == before["position_states"][symbol]["holding_sessions"] + 1
                )
                assert meta["signal_entry_price"] is not None
    assert holds_seen >= 4
    assert (
        max(
            m["holding_sessions"]
            for s in timeline
            for m in s["position_states"].values()
        )
        >= 3
    )


@pytest.mark.parametrize("phase", ["select", "open", "mark", "decide"])
def test_transaction_interruption_resume_and_business_retry(
    replay, tmp_path, monkeypatch, phase
):
    plan, final, records, _, _ = replay
    cut = next(
        i
        for i, e in enumerate(records.events)
        if i > 80 and e.command.payload.to_dict()["phase"] == phase
    )
    path = tmp_path / "resume.sqlite"
    store = EventStore.create(path, records.identity, records.initial_state)
    for event in records.events[:cut]:
        store.commit_event(event.command, store.read().head, ReplayReducer())
    command = records.events[cut].command
    before = store.read()

    def fault(where):
        if where == "after_state_update":
            raise RuntimeError("synthetic_power_cut")

    with pytest.raises(RuntimeError, match="synthetic_power_cut"):
        store.commit_event(command, before.head, ReplayReducer(), _fault_hook=fault)
    assert store.read().current_state == before.current_state
    store.close()

    def forbidden(*args, **kwargs):
        raise AssertionError("external_computation_during_recovery")

    with monkeypatch.context() as m:
        m.setattr(type(plan.monthly.evaluator), "evaluate_validation", forbidden)
        m.setattr(type(plan.monthly.selector), "select", forbidden)
        m.setattr(type(plan.signals), "decide", forbidden)
        engine = ReplayEngine.resume(path, plan, expected_head=before.head)
    try:
        engine.commit(command)
        once = engine.state
        engine.commit(replace(command, event_id="different-id-same-business-phase"))
        assert engine.state == once
        for event in records.events[cut + 1 :]:
            engine.commit(event.command)
        assert engine.state.to_dict() == final
        engine.store.recover(ReplayReducer())
    finally:
        engine.store.close()


@pytest.mark.parametrize(
    "change", ["source", "policy", "catalog", "snapshot", "universe"]
)
def test_resume_rejects_input_substitution(replay, change):
    plan, _, _, _, path = replay
    if change == "source":
        changed = replace(plan, source_identity="different-source")
    elif change == "policy":
        policy = replace(plan.policy, maximum_drawdown="0.8")
        changed = replace(
            plan, policy=policy, monthly=replace(plan.monthly, policy=policy)
        )
    elif change == "catalog":
        catalog = replace(
            plan.signals.catalog,
            candidates=tuple(reversed(plan.signals.catalog.candidates)),
        )
        changed = replace(
            plan,
            monthly=replace(plan.monthly, catalog=catalog),
            signals=replace(plan.signals, catalog=catalog),
        )
    elif change == "snapshot":
        changed = replace(
            plan,
            market=replace(
                plan.market,
                snapshots=(reprice(plan.market.snapshots[0], data_version="another"),),
            ),
        )
    else:
        changed = replace(plan, universe=("A",))
    with pytest.raises((IdentityMismatch, IntegrityError, ReplayContractError)):
        ReplayEngine.resume(path, changed)


def test_corrupt_hash_resume_rejected(replay, tmp_path):
    import shutil
    import sqlite3

    plan, _, _, _, source = replay
    path = tmp_path / "corrupt.sqlite"
    shutil.copy2(source, path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE streams SET current_hash=?", ("0" * 64,))
    with pytest.raises(IntegrityError):
        ReplayEngine.resume(path, plan)


def test_invalid_phase_and_tampered_metadata_rejected(replay):
    _, _, records, timeline, _ = replay
    command = records.events[0].command
    with pytest.raises(ReplayContractError):
        ReplayReducer()(
            records.initial_state.to_dict(), replace(command, event_type="unknown")
        )
    with pytest.raises(ReplayContractError):
        ReplayReducer()(
            timeline[1],
            replace(
                command,
                event_id="bad",
                market_decision_at=command.market_decision_at + timedelta(seconds=1),
            ),
        )


def test_future_change_preserves_past_with_future_positive_control(replay, tmp_path):
    plan, baseline, _, _, _ = replay
    original = plan.market.snapshots[0]
    changed_bars = tuple(
        replace(
            b,
            raw_ohlcv=tuple(
                round(v * 1.1, 6) if i < 4 else v for i, v in enumerate(b.raw_ohlcv)
            ),
        )
        if b.session_date >= date(2024, 8, 1)
        else b
        for b in original.observations
    )
    changed = replace(
        plan,
        market=replace(
            plan.market, snapshots=(reprice(original, observations=changed_bars),)
        ),
    )
    assert changed.identity != plan.identity
    engine = ReplayEngine.create(tmp_path / "future.sqlite", changed)
    try:
        final = engine.run(WALL).to_dict()
    finally:
        engine.store.close()
    assert final["epochs"] == baseline["epochs"]
    assert {d: v for d, v in final["decisions"].items() if d < "2024-08-01"} == {
        d: v for d, v in baseline["decisions"].items() if d < "2024-08-01"
    }
    for key, order in baseline["account"]["orders"].items():
        if key < "2024-08-01":
            actual = final["account"]["orders"][key]
            assert actual["shares"] == order["shares"]
            assert actual["fill"] == order["fill"]
    assert (
        final["account"]["valuation"]["display_equity"]
        != baseline["account"]["valuation"]["display_equity"]
    )


def test_real_runner_resume_uses_committed_selection_without_reselection(
    replay, tmp_path, monkeypatch
):
    plan, baseline, records, _, _ = replay
    path = tmp_path / "runner-resume.sqlite"
    # August selection has already committed. Suffix executes real signal logic.
    cut = next(
        i
        for i, e in enumerate(records.events)
        if e.command.payload.to_dict()["index"] == 14
        and e.command.payload.to_dict()["phase"] == "select"
    )
    store = EventStore.create(path, records.identity, records.initial_state)
    for e in records.events[: cut + 1]:
        store.commit_event(e.command, store.read().head, ReplayReducer())
    store.close()

    def forbidden(*args, **kwargs):
        raise AssertionError("confirmed_selection_recomputed")

    monkeypatch.setattr(type(plan.monthly.evaluator), "evaluate_validation", forbidden)
    engine = ReplayEngine.resume(path, plan)
    try:
        assert engine.run(WALL + timedelta(days=1)).to_dict() == baseline
    finally:
        engine.store.close()


def test_no_candidate_month_keeps_pending_buys_and_manages_old_exit(replay, tmp_path):
    plan, _, _, _, _ = replay
    from delayed_replay.replay_engine import money

    original = plan.market.snapshots[0]
    bars = tuple(
        replace(b, raw_ohlcv=(50.0, 51.0, 49.0, 50.0, 100000.0))
        if b.symbol == "A" and b.session_date.month == 7
        else b
        for b in original.observations
        if b.session_date != date(2024, 7, 31)
    )
    opens = tuple(
        replace(e, open_price=money(50.0))
        if e.instrument_id == "A" and e.session.startswith("2024-07")
        else e
        for e in plan.market.open_snapshots[0].evidence
        if e.session != "2024-07-31"
    )
    selector = replace(
        plan.monthly.selector,
        policy=replace(plan.monthly.selector.policy, minimum_finite_sharpe_count=2),
    )
    changed = replace(
        plan,
        calendar=replace(
            plan.calendar,
            sessions=tuple(
                s for s in plan.calendar.sessions if s.day != date(2024, 7, 31)
            ),
        ),
        monthly=replace(plan.monthly, selector=selector),
        market=replace(
            plan.market,
            snapshots=(reprice(original, observations=bars),),
            open_snapshots=(replace(plan.market.open_snapshots[0], evidence=opens),),
        ),
    )
    engine = ReplayEngine.create(tmp_path / "no-candidate.sqlite", changed)
    try:
        final = engine.run(WALL).to_dict()
        events = engine.store.read().events
    finally:
        engine.store.close()
    assert final["epochs"]["2024-08-01"]["status"] == "no_eligible_candidate"
    assert not any(
        k.startswith("2024-08") and o["request"]["side"] == "BUY"
        for k, o in final["account"]["orders"].items()
    )
    carried = [
        o
        for o in final["account"]["orders"].values()
        if o["request"]["side"] == "BUY"
        and o["request"]["target_session"] == "2024-08-01"
    ]
    assert carried and all(o["status"] == "filled" for o in carried)
    assert any(
        k.startswith("2024-08")
        and o["status"] == "filled"
        and o["request"]["side"] == "SELL"
        for k, o in final["account"]["orders"].items()
    )
    assert (
        sum(
            e.command.payload.to_dict()["data"].get("epoch") is not None for e in events
        )
        == 3
    )


@pytest.mark.parametrize(
    "fault_point", ["before_event_insert", "after_state_update", "after_commit"]
)
def test_phase_crash_atomicity_including_lost_response(replay, tmp_path, fault_point):
    plan, _, records, _, _ = replay
    path = tmp_path / "crash.sqlite"
    engine = ReplayEngine.create(path, plan)
    command = records.events[0].command

    def fault(where):
        if where == fault_point:
            raise RuntimeError("interruption")

    with pytest.raises(RuntimeError, match="interruption"):
        engine.commit(command, fault=fault)
    engine.store.close()
    engine = ReplayEngine.resume(path, plan)
    try:
        head = engine.store.read().head
        assert head.sequence == (1 if fault_point == "after_commit" else 0)
        engine.commit(command)
        assert engine.store.read().head.sequence == 1
        assert len(engine.state.to_dict()["epochs"]) == 1
    finally:
        engine.store.close()
