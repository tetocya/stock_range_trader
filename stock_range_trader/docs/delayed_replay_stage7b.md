# 段階7B：限定取得基盤（Live Acceptance未完了）

7A基準SHA：`575c733517955a7dc9ca9b5a617ac4e43d6e2cc6`。
同一`codex/phase3-walk-forward-validation`ブランチ。既存Production・公開Schemaは変更しない。
専用モジュール・CLI・人工回帰／opt-in Liveテスト・本文書の新規5ファイル。

## 今回の実行結果

2026-09-10 02:20 UTC（11:20 JST）、公式client **2.6.0**。
通信なしpreflight後、明示`--live`入口を実行したが、環境変数`JQUANTS_API_KEY`未設定のため
**0 HTTP試行・0行、api_key_missingでblocked**。キーをチャットで要求せず、資格情報ファイルも検索しない。
実データ期間・snapshot hashは存在しない。これはLive成功ではない。

固定したpreflight候補：72030のみ、`[2026-05-01, 2026-06-01)`、master要求日2026-05-07。
日足APIのtoは2026-05-31（inclusive）。候補日は契約・公開sessionとの実照合済みではない。
再実行時点で範囲を再確認し、明示日付を設定すること。

| 層 | 今回の実データ結果 | オフライン確認 |
| --- | --- | --- |
| 通信 | blocked：キーなし | 実ClientV2 Adapterの内部retry無効、13秒・20試行・20分、redirect禁止、429／5xx／通信エラー |
| データ | not_tested | 既存Canonical変換・検証、7A DailyOpenObservation、hash、欠測・不正・重複・銘柄不一致 |
| 銘柄・単元 | not_tested | masterのCode・Date・名称存在。単元100株を推測しない |
| カレンダー | not_tested | 全日付coverageと明示HolDiv=1/2の価格日一致。市場時刻は生成しない |
| 保存・公開view | not_tested | 7A InputArtifactStore／PriceSnapshot再読込・同値再送hash不変・破損拒否 |
| InputExtension／口座再開 | not_tested | Executable不適合で口座streamを作らない。7A人工再開テストを維持 |
| 約定 | unsupported | 日足Openは実約定証拠ではない。daily_open_proxy未承認・未実装 |
| 企業行動・実改訂 | not_tested | 7A人工試験のみ。無取得を企業行動検証成功としない |

## 現在の公式根拠

確認日2026-09-10。公開資料閲覧は市場API試行と別計上。

