"""Adversarial publication/cutoff/lane tests using artificial observations."""

from dataclasses import replace
from datetime import date, timedelta

import pandas as pd
import pytest
from delayed_replay_stage4_helpers import WALL, fixture_plan, reprice, shorten

from delayed_replay.clock import ReplayClock
from delayed_replay.market_view import WaitingForInput
from delayed_replay.replay_calendar import ReplayCalendar, shift_month
from delayed_replay.replay_engine import ReplayEngine
from delayed_replay.snapshot import ReplayDataView
from delayed_replay.validation import ReplayContractError


@pytest.fixture
def plan():
    return fixture_plan()


def history(plan):
    s = plan.sessions[0]
    return plan.market.history(
        ReplayClock(s.close_at, WALL), date(2024, 4, 1), date(2024, 7, 1), plan.universe
    )


@pytest.mark.parametrize(
    "mutation", ["price", "yfinance", "unknown", "corporate_action", "new_symbol"]
)
def test_monthly_cutoff_before_provider_and_cohort(plan, mutation):
    bars = history(plan)
    s = plan.sessions[0]
    baseline = plan.monthly.evaluate(
        bars, plan.policy.run_start, s.selection_at, plan.universe
    )
    future = bars.iloc[[-1]].copy()
    future["date"] = pd.Timestamp("2024-06-01")
    if mutation == "price":
        future["raw_close"] = -999
    elif mutation in ("yfinance", "unknown"):
        future["provider"] = mutation
    elif mutation == "corporate_action":
        future["stock_split"] = 10
    else:
        future["symbol"] = "NEW"
    extended = pd.concat([bars, future], ignore_index=True)
    assert (
        plan.monthly.evaluate(
            extended, plan.policy.run_start, s.selection_at, plan.universe
        )
        == baseline
    )
    assert (
        plan.monthly.evaluate(
            extended.sample(frac=1, random_state=7),
            plan.policy.run_start,
            s.selection_at,
            plan.universe,
        )
        == baseline
    )


