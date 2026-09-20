"""Artificial serialized bundles, not simulated strategy/clearing runs."""

import copy
import csv
import hashlib
import json
import sqlite3
from dataclasses import fields, replace

import pytest
from test_june_proxy_trial import prepared as prepared  # noqa: F401

from delayed_replay.audit_models import StreamIdentity
from delayed_replay.event_store import EventStore
from delayed_replay.input_artifacts import InputArtifactStore, InputPacket
from delayed_replay.market_view import MarketView
from delayed_replay.serialization import JsonObject, digest
from delayed_replay.snapshot import PriceSnapshot
from research_tools.compare_trials import TrialComparisonReportWriter, main
from research_tools.reader import EvidenceFiles, ObservationError, read_account
from research_tools.trial_comparison import (
    TrialComparabilityAssessment,
    TrialComparisonBuilder,
    TrialComparisonInput,
)
from tests.test_account_view import make_view_trial
from tests.test_order_audit import fingerprint, no_execution  # noqa: F401
from tests.test_preflight_report import freeze_writes


def fixture(
    root,
    *,
    kind="rejected",
    june=False,
    run_id="synthetic-may",
    style="continuous",
    plan_change=None,
    state_change=None,
):
    """Serialize independent artificial May/June ledgers, without a trading reducer."""
    make_view_trial(root, kind)
    with read_account(root / "viewer.sqlite", EvidenceFiles()) as saved:
        initial = saved.initial_state.to_dict()
        current = saved.current_state.to_dict()
        commands = [e.command for e in saved.events]
    plan = initial["identity"]["plan"]
    swaps = {}
    if june:
        sha = plan["packets"][0]
        old = InputArtifactStore(root / "inputs").load(sha).market.snapshots[0]
        raw = json.loads((root / (old.source_artifact_sha256 + ".json")).read_text())
        for row in raw["data"]:
            row["Date"] = row["Date"].replace("2026-05", "2026-06")
        obj = JsonObject.from_value(raw)
        (root / (obj.sha256 + ".json")).write_text(obj.encoded)
        values = {
            f.name: getattr(old, f.name)
            for f in fields(old)
            if f.name != "payload_sha256"
        }
        values.update(
            source_artifact_sha256=obj.sha256,
            observations=tuple(
                replace(b, session_date=b.session_date.replace(month=6))
                for b in old.observations
            ),
        )
        snapshot = PriceSnapshot.create(**values)
        new_sha = InputArtifactStore(root / "inputs").publish(
            InputPacket(MarketView((snapshot,), ()))
        )
        swaps = {sha: new_sha, old.payload_sha256: snapshot.payload_sha256}

    def transform(v):
        if isinstance(v, dict):
            return {transform(k): transform(x) for k, x in v.items()}
        if isinstance(v, list):
            return [transform(x) for x in v]
        if isinstance(v, str):
            return swaps.get(v, v).replace("2026-05", "2026-06") if june else v
        return v

    initial, current = transform(initial), transform(current)
    plan = initial["identity"]["plan"]
    if june:
        plan["scope"].update(start="2026-06-01", end="2026-07-01")
    if plan_change:
        plan_change(plan)
    obj = JsonObject.from_value(plan)
    (root / "trial_plan.json").write_text(obj.encoded)
    for state in (initial, current):
        state["identity"].update(
            plan=copy.deepcopy(plan), plan_hash=obj.sha256, style=style
        )
        state["episodes"] = {}
    if state_change:
        state_change(current)
    init = JsonObject.from_value(initial)
    identity = StreamIdentity.create(
        stream_id=run_id,
        purpose="synthetic_test",
        config_hash=digest(initial["identity"]),
        protocol_hash=obj.sha256,
        source_identity="artificial",
        reducer_identity="artificial-record-fixture-v1",
        initial_state=init,
    )
    db = root / "comparison"
    db.mkdir()
    store = EventStore.create(db / "continuous.sqlite", identity, init)
    for cmd in commands:
        cmd = replace(
            cmd,
            stream_id=run_id,
            stream_identity_hash=identity.genesis_hash,
            config_hash=identity.config_hash,
            payload=JsonObject.from_value(transform(cmd.payload.to_dict())),
            market_decision_at=cmd.market_decision_at.replace(month=6)
            if june
            else cmd.market_decision_at,
        )

        class SavedFixture:
            identity = "artificial-record-fixture-v1"

            def __call__(self, state, command):
                return copy.deepcopy(current)

        store.commit_event(cmd, store.read().head, SavedFixture())
    store.close()
    return root


