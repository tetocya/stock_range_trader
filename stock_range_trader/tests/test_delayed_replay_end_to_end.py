"""Real offline Stage 1--5 chain; no substituted selection or trading results."""

import csv
import json
import socket
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from delayed_replay_e2e_helpers import (
    artifact_bytes,
    candidate_for,
    reconcile,
    run,
    timeline,
    verify_artifacts,
)
from delayed_replay_e2e_helpers import baseline as baseline
from delayed_replay_e2e_helpers import network_guard as network_guard
from delayed_replay_stage4_helpers import reprice

from delayed_replay.checkpoint_report import CHECKPOINTS_FILENAME, RESULT_FILENAME
from delayed_replay.registration_candidate import CANDIDATE_SCHEMA

pytestmark = pytest.mark.usefixtures("network_guard")


def test_real_chain_measurements_and_independent_accounting(baseline):
    state = baseline.state
    assert len(baseline.plan.sessions) == 21
    assert [e["candidate_id"] for e in state["epochs"].values()] == [
        "slow",
        "slow",
        "fast",
    ]
    account = state["account"]
    fills = [o for o in account["orders"].values() if o["status"] == "filled"]
    assert sum(o["request"]["side"] == "BUY" for o in fills) == 8
    assert sum(o["request"]["side"] == "SELL" for o in fills) == 7
    assert len(account["episodes"]) == 7 and len(account["positions"]) == 1
    assert (
        sum(
            e["position"]["entry_order_id"][:7] != e["exit_order_id"][:7]
            for e in account["episodes"].values()
        )
        == 2
    )
    reconcile(baseline.plan, baseline.records)
    one, three = baseline.pair
    assert (one.equity, one.return_value, one.unique_symbols, one.completed_trades) == (
        "227859",
        "0.139295",
        2,
        2,
    )
    assert (
        three.equity,
        three.return_value,
        three.unique_symbols,
        three.completed_trades,
    ) == ("324487.89", "0.62243945", 2, 7)
    assert baseline.result.to_dict()["reason_codes"] == ["insufficient_sample"]
    assert baseline.result.to_dict()["label"] == "INCONCLUSIVE"
    # Independent half-open checkpoint accounting from actual orders and marks.
    for checkpoint in baseline.pair:
        cutoff = checkpoint.boundary.isoformat()
        eligible = [o for o in fills if "2024-06-01" <= o["fill"]["session"] < cutoff]
        assert checkpoint.unique_symbols == len(
            {o["request"]["instrument_id"] for o in eligible}
        )
        assert checkpoint.completed_trades == sum(
            o["request"]["side"] == "SELL" for o in eligible
        )
        mark = next(
            s
            for _, s in timeline(baseline.records)
            if s["visible"]["session"] == checkpoint.expected_session.isoformat()
            and s["visible"]["phase"] == "mark"
        )
        assert checkpoint.equity == mark["account"]["valuation"]["display_equity"]
    verify_artifacts(baseline)
    exported = json.loads((baseline.bundle / RESULT_FILENAME).read_text())
    assert exported["checkpoints"] == [c.to_dict() for c in baseline.pair]
    with (baseline.bundle / CHECKPOINTS_FILENAME).open() as f:
        assert [r["months"] for r in csv.DictReader(f)] == ["1", "3"]
    draft = baseline.candidate.to_dict()
    assert draft["schema"] == CANDIDATE_SCHEMA
    assert draft["registration_status"] == "draft_not_registered"
    assert draft["formal_registration_performed"] is False
    assert {
        "live_price_basis_unverified",
        "additional_input_events_unsupported",
        "real_daily_open_evidence_unsupported",
        "unapproved_fees",
    } <= set(draft["structural_unmet"])


def test_same_clocks_shuffled_inputs_and_other_path_are_identical(baseline, tmp_path):
    plan = baseline.plan
    snapshot = plan.market.snapshots[0]
    shuffled = replace(
        plan,
        universe=tuple(reversed(plan.universe)),
        calendar=replace(
            plan.calendar, sessions=tuple(reversed(plan.calendar.sessions))
        ),
        market=replace(
            plan.market,
            snapshots=(
                reprice(snapshot, observations=tuple(reversed(snapshot.observations))),
            ),
            open_snapshots=(
                replace(
                    plan.market.open_snapshots[0],
                    evidence=tuple(reversed(plan.market.open_snapshots[0].evidence)),
                ),
            ),
        ),
    )
    repeated = run(tmp_path / "other", shuffled)
    assert repeated.candidate == baseline.candidate
    assert repeated.records.events == baseline.records.events
    assert repeated.records.head == baseline.records.head
    assert repeated.state == baseline.state
    assert artifact_bytes(repeated.bundle.parent) == artifact_bytes(
        baseline.bundle.parent
    )


