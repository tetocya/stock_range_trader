# 46890・2026年6月限定Proxy試験

6月専用scope、保存履歴の照合、新単元レビュー、calendar分割、
取得・取得再開・入力成果物に加え、専用清算Serviceと口座再開を人工入力で検証した。
市場API取得・実データ清算は実行していない。入力manifestは引き続き
`executable=false`であり、生成だけでは清算を許可しない。正式登録、OOS、月次Validation、Checkpoint、
既存Executable Gateの変更はない。前回5月の取得・清算許可を転用しない。

## 固定契約

- 対象46890のみ、半開区間 `[2026-06-01, 2026-07-01)`。銘柄・期間の再選定なし。
- 独立した6月研究口座を初期Cash/Equity 200000・保有/注文なしで作成する。
  5月口座のCash/Position/Order状態は移植せず、履歴価格と出典だけを再利用する。
  今回作成した口座は人工fixtureのみ。実データ口座は未作成。
- 20万円、100株単位、1銘柄25%、最大1銘柄、Commission 0.1%、最低手数料なし、
  Slippage BUY +0.1%/SELL -0.1%、buffer 1%、quantum 0.01円と従来丸めを維持。
- 固定baseline、ATR倍率BUY/SELL 1.5、Range Score 70、ADX ENTRY上限25。
  その他も前回固定済みstrategy/terms/rulesの全項目をhash一致で検査する。
- 調整済みOHLCVはSignal用、報告された非調整OpenはProxy用、Closeは時価評価用。
  配当除外。数量と予約額の固定、予算不足時の拒否、強制EXITなし等の計算規則は無変更。
- 新しい実装hashは共有ソース全体と6月CLIのhashから算出する。
  追加ソースによって実装hashは変わるが、旧hashを書き換えずparentとして残す。

| 対象 | SHA-256 |
| --- | --- |
| 6月取得準備plan（清算planとは別） | `b6015fdeeb990689b13ecf930146b1c6e8347ab2505fc2b83862f3a593ce2e47` |
| 6月実装 | `bac4ed3904f7176182b35fd4316369c2e185ca101e15134169321eef1a76860b` |
| 維持した設定 | `69cd67379ea44b39c1084a7c19042e3277180a5815b8d055c0040f111c538b38` |
| 新単元レビュー | `7e85f6b7034b260f0b01c0030ed59d3d5345ee5aaa1b5aeba0a150ef5683f4c2` |
| 参照元5月plan | `e6edf4316a2bb7eb173da0b1761bae89519243420bc536b5f7ae03f252c2fed5` |
| 参照元5月実装 | `68bd43e72b3937f4bf241294f2f25aa6a7161f380a4c621247b1298eacefc79f` |

plan/reviewのhashは正規化JSONのSHA-256。整形済み公開JSONのファイルbytes hashとは区別する。
公開スナップショットは `config/june_proxy_trial_plan.json` と
`config/june_proxy_lot_review.json`。価格やDBは含まない。
旧準備plan `83d31b3de263b8672458365415215955ab41e5f3fb0810b5b3609b62c3231d7a`
は元のv1フォルダに保持した。旧receiptがplan記録1件・HTTP試行0件であることを確認した上で、
新実装を指すv3を別フォルダに作成した。途中のv2も通信0件のまま保持する。
既存通信予算・取得済みデータのリセットではない。
単元レビュー内容・日時・hashも維持する。新旧とも実取得・実清算は未承認。

## 履歴・単元・価格・calendar

保存済み4packetの97観測（1月〜4月79 + 5月18）を読み取り専用で再検証する。
親SelectedInputsによる元応答、packet、source hash、取得時刻、両価格laneの照合を行う。
packetの役割を6月では履歴参照にするだけで、元bytes/hash/取得時刻を変更しない。

履歴再取得応答の全97観測について、Date/Code、O/H/L/C/Vo、AdjO/AdjH/AdjL/AdjC/AdjVo、
AdjFactor、ExRTを比較する。Decimal文字列表現だけを正規化し、丸め許容差は設けない。
行順、取得時刻や無関係な列の違いは価格訂正としない。欠落・重複・未知日付・銘柄混入・
価格/出来高/調整/企業行動の変更は停止し、旧履歴を上書きしない。

