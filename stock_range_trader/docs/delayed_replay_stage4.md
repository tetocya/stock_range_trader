# 遅延再生 段階4：人工データ接続（DRAFT・未登録）

段階4は実データOOS開始の許可ではありません。市場API、Live取得、正式登録、
Checkpoint合否、Benchmark、Report bundle、Broker、税金は対象外です。
初期資金20万円・共有口座・Long Only・100株単位を維持します。
人工fixtureの保有上限40%、最大2銘柄、参照1か月、費用0等は**テスト設定**であり、
運用推奨値・承認済みの既定値ではありません。

## Interfaceと既存層との対応

| 専用Interface | 接続先・契約 |
| --- | --- |
| `ReplayPolicy`, `ReplayPlan` | 全policy、独立Validation設定、Catalog、Universe、source、入力版を明示固定 |
| `ReplayCalendar`, `ReplaySession` | 明示したJST session、選択・Open・Close時刻。曜日推測なし |
| `MarketView`, `OpenSnapshot` | 段階1 `PriceSnapshot` と人工Open証拠をpinし、独立した公開済みviewを生成 |
| `MonthlySelection`, `SelectionEpoch` | 既存 `ExecutableOutcomeEvaluator.evaluate_validation()` → `ExecutableCandidateSelector.select()` |
| `SignalAdapter` | 既存detector → scorer → `MeanReversionStrategy`。調整済みOHLCVだけを受け取る |
| `ReplayEngine` | 計算・公開制御を純粋reducerの外で実行し、結果をcommandに固定 |
| `ReplayState`, `ReplayReducer` | 段階3 `AccountReducer` を合成。段階2 `EventStore` の単一transactionで口座とcursorを更新 |

既存Phase 1〜3、遅延再生段階1〜3の公開Interface・設定・既定値は変更していません。
特に段階1 `ReplayDataView.history()` はClose後も当日Bar非公開のままです。

## 固定イベント順序

各sessionを `select → open → mark → prepare → decide → finish` の6phaseで処理します。
Open公開とOpen処理、Close公開と一括markはそれぞれ同じphaseです。
月次選択以外のsessionでもselect phaseは進みますが、新しいepochは生成しません。

1. 暦月初cutoffで月次選択。休日でもcutoffは月初のまま、実行は最初のsessionの明示selection_at。
2. 明示Open時刻に、前sessionからのpending注文を固定数量で処理。SELL優先等は段階3を維持。
3. Close確定後、全保有銘柄のmarkが揃った場合だけ一括評価。同日評価の改訂なし。
4. Close評価品質を確認し、売却代金holdの解除と累積drawdown判定。
5. 旧Positionのentry CandidateによるEXITと、その月の新CandidateによるENTRYを生成。
6. sessionを確定し次へ進む。

月途中開始は拒否し、`[run_start, run_end)`外で約定しません。
最終closeの非HOLD判断は`no_next_bar`、新規予約なし。残存Positionは時価評価し強制決済しません。
`completed`は予定範囲の再生終了であり、プロトコルPASSではありません。

## 明示policyと価格

全項目を要求する `ReplayPolicy` に実行既定値はありません。

- `selection_mode=independent_symbol_validation`：独立symbol Validationを固定条件で比較。
- `no_candidate_mode=disable_entries_keep_positions_and_pending`：新規ENTRY停止、既存EXIT継続、有効pending維持。
- `proceeds_mode=release_after_sale_open_at_complete_close`：同Open中の売却代金再利用は禁止。
- `missing_input_mode=wait_without_expiry`：入力待ちだけで取消・次session繰越をしない。
- `corporate_action_mode=stop_preserve_positions`：保有中の未対応企業行動は保有を残して停止。
- lookback、warmupの暦月数、最低warmup観測数、最大DD正幅は明示値。

段階3のAccountPolicyは引き続き`hold_until_later_decision_session`しか受理しません。
段階4のprepare bridgeだけが、明示したClose解除policy、当日一括評価完了、
`sale_at < close_time`を検証してholdを解除します。将来日を偽装して段階3releaseを
呼びません。独立した段階3クライアントの従来挙動は変わりません。
hold解除とcursorは同じtransactionで、業務phase IDによって重複解除されません。

| 用途 | 価格lane |
| --- | --- |
| 指標・Range Score・ENTRY・EXIT | 調整済みOHLCV。SignalAdapterにExecution列は渡さない |
| Signal stop lossのentry基準 | entry session調整済みOpen × (1 + 明示buy slippage)。Phase 1と同じ |
| 固定数量・予約・commission・cash・mark | 非調整Execution laneと段階3のDecimal/丸めpolicy |
| 配当 | Strategy/Validationとも利益へ加算しない。株数分割適用なし |

