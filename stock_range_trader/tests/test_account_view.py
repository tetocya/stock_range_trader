"""Artificial saved ledgers only; no trading reducer or market API execution."""

import copy
import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from delayed_replay.audit_models import StreamIdentity
from delayed_replay.event_store import EventStore
from delayed_replay.serialization import JsonObject, digest
from research_tools.account_html import (
    AccountHtmlWriter,
    _json_script,
    chart_segments,
)
from research_tools.account_view import AccountReadModel, AccountViewBuilder, main
from research_tools.reader import EvidenceFiles, ObservationError, read_account
from tests.browser_session import browser_session
from tests.test_order_audit import fingerprint, make_trial, no_execution  # noqa: F401


def make_view_trial(root, kind="rejected", mutate=None):
    source = make_trial(
        root,
        side="SELL" if kind == "filled" else "BUY",
        status="filled"
        if kind == "filled"
        else "pending"
        if kind == "waiting"
        else "rejected",
    )
    with read_account(source.db, EvidenceFiles()) as records:
        initial, current = (
            records.initial_state.to_dict(),
            records.current_state.to_dict(),
        )
        commands = [e.command for e in records.events]
    identity = copy.deepcopy(initial["identity"])
    identity["run_sessions"] = ["2026-05-25", "2026-05-26"]
    for s in (initial, current):
        s.update(
            identity=identity,
            equity=s["cash"],
            proceeds_hold="0",
            realized_profit="0",
            positions={},
            marks={},
            valuation_complete=True,
            index=0,
            phase="select",
            phase_observations={},
        )
    current.update(index=2, phase="select")
    current["marks"] = {
        day: dict(cash=current["cash"], equity=current["equity"], reserved_cash="0")
        for day in identity["run_sessions"]
    }
    current["phase_observations"] = {"mark": dict(session_date="2026-05-26")}
    if kind in ("empty", "holding", "missing"):
        current["orders"] = {}
    if kind in ("holding", "missing"):
        current["positions"] = {
            "46890": dict(shares=100, cost_basis="39800", entry_session="2026-05-25")
        }
        current["equity"] = "240600"
        current["marks"]["2026-05-26"]["equity"] = "240600"
        current["marks"]["2026-05-25"]["equity"] = "239800"
    if kind == "missing":
        current.update(
            index=1,
            phase="mark",
            status="waiting_for_input",
            reason="missing_close",
            valuation_complete=False,
            equity="239800",
        )
        del current["marks"]["2026-05-26"]
    if kind == "waiting":
        current.update(
            index=1,
            phase="resolve",
            status="waiting_for_input",
            reason="missing_open",
            proceeds_hold="1000",
        )
    if kind == "filled":
        current["proceeds_hold"] = "40518.44"
    if mutate:
        mutate(current)
    initial_obj = JsonObject.from_value(initial)
    stream = StreamIdentity.create(
        stream_id="artificial-view",
        purpose="synthetic_test",
        config_hash=digest(identity),
        protocol_hash=digest(identity["plan"]),
        source_identity="artificial",
        reducer_identity="artificial-record-fixture-v1",
        initial_state=initial_obj,
    )
    db = root / "viewer.sqlite"
    store = EventStore.create(db, stream, initial_obj)
    for command in commands:
        command = replace(
            command,
            stream_id=stream.stream_id,
            stream_identity_hash=stream.genesis_hash,
            config_hash=stream.config_hash,
        )

        class FixtureReducer:
            identity = stream.reducer_identity

            def __call__(self, state, command):
                return copy.deepcopy(current)

        store.commit_event(command, store.read().head, FixtureReducer())
    store.close()
    return source


@pytest.fixture
def saved(tmp_path):
    return make_view_trial(tmp_path / "input")


def read(source):
    return AccountReadModel.read(source.root, "viewer.sqlite")