def test_nonzero_fees_slippage_are_charged_once(tmp_path):
    from delayed_replay_stage4_helpers import fixture_plan

    plan = fixture_plan()
    config = replace(
        plan.signals.base_config, commission_rate=0.001, slippage_pct=0.001
    )
    plan = replace(
        plan,
        account_policy=replace(
            plan.account_policy,
            commission_rate="0.001",
            slippage_pct="0.001",
            reservation_buffer_pct="0.1",
        ),
        monthly=replace(
            plan.monthly, evaluator=replace(plan.monthly.evaluator, base_config=config)
        ),
        signals=replace(plan.signals, base_config=config),
    )
    actual = run(tmp_path / "fees", plan)
    reconcile(plan, actual.records)
    assert actual.state["account"]["episodes"]
    assert all(
        Decimal(o["fill"]["commission"]) > 0
        for o in actual.state["account"]["orders"].values()
        if o["status"] == "filled"
    )
    assert actual.result.to_dict()["label"] == "INCONCLUSIVE"


def test_future_repricing_preserves_one_month_with_positive_control(baseline, tmp_path):
    plan = baseline.plan
    snapshot = plan.market.snapshots[0]
    bars = tuple(
        replace(
            b,
            raw_ohlcv=tuple(
                round(v * 1.1, 6) if i < 4 else v for i, v in enumerate(b.raw_ohlcv)
            ),
        )
        if b.session_date >= date(2024, 8, 1)
        else b
        for b in snapshot.observations
    )
    changed = replace(
        plan,
        market=replace(plan.market, snapshots=(reprice(snapshot, observations=bars),)),
    )
    actual = run(tmp_path / "future", changed)
    assert actual.records.identity != baseline.records.identity
    assert actual.state["epochs"] == baseline.state["epochs"]
    before = [
        (e, s)
        for e, s in timeline(baseline.records)
        if e.command.payload.to_dict()["index"] < 14
    ]
    after = [
        (e, s)
        for e, s in timeline(actual.records)
        if e.command.payload.to_dict()["index"] < 14
    ]

    # Only provenance hashes differ: retain all economic values, dates and reasons.
    def economic(value):
        excluded = {"snapshot_hash", "batch_hash", "operations"}
        if isinstance(value, dict):
            return {k: economic(v) for k, v in value.items() if k not in excluded}
        if isinstance(value, list):
            return [economic(v) for v in value]
        return value

    for (_, old), (_, new) in zip(before, after, strict=True):
        for field in (
            "account",
            "position_states",
            "decisions",
            "epochs",
            "cursor",
            "visible",
        ):
            assert economic(old[field]) == economic(new[field])
    assert actual.pair[0].equity == baseline.pair[0].equity
    assert actual.pair[0].completed_trades == baseline.pair[0].completed_trades
    assert actual.pair[1].equity != baseline.pair[1].equity
    assert actual.candidate.candidate_id == candidate_for(plan).candidate_id


def test_offline_guards_fail_before_connection():
    import requests

    from data.providers.jquants_v2 import JQuantsV2Provider
    from data.providers.yfinance import YFinanceProvider

    for call in (
        lambda: requests.get("https://example.invalid"),
        lambda: JQuantsV2Provider.get_daily_bars(None),
        lambda: YFinanceProvider.get_daily_bars(None),
        lambda: socket.create_connection(("example.invalid", 443)),
    ):
        with pytest.raises(AssertionError, match="offline_e2e"):
            call()
    with socket.socket() as sock, pytest.raises(AssertionError, match="offline_e2e"):
        sock.connect(("192.0.2.1", 443))
    left, right = socket.socketpair()
    try:
        left.sendall(b"local-ipc")
        assert right.recv(9) == b"local-ipc"
    finally:
        left.close()
        right.close()
