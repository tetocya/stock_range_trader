# 46890・2026年6月限定Proxy試験の入力準備

この変更は6月専用scope、保存履歴の照合、新単元レビュー、calendar分割、
取得・取得再開・入力成果物の準備まで。市場API取得・実データ清算は実行していない。
6月清算用Service/Reducer接続と清算コマンドは含めず、入力manifestも
`executable=false`とする。正式登録、OOS、月次Validation、Checkpoint、
既存Executable Gateの変更はない。前回5月の取得・清算許可を転用しない。

## 固定契約

- 対象46890のみ、半開区間 `[2026-06-01, 2026-07-01)`。銘柄・期間の再選定なし。
- 独立した6月研究口座を想定し、5月口座のCash/Position/Order状態は移植しない。
  この段階では口座を作らない。5月は履歴価格と出典だけを再利用する。
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
| 6月plan | `83d31b3de263b8672458365415215955ab41e5f3fb0810b5b3609b62c3231d7a` |
| 6月実装 | `900cc045f14499ec99fb72a6a5ae2b1f85bcb19235b866748bf1a0c8c4937239` |
| 維持した設定 | `69cd67379ea44b39c1084a7c19042e3277180a5815b8d055c0040f111c538b38` |
| 新単元レビュー | `7e85f6b7034b260f0b01c0030ed59d3d5345ee5aaa1b5aeba0a150ef5683f4c2` |
| 参照元5月plan | `e6edf4316a2bb7eb173da0b1761bae89519243420bc536b5f7ae03f252c2fed5` |
| 参照元5月実装 | `68bd43e72b3937f4bf241294f2f25aa6a7161f380a4c621247b1298eacefc79f` |

plan/reviewのhashは正規化JSONのSHA-256。整形済み公開JSONのファイルbytes hashとは区別する。
公開スナップショットは `config/june_proxy_trial_plan.json` と
`config/june_proxy_lot_review.json`。価格やDBは含まない。

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
june_preparation_root=.delayed_replay/june_trial/46890-202606-preparation-v1

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

## 回帰検証・残る制限

新規39テストはネットワーク禁止の人工データ。変更前に取得した5月人工データの
golden結果（Cash/Equity 202590.45、実現損益2590.45、BUY/SELL各500株、各予約額・
価格・手数料）と、連続/分割再開一致を確認する。実市場データの再清算ではない。
既存5月モジュールとProxy算術ソースは変更していない。

6月市場データは未取得、実際のsession集合と指標有限性は未検証。
市場の実約定時刻、停止情報の網羅性、外部価格との独立照合も未検証のまま。
6月の清算接続・口座再開・非空Fill会計は今回の入力準備には含まれない。
将来実装時は月初/翌session/期末の情報境界を維持し、5月末Signalの注文移植や
7月価格取得・強制EXIT・正式OOS許可の転用を行わない。
