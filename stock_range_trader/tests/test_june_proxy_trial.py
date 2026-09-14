"""Offline-only June preparation and unchanged May financial golden results."""

import copy
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from delayed_replay_e2e_helpers import network_guard as network_guard
from test_selected_proxy_trial import Response, artifacts

from delayed_replay import june_trial as june
from delayed_replay.input_artifacts import InputArtifactStore
from delayed_replay.selected_trial.acquisition import AcquisitionStopped, Receipt
from delayed_replay.selected_trial.service import SelectedService, generated_rows
from delayed_replay.selected_trial.workflow import write_once
from delayed_replay.serialization import JsonObject
from examples.june_proxy_trial import main
from examples.validate_limited_proxy import compare

pytestmark = pytest.mark.usefixtures("network_guard")


@pytest.fixture
def prepared(tmp_path):
    may, root = tmp_path / "may", tmp_path / "june"
    _, parent, auth, clock, *_ = artifacts(may)
    write_once(may / "trial_plan.json", parent.payload.to_dict())
    clock.value = datetime(2026, 9, 25, tzinfo=UTC)
    plan = june.prepare(
        root, may, "artificial-review-only-not-owner-approval", now=clock.now()
    )
    _, bundle, cal = june.load_context(root, may)
    return root, may, plan, parent, auth, clock, bundle, cal


def responses(bundle, old_calendar):
    june_days = [(date(2026, 6, 1) + timedelta(days=i)).isoformat() for i in range(30)]
    # A deliberately different synthetic calendar, not an inferred live calendar.
    cal = [r for r in old_calendar if r["Date"] >= "2026-05-29"] + [
        dict(
            Date=d,
            HolDiv="1"
            if date.fromisoformat(d).weekday() < 5 and d != "2026-06-10"
            else "0",
        )
        for d in june_days
    ]
    history = [
        dict(
            Date=d,
            Code=r["Code"],
            **dict(zip(june.DAILY_FIELDS, r["values"], strict=True)),
            ExRT=r["ExRT"],
        )
        for d, r in sorted(june.history_values(bundle).items())
    ]
    run = list(
        generated_rows(
            [
                r["Date"]
                for r in cal
                if r["Date"] >= "2026-06-01" and r["HolDiv"] == "1"
            ],
            "46890",
        ).values()
    )
    return [cal, [dict(Date="2026-06-01", Code="46890", ProdCat="011")], history, run]


def transport_for(receipt, bundle, calendar, clock, mutate=None):
    rows = responses(bundle, calendar)
    if mutate:
        mutate(rows)
    calls = []

    def request(path, params, timeout):
        q = dict(path=path, params=params)
        calls.append(q)
        return Response(rows[june.queries().index(q)])

    return june.JuneTransport(receipt, request=request, sleep=clock.sleep), calls


def test_prepare_is_not_acquisition_or_execution_approval(prepared):
    root, may, plan, *_ = prepared
    result = june.inspect(root, may)
    assert result["settings_hash"] == june.SETTINGS_HASH
    assert result["communication"]["attempts"] == 0
    assert result["clearing"] == "not_executed"
    assert plan.payload.to_dict()["scope"] == june.SCOPE
    auth = june.load_json(root / "acquisition_authorization.template.json")
    assert auth["status"] == "not_approved" and auth["execution_permission"] is False
    with pytest.raises(ValueError, match="new_output"):
        june.prepare(root, may, "test")
    assert not list(root.glob("*continuous*"))


def test_new_lot_subject_preserves_old_review_and_pdf(prepared):
    root, may, plan, parent, *_ = prepared
    old = parent.payload.to_dict()["references"]["lot"]
    old_bytes = (may / (old["review_hash"] + ".json")).read_bytes()
    r = june.read(root, plan.payload.to_dict()["lot_review_hash"])
    assert r["start"] == "2026-06-01" and r["end"] == "2026-07-01"
    assert r["subject_hash"] != old["subject_hash"]
    june.require_lot(r, may, parent)
    assert (may / (old["review_hash"] + ".json")).read_bytes() == old_bytes
    r["end"] = "2026-06-01"
    with pytest.raises(ValueError, match="lot_review_scope"):
        june.require_lot(r, may, parent)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_attempts", True),
        ("max_attempts", 21),
        ("max_seconds", -1),
        ("not_before", "2026-09-14"),
        ("execution_permission", "approved"),
    ],
)
def test_scope_mutation_refused(prepared, field, value):
    p = prepared[2].payload.to_dict()
    p[field] = value
    with pytest.raises(ValueError):
        june.JunePlan(JsonObject.from_value(p))


