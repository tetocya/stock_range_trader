# 段階7A：オフライン価格証拠・追加入力契約

基準SHA `380c6ae24376af436cba21c6a4e3238c8f9cce4d`、同一ブランチ。
確認日 **2026-09-10**、ローカル公式Python client **jquants-api-client 2.6.0**
（既存依存範囲`>=2.6,<2.7`）。公開仕様の閲覧のみ実施し、市場API／認証／Live通信はしていない。
20万円・単一口座・100株・Long Only・月次選択、未承認の費用／上限等は変更しない。

## 公式仕様とCapability matrix

資料は個人向けV2の一次資料。Proや古いV1のプラン条件を代用しない。

| 対象 | 一次資料・確認内容 | 実装／7Bの扱い |
| --- | --- | --- |
| Free期間・遅延 | [公式サイト](https://jpx-jquants.com/?lang=ja%2F)の公開情報は過去2年・12週間遅延。[契約別仕様](https://jpx-jquants.com/ja/spec/data-spec)は今回403で本文取得不能 | 厳密な最古／最新利用可能日や全Endpoint権限は未確認。7Bで契約と取得範囲を再確認。日数を足して公開日時を捏造しない |
| Rate limit | [公式制限](https://jpx-jquants.com/ja/spec/rate-limits)：Free 5回/分、429制限 | 既存13秒逐次取得・単一retry管理を維持。並列化・認証通信なし |
| 認証・pagination | [Quickstart](https://jpx-jquants.com/ja/spec/quickstart)、[ページング](https://jpx-jquants.com/ja/spec/pagination) | V2 `x-api-key`、応答のpagination_keyを次requestへ渡す既存実装。キーは引き続きJQUANTS_API_KEYのみ。timeout適用を変更しない |
| 日足始値・欠測 | [日足仕様](https://jpx-jquants.com/ja/spec/eq-bars-daily)：O/H/L/Cは調整前、無取引や全日停止時はNull | 専用adapterで報告値／欠測／不正を区別。Nullだけから停止理由を断定しない。UL/LLは停止フラグとして扱わない |
| 調整・企業行動 | [調整計算](https://jpx-jquants.com/ja/spec/eq-bars-daily/adj)：分割・併合・ライツ、配当等は対象外 | AdjFactorとAdj系列を保持。AdjFactor≠1／ExRTありはunsupported。既存株数分割適用禁止・配当別加算なしを維持 |
| 単元・有効日 | [master仕様](https://jpx-jquants.com/ja/spec/eq-master)：Dateは適用日。単元の項目は掲載なし。休日指定は翌営業日の情報を返す | masterだけから100株を推測しない。適用日不一致は拒否。lot_from_masterは未確認を返す。別の根拠資料と有効期間の明示reviewが必要 |
| カレンダー | [calendar仕様](https://jpx-jquants.com/ja/spec/mkt-cal)、[休日区分](https://jpx-jquants.com/ja/spec/mkt-cal/holiday-division) | Free権限の独立確認は保留。全日付coverageと1/2=東証sessionを照合、3=OSE祝日取引を東証営業日へ流用しない。時刻は独立の明示証拠を要求 |

既存Providerの`get_trading_calendar`はAPIエラーを平日推測で代替しない。
ローカルの明示CalendarEvidenceを入力可能だが、出典・対象hash・review参照・期間の充足が必要。
ReferenceReviewは保存された外部review参照との**構造的対応**の契約であり、署名検証や資料の真正性確認を自動化しない。
7Aのテスト用review hashは人工証拠で、運用・Liveの確認済み証拠ではない。

## Provider報告値と約定の分離

`PriceEvidenceKind`は`provider_daily_open`／`auction_execution_evidence`／`simulated_fill`を区別する。
`DailyOpenObservation`は前者のみ。`trade_at=None`を保持し、`require_execution()`は常に
`UnsupportedDailyExecution`。既存`ExecutionOpenEvidence`へ変換する関数は追加しない。
後者の実オークション証拠は未取得。追加再生層はsynthetic入力の`simulated_fill`だけで、
`research_only`／`unapproved`／`draft_not_registered`を台帳のCapabilityへ固定する。

日足始値を9時の寄付き約定と同一視しない。特別気配等で初回約定が日中になる可能性を
日足から判別できず、注文の参加条件・成立・時刻の保証はない。
出来高0は事後的な不成立情報であってOpen時点の情報ではない。出来高正も注文の成立保証ではない。
`daily_open_proxy`は設計候補に留め、実装・採用しない。採用には対象オークション、初回約定遅延、
不成立・停止、処理順、Slippage等のモデル定義・承認と、旧結果との別管理が必要。

`adapt_daily_response`は指定Provider／basis／V2 schema／symbol／dateを照合し、
`response_payload()`で正規化した応答のSHA-256へ値を結び付ける。このhashはHTTP生bytesではない。
数値は有限・正規化文字列へ変換し、bool・NaN・Infinity・異常型を拒否する。
欠測はNoneで保持。既知の不正価格は他の列が欠測でもinvalidにする。
無取引と停止の原因を日足だけで識別できない場合はunknownのまま。
取得／初観測時刻と市場の約定時刻を混同しない。

## InputVersion／InputExtensionEvent

新規Opt-in API：`ExtensibleReplayEngine.create/resume/accept/advance/run`。
既存ReplayEngine、Phase 1～3、段階1～6の公開Interface／Schema／結果は変更しない。
旧段階4ストリームへの暗黙migrationは行わず、専用`input-replay-7a-1`ストリームを使う。
そのストリーム内では追加のたびに口座を作らず、同じSQLiteの口座・cursorを継続する。

| 型／状態 | 記録内容 |
| --- | --- |
| InputPacket | immutable PriceSnapshot／synthetic OpenSnapshotを正規化保存。既存hashを復元検証 |
| InputExtensionEvent | extension_id、parent_version、packet_hash、accepted_at |
| 台帳descriptor | packet hash、各symbol/date/laneの意味hash、snapshot参照、各first_observed_at／fetched_at |
| InputVersion | schema、parent、packet、status、accepted_at、対象行の明示集合、snapshot参照。全payloadのSHA-256がversion ID |
| input head | 最新の受理・隔離履歴version。実際に採用する行のowner indexとは別 |
| base_identity_hash | 基礎run・設定・Catalog・Universe・モデル・初期入力を固定。追加時も不変 |

対象範囲は連続区間へ丸めず、行キー`price|symbol|date`／`open|symbol|date`の明示集合として保持する。
accepted／refetched／quarantined_revision／quarantined_pastを区別し、隔離・再取得の履歴も保存する。
同一行は最初のownerを維持し、fetched_atだけが異なる再取得を自動採用しない。
同値＋不足行補充が混在しても重複行を公開せず、元のsnapshot hashを保持する。
改訂値が一つでもあれば**バッチ全体**を隔離。未知Provider／basis／銘柄／calendar外日は全体拒否。
既存選択の参照期間や完了済み判断日への過去補充も全体隔離。未来・未処理の必要行だけを追加可能。
保有中企業行動は銘柄削除で済ませず、既存のstop-preserve-positions経路を通す。

同じextension IDと同じ内容の再送は追加eventなし。ID異内容、古いparentでの新規追加は拒否。
phase計算は同じread snapshotのinput headとDB headへ結び付け、並行受理が入ればCAS競合で拒否する。
追加受理は当該市場cursorの直近時刻へ記録し、市場時計を過去へ戻さない。
`fetched_at <= accepted_at <= replayed_at`を守るが、`fetched_at <= market_time`は要求しない。
公開viewは既存market_time境界を通すため、受理だけで未公開Closeや事後出来高をOpenへ渡さない。

## ファイルとDBの障害境界

`InputArtifactStore.publish`は一時ファイルwrite/fsync→exclusive hard-link公開→directory fsync→hash再読込。
その後`accept`がhash検証済み参照をDB transactionへ記録する。両者が同一transactionとは主張しない。
DB確定前の孤立ファイルは未受理として無視し、無関係なファイルのcleanupは行わない。
DB確定後は採用・再取得・隔離を含む参照ファイルの欠落／破損で復旧を拒否する。
パケット内の元snapshot digest、台帳descriptor、受理日時も照合する。
一度読んだ不変オブジェクトでphase計算する。検証後の外部削除まで同一transactionで防ぐ仕組みではなく、
後の復旧時にfail-closedで検出する。物理電源断・第三者によるファイル改ざんの防止自体は保証しない。

新層はStage4のprotected `_compute/_decide`と純粋ReplayReducerを再利用する。
内側の計算で受理済み参照を展開し、保存された基礎identityは変更しない。
この依存点は将来のStage4変更時に互換性再検証が必要。公式clientのprivate transport依存は既存どおりで変更なし。

## 1M証拠と既存Stage5の境界

新台帳では明示最終sessionのfinish時、1M／3Mの測定receiptを同じtransactionで凍結する。
Equity・標本・input head・event IDを保存し、後の入力追加で書き換えない。
この`input-checkpoint-measurement-7a-1`は測定receiptであり、Stage5のdeadline／最終判定証拠に偽装しない。
既存`LedgerCheckpointSource`へ新台帳を無理に渡すadapterは今回追加しない。
既存Stage6の旧台帳→Checkpoint→Judge→Report経路は全回帰で維持する。
新台帳から正式Checkpoint／登録へ接続するにはlineage・最終化の明示adapterが必要で、未実装として残す。

## 7B準備状況・開始条件

| 分類 | 状態 |
| --- | --- |
| 取得検証可能 | 公開V2仕様に沿うオフライン日足adapter、price/basis/hash、NULL・異常値を検証可能。実取得は別途明示許可が必要 |
| Executable接続可能 | **実データは不可**。synthetic追加→同じ口座/cursorで再開は可能。日足Openの実約定Gateはunsupported |
| モデル承認待ち | daily_open_proxy未採用。寄付き参加・時刻不明・停止／無取引・処理順等の規約承認が必要 |
| 権限／証拠不足 | Freeの厳密な履歴境界・calendar権限、銘柄別有効単元、日足のLive整合性、出典review、価格basis検証 |
| 将来接続 | 新台帳→Stage5最終化／Reportのlineage adapter、旧ストリームmigrationは未対応 |

7A成功はオフライン契約と正しいunsupported検出の成果であり、7Bの自動実行・実OOS開始資格ではない。
正式登録候補の既存未充足フラグを緩めない。既存候補は旧実行経路のcapabilityを表すため変更せず、
新経路の限定capabilityは専用台帳と本書へ記録する。

## 検証

人工response fixtureのみ使用。段階6のnetwork guardをすべての新規テストへ適用し、
Provider取得入口・HTTP・socket通信を禁止。全transaction障害の別名複製はせず、
新しいファイル公開前後／DB確定前後、再送、親競合、同値再取得、改訂・過去補充隔離を検証する。
実Evaluator／Selector／Signal／共有口座を通し、待機解除後の完了7取引、期末Equity 324,487.89円を確認。
これは人工fixtureの値で市場成績ではない。入力追加前の口座event prefix・SelectionEpoch・1M receiptを保持する。

新層のレビューで、入力viewとCAS headを別読取する競合余地を補正し、割込み受理の回帰試験を追加した。
既存Productionファイル・Schemaは変更しない。新規モジュール4件・テスト2件・文書1件。

```bash
RUN_LIVE_JQUANTS_TESTS=0 RUN_LIVE_YFINANCE_TESTS=0 python -m pytest -q
ruff check .
ruff format --check .
git diff --check
```

Live 6件は未検証skipを維持。市場API通信、正式登録、OOS開始、PR／merge、7B実行、Node.js保守は行わない。
