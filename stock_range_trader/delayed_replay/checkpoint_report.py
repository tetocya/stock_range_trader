"""Separate, deterministic precondition and result exports; no ledger writes."""

import csv
import hashlib
import io
import os
import shutil
import tempfile
from pathlib import Path

from .checkpoint_models import ProtocolResult
from .protocol_judge import ProtocolJudge
from .registration_candidate import RegistrationCandidate, public_payload
from .serialization import canonical_json, parse_time
from .validation import ReplayContractError

REGISTRATION_FILENAME = "registration_candidate.json"
CHECKPOINTS_FILENAME = "checkpoints.csv"
RESULT_FILENAME = "protocol_result.json"
ARTIFACTS_FILENAME = "checkpoint_artifacts.json"
ARTIFACT_SCHEMA = "delayed-checkpoint-artifacts-1"
CSV_COLUMNS = (
    "months",
    "boundary",
    "expected_session",
    "actual_session",
    "market_at",
    "valuation_state",
    "sample_state",
    "equity",
    "initial_equity",
    "return",
    "unique_symbols",
    "completed_trades",
    "evidence_hash",
)


def encoded(value):
    public_payload(value)
    return (canonical_json(value) + "\n").encode("utf-8")


def write_registration_candidate(candidate: RegistrationCandidate, output: Path):
    """Atomic exclusive file publication, separately callable before any replay."""
    if not isinstance(candidate, RegistrationCandidate):
        raise ReplayContractError("registration_candidate_required")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / REGISTRATION_FILENAME
    fd, name = tempfile.mkstemp(prefix=".candidate-", dir=output)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(encoded(candidate.to_dict()))
            file.flush()
            os.fsync(file.fileno())
        os.link(name, target)  # Atomic, fails if destination already exists.
    finally:
        Path(name).unlink(missing_ok=True)
    return target


def write_checkpoint_results(
    one, three, result: ProtocolResult, output: Path, *, fault=None
):
    """Only fixed evidence enters; no callbacks to evaluator, runner or store."""
    if not isinstance(result, ProtocolResult):
        raise ReplayContractError("protocol_result_required")
    result_data = result.to_dict()
    recomputed = ProtocolJudge().evaluate(
        one, three, now=parse_time(result_data["assessed_at"])
    )
    if recomputed != result:
        raise ReplayContractError("result_evidence_mismatch")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for checkpoint in (one, three):
        data = checkpoint.to_dict()
        row = {name: data.get(name) for name in CSV_COLUMNS}
        row.update(
            valuation_state=checkpoint.valuation.status,
            sample_state=checkpoint.samples.status,
            evidence_hash=checkpoint.sha256,
        )
        writer.writerow(row)
    # Full evidence (including deadline/secondary/provenance) stays with result,
    # while the CSV is deliberately a compact audit table.
    result_data["checkpoints"] = [one.to_dict(), three.to_dict()]
    content = {
        CHECKPOINTS_FILENAME: buffer.getvalue().encode(),
        RESULT_FILENAME: encoded(result_data),
    }
    metadata = dict(
        schema=ARTIFACT_SCHEMA,
        files={
            name: hashlib.sha256(body).hexdigest() for name, body in content.items()
        },
        evidence_hashes=[one.sha256, three.sha256],
        result_payload_sha256=result.payload_sha256,
    )
    content[ARTIFACTS_FILENAME] = encoded(metadata)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.parent / ("." + output.name + ".publish-lock")
    # Serialize cooperating publishers and recheck before rename. Never replace
    # an existing result, even an empty directory or symlink.
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    temp = None
    try:
        os.close(fd)
        if output.exists() or output.is_symlink():
            raise FileExistsError(output)
        temp = Path(tempfile.mkdtemp(prefix=".checkpoint-", dir=output.parent))
        for name, body in content.items():
            with (temp / name).open("xb") as file:
                file.write(body)
                file.flush()
                os.fsync(file.fileno())
            if (temp / name).read_bytes() != body:
                raise ReplayContractError("written_artifact_mismatch")
            if fault is not None:
                fault(name)
        if output.exists() or output.is_symlink():
            raise FileExistsError(output)
        temp.rename(output)
        temp = None
    finally:
        if temp is not None:
            shutil.rmtree(temp)
        lock.unlink(missing_ok=True)
    return output
