"""Artificial fixtures only; no reading of saved 7B prices or API access."""

from dataclasses import asdict, replace
from datetime import timedelta

import pytest
from delayed_replay_account_helpers import policy as arithmetic_policy
from delayed_replay_e2e_helpers import network_guard as network_guard
from delayed_replay_stage4_helpers import WALL, fixture_plan

from data.price_policy import provider_price_basis
from delayed_replay.account_policy import D
from delayed_replay.audit_errors import IdentityMismatch, InvalidEvent
from delayed_replay.checkpoints import LedgerCheckpointSource
from delayed_replay.proxy.input_adapter import (
    ProxyInputStore,
    ProxyPacket,
    SyntheticRecipe,
)
from delayed_replay.proxy.policy import DailyOpenProxyPolicy
from delayed_replay.proxy.reducer import ProxyReducer
from delayed_replay.proxy.resolution import FrozenProxyOrder, resolve_batch
from delayed_replay.proxy.service import ProxyPlan, ProxyService
from delayed_replay.serialization import JsonObject, time_text
from delayed_replay.validation import ReplayContractError

pytestmark = pytest.mark.usefixtures("network_guard")


def policy(**changes):
    values = dict(max_position_pct="0.4", max_positions=2)
    values.update(changes)
    terms = asdict(arithmetic_policy(**values))
    terms.pop("purpose")
    rules = dict(
        no_trade="terminal_no_fill",
        volume_excess="reject_instrument_batch",
        missing="wait_without_expiry",
        expiry="explicit_finalization_not_implemented",
        temporary_halt="allow_daily_proxy_without_time_condition",
        time_condition="none",
        volume_unit="execution_shares",
        provider_price_basis=provider_price_basis("jquants"),
        availability="session_phase_not_actual_publication_v1",
        corporate_action="stop_preserve",
        risk="no_new_dd_stop",
        end="mark_without_forced_exit",
    )
    return DailyOpenProxyPolicy(
        JsonObject.from_value(terms), JsonObject.from_value(rules)
    )


def plan_and_packet(overrides=None, missing=(), *, monthly=False):
    old = fixture_plan()
    recipe = SyntheticRecipe(
        "proxy-e2e-fixture",
        tuple(s.day.isoformat() for s in old.calendar.sessions),
        ("TEST_A",),
        "100",
        "10",
        "100000",
        time_text(WALL),
        JsonObject.from_value(overrides or {}),
    )
    plan = ProxyPlan(
        "proxy-tests",
        "artificial-source-v1",
        recipe,
        policy(),
        old.signals,
        tuple(s.day.isoformat() for s in old.sessions),
        "fast",
        old.monthly if monthly else None,
    )
    packet = ProxyPacket.generate(
        recipe, [k for k in recipe.records() if k not in missing]
    )
    return plan, packet


def create(tmp_path, **kwargs):
    plan, packet = plan_and_packet(**kwargs)
    artifacts = ProxyInputStore(tmp_path / "inputs")
    service = ProxyService.create(tmp_path / "account.sqlite", plan, artifacts, packet)
    return service, packet


def test_artificial_end_to_end_real_strategy(tmp_path):
    s, _ = create(tmp_path)
    result = s.run(WALL)
    assert result["status"] == "completed"
    fills = [o for o in result["orders"].values() if o["status"] == "filled"]
    assert any(o["frozen"]["side"] == "BUY" for o in fills)
    assert any(o["frozen"]["side"] == "SELL" for o in fills)
    assert result["capability"]["model_approval"] == "unapproved"
    assert result["reason"] == "no_next_bar_no_forced_exit"
    assert all(o["resolution"]["actual_trade_at"] is None for o in fills)
    assert (result["cash"], result["equity"], len(fills), len(result["episodes"])) == (
        "232000",
        "232000",
        8,
        4,
    )
    balance = D("200000")
    for o in fills:
        r = o["resolution"]
        assert D(r["net"]) == D(r["price"]) * r["shares"]
        balance += D(r["net"]) * (-1 if o["frozen"]["side"] == "BUY" else 1)
    assert balance == D(result["cash"])
    assert sum(D(e["profit"]) for e in result["episodes"].values()) == D("32000")
    s.store.close()


