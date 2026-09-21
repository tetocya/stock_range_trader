"""Explicit saved trial comparison; offline, read-only and non-executing."""

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
from .trial_comparison import TrialComparisonBuilder, TrialComparisonInput

RESULT_COLUMNS = (
    "order_count",
    "buy_count",
    "sell_count",
    "filled_count",
    "rejected_count",
    "cancelled_count",
    "waiting_count",
    "terminal_order_count",
    "fill_rate_percent",
    "entry_condition_count",
    "charged_commission_as_of",
    "cash_as_of",
    "equity_as_of",
    "realized_profit_as_of",
    "holdings_as_of",
    "final_cash",
    "final_equity",
    "end_holdings",
    "completed_trades_as_of",
    "last_finalized_session",
)


def _rows(data):
    return [
        dict(
            trial_id=t["trial_id"],
            run_id=t["run_id"],
            replica_group=t["replica_group"],
            provenance=t["provenance"],
            availability=t["availability"],
            conditions=t["conditions"],
            input_validation=t["input_validation"],
            audit_validation=t["audit_validation"],
            input_counts=t["input_counts"],
            saved_comparison=t["saved_comparison"],
            limitations=t["limitations"],
            **(t["metrics"] or {}),
        )
        for t in data["trials"]
    ]


def _display(value):
    if value is None:
        return "N/A"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _table(rows, columns):
    return (
        "<table><thead><tr>"
        + "".join("<th>" + html.escape(c) + "</th>" for c in columns)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>"
            + "".join(
                "<td>" + html.escape(_display(row.get(c))) + "</td>" for c in columns
            )
            + "</tr>"
            for row in rows
        )
        + "</tbody></table>"
    )


class TrialComparisonReportWriter:
    def write(self, bundle, output, *, fault=None):
        for item in bundle.inputs:
            for root in item.evidence_roots:
                _output_contract(Path(output).absolute(), root)
        bundle.files.verify()
        data = bundle.payload.to_dict()
        rows = _rows(data)
        columns = sorted({k for row in rows for k in row} | set(RESULT_COLUMNS))
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: csv_value(row.get(k)) for k in columns} for row in rows)
        page = [
            '<!doctype html><html lang="ja"><head><meta charset="utf-8">',
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">",
            "<title>試験結果の比較 — 読取専用研究レポート</title><style>body{font:16px system-ui;margin:2rem}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.5rem;vertical-align:top;text-align:left}td{max-width:35rem;overflow-wrap:anywhere}section{overflow:auto}</style></head><body>",
            "<h1>試験結果の比較レポート</h1><p>研究用 simulated_fill（実約定ではありません）。保存済み記録のみ。取得・戦略・清算の再実行はしていません。</p>",
            "<p>人工・未取得期間は実市場結果ではありません。各行のprovenanceを参照してください。独立口座の合算、Equity接続、複利合成、ランキング、推薦、正式OOS判定はありません。</p>",
            "<h2>1. 条件と証拠（結果の前に確認）</h2><section>",
            _table(
                rows,
                [
                    "trial_id",
                    "run_id",
                    "provenance",
                    "availability",
                    "conditions",
                    "input_validation",
                    "audit_validation",
                ],
            ),
            "</section><h2>2. 条件一致・不一致・同等性未確認</h2>",
            "<p>conditions_matchは保存契約の一致であり、実行の正当性や実市場の同等性の証明ではありません。実装hashの差だけでは計算規則の差と断定しません。</p>",
        ]
        for assessment in data["assessments"]:
            page.extend(
                [
                    "<h3>"
                    + html.escape(
                        assessment["left_run"] + " / " + assessment["right_run"]
                    )
                    + "</h3>",
                    "<p>"
                    + html.escape(
                        assessment["status"]
                        + " / equivalence: "
                        + assessment["equivalence"]
                    )
                    + "</p><section>",
                    _table(
                        assessment["fields"], ["condition", "left", "right", "status"]
                    ),
                    "</section>",
                ]
            )
        page.extend(
            [
                "<h2>3. 試験別の保存結果</h2><p>約定率 = filled / 終端注文（filled・rejected・cancelled・canceled）。pending・waitingは分母から除外。分母0はN/A、全終端拒否は0%。as_of値は読取head時点であり、未完了試験の最終結果ではありません。欠測はN/A（CSV空欄・JSON null）。</p><section>",
                _table(rows, columns),
                "</section><h2>4. 複製グループ</h2><p>同一plan・同一宣言入力の別実行は複製です。実行行数を独立市場サンプル数として扱わず、件数・金額の合計も出しません。</p>",
                _table(data["replica_groups"], ["group_id", "runs"]),
                "</body></html>",
            ]
        )
        artifacts = {
            "trial_comparison.json": bundle.payload.encoded.encode(),
            "trial_comparison.csv": buffer.getvalue().encode(),
            "trial_comparison.html": "\n".join(page).encode(),
        }
        manifest = dict(
            schema="trial-comparison-manifest-v1",
            tool=tool_identity(),
            generated_at=time_text(datetime.now(UTC)),
            deterministic_content_hash=bundle.payload.sha256,
            input_file_hashes=bundle.files.hashes,
            inputs=[
                {
                    k: t[k]
                    for k in (
                        "trial_id",
                        "run_id",
                        "provenance",
                        "availability",
                        "metadata",
                        "input_validation",
                        "audit_validation",
                        "replica_group",
                    )
                }
                for t in data["trials"]
            ],
            artifacts={k: hashlib.sha256(v).hexdigest() for k, v in artifacts.items()},
            json_decimal_encoding=data["decimal_encoding"],
            execution_invoked=False,
            authorization_changed=False,
            budget_changed=False,
            formal_oos=False,
        )
        artifacts["comparison_manifest.json"] = JsonObject.from_value(
            manifest
        ).encoded.encode()
        return publish_artifacts(bundle, output, artifacts, fault=fault)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-root", type=Path, action="append", required=True)
    parser.add_argument(
        "--account",
        action="append",
        help="Relative DB per trial, in the same order; omit all for comparison/continuous.sqlite",
    )
    parser.add_argument(
        "--history-root",
        action="append",
        help="History evidence root per trial, in the same order; '-' or omission uses that trial root. Only plan.history_packets are routed here.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.account is not None and len(args.account) != len(args.trial_root):
        parser.error("provide one --account per --trial-root or omit all")
    if args.history_root is not None and len(args.history_root) != len(args.trial_root):
        parser.error(
            "provide one --history-root per --trial-root or omit all; use '-' for a local history root"
        )
    try:
        inputs = [
            TrialComparisonInput.read(
                root, account, history_root=None if history in (None, "-") else history
            )
            for root, account, history in zip(
                args.trial_root,
                args.account or [None] * len(args.trial_root),
                args.history_root or [None] * len(args.trial_root),
                strict=True,
            )
        ]
        bundle = TrialComparisonBuilder().build(inputs)
        TrialComparisonReportWriter().write(bundle, args.output)
    except (ValueError, OSError, KeyError, TypeError):
        print(
            json.dumps(
                dict(
                    report_written=False,
                    reason="input_schema_integrity_or_output_contract",
                )
            )
        )
        return 2
    print(
        json.dumps(dict(report_written=True, execution_invoked=False, formal_oos=False))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
