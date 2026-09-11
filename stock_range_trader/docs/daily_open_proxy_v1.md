# daily_open_proxy_v1 — オフライン研究モデル

基準は `7a1d62be6e8500d5a7b01440523beb2b1f37741a`。既存公開Interface・既定設定を変更せず、
`delayed_replay/proxy/` に別経路を追加した。保存済み72030価格の清算、追加市場API通信、
運用値の採用、正式登録、OOS開始は行っていない。これはモデル承認でも戦略PASSでもない。

## 新旧Interface

| 既存 | Proxy接続 | 変更しない契約 |
| --- | --- | --- |
| AccountPolicy / size_buy | 明示termsから算術adapterを構築 | Decimal丸め・費用込み数量・予約計算のみ共用 |
| SignalAdapter | フェーズ制限した人工OHLCVを渡す | 戦略・Candidate設定・旧PositionのEXIT設定を維持 |
| 月次Selection adapter | 月初境界より前の人工Canonicalのみ | 選択用口座を継続口座へ持ち込まない |
| EventStore / recovery | ProxyReducer、独立DB・draft_audit stream | 既存transaction / CAS / hash鎖 / 再送検証を共用 |
| 7A不変入力・追加手順 | ProxyInputStore / ProxyPacket / extension | 専用lane。旧InputPacketの価格契約を拡張しない |
| ExecutionOpenEvidence / Gate | 接続しない | 日足を実約定証拠や旧synthetic証拠へ詰め替えない |
| LedgerCheckpointSource | 接続拒否 | 既存正式CheckpointへProxy損益を流さない |

`DailyOpenProxyPolicy`、`SyntheticRecipe`、`ProxyPacket`、`FrozenProxyOrder`、
`ProxyResolution`、`ProxyPlan`、`ProxyService` が新しい入口。
schemaは `proxy-input-lane-v1` / `proxy-plan-v1` / `proxy-state-v1` /
`proxy-event-v1` / `proxy-resolution-v1`、reducer identityは `daily-open-proxy-reducer-v1`。
外側の汎用EventStore envelopeは既存v1のまま。旧streamへのmigrationなし。

## 価格観測と研究上の仮定

metadataは `model_id=daily_open_proxy_v1`, `mode=research_only`,
`model_approval=unapproved`, `registration_status=draft_not_registered`,
`fill_kind=simulated_fill`, `actual_trade_at=null`。入力provenanceは人工生成。
モデル・費用・availability・入力・Candidateのhashを台帳identityへ保存する。

将来のProvider報告日足Openは価格の観測事実であり、所定数量の売買成立は別の仮定である。
今回の価格はすべて決定的な人工生成値であり、Provider実応答ではない。
明示仮定は、全量を代理価格＋Slippageで清算できること、待ち行列・価格影響・部分約定・
寄付き参加を再現しないこと、実時刻・実数量を証明しないこと、日次出来高は
事後的な矛盾検査で寄付き流動性を保証しないこと。全てidentityへ保存する。

初期資金20万円、単一共有口座、100株、Long Onlyは固定。
その他の値には運用既定値を設けない。DraftのNoneは保持できるが実行検証で拒否する。
terms/rulesの省略、未知費用モデル、未知価格basis、時刻条件は拒否する。
テストは1銘柄40%・最大2枠・手数料0・Slippage0・buffer0を明示し、
別の算術試験で手数料1%・Slippage2%・buffer5%を明示する。どれも運用承認値ではない。
配当は除外。企業行動の復元・株数変換は行わず、非単位係数は停止する。

## 4補正と情報公開順序

1. D清算用の注文はDより前に固定する。一方、DのClose確定後にDまでのOHLCVから
   D+1注文を作ることは許す。D終値変更で翌SELLのみが変わるpositive controlを持つ。
2. D最終Volumeは清算側が全対象数量を検査するために使い、Fillより先に検査を完了する。
   前日判断へDのOHLCVを渡さない。D終値後の翌注文判断ではDまでのOHLCVを利用する。
3. 実約定時刻不明そのものは拒否理由ではない。今回対応は明示 `time_condition=none` のみ。
   未対応の時刻制約を無視せず入口で拒否する。
4. Proxy専用Gate・型・streamで隔離する。`offline_synthetic` 以外は拒否する。

日次phaseは select → resolve → mark → decide → finish。
月初selectは境界より前のみ、decideは当日を含む終値確定履歴のみ。
明示カレンダーに対する履歴欠測を補間・営業日推測で埋めない。
Signal pipelineは既存契約の列だけを受け取る独立コピーで、入力snapshotを変更できない。

各入力行は元packet hashと `first_observed_at` / `fetched_at` を保持する。
新規人工行では両方が明示 `acquired_at` と同じ時刻。後日の未取得値補充では初回観測時刻を維持し、
fetched_atだけを新packetの取得時刻に更新する。取得時刻は生成元identityと分離し、元packetは不変。
Canonical adapterも取得値を維持する。
各phase監査には `session_date`, `replayed_at`, `modeled_available_phase`,
`availability_model_hash`, input head / snapshot hashes, `actual_trade_at=null` を記録する。
取得済みでない入力は再生拒否。`fetched_at <= market_time` は要求しない。
汎用auditの `market_decision_at` はsessionのUTC午前0時を単なる日付anchorとして使用し、
市場公開・判断・約定時刻と解釈しない。順序はphaseとevent sequenceで表現する。
Frozen注文の `equity_at` も `YYYY-MM-DD:after_close` という論理評価時点であり実時刻ではない。
過去9時へのtimestamp置換、実約定時刻の捏造はない。