def test_rejected_account_and_immutable_read(saved, tmp_path, request):
    request.getfixturevalue("no_execution")
    before = fingerprint(saved.root)
    model = read(saved)
    data = AccountViewBuilder().build(model).to_dict()
    assert data["account"]["cash"] == data["account"]["equity"] == "200000"
    assert data["summary"] == dict(
        position_count=0,
        order_count=1,
        rejected_count=1,
        filled_count=0,
        status_counts={"rejected": 1},
        charged_commission="0",
    )
    data["account"]["cash"] = "0"
    assert AccountViewBuilder().build(model).to_dict()["account"]["cash"] == "200000"
    output = AccountHtmlWriter().write(model, tmp_path / "report")
    manifest = json.loads((output / "report_manifest.json").read_text())
    assert manifest["input"]["read_head"] == model.audit.metadata.to_dict()["read_head"]
    assert manifest["input"]["provenance"] == "artificial_fixture"
    for name, sha in manifest["artifacts"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == sha
    assert fingerprint(saved.root) == before
    assert read(saved).payload == model.payload


@pytest.mark.parametrize("kind", ["empty", "holding", "missing", "filled", "waiting"])
def test_saved_states(tmp_path, kind):
    model = read(make_view_trial(tmp_path / "input", kind))
    data = AccountViewBuilder().build(model).to_dict()
    a = data["account"]
    if kind == "empty":
        assert data["summary"]["order_count"] == data["summary"]["position_count"] == 0
        assert a["cash"] == a["equity"] == "200000"
    elif kind == "holding":
        assert a["equity"] == "240600" and a["position_value"] == "40600"
        assert data["positions"][0]["valuation_price"] == "406"
        assert data["positions"][0]["cost_basis"] == "39800"
        assert data["replay"]["last_finalized_session"] == "2026-05-26"
    elif kind == "missing":
        assert a["equity"] is a["position_value"] is None
        assert a["saved_equity_last_recorded"] == "239800"
        assert a["last_valuation_session"] == "2026-05-25"
        assert data["positions"][0]["valuation_price"] is None
        assert data["history"][-1]["equity"] is None
        assert data["replay"]["reason"] == "missing_close"
    elif kind == "filled":
        assert data["summary"]["filled_count"] == 1
        assert data["summary"]["charged_commission"] == "40.56"
        assert a["proceeds_hold"] == a["reserved_total"] == "40518.44"
        assert a["available_cash"] == "200000"
        assert a["equity"] == "240518.44"  # No double subtraction.
    else:
        assert a["reserved_total"] == "41279.24"
        assert a["available_cash"] == "158720.76"
        assert a["equity"] == "200000"
        assert data["orders"][0]["saved_status"] == "pending"
        assert data["replay"]["status"] == "waiting_for_input"


def test_missing_external_valuation_is_not_normal(tmp_path):
    source = make_view_trial(tmp_path / "input", "holding")
    next(p for p in source.root.glob("*.json") if p.name != "trial_plan.json").unlink()
    data = AccountViewBuilder().build(read(source)).to_dict()
    assert data["account"]["cash"] == "200000"  # Saved, not reconstructed.
    assert data["account"]["equity"] is None
    assert data["validation"]["status"] == "attention_required"
    assert data["missing_evidence"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(index=True),
        lambda s: s.update(valuation_complete="yes"),
        lambda s: s.update(cash="NaN"),
        lambda s: s.update(proceeds_hold="200001"),
        lambda s: s.update(equity="199999"),
        lambda s: s.update(phase="unknown"),
        lambda s: s["marks"]["2026-05-26"].update(equity="1"),
        lambda s: s["positions"].update(
            {"46890": dict(shares=True, cost_basis="1", entry_session="2026-05-25")}
        ),
    ],
)
def test_invalid_saved_structures_fail_closed(tmp_path, mutation):
    source = make_view_trial(tmp_path / "input", mutate=mutation)
    with pytest.raises((ObservationError, ValueError)):
        read(source)


def test_wal_rejected_without_changes(saved):
    db = saved.root / "viewer.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA journal_mode=WAL")
    before = fingerprint(saved.root)
    try:
        with pytest.raises(ObservationError, match="non_wal"):
            read(saved)
        assert before == fingerprint(saved.root)
    finally:
        connection.close()


def test_corrupt_state_not_zero_account(saved):
    db = saved.root / "viewer.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA user_version=999")
    connection.close()
    with pytest.raises(ValueError):
        read(saved)


