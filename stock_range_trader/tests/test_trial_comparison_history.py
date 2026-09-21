"""Real June service with artificial inputs; observers never run clearing."""

import json

import pytest
from delayed_replay_e2e_helpers import network_guard as network_guard  # noqa: F401
from test_june_clearing import built
from test_june_proxy_trial import prepared as prepared  # noqa: F401

from delayed_replay import june_clearing as clear
from delayed_replay.input_artifacts import InputArtifactStore
from research_tools.account_view import AccountReadModel, AccountViewBuilder
from research_tools.compare_trials import TrialComparisonReportWriter, main
from research_tools.order_audit import OrderAuditReader
from research_tools.reader import EvidenceFiles, ObservationError, read_account
from research_tools.trial_comparison import (
    TrialComparisonBuilder,
    TrialComparisonInput,
    _inventory,
)
from tests.test_order_audit import fingerprint
from tests.test_preflight_report import freeze_writes
from tests.test_trial_comparison import fixture

pytestmark = pytest.mark.usefixtures("network_guard")


@pytest.fixture
def june_account(prepared):
    root, may, plan, auth, clock, _ = built(prepared)
    service = clear.JuneClearingService.create(
        root / "saved.sqlite", plan, auth, root, may, "continuous"
    )
    service.store.close()
    return root, may, plan, auth, clock


def reference(june_account):
    _, may, plan, *_ = june_account
    sha = plan.payload.to_dict()["history_packets"][0]
    packet = InputArtifactStore(may / "inputs").load(sha)
    raw = packet.market.snapshots[0].source_artifact_sha256
    return may / "inputs" / (sha + ".json"), may / (raw + ".json")


def read(june_account, **kwargs):
    root, may, *_ = june_account
    return TrialComparisonInput.read(root, "saved.sqlite", history_root=may, **kwargs)


def test_service_generated_june_history_read_without_copy(
    june_account, monkeypatch, tmp_path
):
    root, may, plan, auth, clock = june_account
    service = clear.JuneClearingService.resume(
        root / "saved.sqlite", plan, auth, root, may, "continuous"
    )
    try:
        service.run(clock.now())
        assert service.state["status"] == "completed"
    finally:
        service.store.close()
    before = fingerprint(root), fingerprint(may)
    freeze_writes(monkeypatch)
    audit = OrderAuditReader().read(root, "saved.sqlite")
    view = (
        AccountViewBuilder()
        .build(AccountReadModel.read(root, "saved.sqlite"))
        .to_dict()
    )
    assert all(
        r.to_dict()["verification"]["status"] == "verified" for r in audit.records
    )
    assert view["validation"]["status"] == "saved_records_checked_not_replayed"
    item = read(june_account)
    row = item.payload.to_dict()
    assert row["provenance"] == "artificial_fixture"
    assert row["input_validation"] == dict(
        status="saved_rows_checked", missing_evidence=[]
    )
    # This synthetic calendar has 104 warm-up observations, unlike saved May data.
    assert row["input_counts"]["warmup_observations"] == 104
    assert row["metrics"]["filled_count"] == 4
    assert row["metrics"]["final_cash"] == row["metrics"]["final_equity"] == "206565.83"
    assert row["audit_validation"] == view["validation"]
    for sha in plan.payload.to_dict()["history_packets"]:
        assert not (root / "inputs" / (sha + ".json")).exists()
    output = TrialComparisonReportWriter().write(
        TrialComparisonBuilder().build([item]), tmp_path / "report"
    )
    manifest = json.loads((output / "comparison_manifest.json").read_text())
    for path in reference(june_account):
        assert fingerprint(path.parent)[path.name] in manifest["input_file_hashes"]
    assert str(may) not in (output / "comparison_manifest.json").read_text()
    assert (fingerprint(root), fingerprint(may)) == before


@pytest.mark.parametrize(
    "kind",
    [
        "missing_packet",
        "changed_packet",
        "missing_raw",
        "changed_raw",
        "wrong_root",
        "symlink_root",
    ],
)
def test_bad_history_refused(june_account, monkeypatch, tmp_path, kind):
    root, may, *_ = june_account
    packet, raw = reference(june_account)
    history = may
    if kind.startswith("missing_"):
        (packet if kind.endswith("packet") else raw).unlink()
    elif kind.startswith("changed_"):
        path = packet if kind.endswith("packet") else raw
        path.write_text("{}")
    elif kind == "wrong_root":
        history = root  # No search/fallback to the correct sibling May root.
    else:
        history = tmp_path / "history-link"
        history.symlink_to(may, target_is_directory=True)
    before = fingerprint(root), fingerprint(may)
    freeze_writes(monkeypatch)
    with pytest.raises(ObservationError):
        TrialComparisonInput.read(root, "saved.sqlite", history_root=history)
    assert (fingerprint(root), fingerprint(may)) == before


