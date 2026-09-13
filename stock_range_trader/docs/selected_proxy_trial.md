# 承認範囲限定：固定baseline・代表1銘柄の保存データ研究

この試験は所有者の会話上の明示承認に基づく限定研究であり、正式登録・OOS・
Paper Trading・Broker接続ではない。元の72030 planは変更しない。
月次ValidationとSelectorは対象外。既存Executable Gateと正式Checkpointは変更しない。

## 固定する設定

- 資金200000円、100株単位、1銘柄上限25%、最大保有1。
- Commission売買代金の0.1%（最低手数料なし）、BUY Slippage +0.1%、SELL -0.1%。
- 予約buffer 1%。価格・金額quantum 0.01円。
- BUY切上げ、SELL切下げ、費用・予約切上げ、予算切下げ、その他half-even。
- baselineのみ。BUY/SELL ATR倍率1.5、Range Score閾値70、ADX ENTRY上限25。
- `config/selected_proxy_strategy.yaml`は承認時の`strategy.yaml`のbytesコピー。
  コピー元SHA-256は`135f38b9c6557520b4270895770ac573e0c23fdc023fb0a90b63ed162683580c`。
  全項目を取り込み、資金・口座上限・Commission等の承認済み差分だけを明示適用する。
  元ファイルを後日変更しても取り込まない。固定コピー自体が変われば拒否。
- 日足Openによる代理全量約定。実約定・寄付き参加・個別約定時刻を証明しない。
  `actual_trade_at=null`、モデル全体はunapprovedのまま、今回のplanだけを許可する。
- Signalはadjusted OHLCV、約定はraw Open、時価評価はraw Close、配当除外。
  追加買いなし、全株EXIT、同batch売却代金再利用なし、期末強制売却なし。
- 欠測は待機。既存のVolume事後検査、企業行動拒否、停止coverage unknownを維持。
  新しいDD停止・最終化・月次再選択は実装しない。

価格・金額quantumは研究算術の粒度であり、取引所の呼値証拠ではない。
比例手数料とSlippageは研究仮定であり、実際の証券会社の料金を表すものではない。
25%は判断時Equityに対する新規注文予算であり、値上がり後に強制リバランスしない。

## 取得と代表銘柄の決定

Stage Aは46890 / 94320 / 94340の4月30日Masterと日足（6要求）、同日のcalendar（1要求）。
全3銘柄の証拠を検証し、4月30日raw Closeに`size_buy`を適用する。
費用・予約buffer込みで100株以上となる銘柄のコード昇順で代表1銘柄を固定する。
単元資料不足・価格欠測・未知商品区分等を購入不能扱いで飛ばさない。
商品区分011は内国株券であり、それだけで普通株と証明せず、固定した5桁コード・
発行体の普通株資料と併用する。

代表決定前に長期履歴・5月価格は要求できない。決定後も他2銘柄の履歴を要求できない。
代表決定にはScore、Signal件数、バックテスト収益、未来Openを使用しない。
後続工程で履歴不足・企業行動等が見つかっても代表を変更しない。

Stage Bは代表銘柄だけに限定する：

| 内容 | 内部の半開区間／指定日 | 初回要求数 |
| --- | --- | ---: |
| calendar | [2026-01-01, 2026-06-01) | 1 |
| Master | 2026-05-01 | 1 |
| warm-up | [2026-01-01, 2026-04-30) | 1 |
| 試験月 | [2026-05-01, 2026-06-01) | 1 |

