# 保存済み実データの限定Proxy試験：準備完了、清算未承認

基準SHA `85821db6a99200c45b003b79c7cd01cd53b6239d`、同じ
`codex/phase3-walk-forward-validation` ブランチ。追加市場API通信・実データ清算・
正式登録・OOS開始・PR作成・mergeは行っていない。

レビュー用の具体的な設定は [plan JSON](../config/limited_proxy_trial_plan.json)。
canonical JSON（キー整列・compact・数値型保持）のSHA-256：
`13909ceb4213b342d7cc5022b8d5bcfb5dca87850ffc718985fffce925d234f2`。
これは**承認要求対象のドラフト**であって承認記録ではない。未確定証拠や履歴を追加すればhashが変わる。
model hashは共通戦略・指標・算術・台帳・adapter・比較CLIのソースを含み、コード変更でも旧承認を拒否する。
今回、承認ファイルは生成していない。

## 設定表

「既存承認」は今回の指示書と、20万円／単一口座／100株を確定した会話の範囲だけを指す。
既存コードの既定値や前回人工テストの値は、運用承認の参照にはしない。

| 項目 | 現行対応 | 提案値／未確定 | 理由 | 既存承認の参照 | 今回の採用状態 | 不足・不適合時 |
| --- | --- | --- | --- | --- | --- | --- |
| モデル・用途 | 独立Proxy | daily_open_proxy_v1、research_only、simulated_fill | 実約定と区別 | 指示書§3は対象指定 | 準備のみ、清算未承認 | plan限定許可なしでは拒否 |
| 公開順序 | select→resolve→mark→decide→finish | session_phase_not_actual_publication_v1 | 前日固定と当日Close後判断を分離 | 前回4補正 | 仮定をplan固定、実データ使用は未承認 | hash変更で拒否 |
| 実時刻条件 | 条件なしのみ | none、actual_trade_at=null | 個別の寄付き・約定時刻を証明しない | Proxy設計の範囲 | 限定planの承認待ち | 新しい時刻条件はunsupported |
| 約定仮定 | 全量／全量拒否 | Open＋Slippageで全量清算可能と仮定 | Queue・市場影響・部分約定は非再現 | モデル採用は未承認 | 未承認仮定として固定 | 既存Gateへ昇格しない |
| 口座・単元 | 共有口座、Long Only | 200000円、100株、S株なし | 固定条件 | 会話・指示書§3 | 固定条件、試験開始は未承認 | 単元証拠不足でblocked |
| 投資上限・保有数 | 明示必須 | 10%、最大1銘柄 | 保守的な比較案、今回1銘柄限定 | なし | 提案のみ、自動採用なし | Noneはblocked、購入不能は別診断 |
| 追加買い・EXIT | 追加買いなし、全株EXIT | single_position_full_exit | 数量と既存Position設定を保持 | 既存モデルの対応範囲 | 限定planの承認待ち | 未対応方式はunsupported |
| Slippage | 比率型 | 0.001 | 既存strategy設定を参照した案、成績未使用 | なし | 提案のみ | 省略・非有限値は拒否 |
| 手数料 | proportionalのみ | 0、最低手数料なし | 既存設定を参照した簡易案、実費の証明ではない | なし | 提案のみ、0を省略時既定値にしない | 最低手数料方式はunsupported |
| 予約buffer | 費用とは別の余裕 | 0.01 | 1%余裕の例、ギャップ成立を保証しない | なし | 提案のみ | 予約・予算超過は全量拒否 |
| 価格丸め | quantum指定 | 0.01円、BUY切上げ・SELL切下げ | 研究算術の粒度。取引所tickの証拠ではない | なし | 提案のみ | 未指定はblocked |
| 金額丸め | quantum指定 | 0.01円、half_even、費用・予約切上げ、予算切下げ | 算術を固定し再現可能にする | なし | 提案のみ | 未対応丸めは拒否 |
| 注文順位 | SELL→Score降順→instrument順 | 既存順序 | 当日価格で順位を変更しない | 対応モデルのみ、運用承認ではない | 限定planの承認待ち | 未対応順序はunsupported |
| 売却代金 | 同batch再利用禁止 | mark後の翌判断で解放 | 売却予定額からBUYを補填しない | 対応モデルの条件 | 限定planの承認待ち | 他注文の予約を使用しない |
| Volume | 清算前に検査 | 0はno_trade、方向相殺なしの合計超過は銘柄batch拒否 | 事後品質検査であり寄付き流動性の証明ではない | 前回4補正 | 対応方式のみ | 縮小・再選別・Fill取消しなし |
| 欠測・失効 | 待機のみ | wait_without_expiry、最終化未実装 | 時間経過で勝手に失効しない | 対応方式のみ | 明示提案 | 期限付き最終化はunsupported |
| 停止 | unknownを維持 | 全日停止矛盾はfailed、一時停止は時刻条件なしで許容 | 停止情報の網羅性は未確認 | なし | 研究仮定の承認待ち | 不明を「停止なし」にしない |
| 企業行動・配当 | 復元なし、配当除外 | 非単位係数／分割／ExRTはunsupported | 不正確な株数調整を避ける | 既存価格規約 | 範囲固定、追加取扱い未承認 | 状態保持・清算拒否 |
| 戦略 | 既存SignalAdapter | 現行strategyを複製し資金のみ固定条件へ合わせる | 指標短縮・成績による閾値変更をしない | 戦略維持の指示 | 設定案。既存configは無変更 | 履歴不足はinsufficient_history |
| Candidate供給 | 今回は事前指定方式のみ | baselineを固定する限定接続案 | 性能比較・選択口座・新Selectorを使わない | この案は未承認 | predeclared_single_candidate案 | 月次選択モードは現入口ではunsupported |
| 月次lookback | 既存MonthlySelectionは別途存在 | lookback・warmup・最低session数はNone | 未承認運用値を暗黙補完しない | 月次再選択方針を今回変更しない | 本案では不使用。月次接続は別判断 | 既存運用方式の代替とは呼ばない |
| 入力範囲 | 7B固定capture | 72030、[2026-05-01, 2026-06-01)、18行を9＋9 | 保存物と照合できる最小範囲 | 指示書§3 | 入力検査のみ実施 | 新銘柄・新期間・入力変更は旧許可拒否 |
| Risk・終了 | 新DD停止なし | 期末時価評価、強制決済なし | 既存結果を維持 | 既存Proxy範囲 | 限定planの承認待ち | 正式Judge／Checkpointへ接続しない |