def read(root):
    return TrialComparisonInput.read(root)


def data(root):
    return read(root).payload.to_dict()


@pytest.mark.parametrize(
    "kind", ["rejected", "empty", "filled", "holding", "waiting", "missing"]
)
def test_saved_states_no_execution(tmp_path, kind, request):
    root = fixture(tmp_path / kind, kind=kind)
    before = fingerprint(root)
    request.getfixturevalue("no_execution")
    row = data(root)
    m = row["metrics"]
    assert row["provenance"] == "artificial_fixture"
    assert m["entry_condition_count"] is None
    assert m["completed_trades_as_of"] == 0
    if kind == "rejected":
        assert m["terminal_order_count"] == m["rejected_count"] == 1
        assert m["fill_rate_percent"] == "0"
        assert m["charged_commission_as_of"] == "0"
        assert m["final_cash"] == m["final_equity"] == "200000"
        assert m["constraint_counts"] == {"reservation_only_exceeded": 1}
    elif kind == "filled":
        assert m["fill_rate_percent"] == "100"
        assert m["sell_count"] == 1
        assert m["charged_commission_as_of"] == "40.56"
    elif kind == "holding":
        assert m["end_holdings"][0]["shares"] == 100
        assert m["final_equity"] == "240600"
        assert m["fill_rate_percent"] is None
    else:
        assert m["terminal_order_count"] == 0
        assert m["fill_rate_percent"] is None
        if kind in ("waiting", "missing"):
            assert row["availability"] == "incomplete"
            assert m["final_cash"] is m["final_equity"] is m["end_holdings"] is None
        if kind == "missing":
            assert m["equity_as_of"] is None
        if kind == "waiting":
            assert m["waiting_count"] == 1
    assert fingerprint(root) == before


def test_artificial_two_month_bundles_stable_no_aggregation(tmp_path, monkeypatch):
    may = fixture(tmp_path / "may")
    june = fixture(
        tmp_path / "june", june=True, kind="holding", run_id="synthetic-june"
    )
    before = fingerprint(may), fingerprint(june)
    freeze_writes(monkeypatch)
    a, b = read(may), read(june)
    builder = TrialComparisonBuilder()
    one, two = builder.build([a, b]), builder.build([b, a])
    assert one.payload == two.payload
    p = one.payload.to_dict()
    assert p["aggregation"] == "none_replicas_not_independent_market_samples"
    assert [r["conditions"]["period"] for r in p["trials"]] == [
        ["2026-05-01", "2026-06-01"],
        ["2026-06-01", "2026-07-01"],
    ]
    assert all(r["provenance"] == "artificial_fixture" for r in p["trials"])
    assert p["trials"][1]["metrics"]["last_finalized_session"] == "2026-06-26"
    assert p["assessments"][0]["status"] == "conditions_mismatch"
    output = TrialComparisonReportWriter().write(one, tmp_path / "report")
    output2 = TrialComparisonReportWriter().write(two, tmp_path / "report2")
    for name in (
        "trial_comparison.json",
        "trial_comparison.csv",
        "trial_comparison.html",
    ):
        assert (output / name).read_bytes() == (output2 / name).read_bytes()
    assert (fingerprint(may), fingerprint(june)) == before