- [公式プラン表・FAQ](https://jpx-jquants.com/en)：Freeは2年履歴・12週遅延・5回/分、
  Trading CalendarがFreeに含まれることを今回確認。ローカル蓄積の案内あり。
  閲覧可能な形でのデータ共有・再配布は禁止というFAQを確認。
  7Aで保留したcalendarの**公開プラン条件**は確認できたが、実アカウント権限は未検証。
- [日足仕様](https://jpx-jquants.com/ja/spec/eq-bars-daily)：調整前／調整済み四本値、
  無取引時Null、企業行動の制約。実約定時刻の証拠へ拡大しない。
- [カレンダー](https://jpx-jquants.com/ja/spec/mkt-cal)、
  [認証](https://jpx-jquants.com/ja/spec/quickstart)：専用endpointとV2 APIキー。
- [契約別詳細](https://jpx-jquants.com/ja/spec/data-spec)、
  [rate limit詳細](https://jpx-jquants.com/ja/spec/rate-limits)、
  [master詳細](https://jpx-jquants.com/ja/spec/eq-master)は今回の閲覧経路が403。
  7A確認内容を現在の詳細規約再確認済みとは呼ばない。

詳細な契約・保存条件と厳密なFree境界は再確認が必要。Live前に
`--reviewed-free-window-and-terms`の明示確認を要求する。真正性・モデル承認の自動検証ではない。
さらに直近12週と730日より古い日を保守的に拒否するが、これ自体は権限の証明ではない。
公式上限が5回/分より厳しくなった場合は実行せず、間隔を見直す。

## 実行方法・予算

Pythonプロジェクトディレクトリで通信なしpreflight：

```bash
python -m examples.validate_delayed_replay_live \
  --start 2026-05-01 --end 2026-06-01 --master-date 2026-05-07
```

日付・条件確認後だけ`--live --reviewed-free-window-and-terms`を明示。
上の日付は今回の候補例であり、将来も使える既定値ではない。
APIキーは`JQUANTS_API_KEY`のみ。別Provider・有料化・別銘柄へのfallbackはない。

順序はmaster（code/date指定）→限定日足→カレンダー、paginationは逐次。
全endpoint合計20 HTTP試行、20分。1page最大3試行。Retry-Afterを優先し、
残り時間を超える待機を短縮して再送しない。redirect禁止・SDK内部retry全0。
既存private `_configure_official_client_transport`を再利用し、SDK request/retry loopは呼ばない。
ClientV2更新時はSession・base headers互換性の再検証が必要。
POSIX main threadタイマーでsocket/body読取・retry待機を打ち切る。
機構を使えない環境や既存process timerがある場合は通信前にblocked。
socket timeoutは最大30秒かつ残り予算以下。4MB/page・1000行/endpointにも上限。
一回のprobe全体の予算であり、手動再実行で無断拡大してよい意味ではない。

## 保存・情報境界

CLI保存先はGit除外の`.delayed_replay/stage7b/<unique-run>/`。
報告は時刻・要求範囲・件数・固定理由・依存version・hash・endpoint識別・試行数。
APIキー・認証header・例外全文・URL query・個人絶対パス・価格値は報告へ出さない。
Raw/capture・DBをcommit、PR、公開CI artifactへ掲載しない。

完了endpointだけを保存。pagination途中失敗の価格は保存せず、完了済みmasterだけが残る場合がある。
`acquisition_complete`は日足pagination完了であり、市場session充足は独立のcalendar判定。
calendar拒否時も独立した保存検証は継続できる。
temp→fsync→排他的link→directory fsync→再読込hash確認。既存ファイルの上書きなし。
原応答の正規化capture、DailyOpenObservation、7A InputPacketをhashで結び付ける。

reported行が2行以上で価格契約を満たす場合だけ、ローカルで2区間の7A InputPacketへ分割。
同値再publish・再読込でsnapshot bytes/hash不変を確認する。
これはInputExtensionEventによる口座再開の代替成功ではない。口座接続は日足Open Gateで止める。
人工Fill証拠を実データへ足さない。

実公開時刻が不明なため、全日足の`market_available_at`を**実取得時刻**へ保守的に置き、
それ以前のviewには出さない。9時・終値確定時刻・過去の公開時刻を捏造しない。
取得後アーカイブ検証であり、歴史上の公開タイミングや日々の追加公開を観測した試験ではない。
既存口座・Selector・ProtocolJudge・登録候補を呼び出さない。

## テスト

新規人工42件は段階6network guard付き。通常CIでは新Live 1件がskip。
既存Live 6件と合わせて7件skipとなり、Live成功件数へ含めない。

新Liveテストだけを実行する場合（日付と条件を再確認すること）：

```bash
RUN_LIVE_JQUANTS_TESTS=1 RUN_LIVE_STAGE7B_TESTS=1 \
STAGE7B_START=2026-05-01 STAGE7B_END=2026-06-01 \
STAGE7B_MASTER_DATE=2026-05-07 STAGE7B_REVIEWED=1 \
python -m pytest -q tests/test_delayed_replay_stage7b_live.py
```

既存Live一括起動は20試行予算に含められないため行わない。
opt-inありの通信失敗は失敗でありskipへ変換しない。キーなしは未実施skip。

## 正式登録前に残る項目

- キー・実権限・現在の規約／保存条件と対象期間確認、限定Live。
- 価格basis・日付・品質・保存の実データ検証。
- 銘柄別有効単元、カレンダーの日付・時刻の根拠。
- 約定モデル承認（daily_open_proxyを自動採用しない）。
- 未確定運用値・正式登録条件。20万円／100株を勝手に変更しない。
- 実データInputExtension・再開、実改訂・企業行動、7A新台帳→Stage5 lineage接続。

今回の成果はオフライン基盤の実装・検証。Live Acceptance、正式OOS、戦略PASSではない。
段階8・Broker・正式登録・OOS開始・PR／mergeは実施しない。