この表の数値を一括採用していない。コードの`propose`は明示的にドラフトを作るコマンドであり、
実行入口が省略値を補う機能ではない。今回のfixed Candidate案は月次再選択の運用承認を置換しない。
月次Validationを必要とする場合は、供給方式・参照期間・必要履歴を別途確定して新しいplanにする。

## 保存済み証拠の再検証

ローカルの既存7B reportから対象runを解決し、content-addressed captureを再読込した。
下記hashはplanに全文を収録。元ファイルのbytes・取得時刻を変更していない。
日付・件数は報告値の転記ではなく、capture・snapshot・カレンダー間の照合結果。

| 証拠／出典 | instrument・有効範囲 | 取得日・hash先頭 | 確認内容 | 限界・状態 |
| --- | --- | --- | --- | --- |
| J-Quants master capture | 72030、要求有効日2026-05-07 | 2026-09-11のrun、8c520e92… | Code・Date一致、1行、hash一致 | masterに検証済み単元欄なし。5月全体の100株単元はblocked |
| J-Quants日足原応答capture | 72030、5月1日～29日 | 2026-09-11、7bb55215… | 18行、個別応答→DailyOpenObservationの再構築一致 | 独立外部価格照合はunverified |
| 日足の正規化証拠capture | 同上 | 2026-09-11 10:04:53.616898 UTC、ced0e4f2… | raw／adjusted／Volume／係数／取得時刻がsnapshotと一致 | Provider契約と保存物内の整合を確認。外部真正性の保証ではない |
| J-Quants calendar capture | 2026年5月の全31日、TSE | 2026-09-11のrun、1c27cc5a… | 全日付coverage、HolDiv 1/2の18sessionと価格日集合が一致 | 個別取得時刻・市場時刻は未記録。HolDiv 3をTSE営業日にしない |
| 第1価格packet | 72030、前半9行 | 取得時刻を保持、ffd0d6f7… | packet hash・snapshot内部hash・出典・OHLCV両laneが一致 | 新規公開を日々観測した証拠ではない |
| 第2価格packet | 72030、後半9行 | 同上、038d60c7… | 同上。第1区間と非重複で時系列順 | 2区間投入は保存データ再生の試験方法 |
| 単元のReferenceReview | 5月全体を覆う証拠が必要 | 未取得、hashなし | 今回はlot=null | 現在の単元から過去へ推測しない。発行体／取引所の有効日・変更履歴との照合が必要 |
| 停止・個別時刻 | 72030・対象月 | 網羅的証拠なし | 保存日足に明示された企業行動なし、既知停止の追加記録なし | halt coverage=unknown、actual_trade_at=null。停止なし／実寄付き時刻を証明しない |

