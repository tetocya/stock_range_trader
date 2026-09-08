"""Valuation/Risk, business idempotency and real account transaction recovery."""

from dataclasses import replace

import pytest
from delayed_replay_account_helpers import Harness, evidence, policy, request

from delayed_replay.account_models import SharedAccountState
from delayed_replay.audit_errors import InvalidEvent
from delayed_replay.serialization import JsonObject


def test_aggregate_mark_order_signal_lane_independence_and_positive_control(tmp_path):
    a, b = (
        Harness(tmp_path / "a.sqlite3", policy(max_position_pct="0.5")),
        Harness(tmp_path / "b.sqlite3", policy(max_position_pct="0.5")),
    )
    try:
        for h in (a, b):
            h.submit(request(), request("buy-B", "B"))
            h.execute(
                "2024-01-03", evidence("A", "2024-01-03"), evidence("B", "2024-01-03")
            )
        marks = [
            evidence("A", "2024-01-03", "200", mark=True),
            evidence("B", "2024-01-03", "1", mark=True),
        ]
        # Signal lane values are intentionally not an accepted accounting input.
        for signal_prices, h, ordered in (
            ([1, 2], a, marks),
            ([999, 888], b, list(reversed(marks))),
        ):
            assert signal_prices
            h.mark("2024-01-03", *ordered)
        assert a.state == b.state
        assert a.state["risk"]["high_water"] == "201000"  # Not an intermediate 300000.
        a.mark(
            "2024-01-04",
            evidence("A", "2024-01-04", "201", mark=True),
            evidence("B", "2024-01-04", "1", mark=True),
        )
        b.mark(
            "2024-01-04",
            evidence("A", "2024-01-04", "200", mark=True),
            evidence("B", "2024-01-04", "1", mark=True),
        )
        assert a.state["risk"]["last_equity"] != b.state["risk"]["last_equity"]
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("quality", ["missing", "stale"])
def test_incomplete_marks_keep_positions_without_high_water_or_budget_updates(
    tmp_path, quality
):
    h = Harness(tmp_path / "a.sqlite3", policy(max_position_pct="0.5"))
    try:
        h.submit(request(), request("b", "B"))
        h.execute(
            "2024-01-03", evidence("A", "2024-01-03"), evidence("B", "2024-01-03")
        )
        h.mark(
            "2024-01-03",
            evidence("A", "2024-01-03", "999", mark=True),
            evidence("B", "2024-01-03", None, mark=True, quality=quality),
        )
        assert not h.state["valuation"]["complete"]
        assert set(h.state["positions"]) == {"A", "B"}
        assert (
            h.state["risk"]["high_water"] == h.state["risk"]["last_equity"] == "200000"
        )
        assert (
            h.state["valuation"]["display_equity"] is not None
        )  # Explicitly stale display.
        h.submit(request("c", "C", day="2024-01-03", target="2024-01-04"))
        assert h.state["orders"]["c"]["reason"] == "valuation_incomplete"
    finally:
        h.close()


def test_unsupported_action_halts_account_without_removing_position(tmp_path):
    h = Harness(tmp_path / "a.sqlite3")
    try:
        h.submit(request())
        h.execute("2024-01-03", evidence("A", "2024-01-03"))
        position = h.state["positions"]["A"]
        h.mark(
            "2024-01-03",
            evidence(
                "A",
                "2024-01-03",
                mark=True,
                corporate_action_supported=False,
                split_ratio="2",
            ),
        )
        assert h.state["positions"]["A"] == position
        assert h.state["risk"]["halted"]
        assert not h.state["valuation"]["complete"]
        h.reopen()
        assert h.state["positions"]["A"] == position
        assert "unsupported_corporate_action" in h.state["risk"]["stop_reasons"]
    finally:
        h.close()


def test_dd_is_descriptive_no_automatic_stop_or_month_reset(tmp_path):
    h = Harness(tmp_path / "a.sqlite3", policy(max_position_pct="1"))
    try:
        h.submit(request(day="2024-01-31", target="2024-02-01"))
        h.execute("2024-02-01", evidence("A", "2024-02-01"))
        h.mark("2024-02-01", evidence("A", "2024-02-01", "1", mark=True))
        assert h.state["risk"]["drawdown"] == "0.99"
        assert h.state["risk"]["buy_enabled"]
        h.send("stop", {"reason": "synthetic_external_stop"}, "2024-02-01")
        h.submit(
            request(
                "sell",
                day="2024-02-01",
                target="2024-02-02",
                side="SELL",
                requested_shares=2000,
            )
        )
        assert h.state["orders"]["sell"]["status"] == "pending"
        h.submit(request("b", "B", day="2024-02-01", target="2024-02-02"))
        assert h.state["orders"]["b"]["reason"] == "buy_risk_stopped"
        h.execute("2024-02-02", evidence("A", "2024-02-02", "1"))
        assert h.state["cash"] == "2000"
        h.mark("2024-03-01")
        h.reopen()
        assert h.state["cash"] == "2000"
        assert not h.state["risk"]["buy_enabled"]
        assert h.state["proceeds_holds"]
    finally:
        h.close()


