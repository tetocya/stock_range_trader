"""Artificial June only. These tests do not verify real-data clearing."""

import json

import pytest
from delayed_replay_e2e_helpers import network_guard as network_guard
from test_june_proxy_trial import prepared as prepared
from test_june_proxy_trial import transport_for

from delayed_replay import june_clearing as clear
from delayed_replay import june_trial as june
from delayed_replay.selected_trial.acquisition import Receipt
from delayed_replay.serialization import JsonObject, digest, time_text
from examples.june_proxy_trial import main

pytestmark = pytest.mark.usefixtures("network_guard")


def built(prepared, mutate=None):
    root, may, acquisition, _, _, clock, bundle, calendar = prepared
    receipt = Receipt(root / "acquisition.sqlite", acquisition, now=clock.now)
    try:
        transport, _ = transport_for(receipt, bundle, calendar, clock, mutate)
        june.acquire_inputs(transport, bundle, calendar)
    finally:
        receipt.close()
    manifest = june.build(root, may)
    plan = clear.prepare_clearing(root, may)
    auth = june.load_json(root / "clearing_authorization.template.json")
    auth.update(
        status="artificial_test_authorization",
        approval_reference="fixture_only_not_real_permission",
        recorded_at=time_text(clock.now()),
    )
    return (
        root,
        may,
        plan,
        clear.JuneClearingAuthorization(JsonObject.from_value(auth)),
        clock,
        manifest,
    )


def test_artificial_continuous_wait_accept_resume(prepared, tmp_path):
    root, may, plan, auth, clock, manifest = built(prepared)
    assert list(map(len, manifest["parts"])) == [10, 11]
    result = clear.compare(root, may, plan, auth, tmp_path / "comparison", clock.now())
    assert all(result["matches"].values())
    assert result["accepted_state_restored"] and result["prefix_unchanged"]
    assert result["real_data_clearing"] == "unverified"
    assert result["report"]["fill_count"] > 0
    service = clear.JuneClearingService.resume(
        tmp_path / "comparison" / "continuous.sqlite",
        plan,
        auth,
        root,
        may,
        "continuous",
    )
    try:
        state = service.state
        assert state["status"] == "completed"
        assert state["reason"] == "no_next_bar_no_forced_exit"
        assert all(
            "2026-06-01" <= o["frozen"]["decision_session"] < "2026-07-01"
            for o in state["orders"].values()
        )
        assert max(state["decisions"]) <= "2026-06-30"
        assert state["cash"] == state["equity"] == "206565.83"
        assert not state["positions"]
        assert result["report"]["fill_count"] == 4
        assert all(
            o["frozen"]["target"] < "2026-07-01" for o in state["orders"].values()
        )
    finally:
        service.store.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "unapproved",
        "acquisition_only",
        "wrong_manifest",
        "wrong_plan",
        "wrong_acquisition",
        "wrong_provenance",
    ],
)
def test_separate_authorization_rejects_before_account(prepared, tmp_path, mutation):
    root, may, plan, auth, *_ = built(prepared)
    grant = auth.grant.to_dict()
    if mutation == "unapproved":
        grant["status"] = "not_approved"
    elif mutation == "acquisition_only":
        grant = june.load_json(root / "acquisition_authorization.template.json")
    elif mutation == "wrong_provenance":
        grant["status"] = "approved_for_limited_trial"
    else:
        grant[
            {
                "wrong_manifest": "input_manifest_hash",
                "wrong_plan": "plan_hash",
                "wrong_acquisition": "acquisition_plan_hash",
            }[mutation]
        ] = "0" * 64
    path = tmp_path / "not-created.sqlite"
    with pytest.raises(ValueError):
        clear.JuneClearingService.create(
            path,
            plan,
            clear.JuneClearingAuthorization(JsonObject.from_value(grant)),
            root,
            may,
            "continuous",
        )
    assert not path.exists()


@pytest.mark.parametrize(
    "field",
    [
        "parts",
        "run_packets",
        "provenance",
        "history_comparison",
        "settings_hash",
        "executable",
    ],
)
def test_build_manifest_tampering_refused(prepared, field):
    root, may, plan, _, _, manifest = built(prepared)
    manifest[field] = {"unexpected": "mutation"}
    (root / "input_manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest_changed"):
        clear.verified_inputs(plan, root, may)


