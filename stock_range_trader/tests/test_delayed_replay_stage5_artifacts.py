"""Pre-result candidate integrity, exclusive publication and no side effects."""

import hashlib
import json
from dataclasses import replace
from datetime import date, timedelta

import pytest
from test_delayed_replay_stage5_judge import NOW, checkpoint

from delayed_replay.checkpoint_report import (
    ARTIFACTS_FILENAME,
    CHECKPOINTS_FILENAME,
    REGISTRATION_FILENAME,
    RESULT_FILENAME,
    write_checkpoint_results,
    write_registration_candidate,
)
from delayed_replay.protocol_judge import ProtocolJudge
from delayed_replay.registration_candidate import (
    RegistrationCandidate,
    RegistrationInputs,
    SourceIdentity,
    VerificationEvidence,
    build_registration_candidate,
)
from delayed_replay.serialization import JsonObject
from delayed_replay.validation import ReplayContractError


def draft(**changes):
    values = dict(
        protocol_hash=None,
        source=SourceIdentity("git_unavailable", None, None),
        account_policy=None,
        replay_policy=None,
        config_hash=None,
        catalog_hash=None,
        selection_policy_hash=None,
        price_contract_hash=None,
        universe=None,
        universe_reference_date=None,
        calendar_hash=None,
        planned_start=None,
        one_month_finalization=None,
        three_month_finalization=None,
        history_snapshot_hashes=(),
        verification=(),
        unresolved_operational_fields=("fees", "max_positions", "lookback_months"),
    )
    values.update(changes)
    return RegistrationInputs(**values)


def test_draft_none_values_and_capability_gaps():
    result = build_registration_candidate(draft(), generated_at=NOW).to_dict()
    assert result["account_policy"] is None and result["planned_start"] is None
    assert result["formal_registration_performed"] is False
    assert result["registration_status"] == "draft_not_registered"
    for reason in (
        "source_git_unavailable",
        "source_hash_missing",
        "real_daily_open_evidence_unsupported",
        "additional_input_events_unsupported",
        "live_price_basis_unverified",
        "live_lot_unverified",
        "live_calendar_unverified",
        "unapproved_fees",
        "unresolved_one_month_finalization",
    ):
        assert reason in result["structural_unmet"]
    assert "registration_timestamp" not in result


def test_candidate_identity_order_time_and_no_result_dependency():
    inputs = draft(
        universe=("B", "A"),
        history_snapshot_hashes=("b" * 64, "a" * 64),
        planned_start=date(2024, 12, 1),
    )
    first = build_registration_candidate(inputs, generated_at=NOW)
    second = build_registration_candidate(
        replace(
            inputs,
            universe=("A", "B"),
            history_snapshot_hashes=("a" * 64, "b" * 64),
            unresolved_operational_fields=tuple(
                reversed(inputs.unresolved_operational_fields)
            ),
        ),
        generated_at=NOW + timedelta(days=1),
    )
    assert first.candidate_id == second.candidate_id
    assert first.to_dict()["one_month_boundary"] == "2025-01-01"
    assert first.to_dict()["three_month_boundary"] == "2025-03-01"
    # Outcomes are not accepted by the builder, and changing outcomes has no path
    # into the fixed inputs.
    for equity in ("100000", "200000", "300000"):
        ProtocolJudge().evaluate(checkpoint(1), checkpoint(3, equity), now=NOW)
        assert (
            build_registration_candidate(inputs, generated_at=NOW).candidate_id
            == first.candidate_id
        )
    with pytest.raises(TypeError):
        build_registration_candidate(inputs, generated_at=NOW, outcome="PASS")


def test_candidate_digest_and_payload_revalidated():
    original = build_registration_candidate(draft(), generated_at=NOW)
    with pytest.raises(ReplayContractError):
        replace(original, payload_sha256="0" * 64)
    value = original.payload.to_dict()
    value["initial_capital"] = "1000000"
    with pytest.raises(ReplayContractError):
        replace(original, payload=JsonObject.from_value(value))
    value = original.payload.to_dict()
    value["formal_registration_performed"] = True
    payload = JsonObject.from_value(value)
    with pytest.raises(ReplayContractError):
        RegistrationCandidate(
            payload, payload.sha256, "registration-candidate-" + payload.sha256, NOW
        )


