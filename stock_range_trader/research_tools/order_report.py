"""Offline escaped CSV/JSON/HTML bundle with atomic, non-overwriting publication."""

import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from delayed_replay.limited_trial.models import implementation_hash
from delayed_replay.serialization import JsonObject, digest, time_text

from . import __version__
from .reader import ObservationError


def tool_identity():
    root = Path(__file__).parents[1]
    sources = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "research_tools").glob("*.py"))
    }
    sources["pyproject.toml"] = hashlib.sha256(
        (root / "pyproject.toml").read_bytes()
    ).hexdigest()
    payload = dict(sources=sources, shared_source_hash=implementation_hash())
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=root,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        source = dict(commit=sha, state="dirty" if dirty else "clean")
    except (OSError, subprocess.CalledProcessError):
        source = dict(commit=None, state="git_unavailable")
    return dict(version=__version__, implementation_hash=digest(payload), **source)


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, str):
        if value[:1] in ("\t", "\r", "\n") or (
            value.lstrip()[:1] in ("=", "+", "-", "@")
            and not re.fullmatch(r"-?\d+(?:\.\d+)?", value)
        ):
            return "'" + value
    return value


def table_row(record):
    row = record.to_dict()
    verification = row.pop("verification")
    row.update(verification["calculated"])
    row.update(
        verification_status=verification["status"],
        constraint_diagnosis=verification["constraint_diagnosis"],
        mismatches=verification["mismatches"],
        missing_evidence=verification["missing_evidence"],
    )
    row["constraint_explanation"] = {
        "reservation_only_exceeded": "予約制約に抵触、固定予算には抵触しない",
        "budget_only_exceeded": "固定予算に抵触、予約制約には抵触しない",
        "both_exceeded": "予約制約・固定予算の両方に抵触",
        "within_both_limits": "予約制約・固定予算の両方以内（約定可否の再判定ではない）",
        "not_applicable": "適用外または証拠不足",
    }[verification["constraint_diagnosis"]]
    return row


def _output_contract(output, input_root):
    if output.exists() or output.is_symlink():
        raise ObservationError("output_exists_no_overwrite")
    target, original = output.resolve(), input_root.resolve()
    if target == original or original in target.parents or target in original.parents:
        raise ObservationError("separate_output_required")
    if any(p.is_symlink() for p in output.absolute().parents):
        raise ObservationError("symlink_output_refused")
    parent = output.parent
    while not parent.exists():
        parent = parent.parent
    try:
        repo = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=parent,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return  # Outside any worktree; private output permissions still apply.
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", str(target)], cwd=repo, check=False
    )
    if result.returncode != 0:
        raise ObservationError("output_must_be_git_ignored")


class OrderAuditReportWriter:
    def write(self, bundle, output, *, fault=None):
        output = Path(output).absolute()
        _output_contract(output, bundle.input_root)
        bundle.files.verify()
        records = sorted(
            bundle.records,
            key=lambda r: (
                r.to_dict()["sequence"] is None,
                r.to_dict()["sequence"] or 0,
                r.to_dict()["order_id"],
            ),
        )
        if bundle.metadata.to_dict()["order_count"] != len(records):
            raise ObservationError("report_order_count_mismatch")
        rows = [table_row(record) for record in records]
        columns = list(dict.fromkeys(key for row in rows for key in row)) or [
            "order_id",
            "sequence",
            "saved_status",
            "verification_status",
        ]
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: csv_value(v) for k, v in row.items()} for row in rows)
        metadata = bundle.metadata.to_dict()
        exact = JsonObject.from_value(
            dict(
                schema="order-audit-v1",
                metadata=metadata,
                orders=[r.to_dict() for r in records],
            )
        )
        title = "注文監査 — 研究用 simulated_fill（実約定ではありません）"
        content = [
            '<!doctype html><html lang="ja"><meta charset="utf-8">',
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">",
            "<title>"
            + html.escape(title)
            + "</title><body><h1>"
            + html.escape(title)
            + "</h1>",
            "<p>hash一致は真正性や実約定の証明ではありません。売買判断・清算・状態遷移の再実行はしていません。</p>",
            "<p>欠測は空欄/未検証として扱います。JSONが精度を保持した機械用原本です。</p>",
            "<p>口座Cash照合: "
            + html.escape(str(metadata["account_cash_reconciliation"]))
            + "</p>",
            "<table><thead><tr>"
            + "".join("<th>" + html.escape(c) + "</th>" for c in columns)
            + "</tr></thead><tbody>",
        ]
        for row in rows:
            content.append(
                "<tr>"
                + "".join(
                    "<td>"
                    + html.escape(
                        json.dumps(row.get(c), ensure_ascii=False)
                        if isinstance(row.get(c), (dict, list))
                        else str(row.get(c, ""))
                    )
                    + "</td>"
                    for c in columns
                )
                + "</tr>"
            )
        content.append("</tbody></table></body></html>")
        artifacts = {
            "order_audit.json": exact.encoded.encode(),
            "order_audit.csv": buffer.getvalue().encode(),
            "order_audit.html": "\n".join(content).encode(),
        }
        manifest = dict(
            schema="order-audit-report-manifest-v1",
            tool=tool_identity(),
            generated_at=time_text(datetime.now(UTC)),
            input=metadata,
            input_file_hashes=bundle.files.hashes,
            verification_counts={
                s: sum(r["verification_status"] == s for r in rows)
                for s in (
                    "verified",
                    "mismatch",
                    "insufficient_evidence",
                    "unsupported_verification_model",
                )
            },
            artifacts={
                name: hashlib.sha256(body).hexdigest()
                for name, body in artifacts.items()
            },
            json_decimal_encoding="exact_decimal_strings",
            csv_text_safety="formula_prefixed_apostrophe_exact_values_in_json",
            execution_invoked=False,
            formal_oos=False,
            authorization_changed=False,
        )
        artifacts["report_manifest.json"] = JsonObject.from_value(
            manifest
        ).encoded.encode()
        return publish_artifacts(bundle, output, artifacts, fault=fault)


def publish_artifacts(bundle, output, artifacts, *, fault=None):
    """Shared private, atomic, non-overwriting report publication."""
    output = Path(output).absolute()
    _output_contract(output, bundle.input_root)
    bundle.files.verify()
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.parent / ("." + output.name + ".publish-lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    temporary = None
    try:
        os.close(fd)
        _output_contract(output, bundle.input_root)
        temporary = Path(tempfile.mkdtemp(prefix=".order-audit-", dir=output.parent))
        for name, body in artifacts.items():
            if Path(name).name != name or name in (".", ".."):
                raise ObservationError("invalid_artifact_name")
            path = temporary / name
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            if path.read_bytes() != body:
                raise ObservationError("published_artifact_mismatch")
            if fault is not None:
                fault(name)
        bundle.files.verify()
        if output.exists() or output.is_symlink():
            raise ObservationError("output_exists_no_overwrite")
        temporary.rename(output)
        temporary = None
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)
        lock.unlink(missing_ok=True)
    return output