def order(**changes):
    values = dict(
        order_id="order-1",
        episode_id="episode-1",
        symbol="TEST_A",
        side="BUY",
        target="2024-06-04",
        decision_session="2024-06-03",
        decision_phase="after_close",
        candidate_id="fast",
        config_hash="a" * 64,
        shares=100,
        budget="20000",
        reserved="15000",
        reference_price="100",
        decision_equity="200000",
        equity_at="2024-06-03:after_close",
        policy_hash=policy().sha256,
        rank=0,
        score="50",
    )
    values.update(changes)
    return FrozenProxyOrder(JsonObject.from_value(values)).value.to_dict()


def observation(**changes):
    values = dict(
        symbol="TEST_A",
        session="2024-06-04",
        open="100",
        high="150",
        low="50",
        close="100",
        volume="10000",
        adjustment_factor="1",
        halt="unknown",
        volume_unit="execution_shares",
        actual_trade_at=None,
    )
    values.update(changes)
    return values


def resolve(row=None, orders=None, p=None, cash="200000"):
    return resolve_batch(
        orders or [order()], {"TEST_A": row or observation()}, p or policy(), cash
    ).value.to_dict()


@pytest.mark.parametrize(
    "change,status,reason",
    [
        ({"volume": "0"}, "resolved", "no_trade"),
        ({"volume": "99"}, "resolved", "instrument_batch_volume_exceeded"),
        ({"volume": "100"}, "resolved", None),
        ({"volume": None}, "waiting", "missing_volume"),
        ({"open": None}, "waiting", "missing_open"),
        ({"open": "0"}, "stopped_contract", "invalid_open"),
        ({"halt": "full", "open": None, "volume": "0"}, "resolved", "no_trade"),
        ({"halt": "full"}, "stopped_contract", "contradictory_full_halt"),
        ({"halt": "temporary"}, "resolved", None),
        ({"halt": "delayed_first_trade"}, "resolved", None),
        ({"low": "101"}, "stopped_contract", "invalid_ohlc"),
        (
            {"adjustment_factor": "0.5"},
            "stopped_contract",
            "unsupported_corporate_action",
        ),
    ],
)
def test_quality_before_fill(change, status, reason):
    r = resolve(observation(**change))
    assert r["status"] == status
    if status == "resolved":
        assert r["results"][0]["reason"] == reason
        assert r["results"][0]["filled"] is (reason is None)
        assert r["results"][0]["shares"] == 100
        assert r["results"][0]["actual_trade_at"] is None
    else:
        assert r["reason"] == reason and not r["results"]


def test_volume_aggregate_no_direction_netting_or_selection():
    orders = [
        order(),
        order(order_id="sell", side="SELL", budget="0", reserved="0", rank=1),
    ]
    r = resolve(observation(volume="199"), orders)
    assert len(r["results"]) == 2
    assert all(not x["filled"] and x["shares"] == 100 for x in r["results"])
    assert all(
        x["filled"] for x in resolve(observation(volume="200"), orders)["results"]
    )


def test_cost_slippage_buffer_and_fixed_quantity():
    p = policy(
        commission_rate="0.01", slippage_pct="0.02", reservation_buffer_pct="0.05"
    )
    o = order(policy_hash=p.sha256)
    buy = resolve(orders=[o], p=p)["results"][0]
    assert (buy["price"], buy["gross"], buy["commission"], buy["net"]) == (
        "102",
        "10200",
        "102",
        "10302",
    )
    sell = order(side="SELL", reserved="0", budget="0", policy_hash=p.sha256)
    result = resolve(orders=[sell], p=p)["results"][0]
    assert (result["price"], result["commission"], result["net"]) == (
        "98",
        "98",
        "9702",
    )
    gap = resolve(observation(open="200", high="210"))["results"][0]
    assert not gap["filled"] and gap["shares"] == 100
    assert gap["reason"] == "frozen_reservation_or_budget_exceeded"