@pytest.mark.parametrize(
    "phase", ["pending_buy", "position", "pending_sell", "hold", "risk_stop"]
)
def test_reopen_preserves_all_account_phases_and_provenance(tmp_path, phase):
    h = Harness(tmp_path / "a.sqlite3")
    try:
        h.submit(request())
        if phase != "pending_buy":
            h.execute("2024-01-03", evidence("A", "2024-01-03"))
            h.mark("2024-01-03", evidence("A", "2024-01-03", mark=True))
        if phase in ("pending_sell", "hold"):
            h.submit(
                request(
                    "sell",
                    day="2024-01-03",
                    target="2024-01-04",
                    side="SELL",
                    requested_shares=200,
                )
            )
        if phase == "hold":
            h.execute("2024-01-04", evidence("A", "2024-01-04", "110"))
        if phase == "risk_stop":
            h.send("stop", {"reason": "synthetic_pause"}, "2024-01-03")
        before = h.store.read()
        h.reopen()
        assert h.store.read() == before
        detached = h.state
        detached["orders"]["buy-A"]["request"]["candidate_id"] = "later-candidate"
        assert (
            h.state["orders"]["buy-A"]["request"]["candidate_id"]
            == "synthetic-candidate"
        )
    finally:
        h.close()


def test_event_and_business_idempotency_no_double_fill_release_or_mark(tmp_path):
    h = Harness(tmp_path / "a.sqlite3")
    try:
        h.submit(request())
        head = h.store.read().head
        receipt = h.execute("2024-01-03", evidence("A", "2024-01-03"))
        original = h.last_command
        before = h.state
        assert h.service.commit(original, head) == receipt
        h.service.commit(
            replace(original, event_id="different-event"), h.store.read().head
        )
        assert h.state == before
        changed = original.payload.to_dict()
        changed["evidence"][0]["open_price"] = "90"
        with pytest.raises(InvalidEvent):
            h.service.commit(
                replace(
                    original,
                    event_id="conflict-event",
                    payload=JsonObject.from_value(changed),
                ),
                h.store.read().head,
            )
        # Same business session under a different operation ID is not silently accepted.
        with pytest.raises(InvalidEvent):
            h.execute("2024-01-03", evidence("A", "2024-01-03"))
        h.mark("2024-01-03", evidence("A", "2024-01-03", mark=True))
        mark_command = h.last_command
        before = h.state
        h.service.commit(
            replace(mark_command, event_id="mark-retry"), h.store.read().head
        )
        assert h.state == before
        with pytest.raises(InvalidEvent):
            h.mark("2024-01-03", evidence("A", "2024-01-03", "999", mark=True))
    finally:
        h.close()


@pytest.mark.parametrize("point", ["after_state_update", "after_commit"])
def test_account_fill_fault_and_response_loss_match_continuous_run(tmp_path, point):
    expected, actual = (
        Harness(tmp_path / "expected.sqlite3"),
        Harness(tmp_path / "actual.sqlite3"),
    )
    try:
        for h in (expected, actual):
            h.submit(request(), request("b", "B"))
        expected.execute(
            "2024-01-03", evidence("A", "2024-01-03"), evidence("B", "2024-01-03")
        )
        before = actual.state
        old_head = actual.store.read().head

        def fail(at):
            if at == point:
                raise RuntimeError("synthetic_fill_fault")

        with pytest.raises(RuntimeError):
            actual.execute(
                "2024-01-03",
                evidence("A", "2024-01-03"),
                evidence("B", "2024-01-03"),
                fault=fail,
            )
        cmd = actual.last_command
        if point != "after_commit":
            assert actual.state == before
        actual.reopen()
        actual.service.commit(cmd, old_head, save_snapshot=True)
        assert actual.store.read() == expected.store.read()
    finally:
        expected.close()
        actual.close()


def test_state_invariants_reject_money_or_reservation_corruption(tmp_path):
    h = Harness(tmp_path / "a.sqlite3")
    try:
        h.submit(request())
        for field, value in (("cash", "1"), ("cash", "200001")):
            damaged = h.state
            damaged[field] = value
            with pytest.raises(InvalidEvent):
                SharedAccountState(JsonObject.from_value(damaged))
        damaged = h.state
        damaged["orders"]["buy-A"]["reservation"]["cash"] = "200001"
        with pytest.raises(InvalidEvent):
            SharedAccountState(JsonObject.from_value(damaged))
    finally:
        h.close()