@pytest.mark.parametrize(
    "change,expected",
    [
        (None, "conditions_match"),
        ("commission", "conditions_mismatch"),
        ("implementation", "comparability_unverified"),
        ("basis", "comparability_unverified"),
    ],
)
def test_comparability_classes(tmp_path, change, expected):
    def alter(p):
        if change == "commission":
            p["terms"]["commission_rate"] = "0.002"
        elif change == "implementation":
            p["model_hash"] = "e" * 64
        elif change == "basis":
            del p["rules"]["provider_price_basis"]

    a = data(fixture(tmp_path / "a"))
    b = data(fixture(tmp_path / "b", run_id="other", plan_change=alter))
    result = TrialComparabilityAssessment().assess(a, b)
    assert result["status"] == expected
    if change == "implementation":
        assert a["conditions"]["settings_hash"] == b["conditions"]["settings_hash"]
        assert result["equivalence"] == "unverified"
        assert not any(f["status"] == "mismatch" for f in result["fields"])


def test_duplicate_and_replica_not_double_counted(tmp_path):
    a = read(fixture(tmp_path / "a"))
    b = read(fixture(tmp_path / "b", run_id="split", style="split_resume"))
    result = TrialComparisonBuilder().build([a, b, a]).payload.to_dict()
    assert len(result["trials"]) == 2
    assert len(result["replica_groups"]) == 1
    assert len(result["replica_groups"][0]["runs"]) == 2
    assert result["replica_groups"][0]["independent_sample_count"] is None


def test_conflicting_same_run_id_rejected(tmp_path):
    a = read(fixture(tmp_path / "a"))
    b = read(fixture(tmp_path / "b", kind="empty"))
    with pytest.raises(ObservationError, match="same_run_id_conflicting_content"):
        TrialComparisonBuilder().build([a, b])


def test_unacquired_is_not_zero_and_receipt_unchanged(prepared, monkeypatch, tmp_path):
    root, may, *_ = prepared
    before = fingerprint(root), fingerprint(may)
    freeze_writes(monkeypatch)
    item = read(root)
    p = item.payload.to_dict()
    assert p["availability"] == p["provenance"] == "unacquired"
    assert p["conditions"]["provenance"] == "unacquired"
    assert p["metrics"] is p["input_counts"] is p["metadata"] is None
    output = TrialComparisonReportWriter().write(
        TrialComparisonBuilder().build([item]), tmp_path / "unacquired-report"
    )
    with (output / "trial_comparison.csv").open() as f:
        row = next(csv.DictReader(f))
    assert row["final_cash"] == row["order_count"] == row["fill_rate_percent"] == ""
    assert (fingerprint(root), fingerprint(may)) == before


@pytest.mark.parametrize("status", ["cancelled", "canceled"])
def test_terminal_cancel_not_waiting(tmp_path, status):
    def mutate(s):
        order = next(iter(s["orders"].values()))
        order.update(status=status)
        order.pop("resolution")

    m = data(fixture(tmp_path / "input", state_change=mutate))["metrics"]
    assert m["cancelled_count"] == m["terminal_order_count"] == 1
    assert m["waiting_count"] == 0 and m["fill_rate_percent"] == "0"


def test_missing_external_proof_is_not_verified(tmp_path):
    root = fixture(tmp_path / "input")
    for p in (root / "inputs").glob("*.json"):
        p.unlink()
    row = data(root)
    assert row["input_validation"]["status"] == "insufficient_evidence"
    assert (
        TrialComparabilityAssessment().assess(row, row)["status"]
        == "comparability_unverified"
    )


def test_corrupt_or_missing_db_never_empty(tmp_path):
    root = fixture(tmp_path / "input")
    path = root / "comparison/continuous.sqlite"
    path.write_bytes(b"broken")
    with pytest.raises(ObservationError, match="invalid_database_header"):
        read(root)
    path.unlink()
    with pytest.raises(ObservationError, match="missing_input"):
        read(root)