def test_output_contracts_and_input_change(saved, tmp_path):
    model = read(saved)
    out = tmp_path / "output"
    AccountHtmlWriter().write(model, out)
    old = fingerprint(out)
    with pytest.raises(ObservationError, match="output_exists"):
        AccountHtmlWriter().write(model, out)
    assert fingerprint(out) == old
    with pytest.raises(ObservationError, match="separate_output"):
        AccountHtmlWriter().write(model, saved.root / "output")

    def change(_):
        (saved.root / "trial_plan.json").write_text("{}")

    with pytest.raises(ObservationError, match="input_changed"):
        AccountHtmlWriter().write(model, tmp_path / "changed", fault=change)
    assert not (tmp_path / "changed").exists()
    assert not list(tmp_path.glob(".order-audit-*"))


def test_chart_segments_do_not_bridge_missing():
    rows = [dict(equity=v) for v in ("200000", None, "199999", "200001", None)]
    assert chart_segments(rows, "equity") == [
        [(0, "200000")],
        [(2, "199999"), (3, "200001")],
    ]


def test_script_end_and_long_reason_safe(tmp_path):
    attack = '</script><script>alert("x")</script>&\u2028' + "理由" * 2000
    source = make_view_trial(
        tmp_path / "input", mutate=lambda s: s.update(reason=attack)
    )
    out = AccountHtmlWriter().write(read(source), tmp_path / "report")
    html = (out / "account_view.html").read_text()
    assert attack not in html
    assert json.loads(_json_script(dict(value=attack)))["value"] == attack
    assert (
        json.loads((out / "account_view_data.json").read_text())["replay"]["reason"]
        == attack
    )
    assert "https://" not in html and "fetch(" not in html and "innerHTML" not in html
    assert "default-src 'none'" in html


