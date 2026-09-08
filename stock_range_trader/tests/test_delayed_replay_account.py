"""Real SQLite plus Production account reducer: accounting and lifecycle."""

from dataclasses import asdict, replace

import pytest
from delayed_replay_account_helpers import Harness, evidence, policy, request

from delayed_replay.account_policy import D
from delayed_replay.audit_errors import InvalidEvent


@pytest.fixture
def account(tmp_path):
    h = Harness(tmp_path / "account.sqlite3")
    yield h
    h.close()


def buy(h, *, day="2024-01-03"):
    h.submit(request())
    h.execute(day, evidence("A", day))


def sell_request(h, **changes):
    values = dict(
        order_id="sell-A",
        day="2024-01-03",
        target="2024-01-04",
        side="SELL",
        requested_shares=h.state["positions"]["A"]["shares"],
    )
    values.update(changes)
    return request(**values)


def test_exact_hand_accounting_commission_and_proceeds_hold(tmp_path):
    h = Harness(tmp_path / "account.sqlite3", policy(commission_rate="0.001"))
    try:
        h.submit(request())
        before = h.state
        assert before["cash"] == before["valuation"]["display_equity"] == "200000"
        assert before["orders"]["buy-A"]["shares"] == 100
        assert h.service.state.reserved_cash == D("10010")
        h.execute("2024-01-03", evidence("A", "2024-01-03"))
        assert h.state["cash"] == "189990"
        assert h.state["positions"]["A"]["cost_basis"] == "10010"
        assert h.service.state.reserved_cash == 0
        h.mark("2024-01-03", evidence("A", "2024-01-03", "110", mark=True))
        assert h.state["valuation"]["display_equity"] == "200990"
        assert h.state["valuation"]["unrealized_profit"] == "990"
        h.submit(sell_request(h))
        assert h.state["orders"]["sell-A"]["reservation"]["shares"] == 100
        h.execute("2024-01-04", evidence("A", "2024-01-04", "110"))
        state = h.state
        assert state["cash"] == "200979"
        assert state["realized_profit"] == "979"
        assert state["episodes"]["buy-A"]["position"]["realized_profit"] == "979"
        assert state["positions"] == {}
        assert state["orders"]["sell-A"]["fill"]["commission"] == "11"
        assert h.service.state.reserved_cash == D("10989")
        assert h.service.state.available_cash == D("189990")
        assert state["valuation"]["display_equity"] == "200979"
        h.mark("2024-01-04")
        before = h.state
        with pytest.raises(InvalidEvent):
            h.send(
                "release",
                dict(
                    decision_session="2024-01-04",
                    hold_ids=["sell-A"],
                    reason="next_eligible_decision",
                ),
                "2024-01-04",
            )
        assert h.state == before
        h.send(
            "release",
            dict(
                decision_session="2024-01-05",
                hold_ids=["sell-A"],
                reason="next_eligible_decision",
            ),
            "2024-01-05",
        )
        assert h.service.state.available_cash == D("200979")
        assert h.state["cash"] == "200979"
        assert h.state["valuation"]["display_equity"] == "200979"
        released = h.state
        release_command = h.last_command
        h.service.commit(
            replace(release_command, event_id="release-retry"), h.store.read().head
        )
        assert h.state == released
        with pytest.raises(InvalidEvent):
            h.send(
                "release",
                dict(
                    decision_session="2024-01-05",
                    hold_ids=["sell-A"],
                    reason="next_eligible_decision",
                ),
                "2024-01-05",
            )
        assert h.state == released
    finally:
        h.close()


def test_priority_shuffle_cash_competition_and_no_opportunistic_new_buy(tmp_path):
    left, right = (
        Harness(tmp_path / "left.sqlite3", policy(max_position_pct="0.6")),
        Harness(tmp_path / "right.sqlite3", policy(max_position_pct="0.6")),
    )
    try:
        reqs = [
            request("c", "C", range_score="60"),
            request("b", "B", range_score="80"),
            request("a", "A", range_score="80"),
        ]
        left.submit(*reqs)
        right.submit(*reversed(reqs))
        assert left.state == right.state
        assert left.state["orders"]["a"]["shares"] == 1200
        assert left.state["orders"]["b"]["shares"] == 800
        assert left.state["orders"]["c"]["reason"] == "insufficient_lot_budget"
        assert left.service.state.reserved_cash == D("200000")
        assert left.service.state.available_cash == 0
        left.execute(
            "2024-01-03",
            evidence("A", "2024-01-03", "90"),
            evidence("B", "2024-01-03", "100"),
        )
        assert left.state["positions"]["A"]["shares"] == 1200
        assert left.state["cash"] == "12000"
        assert "C" not in left.state["positions"]
        assert left.state["orders"]["c"]["status"] == "rejected"
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize(
    "open_price,filled", [("90", True), ("100", True), ("101", False)]
)
def test_frozen_budget_fixed_quantity_gap_and_no_cash_topup(
    account, open_price, filled
):
    account.submit(request())
    original = account.state["orders"]["buy-A"]
    account.execute("2024-01-03", evidence("A", "2024-01-03", open_price))
    order = account.state["orders"]["buy-A"]
    assert order["shares"] == original["shares"] == 200
    assert order["frozen_budget"] == original["frozen_budget"]
    assert order["initial_reserved_cash"] == original["reservation"]["cash"]
    assert order["status"] == ("filled" if filled else "rejected")
    if not filled:
        assert order["reason"] == "reservation_exceeded"
        assert account.state["cash"] == "200000"  # Plenty of other cash is not used.
    assert account.service.state.reserved_cash == 0