## 数量・品質・会計

前終値・確定Equity・available cash・費用・bufferから100株単位の最大数量を固定する。
Candidate/EXIT hash、対象日、参照価格、予算、予約、policy hash、優先順位を凍結する。
順位はSELL優先、Score降順、銘柄順。監査Scoreは12小数桁へ明示的に正規化する。
翌Openで数量を増減しない。追加BUY・部分EXITなし、SELLは全株を予約する。
月次選択は既存PositionのCandidate/EXITや注文を書き換えない。

全instrumentの品質検査後、固定batch hashに対するresolution計画を作り、一括commitする。
Volume=0はno_trade、Open/Volume不足はwaiting、日次Volume不足は当該銘柄全batch拒否。
BUY/SELLを方向相殺せず数量を合計する。出来高に合わせた縮小・注文選別なし。
全日停止の明示＋Null Openならno_trade可、正のOpen/Volumeとの矛盾はstopped_contract。
一時停止・初回約定遅延は時刻条件なしの明示policy下のみ許可し、停止情報unknownを保持する。
実株数Volumeの単位不明は拒否。HLCが揃う場合の不整合も停止する。
Close欠測なら清算後markで待機できるため、HLC全部が揃うまで必ず清算を待つ契約ではない。

BUY=Open×(1+Slippage)、SELL=Open×(1−Slippage)。価格丸め後にgrossとcommissionを算出する。
bufferは予約余裕であり実費に加算しない。予約・予算超過は全量拒否、他注文から補填しない。
SELL代金は同batch BUYへ使わず、mark完了後の次判断から利用する。
`available_cash=cash−reserved_cash`、`equity=cash+保有時価`。
売却代金拘束・予約・手数料・Slippageを二重控除しない。新しいDD停止・合否閾値は追加しない。
予定終了は時価評価のみ、強制決済なし、`no_next_bar_no_forced_exit`。

## 永続化・復旧

入力はtemp→fsync→排他的link→directory fsync→hash再読込後にDBへ受理する。
extensionは親input headを照合し、version/hash鎖を追記する。既知値の改訂や消費済み過去補充は
隔離して口座入力を変えない。未処理日の未取得値の補充だけを許可する。
ファイル公開後DB未確定なら孤立ファイルは可能だが、未受理入力を口座に使わない。
通常APIでの人工由来をrecipe・生成器・TEST_A/TEST_B限定・再生成一致で検証する。
これは悪意ある手動改変の完全な真正性証明ではなく、実snapshotの誤接続防止である。

清算計画、注文状態、予約解除、cash/Position、phase cursor、監査は同一DB transaction。
同じevent IDの再送と別event IDの同じbusiness ID再送は二重清算しない。
同一business ID異内容、stale head、訂正上書きは拒否する。
再開は元入力を再読込・hash検証し、純粋reducerで監査を検証する。
決定済みの選択・Signal・resolutionを保存したcommandから復元し、Evaluatorを再実行しない。
source、policy、model/availability、初期packetの不一致は拒否する。

待機は元対象日のまま数量・予約・cursorを維持する。待機時間で注文を失効させない。
今回 `wait_without_expiry` / `explicit_finalization_not_implemented` の明示指定のみ対応する。
清算後のClose不足はFillを維持し、新BUYの予算を作らない。補充後markから再開する。
過去履歴の不完全性は例外でfail-closedとし、既に消費した過去データを補充して再計算しない。
実証拠・明示期限に基づく最終化/失効は将来の別実装であり、今回の暗黙fallbackではない。

## 人工テストと限界

`python -m pytest -q tests/test_daily_open_proxy.py` はnetwork guard付きの70件。
実store/reducer/既存SignalAdapter・月次選択を用い、主要計算をmockしない。
基準人工E2Eは12注文のうち8 simulated fills（4 BUY・4 SELL）、4拒否、4完了episode。
手数料0の明示fixtureで20万円→232000円、実現差額32000円を台帳と手計算照合する。
これは人工算術の検証値であり、実相場成績・モデル優位性の根拠ではない。
欠測追加／Close待ち／中断再開で連続実行と業務状態一致、監査prefix不変を検証する。
commit前障害はrollback、commit後応答消失は再送・復元で二重支出を防ぐ。
入力改訂隔離・ファイル欠落/改変・未知時刻条件・単位・型・Checkpoint拒否も回帰対象。

実データ実行には別途、モデルと運用パラメータの明示承認、価格basis/企業行動・配当規約、
銘柄別有効日付き単元証拠、公式カレンダーcoverage、停止・無取引・欠測の証拠と解決規則、
実入力adapterと追加入力・再開の検証が必要。単元100株は人工fixtureでは定義されるが、
実72030の単元を今回証明したことにはならない。有効日付き発行体/取引所根拠との接続が必要。
実市場時刻・日足公開時刻も今回未検証。時刻依存policy導入時には対応する根拠と検証が必要で、
日付カレンダーや日足Openから実寄付き/約定時刻を推論しない。

将来Checkpointへ接続する場合もresearch_only / simulated_fill / model version/hash /
availability hash / provenance / approval / registration /価格・費用policyを必須保持する。
旧1M台帳へ追加せず独立lineageで接続する。現在は正式Checkpointもformal OOSも拒否する。
既存層・Gate・Live設定・公開CLIは変更していない。Liveは今回未検証であり成功扱いしない。
