# ② 研究口座の閲覧画面

生成時点の保存状態を自己完結HTMLで閲覧する。①のコミット
`63c85192a5f6b01595319f3e8c6371e251fd66be` を起点とする。
API取得、Signal、売買判断、清算、復旧、取消、承認、登録、正式OOSを呼ばない。
③Preflight・④比較機能、サーバー常駐・ログインは含まない。

## 実行

```bash
python -m research_tools.account_view \
  --trial-root INPUT_TRIAL_ROOT \
  --output outputs/account-view-UNUSED_NAME
```

DB既定値は `comparison/continuous.sqlite`。保存済み別口座は
`--account comparison/split.sqlite` のように相対パスで指定する。
同じ出力先は上書きしない。

今回の5月保存物を新worktreeから閲覧する具体例：

```bash
cd /Users/harimatakeuchi/stock_range_trader_observability/stock_range_trader
/Users/harimatakeuchi/stock_range_trader/stock_range_trader/.venv/bin/python \
  -m research_tools.account_view \
  --trial-root /Users/harimatakeuchi/stock_range_trader/stock_range_trader/.delayed_replay/selected_trial/owner-approved-fixed-baseline-v1 \
  --output outputs/account-view-may-v1
```

追加ライブラリ・環境再インストールは不要。
`account_view.html` を現行のBigInt対応ブラウザで直接開く。外部CDNなし。
JavaScript無効時も残高概要・グラフは表示するが、表の閲覧・操作はJavaScriptを要する。
正確な機械用データは `account_view_data.json` に保存する。

## 再利用とSchema

- `AccountReadModel`：①の `OrderAuditReader` の内部projection hookで、**同じSQLite読取transaction**
  の確定状態と同じ証拠集合を使用。別のDB読取で時点を混ぜない。
- `AccountViewBuilder`：保存状態と会計恒等式から表示用データを構成。Reducer再生なし。
- `AccountHtmlWriter`：①の `table_row` を用いて注文監査をインライン表示。
  独立算術を再実装せず、保存残高と監査診断を別に表示する。
- ①の出力末尾を `publish_artifacts` として共通化。出力契約・排他lock・0600ファイル／
  0700ディレクトリ・入力再検証・原子的公開を両機能で共有する。

新設Schemaは `account-read-v1`（内部投影）、`account-view-v1`（JSON）、
`account-view-report-manifest-v1`（manifest）。①のSchemaと既定CLIは変更しない。
ManifestにはツールSHA/hash、入力ID/head、基準再生時刻、生成時刻、検証状態、成果物hashを記録。
APIキー・環境変数一覧・不要なローカルパスは出力しない。

## 口座値・評価の意味

現行Proxy会計の `reserved(state)` は **注文予約Cashの合計＋売却代金拘束**。
画面はこの2項目と総額を別表示し、利用可能Cashを
`Cash − 注文予約Cash − 売却代金拘束` としてDecimalで算出する。
保存Equityから予約を差し引かない。保有時価は保存Equity−Cashおよび保存Close×株数で照合。
原本・保存残高の修正はしない。

現在評価が未完了なら現在Equity・保有時価・保有の評価価格をnullとし、
最後に記録されたEquity、最終評価日、入力待ち理由を別表示する。
評価完了フラグがあっても、保有評価の価格証拠が不足すれば現在評価は未検証とする。
現在価格・過去価格による補完、期末強制決済は行わない。
数量・会計値・Schema・hashが破損した入力は拒否。監査の不一致・証拠不足は注意状態を明示する。

日次表は保存済み `marks` のCash／Equityを表示し、保有時価は差額と明示する。
日次予約総額は当時の保存値で、拘束解放後の現在値とは別物。
calendar由来の保存session一覧から既に到達した範囲だけを使い、markのない日はnullにする。
欠測箇所でSVGの線を切断する。休日を補間したり未到達の将来日に値を描いたりしない。
グラフ座標のみ表示用浮動小数点、金額・JSONはDecimal文字列。
過去日の保有構成や会計状態遷移を再生成して正しさを証明するツールではない。

## 操作と安全性

- 日付は注文の判断日／対象日、保有の取得日／評価日、日次表の当日に一致する行を表示。
- 銘柄は保有・注文、statusは注文だけに適用。口座全体の日次表を銘柄別へ再計算しない。
- フィルターは表だけに適用し、現在残高とグラフを変えない。
- 列ボタンで昇順／降順。金額文字列はBigIntで桁を揃えて比較し、二進floatで並べない。
  nullは常に末尾。基礎データは再帰的にfreezeし、表示側配列のみ並べ替える。
- 注文詳細を展開すると①の保存値・独立算術診断・証拠hashが見える。
  外部HTMLへの依存リンクではなく、同じ読取headの監査情報を内包する。
- HTML escape、DOMのtextContent、埋込JSONのscript終端対策、CSPのscript hashで安全化。
  fetch／外部script／CDN／状態変更ボタンなし。

①の読取専用契約を維持する。WAL・journal付きDBは接続前に拒否。
停止・整合済みDELETE-journal保存物を要求し、閲覧のためにcheckpoint・repair・migrationしない。
読取から公開直前まで外部証拠の途中変更を検出する。hashは真正性の証明ではない。

## 検証と保護対象

保存済み5月の期待値はCash／Equity各200000円、保有0、注文1、拒否1、徴収手数料0円。
非空Fill・保有・入力待ち・評価欠測は人工の保存ledger fixtureで検証し、実データ検証と区別する。
実データの非空Fill、6月口座の閲覧は未検証。6月はDB未作成なら明示的に拒否する。

```bash
python -m pytest -q tests/test_account_view.py tests/test_order_audit.py
python -m pytest -q
ruff check .
ruff format --check .
git diff --check
```

ブラウザの実行ファイルを明示すると、人工6状態のheadless操作検証も実行する。
指定がない環境ではbrowserテストはskipし、成功とは扱わない。

```bash
ACCOUNT_VIEW_TEST_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  python -m pytest -q tests/test_account_view.py -k local_browser
```

使い捨てprofile、外部ホスト解決無効、background networking無効でローカルHTMLを開く。
表の描画、日付／銘柄／status、詳細、正確な数値順、欠測、基礎データ不変、
ページの外部resource読込0件を確認する。

承認済みworktreeは `034f5ece86345f111d04f36d938bfee18faf24de` のまま維持。
設定・既存hash計算範囲・実装・台帳・許可は変更しない。
閲覧ツールのhashは追加2モジュールも含めて①と別に更新するが、6月実行承認には転用しない。
ローカルcommitのみ。push・PR・mergeなし。②の完了報告で停止する。