def test_settings_and_model_tamper_refused(prepared):
    root, may, plan, *_ = prepared
    p = plan.payload.to_dict()
    p["settings"]["terms"]["reservation_buffer_pct"] = "0.02"
    with pytest.raises(ValueError, match="settings_changed"):
        june.JunePlan(JsonObject.from_value(p))
    p = plan.payload.to_dict()
    p["implementation_hash"] = "0" * 64
    (root / "plan.json").write_text(JsonObject.from_value(p).encoded)
    with pytest.raises(ValueError, match="implementation_changed"):
        june.load_context(root, may)


@pytest.mark.parametrize(
    "field", list(june.DAILY_FIELDS) + ["ExRT", "Date", "Code", "missing", "duplicate"]
)
def test_history_all_fields_revision_refused_before_june_fetch(prepared, field):
    root, _, plan, _, _, clock, bundle, calendar = prepared

    def mutate(rows):
        if field == "missing":
            rows[2].pop()
        elif field == "duplicate":
            rows[2].append(copy.deepcopy(rows[2][0]))
        else:
            rows[2][0][field] = "2" if field != "Date" else "2026-06-01"

    with_receipt = Receipt(root / "acquisition.sqlite", plan, now=clock.now)
    try:
        transport, calls = transport_for(with_receipt, bundle, calendar, clock, mutate)
        with pytest.raises(ValueError):
            june.acquire_inputs(transport, bundle, calendar)
        assert len(calls) == 3 and june.queries()[3] not in calls
    finally:
        with_receipt.close()


def test_semantic_history_equal_despite_order_and_decimal_rendering(prepared):
    *_, bundle, cal = prepared
    rows = responses(bundle, cal)[2][::-1]
    rows[0]["C"] += "0" if "." in rows[0]["C"] else ".0"
    c = dict(query=june.queries()[2], data=rows, fetched_at="different-not-compared")
    assert june.compare_history(bundle, c)["status"] == "matched"


def test_all_four_requests_build_calendar_parts_without_account(prepared):
    root, may, plan, _, _, clock, bundle, calendar = prepared
    before = {p.name: p.read_bytes() for p in (may / "inputs").glob("*.json")}
    receipt = Receipt(root / "acquisition.sqlite", plan, now=clock.now)
    try:
        transport, calls = transport_for(receipt, bundle, calendar, clock)
        captured = june.acquire_inputs(transport, bundle, calendar)
        assert calls == june.queries()
        assert june.receipt_captures(receipt) == captured
    finally:
        receipt.close()
    built = june.build(root, may)
    assert built["input_ready"] and built["executable"] is False
    assert list(map(len, built["parts"])) == [10, 11]
    assert "2026-06-10" not in sum(built["parts"], [])
    assert built == june.build(root, may)
    store = InputArtifactStore(root / "inputs")
    for h, dates in zip(built["run_packets"], built["parts"], strict=True):
        packet = store.load(h)
        assert [
            o.session_date.isoformat() for o in packet.market.snapshots[0].observations
        ] == dates
        assert packet.market.snapshots[0].fetched_at == clock.now()
    assert {p.name: p.read_bytes() for p in (may / "inputs").glob("*.json")} == before
    assert not list(root.glob("*continuous*"))


