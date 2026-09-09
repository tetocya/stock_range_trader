# 段階5：Checkpoint・ProtocolJudge・登録候補（DRAFT）

本段階は人工データによる測定・判定・出力接続です。正式登録、実OOS開始、
市場API、Live取得、段階6、Benchmark、Brokerは実装・実行していません。
プロトコル本文は変更しません。付録の10%・5銘柄等は今回のユーザー指示を優先し、
未承認値として扱います。人工試験の値を運用承認に読み替えません。

## Interface

| 専用型／API | 責務 |
| --- | --- |
| `CheckpointSchedule` | JST月初B0、B1、B3と明示カレンダーの最終session |
| `FinalizationPolicy`, `FinalizationState`, `ExternalAbsence` | 明示予定利用時刻／公開policy hash、deadline、証拠状態と確定時刻 |
| `CheckpointEvidence` | 同一初期資金、評価・標本、Secondary、台帳参照を固定 |
| `LedgerCheckpointSource` | 一貫した`StoredRecords`読取と期待head、純粋reducerによる完全検証 |
| `collect_store_checkpoints` | 監査破損をINVALID証拠化。誤った期待headやDB競合は別途拒否 |
| `ProtocolJudge` | 型付き証拠と明示nowのみを受け取り、固定優先順位で判定 |
| `RegistrationInputs`, `VerificationEvidence`, `RegistrationCandidate` | 結果を入力にしない事前条件候補・証拠対応・未充足診断 |
| `write_registration_candidate`, `write_checkpoint_results` | 独立APIによる最小成果物の排他的・原子的公開 |

既存Phase 1〜3・段階1〜4のInterface、設定、価格契約、売買コードは変更していません。
Phase 3のFormal OOSフラグ／manifest schemaは流用しません。

## 測定規約

1Mは`[B0, B0+1暦月)`、3Mは`[B0, B0+3暦月)`です。休日でも境界を丸めません。
評価には各区間の明示カレンダー最終sessionの**全銘柄mark確定済み**Equityを使い、
欠損時に前sessionの評価を代用しません。境界以後のFill・完了episodeは除外します。

`R = (Equity - InitialCapital) / InitialCapital`。InitialCapitalは同じ口座の200,000円。
月次平均、Validation資金、独立symbol-fold中央値は使いません。
Equityはcash＋保有時価であり、注文予約・売却代金holdを再控除しません。
未決済を時価評価し、強制売却・仮想EXIT費用を加えません。配当別加算なし・税引前です。

約定ユニーク銘柄数は期間内filled注文の固定instrument ID集合です。
完了取引は段階3のflat→保有→flat episodeをEXIT日で一度だけ数えます。
未決済、rejected、canceledは完了取引数にしません。境界後に閉じるepisodeを
過去へ遡って数えません。entry／exit参照とnet_profitを照合し、同一ID異内容は拒否します。
標本証拠が未確定ならNoneであり、0取引と推測しません。

入力金額は既存の正規化有限Decimal文字列契約を使い、bool、NaN、Infinity、
無効分母は拒否します。Primary gateは元のEquityを190,000円／200,000円と直接比較します。
Returnは256桁のDecimal演算枠で計算し、表示百分率へ丸めません。
Secondaryの循環小数は64有効桁で出力します。この出力精度を合否へ使いません。

## 最終化

市場境界、expected session、actual session、market_at、observed_at、finalized_at、
予定利用可能時刻、deadline、算定根拠を分けます。Providerの実公開時刻は推測しません。
deadline・公開policyが未確定ならDraftを保持できますが、測定は正式最終化されません。
5session等の猶予や公開遅延日数に既定値はありません。

- 市場期間未完了または必要証拠待ちはpending。
- 期間完了後、必要証拠が揃えばdeadline前でもavailable。
- deadlineは`now >= deadline`。監査が妥当で、明示`ExternalAbsence`がある場合だけ外部欠測を確定。
- 単なる再生未完了を外部障害とは推測しない。期限後も証拠不明ならpendingのまま。
- 監査・会計破損や保有中未対応企業行動による停止はinvalid。外部欠測へ変換しない。
- 1M／3Mおよび評価／標本は独立最終化。1M gate失敗を売買停止commandへ変換しない。

`previous`で確定済み証拠を渡すと、変更された評価・標本・policyによる黙った改訂を拒否します。
両証拠が確定済みなら元のCheckpointを保持します。1M先行確定時のheadは、3M側で
検証済みの祖先headであることを確認します。同一口座ではない組合せは拒否します。
追記訂正イベントの実装は対象外です。確定後に遅れて届いた異なる証拠は上書きできません。

## 固定判定表

上から順に適用します。MDDやSecondaryは判定入力として参照しません。

| 優先 | 条件 | label／outcome |
| --- | --- | --- |
| 1 | invalid証拠あり | INVALID／N/A |
| 2 | 3M市場期間未完了 | PENDING／N/A |
| 3 | 1M gateが未確定 | PENDING／N/A |
| 4 | 1M外部欠測が確定 | INCONCLUSIVE |
| 5 | R1M < −0.05 | FAIL |
| 6 | 3M標本証拠が未確定／外部欠測確定 | PENDING／INCONCLUSIVE |
| 7 | 約定20銘柄未満または完了100取引未満 | INCONCLUSIVE |
| 8 | 3M評価が未確定／外部欠測確定 | PENDING／INCONCLUSIVE |
| 9 | R3M > 0／それ以外 | PASS／FAIL |