最初の保有closeでSignal entry基準を保存し、以降変更しません。約定当日のcloseを
保有session 1とし、以後銘柄の実観測ごとに増加します。range breakdown streakは
run開始以降のそのentry Candidateの準備済み系列に沿い、Position状態として保存します。
月替わりで日数・streak・高値資産・cash・pendingをリセットしません。
EXIT優先順位は既存どおり stop loss → range breakdown → 最大保有 → SMA/ATR回帰。
pendingのある銘柄へ重複注文を発行しません。
Scoreの注文優先順位への受渡しのみ、段階3の表現上限に合わせ小数12桁half-evenとします。
価格・金額の丸めは明示AccountPolicyに従い、暗黙に資金上限を緩和しません。

## 月次選択と情報境界

Validationは`[boundary-lookback_months, boundary)`、warmupはその前の明示月数です。
最低warmup実観測数未達は銘柄単位の`insufficient_warmup`として残し、期間を短縮しません。
Evaluatorへ渡す前にcutoff・固定Universeへ切り出し、入力不正は候補なしへ隠しません。
空履歴／warmup未達による`insufficient_history`、正常な`no_eligible_candidate`、
入力待ち、計算例外を区別します。全候補の共通cohort・除外、Score、symbol outcome、
選択順位、期間、設定hash、可視入力hashをepochへ保存します。
Fold adapterの構造上Test区間の日付は必要ですが、Test価格は作らずTest評価も呼びません。
Validation末尾未決済は末尾CloseでMTMし、完了取引数には足しません。
共有口座を引数に取らず、Validation状態をOOS共有口座へコピーしません。
前月の市場履歴が翌月のlookbackへ入ることは固定規則どおり許可します。

market_timeとreplayed_atを分離し、fetched_atがmarket_timeより後でも遅延再生として許容。
ただしfetched_atがreplayed_atより後なら利用を待ちます。公開不明はunknownのままです。
Close直前は当日OHLCVを公開せず、Close後だけ専用viewへ追加します。
Openには人工 `ExecutionOpenEvidence` だけを渡し、当日高安終値・最終出来高を渡しません。
日足最終Volumeが正であることから寄付き約定可能性を推測しません。

run入力identityはSnapshot hash/version、全calendar、Universe、Catalog、設定、sourceを固定。
別runの未来行変更は入力identityを変えますが、過去の選択・注文価格・株数を変えません。
従って別入力run間の全監査hash一致は要求しません。固定runへの差替えは拒否します。

## transaction・再開・異常

公開viewからEvaluator／Signalを外側で計算し、immutable JSON commandにします。
段階2DBに口座、cursor、epoch、戦略状態、判断、phase batch hashを一括commitします。
段階3の内部commandには正しいAccountPolicy hashを渡し、外側は別のReplay identityです。
純粋reducerに市場取得、ファイル読込、再選択、Signal再計算はありません。

- 計算後・commit前の中断：固定入力で計算し直してよい。
- commit中の障害：口座とcursorを共にrollback。注文だけ反映した状態を残さない。
- commit後・応答前：確定cursorから再開。同event IDは段階2冪等性、別event IDの同業務phaseは段階4冪等性で保護。
- 選択確定後：復旧時にEvaluator／Selectorを再実行しない。
- Open batch途中：全量transactionなので途中口座が正本になることはない。
- mark後／注文受付後：それぞれ次phaseから再開。日次mark再実行や重複予約なし。
- 入力不足：`waiting_for_input`と理由を保存しcursorを保持。揃うまで同日markを確定しない。
- 保有中の未対応企業行動：`stopped_contract`と理由を保存し保有を残す。
- DB改ざん、source/policy/Catalog/Universe/Snapshot不一致：検証付きresumeで拒否。
- 計算破損等：例外を呼出元へ返す。直前の確定phaseは残り、候補なしへ変換しない。

初期版は単一writer・全量固定Snapshotです。後から別Snapshotを追加するイベントは
未実装であり、既存Snapshotのwall時刻待ちで解決しない欠損は待機のままです。
推測session完了による注文失効、最終化期限、INCONCLUSIVE判定は実装しません。
全台帳検証のコストは累積するため、大規模長期間向けの性能保証はありません。

## 人工検証の実行

Pythonプロジェクトディレクトリから、既存の開発環境で実行します。

```bash
RUN_LIVE_JQUANTS_TESTS=0 RUN_LIVE_YFINANCE_TESTS=0 python -m pytest -q
ruff check .
ruff format --check .
git diff --check
```

`tests/delayed_replay_stage4_helpers.py`は明示した不規則な人工sessionと2銘柄・2候補を使用。
3暦月、21sessionで実Evaluator／Selectorによる`slow → slow → fast`を発生させます。
正常fixtureではBUY 8件、SELL 7件、完了取引の月跨ぎ2件、期末未決済1銘柄を確認します。
市場の実際の取引日・取引頻度・成績を再現するfixtureではありません。
復旧試験はSQLite transactionの障害注入と検証付き再開であり、物理停電試験ではありません。

実日足のみではOpen時点の証拠が十分ではなく、実データExecutable再生は接続未検証です。
この機能は当時同じ価格版が配信されていた証明でも、分割遡及調整の完全な除去でもありません。
yfinance Executable禁止は継続します。Live・正式OOSは未実施です。