価格basisは既存`data.price_policy`のJ-Quants契約を使用し、Execution株数にはraw Volumeを照合する。
Signalはadjusted OHLCV、清算はraw Open、時価評価はraw Close。元の調整系列を加工・再復元しない。
外部価格照合は未検証だが、既存Proxy契約にない新しい必須Gateとして追加していない。
ReferenceReviewのsubject hash・有効期間・review参照を構造検証しても、資料の真正性まで自動verifiedにしない。
実資料の確認とローカル承認参照の対応は所有者のレビュー責任であり、署名基盤は新設していない。

## 履歴・購入可能性・今回の判定

既存指標実装の最初の有限値に必要な観測数：SMA20、ATR20、ADX28、正規化傾き39、
range window60、SMA交差79、ATR安定性39、range幅安定性79、流動性60。
初日Closeから判断するには最低78観測の事前履歴が必要。数が足りても価格系列によって有限性は別検査となる。
今回は期間前0観測、対象期間18観測。期間途中から勝手に開始したり、将来行をwarm-upに使用しない。
リポジトリ内の`.delayed_replay`とデータ保存領域を探索した範囲で、追加候補となる72030の過去snapshotは見つからなかった。
依存ライブラリのParquetテストfixtureは市場履歴候補に含めない。別フォルダ・別リポジトリは未探索。

5月1日Closeを使う静的診断（前日の注文予算ではない）では、提案10%の予算は20000円、
提案Slippage・費用による100株の参照費用は300300円、購入可能数量は0株。
この参照価格では20万円全額でも1単元に届かない。上限・資金・銘柄・単元を変更していない。
これは注文や損益を計算した結果ではなく、参照日と用途を明示した単元予算の診断である。

preflightは`blocked`、理由は **approval_pending / lot_period_or_review_missing / insufficient_history**。
`fill_count=null`, `clearing=not_executed`。予算不足の診断も別項目に保持する。
no_signalは計算を終えた場合に限り、未実行・処理途中・履歴不足とは区別する。
0 Fillのrunを入力受理・清算・会計・注文待機の全てがverifiedになったと報告しない。

## 実装と副作用の境界

`LimitedProxyTrialPlan`は設定・候補・入力hash・証拠・用途・反復方法を固定する。
`ScopedResearchAuthorization`は所有者が別途記録した具体的plan hashと承認参照を要求する。
実試験は`approved_for_limited_trial`、人工試験は`artificial_test_authorization`で相互代用不可。
どちらもmodel全体はunapproved、登録はdraft_not_registered、正式OOS不許可。
人工由来はフラグだけでなく、専用生成recipeから価格まで再生成一致を検証する。
保存済み実snapshotを旧synthetic入口へ渡す経路も拒否する。

