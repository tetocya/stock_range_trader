# ① 保存注文・拒否理由の監査

`research_tools.order_audit` は保存済み研究口座を読むだけのツール。
Strategy、Signal、選択、清算、Reducer再生、取得、許可・登録・OOS処理は呼ばない。
②口座閲覧画面・③Preflight一覧・④試験比較は本変更に含めない。

## 実行方法

通常のプロジェクト環境では次を実行する。追加ライブラリは不要。
`--trial-root` は価格証拠・入力packetと `trial_plan.json` または
`clearing_plan.json` を含む保存済み試験ルートを指定する。

```bash
python -m research_tools.order_audit \
  --trial-root INPUT_TRIAL_ROOT \
  --output outputs/order-audit-UNUSED_NAME
```

DBの既定値は試験ルート内の `comparison/continuous.sqlite`。
別DBは `--account comparison/split.sqlite` または `--account research_split.sqlite`
のように明示する。既存成果物を上書きしないため、出力は未使用のディレクトリを指定する。
6月は清算未実行でDB・清算planがない間はエラーとなる。空口座のレポートにはしない。

今回の別worktreeから5月保存物を読む具体例（再清算ではない）：

```bash
cd /Users/harimatakeuchi/stock_range_trader_observability/stock_range_trader
/Users/harimatakeuchi/stock_range_trader/stock_range_trader/.venv/bin/python \
  -m research_tools.order_audit \
  --trial-root /Users/harimatakeuchi/stock_range_trader/stock_range_trader/.delayed_replay/selected_trial/owner-approved-fixed-baseline-v1 \
  --output outputs/order-audit-may-v1
```

既存Pythonを使う場合もcwdは閲覧ツール側とする。pytestには既存の `pythonpath=["."]`
を使う。既存環境への再インストール・既存editable installの付替えは行わない。
配布用には `pyproject.toml` のpackage discoveryへ `research_tools*` だけを追加した。

## 成果物・Schema

| 成果物 | 契約 |
|---|---|
| `order_audit.json` | `order-audit-v1`。注文の正確な値と独立照合結果。金額はDecimal文字列、未知値はnull |
| `order_audit.csv` | 注文1件1行。数値sequence順。不足値は空欄。数式候補の文字列にアポストロフィを付加 |
| `order_audit.html` | 外部通信・JavaScriptなし。全値をescapeし、研究用simulated_fillと明示 |
| `report_manifest.json` | `order-audit-report-manifest-v1`。ツールversion/SHA/hash、入力ID、読取head、基準日時、検証状態、各成果物hash |

読取metadataのSchemaは `order-audit-read-v1`。
CSVで安全化した文字列の正確な値はJSONに保持する。`sequence` は数値として並べ、
不明の場合はnullで末尾に置く。CSVの負の金額は数値表現のまま保持する。
Manifest自身のhashをManifest内へ入れない。ローカル絶対パス、APIキー、許可ファイル、
環境変数一覧は出力しない。成果物だけから登録・許可・OOS状態を昇格させない。
CLI成功はレポート作成成功であり、注文や証拠の全項目が検証済みという意味ではない。
`verification_counts` と `account_cash_reconciliation` も確認する。

## 設計と再利用範囲

- `OrderAuditReader`：固定されたplan、SQLite記録、注文に関係する価格証拠を読む。
- `OrderAuditRecord`：`JsonObject` による不変レコード。書き戻しAPIなし。
- `OrderArithmeticVerifier`：`order-audit-daily-open-proxy-v1` の独立Decimal照合。
- `OrderAuditReportWriter`：出力分離、CSV/HTML安全化、bundleの一時生成・原子的公開。
- `EvidenceFiles` / `read_account`：将来機能でも再利用できる最小限の読取部品。

既存の `EventStore._load()`、`StreamIdentity`、`verify_chain`、`InputPacket`、
`FrozenProxyOrder`、`JsonObject` の構造・hash検証を再利用する。
`EventStore._load()` はprivate Interfaceなので、既存側が変わる際には読取互換テストを
再実行する必要がある。`EventStore.resume/recover`、`recover_records`、清算・sizing・
費用計算関数は呼ばない。保存されたイベント列・状態hashの構造検証と、
状態遷移を再生して確かめる検証は別物。後者は意図して行わない。

根拠イベントIDは注文の判断日/対象sessionとphaseを使って関連付ける。
イベントpayloadから注文を再生成して同一性を証明するものではなく、
`event_link_basis=session_phase_association_not_order_replay` と記録する。
市場判断時計は保存されたモデル上の時計であり、実際の取引所約定時刻ではない。

## 読取専用性・一貫性

