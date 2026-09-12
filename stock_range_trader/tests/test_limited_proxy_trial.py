"""Generated fixtures only: no saved 7B prices, API, or actual owner approvals."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from delayed_replay_e2e_helpers import network_guard as network_guard
from delayed_replay_stage4_helpers import fixture_plan

from data.price_policy import provider_price_basis
from delayed_replay.audit_errors import IdentityMismatch, InvalidEvent
from delayed_replay.checkpoints import LedgerCheckpointSource
from delayed_replay.daily_evidence import (
    JQUANTS_RESPONSE_SCHEMA,
    adapt_daily_response,
    response_payload,
)
from delayed_replay.input_artifacts import InputArtifactStore, InputPacket
from delayed_replay.limited_trial.inputs import (
    SavedProxyInputs,
    artificial_response_rows,
)
from delayed_replay.limited_trial.models import (
    LimitedProxyTrialPlan,
    ScopedResearchAuthorization,
)
from delayed_replay.limited_trial.preflight import LimitedTrialPreflight
from delayed_replay.limited_trial.proposal import saved_run_proposal
from delayed_replay.limited_trial.service import (
    LimitedTrialService,
    _LimitedReducer,
    signal_view,
)
from delayed_replay.market_view import MarketView
from delayed_replay.proxy.input_adapter import ProxyInputStore
from delayed_replay.proxy.service import ProxyService
from delayed_replay.replay_policy import audit_value
from delayed_replay.serialization import JsonObject, time_text
from delayed_replay.snapshot import PriceObservation, PriceSnapshot
from delayed_replay.validation import ReplayContractError
from examples.validate_limited_proxy import compare, main

pytestmark = pytest.mark.usefixtures("network_guard")
WALL = datetime(2026, 9, 12, tzinfo=UTC)
SESSIONS = (
    "2026-05-01",
    "2026-05-07",
    "2026-05-08",
    "2026-05-11",
    "2026-05-12",
    "2026-05-13",
    "2026-05-14",
    "2026-05-15",
    "2026-05-18",
    "2026-05-19",
    "2026-05-20",
    "2026-05-21",
    "2026-05-22",
    "2026-05-25",
    "2026-05-26",
    "2026-05-27",
    "2026-05-28",
    "2026-05-29",
)


def write_capture(root, value):
    payload = JsonObject.from_value(value)
    path = root / (payload.sha256 + ".json")
    path.write_text(payload.encoded)
    return payload.sha256


def fixture(root, *, history=True, default_strategy=False):
    root.mkdir(parents=True, exist_ok=True)
    past = tuple(f"2026-04-{i:02}" for i in range(1, 21)) if history else ()
    generated = artificial_response_rows(past + SESSIONS)
    source_hash = write_capture(
        root,
        dict(
            fixture_generator="limited-artificial-v1",
            fixture_sessions=list(past + SESSIONS),
            data=[generated[d] for d in SESSIONS],
        ),
    )
    daily = []
    for day in SESSIONS:
        response = generated[day]
        daily.append(
            adapt_daily_response(
                response,
                provider="jquants",
                basis=provider_price_basis("jquants"),
                response_schema=JQUANTS_RESPONSE_SCHEMA,
                symbol="72030",
                session=date.fromisoformat(day),
                first_observed_at=WALL,
                fetched_at=WALL,
                snapshot_hash=response_payload(response).sha256,
            ).to_dict()
        )
    daily_hash = write_capture(
        root,
        dict(schema="stage7b-daily-capture-1", fetched_at=time_text(WALL), data=daily),
    )
    master = write_capture(
        root,
        dict(
            schema="stage7b-master-capture-1",
            data=[dict(Code="72030", Date="2026-05-07", CoName="ARTIFICIAL")],
        ),
    )
    calendar = write_capture(
        root,
        dict(
            schema="stage7b-calendar-capture-1",
            data=[
                dict(
                    Date=f"2026-05-{i:02}",
                    HolDiv="1" if f"2026-05-{i:02}" in SESSIONS else "0",
                )
                for i in range(1, 32)
            ],
        ),
    )
    store = InputArtifactStore(root / "inputs")

    def packet(days):
        bars = []
        for day in days:
            r = generated[day]
            values = tuple(float(r[k]) for k in ("O", "H", "L", "C", "Vo"))
            bars.append(
                PriceObservation(
                    "72030",
                    date.fromisoformat(day),
                    WALL,
                    values,
                    values,
                    1.0,
                    0.0,
                    0.0,
                )
            )
        snapshot = PriceSnapshot.create(
            provider="jquants",
            provider_price_basis=provider_price_basis("jquants"),
            source_artifact_sha256=source_hash,
            data_version="artificial-limited-v1",
            first_observed_at=WALL,
            fetched_at=WALL,
            provider_published_at=None,
            publication_time_unknown_reason="artificial_fixture",
            price_basis_evidence_id="artificial_only",
            observations=tuple(bars),
        )
        return store.publish(InputPacket(MarketView((snapshot,), ())))

    parts = [packet(SESSIONS[:9]), packet(SESSIONS[9:])]
    report = dict(
        snapshot_hashes=parts,
        daily_source_hash=source_hash,
        daily_capture_hash=daily_hash,
        master_capture_hash=master,
        calendar_capture_hash=calendar,
    )
    (root / "report.json").write_text(json.dumps(report))
    proposed = saved_run_proposal(root).payload.to_dict()
    proposed["provenance"] = "artificial_fixture"
    proposed["history_packets"] = [packet(past)] if past else []
    old = fixture_plan()
    if not default_strategy:
        proposed["strategy"] = audit_value(old.signals.base_config)
        proposed["candidates"] = audit_value(old.signals.catalog.candidates)
        proposed["candidate_id"] = "fast"
        proposed["terms"].update(
            max_position_pct="0.4",
            max_positions=2,
            commission_rate="0",
            slippage_pct="0",
            reservation_buffer_pct="0",
        )
    lot = dict(instrument="72030", start="2026-05-01", end="2026-06-01", lot_size=100)
    proposed["references"]["lot"] = {
        **lot,
        "source": "artificial-period-proof",
        "subject_hash": JsonObject.from_value(lot).sha256,
        "review_hash": "b" * 64,
        "acquired_at": time_text(WALL),
        "authenticity": "artificial_fixture",
    }
    plan = LimitedProxyTrialPlan(JsonObject.from_value(proposed))
    auth = ScopedResearchAuthorization(
        JsonObject.from_value(
            dict(
                schema="limited-authorization-v1",
                plan_hash=plan.sha256,
                status="artificial_test_authorization",
                approval_reference="artificial-fixture-not-owner-approval",
                recorded_at=time_text(WALL),
            )
        )
    )
    return plan, auth


def changed(plan, **changes):
    return LimitedProxyTrialPlan(
        JsonObject.from_value({**plan.payload.to_dict(), **changes})
    )


def service(root, plan, auth, style="continuous", filename="account.sqlite"):
    return LimitedTrialService.create(
        root / filename, plan, auth, root / "inputs", root, style
    )


def test_artificial_authorized_comparison_positive_control(tmp_path):
    p, a = fixture(tmp_path / "source")
    d = LimitedTrialPreflight.evaluate(
        p, tmp_path / "source/inputs", tmp_path / "source", a
    ).to_dict()
    assert d["ready"], d
    r = compare(p, a, tmp_path / "source", tmp_path / "comparison", WALL).to_dict()
    assert r["logical_state_equal"] and r["prefix_unchanged"]
    assert r["report"]["fill_count"] > 0
    assert (
        r["report"]["capability"]["approval_status"] == "artificial_test_authorization"
    )
    assert r["report"]["capability"]["model_approval"] == "unapproved"
    assert r["report"]["formal_checkpoint"] == "unsupported"


def test_preflight_success_without_approval_does_not_clear(tmp_path):
    p, _ = fixture(tmp_path)
    before = {x: x.read_bytes() for x in tmp_path.rglob("*.json")}
    d = LimitedTrialPreflight.evaluate(p, tmp_path / "inputs", tmp_path).to_dict()
    assert d["reasons"] == ["approval_pending"]
    assert d["fill_count"] is None and d["clearing"] == "not_executed"
    with pytest.raises(ReplayContractError, match="approval_required"):
        service(tmp_path, p, None)
    assert not (tmp_path / "account.sqlite").exists()
    assert before == {x: x.read_bytes() for x in tmp_path.rglob("*.json")}


@pytest.mark.parametrize(
    "mutation", ["policy", "candidate", "source", "model", "input"]
)
def test_authorization_hash_binds_entire_plan(mutation, tmp_path):
    p, a = fixture(tmp_path)
    raw = p.payload.to_dict()
    if mutation == "policy":
        raw["terms"]["reservation_buffer_pct"] = "0.01"
    elif mutation == "candidate":
        raw["candidate_id"] = "slow"
    elif mutation == "source":
        raw["source_identity"] = "different-source"
    elif mutation == "model":
        raw["model_hash"] = "f" * 64
    else:
        raw["packets"][1] = "c" * 64
    with pytest.raises(ReplayContractError, match="scope_mismatch"):
        service(tmp_path, LimitedProxyTrialPlan(JsonObject.from_value(raw)), a)
    assert not (tmp_path / "account.sqlite").exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("symbol", "70110"),
        ("end", "2026-06-02"),
        ("start", "2026-04-01"),
        ("mode", "formal_oos"),
    ],
)
def test_exact_trial_scope(key, value, tmp_path):
    p, _ = fixture(tmp_path)
    with pytest.raises(ReplayContractError, match="plan_scope"):
        changed(p, scope={**p.payload.to_dict()["scope"], key: value})


def test_insufficient_18_row_history_not_zero_fill(tmp_path):
    p, a = fixture(tmp_path, history=False, default_strategy=True)
    d = LimitedTrialPreflight.evaluate(p, tmp_path / "inputs", tmp_path, a).to_dict()
    assert "insufficient_history" in d["reasons"]
    assert d["history"]["available_before_start"] == 0
    assert d["history"]["available_in_period"] == 18
    assert d["history"]["required_before_first_close"] == 78
    assert d["history"]["requirements"]["adx"] == 28
    assert d["fill_count"] is None and not d["ready"]
    with pytest.raises(ReplayContractError, match="insufficient_history"):
        service(tmp_path, p, a)


@pytest.mark.parametrize("mutation", ["missing", "period", "hash", "unit"])
def test_lot_evidence_effective_period_and_review(mutation, tmp_path):
    p, _ = fixture(tmp_path)
    refs = p.payload.to_dict()["references"]
    if mutation == "missing":
        refs["lot"] = None
    elif mutation == "period":
        refs["lot"]["start"] = "2026-05-07"
        refs["lot"]["subject_hash"] = JsonObject.from_value(
            {k: refs["lot"][k] for k in ("instrument", "start", "end", "lot_size")}
        ).sha256
    elif mutation == "hash":
        refs["lot"]["subject_hash"] = "e" * 64
    else:
        refs["lot"]["lot_size"] = True
    d = LimitedTrialPreflight.evaluate(
        changed(p, references=refs), tmp_path / "inputs", tmp_path
    ).to_dict()
    assert d["checks"]["lot"]["status"] == (
        "failed" if mutation == "hash" else "blocked"
    )


def test_basis_unknown_is_unsupported_external_review_not_added_as_gate(tmp_path):
    p, a = fixture(tmp_path)
    assert LimitedTrialPreflight.evaluate(
        p, tmp_path / "inputs", tmp_path, a
    ).to_dict()["ready"]
    refs = p.payload.to_dict()["references"]
    assert refs["external_price"] is None
    refs["price"]["basis"] = "unknown"
    d = LimitedTrialPreflight.evaluate(
        changed(p, references=refs), tmp_path / "inputs", tmp_path
    ).to_dict()
    assert d["checks"]["price_basis"]["status"] == "unsupported"


@pytest.mark.parametrize("damage", ["calendar", "capture", "packet"])
def test_evidence_corruption_fails_closed(damage, tmp_path):
    p, a = fixture(tmp_path)
    raw = p.payload.to_dict()
    sha = (
        raw["packets"][0]
        if damage == "packet"
        else raw["captures"]["calendar" if damage == "calendar" else "daily_capture"]
    )
    path = (tmp_path / "inputs" if damage == "packet" else tmp_path) / (sha + ".json")
    path.write_text("{}")
    d = LimitedTrialPreflight.evaluate(p, tmp_path / "inputs", tmp_path, a).to_dict()
    assert d["status"] == "failed"
    with pytest.raises(ReplayContractError):
        service(tmp_path, p, a)


def test_synthetic_and_saved_authorization_cannot_be_interchanged(tmp_path):
    p, a = fixture(tmp_path)
    saved = changed(p, provenance="saved_jquants")
    with pytest.raises(ReplayContractError):
        a.require(saved)
    with pytest.raises(ReplayContractError, match="provenance_mismatch"):
        SavedProxyInputs.load(saved, tmp_path / "inputs", tmp_path)
    auth = replace(
        a,
        payload=JsonObject.from_value(
            {**a.payload.to_dict(), "status": "approved_for_limited_trial"}
        ),
    )
    with pytest.raises(ReplayContractError):
        auth.require(p)


def test_original_synthetic_entry_rejects_real_input_packet(tmp_path):
    from test_daily_open_proxy import plan_and_packet

    p, _ = fixture(tmp_path)
    packet = InputArtifactStore(tmp_path / "inputs").load(
        p.payload.to_dict()["packets"][0]
    )
    old, _ = plan_and_packet()
    with pytest.raises(ReplayContractError):
        ProxyService.create(
            tmp_path / "old.sqlite", old, ProxyInputStore(tmp_path / "proxy"), packet
        )


def test_rollback_and_separate_delivery_id_no_double_clearing(tmp_path):
    p, a = fixture(tmp_path)
    s = service(tmp_path, p, a)
    for _ in range(120):
        before = s.state
        if before["phase"] == "resolve" and any(
            o["status"] == "pending" for o in before["orders"].values()
        ):
            break
        s.advance(WALL)
    else:
        pytest.fail("no pending batch")

    def fault(point):
        if point == "after_state_update":
            raise RuntimeError("fault")

    with pytest.raises(RuntimeError):
        s.advance(WALL, fault=fault)
    assert s.state == before
    s.advance(WALL)
    after, records = s.state, s.store.read()
    cmd = records.events[-1].command
    s.store.commit_event(
        replace(cmd, event_id="another-delivery"), records.head, _LimitedReducer()
    )
    assert s.state == after
    s.store.close()


def test_stream_identity_checkpoint_and_restart_protection(tmp_path):
    p, a = fixture(tmp_path)
    s = service(tmp_path, p, a)
    s.advance(WALL)
    with pytest.raises(IdentityMismatch):
        LedgerCheckpointSource.from_store(s.store, s.store.read().head)
    with pytest.raises(ReplayContractError):
        service(tmp_path, p, a)
    state = s.state
    s.store.close()
    s = LimitedTrialService.resume(
        tmp_path / "account.sqlite", p, a, tmp_path / "inputs", tmp_path, "continuous"
    )
    assert s.state == state
    s.store.close()
    with pytest.raises(IdentityMismatch):
        LimitedTrialService.resume(
            tmp_path / "account.sqlite",
            p,
            a,
            tmp_path / "inputs",
            tmp_path,
            "split_resume",
        )


def test_cli_preflight_collision_and_no_authorization_generation(tmp_path):
    p, _ = fixture(tmp_path / "source")
    plan = tmp_path / "plan.json"
    plan.write_text(p.payload.encoded)
    args = [
        "preflight",
        "--saved-run",
        str(tmp_path / "source"),
        "--plan",
        str(plan),
        "--output",
        str(tmp_path / "out"),
    ]
    assert main(args) == 0
    assert main(args) == 2
    assert sorted(x.name for x in (tmp_path / "out").iterdir()) == [
        "plan.json",
        "preflight.json",
    ]
    assert not list(tmp_path.rglob("*.sqlite"))


def test_late_input_parent_competition_and_source_prefix_immutable(tmp_path):
    p, a = fixture(tmp_path)
    original = {x: x.read_bytes() for x in (tmp_path / "inputs").iterdir()}
    s = service(tmp_path, p, a, "split_resume")
    waiting = s.run(WALL)
    assert waiting["status"] == "waiting_for_input"
    head = s.state["input_head"]
    sha = p.payload.to_dict()["packets"][1]
    prefix = s.store.read().events
    s.accept(sha, "one", head, WALL)
    with pytest.raises(InvalidEvent):
        s.accept(sha, "conflict", head, WALL)
    assert s.store.read().events[: len(prefix)] == prefix
    assert original == {x: x.read_bytes() for x in (tmp_path / "inputs").iterdir()}
    assert s.run(WALL)["status"] == "completed"
    s.store.close()


def test_public_signal_view_detached_and_clock_bound(tmp_path):
    p, a = fixture(tmp_path)
    s = service(tmp_path, p, a)
    before = s.state
    frame = signal_view(before, "72030", "2026-05-01", WALL)
    assert all(frame.date.dt.date < date(2026, 5, 1))
    frame.loc[:, "close"] = 999
    assert s.state == before
    with pytest.raises(ReplayContractError):
        signal_view(before, "72030", "2026-05-01", WALL - timedelta(days=1))
    s.store.close()


@pytest.mark.parametrize(
    "field,value,status",
    [
        ("slippage_pct", None, "blocked"),
        ("lot_size", True, "unsupported"),
        ("commission_rate", "NaN", "unsupported"),
        ("price_quantum", "0", "unsupported"),
        ("cost_model", "minimum_fee", "unsupported"),
    ],
)
def test_no_implicit_policy_values(field, value, status, tmp_path):
    p, _ = fixture(tmp_path)
    terms = p.payload.to_dict()["terms"]
    terms[field] = value
    d = LimitedTrialPreflight.evaluate(
        changed(p, terms=terms), tmp_path / "inputs", tmp_path
    ).to_dict()
    assert d["checks"]["configuration"]["status"] == status


def test_unknown_time_condition_unsupported_actual_time_not_required(tmp_path):
    p, a = fixture(tmp_path)
    assert LimitedTrialPreflight.evaluate(
        p, tmp_path / "inputs", tmp_path, a
    ).to_dict()["ready"]
    rules = p.payload.to_dict()["rules"]
    rules["time_condition"] = "before_09:05"
    d = LimitedTrialPreflight.evaluate(
        changed(p, rules=rules), tmp_path / "inputs", tmp_path
    ).to_dict()
    assert d["checks"]["configuration"]["status"] == "unsupported"


def test_semantically_missing_calendar_not_inferred_from_prices(tmp_path):
    p, _ = fixture(tmp_path)
    captures = p.payload.to_dict()["captures"]
    old = json.loads((tmp_path / (captures["calendar"] + ".json")).read_text())
    old["data"].pop()
    captures["calendar"] = write_capture(tmp_path, old)
    d = LimitedTrialPreflight.evaluate(
        changed(p, captures=captures), tmp_path / "inputs", tmp_path
    ).to_dict()
    assert d["checks"]["saved_inputs"]["status"] == "blocked"


def test_forged_artificial_source_flag_is_not_enough(tmp_path):
    p, _ = fixture(tmp_path)
    captures = p.payload.to_dict()["captures"]
    old = json.loads((tmp_path / (captures["daily_source"] + ".json")).read_text())
    old["data"][0]["O"] = "110"
    captures["daily_source"] = write_capture(tmp_path, old)
    with pytest.raises(ReplayContractError, match="artificial_generation_mismatch"):
        SavedProxyInputs.load(
            changed(p, captures=captures), tmp_path / "inputs", tmp_path
        )


@pytest.mark.parametrize(
    "halt,status",
    [("full", "failed"), ("temporary", "ready"), ("delayed_first_trade", "ready")],
)
def test_halt_quality_contract(halt, status, tmp_path):
    p, _ = fixture(tmp_path)
    refs = p.payload.to_dict()["references"]
    refs["halt"]["observations"][SESSIONS[0]] = halt
    p = changed(p, references=refs)
    a = ScopedResearchAuthorization(
        JsonObject.from_value(
            dict(
                schema="limited-authorization-v1",
                plan_hash=p.sha256,
                status="artificial_test_authorization",
                approval_reference="artificial-only",
                recorded_at=time_text(WALL),
            )
        )
    )
    assert (
        LimitedTrialPreflight.evaluate(p, tmp_path / "inputs", tmp_path, a).to_dict()[
            "status"
        ]
        == status
    )


def test_zero_fill_does_not_verify_clearing_or_order_waiting(tmp_path):
    p, _ = fixture(tmp_path)
    terms = p.payload.to_dict()["terms"]
    terms["max_position_pct"] = "0.0001"
    p = changed(p, terms=terms)
    a = ScopedResearchAuthorization(
        JsonObject.from_value(
            dict(
                schema="limited-authorization-v1",
                plan_hash=p.sha256,
                status="artificial_test_authorization",
                approval_reference="artificial-zero-fill",
                recorded_at=time_text(WALL),
            )
        )
    )
    s = service(tmp_path, p, a)
    s.run(WALL)
    r = s.report().to_dict()
    assert r["fill_count"] == 0 and r["zero_fill_is_not_clearing_verification"]
    assert r["reasons"] == ["insufficient_lot_budget"]
    s.store.close()


def test_post_commit_reply_loss_resume_no_second_fill(tmp_path):
    p, a = fixture(tmp_path)
    s = service(tmp_path, p, a)
    for _ in range(120):
        before = s.state
        if before["phase"] == "resolve" and any(
            o["status"] == "pending" for o in before["orders"].values()
        ):
            break
        s.advance(WALL)
    else:
        pytest.fail("no order")

    def fault(point):
        if point == "after_commit":
            raise RuntimeError("reply_lost")

    with pytest.raises(RuntimeError):
        s.advance(WALL, fault=fault)
    committed, records = s.state, s.store.read()
    s.store.close()
    s = LimitedTrialService.resume(
        tmp_path / "account.sqlite", p, a, tmp_path / "inputs", tmp_path, "continuous"
    )
    s.store.commit_event(records.events[-1].command, records.head, _LimitedReducer())
    assert s.state == committed
    s.store.close()


@pytest.mark.parametrize("mutation", ["extra", "missing", "bool"])
def test_candidate_schema_does_not_ignore_execution_terms(mutation, tmp_path):
    p, _ = fixture(tmp_path)
    candidates = p.payload.to_dict()["candidates"]
    if mutation == "extra":
        candidates[0]["initial_capital"] = "999999"
    elif mutation == "bool":
        candidates[0]["buy_atr_multiplier"] = True
    else:
        del candidates[0]["buy_atr_multiplier"]
    p = changed(p, candidates=candidates)
    with pytest.raises(ReplayContractError, match="limited_candidate_schema"):
        p.signals()
