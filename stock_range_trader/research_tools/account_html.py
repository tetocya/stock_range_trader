"""Self-contained, escaped account UI with local-only filters and exact sorting."""

import base64
import hashlib
import html
import json
from datetime import UTC, datetime

from delayed_replay.serialization import JsonObject, time_text

from .account_view import AccountViewBuilder
from .order_report import publish_artifacts, table_row, tool_identity

SCRIPT = r"""
'use strict';
const data = JSON.parse(document.getElementById('account-data').textContent);
function deepFreeze(v) { if (v && typeof v === 'object') {
  Object.values(v).forEach(deepFreeze); Object.freeze(v); } return v; }
deepFreeze(data);
// Decimal strings are sorted as scaled BigInts, not binary floats.
function compareDecimal(a, b) {
  function parts(v) { const s = String(v); const negative = s.startsWith('-');
    const p = (negative ? s.slice(1) : s).split('.');
    return [negative ? -1n : 1n, p[0], p[1] || '']; }
  const x=parts(a), y=parts(b), n=Math.max(x[2].length,y[2].length);
  const l=x[0]*BigInt(x[1]+x[2].padEnd(n,'0'));
  const r=y[0]*BigInt(y[1]+y[2].padEnd(n,'0'));
  return l<r ? -1 : l>r ? 1 : 0;
}
const tables = [
  ['positions',data.positions,[['symbol','銘柄'],['shares','株数',true],
   ['cost_basis','取得原価（費用込）',true],['entry_session','取得日'],
   ['valuation_price','評価価格',true],['valuation_session','評価日'],
   ['position_value','保有時価',true],['valuation_status','評価状態']]],
  ['orders',data.orders,[['sequence','Sequence',true],['symbol','銘柄'],
   ['side','売買'],['fixed_shares','固定株数',true],['decision_session','判断日'],
   ['target_session','対象session'],['fixed_reserved_amount','固定予約',true],
   ['saved_status','Status'],['saved_reason','保存理由'],
   ['charged_commission','徴収手数料',true]]],
  ['history',data.history,[['session','日付'],['cash','Cash',true],
   ['equity','Equity',true],['position_value','保有時価',true],
   ['reserved_total','当時の予約総額',true],['valuation_status','評価状態']]]
];
const sort = {};
function label(v) { return v === null || v === undefined ? '未評価／不明' :
  typeof v === 'object' ? JSON.stringify(v) : String(v); }
function filtered(rows,id) {
  const day=document.getElementById('date-filter').value;
  const symbol=document.getElementById('symbol-filter').value;
  const status=document.getElementById('status-filter').value;
  return rows.filter(r => (!day || (id==='history' ? r.session===day :
    id==='orders' ? r.decision_session===day || r.target_session===day :
    r.entry_session===day || r.valuation_session===day)) &&
    (!symbol || id==='history' || r.symbol===symbol) &&
    (!status || id!=='orders' || r.saved_status===status));
}
function render() {
  for (const [id,source,cols] of tables) {
    const table=document.getElementById(id); table.replaceChildren();
    const head=document.createElement('thead'), hr=document.createElement('tr');
    for (const [key,title,numeric] of cols) {
      const th=document.createElement('th'), button=document.createElement('button');
      button.type='button'; button.textContent=title; button.dataset.sort=key;
      button.addEventListener('click',()=>{sort[id]={key,numeric,
        direction:sort[id]?.key===key ? -sort[id].direction : 1};render();});
      if(sort[id]?.key===key) th.setAttribute('aria-sort',sort[id].direction===1?'ascending':'descending');
      th.append(button); hr.append(th);
    }
    if(id==='orders') { const th=document.createElement('th');th.textContent='① 独立監査';hr.append(th); }
    head.append(hr); table.append(head);
    const body=document.createElement('tbody'), rows=filtered(source,id).slice();
    if(sort[id]) { const {key,numeric,direction}=sort[id];
      rows.sort((a,b)=>{ const x=a[key],y=b[key];
        if(x===null || x===undefined) return y==null ? 0 : 1;
        if(y===null || y===undefined) return -1;
        return direction*(numeric?compareDecimal(x,y):String(x).localeCompare(String(y))); }); }
    for(const row of rows) {const tr=document.createElement('tr');
      for(const [key] of cols) {const td=document.createElement('td');
        td.textContent=label(row[key]); td.dataset.field=key;tr.append(td);}
      if(id==='orders') {const td=document.createElement('td'),detail=document.createElement('details');
        const summary=document.createElement('summary');summary.textContent='保存値と独立照合（再清算なし）';
        const pre=document.createElement('pre');pre.textContent=JSON.stringify(data.order_audit[row.order_id],null,2);
        detail.append(summary,pre);td.append(detail);tr.append(td);}
      body.append(tr);
    }
    table.append(body); document.getElementById(id+'-count').textContent=String(rows.length);
  }
}
for(const id of ['date-filter','symbol-filter','status-filter']) {
  document.getElementById(id).addEventListener('input',render);
  document.getElementById(id).addEventListener('change',render);
}
render();
"""