def test_calendar_odd_even_and_shuffling():
    for count in (18, 21, 22):
        rows = [
            dict(Date=f"2026-06-{i:02d}", HolDiv="1" if i <= count else "0")
            for i in range(1, 31)
        ]
        a, b = june.split_sessions(rows[::-1])
        assert (len(a), len(b)) == (count // 2, count - count // 2)
        assert a + b == tuple(sorted(a + b))
    with pytest.raises(ValueError):
        june.split_sessions(rows[:-1])


def test_request_allowlist_and_history_first(prepared):
    root, _, plan, _, _, clock, bundle, calendar = prepared
    r = Receipt(root / "acquisition.sqlite", plan, now=clock.now)
    try:
        t, calls = transport_for(r, bundle, calendar, clock)
        with pytest.raises(ValueError, match="history_first"):
            t.fetch(june.queries()[3])
        bad = copy.deepcopy(june.queries()[2])
        bad["params"]["code"] = "94320"
        with pytest.raises(ValueError, match="outside_plan"):
            t.fetch(bad)
        assert not calls
    finally:
        r.close()


def test_resume_preserves_attempts_deadline_and_reuses_capture(prepared):
    root, _, plan, _, _, clock, bundle, calendar = prepared
    r = Receipt(root / "acquisition.sqlite", plan, now=clock.now)
    t, calls = transport_for(r, bundle, calendar, clock)
    t.fetch(june.queries()[0])
    started = r.statistics()["started_at"]
    r.close()
    clock.sleep(30)
    r = Receipt(root / "acquisition.sqlite", plan, now=clock.now)
    try:
        t, calls = transport_for(r, bundle, calendar, clock)
        june.acquire_inputs(t, bundle, calendar)
        assert len(calls) == 3 and r.statistics()["attempts"] == 4
        assert r.statistics()["started_at"] == started
        clock.sleep(1200)
        with pytest.raises(ValueError, match="deadline_exhausted"):
            t.fetch(june.queries()[0])
    finally:
        r.close()


def test_twenty_attempts_no_reset(prepared):
    root, _, plan, _, _, clock, bundle, calendar = prepared
    r = Receipt(root / "acquisition.sqlite", plan, now=clock.now)
    for _ in range(20):
        r.append("attempt", dict(query=june.queries()[0]))
    r.close()
    r = Receipt(root / "acquisition.sqlite", plan, now=clock.now)
    try:
        t, calls = transport_for(r, bundle, calendar, clock)
        with pytest.raises(AcquisitionStopped, match="attempt_budget"):
            t.fetch(june.queries()[0])
        assert not calls
    finally:
        r.close()


def test_permission_date_and_missing_receipt_gates(prepared):
    root, may, plan, *_ = prepared
    p = plan.payload.to_dict()
    p["parent"]["provenance"] = "saved_jquants"
    plan = june.JunePlan(JsonObject.from_value(p))
    auth = dict(
        schema="june-acquisition-authorization-v1",
        plan_hash=plan.sha256,
        status="approved_for_acquisition",
        approval_reference="fake-owner-reference-unit-test",
        permission="acquire_only",
        execution_permission=False,
    )
    for day in (14, 22, 23):
        with pytest.raises(ValueError, match="window"):
            june.require_acquisition_permission(
                plan, auth, datetime(2026, 9, day, tzinfo=UTC), True
            )
    june.require_acquisition_permission(
        plan, auth, datetime(2026, 9, 25, tzinfo=UTC), True
    )
    auth["status"] = "not_approved"
    with pytest.raises(ValueError, match="separate_acquisition_approval"):
        june.require_acquisition_permission(
            plan, auth, datetime(2026, 9, 25, tzinfo=UTC), True
        )
    (root / "acquisition.sqlite").unlink()
    with pytest.raises(ValueError, match="receipt_missing"):
        june.load_context(root, may)
    assert not (root / "acquisition.sqlite").exists()


def test_cli_never_implicitly_acquires_or_clears(prepared):
    root, may, *_ = prepared
    args = ["--root", str(root), "--may-root", str(may)]
    assert main(["inspect", *args]) == 0
    assert main(["acquire", *args]) == 2
    assert main(["resume", *args]) == 2
    assert main(["build-inputs", *args]) == 2
    with pytest.raises(SystemExit):
        main(["execute", *args])


def test_empty_receipt_cannot_reset_budget(prepared):
    root, may, *_ = prepared
    (root / "acquisition.sqlite").write_bytes(b"")
    with pytest.raises(ValueError, match="receipt_invalid_no_reset"):
        june.inspect(root, may)
    assert (root / "acquisition.sqlite").read_bytes() == b""


def test_pdf_corruption_blocks_readiness(prepared):
    root, may, plan, *_ = prepared
    review = june.read(root, plan.payload.to_dict()["lot_review_hash"])
    (may / (review["documents"][0]["sha256"] + ".pdf")).write_bytes(b"changed")
    with pytest.raises(ValueError):
        june.load_context(root, may)


@pytest.mark.parametrize("failure", ["calendar", "master", "corporate_action"])
def test_invalid_evidence_stops_at_correct_request(prepared, failure):
    root, _, plan, _, _, clock, bundle, calendar = prepared

    def mutate(rows):
        if failure == "calendar":
            rows[0].pop()
        elif failure == "master":
            rows[1][0]["Date"] = "2026-06-02"
        else:
            rows[3][0]["AdjFactor"] = "0.5"

    r = Receipt(root / "acquisition.sqlite", plan, now=clock.now)
    try:
        t, calls = transport_for(r, bundle, calendar, clock, mutate)
        with pytest.raises(ValueError):
            june.acquire_inputs(t, bundle, calendar)
        assert (
            len(calls) == {"calendar": 1, "master": 2, "corporate_action": 4}[failure]
        )
    finally:
        r.close()


def test_nonfinite_features_publish_no_input_manifest(prepared, monkeypatch):
    import pandas as pd

    from config.settings import StrategyConfig

    root, may, plan, _, _, clock, bundle, calendar = prepared
    r = Receipt(root / "acquisition.sqlite", plan, now=clock.now)
    try:
        t, _ = transport_for(r, bundle, calendar, clock)
        june.acquire_inputs(t, bundle, calendar)
    finally:
        r.close()

    class Scorer:
        def transform(self, frame):
            return pd.DataFrame(
                [dict(sma=float("nan"), atr=1.0, adx=1.0, range_score=1.0)]
            )

    monkeypatch.setattr(StrategyConfig, "create_scorer", lambda _: Scorer())
    with pytest.raises(ValueError, match="nonfinite"):
        june.build(root, may)
    assert not (root / "input_manifest.json").exists()
    assert not (root / "inputs").exists()


def test_may_artificial_financial_golden_unchanged(prepared, tmp_path):
    _, may, _, parent, auth, clock, *_ = prepared
    out = tmp_path / "may_regression"
    report = compare(
        parent, auth, may, out, clock.now(), _service=SelectedService
    ).to_dict()
    assert report["logical_state_equal"] and report["prefix_unchanged"]
    with sqlite3.connect(f"file:{out}/continuous.sqlite?mode=ro", uri=True) as c:
        s = json.loads(c.execute("select current_json from streams").fetchone()[0])
    assert s["cash"] == s["equity"] == "202590.45"
    assert s["realized_profit"] == "2590.45" and s["positions"] == {}
    assert s["status"] == "completed" and s["reason"] == "no_next_bar_no_forced_exit"
    orders = list(s["orders"].values())
    actual = [
        (
            o["frozen"]["decision_session"],
            o["frozen"]["target"],
            o["frozen"]["side"],
            o["frozen"]["shares"],
            o["frozen"]["reference_price"],
            o["frozen"]["reserved"],
            o["frozen"]["budget"],
            o["resolution"]["price"],
            o["resolution"]["gross"],
            o["resolution"]["commission"],
            o["resolution"]["net"],
            o["status"],
        )
        for o in orders
    ]
    assert actual == [
        (
            "2026-05-11",
            "2026-05-12",
            "BUY",
            500,
            "97.65",
            "49414.37",
            "50000",
            "96.86",
            "48430",
            "48.43",
            "48478.43",
            "filled",
        ),
        (
            "2026-05-20",
            "2026-05-21",
            "SELL",
            500,
            "101.24",
            "0",
            "0",
            "102.24",
            "51120",
            "51.12",
            "51068.88",
            "filled",
        ),
    ]
