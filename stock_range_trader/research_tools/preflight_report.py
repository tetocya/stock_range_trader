"""Offline stage-readiness reports. Report success is not execution permission."""

import argparse
import csv
import hashlib
import html
import io
import json
from datetime import UTC, datetime
from pathlib import Path

from delayed_replay.serialization import JsonObject, time_text

from .order_report import _output_contract, csv_value, publish_artifacts, tool_identity
from .preflight import STAGES, ReadOnlyPreflightInspector


class PreflightReportWriter:
    def write(self, bundle, output, *, fault=None):
        # Neither June nor the independent May evidence tree may be an output.
        _output_contract(Path(output).absolute(), bundle.other_input_root)
        data = bundle.payload.to_dict()
        rows = [c for stage in data["stages"] for c in stage["checks"]]
        buffer = io.StringIO(newline="")
        columns = [
            "stage",
            "check_id",
            "status",
            "reason",
            "required",
            "checked_at",
            "evidence_refs",
            "next_action",
            "details",
        ]
        writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: csv_value(v) for k, v in row.items()} for row in rows)

        def esc(v):
            return html.escape(str(v), quote=True)

        page = [
            '<!doctype html><html lang="ja"><head><meta charset="utf-8">',
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">",
            "<title>実行前チェック — 読取専用診断</title><style>body{font:16px system-ui;margin:2rem}table{border-collapse:collapse}td,th{padding:.6rem;border:1px solid #ccc;text-align:left;vertical-align:top}td{max-width:40rem;overflow-wrap:anywhere}details{white-space:pre-wrap}</style></head><body>",
            "<h1>実行前チェックの結果一覧</h1><p>診断専用。段階別readyは実行許可でも全体の実行可否でもありません。取得・清算・認証通信・予算開始は行いません。</p>",
            "<p>確認日時: " + esc(data["checked_at"]) + "</p>",
        ]
        for stage in data["stages"]:
            page.append(
                "<h2>"
                + esc(stage["stage"])
                + " — "
                + esc(stage["status"])
                + "</h2><p>"
                + esc("ready_for_" + stage["stage"])
                + ": "
                + esc(stage["ready_for_" + stage["stage"]])
                + "</p>"
            )
            page.append(
                "<table><thead><tr>"
                + "".join("<th>" + esc(k) + "</th>" for k in columns)
                + "</tr></thead><tbody>"
            )
            for row in stage["checks"]:
                page.append(
                    "<tr>"
                    + "".join(
                        "<td>"
                        + (
                            "<details><summary>証拠hash</summary>"
                            + esc(json.dumps(row[k]))
                            + "</details>"
                            if k == "evidence_refs"
                            else esc(
                                json.dumps(row[k], ensure_ascii=False)
                                if isinstance(row[k], (dict, list))
                                else row[k]
                            )
                        )
                        + "</td>"
                        for k in columns
                    )
                    + "</tr>"
                )
            page.append("</tbody></table>")
        page.append("</body></html>")
        artifacts = {
            "preflight_checks.json": bundle.payload.encoded.encode(),
            "preflight_checks.csv": buffer.getvalue().encode(),
            "preflight_report.html": "\n".join(page).encode(),
        }
        manifest = dict(
            schema="preflight-report-manifest-v1",
            tool=tool_identity(),
            generated_at=time_text(datetime.now(UTC)),
            checked_at=data["checked_at"],
            plan_hash=data["plan_hash"],
            acquisition_head=data["acquisition_head"],
            input_file_hashes=bundle.files.hashes,
            stages=[
                {k: v for k, v in stage.items() if k != "checks"}
                for stage in data["stages"]
            ],
            artifacts={k: hashlib.sha256(v).hexdigest() for k, v in artifacts.items()},
            diagnostic_only=True,
            execution_invoked=False,
            budget_changed=False,
            authorization_changed=False,
            formal_oos=False,
        )
        artifacts["report_manifest.json"] = JsonObject.from_value(
            manifest
        ).encoded.encode()
        return publish_artifacts(bundle, output, artifacts, fault=fault)


def result_exit_code(bundle):
    statuses = [s["status"] for s in bundle.payload.to_dict()["stages"]]
    return 3 if "error" in statuses else 0 if all(s == "ready" for s in statuses) else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-root", required=True, type=Path)
    parser.add_argument("--may-root", required=True, type=Path)
    parser.add_argument("--stage", choices=(*STAGES, "all"), default="all")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--acquisition-authorization", default="owner_approved_acquisition.json"
    )
    parser.add_argument(
        "--clearing-authorization", default="owner_approved_clearing.json"
    )
    parser.add_argument(
        "--account", help="Explicit relative saved account DB; never create one"
    )
    args = parser.parse_args(argv)
    try:
        bundle = ReadOnlyPreflightInspector().inspect(
            args.trial_root,
            args.may_root,
            stage=args.stage,
            acquisition_authorization=args.acquisition_authorization,
            clearing_authorization=args.clearing_authorization,
            account=args.account,
        )
        PreflightReportWriter().write(bundle, args.output)
        print(
            json.dumps(
                dict(
                    report_written=True,
                    diagnostic_only=True,
                    stages={
                        s["stage"]: s["status"]
                        for s in bundle.payload.to_dict()["stages"]
                    },
                )
            )
        )
        return result_exit_code(bundle)
    except (ValueError, OSError, TypeError, KeyError, OverflowError):
        print(
            json.dumps(
                dict(
                    report_written=False,
                    status="error",
                    reason="input_or_output_contract",
                    execution_invoked=False,
                )
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
