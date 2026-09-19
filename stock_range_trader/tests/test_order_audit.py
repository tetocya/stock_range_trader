"""Artificial serialized ledgers; observers never run trading logic."""

import copy
import csv
import hashlib
import json
import socket
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from data.price_policy import provider_price_basis
from delayed_replay.audit_models import EventCommand, StreamIdentity
from delayed_replay.event_store import EventStore
from delayed_replay.input_artifacts import InputArtifactStore, InputPacket
from delayed_replay.market_view import MarketView
from delayed_replay.serialization import JsonObject, digest, time_text
from delayed_replay.snapshot import PriceObservation, PriceSnapshot
from research_tools.arithmetic import OrderArithmeticVerifier
from research_tools.order_audit import OrderAuditReader, OrderAuditRecord, main
from research_tools.order_report import OrderAuditReportWriter
from research_tools.reader import EvidenceFiles, ObservationError, read_account


def fingerprint(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def make_trial(root, *, side="BUY", status="rejected", include_resolution=True):
    root.mkdir()
    p = json.loads(
        (Path(__file__).parents[1] / "config/limited_proxy_trial_plan.json").read_text()
    )
    p["schema"] = "selected-trial-plan-v1"
    p["scope"].update(
        symbol="46890", acquisition_hash="a" * 64, selection_hash="b" * 64
    )
    p["terms"].update(
        max_position_pct="0.25",
        commission_rate="0.001",
        slippage_pct="0.001",
        reservation_buffer_pct="0.01",
    )
    p.update(
        provenance="artificial_fixture",
        model_hash="d" * 64,
        source_identity="artificial-order-audit-records",
    )
    wall = datetime(2026, 9, 1, tzinfo=UTC)
    source = dict(
        data=[
            dict(
                Date=day,
                Code="46890",
                O=price,
                H="420",
                L="380",
                C=price,
                Vo="1000000",
                AdjO=price,
                AdjH="420",
                AdjL="380",
                AdjC=price,
                AdjVo="1000000",
                AdjFactor="1",
                ExRT=None,
            )
            for day, price in (("2026-05-25", "398"), ("2026-05-26", "406"))
        ]
    )
    raw = JsonObject.from_value(source)
    (root / (raw.sha256 + ".json")).write_text(raw.encoded)
    snapshot = PriceSnapshot.create(
        provider="jquants",
        provider_price_basis=provider_price_basis("jquants"),
        source_artifact_sha256=raw.sha256,
        data_version="artificial-audit-v1",
        first_observed_at=wall,
        fetched_at=wall,
        provider_published_at=None,
        publication_time_unknown_reason="artificial",
        price_basis_evidence_id="artificial",
        observations=tuple(
            PriceObservation(
                "46890",
                date.fromisoformat(r["Date"]),
                wall,
                tuple(float(r[k]) for k in ("O", "H", "L", "C", "Vo")),
                tuple(float(r[k]) for k in ("AdjO", "AdjH", "AdjL", "AdjC", "AdjVo")),
                1.0,
                0.0,
                0.0,
            )
            for r in source["data"]
        ),
    )
    sha = InputArtifactStore(root / "inputs").publish(
        InputPacket(MarketView((snapshot,), ()))
    )
    p["packets"], p["history_packets"] = [sha], []
    plan = JsonObject.from_value(p)
    (root / "trial_plan.json").write_text(plan.encoded)
    inputs = {
        "46890|" + r["Date"]: dict(
            row=dict(
                symbol="46890",
                session=r["Date"],
                **dict(
                    zip(
                        ("open", "high", "low", "close", "volume"),
                        [r[k] for k in ("O", "H", "L", "C", "Vo")],
                        strict=True,
                    )
                ),
                adjustment_factor="1",
                halt="unknown",
                volume_unit="execution_shares",
                actual_trade_at=None,
            ),
            adjusted=[r[k] for k in ("AdjO", "AdjH", "AdjL", "AdjC", "AdjVo")],
            packet=sha,
            snapshot_hash=snapshot.payload_sha256,
            first_observed_at=time_text(wall),
            fetched_at=time_text(wall),
        )
        for r in source["data"]
    }
    identity = dict(plan=p, plan_hash=plan.sha256)
    initial = dict(
        schema="limited-proxy-state-v1",
        identity=identity,
        cash="200000",
        orders={},
        inputs=inputs,
        input_head="e" * 64,
        status="running",
        reason=None,
    )
    oid = "2026-05-25:46890:" + side
    frozen = dict(
        order_id=oid,
        episode_id=oid,
        symbol="46890",
        side=side,
        target="2026-05-26",
        decision_session="2026-05-25",
        decision_phase="after_close",
        candidate_id="baseline",
        config_hash="c" * 64,
        shares=100,
        budget="50000" if side == "BUY" else "0",
        reserved="40279.24" if side == "BUY" else "0",
        reference_price="398",
        decision_equity="200000",
        equity_at="2026-05-25:after_close",
        policy_hash="f" * 64,
        rank=0,
        score="90",
    )
    order = dict(
        frozen=frozen,
        status=status,
        reserved_cash=frozen["reserved"] if status in ("pending", "waiting") else "0",
        reserved_shares=100
        if side == "SELL" and status in ("pending", "waiting")
        else 0,
    )
    if status in ("rejected", "filled") and include_resolution:
        order["resolution"] = dict(
            order_id=oid,
            filled=status == "filled",
            reason="frozen_reservation_or_budget_exceeded"
            if status == "rejected"
            else None,
            price="406.41" if side == "BUY" else "405.59",
            gross="40641" if side == "BUY" else "40559",
            commission="40.65" if side == "BUY" else "40.56",
            net="40681.65" if side == "BUY" else "40518.44",
            shares=100,
            actual_trade_at=None,
        )
    initial_obj = JsonObject.from_value(initial)
    stream = StreamIdentity.create(
        stream_id="artificial-order-audit",
        purpose="synthetic_test",
        config_hash=digest(identity),
        protocol_hash=plan.sha256,
        source_identity="artificial",
        reducer_identity="artificial-record-fixture-v1",
        initial_state=initial_obj,
    )
    path = root / "account.sqlite"
    store = EventStore.create(path, stream, initial_obj)
    current = copy.deepcopy(initial)
    for phase, day in (("decide", "2026-05-25"), ("resolve", "2026-05-26")):
        current["orders"][oid] = copy.deepcopy(order)
        if phase == "resolve":
            current.update(status="completed", reason="no_next_bar_no_forced_exit")
            if status == "filled":
                current["cash"] = "159318.35" if side == "BUY" else "240518.44"
        event = EventCommand(
            stream.stream_id,
            stream.genesis_hash,
            stream.config_hash,
            stream.reducer_identity,
            "fixture-" + phase,
            "limited.phase",
            datetime.fromisoformat(day).replace(tzinfo=UTC),
            wall,
            JsonObject.from_value(
                dict(
                    schema="limited-proxy-event-v1",
                    action="phase",
                    business_id="fixture-" + phase,
                    data=dict(phase=phase, index=0, input_head="e" * 64, result={}),
                )
            ),
            (sha,),
        )

        class FixtureReducer:
            identity = stream.reducer_identity

            def __call__(self, state, command):
                return copy.deepcopy(current)

        store.commit_event(event, store.read().head, FixtureReducer())
    store.close()
    return SimpleNamespace(
        root=root, db=path, plan=p, frozen=frozen, order=order, packet=sha
    )


@pytest.fixture
def trial(tmp_path):
    return make_trial(tmp_path / "input")


@pytest.fixture
def no_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("observer attempted execution, writing or networking")

    import delayed_replay.proxy.resolution as resolution
    import delayed_replay.recovery as recovery
    from delayed_replay.limited_trial.service import LimitedTrialService
    from delayed_replay.proxy.reducer import ProxyReducer
    from delayed_replay.signal_adapter import SignalAdapter

    monkeypatch.setattr(socket, "socket", forbidden)
    for cls, methods in (
        (LimitedTrialService, ("create", "resume", "advance", "run")),
        (EventStore, ("create", "resume", "commit_event", "recover")),
        (ProxyReducer, ("__call__", "phase")),
        (SignalAdapter, ("decide",)),
    ):
        for name in methods:
            monkeypatch.setattr(cls, name, forbidden)
    monkeypatch.setattr(resolution, "_resolve_batch", forbidden)
    monkeypatch.setattr(resolution, "resolve_batch", forbidden)
    monkeypatch.setattr(recovery, "recover_records", forbidden)


def test_may_arithmetic_read_only_report(trial, no_execution, tmp_path):
    before = fingerprint(trial.root)
    bundle = OrderAuditReader().read(trial.root, "account.sqlite")
    row = bundle.records[0].to_dict()
    calculated = row["verification"]["calculated"]
    for name, expected in dict(
        reservation_unit="402.39",
        reservation_gross="40239",
        reservation_commission="40.24",
        reservation_total="40279.24",
        execution_unit="406.41",
        hypothetical_commission="40.65",
        required_amount="40681.65",
        required_minus_reserved="402.41",
        budget_minus_required="9318.35",
    ).items():
        assert calculated[name] == expected
    assert row["verification"]["status"] == "verified"
    assert row["verification"]["constraint_diagnosis"] == "reservation_only_exceeded"
    assert row["actual_cash_change"] == row["charged_commission"] == "0"
    assert row["saved_status"] == "rejected"
    assert row["decision_event_ids"] == ["fixture-decide"]
    assert row["resolution_event_ids"] == ["fixture-resolve"]
    assert bundle.metadata.to_dict()["account_cash_reconciliation"] is True
    output = tmp_path / "report"
    OrderAuditReportWriter().write(bundle, output)
    assert set(p.name for p in output.iterdir()) == {
        "order_audit.json",
        "order_audit.csv",
        "order_audit.html",
        "report_manifest.json",
    }
    manifest = json.loads((output / "report_manifest.json").read_text())
    assert "report_manifest.json" not in manifest["artifacts"]
    assert all(
        hashlib.sha256((output / k).read_bytes()).hexdigest() == v
        for k, v in manifest["artifacts"].items()
    )
    assert str(trial.root) not in (output / "report_manifest.json").read_text()
    assert fingerprint(trial.root) == before


@pytest.mark.parametrize(
    "reserved,budget,diagnosis",
    [
        ("40681.64", "50000", "reservation_only_exceeded"),
        ("50000", "40681.64", "budget_only_exceeded"),
        ("40681.64", "40681.64", "both_exceeded"),
        ("40681.65", "40681.65", "within_both_limits"),
        ("40681.66", "40681.66", "within_both_limits"),
    ],
)
def test_four_constraints_and_one_cent_boundary(trial, reserved, budget, diagnosis):
    order = dict(trial.frozen, reserved=reserved, budget=budget)
    result = OrderArithmeticVerifier().verify(
        order, trial.plan["terms"], "daily_open_proxy_v1", "406", None, "pending"
    )
    assert result["constraint_diagnosis"] == diagnosis


@pytest.mark.parametrize("status", ["pending", "waiting", "cancelled", "rejected"])
def test_nonfills_never_become_fills(tmp_path, status):
    trial = make_trial(tmp_path / "trial", status=status)
    row = OrderAuditReader().read(trial.root, "account.sqlite").records[0].to_dict()
    assert row["saved_status"] == status
    assert row["charged_commission"] == row["actual_cash_change"] == "0"
    assert row["verification"]["calculated"]["hypothetical_commission"] == "40.65"


def test_sell_does_not_use_buy_reservation_formula(tmp_path):
    trial = make_trial(tmp_path / "sell", side="SELL", status="filled")
    row = OrderAuditReader().read(trial.root, "account.sqlite").records[0].to_dict()
    assert row["verification"]["status"] == "verified"
    assert row["verification"]["calculated"]["reservation_total"] is None
    assert row["sell_fixed_share_reservation"] == 100
    assert row["charged_commission"] == "40.56"
    assert row["actual_cash_change"] == row["sell_proceeds_hold_on_fill"] == "40518.44"


def test_unknown_model_and_missing_evidence(trial):
    verifier = OrderArithmeticVerifier()
    assert (
        verifier.verify(
            trial.frozen, trial.plan["terms"], "future-model", "406", None, "pending"
        )["status"]
        == "unsupported_verification_model"
    )
    assert (
        verifier.verify(
            trial.frozen, {}, "daily_open_proxy_v1", "406", None, "pending"
        )["status"]
        == "insufficient_evidence"
    )
    (trial.root / "inputs" / (trial.packet + ".json")).unlink()
    row = OrderAuditReader().read(trial.root, "account.sqlite").records[0].to_dict()
    assert row["verification"]["status"] == "insufficient_evidence"


def test_resolution_discrepancy_is_visible(trial):
    wrong = dict(trial.order["resolution"], commission="40.64")
    result = OrderArithmeticVerifier().verify(
        trial.frozen,
        trial.plan["terms"],
        "daily_open_proxy_v1",
        "406",
        wrong,
        "rejected",
    )
    assert result["status"] == "mismatch"
    assert "resolution_commission" in result["mismatches"]


@pytest.mark.parametrize("change", ["hash", "schema", "head", "event"])
def test_corrupt_database_never_becomes_empty_account(trial, change):
    connection = sqlite3.connect(trial.db)
    if change == "hash":
        connection.execute("update streams set current_hash=?", ("0" * 64,))
    elif change == "schema":
        connection.execute("pragma user_version=2")
    elif change == "head":
        connection.execute("update streams set head_sequence=999")
    else:
        connection.execute("update events set event_json='{}' where sequence=1")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError):
        OrderAuditReader().read(trial.root, "account.sqlite")