新規の`limited-proxy-plan-v1` / `limited-proxy-event-v1` / `limited-proxy-state-v1` /
`limited-resolution-v1`と、`limited-daily-open-proxy-reducer-v1`による別DB／draft_audit streamを使う。
旧streamの暗黙migrationなし。旧Checkpointのreducer identity検査で誤接続を拒否する。
既存Proxy側の変更はprivateなresolver呼出し口とresult factoryだけで、既定経路・Gate・schemaは不変。
清算前品質検査、予約、cash/Position、cursor、監査は既存共通算術と同一DB transactionを使用する。
新しい戦略・Selector・Broker・正式Judgeは実装していない。

preflightはファイル検証と件数・静的予算診断のみで、Signal／月次Validation／清算を実行しない。
取得済みsnapshotは外部でhash検証、pure reducerへは検証済みcommandを渡す。
入力version追加は固定catalogの既知hashだけを受理し、親head・日付・内容を照合する。
同一event再送・別event IDの同一業務再送は二重清算しない。確定済みprefixを改訂データで再計算しない。
モデルhash・plan・source・承認・style・入力不一致の再開は拒否する。
未取得日時点の再生も拒否。元の取得時刻を過去9時へ変更しない。

## コマンド

Pythonプロジェクトディレクトリから、元7B runを明示する。以下はpreflightのみ：

```bash
python -m examples.validate_limited_proxy preflight \
  --plan config/limited_proxy_trial_plan.json \
  --saved-run .delayed_replay/stage7b/84f2ff0dc85e4080b97abcddbbe9be3d \
  --output .delayed_replay/limited_trial/new-preflight
```

今回のローカル成果物は`.delayed_replay/limited_trial/preparation-20260912-004/`の
`plan.json`と`preflight.json`。旧001～003は補正前のドラフト履歴であり、現行model hashでは実行不可。
出力先衝突は上書きせず拒否する。Raw価格・生成DB・承認参照はGit除外領域に保存する。

**次は今回実行していない。** 証拠・履歴を満たした更新planへ具体的承認を得た後だけ：

```bash
python -m examples.validate_limited_proxy compare \
  --plan REVIEWED_PLAN_JSON \
  --authorization OWNER_RECORDED_AUTHORIZATION_JSON \
  --saved-run SAVED_RUN_DIRECTORY \
  --replayed-at EXPLICIT_UTC_TIME_WITH_MICROSECONDS \
  --output .delayed_replay/limited_trial/unique-approved-comparison
```

承認JSONは`limited-authorization-v1`、plan_hash、status、approval_reference、recorded_atが必須。
compareは承認・preflight通過後だけ、別々のcontinuous／split DBを作り、9行投入→待機→再起動→
残り9行受理→冪等再送→再開を行い、論理状態と監査prefixを比較する。
同一planの許可された比較であって設定探索ではない。所有者の悪意ある手動改変を防ぐ認証基盤ではない。

## 人工検証・残る判断

新規43件はnetwork guard下の人工capture／InputPacket／実EventStore／共通戦略を使用する。
許可済み人工fixtureで非空Fillと連続／分割再開一致、実承認との相互代用拒否、元入力とprefix不変、
rollback／応答消失、親head競合、旧stream拒否、カレンダー不足とhash破損、単元期間・basis・停止・
時刻条件、未指定設定、18行履歴不足、zero-fill、出力衝突を検証する。
既存Proxy70件を維持。全回帰・CI結果は対象commitの完了報告を参照。
通常pytestに実7B清算やAPIキーを必要とするテストは追加していない。

次の判断はまとめて必要：

1. 全量代理清算の仮定、数値設定、固定baseline供給案を限定試験に採用するか。
2. 月次選択方式を維持する場合のlookback等と接続範囲。今回は運用方針を変更していない。
3. 2026年5月全体の100株単元根拠・レビュー参照の用意。
4. 必要な事前履歴をどう用意するか。取得や期間拡大は別許可であり、今回行わない。
5. 現資金・単元で非空Fillが出ない可能性を受け入れ、入力接続と売買会計検証を分けるか。

手動固定注文で入力接続だけを試す案も、戦略E2Eとは別用途・別承認・別streamが必要。
今回は手動注文の実データ清算を行う入口は用意していない。

**実データ清算開始：承認待ち＋証拠不足＋履歴不足。加えて提案設定・参照価格では単元予算不足。**