−5%ちょうどはgate通過。標本充足時の3M=0%はFAIL。
標本不足なら3Mが負でもINCONCLUSIVE。ただし1M失敗やinvalidの優先順位を維持します。
3M期間終了後に1M失敗が確定していれば、不要な3M評価／標本の取得待ちでFAILを遅らせません。
3M途中ならgate=failedを保存しても最終label=PENDINGです。

`lifecycle`、`validity`、`outcome`、`one_month_gate`を分離します。
`evidence_kind=synthetic`のPASSは実OOS成功ではありません。
成果物は常に`draft_not_registered`、`formal_registration_performed=false`です。

## Secondary

初期資金を最初のhigh-waterに含むMDD正幅、完了episodeのnet expectancy、勝率、
Profit Factor、完了／未決済数、月次Candidate・完了取引数を記録します。
評価欠損があれば完全期間MDDはnull、観測範囲のMDD・coverage・欠損sessionを併記します。
完了取引0、損失0等の未定義値はnull＋理由で、Infinityを保存しません。
含み損益を完了取引のexpectancyやProfit Factorに混ぜません。
既存凍結Riskルールは変更せず、Checkpoint MDDを新たな停止・合否条件にしません。

## 登録候補と証拠

`build_registration_candidate(inputs, generated_at=...)`は口座・損益・Checkpoint・Outcomeを
受け取りません。生成日時・出力先はcandidate IDから除外し、Universeと証拠参照を
正規化してSHA-256を再計算します。渡されたdigestを信用しません。

未確定値はNoneで保持し、`structural_unmet`へ列挙します。sourceのdirty／git_unavailable、
必要hash、将来期間・最終化policy、運用承認、PIT Universe、履歴参照不足を記録します。
検証証拠は専用schema、ID、payload hash、対象commit/tree、subject hashを照合します。
syntheticの成功だけではLive価格basis・単元・カレンダーを確認済みにしません。
実日足Open証拠と追加入力イベントは現実装capability=falseで、必ず未充足です。
証拠内容の真正性を外部署名やCI APIへ問い合わせる機能はなく、callerが保存した検証成果物の
内容整合性・対象一致の診断です。登録承認そのものではありません。

将来の未取得市場データhashは要求しません。登録timestampやapproval IDを生成しません。
固定条件が埋まっても登録APIはありません。絶対パス・secret系フィールドを出力境界で拒否し、
Raw市場データを結果へ保存しません。

## 台帳と成果物

`EventStore.read()`の一貫したsnapshotを固定し、期待headと全履歴を検証します。
保存済みcommandの純粋reducer復元だけを行い、Evaluator／Selector／ReplayEngineは呼びません。
口座は更新しません。既存DBの読取後の変更は固定`StoredRecords`へ流入しません。
別headの資料ではprovenance hashは変わり得ますが、境界外の売買は過去の測定値を変えません。

成果物は以下です。

- `registration_candidate.json`：事前条件のみ。結果と独立して生成可能。
- `checkpoints.csv`：1M／3Mの状態、評価、標本数。
- `protocol_result.json`：判定と完全なCheckpoint証拠、deadline、Secondary、参照。
- `checkpoint_artifacts.json`：ファイルhash・schema。自己hashは含めない。

候補ファイルは一時ファイルからexclusive hard-linkで公開します。
結果bundleは一時ディレクトリで検証後rename公開し、協調publisher間の排他lockを使います。
既存出力・symlinkは上書きせず、通常例外時は一時領域をcleanupします。
第三者が排他規約を無視して同時にファイルシステムを改変する状況や物理停電を保証しません。
プロセス強制終了でlockが残る場合の自動回収は行いません。

## 検証と制限

実段階4の人工3暦月・2銘柄fixtureから1M／3Mを算出し、標本不足のINCONCLUSIVEを確認します。
この人工fixtureの1MはEquity 227,859円・Return 0.139295・2銘柄／2完了、
3MはEquity 324,487.89円・Return 0.62243945・2銘柄／7完了です。
市場成績ではなく、口座接続と固定判定の試験値です。
PASS/FAIL境界は別の明示人工証拠で試験し、実台帳の取引数を改変して標本充足を演出しません。
登録候補・出力では再生・選択・口座更新を禁止するspy、改変・head不一致、公開失敗cleanupを検証します。

```bash
RUN_LIVE_JQUANTS_TESTS=0 RUN_LIVE_YFINANCE_TESTS=0 python -m pytest -q
ruff check .
ruff format --check .
git diff --check
```

Live・実データOOS・正式登録は未実施。既存のLive opt-in 6件はskipで、成功扱いにしません。
Benchmarkやプロトコルの全Secondary一覧、包括的な段階6 E2E、正式登録CLIは対象外です。