def test_sell_proceeds_never_fund_same_batch_buy():
    orders = [
        order(order_id="sell", side="SELL", reserved="0", budget="0", rank=0),
        order(order_id="buy", reserved="9000", rank=1),
    ]
    r = resolve(orders=orders, cash="9000")["results"]
    assert r[0]["filled"] and not r[1]["filled"]


@pytest.mark.parametrize(
    "name,value",
    [
        ("shares", True),
        ("shares", 100.0),
        ("shares", 99),
        ("shares", -100),
        ("reference_price", "NaN"),
        ("reference_price", "Infinity"),
        ("reference_price", 100.0),
        ("rank", True),
        ("budget", None),
        ("decision_session", "2024-06-04"),
        ("reserved", "30000"),
    ],
)
def test_frozen_order_rejects_invalid_contract(name, value):
    with pytest.raises((InvalidEvent, ReplayContractError, TypeError, ValueError)):
        order(**{name: value})


@pytest.mark.parametrize(
    "name,value",
    [
        ("volume_unit", "adjusted_shares"),
        ("volume_unit", None),
        ("volume", "NaN"),
        ("volume", 100.0),
        ("actual_trade_at", "2024-06-04T09:00:00Z"),
    ],
)
def test_unknown_units_and_fabricated_actual_time_rejected(name, value):
    with pytest.raises((InvalidEvent, ReplayContractError)):
        resolve(observation(**{name: value}))


def test_draft_missing_and_unknown_policy_rejected():
    with pytest.raises(ReplayContractError):
        DailyOpenProxyPolicy().require()
    p = policy()
    for terms in ({}, {**p.terms.to_dict(), "commission_rate": None}):
        draft = DailyOpenProxyPolicy(JsonObject.from_value(terms), p.rules)
        with pytest.raises(ReplayContractError):
            draft.require()
    for name, value in (
        ("time_condition", "before_09:05"),
        ("provider_price_basis", "unknown"),
        ("missing", None),
    ):
        rules = {**p.rules.to_dict(), name: value}
        with pytest.raises(ReplayContractError):
            replace(p, rules=JsonObject.from_value(rules)).require()


@pytest.mark.parametrize("mode", ["real_data", "formal_oos", "synthetic_test", True])
def test_only_explicit_generated_offline_mode(mode):
    plan, _ = plan_and_packet()
    with pytest.raises(ReplayContractError):
        replace(plan, mode=mode)


def test_generated_packet_is_detached_and_rejects_real_symbols():
    plan, packet = plan_and_packet()
    original = packet.payload.sha256
    d = packet.payload.to_dict()
    key = next(iter(d["records"]))
    d["records"][key]["open"] = "999"
    assert packet.payload.sha256 == original
    assert packet.payload.to_dict()["records"][key]["open"] != "999"
    with pytest.raises(ReplayContractError):
        ProxyPacket(JsonObject.from_value(d))
    with pytest.raises(ReplayContractError):
        replace(plan.recipe, symbols=("72030",))
    with pytest.raises(ReplayContractError):
        ProxyPacket.generate(object(), [])


def until(service, predicate):
    for _ in range(200):
        if predicate(service.state):
            return service.state
        assert service.advance(WALL) == "running"
    pytest.fail("phase not reached")


def first_buy_boundary(service):
    return until(
        service,
        lambda s: (
            s["phase"] == "resolve"
            and any(
                o["status"] == "pending" and o["frozen"]["side"] == "BUY"
                for o in s["orders"].values()
            )
        ),
    )


def business_state(state):
    return {
        k: v
        for k, v in state.items()
        if k
        not in (
            "identity",
            "inputs",
            "input_head",
            "versions",
            "operations",
            "phase_observations",
        )
    }


