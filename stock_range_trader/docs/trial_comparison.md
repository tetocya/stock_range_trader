# ④ 試験結果の比較レポート

読取専用・オフラインの研究用比較。①注文監査の独立Decimal算術、②口座Read Model、③までの証拠読取・原子的公開を再利用する。Strategy、清算、復旧、API、取得台帳の予算処理は呼び出さない。6月承認済みworktree・実装hashは変更しない。

## 実行

観測用worktreeのPythonプロジェクトから、明示した入力だけを読む。

```bash
python -m research_tools.compare_trials \
  --trial-root /path/to/saved-trial-a \
  --trial-root /path/to/saved-trial-b \
  --output outputs/trial-comparison-v1
```

既定の口座は各rootの `comparison/continuous.sqlite`。別口座を指定する場合は、rootと同じ順序で同数の相対パスを渡す。同じ試験の連続／分割を比較する例：

```bash
python -m research_tools.compare_trials \
  --trial-root /path/to/saved-trial --account comparison/continuous.sqlite \
  --trial-root /path/to/saved-trial --account comparison/split.sqlite \
  --output outputs/replica-comparison-v1
```

単一試験の読取レポートも可能。出力は入力と別のGit除外領域へ新規作成する。既存出力・symlink・WAL稼働中DBを拒否する。全入力rootの証拠を公開直前まで再検証し、途中変更は中断する。CLI異常時は固定メッセージとexit 2を返し、パスや機密値を表示しない。

### 別rootに保存された事前履歴

6月の清算口座は事前履歴を5月rootに保持する。保存済み口座の比較時は、Pythonでは `TrialComparisonInput.read(june_root, history_root=may_root)`、CLIでは `--history-root` で明示する。

```bash
python -m research_tools.compare_trials \
  --trial-root /path/to/saved-may --history-root - \
  --trial-root /path/to/saved-june --history-root /path/to/saved-may \
  --output outputs/trial-comparison-v2
```

`--history-root`を使う場合は試験rootと同じ順序で同数指定する。`-`または全省略は当該試験root。参照先は日付やファイルの存在ではなく、監査済みplanの `history_packets` / `packets` に宣言されたhashで決定する。両集合の重複・未宣言packetは拒否する。history packet本体・snapshot・raw価格証拠を指定rootで検証し、欠落・改変ならレポートを発行しない。試験root等への探索・フォールバック、原本のコピー・修復は行わない。run packetは常に試験rootで読み、履歴rootへ迂回させない。

履歴rootの読取証拠も同一EvidenceFilesで公開直前まで再検証し、出力先が履歴root内／親である場合も拒否する。Manifestの既存 `input_file_hashes` に外部履歴の読取hashを含め、絶対パスは成果物へ追加しない。参照rootは保存場所の指定であり、試験ID・複製グループを変更しない。未取得laneでは履歴の検証成功を主張せず、従来どおり結果はnull。

## Schemaと比較契約

- `TrialComparisonInput.read()`：既存 `AccountReadModel.read()` の内部observerから、同一SQLite読取transaction内で入力・口座・注文を採取する。追加hookは省略時の既存挙動・Schemaを変えない。
- `TrialComparabilityAssessment`：条件ごとにmatch／mismatch／unverified。全体は `conditions_match`、`conditions_mismatch`、`comparability_unverified`。既知の差と未知項目が併存する場合、全体はmismatch、equivalenceはunverifiedとして両方を残す。
- `TrialComparisonBuilder`：`trial-comparison-v1`。期間・plan hash・run IDで安定整列。生成日時を含まない決定的内容。
- `TrialComparisonReportWriter`：CSV・自己完結HTML・精度保持JSONと `trial-comparison-manifest-v1` を原子的に公開。ManifestはツールSHA/hash、入力plan/run ID・head・保存基準日時・provenance・検証状態・成果物hashを記録。Manifest自身をhashしない。

出力は `trial_comparison.csv`、`trial_comparison.html`、`trial_comparison.json`、`comparison_manifest.json`。HTMLは条件と証拠、条件差、保存結果、複製グループの順。表示値はescapeし、CSVの数式先頭文字は保護する。JSON金額はDecimal文字列、欠測はnull（HTMLはN/A、CSVは空欄）。率はDecimal精度128で計算し、正確な分子・分母も保存する。

条件は銘柄・期間・初期資金・単元・保有上限・Candidate・戦略・設定hash・費用・Slippage・buffer・丸め・価格基準・配当・企業行動規約・モデルID・実装hash・口座区分・provenance。dict内の個別項目まで差を列挙する。実装hash差や設定hash差だけで計算規則の差／同等性を断定しない。根拠不足はunverified。一致しても保存契約の一致であり、真正性、遷移の正当性、実市場同等性の証明ではない。

## 結果・欠測・複製

約定率 = filled / 終端注文数。終端集合は既存statusの `filled`、`rejected`、`cancelled`、`canceled`。`pending`・`waiting`を分母から除き別集計する。分母0はN/A、全終端注文が拒否なら0%。研究用simulated_fillを実約定とは呼ばない。

`as_of`値は読取head時点の保存件数・金額であり、未完了試験の最終成績ではない。未完了ではfinal Cash／Equity／期末保有をnullとする。評価欠測のEquityはnull。保存済み注文ゼロと未取得は区別する。清算planの口座が欠損・破損した場合はエラーであり、空口座に置換しない。

未取得laneは既存JunePlanの純粋検証に通る取得専用planだけ。清算plan・input_manifest・口座がないことを確認し、provenance=`unacquired`、結果・観測数はnull。planの親データprovenanceを6月実取得の証明にしない。部分取得の通信台帳を解釈して完了扱いにする機能はない。

入力数は保存行と保存run scheduleに基づく（取引日を推測しない）。全保存行のpacket・snapshot・raw価格整合を①と同じ検査で確認するが、lot・calendar・取得認証の全面再検査ではない。欠測日数はそのscheduleに対する未保存日数で、公式営業日完全性の保証ではない。保存されていないEntry条件成立数はN/A、Signalを再計算しない。完了取引数は保存episodesの件数、フィールド欠損ならN/A。徴収Commissionは保存Fill割当で、拒否注文の見積手数料を徴収額へ含めない。

同一run ID・同一内容の重複入力を除去し、同じIDの異内容は矛盾として拒否する。同じplan hash・同じ宣言入力packet群／sourceの別runは複製グループとする（連続／分割を含む）。部分受理の進捗は別の独立市場サンプルにしない。各runを行として確認できるが、合計件数・合計金額・独立サンプル数は出さない。保存済みcomparison.jsonはplan結合を検査して記録済みassertionとして表示し、現行DBペア同等性の再証明には使わない。

独立口座のEquity接続、月次リターンの複利合成、成績ランキング、設定推薦、正式OOS判定、許可・登録状態の昇格は実装しない。

## 検証の区別

複数試験比較は人工May／人工Juneの保存bundleで検証する。人工Fill・期末保有・入力待ち・評価欠測も実データ検証ではない。保存済み実データ由来Mayの読取確認と人工Juneを「2か月の実市場結果」と表示しない。実Juneの入力・清算は未取得／未検証のまま。