4月30日はStage Aの応答を再利用する。合計初回11要求。
calendarの4月30日重複部分を照合する。APIの`to`へは半開区間終了前日を渡す。
履歴不足・不適合がわかった時点で停止し、5月取得へ進まない。
実行日に[Freeの提供期間](https://jpx-jquants.com/ja/spec/data-spec)を確認する。
実装の730日前〜12週前の境界は保守的な内側制限であり、公式権利範囲そのものではない。

warm-upは固定取得枠全体を使用し、事前78実観測以上を要求する。
推測session・休日補間・指標期間短縮・開始日変更・過去への自動延長はしない。
清算前に各試験日までのprefixだけからSMA/ATR/ADX/Range Scoreの有限性を確認する。
その確認はSignal選択・収益評価ではなく、入力適合性検査である。

## 20試行／20分は再開しても一つ

`acquisition.sqlite`にplan、送信前予約、応答、capture、選定をhash chainで追記する。
同時に一つのプロセスだけが排他leaseを持つ。

- 最初の送信前予約から1200秒の絶対deadline。停止・再起動中も時計は進む。
- 送信前にSQLite FULL同期で試行枠を記録する。応答消失でも消費済み。
- 全HTTP試行、pagination、429/5xx/Network Errorの再試行を合計20回に算入。
- 13秒以上の逐次間隔、Retry-After優先。待機が残時間を超える場合も停止。
- HTTP timeoutはmin(30秒, 残時間)。応答body全体もPOSIX deadlineと残時間検査で制限。
- ClientV2の実Sessionを再利用し、Adapter内部Retryを無効化。外側だけが再試行する。
- redirect禁止。APIキーは`JQUANTS_API_KEY`だけから読む。秘密値やエラー本文を監査へ入れない。
- 再開は既存receiptが必須。既存planへのprepareや、receipt欠落からの自動再生成は禁止。
- 完全なcaptureは再利用する。途中pageの応答だけで再開した場合の再送も残予算を消費する。
- 情報不足・非対応・期限・試行数不足で停止したreceiptを自動再開しない。

所有者がローカルファイル一式を故意に削除・改ざんして新しい試験を作ることまで防ぐ
認証・外部課金基盤ではない。通常入口での再開は同一root・同一plan・同一receiptを使う。
POSIX signalによるdeadlineを適用できない環境はfail-closed。
[公式Free rate limit](https://jpx-jquants.com/ja/spec/rate-limits)は5回/分。

## 出典・承認・口座の分離

所有者の条件付き承認 → AcquisitionPlan hash → 選定根拠hash →
全capture/packet hash付きSelectedTrialPlan → そのplanだけのScopedResearchAuthorization。
この派生許可は既存の会話上の承認を狭めて記録するものであり、再承認の捏造や正式OOS許可ではない。

旧72030の`LimitedProxyTrialPlan`は新schemaを拒否する。
新規`selected-daily-open-proxy-reducer-v1`のstreamで共通Proxy算術だけを再利用し、
旧台帳をmigrationしない。元planファイル・既存Executable Gate・正式Checkpointは無変更。
共通実装変更によりmodel hashは変わるため、旧hashを新コードへの許可として再利用しない。

株価の元HTTP本文は非公開receiptへ保存する。小数は文字列へ正規化した派生captureと
対応付ける。履歴・4月30日・5月の取得時刻、source hash、raw/adjusted両laneを照合する。
元取得時刻を市場当日9時などへ書き換えない。履歴snapshotも出典応答へ再照合する。
公開文書・Gitへraw価格やキー、DB、ローカル絶対パスは出さない。

単元証拠は発行体が掲載する日付付き定款の該当条文と掲載ページをレビューする。
資料bytesのSHA-256と対象期間を記録する。構造検証と資料真正性は同じではない。
これは事後的な公開資料レビューであり、当時の配信観測や署名付き証明ではない。
市場の個別時刻、網羅的停止情報、独立した外部価格照合は未検証のまま残す。

## 操作の境界

`python -m examples.selected_proxy_trial`は以下を明示分離する。

1. `prepare --root ... --reviews ... --approval-reference ...`：取得前の計画・承認・receipt生成。
2. `acquire --root ... --live --reviewed-free-window`：承認済み範囲だけの限定取得。
3. `construct --root ...`：取得receiptを再検証し、派生plan・限定許可・preflightを保存。
4. `execute --root ...`：条件充足後だけProxy清算と連続／分割再開比較。

清算比較は同一planで2口座を独立作成する許可済み比較であり、パラメータ探索ではない。
9行受理→不足待機→再起動→残り9行受理→冪等再送→完了の論理状態と既存監査prefixを比較する。
約定0なら銘柄・期間・設定を変更しない。入力受理・再開が成功しても非空Fillの
会計検証に成功したとは扱わない。正式1M/3M CheckpointやProtocolJudgeへ渡さない。

## 今回の実施結果

2026-09-13に所有者承認済みの範囲だけを実行した。取得後の銘柄・期間・設定変更なし。

| 固定対象 | SHA-256 |
| --- | --- |
| AcquisitionPlan | `77129493631c6a350c0d86a8763f668103abb846a22ccf7ee70287686893ecd4` |
| Settings（strategy + terms + rules） | `69cd67379ea44b39c1084a7c19042e3277180a5815b8d055c0040f111c538b38` |
| Selection | `9a4349de991fb06c3964df54f5653a8ea5543faaecda55846879f89700e496ce` |
| SelectedTrialPlan | `e6edf4316a2bb7eb173da0b1761bae89519243420bc536b5f7ae03f252c2fed5` |
| Model implementation | `68bd43e72b3937f4bf241294f2f25aa6a7161f380a4c621247b1298eacefc79f` |

- 4月30日の購入可能性・単元資料・Master・calendarを検証し、3銘柄とも適合。
  昇順規則により**46890（LINEヤフー）**に固定した。他2銘柄の履歴・5月価格は取得していない。
- 初回送信前予約は09:25:20.642978 UTC、最終capture記録は09:27:30.824267 UTC。
  経過**130.181289秒**、**11試行・全HTTP 200**、最短試行間隔13.003339秒。
  リトライ・paginationなし。20試行／1200秒の一つの予算内で完了し、その後の追加取得なし。
- 事前履歴79観測（1月〜4月29日の78観測 + 再利用した4月30日の1観測）、
  5月18観測。各試験日のprefixのみを使うSMA／ATR／ADX／Range Score有限性検査を通過。
  preflightは`ready=true`。source hash、raw/adjusted lane、単元資料、calendar、
  保存・再読込、対象内の非対応企業行動が報告されていないことを検証した。
- Proxy実行は`completed`。5月25日にBUY 100株の注文を1件固定したが、翌sessionの
  費用込み必要額が予約額を上回り、`frozen_reservation_or_budget_exceeded`で拒否された。
  **約定0件、最終Cash／Equityとも200000円、保有0、実現損益0**。
  期末理由は`no_next_bar_no_forced_exit`。追加予算・数量の再計算・強制EXITは行っていない。
- 連続実行と9 + 9観測の分割再開で`logical_state_equal=true`、
  `prefix_unchanged=true`。後半packetの冪等再送も実施した。
  両者の論理hashは`70ac03ffa007801f4e4968e539e089d0f58a08b30a49df172ed57653cfd36f01`。
- 約定0なので、**実データ由来の非空Fill清算会計は未検証**。
  人工データの正例では非空Fillと再開一致を別途検証するが、この未検証を代替しない。
  月次Validation、正式Checkpoint、正式登録、OOS、PR作成・mergeは実施していない。

単元レビューには、[LINEヤフー定款（2023-10-01）](https://www.lycorp.co.jp/ja/company/overview/pdf/cg00_%E5%AE%9A%E6%AC%BE_231001.pdf)、
[NTT定款（2025-07-01）](https://group.ntt/jp/ir/shares/pdf/articles_incorporation.pdf?250701=)、
[ソフトバンク定款（2024-10-01）](https://www.softbank.jp/corp/set/data/aboutus/profile/pdf/articles_of_incorporation.pdf?202410_1=)
の100株条文と各社の掲載ページを使用した。実際の市場約定時刻、網羅的な売買停止、
独立した外部価格照合は引き続き未検証。単元資料の事後レビューを当時の観測としない。

詳細なreceipt、価格、証拠PDF、入力packet、preflight、口座DB、`comparison.json`は
Git除外対象の`.delayed_replay/selected_trial/owner-approved-fixed-baseline-v1/`へ保存した。
APIキー・市場データ本文・DBはcommit対象外。
人工データ37件は別のfixture由来・別の承認状態を使い、実際の所有者承認と区別する。
