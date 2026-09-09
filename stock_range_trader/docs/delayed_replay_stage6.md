# 段階6：Offline E2E Acceptance

基準は`8489bc3f0c4bf61d2b451662f6a35c1de62979f4`、同一ブランチ
`codex/phase3-walk-forward-validation`。既存1016件を維持し、testsと文書のみを追加する。
Production、公開Interface／Schema、既定設定、価格契約、選択順位は変更しない。
これは人工試験の接続確認であり、正式登録、実OOS開始資格や戦略の有効性を意味しない。

## 既存保証・追加接続・保証対象外

| 契約 | 既存保証（tests内） | 段階6の追加接続 |
| --- | --- | --- |
| Draft・時計・Snapshot不変性／hash | `test_delayed_replay_contracts.py`のmutation／digest／clock試験 | 同じSnapshotの実消費、候補を再生前に別APIで生成 |
| transaction・破損・head・ID | `test_delayed_replay_recovery.py`、`test_delayed_replay_audit_store.py` | 実口座cursorから復旧しCheckpoint・出力まで比較 |
| 会計・予約・費用・冪等性 | `test_delayed_replay_account*.py` | 全実イベントの現金／数量と非ゼロ費用を独立算術で照合 |
| 月次変更・旧Position EXIT | `test_delayed_replay_stage4_integration.py` | 3か月から候補・結果CSV／JSONまで一経路、旧EXIT hash照合 |
| 未来Provider・未知Provider・企業行動・Test-only銘柄 | `test_monthly_cutoff_before_provider_and_cohort` | 既存5種mutationを維持。別runの未来価格変更を実会計・1M・3Mまで接続 |
| Close前OHLCV非公開 | `test_close_publication_and_legacy_same_day_exclusion` | 既存公開viewを通した実Signal。Stage1の当日全除外契約は変更しない |
| 翌Openと前日注文 | 段階3 sizing／段階4 Open待機試験 | 翌Open急騰で前日注文・数量・予約は同一、Fillは拒否へ変化 |
| OOSと独立Validationの分離 | `MonthlySelection.evaluate`は口座引数なし | Openだけを変えて共有口座状態が変わっても全SelectionEpochは同一 |
| 入力shuffle・競合順位 | 段階4 shuffle試験 | 入力行・Universe・calendarを逆順化した別出力先でhead／全成果物も一致 |
| 候補なし・欠損・停止 | 段階4 boundaries／integration | 実適格性不足月・既受付BUY・旧EXITを最終Checkpointへ接続。固定Snapshot公開待機から3Mへ復旧 |
| unsupported企業行動・破損時の保有維持 | `test_unsupported_held_action_stops_preserving_position`、`test_corrupt_hash_resume_rejected` | 既存試験を維持。破損台帳から無理に成果物を作らない |
| Judge数値境界・MDD不使用 | `test_delayed_replay_stage5_judge.py` | 全境界の複製はせず、欠測優先順位7ケース＋期間内PENDINGをReportへ接続 |
| 最終化・1M保存 | Stage5 ledger／judge試験 | 実1Mの保存後に再開し、祖先headの証拠を3M Reportへ引き継ぐ |
| Artifact hash・非上書き・失敗cleanup | Stage5 artifacts試験 | 実台帳head／口座不変、再実行禁止監視と実bytesのhash確認 |
| 実Open証拠・追加入力・Live・正式登録 | 未対応または未検証 | 今回も保証しない。Draftの未充足理由を維持 |

段階2の全障害パターンや段階5の全閾値を別名で複製しない。
source／policy／catalog／snapshot／Universe差替え拒否と、同一ID異内容拒否は既存回帰へ委ねる。

## 実際の経路・mockの範囲

`fixture_plan()` → `PriceSnapshot`／`MarketView`／`ReplayClock` →
`MonthlySelection` → 実`ExecutableOutcomeEvaluator`／`ExecutableCandidateSelector` →
`SignalAdapter` → `ReplayEngine`／`ReplayReducer`／`AccountReducer`／実Fill →
`EventStore` SQLite → `LedgerCheckpointSource` → `ProtocolJudge` → 既存成果物出力。

`RegistrationInputs`から独立生成した候補を再生前に出力する。口座streamや損益は候補入力にしない。
未承認の運用値は人工設定と区別し、`unresolved_operational_fields`へ明記する。
歴史sourceの承認証拠・Git証拠は捏造せず、空参照／`git_unavailable`として残す。

主要計算の返り値をmockしない。patchの用途は通信拒否、確定済み選択の再実行禁止、
Report中の副作用禁止、実Fill計算後の障害注入のみ。
Open途中試験では最初の実Fill後、2つ目の計算で例外を注入し、全batchのrollbackを確認する。
Reducer例外は既存どおり`InvalidEvent(reducer_rejected_transition)`へ安全化される。

## 人工正常系の実測

3暦月、21明示session、2銘柄、2候補。選択はslow → slow → fast。
BUY 8件、SELL 7件、完了episode 7件、月跨ぎ完了2件、期末未決済1銘柄。

| 区間 | 最終評価session | Equity | Return | 約定銘柄 | 完了episode |
| --- | --- | --- | --- | --- | --- |
| 1M `[2024-06-01, 2024-07-01)` | 2024-06-28 | 227,859円 | 13.9295% | 2 | 2 |
| 3M `[2024-06-01, 2024-09-01)` | 2024-08-30 | 324,487.89円 | 62.243945% | 2 | 7 |

最終判定は`INCONCLUSIVE / insufficient_sample`。20銘柄かつ100完了取引は未達。
初期資金20万円・単一口座を維持し、PASSのための閾値変更やepisode改変は行わない。
この人工価格の収益は実市場成績ではない。

## 独立会計照合の範囲