def test_input_missing_wait_extend_resume_matches_uninterrupted(tmp_path):
    baseline, _ = create(tmp_path / "base")
    before = first_buy_boundary(baseline)
    target = baseline.plan.run_sessions[before["index"]]
    expected = baseline.run(WALL)
    baseline.store.close()
    key = "TEST_A|" + target
    service, initial = create(tmp_path / "split", missing=(key,))
    waiting = service.run(WALL)
    assert waiting["status"] == "waiting_for_input" and waiting["phase"] == "resolve"
    assert waiting["orders"] == before["orders"]
    assert target not in waiting["batches"]
    prefix = service.store.read().events
    addition = ProxyPacket.generate(service.plan.recipe, [key])
    sha = service.artifacts.publish(addition)
    service.accept(sha, "fill-missing", waiting["input_head"], WALL)
    service.store.close()
    service = ProxyService.resume(
        tmp_path / "split/account.sqlite",
        service.plan,
        service.artifacts,
        initial.payload.sha256,
    )
    result = service.run(WALL)
    assert service.store.read().events[: len(prefix)] == prefix
    assert business_state(result) == business_state(expected)
    service.store.close()


def test_close_missing_preserves_fill_then_resumes_without_repeat(tmp_path):
    base, _ = create(tmp_path / "base")
    boundary = first_buy_boundary(base)
    target = base.plan.run_sessions[boundary["index"]]
    expected = base.run(WALL)
    base.store.close()
    key = "TEST_A|" + target
    s, initial = create(tmp_path / "missing", overrides={key: {"close": None}})
    waiting = s.run(WALL)
    assert waiting["phase"] == "mark" and waiting["reason"] == "missing_close"
    assert not waiting["valuation_complete"] and waiting["positions"]
    assert target in waiting["batches"] and target not in waiting["decisions"]
    frozen_batch = waiting["batches"][target]
    recipe = replace(s.plan.recipe, overrides=JsonObject.from_value({}))
    addition = ProxyPacket.generate(recipe, [key])
    sha = s.artifacts.publish(addition)
    s.accept(sha, "close", waiting["input_head"], WALL)
    s.store.close()
    s = ProxyService.resume(
        tmp_path / "missing/account.sqlite", s.plan, s.artifacts, initial.payload.sha256
    )
    result = s.run(WALL)
    assert result["batches"][target] == frozen_batch
    assert business_state(result) == business_state(expected)
    s.store.close()


@pytest.mark.parametrize(
    "point", ["before_event_insert", "after_event_insert", "after_state_update"]
)
def test_batch_transaction_rollback(point, tmp_path):
    s, initial = create(tmp_path)
    before = first_buy_boundary(s)
    head = s.store.read().head

    def fault(name):
        if name == point:
            raise RuntimeError("artificial_crash")

    with pytest.raises(RuntimeError, match="artificial_crash"):
        s.advance(WALL, fault=fault)
    assert s.state == before and s.store.read().head == head
    s.store.close()
    s = ProxyService.resume(
        tmp_path / "account.sqlite", s.plan, s.artifacts, initial.payload.sha256
    )
    assert s.advance(WALL) == "running"
    assert s.state["phase"] == "mark"
    s.store.close()


def test_commit_reply_loss_and_event_business_retransmission(tmp_path):
    s, initial = create(tmp_path)
    first_buy_boundary(s)

    def fault(name):
        if name == "after_commit":
            raise RuntimeError("reply_lost")

    with pytest.raises(RuntimeError):
        s.advance(WALL, fault=fault)
    committed = s.state
    records = s.store.read()
    command = records.events[-1].command
    s.store.commit_event(command, records.head, ProxyReducer())
    assert s.state == committed
    s.store.commit_event(
        replace(command, event_id="new-delivery"), s.store.read().head, ProxyReducer()
    )
    assert s.state == committed
    payload = command.payload.to_dict()
    payload["data"]["index"] += 1
    with pytest.raises(InvalidEvent):
        s.store.commit_event(
            replace(
                command,
                event_id="conflicting-delivery",
                payload=JsonObject.from_value(payload),
            ),
            s.store.read().head,
            ProxyReducer(),
        )
    s.store.close()
    s = ProxyService.resume(
        tmp_path / "account.sqlite", s.plan, s.artifacts, initial.payload.sha256
    )
    assert s.state == committed
    assert s.run(WALL)["cash"] == "232000"
    s.store.close()


