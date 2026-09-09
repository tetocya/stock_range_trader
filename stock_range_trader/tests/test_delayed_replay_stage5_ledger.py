"""Checkpoint integration with actual Stage 2--4 synthetic records."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from delayed_replay_stage4_helpers import WALL, fixture_plan

from delayed_replay.audit_errors import IntegrityError
from delayed_replay.audit_models import Head
from delayed_replay.checkpoint_models import (
    CheckpointSchedule,
    ExternalAbsence,
    FinalizationPolicy,
)
from delayed_replay.checkpoints import (
    LedgerCheckpointSource,
    sample_counts,
    secondary_metrics,
    unique_records,
)
from delayed_replay.protocol_judge import ProtocolJudge
from delayed_replay.replay_engine import ReplayEngine
from delayed_replay.replay_state import ReplayReducer
from delayed_replay.serialization import JsonObject
from delayed_replay.validation import ReplayContractError


@pytest.fixture(scope="module")
def ledger(tmp_path_factory):
    plan = fixture_plan()
    engine = ReplayEngine.create(
        tmp_path_factory.mktemp("stage5") / "replay.sqlite", plan
    )
    engine.run(WALL)
    records = engine.store.read()
    engine.store.close()
    source = LedgerCheckpointSource(records, records.head)
    schedule = CheckpointSchedule(plan.policy.run_start, plan.calendar)
    policy = FinalizationPolicy(
        WALL - timedelta(days=1),
        None,
        WALL + timedelta(days=1),
        "synthetic_explicit_wall_dates",
    )
    return plan, source, schedule, {1: policy, 3: policy}


def prefix(records, count):
    state = records.initial_state.to_dict()
    for event in records.events[:count]:
        state = ReplayReducer()(state, event.command)
    head = Head(
        count,
        records.identity.genesis_hash
        if count == 0
        else records.events[count - 1].event_hash,
    )
    return replace(
        records,
        events=records.events[:count],
        snapshots=tuple(s for s in records.snapshots if s.sequence <= count),
        head=head,
        current_state=JsonObject.from_value(state),
    )


def test_actual_checkpoint_no_reexecution_and_same_capital(ledger, monkeypatch):
    plan, source, schedule, policies = ledger

    def forbidden(*args, **kwargs):
        raise AssertionError("checkpoint_must_not_reexecute")

    monkeypatch.setattr(ReplayEngine, "run", forbidden)
    monkeypatch.setattr(type(plan.monthly.evaluator), "evaluate_validation", forbidden)
    monkeypatch.setattr(type(plan.monthly.selector), "select", forbidden)
    one, three = source.collect(schedule, policies, now=WALL)
    assert one.expected_session == date(2024, 6, 28)
    assert one.boundary == date(2024, 7, 1)
    assert three.expected_session == date(2024, 8, 30)
    assert three.boundary == date(2024, 9, 1)  # Sunday, no rounding.
    assert one.unique_symbols == three.unique_symbols == 2
    assert one.completed_trades == 2 and three.completed_trades == 7
    assert one.initial_equity == three.initial_equity == "200000"
    account = source.records.current_state.to_dict()["account"]
    expected = Decimal(account["cash"]) + sum(
        p["shares"] * Decimal(account["valuation"]["marks"][s]["price"])
        for s, p in account["positions"].items()
    )
    assert Decimal(three.equity) == expected
    assert three.secondary.to_dict()["open_episode_count"] == 1
    assert three.secondary.to_dict()["completed_episode_count"] == 7
    assert three.secondary.to_dict()["observed_valuation_count"] == 21
    assert (
        ProtocolJudge().evaluate(one, three, now=WALL).to_dict()["label"]
        == "INCONCLUSIVE"
    )


def test_future_fills_do_not_change_one_month_and_freeze_is_preserved(ledger):
    _, source, schedule, policies = ledger
    short = prefix(source.records, 42)
    old = LedgerCheckpointSource(short, short.head).collect(
        schedule, policies, now=WALL
    )
    new = source.collect(schedule, policies, now=WALL)
    assert old[0].equity == new[0].equity
    assert old[0].completed_trades == new[0].completed_trades == 2
    assert (
        old[0].references.to_dict()["completed_episode_ids"]
        == new[0].references.to_dict()["completed_episode_ids"]
    )
    assert old[1].samples.status == "pending" and old[1].completed_trades is None
    frozen = source.collect(schedule, policies, now=WALL, previous={1: old[0]})
    assert frozen[0] == old[0]
    assert (
        ProtocolJudge().evaluate(*frozen, now=WALL).to_dict()["label"] == "INCONCLUSIVE"
    )


def test_expected_missing_mark_no_fallback_and_external_deadline(ledger):
    _, source, schedule, policies = ledger
    # Stop immediately before the expected 1M mark, not a fabricated equity.
    partial = prefix(source.records, 38)
    partial_source = LedgerCheckpointSource(partial, partial.head)
    one, three = partial_source.collect(schedule, policies, now=WALL)
    assert one.valuation.status == "pending" and one.equity is None
    assert one.actual_session is None
    assert one.secondary.to_dict()["maximum_drawdown"] is None
    assert "2024-06-28" in one.secondary.to_dict()["missing_valuation_sessions"]
    deadline = policies[1].deadline
    assert (
        partial_source.collect(schedule, policies, now=deadline)[0].valuation.status
        == "pending"
    )
    absence = {1: ExternalAbsence("provider-outage", "b" * 64, WALL, "provider_outage")}
    finalized = partial_source.collect(
        schedule, policies, now=deadline, external_absence=absence
    )
    assert finalized[0].valuation.status == "unavailable_external"
    assert (
        ProtocolJudge().evaluate(*finalized, now=deadline).to_dict()["label"]
        == "INCONCLUSIVE"
    )
    with pytest.raises(ReplayContractError, match="amendment"):
        source.collect(
            schedule,
            policies,
            now=deadline,
            previous={1: finalized[0], 3: finalized[1]},
        )


def test_head_and_episode_reference_tampering_rejected(ledger):
    _, source, _, _ = ledger
    with pytest.raises(IntegrityError):
        LedgerCheckpointSource(
            source.records, Head(source.expected_head.sequence, "0" * 64)
        )
    account = source.records.current_state.to_dict()["account"]
    key = next(iter(account["episodes"]))
    account["episodes"][key]["exit_order_id"] = "not-an-exit"
    with pytest.raises(ReplayContractError, match="episode"):
        sample_counts(account, "2024-06-01", "2024-09-01", ("A", "B"))
    assert (
        len(unique_records([{"id": "a", "value": 1}, {"id": "a", "value": 1}], "id"))
        == 1
    )
    with pytest.raises(ReplayContractError, match="same_id"):
        unique_records([{"id": "a", "value": 1}, {"id": "a", "value": 2}], "id")


def test_secondary_undefined_and_reservations_are_not_equity_deductions(ledger):
    _, source, _, _ = ledger
    account = source.records.current_state.to_dict()["account"]
    values = secondary_metrics({}, ["2024-06-03"], account, {}).to_dict()
    assert (
        values["net_expectancy"] is None
        and values["net_expectancy_reason"] == "no_completed_trades"
    )
    assert (
        values["profit_factor"] is None
        and values["profit_factor_reason"] == "undefined_zero_denominator"
    )
    positive = secondary_metrics(
        {"2024-06-03": "190000"},
        ["2024-06-03"],
        account,
        {"synthetic": {"net_profit": "10"}},
    ).to_dict()
    assert positive["maximum_drawdown"] == "0.05"
    assert (
        positive["profit_factor"] is None
        and positive["profit_factor_reason"] == "no_losing_trades"
    )
    # All actual marks use cash + held prices; reservations are accounting locks.
    state = source.records.initial_state.to_dict()
    for event in source.records.events:
        state = ReplayReducer()(state, event.command)
        a = state["account"]
        if a["valuation"]["complete"]:
            equity = Decimal(a["cash"]) + sum(
                p["shares"] * Decimal(a["valuation"]["marks"][s]["price"])
                for s, p in a["positions"].items()
            )
            assert equity == Decimal(a["valuation"]["display_equity"])


def test_calendar_year_crossing(ledger):
    plan, _, _, _ = ledger
    from delayed_replay.replay_calendar import shift_month

    assert shift_month(date(2023, 12, 1), 1) == date(2024, 1, 1)
    assert shift_month(date(2023, 12, 1), 3) == date(2024, 3, 1)
    assert CheckpointSchedule(plan.policy.run_start, plan.calendar).boundary(3) == date(
        2024, 9, 1
    )


def test_corrupt_store_produces_invalid_not_external(ledger, tmp_path):
    from delayed_replay.checkpoints import collect_store_checkpoints
    from delayed_replay.event_store import EventStore

    _, source, schedule, policies = ledger
    records = prefix(source.records, 0)
    store = EventStore.create(
        tmp_path / "corrupt.sqlite", records.identity, records.initial_state
    )
    try:
        # Deliberate corruption of an artificial test DB only.
        store._connection.execute("UPDATE streams SET current_hash=?", ("0" * 64,))
        one, three = collect_store_checkpoints(
            store, schedule, policies, expected_head=records.head, now=WALL
        )
        assert one.valuation.status == three.samples.status == "invalid"
        assert (
            ProtocolJudge().evaluate(one, three, now=WALL).to_dict()["label"]
            == "INVALID"
        )
    finally:
        store.close()


def test_wrong_expected_head_is_not_an_external_outage(ledger, tmp_path):
    from delayed_replay.checkpoints import collect_store_checkpoints
    from delayed_replay.event_store import EventStore

    _, source, schedule, policies = ledger
    records = prefix(source.records, 0)
    store = EventStore.create(
        tmp_path / "head.sqlite", records.identity, records.initial_state
    )
    try:
        with pytest.raises(IntegrityError, match="head_mismatch"):
            collect_store_checkpoints(
                store, schedule, policies, expected_head=Head(0, "b" * 64), now=WALL
            )
    finally:
        store.close()


def test_exact_boundary_fills_and_nonfilled_orders_excluded():
    account = {"orders": {}, "episodes": {}}
    for key, day, status in (
        ("before", "2024-01-31", "filled"),
        ("boundary", "2024-02-01", "filled"),
        ("rejected", "2024-01-30", "rejected"),
        ("canceled", "2024-01-29", "canceled"),
    ):
        account["orders"][key] = {
            "status": status,
            "shares": 100,
            "fill": {"session": day},
            "request": {"instrument_id": key},
        }
    symbols, ids, episodes = sample_counts(
        account, "2024-01-01", "2024-02-01", tuple(account["orders"])
    )
    assert symbols == ("before",) and ids == ("before",) and episodes == {}
    account["orders"]["before"]["shares"] = 0
    with pytest.raises(ReplayContractError, match="share_quantity"):
        sample_counts(account, "2024-01-01", "2024-02-01", tuple(account["orders"]))


def test_explicit_year_crossing_and_leap_day_calendar():
    from datetime import datetime

    from delayed_replay.replay_calendar import JST, ReplayCalendar, ReplaySession

    days = (date(2023, 12, 29), date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 1))
    calendar = ReplayCalendar(
        tuple(
            ReplaySession(
                d,
                datetime(d.year, d.month, d.day, 8, tzinfo=JST),
                datetime(d.year, d.month, d.day, 9, tzinfo=JST),
                datetime(d.year, d.month, d.day, 15, tzinfo=JST),
                "Asia/Tokyo",
            )
            for d in days
        )
    )
    schedule = CheckpointSchedule(date(2023, 12, 1), calendar)
    assert schedule.expected(1).day == date(2023, 12, 29)
    assert schedule.expected(3).day == date(2024, 2, 29)
    assert schedule.boundary(3) == date(2024, 3, 1)