各保存イベントを実reducerで復元するが、照合の算術にはProductionのcost／集計関数を使わない。
テスト側Decimalで初期cash − BUY gross/fee ＋ SELL gross−feeを計算し、現金を照合する。
明示Open・Slippageから1銭単位の価格丸め、約定株数からgross、料率から手数料を計算する。
非ゼロ費用variantはcommission=0.001、slippage=0.001、予約buffer=0.1を明示した人工試験。
既存正常fixtureの費用0を変更せず、手数料とSlippageの二重控除を検出する。

予約・売却代金holdはcashから支出扱いせず、active注文予約＋holdをreserved_cashと照合する。
cash＋保有markでEquityを照合し、available_cash非負、BUY−SELL株数とPositionを確認する。
episode ID／entry・exit注文ID・数量・candidate・EXIT設定hash・両側費用を照合する。
期末未決済はEquityへ含め完了数から除外。Checkpointは実Fill日を半開区間で絞って別途数える。
全戦略や全丸めpolicyの再実装はせず、一般のsizing・売買規則・他の丸め契約は既存単体テストへ委ねる。

## 中断再開・決定性

- 8月選択／Open／mark／decideのcommit後receipt喪失：同じcommandの再送で追加eventも二重効果もなし。
  保存済み選択を再評価しないspyの下で、実Runnerの残りを実行し全業務状態・Checkpoint・成果物を比較。
- Open batch内の例外：cash／Position／予約とcursorを同時rollbackし、復旧後は連続runと一致。
- 別event IDで同じOpenを再送：監査eventは1件増えるが、業務状態・標本数・Equity・判定に二重効果なし。
- 子プロセスを8月選択commit直後に`os._exit(23)`で終了、別Pythonプロセスで復旧する。
  終了コード・再選択禁止・最終head・口座・全CSV／JSON一致を確認。物理電源断試験ではない。
- 1M先行確定：既存証拠を`previous={1: one}`として引き継ぎ、証拠の黙った上書きを拒否する契約を維持。
- 固定Snapshotの取得公開を遅延させてmark待機→復旧。部分markでEquityを確定せず、確定markは21回だけ。
- 一時成果物書込み失敗：完成bundleなし・一時領域cleanup・台帳不変。既存出力への上書きも拒否。

同一入力・ID・clockでは論理event・head・全CSV／JSON bytesを比較する。SQLiteのbytesは比較しない。
再送の追加監査eventや1M先行head、待機再開の異なるreplayed_atで証拠hashが変わる場合は、
その違いを隠さず、口座・測定値・判定を比較する。取得日時等をむやみに正規化しない。

別runの未来価格変更では全入力hashが変わる。過去prefixの比較で除外するのは
`snapshot_hash`、`batch_hash`、`operations`内のprovenance digestのみ。
数量、cash、費用、日時、Candidate、判断理由、Risk、戦略状態は除外しない。
8月価格変更でそれ以前のprefix／1M不変、3M Equityは変化するpositive controlを確認する。

## 成果物・オフライン性

ファイル名／Schemaを既存ソース定数と照合。候補と結果を分離し、実bytesからSHA-256を再計算する。
自己hashは含めない。価格lane名`raw_ohlcv`は公開規約として許容するが、Raw観測値本文は保存しない。
絶対パス・秘密値・Raw市場データを出力しない既存契約を維持する。

固定正常fixtureのローカル実測hash（正式登録IDではない）：

| 対象 | SHA-256 |
| --- | --- |
| 候補payload（IDは`registration-candidate-`＋この値） | `8f74d4dd0ae1d213e356f1aaf32c5cc01a841b01918cbb3d2da90ff74bc9684e` |
| checkpoints.csv bytes | `18bdd8256b3af6d17cf35c450f446cbe95e8f45a4bdd8dee72de6be04b49de70` |
| protocol_result.json bytes | `0ec1c4b709ea4f9b296a061c5b0a1e331263f93dbb0676b32cf771f4628c92a1` |

上記値への固定assertではなく、各実行のbytes／正規化payloadからの独立再計算、
および同じ固定入力の反復・復旧間の一致を試験する。異なる依存バージョン間のhash同一性は保証しない。

ハーネスの再生・収集・出力、各接続テスト、子プロセス内で通信拒否を適用する。
J-Quants価格／Universe／calendar、yfinance価格取得入口、requests、urllib、urllib3、
curl_cffi Session、IP socket connect/connect_ex/sendto、名前解決を拒否する。
AF_UNIXのローカルIPCは維持する。ガードの自己試験は呼出しを即拒否し、実通信しない。
任意の新C拡張・別外部コマンドまで隔離するOS firewallではない。実装の利用経路に対するガードである。
git pushとGitHub Actions確認はテストプロセス外の許可された操作として分離する。

## 検証・残る制限

追加は正常5件、復旧8件、整合性12件の計25件。既存1016件と合わせ1041件、
既存Live opt-in 6件はskipのまま（未検証）。全pytest・Ruff・diff検査と対象SHA CIを完了報告で示す。

```bash
RUN_LIVE_JQUANTS_TESTS=0 RUN_LIVE_YFINANCE_TESTS=0 python -m pytest -q
ruff check .
ruff format --check .
git diff --check
```

Production defect補正なし。追加テストでは公開lane名とRawデータ本文の区別、
Reducer例外の安全化という既存契約に合わせて検査を調整した。正常系の期待収益は変更していない。
未承認値、実Open証拠・追加入力未対応、Live価格基準・単元・calendar未検証を維持する。
正式登録／OOS開始、段階7、Broker、最適化、PR作成・merge、Node.js保守は行わない。
人工E2E成功を実OOS開始資格へ昇格させない。物理停電・強制終了時の成果物lock自動回収は保証しない。