新単元レビューは別のsubject `[2026-06-01, 2026-07-01)` として記録した。
2026-09-14に[発行体会社概要](https://www.lycorp.co.jp/ja/company/overview)の100株表示と
2023-10-01定款掲載を確認し、保存済み定款第7条のPDF bytes hashを再検証した。
旧レビューのendを変更していない。これは事後の文書レビューであり、当時の配信時刻や
署名付き真正性証明ではない。PDF実体は元保存先に依存するため、欠落・変更は停止する。

新calendarの5月29〜31日を保存済みcalendarと照合する。6月は30暦日すべてを要求し、
営業日の推測・欠損補間をしない。観測session数Nの前半floor(N/2)、後半残りを固定し、
両方非空を要求する。18観測や9+9への固定はない。価格・Signal・成績を分割に使わない。
各6月sessionのprefixのみでSMA/ATR/ADX/Range Score有限性を検査し、Signal生成はしない。

## 取得順序と上限

| 順序 | Endpoint | 明示パラメータ | 初回要求数 |
| --- | --- | --- | ---: |
| 1 | /markets/calendar | from=2026-05-29, to=2026-06-30 | 1 |
| 2 | /equities/master | code=46890, date=2026-06-01 | 1 |
| 3 | /equities/bars/daily | code=46890, from=2026-01-01, to=2026-05-31 | 1 |
| 4 | /equities/bars/daily | code=46890, from=2026-06-01, to=2026-06-30 | 1 |

1の不備は2より前、2の不備は3より前、3の履歴不一致は4より前に停止する。
新しいHTTP入口も既存ClientV2 SessionのRetry無効化・timeout・逐次paginationを再利用する。
全実試行（429/5xx/network error/pageを含む）を送信前に記録し、合計20試行以内。
最初の送信前予約から1200秒。休止・再開も同一時計、13秒以上の間隔、Retry-After優先。
timeout=min(30秒, 残時間)、応答全体もdeadline内。4要求の間隔だけの最低時間は39秒。
他のJ-Quants取得を並行させない。キーはJQUANTS_API_KEYだけから読み、記録しない。

Freeの12週間遅延に加え、計画上のnot_beforeを2026-09-24 18:00 JSTと固定した。
実行直前の[公式提供範囲](https://jpx-jquants.com/ja/spec/data-spec.md)確認も必須。
提供を保証する日時ではなく、範囲外なら期間短縮せず停止する。

空/欠落/破損receiptから予算を再生成しない。取得前prepareも既存rootへの上書きを拒否。
クラッシュ後resumeは保存済み完全captureを再利用し、消費済み試行と最初のdeadlineを維持。
終端停止（不適合・予算切れ等）の自動再開は禁止。完了後のbuild-inputsはHTTPを使わず、
期限切れ後でも保存された元応答pageとcaptureを再検証して入力だけを再構築できる。

## 操作コマンド

Pythonプロジェクトディレクトリで実行する。以下の変数は保存場所を指すだけで、
planに絶対パスを含めない。

```bash
cd /Users/harimatakeuchi/stock_range_trader/stock_range_trader
june_saved_may=.delayed_replay/selected_trial/owner-approved-fixed-baseline-v1
june_preparation_root=.delayed_replay/june_trial/46890-202606-clearing-preparation-v3

# 現在実行可能な読み取り検査。取得・清算を開始しない。
.venv/bin/python -m examples.june_proxy_trial inspect \
  --root "$june_preparation_root" --may-root "$june_saved_may"
```

prepareは今回実施済みで、通信0試行、started_at/deadline=null、取得/清算未許可。
今回作ったrootで再実行すると予算リセット防止のため拒否する。
新規準備時の入口は次の形。review-referenceは実際に行った資料レビューを指定し、
新しいreview日時・plan hashを得る。旧承認をコピーしない。

```bash
.venv/bin/python -m examples.june_proxy_trial prepare \
  --root NEW_UNUSED_ROOT --may-root "$june_saved_may" \
  --lot-review-reference ACTUAL_REVIEW_REFERENCE
```

以下は**将来、別途取得が明示承認され、提供期間を確認した場合のみ**のコマンド。
この変更は承認済みファイルを作らない。保存済みtemplateはstatus=not_approved。
承認時には対象plan hash、実際の承認参照、permission=acquire_only、
status=approved_for_acquisition、execution_permission=falseを持つ別ファイルが必要。
現在の準備依頼だけを根拠にこのstatusへ変更してはならない。

```bash
.venv/bin/python -m examples.june_proxy_trial acquire \
  --root "$june_preparation_root" --may-root "$june_saved_may" \
  --authorization "$june_preparation_root/owner_approved_acquisition.json" \
  --live --reviewed-free-window

# 同じroot・同じ承認・同じ20試行/20分予算での中断後再開
.venv/bin/python -m examples.june_proxy_trial resume \
  --root "$june_preparation_root" --may-root "$june_saved_may" \
  --authorization "$june_preparation_root/owner_approved_acquisition.json" \
  --live --reviewed-free-window

# 全取得完了後のみ。HTTP/口座生成/清算はしない。
.venv/bin/python -m examples.june_proxy_trial build-inputs \
  --root "$june_preparation_root" --may-root "$june_saved_may"
```

build-inputsはsource hash・取得時刻付き2packetとinput_manifestを作る。
同一入力の再実行は同一結果、不一致成果物の上書きは禁止。
元履歴packetは元保存先のまま参照する。成果物だけ移して元証拠を失えば検証できない。

## 清算Service・情報境界・口座再開

`delayed_replay/june_clearing.py`の`JuneClearingPlan`、
`JuneClearingAuthorization`、`JuneClearingService`は6月専用。
`prepare-clearing`は既存`build-inputs`成果物を**書き換えず**再検証する。
receipt元応答→capture→履歴照合→calendar→packet/source/timestamp→manifestの
完全一致と、各prefixの指標有限性を要求する。未取得・不完全・改変は停止する。
新口座作成/再開でもこの検査を行い、稼働中は検証済み不変bundleを使い、各イベント前に
証拠ファイルのbytes hashを照合する。単なる`input_ready=true`を信用しない。

専用Reducerは既存`_LimitedReducer`と`_resolve_batch`を再利用し、数量・予約・
丸め・費用・Cash・Positionの算術を複製しない。公開Phase 3 Interfaceや既定値は変更しない。
6月初日は新しい選択epochだけを作る。初日のBUYは初日終値確定後の新規判断に限り、
翌calendar sessionへ送る。5月末Signal/注文は持ち込まない。
SignalAdapterへ渡る調整済み価格は判断日まで。次sessionのraw Openはその約定フェーズで
だけ使い、決定済み株数・予約額を増やさない。6月最終sessionでは新たな翌日注文を出さず、
7月価格・強制EXITを使わない。保有があれば6月末Closeで評価して残す。

分割はcalendar順のfloor(N/2)。初期入力は全履歴と第1packetのみで、後半は台帳に未公開。
前半完了後に`waiting_for_input`となり、第2packetを追加受理してからDBを閉じる。
再開は元genesis・plan・許可・入力を照合し、追記型イベント列を再生して口座状態を復元する。
同じ追加イベントの再送は冪等。別packet/parent/過去訂正は契約で拒否する。
比較は注文（凍結数量・予算・予約を含む）、予約、Fill、Cash/Equity、Position、cursor、
判断・評価・epoch・完了取引を照合する。入力受理時刻・配送方式によるログの差は除外するが、
既存イベントprefix不変と追加受理直後の完全な状態復元も別途要求する。

### 取得許可と清算許可

`acquisition_authorization.template.json`は取得専用で清算不可。
`clearing_authorization.template.json`は`prepare-clearing`で作成する別の未承認提案で、
取得plan hash・入力manifest hash・清算plan hashを結ぶ。`permission=clear_only`、
`acquisition_permission=false`を要求し、許可の完全な内容も口座identityへ記録する。
実行には別途、対象hashへの明示承認と`status=approved_for_limited_trial`、
実際の`approval_reference`、`recorded_at`が必要。今回の人工検証依頼をこの承認に転用しない。
人工テストは`artificial_test_authorization`を明示し、実CLIは人工provenanceを拒否する。
モデル全体は`unapproved`、Formal Checkpointは`unsupported`を維持する。

### 取得完了後の将来用コマンド（今回は未実行）

前節のacquire/resume/build-inputsに続ける。`prepare-clearing`も現在は実入力がないため停止する。
清算plan hashは取得後のcapture・packet・manifestに依存するため、取得前には確定できない。
最終取得準備plan/実装hashと混同せず、実入力完成後に出力された清算planへ承認を結び付ける。

```bash
.venv/bin/python -m examples.june_proxy_trial prepare-clearing \
  --root "$june_preparation_root" --may-root "$june_saved_may"

# 以下は別途清算の明示承認後のみ。承認ファイルは現在存在しない。
june_clearing_permission="$june_preparation_root/owner_approved_clearing.json"
june_account="$june_preparation_root/research_split.sqlite"
.venv/bin/python -m examples.june_proxy_trial start-clearing \
  --root "$june_preparation_root" --may-root "$june_saved_may" \
  --clearing-authorization "$june_clearing_permission" --execute-saved-data \
  --account "$june_account" --style split_resume

# 入力待機から第2packetを受理するだけ。受理後にプロセス/DBを閉じる。
.venv/bin/python -m examples.june_proxy_trial accept-inputs \
  --root "$june_preparation_root" --may-root "$june_saved_may" \
  --clearing-authorization "$june_clearing_permission" --execute-saved-data \
  --account "$june_account" --style split_resume

.venv/bin/python -m examples.june_proxy_trial resume-clearing \
  --root "$june_preparation_root" --may-root "$june_saved_may" \
  --clearing-authorization "$june_clearing_permission" --execute-saved-data \
  --account "$june_account" --style split_resume

# 同じ許可・同じ入力による別研究口座の連続/分割比較。既存出力の上書き禁止。
.venv/bin/python -m examples.june_proxy_trial compare-clearing \
  --root "$june_preparation_root" --may-root "$june_saved_may" \
  --clearing-authorization "$june_clearing_permission" --execute-saved-data \
  --output "$june_preparation_root/comparison-v1"
```

`start-clearing --style continuous`は全packetを持つ新口座を作る。
`resume`はHTTP取得再開、`resume-clearing`は保存済み口座の再開であり、用途を混同しない。
口座再開にHTTP予算のリセットや追加取得は含まれない。
`accept-inputs`の再送は既受理packetを検出して追加イベントなしで返す。
清算系コマンドのエラー表示は`unverified_inspect_account_if_created`とし、
部分実行済みかもしれない口座を「未実行」と誤報しない。再開前に台帳を確認する。

## 回帰検証・残る制限

新規39テストはネットワーク禁止の人工データ。変更前に取得した5月人工データの
golden結果（Cash/Equity 202590.45、実現損益2590.45、BUY/SELL各500株、各予約額・
価格・手数料）と、連続/分割再開一致を確認する。実市場データの再清算ではない。
既存5月モジュールとProxy算術ソースは変更していない。

6月は追加27テストで入力改変、許可分離、月初の空口座、21sessionの10+11分割、
非空Fill/拒否/期末保有、予約を保持した再開、7月/欠測/重複拒否を検証する。
基準人工fixtureはBUY/SELL各2件、Cash/Equity 206565.83。人工gap fixtureは500株を
減らさず固定予約超過で拒否する。別人工fixtureは500株を期末まで保持する。
これらは固定設定の接続試験であり、実市場の収益や約定可能性を示さない。

6月市場データは未取得、実際のsession集合と指標有限性は未検証。
市場の実約定時刻、停止情報の網羅性、外部価格との独立照合も未検証のまま。
6月の清算接続・口座再開・非空Fill・拒否・期末保有は人工fixtureでのみ検証した。
実際のJuneデータでのこれらの挙動は**unverified**。人工利益は実成績の証拠ではない。
停止情報はunknownで、日足Openは当時の実約定時刻の証拠ではない。株式分割などの
企業行動や欠測を補間して清算を強行しない。モデル全体のExecutable Gateは解除しない。