def _escaped(value):
    if value is None:
        return "未評価／不明"
    return html.escape(str(value), quote=True)


def _json_script(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def chart_segments(history, key):
    """Break at every explicit missing scheduled mark; never bridge a gap."""
    segments, current = [], []
    for index, row in enumerate(history):
        if row[key] is None:
            if current:
                segments.append(current)
                current = []
        else:
            current.append((index, row[key]))
    if current:
        segments.append(current)
    return segments


def _chart(history):
    series = [
        (key, color, chart_segments(history, key))
        for key, color in (
            ("equity", "#1d4ed8"),
            ("cash", "#15803d"),
            ("position_value", "#b45309"),
        )
    ]
    values = [float(v) for _, _, segments in series for seg in segments for _, v in seg]
    if not values:
        return '<p id="empty-chart">保存された評価はありません（補間なし）。</p>'
    low, high = min(values), max(values)
    span = high - low or 1
    result = [
        '<svg viewBox="0 0 700 210" role="img" aria-label="保存日次推移。欠測で線を切断">'
    ]
    result.append(
        f'<text x="0" y="15">{high:g}円</text><text x="0" y="205">{low:g}円</text>'
    )
    for key, color, segments in series:
        for seg in segments:
            points = [
                (
                    65 + index * 620 / max(len(history) - 1, 1),
                    185 - (float(v) - low) / span * 160,
                )
                for index, v in seg
            ]
            coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
            result.append(
                f'<polyline data-series="{key}" fill="none" stroke="{color}" points="{coords}"/>'
            )
            for x, y in points:
                result.append(
                    f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{color}"/>'
                )
    result.append(
        "</svg><p>青: Equity　緑: Cash　橙: 保有時価。横軸: 下表の日付順。欠測は線を接続しません。</p>"
    )
    return "".join(result)


def _html(data):
    script_hash = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
    out = [
        f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-{script_hash}'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>研究口座 — 保存状態の閲覧</title>
<style>body{{font:16px system-ui,sans-serif;margin:2rem;color:#17212b;background:#f5f7fa}}
section{{background:white;border:1px solid #ccd5df;border-radius:8px;padding:1rem;margin:1rem 0}}
dl{{display:grid;grid-template-columns:minmax(10rem,1fr) 3fr;gap:.5rem}}dd{{margin:0;overflow-wrap:anywhere}}
.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%}}td,th{{padding:.55rem;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}}
td,pre{{overflow-wrap:anywhere;white-space:pre-wrap;max-width:32rem}}button,input,select{{font:inherit;padding:.4rem}}
.warning{{background:#fff3cc;padding:1rem}}svg{{width:100%;max-width:900px}}label{{display:inline-block;margin:.3rem}}
</style></head><body><h1>研究口座の閲覧</h1>
<p class="warning">生成時点の保存状態です。研究用 simulated_fill であり、実約定ではありません。
取得・注文・取消・清算・再開・承認の操作はありません。hash一致は真正性の証明ではありません。</p>
<noscript>表の閲覧・操作にはローカルJavaScriptが必要です。正確な値は account_view_data.json でも確認できます。</noscript>"""
    ]

    def section(title, values):
        out.append("<section><h2>" + _escaped(title) + "</h2><dl>")
        for key, value in values.items():
            out.append("<dt>" + _escaped(key) + "</dt><dd>" + _escaped(value) + "</dd>")
        out.append("</dl></section>")

    metadata, account = data["metadata"], data["account"]
    section(
        "試験概要",
        {
            **metadata["scope"],
            "設定hash": metadata["settings_hash"],
            "出自": metadata["provenance"],
            "検証状態": data["validation"]["status"],
            "検証上の注意": data["validation"]["issues"],
            "不足証拠": data["missing_evidence"],
        },
    )
    section(
        "口座（保存確定値・円）",
        {
            "Cash": account["cash"],
            "Equity（現在評価）": account["equity"],
            "注文予約現金": account["order_reserved_cash"],
            "売却代金拘束": account["proceeds_hold"],
            "予約総額（拘束込）": account["reserved_total"],
            "利用可能Cash": account["available_cash"],
            "保有時価": account["position_value"],
            "評価完了": account["valuation_complete"],
            "最終評価日": account["last_valuation_session"],
            "最後に記録されたEquity（現在値とは限らない）": account[
                "saved_equity_last_recorded"
            ],
            "保有銘柄数": data["summary"]["position_count"],
            "注文数": data["summary"]["order_count"],
            "拒否数": data["summary"]["rejected_count"],
            "simulated_fill数": data["summary"]["filled_count"],
            "徴収手数料": data["summary"]["charged_commission"],
        },
    )
    section("再生状態", data["replay"])
    out.append(
        '<section><h2>表示フィルター</h2><p>現在残高・グラフは固定。フィルターは下の表だけに適用し、口座を再計算しません。日次表は口座全体です。</p><label>日付 <input id="date-filter" type="date"></label>'
    )
    for field, title, options in (
        (
            "symbol",
            "銘柄",
            sorted(
                {o["symbol"] for o in data["orders"]}
                | {p["symbol"] for p in data["positions"]}
            ),
        ),
        ("status", "注文status", sorted({o["saved_status"] for o in data["orders"]})),
    ):
        out.append(
            f'<label>{title} <select id="{field}-filter"><option value="">すべて</option>'
        )
        out.extend(
            '<option value="' + _escaped(v) + '">' + _escaped(v) + "</option>"
            for v in options
        )
        out.append("</select></label>")
    out.append("</section>")
    for key, title in (
        ("positions", "保有"),
        ("orders", "注文・①監査詳細"),
        ("history", "保存日次推移"),
    ):
        out.append(
            f'<section><h2>{title}（表示 <span id="{key}-count"></span> 件）</h2>'
        )
        if key == "history":
            out.append(_chart(data["history"]))
        out.append(f'<div class="scroll"><table id="{key}"></table></div></section>')
    section(
        "根拠",
        {
            "読取head": metadata["read_head"],
            "基準再生時刻": metadata["as_of_replayed_at"],
            "生成時刻": data["generated_at"],
            "証拠hash": data["evidence_hashes"],
            "検証範囲": metadata["validation"],
            "正式OOS": False,
        },
    )
    out.append(
        '<script id="account-data" type="application/json">'
        + _json_script(data)
        + "</script>"
    )
    out.append("<script>" + SCRIPT + "</script></body></html>")
    return "\n".join(out).encode()


class AccountHtmlWriter:
    def write(self, model, output, *, fault=None):
        data = AccountViewBuilder().build(model).to_dict()
        data["generated_at"] = time_text(datetime.now(UTC))
        # Same independent diagnostics as ①, inline and self-contained.
        data["order_audit"] = {
            r.to_dict()["order_id"]: table_row(r) for r in model.audit.records
        }
        artifacts = {
            "account_view_data.json": JsonObject.from_value(data).encoded.encode(),
            "account_view.html": _html(data),
        }
        manifest = dict(
            schema="account-view-report-manifest-v1",
            tool=tool_identity(),
            generated_at=data["generated_at"],
            input=data["metadata"],
            input_file_hashes=model.audit.files.hashes,
            validation=data["validation"],
            missing_evidence=data["missing_evidence"],
            artifacts={k: hashlib.sha256(v).hexdigest() for k, v in artifacts.items()},
            json_decimal_encoding="exact_decimal_strings",
            execution_invoked=False,
            formal_oos=False,
            authorization_changed=False,
        )
        artifacts["report_manifest.json"] = JsonObject.from_value(
            manifest
        ).encoded.encode()
        return publish_artifacts(model.audit, output, artifacts, fault=fault)