def test_reservation_buffer_not_charged_as_slippage_or_profit(tmp_path):
    h = Harness(
        tmp_path / "account.sqlite3",
        policy(
            max_position_pct="0.2", slippage_pct="0.01", reservation_buffer_pct="0.1"
        ),
    )
    try:
        h.submit(request())
        order = h.state["orders"]["buy-A"]
        assert order["shares"] == 300
        assert order["reservation"]["cash"] == "33330"
        h.execute("2024-01-03", evidence("A", "2024-01-03"))
        assert h.state["orders"]["buy-A"]["fill"]["price"] == "101"
        assert h.state["positions"]["A"]["cost_basis"] == "30300"
        assert h.state["cash"] == "169700"
        assert h.state["realized_profit"] == "0"
        assert h.service.state.reserved_cash == 0
        h.mark("2024-01-03", evidence("A", "2024-01-03", "110", mark=True))
        h.submit(sell_request(h))
        h.execute("2024-01-04", evidence("A", "2024-01-04", "110"))
        assert h.state["orders"]["sell-A"]["fill"]["price"] == "108.9"
        assert h.state["realized_profit"] == "2370"
    finally:
        h.close()


def test_slots_include_pending_and_planned_sell_does_not_free_slot(tmp_path):
    h = Harness(tmp_path / "account.sqlite3", policy(max_positions=1))
    try:
        h.submit(request(), request("b", "B", range_score="60"))
        assert h.state["orders"]["b"]["reason"] == "holding_slots_exhausted"
        h.execute("2024-01-03", evidence("A", "2024-01-03"))
        h.mark("2024-01-03", evidence("A", "2024-01-03", mark=True))
        h.submit(
            sell_request(h), request("c", "C", day="2024-01-03", target="2024-01-04")
        )
        assert h.state["orders"]["c"]["reason"] == "holding_slots_exhausted"
        h.execute("2024-01-04", evidence("A", "2024-01-04"))
        assert h.state["positions"] == {}
        assert h.state["orders"]["c"]["status"] == "rejected"
    finally:
        h.close()


def test_duplicate_batch_instrument_order_and_additional_buy_rejected(account):
    before = account.state
    with pytest.raises(InvalidEvent):
        account.submit(request(), request("other", "A"))
    assert account.state == before
    account.submit(request())
    with pytest.raises(InvalidEvent):
        account.submit(request())
    account.submit(request("second", "A"))
    assert account.state["orders"]["second"]["reason"] == "additional_buy_not_supported"


def test_sell_unheld_partial_duplicate_and_entry_provenance(account):
    account.submit(request("unheld", side="SELL", requested_shares=100))
    assert account.state["orders"]["unheld"]["reason"] == "unheld_sell"
    buy(account)
    account.mark("2024-01-03", evidence("A", "2024-01-03", mark=True))
    account.submit(sell_request(account, order_id="partial", requested_shares=100))
    assert account.state["orders"]["partial"]["reason"] == "partial_exit_not_supported"
    account.submit(
        sell_request(account, order_id="wrong-candidate", candidate_id="new-candidate")
    )
    assert (
        account.state["orders"]["wrong-candidate"]["reason"]
        == "entry_provenance_mismatch"
    )
    account.submit(sell_request(account))
    account.submit(sell_request(account, order_id="duplicate"))
    assert account.state["orders"]["duplicate"]["reason"] == "duplicate_sell"
    assert account.state["orders"]["sell-A"]["reservation"]["shares"] == 200