def test_active_wal_refused(tmp_path):
    root = fixture(tmp_path / "input")
    connection = sqlite3.connect(root / "comparison/continuous.sqlite")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        with pytest.raises(ObservationError, match="non_wal_snapshot"):
            read(root)
    finally:
        connection.close()


def test_atomic_output_input_change_and_no_overwrite(tmp_path):
    root = fixture(tmp_path / "input")
    bundle = TrialComparisonBuilder().build([read(root)])
    output = tmp_path / "report"

    def fault(name):
        raise OSError("injected publication failure")

    with pytest.raises(OSError):
        TrialComparisonReportWriter().write(bundle, output, fault=fault)
    assert not output.exists()
    assert not list(tmp_path.glob(".order-audit-*"))
    TrialComparisonReportWriter().write(bundle, output)
    with pytest.raises(ObservationError, match="output_exists"):
        TrialComparisonReportWriter().write(bundle, output)

    def change(name):
        (root / "trial_plan.json").write_text("{}")

    with pytest.raises(ObservationError, match="input_changed"):
        TrialComparisonReportWriter().write(bundle, tmp_path / "failed", fault=change)
    assert not (tmp_path / "failed").exists()


def test_all_input_roots_protected(tmp_path):
    a = read(fixture(tmp_path / "a"))
    b = read(fixture(tmp_path / "b", run_id="second"))
    with pytest.raises(ObservationError, match="separate_output"):
        TrialComparisonReportWriter().write(
            TrialComparisonBuilder().build([a, b]), tmp_path / "b/report"
        )