def test_absent_history_root_fails_closed(june_account, monkeypatch):
    root, *_ = june_account
    freeze_writes(monkeypatch)
    with pytest.raises(ObservationError, match="missing_input"):
        TrialComparisonInput.read(root, "saved.sqlite")


def test_routing_uses_declared_packet_hash(june_account, monkeypatch):
    import research_tools.trial_comparison as comparison

    root, may, plan, *_ = june_account
    original = comparison._price_evidence
    calls = []

    def spy(source, state, symbol, day, files, cache):
        calls.append((state["inputs"][symbol + "|" + day]["packet"], source))
        return original(source, state, symbol, day, files, cache)

    monkeypatch.setattr(comparison, "_price_evidence", spy)
    freeze_writes(monkeypatch)
    read(june_account)
    history = set(plan.payload.to_dict()["history_packets"])
    run = set(plan.payload.to_dict()["packets"])
    assert {sha for sha, _ in calls} == history | run
    assert all(source == (may if sha in history else root) for sha, source in calls)


@pytest.mark.parametrize("kind", ["overlap", "duplicate", "undeclared", "not_list"])
def test_packet_mapping_rejected(june_account, kind):
    root, may, *_ = june_account
    files = EvidenceFiles()
    with read_account(root / "saved.sqlite", files) as stored:
        state = stored.current_state.to_dict()
        plan = state["identity"]["plan"]
        if kind == "overlap":
            plan["history_packets"].append(plan["packets"][0])
        elif kind == "duplicate":
            plan["history_packets"] *= 2
        elif kind == "not_list":
            plan["history_packets"] = {}
        else:
            next(iter(state["inputs"].values()))["packet"] = "0" * 64
        with pytest.raises(ObservationError):
            _inventory(state, stored, root, files, {}, history_root=may)


def test_history_root_protected_as_output(june_account):
    _, may, *_ = june_account
    bundle = TrialComparisonBuilder().build([read(june_account)])
    with pytest.raises(ObservationError, match="separate_output"):
        TrialComparisonReportWriter().write(bundle, may / "report")
    assert not (may / "report").exists()


@pytest.mark.parametrize("when", ["before", "during"])
def test_history_change_aborts_atomic_publication(june_account, tmp_path, when):
    _, raw = reference(june_account)
    bundle = TrialComparisonBuilder().build([read(june_account)])

    def changed(*_):
        raw.write_text("{}")

    if when == "before":
        changed()
    output = tmp_path / "report"
    with pytest.raises(ObservationError, match="input_changed"):
        TrialComparisonReportWriter().write(
            bundle, output, fault=changed if when == "during" else None
        )
    assert not output.exists()
    assert not list(tmp_path.glob(".order-audit-*"))


def test_cli_history_mapping_per_trial(june_account, tmp_path, monkeypatch, capsys):
    root, may, *_ = june_account
    other = fixture(tmp_path / "other-may")
    freeze_writes(monkeypatch)
    args = [
        "--trial-root",
        str(other),
        "--account",
        "comparison/continuous.sqlite",
        "--history-root",
        "-",
        "--trial-root",
        str(root),
        "--account",
        "saved.sqlite",
        "--history-root",
        str(may),
        "--output",
        str(tmp_path / "report"),
    ]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["report_written"] is True
    data = json.loads((tmp_path / "report/trial_comparison.json").read_text())
    assert len(data["trials"]) == 2
    assert all(
        row["input_validation"]["status"] == "saved_rows_checked"
        for row in data["trials"]
    )


def test_cli_rejects_missing_or_misaligned_history(june_account, tmp_path, capsys):
    root, *_ = june_account
    assert (
        main(
            [
                "--trial-root",
                str(root),
                "--account",
                "saved.sqlite",
                "--output",
                str(tmp_path / "no-history"),
            ]
        )
        == 2
    )
    assert str(root) not in capsys.readouterr().out
    assert not (tmp_path / "no-history").exists()
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--trial-root",
                str(root),
                "--trial-root",
                str(root),
                "--history-root",
                "-",
                "--output",
                str(tmp_path / "bad"),
            ]
        )
    assert error.value.code == 2