def test_cancel_no_trade_explicit_finalization_and_no_next_day_fill(account):
    account.submit(
        request(),
        request("b", "B", range_score="60"),
        request("c", "C", range_score="50"),
    )
    account.send("cancel", dict(order_id="c", reason="user_cancel"), "2024-01-02")
    account.execute("2024-01-03", evidence("A", "2024-01-03", tradability="no_trade"))
    assert account.state["orders"]["buy-A"]["reason"] == "no_trade_evidence"
    assert account.state["orders"]["b"]["status"] == "pending"
    account.send("finalize", dict(session="2024-01-03"), "2024-01-03")
    assert account.state["orders"]["b"]["reason"] == "no_execution_evidence"
    assert account.service.state.reserved_cash == 0
    with pytest.raises(InvalidEvent):
        account.send("cancel", dict(order_id="c", reason="user_cancel"), "2024-01-03")
    with pytest.raises(InvalidEvent):
        account.send(
            "execute",
            dict(
                session="2024-01-04",
                order_ids=["b"],
                evidence=[asdict(evidence("B", "2024-01-04"))],
            ),
            "2024-01-04",
            0,
        )


@pytest.mark.parametrize(
    "change", ["session", "instrument", "snapshot", "basis", "future_availability"]
)
def test_evidence_mismatch_rejected_before_account_changes(account, change):
    account.submit(request())
    proof = evidence("A", "2024-01-03")
    if change == "session":
        proof = evidence("A", "2024-01-04")
    elif change == "instrument":
        proof = evidence("B", "2024-01-03")
    elif change == "snapshot":
        proof = replace(proof, snapshot_hash="7" * 64)
    elif change == "basis":
        proof = replace(proof, basis_evidence_hash="7" * 64)
    else:
        proof = evidence(
            "A",
            "2024-01-03",
            market_available_at=evidence(
                "A", "2024-01-03", mark=True
            ).market_available_at,
        )
    before = account.state
    with pytest.raises(InvalidEvent):
        account.execute("2024-01-03", proof)
    assert account.state == before


def test_open_cannot_be_retroactively_processed_after_close(account):
    account.submit(request())
    before = account.state
    with pytest.raises(InvalidEvent):
        account.send(
            "execute",
            dict(
                session="2024-01-03",
                order_ids=["buy-A"],
                evidence=[asdict(evidence("A", "2024-01-03"))],
            ),
            "2024-01-03",
            8,
        )
    assert account.state == before


def test_stale_equity_blocks_new_buy_and_known_stop_blocks_pending_fill(account):
    buy(account)
    account.mark("2024-01-03", evidence("A", "2024-01-03", mark=True))
    account.submit(request("b", "B", day="2024-01-04", target="2024-01-05"))
    assert (
        account.state["orders"]["b"]["reason"]
        == "valuation_stale_for_reference_session"
    )


def test_known_stop_after_reservation_rejects_fill_releasing_cash(account):
    account.submit(request())
    account.send("stop", {"reason": "synthetic_stop"}, "2024-01-02")
    account.execute("2024-01-03", evidence("A", "2024-01-03"))
    assert account.state["orders"]["buy-A"]["reason"] == "buy_risk_stopped"
    assert account.state["cash"] == "200000"
    assert account.service.state.reserved_cash == 0


def test_sale_proceeds_are_unavailable_until_explicit_later_release(tmp_path):
    h = Harness(tmp_path / "account.sqlite3", policy(max_position_pct="1"))
    try:
        buy(h)
        assert h.state["cash"] == "0"
        h.mark("2024-01-03", evidence("A", "2024-01-03", mark=True))
        h.submit(sell_request(h))
        h.execute("2024-01-04", evidence("A", "2024-01-04"))
        h.mark("2024-01-04")
        assert h.state["cash"] == "200000"
        assert h.service.state.available_cash == 0
        h.submit(request("blocked", "B", day="2024-01-04", target="2024-01-05"))
        assert h.state["orders"]["blocked"]["reason"] == "insufficient_lot_budget"
        h.send(
            "release",
            dict(
                decision_session="2024-01-05",
                hold_ids=["sell-A"],
                reason="next_eligible_decision",
            ),
            "2024-01-05",
        )
        h.submit(request("allowed", "B", day="2024-01-05", target="2024-01-06"))
        assert h.state["orders"]["allowed"]["shares"] == 2000
    finally:
        h.close()


def test_price_tick_and_money_rounding_are_distinct_and_explicit(tmp_path):
    h = Harness(
        tmp_path / "account.sqlite3",
        policy(price_quantum="0.05", money_quantum="1", commission_rate="0.001"),
    )
    try:
        h.submit(request(reference_price="100.01"))
        assert h.state["orders"]["buy-A"]["shares"] == 100
        assert h.state["orders"]["buy-A"]["initial_reserved_cash"] == "10016"
        h.execute("2024-01-03", evidence("A", "2024-01-03", "100.03"))
        assert h.state["orders"]["buy-A"]["fill"]["price"] == "100.05"
        assert h.state["positions"]["A"]["entry_commission"] == "11"
        assert h.state["cash"] == "189984"
    finally:
        h.close()