def test_escaping_exact_json_manifest_no_paths(tmp_path):
    root = fixture(tmp_path / "input", run_id="=SUM(1,1)<script>&")
    item = read(root)
    output = TrialComparisonReportWriter().write(
        TrialComparisonBuilder().build([item]), tmp_path / "report"
    )
    page = (output / "trial_comparison.html").read_text()
    assert "<script>" not in page and "&lt;script&gt;" in page
    with (output / "trial_comparison.csv").open() as f:
        row = next(csv.DictReader(f))
    assert row["run_id"].startswith("'=SUM")
    assert row["entry_condition_count"] == ""
    exact = json.loads((output / "trial_comparison.json").read_text())
    assert exact["trials"][0]["run_id"].startswith("=SUM")
    manifest = json.loads((output / "comparison_manifest.json").read_text())
    assert (
        manifest["inputs"][0]["metadata"]["read_head"]
        == item.payload.to_dict()["metadata"]["read_head"]
    )
    for name, sha in manifest["artifacts"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == sha
    assert str(tmp_path) not in json.dumps(manifest)
    assert not manifest["budget_changed"] and not manifest["execution_invoked"]


def test_cli_explicit_inputs_no_actions(tmp_path, request, capsys):
    a = fixture(tmp_path / "a")
    b = fixture(tmp_path / "b", june=True, run_id="june")
    request.getfixturevalue("no_execution")
    assert (
        main(
            [
                "--trial-root",
                str(a),
                "--trial-root",
                str(b),
                "--output",
                str(tmp_path / "report"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["execution_invoked"] is False
    assert (
        main(
            [
                "--trial-root",
                str(tmp_path / "missing"),
                "--output",
                str(tmp_path / "bad"),
            ]
        )
        == 2
    )
    assert str(tmp_path) not in capsys.readouterr().out


def test_empty_explicit_set_rejected():
    with pytest.raises(ObservationError, match="explicit_inputs"):
        TrialComparisonBuilder().build([])


def test_missing_terminal_resolution_not_zero_fee(tmp_path):
    def change(s):
        next(iter(s["orders"].values())).pop("resolution")

    row = data(fixture(tmp_path / "input", state_change=change))
    assert row["metrics"]["charged_commission_as_of"] is None
    assert row["audit_validation"]["status"] == "attention_required"


def test_mixed_terminal_status_denominator(tmp_path):
    def change(s):
        original = next(iter(s["orders"].values()))
        for suffix, status in (
            (":cancelled", "cancelled"),
            (":rejected", "rejected"),
            (":waiting", "waiting"),
        ):
            order = copy.deepcopy(original)
            order["frozen"]["order_id"] += suffix
            order.update(status=status)
            order.pop("resolution")
            if status == "rejected":
                order["resolution"] = dict(
                    original["resolution"],
                    order_id=order["frozen"]["order_id"],
                    filled=False,
                    reason="synthetic_reject",
                )
            s["orders"][order["frozen"]["order_id"]] = order

    m = data(fixture(tmp_path / "input", kind="filled", state_change=change))["metrics"]
    assert m["order_count"] == 4
    assert m["terminal_order_count"] == 3
    assert m["filled_count"] == m["waiting_count"] == 1
    assert m["fill_rate_percent"].startswith("33.33333")


@pytest.mark.parametrize("change", ["unknown", "wrong_plan", "invalid_boolean"])
def test_saved_comparison_invalid_refused(tmp_path, change):
    root = fixture(tmp_path / "input")
    plan = json.loads((root / "trial_plan.json").read_text())
    value = dict(
        schema="limited-comparison-v1", plan_hash=digest(plan), logical_state_equal=True
    )
    if change == "unknown":
        value["schema"] = "unknown"
    elif change == "wrong_plan":
        value["plan_hash"] = "a" * 64
    else:
        value["logical_state_equal"] = 1
    (root / "comparison/comparison.json").write_text(
        JsonObject.from_value(value).encoded
    )
    with pytest.raises(ObservationError):
        read(root)


def test_saved_comparison_is_assertion_not_pair_proof(tmp_path):
    root = fixture(tmp_path / "input")
    plan = json.loads((root / "trial_plan.json").read_text())
    value = dict(
        schema="limited-comparison-v1", plan_hash=digest(plan), logical_state_equal=True
    )
    (root / "comparison/comparison.json").write_text(
        JsonObject.from_value(value).encoded
    )
    saved = data(root)["saved_comparison"]
    assert saved["recorded_equal"] is True
    assert saved["current_pair_equivalence"] == "unverified"


def test_new_missing_evidence_file_aborts_publication(tmp_path):
    root = fixture(tmp_path / "input")
    bundle = TrialComparisonBuilder().build([read(root)])
    (root / "comparison/comparison.json").write_text("{}")
    with pytest.raises(ObservationError, match="appeared"):
        TrialComparisonReportWriter().write(bundle, tmp_path / "report")
    assert not (tmp_path / "report").exists()


def test_provenance_difference_never_two_real_months(tmp_path):
    # Both artifacts are synthetic; saved_jquants is a deliberately injected label
    # to verify separation, not a claim that this fixture is real market evidence.
    a = read(
        fixture(
            tmp_path / "a", plan_change=lambda p: p.update(provenance="saved_jquants")
        )
    )
    b = read(fixture(tmp_path / "b", june=True, run_id="artificial-june"))
    result = TrialComparisonBuilder().build([a, b])
    p = result.payload.to_dict()
    assert [r["provenance"] for r in p["trials"]] == [
        "saved_jquants",
        "artificial_fixture",
    ]
    fields = {f["condition"]: f for f in p["assessments"][0]["fields"]}
    assert fields["provenance"]["status"] == "mismatch"
    output = TrialComparisonReportWriter().write(result, tmp_path / "report")
    page = (output / "trial_comparison.html").read_text()
    assert "人工・未取得期間は実市場結果ではありません" in page
    assert "2か月の実市場" not in page


def test_missing_episode_field_is_na(tmp_path):
    row = data(fixture(tmp_path / "input", state_change=lambda s: s.pop("episodes")))
    assert row["metrics"]["completed_trades_as_of"] is None


def test_unknown_saved_plan_schema_rejected(tmp_path):
    root = fixture(tmp_path / "input", plan_change=lambda p: p.update(schema="unknown"))
    with pytest.raises(ObservationError, match="schema"):
        read(root)
