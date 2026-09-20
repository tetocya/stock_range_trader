"""Pure observation of artificial saved preparations, receipts and accounts."""

import json
import socket
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_june_clearing import built
from test_june_proxy_trial import prepared as prepared  # noqa: F401

from delayed_replay import june_clearing as clear
from delayed_replay import june_trial as june
from delayed_replay.event_store import EventStore
from delayed_replay.input_artifacts import InputArtifactStore
from delayed_replay.selected_trial.acquisition import Receipt
from delayed_replay.serialization import JsonObject, digest, parse_time, time_text
from research_tools.preflight import (
    PreflightCheckResult,
    ReadOnlyPreflightInspector,
    StageReadinessResult,
)
from research_tools.preflight_report import (
    PreflightReportWriter,
    main,
    result_exit_code,
)
from research_tools.reader import ObservationError
from tests.test_order_audit import fingerprint


def freeze_writes(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("preflight invoked a writer, trading logic or network")

    import delayed_replay.selected_trial.pipeline as pipeline
    import delayed_replay.selected_trial.service as selected
    from delayed_replay.signal_adapter import SignalAdapter

    monkeypatch.setattr(socket, "socket", forbidden)
    for cls, methods in (
        (Receipt, ("__init__", "append", "remaining", "freeze_selection")),
        (EventStore, ("create", "resume", "recover", "commit_event")),
        (InputArtifactStore, ("publish",)),
        (clear.JuneClearingService, ("create", "resume", "run", "advance")),
        (SignalAdapter, ("decide",)),
    ):
        for name in methods:
            monkeypatch.setattr(cls, name, forbidden)
    for name in (
        "prepare",
        "existing_receipt",
        "inspect",
        "build",
        "live_acquire",
        "acquire_inputs",
        "save",
        "write_once",
    ):
        monkeypatch.setattr(june, name, forbidden)
    monkeypatch.setattr(clear, "prepare_clearing", forbidden)
    monkeypatch.setattr(pipeline, "select", forbidden)
    monkeypatch.setattr(selected, "select", forbidden)


def inspector(now):
    return ReadOnlyPreflightInspector(clock=lambda: now, key_present=lambda: True)


def checks(bundle, stage="acquisition"):
    return {
        c["check_id"]: c
        for s in bundle.payload.to_dict()["stages"]
        if s["stage"] == stage
        for c in s["checks"]
    }


def approve(root, plan):
    value = dict(
        schema="june-acquisition-authorization-v1",
        plan_hash=plan.sha256,
        status="approved_for_acquisition",
        permission="acquire_only",
        execution_permission=False,
        approval_reference="ARTIFICIAL-ONLY",
    )
    (root / "owner_approved_acquisition.json").write_text(
        JsonObject.from_value(value).encoded
    )


@pytest.mark.parametrize("offset,expected", [(-1, "blocked"), (0, "pass"), (1, "pass")])
def test_date_boundary_is_not_official_verification(prepared, offset, expected):
    root, may, plan, *_ = prepared
    approve(root, plan)
    bundle = inspector(
        parse_time(june.NOT_BEFORE) + timedelta(microseconds=offset)
    ).inspect(root, may, stage="acquisition")
    c = checks(bundle)
    assert c["date_window"]["status"] == expected
    assert c["official_range"]["status"] == "unverified"
    assert c["authentication"]["status"] == "unverified"
    assert c["api_key_presence"]["status"] == "pass"
    assert "clearing_plan" not in c


def test_actual_jst_boundary_same_instant(prepared):
    root, may, *_ = prepared
    c = checks(
        inspector(datetime.fromisoformat("2026-09-24T18:00:00+09:00")).inspect(
            root, may
        )
    )
    assert c["date_window"]["status"] == "pass"


def test_no_writes_no_budget_start_repeated(prepared, monkeypatch, tmp_path):
    root, may, plan, *_ = prepared
    approve(root, plan)
    before = (fingerprint(root), fingerprint(may))
    # Compare with existing inspect while its known write-capable entry is allowed
    # only during artificial fixture setup; the inspector below must not call it.
    existing = june.inspect(root, may)
    freeze_writes(monkeypatch)
    now = datetime(2026, 9, 25, tzinfo=UTC)
    one = inspector(now).inspect(root, may)
    two = inspector(now).inspect(root, may)
    assert one.payload == two.payload
    c = checks(one)
    stats = c["budget_time"]["details"]
    assert stats["attempts"] == existing["communication"]["attempts"] == 0
    assert stats["started_at"] is stats["deadline"] is None
    assert stats["budget_state"] == "not_started"
    assert (
        one.payload.to_dict()["history_observations"]
        == existing["history_observations"]
    )
    PreflightReportWriter().write(one, tmp_path / "report")
    assert before == (fingerprint(root), fingerprint(may))
    assert not (root / "clearing_plan.json").exists()
    assert checks(one, "clearing")["input_manifest"]["status"] == "unverified"
    assert checks(one, "resume_account")["account_identity"]["status"] == "unverified"


def seed_attempts(root, plan, count, start):
    now = [start]
    receipt = Receipt(root / "acquisition.sqlite", plan, now=lambda: now[0])
    q = june.queries()[0]
    for i in range(count):
        now[0] = start + timedelta(seconds=13 * i)
        receipt.append(
            "attempt",
            dict(
                query=q,
                params=q["params"],
                request_id=digest(dict(path=q["path"], params=q["params"])),
            ),
        )
    receipt.close()


@pytest.mark.parametrize(
    "count,seconds,time_status,count_status",
    [
        (1, 13, "pass", "pass"),
        (1, 1200, "blocked", "pass"),
        (20, 300, "pass", "blocked"),
        (20, 1201, "blocked", "blocked"),
    ],
)
def test_budget_boundaries_no_reset(
    prepared, count, seconds, time_status, count_status
):
    root, may, plan, *_ = prepared
    start = datetime(2026, 9, 25, tzinfo=UTC)
    seed_attempts(root, plan, count, start)
    before = fingerprint(root)
    bundle = inspector(start + timedelta(seconds=seconds)).inspect(
        root, may, stage="acquisition"
    )
    c = checks(bundle)
    assert c["budget_time"]["status"] == time_status
    assert c["budget_attempts"]["status"] == count_status
    assert c["budget_time"]["details"]["started_at"] == time_text(start)
    assert c["budget_time"]["details"]["deadline"] == time_text(
        start + timedelta(seconds=1200)
    )
    assert c["budget_time"]["details"]["attempts"] == count
    assert fingerprint(root) == before


@pytest.mark.parametrize(
    "mutation,check,status",
    [
        ("missing_receipt", "receipt", "unverified"),
        ("corrupt_receipt", "receipt", "error"),
        ("unknown_schema", "plan", "error"),
        ("implementation", "implementation", "error"),
        ("history_missing", "history", "unverified"),
        ("bad_grant", "acquisition_permission", "error"),
    ],
)
def test_missing_and_corrupt_distinct(prepared, mutation, check, status):
    root, may, plan, *_ = prepared
    if mutation == "missing_receipt":
        (root / "acquisition.sqlite").unlink()
    elif mutation == "corrupt_receipt":
        with sqlite3.connect(root / "acquisition.sqlite") as db:
            db.execute("UPDATE events SET sha=? WHERE seq=0", ("0" * 64,))
    elif mutation == "history_missing":
        (
            may / "inputs" / (plan.payload.to_dict()["parent"]["packets"][0] + ".json")
        ).unlink()
    elif mutation == "bad_grant":
        approve(root, plan)
        p = json.loads((root / "owner_approved_acquisition.json").read_text())
        p["schema"] = "unknown"
        (root / "owner_approved_acquisition.json").write_text(json.dumps(p))
    else:
        p = plan.payload.to_dict()
        p["schema" if mutation == "unknown_schema" else "implementation_hash"] = (
            "unknown" if mutation == "unknown_schema" else "0" * 64
        )
        (root / "plan.json").write_text(JsonObject.from_value(p).encoded)
    bundle = inspector(datetime(2026, 9, 25, tzinfo=UTC)).inspect(
        root, may, stage="acquisition"
    )
    assert checks(bundle)[check]["status"] == status


def test_wal_budget_refused_before_sqlite_connect(prepared, monkeypatch):
    root, may, *_ = prepared
    db = sqlite3.connect(root / "acquisition.sqlite")
    db.execute("PRAGMA journal_mode=WAL")
    before = fingerprint(root)

    def forbidden(*a, **k):
        pytest.fail("WAL DB opened")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    try:
        c = checks(
            inspector(datetime(2026, 9, 25, tzinfo=UTC)).inspect(
                root, may, stage="acquisition"
            )
        )
        assert c["receipt"]["status"] == "blocked"
        assert c["receipt"]["reason"] == "stopped_consistent_non_wal_snapshot_required"
        assert fingerprint(root) == before
    finally:
        db.close()


def test_expired_budget_not_a_build_blocker_and_permissions_separate(
    prepared, monkeypatch
):
    root, may, plan, auth, clock, _ = built(prepared)
    (root / "owner_approved_clearing.json").write_text(auth.grant.encoded)
    acquisition = june.JunePlan(
        JsonObject.from_value(june.load_json(root / "plan.json"))
    )
    approve(root, acquisition)
    freeze_writes(monkeypatch)
    bundle = inspector(clock.now() + timedelta(days=1)).inspect(root, may)
    data = bundle.payload.to_dict()
    assert checks(bundle)["budget_time"]["status"] == "blocked"
    assert checks(bundle, "build_inputs")["indicators"]["status"] == "pass"
    assert next(s for s in data["stages"] if s["stage"] == "build_inputs")[
        "ready_for_build_inputs"
    ]
    assert next(s for s in data["stages"] if s["stage"] == "clearing")[
        "ready_for_clearing"
    ]
    other = inspector(clock.now()).inspect(
        root,
        may,
        stage="clearing",
        clearing_authorization="owner_approved_acquisition.json",
    )
    assert checks(other, "clearing")["clearing_permission"]["status"] == "blocked"


def test_read_only_resume_saved_account(prepared, monkeypatch):
    root, may, plan, auth, clock, _ = built(prepared)
    (root / "owner_approved_clearing.json").write_text(auth.grant.encoded)
    service = clear.JuneClearingService.create(
        root / "saved.sqlite", plan, auth, root, may, "split_resume"
    )
    service.run(clock.now())
    assert service.state["status"] == "waiting_for_input"
    service.store.close()
    before = fingerprint(root)
    freeze_writes(monkeypatch)
    bundle = inspector(clock.now()).inspect(
        root, may, stage="resume_account", account="saved.sqlite"
    )
    c = checks(bundle, "resume_account")
    assert c["account_identity"]["status"] == "pass", c["account_identity"]
    assert c["account_identity"]["details"]["cursor"]["status"] == "waiting_for_input"
    assert fingerprint(root) == before


def test_old_official_record_never_promoted(prepared):
    root, may, plan, *_ = prepared
    value = dict(
        schema="june-authorized-local-preflight-v1",
        plan_hash=plan.sha256,
        recorded_at=time_text(datetime(2026, 9, 14, tzinfo=UTC)),
        official_free_window="verified",
    )
    (root / "authorized_local_preflight.json").write_text(
        JsonObject.from_value(value).encoded
    )
    bundle = inspector(datetime(2026, 9, 25, tzinfo=UTC)).inspect(
        root, may, stage="acquisition"
    )
    assert checks(bundle)["official_range"]["status"] == "unverified"
    assert checks(bundle)["date_window"]["status"] == "pass"


def test_key_redacted_and_absent_distinct(prepared, monkeypatch, tmp_path):
    root, may, *_ = prepared
    monkeypatch.setenv("JQUANTS_API_KEY", "DO-NOT-OUTPUT-THIS-SECRET")
    obj = ReadOnlyPreflightInspector(clock=lambda: datetime(2026, 9, 25, tzinfo=UTC))
    b = obj.inspect(root, may, stage="acquisition")
    PreflightReportWriter().write(b, tmp_path / "report")
    assert checks(b)["api_key_presence"]["status"] == "pass"
    assert checks(b)["authentication"]["status"] == "unverified"
    assert all(
        "DO-NOT-OUTPUT" not in p.read_text() for p in (tmp_path / "report").iterdir()
    )
    monkeypatch.delenv("JQUANTS_API_KEY")
    assert (
        checks(obj.inspect(root, may, stage="acquisition"))["api_key_presence"][
            "status"
        ]
        == "blocked"
    )


def test_publication_changed_input_overwrite_and_may_output(prepared, tmp_path):
    root, may, *_ = prepared
    b = inspector(datetime(2026, 9, 25, tzinfo=UTC)).inspect(root, may)
    PreflightReportWriter().write(b, tmp_path / "report")
    with pytest.raises(ObservationError, match="output_exists"):
        PreflightReportWriter().write(b, tmp_path / "report")
    with pytest.raises(ObservationError, match="separate_output"):
        PreflightReportWriter().write(b, may / "bad")

    def fault(_):
        (root / "extra.json").write_text("{}")

    with pytest.raises(ObservationError, match="inventory_changed"):
        PreflightReportWriter().write(b, tmp_path / "changed", fault=fault)
    assert not (tmp_path / "changed").exists()


def test_required_checks_only_all_pass_and_exit_codes():
    def item(status, required=True):
        return PreflightCheckResult(
            "acquisition",
            str(required),
            status,
            "fixed_reason",
            time_text(datetime(2026, 9, 25, tzinfo=UTC)),
            required,
            (),
            "no_action",
            JsonObject.from_value({}),
        )

    for status in ("blocked", "unverified", "not_applicable", "error"):
        assert not StageReadinessResult("acquisition", (item(status),)).to_dict()[
            "ready_for_acquisition"
        ]
    assert StageReadinessResult(
        "acquisition", (item("pass"), item("unverified", False))
    ).to_dict()["ready_for_acquisition"]
    with pytest.raises(ValueError):
        StageReadinessResult("all", (item("pass"),))


def test_cli_report_success_separate_from_ready(prepared, tmp_path, capsys):
    root, may, *_ = prepared
    code = main(
        [
            "--trial-root",
            str(root),
            "--may-root",
            str(may),
            "--stage",
            "acquisition",
            "--output",
            str(tmp_path / "report"),
        ]
    )
    assert code == 2
    assert json.loads(capsys.readouterr().out)["report_written"]
    assert (
        main(
            [
                "--trial-root",
                str(root),
                "--may-root",
                str(may),
                "--output",
                str(tmp_path / "report"),
            ]
        )
        == 3
    )
    bundle = inspector(datetime(2026, 9, 25, tzinfo=UTC)).inspect(root, may)
    assert result_exit_code(bundle) == 2


def test_schema_clock_and_stage_rejected(prepared):
    root, may, *_ = prepared
    with pytest.raises(ValueError, match="aware"):
        inspector(datetime(2026, 9, 25)).inspect(root, may)
    with pytest.raises(ValueError, match="unknown"):
        inspector(datetime.now(UTC)).inspect(root, may, stage="execute")