def test_verification_hash_source_kind_and_subject():
    source = SourceIdentity("dirty", "a" * 40, "b" * 40)
    data = JsonObject.from_value(
        dict(
            schema="delayed-verification-1",
            evidence_id="synthetic-tests",
            kind="synthetic_test",
            source_commit=source.commit,
            source_tree=source.tree,
            subject_hash="c" * 64,
            result="passed",
        )
    )
    proof = VerificationEvidence(data, data.sha256)
    with pytest.raises(ReplayContractError):
        replace(proof, payload_sha256="0" * 64)
    with pytest.raises(ReplayContractError):
        draft(source=SourceIdentity("clean", "d" * 40, "b" * 40), verification=(proof,))
    candidate = build_registration_candidate(
        draft(source=source, verification=(proof,)), generated_at=NOW
    ).to_dict()
    assert "source_dirty" in candidate["structural_unmet"]
    assert "live_price_basis_unverified" in candidate["structural_unmet"]
    assert "operational_approval_unverified" in candidate["structural_unmet"]
    changed = data.to_dict()
    changed["kind"] = "live_price_basis"
    with pytest.raises(ReplayContractError):
        VerificationEvidence(JsonObject.from_value(changed), data.sha256)
    with pytest.raises(ReplayContractError):
        draft(verification=(True,))


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_key", "secret"),
        ("git_root", "/Users/private/repo"),
        ("description", "Bearer secret"),
        ("description", "/home/private/repo"),
    ],
)
def test_private_fields_and_paths_never_exported(field, value):
    original = build_registration_candidate(draft(), generated_at=NOW)
    data = original.payload.to_dict()
    data[field] = value
    payload = JsonObject.from_value(data)
    with pytest.raises(ReplayContractError):
        RegistrationCandidate(
            payload, payload.sha256, "registration-candidate-" + payload.sha256, NOW
        )


def test_candidate_export_separate_exclusive_and_path_invariant(tmp_path):
    candidate = build_registration_candidate(draft(), generated_at=NOW)
    first = write_registration_candidate(candidate, tmp_path / "preconditions-a")
    second = write_registration_candidate(candidate, tmp_path / "preconditions-b")
    assert (
        first.name == REGISTRATION_FILENAME
        and first.read_bytes() == second.read_bytes()
    )
    with pytest.raises(FileExistsError):
        write_registration_candidate(candidate, first.parent)
    assert not list(first.parent.glob(".candidate-*"))


def test_result_atomic_hashes_determinism_and_no_reexecution(tmp_path, monkeypatch):
    from delayed_replay.event_store import EventStore
    from delayed_replay.replay_engine import ReplayEngine
    from walkforward.executable_evaluation import ExecutableOutcomeEvaluator

    def forbidden(*args, **kwargs):
        raise AssertionError("export_side_effect")

    monkeypatch.setattr(ReplayEngine, "run", forbidden)
    monkeypatch.setattr(EventStore, "commit_event", forbidden)
    monkeypatch.setattr(ExecutableOutcomeEvaluator, "evaluate_validation", forbidden)
    one, three = checkpoint(1), checkpoint(3, "210000")
    result = ProtocolJudge().evaluate(one, three, now=NOW)
    out = write_checkpoint_results(one, three, result, tmp_path / "results")
    metadata = json.loads((out / ARTIFACTS_FILENAME).read_text())
    assert set(metadata["files"]) == {CHECKPOINTS_FILENAME, RESULT_FILENAME}
    assert ARTIFACTS_FILENAME not in metadata["files"]
    for name, expected in metadata["files"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == expected
    again = write_checkpoint_results(one, three, result, tmp_path / "other-results")
    assert all(
        (out / name).read_bytes() == (again / name).read_bytes()
        for name in (ARTIFACTS_FILENAME, CHECKPOINTS_FILENAME, RESULT_FILENAME)
    )
    assert REGISTRATION_FILENAME not in {p.name for p in out.iterdir()}
    with pytest.raises(FileExistsError):
        write_checkpoint_results(one, three, result, out)
    with pytest.raises(ReplayContractError):
        write_checkpoint_results(
            one, replace(three, equity="220000"), result, tmp_path / "tampered"
        )


@pytest.mark.parametrize(
    "name", [CHECKPOINTS_FILENAME, RESULT_FILENAME, ARTIFACTS_FILENAME]
)
def test_publication_failure_cleanup(tmp_path, name):
    one, three = checkpoint(1), checkpoint(3, "210000")
    result = ProtocolJudge().evaluate(one, three, now=NOW)

    def fail(written):
        if written == name:
            raise RuntimeError("synthetic_write_failure")

    with pytest.raises(RuntimeError):
        write_checkpoint_results(one, three, result, tmp_path / "result", fault=fail)
    assert not list(tmp_path.iterdir())