def test_real_cli_does_not_accept_artificial_or_implicit_permission(prepared, tmp_path):
    root, may, *_ = built(prepared)
    common = ["--root", str(root), "--may-root", str(may)]
    for action in (
        "start-clearing",
        "accept-inputs",
        "resume-clearing",
        "compare-clearing",
    ):
        assert main([action, *common]) == 2
        assert (
            main(
                [
                    action,
                    *common,
                    "--execute-saved-data",
                    "--clearing-authorization",
                    str(root / "clearing_authorization.template.json"),
                    "--account",
                    str(tmp_path / "no.sqlite"),
                ]
            )
            == 2
        )
    assert not (tmp_path / "no.sqlite").exists()


def test_new_account_no_may_orders_and_detached_state(prepared, tmp_path):
    root, may, plan, auth, clock, _ = built(prepared)
    service = clear.JuneClearingService.create(
        tmp_path / "account.sqlite", plan, auth, root, may, "split_resume"
    )
    try:
        original = service.state
        assert original["cash"] == original["equity"] == "200000"
        assert original["positions"] == original["orders"] == {}
        assert original["index"] == 0 and original["phase"] == "select"
        assert original["identity"]["run_sessions"][0] == "2026-06-01"
        original["cash"] = "0"
        assert service.state["cash"] == "200000"
        waiting = service.run(clock.now())
        assert waiting["status"] == "waiting_for_input"
        assert waiting["identity"]["run_sessions"][waiting["index"]] == "2026-06-16"
        assert waiting["decided_through"] == "2026-06-15"
        assert all(k < "46890|2026-06-16" for k in waiting["inputs"])
    finally:
        service.store.close()


def test_settings_frozen_and_prepare_not_approval(prepared):
    root, may, plan, _, _, manifest = built(prepared)
    p = plan.payload.to_dict()
    assert (
        digest({k: p[k] for k in ("strategy", "terms", "rules")}) == june.SETTINGS_HASH
    )
    assert not list(root.glob("*account*"))
    assert (
        june.load_json(root / "clearing_authorization.template.json")["status"]
        == "not_approved"
    )
    assert manifest["executable"] is False
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    assert clear.prepare_clearing(root, may) == plan
    assert all(p.read_bytes() == content for p, content in before.items())


@pytest.mark.parametrize("scenario", ["rejection", "terminal_position"])
def test_nonempty_rejection_and_terminal_holding(prepared, tmp_path, scenario):
    def mutate(responses):
        for row in responses[3]:
            if scenario == "rejection" and row["Date"] == "2026-06-02":
                # Ex-ante synthetic gap: keep the previously frozen 500 shares.
                row.update(O="110", AdjO="110", H="110.2", AdjH="110.2")
            if scenario == "terminal_position" and row["Date"] >= "2026-06-24":
                row.update(
                    O="96.8",
                    C="96.8",
                    H="97",
                    L="96.6",
                    AdjO="96.8",
                    AdjC="96.8",
                    AdjH="97",
                    AdjL="96.6",
                )

    root, may, plan, auth, clock, _ = built(prepared, mutate)
    result = clear.compare(root, may, plan, auth, tmp_path / scenario, clock.now())
    assert all(result["matches"].values())
    service = clear.JuneClearingService.resume(
        tmp_path / scenario / "continuous.sqlite", plan, auth, root, may, "continuous"
    )
    try:
        state = service.state
        assert result["report"]["fill_count"] > 0
        if scenario == "rejection":
            order = state["orders"]["2026-06-01:46890:BUY"]
            assert order["status"] == "rejected"
            assert order["frozen"]["shares"] == 500
            assert order["reserved_cash"] == "0"
            assert (
                order["resolution"]["reason"] == "frozen_reservation_or_budget_exceeded"
            )
        else:
            assert state["positions"]["46890"]["shares"] == 500
            assert state["positions"]["46890"]["candidate_id"] == "baseline"
            assert state["reason"] == "no_next_bar_no_forced_exit"
            assert state["equity"] != state["cash"]
            assert all(
                o["frozen"]["target"] < "2026-07-01" for o in state["orders"].values()
            )
    finally:
        service.store.close()