def test_monthly_exception_is_not_no_candidate(plan, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("evaluator_corruption")

    monkeypatch.setattr(type(plan.monthly.evaluator), "evaluate_validation", fail)
    with pytest.raises(RuntimeError, match="evaluator_corruption"):
        plan.monthly.evaluate(
            history(plan),
            plan.policy.run_start,
            plan.sessions[0].selection_at,
            plan.universe,
        )


def test_monthly_insufficient_and_normal_no_candidate_are_distinct(plan):
    s = plan.sessions[0]
    bars = history(plan)
    short = bars.loc[bars.date >= "2024-05-01"]
    result = plan.monthly.evaluate(
        short, plan.policy.run_start, s.selection_at, plan.universe
    ).value.to_dict()
    assert result["status"] == "insufficient_history"
    assert {e["reason"] for e in result["exclusions"]} == {"insufficient_warmup"}
    catalog = replace(
        plan.monthly.catalog,
        candidates=tuple(
            replace(c, buy_atr_multiplier=100) for c in plan.monthly.catalog.candidates
        ),
    )
    result = (
        replace(plan.monthly, catalog=catalog)
        .evaluate(bars, plan.policy.run_start, s.selection_at, plan.universe)
        .value.to_dict()
    )
    assert result["status"] == "no_eligible_candidate"
    assert all(s["total_trade_count"] == 0 for s in result["scores"])


def test_close_publication_and_legacy_same_day_exclusion(plan):
    s = plan.sessions[0]
    before = ReplayClock(s.close_at - timedelta(microseconds=1), WALL)
    after = ReplayClock(s.close_at, WALL)
    assert plan.market.history(
        before, s.day, s.day + timedelta(days=1), plan.universe
    ).empty
    visible = plan.market.history(
        after, s.day, s.day + timedelta(days=1), plan.universe
    )
    assert len(visible) == 2
    visible.loc[:, "raw_close"] = -1
    assert (
        plan.market.history(
            after, s.day, s.day + timedelta(days=1), plan.universe
        ).raw_close
        > 0
    ).all()
    assert not ReplayDataView(plan.market.snapshots).history(after, start=s.day)
    opens = plan.market.opens(
        ReplayClock(s.open_at, WALL), s.day.isoformat(), plan.universe
    )
    assert all(
        not any(
            k in e.to_dict() for k in ("close", "high", "low", "volume", "raw_close")
        )
        for e in opens
    )


def test_pinned_snapshot_wait_not_market_time_requirement(plan):
    s = plan.sessions[0]
    with pytest.raises(WaitingForInput):
        plan.market.history(
            ReplayClock(s.close_at, WALL - timedelta(seconds=1)),
            s.day,
            s.day + timedelta(days=1),
            plan.universe,
        )
    assert (
        len(
            plan.market.history(
                ReplayClock(s.close_at, WALL),
                s.day,
                s.day + timedelta(days=1),
                plan.universe,
            )
        )
        == 2
    )
    assert all(
        snapshot.provider_published_at is None for snapshot in plan.market.snapshots
    )


@pytest.mark.parametrize(
    "field,bad",
    [
        ("lookback_months", True),
        ("warmup_months", 0),
        ("minimum_warmup_sessions", float("nan")),
        ("lookback_months", -1),
        ("lookback_months", "3"),
        ("maximum_drawdown", "1"),
        ("purpose", "formal_oos"),
    ],
)
def test_explicit_policy_rejects_invalid_types_and_unsupported_modes(plan, field, bad):
    with pytest.raises((ValueError, TypeError)):
        replace(plan.policy, **{field: bad})


def test_calendar_determinism_and_no_rounding(plan):
    assert ReplayCalendar(tuple(reversed(plan.calendar.sessions))) == plan.calendar
    with pytest.raises(ReplayContractError):
        ReplayCalendar((plan.sessions[0], plan.sessions[0]))
    with pytest.raises(ReplayContractError):
        replace(plan.policy, run_start=date(2024, 6, 3))
    assert shift_month(date(2024, 1, 1), -1) == date(2023, 12, 1)
    assert shift_month(date(2024, 2, 1), 1) == date(2024, 3, 1)


def test_wait_resume_without_expiring_pending_and_atomic_marks(plan, tmp_path):
    plan = shorten(plan, date(2024, 7, 1))
    original = plan.market.snapshots[0]
    late_day = date(2024, 6, 24)
    late = tuple(
        b
        for b in original.observations
        if b.symbol == "A" and b.session_date == late_day
    )
    assert len(late) == 1
    normal = tuple(b for b in original.observations if b not in late)
    market = replace(
        plan.market,
        snapshots=(
            reprice(original, observations=normal),
            reprice(original, observations=late, fetched_at=WALL + timedelta(days=1)),
        ),
    )
    changed = replace(plan, market=market)
    engine = ReplayEngine.create(tmp_path / "wait.sqlite", changed)
    try:
        assert engine.run(WALL).to_dict()["status"] == "waiting_for_input"
        waiting = engine.state.to_dict()
        assert waiting["cursor"] == {"index": 4, "phase": "mark"}
        assert waiting["account"]["positions"]
        assert waiting["account"]["valuation"]["session"] != late_day.isoformat()
        assert engine.advance(WALL) == "waiting_for_input"
        assert engine.state.to_dict()["account"] == waiting["account"]
    finally:
        engine.store.close()
    engine = ReplayEngine.resume(tmp_path / "wait.sqlite", changed)
    try:
        result = engine.run(WALL + timedelta(days=1)).to_dict()
        assert result["status"] == "completed"
        marks = [
            e
            for e in engine.store.read().events
            if e.command.payload.to_dict()["phase"] == "mark"
            and e.command.payload.to_dict()["action"] == "commit"
        ]
        assert len(marks) == 7
    finally:
        engine.store.close()


def test_signal_lane_is_independent_of_raw_entry_basis(plan):
    bars = history(plan)
    selected = bars.loc[bars.symbol == "A"]
    signal = selected[
        [
            "date",
            "adjusted_open",
            "adjusted_high",
            "adjusted_low",
            "adjusted_close",
            "adjusted_volume",
        ]
    ].rename(columns=lambda c: c.removeprefix("adjusted_"))
    meta = dict(entry_session="2024-06-03", holding_sessions=0, signal_entry_price=None)
    decision = plan.signals.decide(signal, "slow", plan.policy.run_start, meta)
    assert float(decision.signal_entry_price) == float(signal.iloc[-1].open)
    assert float(decision.signal_entry_price) != float(selected.iloc[-1].raw_open)
    changed = signal.copy()
    changed.loc[changed.index[-1], ["low", "close"]] = 1
    later = plan.signals.decide(changed, "slow", plan.policy.run_start, meta)
    assert later.action == "sell" and later.exit_reason == "stop_loss"
    assert decision.exit_reason != "stop_loss"
    with pytest.raises(ReplayContractError):
        plan.signals.decide(selected, "slow", plan.policy.run_start, meta)


@pytest.mark.parametrize("location", ["open", "close"])
def test_unsupported_held_action_stops_preserving_position(plan, tmp_path, location):
    plan = shorten(plan, date(2024, 7, 1))
    day = date(2024, 6, 24)
    held_symbol = "B" if location == "open" else "A"
    market = plan.market
    if location == "close":
        original = market.snapshots[0]
        bars = tuple(
            replace(b, stock_split=2.0)
            if b.symbol == "A" and b.session_date == day
            else b
            for b in original.observations
        )
        market = replace(market, snapshots=(reprice(original, observations=bars),))
    else:
        original = market.open_snapshots[0]
        opens = tuple(
            replace(e, corporate_action_supported=False, split_ratio="2")
            if e.instrument_id == held_symbol and e.session == day.isoformat()
            else e
            for e in original.evidence
        )
        market = replace(market, open_snapshots=(replace(original, evidence=opens),))
    engine = ReplayEngine.create(
        tmp_path / "unsupported.sqlite", replace(plan, market=market)
    )
    try:
        final = engine.run(WALL).to_dict()
        assert final["status"] == "stopped_contract"
        assert "unsupported_corporate_action" in final["reason"]
        assert held_symbol in final["account"]["positions"]
        assert final["account"]["risk"]["halted"]
        assert engine.advance(WALL) == "stopped_contract"
    finally:
        engine.store.close()


def test_shuffle_snapshot_calendar_universe_is_identical_plan(plan):
    original = plan.market.snapshots[0]
    shuffled = replace(
        plan,
        universe=tuple(reversed(plan.universe)),
        calendar=replace(
            plan.calendar, sessions=tuple(reversed(plan.calendar.sessions))
        ),
        market=replace(
            plan.market,
            snapshots=(
                reprice(original, observations=tuple(reversed(original.observations))),
            ),
            open_snapshots=(
                replace(
                    plan.market.open_snapshots[0],
                    evidence=tuple(reversed(plan.market.open_snapshots[0].evidence)),
                ),
            ),
        ),
    )
    assert shuffled.stream_identity() == plan.stream_identity()


def test_input_missing_open_wait_keeps_frozen_order(plan, tmp_path):
    plan = shorten(plan, date(2024, 7, 1))
    original = plan.market.open_snapshots[0]
    missing = tuple(e for e in original.evidence if e.session == "2024-06-18")
    market = replace(
        plan.market,
        open_snapshots=(
            replace(
                original,
                evidence=tuple(e for e in original.evidence if e not in missing),
            ),
            replace(original, evidence=missing, fetched_at=WALL + timedelta(days=1)),
        ),
    )
    engine = ReplayEngine.create(
        tmp_path / "open-wait.sqlite", replace(plan, market=market)
    )
    try:
        before = engine.run(WALL).to_dict()
        assert before["status"] == "waiting_for_input" and before["cursor"] == {
            "index": 3,
            "phase": "open",
        }
        pending = {
            k: o
            for k, o in before["account"]["orders"].items()
            if o["status"] == "pending"
        }
        assert pending and all(
            o["reservation"]["cash"] != "0" for o in pending.values()
        )
        assert engine.advance(WALL) == "waiting_for_input"
        assert before["account"] == engine.state.to_dict()["account"]
        assert engine.advance(WALL + timedelta(days=1)) == "running"
        after = engine.state.to_dict()
        assert all(after["account"]["orders"][k]["status"] == "filled" for k in pending)
        assert all(
            after["account"]["orders"][k]["shares"] == o["shares"]
            for k, o in pending.items()
        )
    finally:
        engine.store.close()


def test_nonzero_breakdown_streak_survives_restart(plan, tmp_path):
    plan = shorten(plan, date(2024, 7, 1))
    config = replace(plan.signals.base_config, range_exit_threshold=100, adx_exit_min=0)
    plan = replace(
        plan,
        signals=replace(plan.signals, base_config=config),
        monthly=replace(
            plan.monthly, evaluator=replace(plan.monthly.evaluator, base_config=config)
        ),
    )
    path = tmp_path / "streak.sqlite"
    engine = ReplayEngine.create(path, plan)
    while True:
        status = engine.advance(WALL)
        state = engine.state.to_dict()
        if any(
            meta["breakdown_streak"] > 0 for meta in state["position_states"].values()
        ):
            break
        assert status == "running"
    engine.store.close()
    engine = ReplayEngine.resume(path, plan)
    try:
        assert engine.state.to_dict() == state
        final = engine.run(WALL).to_dict()
        assert any(
            d.get("reason") == "range_breakdown"
            for rows in final["decisions"].values()
            for d in rows
        )
    finally:
        engine.store.close()