def test_volume_rejection_no_intermediate_fill_and_fixed_orders(tmp_path):
    base, _ = create(tmp_path / "base")
    before = first_buy_boundary(base)
    target = base.plan.run_sessions[before["index"]]
    base.store.close()
    s, _ = create(tmp_path / "reject", overrides={"TEST_A|" + target: {"volume": "0"}})
    altered = first_buy_boundary(s)
    assert altered["orders"] == before["orders"]
    assert s.advance(WALL) == "running"
    assert not s.state["positions"] and s.state["cash"] == "200000"
    assert all(o["status"] == "rejected" for o in s.state["orders"].values())
    assert all(not r["filled"] for r in s.state["batches"][target]["results"])
    assert all(e.command.event_type != "Fill" for e in s.store.read().events)
    s.store.close()


def test_proxy_checkpoint_and_existing_database_refused(tmp_path):
    s, packet = create(tmp_path)
    with pytest.raises(ReplayContractError):
        s.checkpoint()
    with pytest.raises(IdentityMismatch, match="reducer_identity_mismatch"):
        LedgerCheckpointSource.from_store(s.store, s.store.read().head)
    with pytest.raises(ReplayContractError):
        ProxyService.create(tmp_path / "account.sqlite", s.plan, s.artifacts, packet)
    s.store.close()


def test_clock_preserves_acquisition_and_rejects_replay_before_it(tmp_path):
    s, _ = create(tmp_path)
    with pytest.raises(InvalidEvent):
        s.advance(WALL - timedelta(days=1))
    s.advance(WALL)
    state = s.state
    assert all(
        v["fetched_at"] == time_text(WALL) and v["first_observed_at"] == time_text(WALL)
        for v in state["inputs"].values()
    )
    meta = next(iter(state["phase_observations"].values()))
    assert meta["replayed_at"] == time_text(WALL) and meta["actual_trade_at"] is None
    assert meta["availability_model_hash"] == s.plan.policy.availability_hash
    assert meta["snapshot_hashes"]
    s.store.close()


def test_close_positive_control_changes_next_order_not_today_order(tmp_path):
    base, _ = create(tmp_path / "base")
    other, _ = create(
        tmp_path / "other",
        overrides={"TEST_A|2024-06-07": {"close": "150", "high": "160"}},
    )
    original = first_buy_boundary(base)
    changed = first_buy_boundary(other)
    assert original["orders"] == changed["orders"]
    a = until(base, lambda s: s["index"] == 2 and s["phase"] == "select")
    b = until(other, lambda s: s["index"] == 2 and s["phase"] == "select")
    assert a["batches"]["2024-06-07"] == b["batches"]["2024-06-07"]
    assert a["decisions"]["2024-06-07"]["TEST_A"]["action"] == "hold"
    assert b["decisions"]["2024-06-07"]["TEST_A"]["action"] == "sell"
    assert any(o["frozen"]["target"] == "2024-06-12" for o in b["orders"].values())
    base.store.close()
    other.store.close()


@pytest.mark.parametrize(
    "change",
    [{"open": "85"}, {"high": "500", "low": "1"}, {"volume": "1"}, {"close": "85"}],
)
def test_target_ohlcv_never_changes_frozen_decision(change, tmp_path):
    base, _ = create(tmp_path / "base")
    other, _ = create(tmp_path / "other", overrides={"TEST_A|2024-06-07": change})
    assert first_buy_boundary(base)["orders"] == first_buy_boundary(other)["orders"]
    base.store.close()
    other.store.close()