対象は単一stream・既知Schemaの、安定したDELETE-journalモードの保存口座。
DBは `mode=ro`、`query_only=ON`、SQL authorizerによる書込拒否、1つの読取transactionで開く。
そのtransaction内で価格証拠を検証し、終了前後と公開直前に原本hashを再確認する。
外部ファイルの途中変更、欠けていた証拠の途中出現も中断条件となる。
未知Schema、壊れたhead/event/state/packet/sourceは成功・ゼロ件へ変換しない。
価格の外部証拠だけが欠ける場合は保存値を表示できても `insufficient_evidence` とする。

WAL形式・`-wal/-shm/-journal` があるDBは、接続前に
`stopped_consistent_non_wal_snapshot_required` として拒否する。
ツールはDBを単独コピーせず、`immutable=1`、migration、repair、checkpointを使わない。
書込主体を停止し、整合済みの保存物を用意してから使う。読取中にjournal modeを変更しない。
外部書込による正当な更新も一貫性を失うため中断するが、ツールの書込みとは判定しない。

出力は入力ルート内/親ディレクトリを拒否し、Git worktree内ではignore対象を必須とする。
Git外の私有出力先も可。symlink、既存成果物を拒否し、同じ出力名の協調publisherは
排他lockで直列化する。隣接一時ディレクトリを使い、全成果物検証後にrenameで公開する。
生成ファイルは0600、bundleディレクトリは0700。管理者による同時のpath/lock操作は対象外。

## 算術・会計の意味

BUYの予約は、保存参照Close×(1+Slippage)×(1+buffer)を価格単位へ丸め、
売買代金・仮定Commission・予約総額を保存された丸め規則で計算する。
必要額は保存された対象Openと固定数量から別に計算する。
`required_minus_reserved` と `budget_minus_required` を分け、予約のみ/予算のみ/
両方超過/両方以内を診断する。同額は制約内、0.01円の超過は制約外。
通常の既存BUY契約は予約額≤固定予算なので、「予算のみ超過」は通常生成されないが、
独立算術層では4区分を検証する。不正な凍結注文は読取時の既存契約でも拒否する。

SELLへBUY予約式を適用しない。固定株数、現時点の株数予約、
v1の全数量予約契約に基づく当初株数、売却費用、純売却代金拘束を分ける。
拘束解放を再実行しない。未知モデル/追加の未対応費用項目は
`unsupported_verification_model` とする。

保存されたstatus/reasonを変更しない。拒否注文の仮定Commissionと徴収済みCommissionを
分ける。終端注文で清算記録が欠ける場合は徴収額/Cash変動をnullとし、0円を仮定しない。
注文ごとのCash変動は保存されたFillの配賦値であり、中間状態を再生した値ではない。
初期Cash＋保存Fillの配賦総額＝最終Cashを別途照合する。
この照合だけでは各イベントの会計状態遷移の正しさまでは証明しない。

5月例：参照398円・100株、予約単価402.39円、予約代金40239円、予約Commission40.24円、
予約総額40279.24円。対象Open406円から単価406.41円、仮定Commission40.65円、
必要額40681.65円。予約超過402.41円、固定予算50000円の余裕9318.35円。
保存reason `frozen_reservation_or_budget_exceeded` を保ち、徴収Commission/Cash変動は0円。
5月の保存例は拒否1件・Fillなしなので、実保存データの非空Fill会計は未検証。

## 承認済み6月コードの保護

承認済みworktree `/Users/harimatakeuchi/stock_range_trader` は
`034f5ece86345f111d04f36d938bfee18faf24de` のまま保持する。
実装は別worktree・`codex/research-observability` ブランチのみ。
許可・原本・台帳は変更しない。API取得・実データ清算・PR・mergeを行わない。

既存hash関数は列挙されたcoreディレクトリとCLIを走査する方式であり、今回その範囲を
変更・縮小していない。新設 `research_tools` は既存列挙外だが、ツール自身のhashでは
全research_toolsソース、pyproject、共有coreソースhashを含めて別に固定する。
既存6月実装hashの維持はツールを実行用承認へ追加したことを意味しない。

## ローカル検証

```bash
python -m pytest -q tests/test_order_audit.py
python -m pytest -q
ruff check .
ruff format --check .
git diff --check
```

CI向けfixtureは人工の保存レコードのみ。接続実体のreadonly/transaction/書込拒否、
原本不変、破損・欠測・未知モデル、4制約区分、SELL、0.01円境界、保存判定不一致、
CSV数式対策・HTML escape、sequence 0..11、上書き拒否・公開前変更検出を検証する。
API・清算・Signal・状態復旧入口を禁止するテストも含む。Live skipは未検証のまま。