@pytest.mark.parametrize(
    "kind", ["rejected", "empty", "filled", "holding", "waiting", "missing"]
)
def test_local_browser_interactions(tmp_path, request, kind):
    browser = os.environ.get("ACCOUNT_VIEW_TEST_BROWSER")
    if not browser:
        pytest.skip("Explicit local headless browser not configured")
    assert Path(browser).is_file(), "Configured browser must exist; not a skip"
    assert os.name == "posix", "Browser pipe harness requires POSIX"
    source = make_view_trial(tmp_path / "input", kind)
    request.getfixturevalue("no_execution")
    before = fingerprint(source.root)
    model = read(source)
    head = model.audit.metadata.to_dict()["read_head"]
    expected = AccountViewBuilder().build(model).to_dict()
    report = AccountHtmlWriter().write(model, tmp_path / "report")
    with browser_session(browser, tmp_path / "browser") as ui:
        # Separate blank-page loading from the account's unmodified production JS.
        ui.load("about:blank", "blank_load")
        assert ui.evaluate("document.body.textContent") == ""
        ui.load((report / "account_view.html").as_uri(), "account_load")
        original = ui.evaluate("JSON.stringify(data)")
        ui.stage("interaction")
        for table in ("orders", "positions", "history"):
            assert ui.evaluate(
                f"document.querySelectorAll('#{table} tbody tr').length"
            ) == len(expected[table])
        assert ui.evaluate(
            "['10','2','1','9007199254740993.02','9007199254740993.01','-2','0'].sort(compareDecimal)"
        ) == ["-2", "0", "1", "2", "10", "9007199254740993.01", "9007199254740993.02"]
        # Native mouse/key events exercise handlers; no injected success marker.
        ui.evaluate(
            "window.testInputs=[]; ['input','change','click'].forEach(t=>document.addEventListener(t,e=>window.testInputs.push({type:t,trusted:e.isTrusted})))"
        )
        ui.click("#date-filter")
        for _ in range(4):
            ui.key("ArrowLeft", "ArrowLeft", 37)
        for _ in range(3):
            ui.key("ArrowUp", "ArrowUp", 38)
            ui.key("ArrowRight", "ArrowRight", 39)
        # Empty native date segments become January 1, outside every May fixture,
        # regardless of the host year or year/month/day field display order.
        assert ui.evaluate("document.getElementById('date-filter').value").endswith(
            "-01-01"
        )
        assert ui.evaluate("document.querySelectorAll('tbody tr').length") == 0
        # Clear all native date segments, not the underlying saved data.
        for _ in range(3):
            ui.key("Backspace", "Backspace", 8)
            ui.key("ArrowLeft", "ArrowLeft", 37)
        ui.key("Tab", "Tab", 9)
        assert ui.evaluate("document.getElementById('date-filter').value") == ""
        if expected["orders"]:
            for control in ("status", "symbol"):
                selected = expected["orders"][0][
                    "saved_status" if control == "status" else "symbol"
                ]
                ui.click(f"#{control}-filter")
                # Native type-ahead works without relying on an OS popup menu.
                ui.character(selected[0])
                ui.key("Escape", "Escape", 27)
                ui.key("Tab", "Tab", 9)
                assert (
                    ui.evaluate(f"document.getElementById('{control}-filter').value")
                    == selected
                )
                assert (
                    ui.evaluate("document.querySelectorAll('#orders tbody tr').length")
                    == 1
                )
            ui.click("#orders summary")
            assert ui.evaluate("document.querySelector('#orders details').open") is True
            detail = ui.evaluate(
                "document.querySelector('#orders details pre').textContent"
            )
            oid = expected["orders"][0]["order_id"]
            assert json.loads(detail) == ui.evaluate(
                "data.order_audit[" + json.dumps(oid) + "]"
            )
            ui.click("#orders summary")
            assert (
                ui.evaluate("document.querySelector('#orders details').open") is False
            )
        values = [row["equity"] for row in expected["history"]]
        finite = sorted((value for value in values if value is not None), key=Decimal)
        for descending in (False, True):
            ui.click('#history button[data-sort="equity"]')
            amounts = ui.evaluate(
                "[...document.querySelectorAll('#history td[data-field=equity]')].map(e=>e.textContent)"
            )
            assert amounts == (list(reversed(finite)) if descending else finite) + [
                "未評価／不明"
            ] * values.count(None)
        fields = ui.evaluate(
            "Object.fromEntries([...document.querySelectorAll('dt')].map(e=>[e.textContent,e.nextElementSibling.textContent]))"
        )
        assert fields["Cash"] == expected["account"]["cash"]
        assert fields["Equity（現在評価）"] == (
            expected["account"]["equity"] or "未評価／不明"
        )
        assert fields["simulated_fill数"] == str(expected["summary"]["filled_count"])
        assert fields["徴収手数料"] == expected["summary"]["charged_commission"]
        if kind == "missing":
            assert fields["保有時価"] == "未評価／不明"
            assert fields["徴収手数料"] == "0"
        elif kind in ("rejected", "empty"):
            assert fields["保有時価"] == "0"
        if kind in ("missing", "waiting"):
            assert fields["status"] == "waiting_for_input"
        assert ui.evaluate("JSON.stringify(data)") == original
        assert ui.evaluate("Object.isFrozen(data.orders)") is True
        events = ui.evaluate("window.testInputs")
        assert events and all(event["trusted"] for event in events)
        assert {event["type"] for event in events} >= {"click", "input", "change"}
        assert ui.evaluate("performance.getEntriesByType('resource').length") == 0
    assert ui.report["passed"] and ui.report["returncode"] == 0
    assert not ui.report["forced_cleanup"] and not ui.report["remaining_process_group"]
    assert fingerprint(source.root) == before
    assert read(source).audit.metadata.to_dict()["read_head"] == head
    print(json.dumps(dict(case=kind, **ui.report)))


def test_cli(saved, tmp_path, capsys):
    assert (
        main(
            [
                "--trial-root",
                str(saved.root),
                "--account",
                "viewer.sqlite",
                "--output",
                str(tmp_path / "report"),
            ]
        )
        == 0
    )
    assert "view_written_not_execution" in capsys.readouterr().out
    assert (
        main(
            [
                "--trial-root",
                str(saved.root),
                "--account",
                "absent.sqlite",
                "--output",
                str(tmp_path / "bad"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "bad").exists()