def test_revision_quarantine_parent_conflict_and_prefix(tmp_path):
    s, _ = create(tmp_path)
    first_buy_boundary(s)
    before = s.state
    prefix = s.store.read().events
    key = "TEST_A|2024-06-03"
    recipe = replace(
        s.plan.recipe, overrides=JsonObject.from_value({key: {"close": "101"}})
    )
    packet = ProxyPacket.generate(recipe, [key])
    sha = s.artifacts.publish(packet)
    with pytest.raises(InvalidEvent):
        s.accept(sha, "stale", "0" * 64, WALL)
    assert s.state == before
    s.accept(sha, "revision", before["input_head"], WALL)
    state = s.state
    assert state["versions"][state["input_head"]]["status"] == "quarantined_revision"
    assert state["inputs"] == before["inputs"]
    assert state["orders"] == before["orders"]
    assert s.store.read().events[: len(prefix)] == prefix
    s.store.close()


@pytest.mark.parametrize(
    "point", ["before_file_write", "before_file_publish", "after_file_publish"]
)
def test_immutable_file_publication_fault_cannot_advance_input_head(point, tmp_path):
    s, _ = create(tmp_path)
    before = s.state
    packet = ProxyPacket.generate(s.plan.recipe, [next(iter(s.plan.recipe.records()))])

    def fault(name):
        if name == point:
            raise RuntimeError("file_fault")

    with pytest.raises(RuntimeError):
        s.artifacts.publish(packet, fault=fault)
    assert s.state == before
    assert s.artifacts.path(packet.payload.sha256).exists() is (
        point == "after_file_publish"
    )
    assert not list(s.artifacts.root.glob(".proxy-*"))
    s.store.close()


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_resume_rejects_missing_or_corrupt_evidence(damage, tmp_path):
    s, packet = create(tmp_path)
    s.store.close()
    path = s.artifacts.path(packet.payload.sha256)
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("{}")
    with pytest.raises(ReplayContractError):
        ProxyService.resume(
            tmp_path / "account.sqlite", s.plan, s.artifacts, packet.payload.sha256
        )


@pytest.mark.parametrize("change", ["source", "policy", "candidate"])
def test_resume_identity_mismatch(change, tmp_path):
    s, packet = create(tmp_path)
    s.store.close()
    changes = {
        "source": {"source_identity": "different"},
        "policy": {"policy": policy(max_position_pct="0.3")},
        "candidate": {"fixed_candidate": "slow"},
    }[change]
    plan = replace(s.plan, **changes)
    with pytest.raises(IdentityMismatch):
        ProxyService.resume(
            tmp_path / "account.sqlite", plan, s.artifacts, packet.payload.sha256
        )


@pytest.mark.parametrize("mutation", ["promoted", "missing"])
def test_resolution_metadata_missing_or_promoted_refused(mutation):
    from delayed_replay.proxy.resolution import ProxyResolution

    r = resolve()
    if mutation == "promoted":
        r["capability"]["model_approval"] = "approved"
    else:
        del r["capability"]
    with pytest.raises(ReplayContractError):
        ProxyResolution(JsonObject.from_value(r))


def test_held_corporate_action_stops_preserving_account(tmp_path):
    s, _ = create(
        tmp_path, overrides={"TEST_A|2024-06-12": {"adjustment_factor": "0.5"}}
    )
    before = until(s, lambda s: s["index"] == 2 and s["phase"] == "resolve")
    assert before["positions"]
    assert s.advance(WALL) == "stopped_contract"
    assert s.state["reason"] == "held_corporate_action"
    for key in ("cash", "positions", "orders", "batches", "equity"):
        assert s.state[key] == before[key]
    s.store.close()


def test_run_end_marks_open_position_without_forced_exit(tmp_path):
    plan, packet = plan_and_packet()
    plan = replace(plan, run_sessions=plan.run_sessions[:2])
    s = ProxyService.create(
        tmp_path / "end.sqlite", plan, ProxyInputStore(tmp_path / "inputs"), packet
    )
    result = s.run(WALL)
    assert result["status"] == "completed" and result["positions"]
    assert result["valuation_complete"] and not result["episodes"]
    assert len(result["orders"]) == 1
    assert result["equity"] == "200000" and result["cash"] == "136000"
    s.store.close()