def test_packet_corruption_is_not_missing_evidence(trial):
    (trial.root / "inputs" / (trial.packet + ".json")).write_text("{}")
    with pytest.raises(ValueError):
        OrderAuditReader().read(trial.root, "account.sqlite")


def test_sqlite_writer_is_denied(trial):
    connection = sqlite3.connect(trial.db.as_uri() + "?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("delete from events")
    finally:
        connection.close()
    with read_account(trial.db, EvidenceFiles()) as records:
        assert records.head.sequence == 2


def test_wal_refused_without_sidecar_creation(trial):
    connection = sqlite3.connect(trial.db)
    connection.execute("pragma journal_mode=wal")
    connection.close()
    before = fingerprint(trial.root)
    with pytest.raises(ObservationError, match="non_wal"):
        OrderAuditReader().read(trial.root, "account.sqlite")
    assert fingerprint(trial.root) == before


def test_input_mutation_before_publish_aborts(trial, tmp_path):
    bundle = OrderAuditReader().read(trial.root, "account.sqlite")
    output = tmp_path / "report"

    def mutate(_):
        (trial.root / "trial_plan.json").write_text("{}")

    with pytest.raises(ValueError):
        OrderAuditReportWriter().write(bundle, output, fault=mutate)
    assert not output.exists()
    assert not list(tmp_path.glob(".order-audit-*"))


def test_safe_html_csv_numeric_sequence_and_no_overwrite(trial, tmp_path):
    bundle = OrderAuditReader().read(trial.root, "account.sqlite")
    rows = []
    for i in range(12):
        row = bundle.records[0].to_dict()
        row.update(
            sequence=i,
            order_id='=HYPERLINK("bad")',
            candidate_id="<script>alert(1)</script>",
        )
        rows.append(OrderAuditRecord(JsonObject.from_value(row)))
    metadata = bundle.metadata.to_dict()
    metadata["order_count"] = len(rows)
    bundle = replace(
        bundle, records=tuple(reversed(rows)), metadata=JsonObject.from_value(metadata)
    )
    output = tmp_path / "safe"
    OrderAuditReportWriter().write(bundle, output)
    with (output / "order_audit.csv").open() as handle:
        csv_rows = list(csv.DictReader(handle))
    assert [int(r["sequence"]) for r in csv_rows] == list(range(12))
    assert csv_rows[0]["order_id"].startswith("'=")
    assert "<script>" not in (output / "order_audit.html").read_text()
    assert "&lt;script&gt;" in (output / "order_audit.html").read_text()
    exact = json.loads((output / "order_audit.json").read_text())
    assert exact["orders"][0]["order_id"].startswith("=")
    before = fingerprint(output)
    with pytest.raises(ValueError, match="no_overwrite"):
        OrderAuditReportWriter().write(bundle, output)
    assert fingerprint(output) == before


def test_output_inside_evidence_rejected_and_cli_failure(trial, tmp_path):
    bundle = OrderAuditReader().read(trial.root, "account.sqlite")
    with pytest.raises(ValueError, match="separate_output"):
        OrderAuditReportWriter().write(bundle, trial.root / "report")
    assert (
        main(
            [
                "--trial-root",
                str(tmp_path / "missing"),
                "--output",
                str(tmp_path / "out"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("value", [True, float("nan"), -100, "100"])
def test_invalid_quantity_rejected(trial, value):
    with pytest.raises(ValueError):
        OrderArithmeticVerifier().verify(
            dict(trial.frozen, shares=value),
            trial.plan["terms"],
            "daily_open_proxy_v1",
            "406",
            None,
            "pending",
        )


def test_unknown_cost_extension_not_silently_ignored(trial):
    terms = dict(trial.plan["terms"], minimum_commission="100")
    result = OrderArithmeticVerifier().verify(
        trial.frozen, terms, "daily_open_proxy_v1", "406", None, "pending"
    )
    assert result["status"] == "unsupported_verification_model"


def test_missing_terminal_resolution_has_unknown_accounting(tmp_path):
    trial = make_trial(tmp_path / "missing", include_resolution=False)
    bundle = OrderAuditReader().read(trial.root, "account.sqlite")
    row = bundle.records[0].to_dict()
    assert row["charged_commission"] is None and row["actual_cash_change"] is None
    assert row["verification"]["status"] == "insufficient_evidence"
    assert bundle.metadata.to_dict()["account_cash_reconciliation"] is None


def test_positive_fill_contradiction_is_not_hidden(tmp_path):
    trial = make_trial(tmp_path / "contradiction", status="filled")
    row = OrderAuditReader().read(trial.root, "account.sqlite").records[0].to_dict()
    assert row["saved_status"] == "filled"
    assert row["actual_cash_change"] == "-40681.65"
    assert row["charged_commission"] == "40.65"
    assert "filled_despite_frozen_constraint" in row["verification"]["mismatches"]


def test_actual_reader_uses_ro_transaction_and_denies_writes(trial, monkeypatch):
    real_connect = sqlite3.connect
    trace = []

    def connect(database_uri, **kwargs):
        assert database_uri.endswith("?mode=ro") and kwargs["uri"] is True
        connection = real_connect(database_uri, **kwargs)
        connection.set_trace_callback(trace.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    original_load = EventStore._load

    def load(store):
        assert store._connection.in_transaction
        with pytest.raises(sqlite3.DatabaseError):
            store._connection.execute("delete from events")
        return original_load(store)

    monkeypatch.setattr(EventStore, "_load", load)
    before = fingerprint(trial.root)
    OrderAuditReader().read(trial.root, "account.sqlite")
    assert trace.count("BEGIN") == trace.count("COMMIT") == 1
    assert not any(s.upper().startswith(("DELETE", "UPDATE", "INSERT")) for s in trace)
    assert fingerprint(trial.root) == before


def test_csv_formula_variants_and_exact_negative_number():
    from research_tools.order_report import csv_value

    for value in ("=1+1", "+cmd", "-cmd", "@SUM(1)", " \t=1", "\r=1", "\n=1"):
        assert csv_value(value).startswith("'")
    assert csv_value("-40681.65") == "-40681.65"


def test_missing_file_appearing_is_detected(tmp_path):
    path = tmp_path / "late.json"
    files = EvidenceFiles()
    with pytest.raises(ObservationError, match="missing_input"):
        files.read(path)
    path.write_text("{}")
    with pytest.raises(ObservationError, match="appeared"):
        files.verify()