def test_pending_reservation_survives_close_reopen(prepared, tmp_path):
    root, may, plan, auth, clock, _ = built(prepared)
    path = tmp_path / "reservation.sqlite"
    service = clear.JuneClearingService.create(
        path, plan, auth, root, may, "continuous"
    )
    try:
        for _ in range(4):
            service.advance(clock.now())
        pending = service.state
        order = pending["orders"]["2026-06-01:46890:BUY"]
        assert order["status"] == "pending"
        assert order["reserved_cash"] == "48963.92"
        assert pending["cash"] == "200000"
    finally:
        service.store.close()
    service = clear.JuneClearingService.resume(
        path, plan, auth, root, may, "continuous"
    )
    try:
        assert service.state == pending
        service.advance(clock.now())  # finish the decision session
        service.advance(clock.now())  # select next session
        service.advance(clock.now())  # resolve the frozen order
        order = service.state["orders"]["2026-06-01:46890:BUY"]
        assert order["status"] == "filled"
        assert order["resolution"]["shares"] == 500
        assert order["resolution"]["price"] == "96.3"
        assert order["resolution"]["commission"] == "48.15"
        assert order["reserved_cash"] == "0"
        assert service.state["cash"] == "151801.85"
    finally:
        service.store.close()


def test_mutated_disk_input_refused_before_event(prepared, tmp_path):
    root, may, plan, auth, clock, manifest = built(prepared)
    service = clear.JuneClearingService.create(
        tmp_path / "account.sqlite", plan, auth, root, may, "continuous"
    )
    try:
        before = service.store.read()
        packet = root / "inputs" / (manifest["run_packets"][1] + ".json")
        packet.write_text("{}")
        with pytest.raises(ValueError, match="evidence_changed"):
            service.advance(clock.now())
        assert service.store.read() == before
    finally:
        service.store.close()


@pytest.mark.parametrize("mutation", ["july", "missing", "duplicate"])
def test_future_bar_and_missing_session_fail_before_clearing(prepared, mutation):
    root, may, acquisition, _, _, clock, bundle, calendar = prepared

    def mutate(rows):
        if mutation == "july":
            rows[3].append(dict(rows[3][-1], Date="2026-07-01"))
        elif mutation == "missing":
            rows[3].pop()
        else:
            rows[3].append(dict(rows[3][-1]))

    receipt = Receipt(root / "acquisition.sqlite", acquisition, now=clock.now)
    try:
        transport, _ = transport_for(receipt, bundle, calendar, clock, mutate)
        with pytest.raises(ValueError):
            june.acquire_inputs(transport, bundle, calendar)
    finally:
        receipt.close()
    with pytest.raises(ValueError):
        clear.prepare_clearing(root, may)
    assert not (root / "clearing_plan.json").exists()


def test_clear_grant_is_not_acquisition_permission(prepared):
    root, _, _, auth, clock, _ = built(prepared)
    value = june.load_json(root / "plan.json")
    value["parent"]["provenance"] = "saved_jquants"
    with pytest.raises(ValueError, match="separate_acquisition_approval"):
        june.require_acquisition_permission(
            june.JunePlan(JsonObject.from_value(value)),
            auth.grant.to_dict(),
            clock.now(),
            True,
        )


def test_actual_entry_rejects_unapproved_without_account(prepared, tmp_path):
    root, may, plan, _, _, _ = built(prepared)
    payload = plan.payload.to_dict()
    payload["provenance"] = "saved_jquants"
    proposed = clear.JuneClearingPlan(JsonObject.from_value(payload))
    # Gate check only: never consume real data or run a real account.
    (root / "clearing_plan.json").write_text(proposed.payload.encoded)
    auth = june.load_json(root / "clearing_authorization.template.json")
    auth["plan_hash"] = proposed.sha256
    (root / "unapproved.json").write_text(json.dumps(auth))
    account = tmp_path / "no-real-account.sqlite"
    assert (
        main(
            [
                "start-clearing",
                "--root",
                str(root),
                "--may-root",
                str(may),
                "--account",
                str(account),
                "--execute-saved-data",
                "--clearing-authorization",
                str(root / "unapproved.json"),
            ]
        )
        == 2
    )
    assert not account.exists()