def test_past_new_row_quarantined_after_selection_boundary(tmp_path):
    plan, _ = plan_and_packet()
    key = next(iter(plan.recipe.records()))
    s, _ = create(tmp_path, missing=(key,))
    s.advance(WALL)
    before = s.state
    assert key not in before["inputs"]
    packet = ProxyPacket.generate(s.plan.recipe, [key])
    sha = s.artifacts.publish(packet)
    s.accept(sha, "past", before["input_head"], WALL)
    assert s.state["versions"][s.state["input_head"]]["status"] == "quarantined_past"
    assert s.state["inputs"] == before["inputs"]
    s.store.close()


def test_snapshot_transaction_rollback(tmp_path):
    s, _ = create(tmp_path)
    first_buy_boundary(s)
    s.advance(WALL)
    records = s.store.read()
    command = replace(records.events[-1].command, event_id="snapshot-retry")

    def fault(name):
        if name == "after_snapshot_insert":
            raise RuntimeError("snapshot_fault")

    with pytest.raises(RuntimeError):
        s.store.commit_event(
            command, records.head, ProxyReducer(), save_snapshot=True, _fault_hook=fault
        )
    after = s.store.read()
    assert after.head == records.head and after.snapshots == records.snapshots
    assert after.current_state == records.current_state
    s.store.close()


def test_detached_phase_view_and_missing_calendar_history(tmp_path):
    from delayed_replay.proxy.service import observation_view

    s, _ = create(tmp_path)
    before = s.state
    view = observation_view(before, WALL, "2024-06-01", signal=False)
    assert all(r["date"].date().isoformat() < "2024-06-01" for _, r in view)
    assert all(r["fetched_at"].to_pydatetime() == WALL for _, r in view)
    view[0][1]["close"] = 999.0
    assert s.state == before
    key = next(iter(before["inputs"]))
    del before["inputs"][key]
    with pytest.raises(ReplayContractError, match="proxy_incomplete_history"):
        observation_view(before, WALL, "2024-06-01", signal=True)
    assert key in s.state["inputs"]
    s.store.close()


def test_later_acquisition_preserves_original_snapshot_and_first_observation(tmp_path):
    key = "TEST_A|2024-06-07"
    s, original = create(tmp_path, overrides={key: {"close": None}})
    waiting = s.run(WALL)
    assert waiting["phase"] == "mark"
    original_bytes = s.artifacts.path(original.payload.sha256).read_bytes()
    later = WALL + timedelta(days=1)
    recipe = replace(
        s.plan.recipe, acquired_at=time_text(later), overrides=JsonObject.from_value({})
    )
    packet = ProxyPacket.generate(recipe, [key])
    assert packet.payload.sha256 != original.payload.sha256
    assert recipe.origin == s.plan.recipe.origin
    sha = s.artifacts.publish(packet)
    with pytest.raises(InvalidEvent):
        s.accept(sha, "not-yet-acquired", waiting["input_head"], WALL)
    assert s.state == waiting
    s.accept(sha, "later-close", waiting["input_head"], later)
    item = s.state["inputs"][key]
    assert item["first_observed_at"] == time_text(WALL)
    assert item["fetched_at"] == time_text(later)
    assert s.artifacts.path(original.payload.sha256).read_bytes() == original_bytes
    assert s.run(later)["cash"] == "232000"
    s.store.close()


def test_monthly_adapter_uses_existing_selection(tmp_path):
    s, _ = create(tmp_path, monthly=True)
    result = s.run(WALL)
    assert result["status"] == "completed"
    assert len(result["epochs"]) == 3
    assert all(
        e["selection_mode"] == "existing_monthly_selection"
        for e in result["epochs"].values()
    )
    s.store.close()
