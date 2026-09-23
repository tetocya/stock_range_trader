# 正式Real OOSに向けた標本収集能力の事前実測（F0〜F2）

状態: **事前実測の計画とオフライン基盤のみ**。正式Real OOSの登録、登録Manifestの生成、epochの開始、
共有口座の動的実測（F3）、市場APIからの実取得は行っていない。

## 参照するプロトコル

- 参照元はリポジトリ外の草案 `real_oos_protocol.md`（revision `draft-0.3`、2026-09-07、
  「DRAFT — NOT REGISTERED」、ファイルSHA-256 `ac731c2b9a89552d934ebd852c7c1942a4f67a4e357c92c97fa2388ab8a94d5f`）。
- この文書と実装は草案を正式仕様として扱わない。草案の文言と差がある場合は本書の「未決事項」に置く。

## 目的と範囲

登録前に、下記の条件で3か月の評価期間に「20ユニーク約定銘柄・100完了取引」（草案§6）を集められるかを
運用上の実行可能性として調べる。収益率の最適化ではない。

| 段階 | 内容 | 本PR |
| --- | --- | --- |
| 第1段階（静的） | 固定基準日Rで、100株を1単元買える銘柄と、その他の条件を個別に数える | F1・F2を実装（人工データのみで検証） |
| 第2段階（動的） | 独立した過去期間で、月次Candidate選択から共有口座の完了取引までを数える | 対象外（正式仕様の確定後にF3） |

第1段階の結果は、将来の約定銘柄数・完了取引数の推定ではない。

## 条件の3区分

| 区分 | 内容 |
| --- | --- |
| プロトコル草案で採用済み | 初期資金200,000円、単一共有口座、100株単位（S株なし）、月次Candidate再選択 |
| 第1段階で評価する暫定条件 | 1銘柄上限10%、最大5銘柄（`CensusTerms.status="provisional"`） |
| 正式登録前の未決事項 | 手数料、Slippage、予約buffer、価格・金額の丸め、単元株数の証拠、Universe定義と流動性基準、企業行動・欠測・売買停止の扱い、約定モデル、DD停止、Candidate選択機構（Validationの約定モデル） |

費用・bufferは設定値として受け取る。条件の承認状態と、銘柄ごとの単元株数の検証状態は別々に出力する。

- `terms_approval`: `provisional`／`owner_approved`。後者は承認参照（`approval_reference`）が記録されたことだけを示し、
  コードは承認の真正性を検証しない（`authenticity_verified_by_code=false`）。コードは承認を作らない。
- `lot_evidence_status` と銘柄別の `c_lot_basis`（`verified_lot`／`assumed_lot_unverified`）。
- Cの件数は `c_purchasable_verified_lot`、`c_purchasable_assumed_lot_reference_only`、
  `c_not_evaluable_lot_unknown_or_unsupported` に分ける。単一の確定的な購入可能件数は出さない。
- `result_kind` は、暫定条件なら `reference_only_provisional_terms`、承認参照があっても単元株数が
  未検証・不明を含めば `reference_only_unverified_or_unknown_lots`、両方そろった場合だけ
  `census_recorded_owner_terms_and_lot_evidence`。LotEvidenceの出典も構造的な記録であり、原資料の真正性は検証しない。

**用語**: 本プロジェクトの「Candidate」は戦略パラメータ候補（baseline等）であり、銘柄ではない。
出力の銘柄は `jquants_code`／「銘柄（instrument）」と表記する。

## 第1段階の集計定義（基準日R）

| 記号 | 定義 | 実装 |
| --- | --- | --- |
| A | R時点のJ-Quants銘柄マスタで商品区分`011`かつ市場`0111/0112/0113`の国内普通株。yfinance ticker未解決も含め、別フラグで記録 | 既存 `build_japanese_equity_universe` |
| B | Rの調整前終値（`C`）が正で、単元株数が確認済みまたは仮定100株 | 単元株数の状態: `verified`／`assumed_100_unverified`／`unknown`／`unsupported_lot_size` |
| C | 1単元の必要額 ≤ 建玉上限（下の計算順序）。Rが取得済みと宣言された日でなければ判定しない | Bでない銘柄は理由付きで非該当 |
| D | Rで終わる直近N sessionが欠けずに揃い、調整済みOHLCVから既存の指標パイプラインで SMA・ATR・ADX・Range Score がRで有限 | 企業行動は調整済み系列で尺度がそろうためDを妨げない |
| E | 直近N sessionにデータ上の事象がある: 取得対象外の日、取得済み日に行がない、OHLCのNull（無取引か売買停止かは日足から判別不能）、調整済み値の欠落、出来高の欠落・0、`AdjFactor`の欠落・≠1、`ExRT` | 同じ行の事象は打ち切らずすべて記録。種類別の銘柄数・件数、銘柄ごとの事象の組み合わせ件数、複数事象を持つ行の数を出力 |
| F | 同じN sessionの行を現行の `jquants_daily_to_canonical`→`validate_canonical_bars`→`canonical_to_phase1`→`validate_backtest_price_contract` が受理する | 行の欠落（日付の穴）は現行処理が検出しないため `f_notes` に記録 |

C〜Fは連続した除外段階ではない。各条件の件数、C〜Fの組み合わせパターン件数、主要な重複
（例: 価格上限超過かつ履歴不足）を個別に出力する。E・Fの結果を理由に正式Universeから除外する規則は新設しない。

### 1単元の必要額の計算順序

既存の遅延再生 `size_buy` の1単元分と同じ順序（テストで一致を確認）。

1. 上限 = 丸め(初期資金 × 1銘柄上限, 金額quantum, budget_rounding)。R時点は保有なしのため現金上限と同額
2. 単価 = 丸め(Rの終値 × (1+Slippage) × (1+buffer), 価格quantum, buy_price_rounding)
3. 約定金額 = 丸め(単価 × 100, 金額quantum, amount_rounding)
4. 手数料 = 丸め(約定金額 × 手数料率, 金額quantum, fee_rounding)
5. 必要額 = 丸め(約定金額 + 手数料, 金額quantum, reservation_rounding)
6. 丸め後の単価・約定金額・必要額がすべて正で、必要額 ≤ 上限 なら購入可能
   （0以下になる設定は `nonpositive_rounded_unit_or_amount`。`size_buy` も0株を返す）

## 情報時点とデータ来歴

- 日足は `acquired_daily_dates` として宣言された日の行だけを使う。宣言外の行は使わず、日付別件数を
  `acquisition_consistency.rows_outside_declared_acquisition` に残す。Rが宣言されていなければ、Rの価格があっても
  購入可能判定に使わない（`reference_date_not_acquired`）。宣言日のうちsessionでない日、行が一つもない日も記録する。
- 集計はR以前の行だけを使う。Rより後の行は数えて無視し、件数を `future_rows_supplied_and_ignored` に残す。
  Rより後の価格・企業行動を変えても銘柄別の結果と件数が変わらないことをテストで確認している。
- J-Quantsの調整済み系列は取得時点の遡及調整値であり、当時観測された系列ではない
  （`adjusted_series_basis=provider_adjusted_as_of_retrieval`）。過去日付の応答を当時のオリジナルSnapshotとは扱わない。
- 出力には、基準日R、履歴windowの範囲、実際に使った市場データ期間、取得元・取得日時・plan hash、
  入力のhash（銘柄マスタ、使用した日足、session、単元株数の証拠）、単元株数の情報源と状態、暫定の費用・資金設定、
  過去時点の再構成に用いた仮定、既閲覧期間との重複を記録する。
- 単元株数: J-Quants銘柄マスタに単元株数の項目がない。歴史的な証拠がない場合は `assume_100_unverified` の
  参考集計とし、未検証件数を `lot.unverified_count` に記録する。

## 対象期間の決定方法（第2段階の準備を含む）

- 基準日Rと評価区間は、取得前に規則で固定し、plan hashで記録する。
  例: 「取得日時点のFree提供範囲に warm-up・Validation 3か月・評価3か月が収まる全四半期開始月」。
- 評価区間どうしは重ねない。最後の区間を確認用として事前に指定する。
- 既閲覧の市場期間（例: 限定Proxy試験の2026年5月・6月）は除外せず、`viewed_periods` として重複を記録する。
  限定Proxy試験の結果に合わせてUniverse・期間・Candidate・資金上限を選ばない。
- 事前実測は正式Real OOSとは別の実験・別の成果物として扱い、閲覧した市場期間を未開封の正式期間として扱わない。

## 出力先の保護

- 出力できるのは、このcheckoutのGit除外領域 `stock_range_trader/outputs/feasibility/` とシステムの一時ディレクトリの
  配下だけ（呼び出し側は範囲を狭めることだけができる）。
- 相対パス、`..`、symlink・別名（正規化パスと実パスの不一致）、隠し名、既存パスを拒否する。
- 祖先に `.delayed_replay` を持つ木、および最も近いGit checkoutがこのcheckoutと異なる場所（6月限定試験の
  worktree全体を含む）を拒否する。ディレクトリ名の部分一致を主要な仕組みにしない。
- Storeの新規作成と再開の両方に同じ検査を適用する。ディレクトリは排他的に作成した直後に再検査し、
  ファイルは `O_EXCL | O_NOFOLLOW` で作成する。
- 残る制約: 検査と作成の間に祖先ディレクトリを書き換えられる並行プロセスは完全には排除できない
  （ディレクトリfd相対I/Oが必要で、今回は対象外）。

## 取得（F1）の境界

- `feasibility.acquisition` は日付指定の `/equities/master`（`date`）、`/equities/bars/daily`（`date`のみ、`code`なし）、
  `/markets/calendar`（`from`/`to`）だけを扱う。
- plan（対象日・API・範囲・上限）を一度だけ書き、台帳は追記専用のhash鎖。送信前に試行を記録し、
  ページ数・件数・受信日時・応答hashを残す。request数・所要時間・保存容量・ページ数の上限を強制する。
- ページ送りの重複・ループ、行の重複、要求日と異なる行、未対応スキーマで安全に停止する。
  再開時はplan・台帳・応答ファイルの一致を検査する。終端の停止は自動再開しない。
- 台帳に秘密情報、ヘッダ、例外メッセージを保存しない（例外はクラス名のみ）。
- Null行は保持し、銘柄をまとめて除外しない。
- 所要時間の上限は、待機前・待機後・送信直前に検査する。待機が予定より長くなって期限を超えた場合は送信しない。
- 応答は項目ごとに型と値を検査する（日付、5桁の銘柄コード、文字列項目、数値の型・有限性・正負、`HolDiv`、`ExRT`）。
  不正値は例外で落ちずに `invalid_response_value:<項目>` として台帳に記録して停止する。
- 完了済みplanの再実行では完了イベントを追加しない。

クラッシュ地点と再開時の扱い:

| クラッシュ地点 | 再開時の扱い |
| --- | --- |
| 送信前の試行記録後 | 試行は予算消費として残る |
| 応答ファイル保存後・page記録前 | 参照のない応答を孤立ファイルとして `orphan_detected` に記録。容量は保存容量の予算に算入し、同一内容の再取得時は既存ファイルを再利用（上書きしない） |
| 最終page記録後・query完了記録前 | 再取得せずに完了を記録 |
| 台帳・plan・応答の改変、非正規ファイル・symlink、別plan | 再開を拒否 |

**実取得は本PRから開始できない。** 取得の入口は `run_offline_fixture_acquisition` だけで、
具象クラス `OfflineFixtureTransport` の正確な型だけを、Storeの作成・再開より前に受け付ける。
このクラスは呼び出し側が渡した不変の応答値を再生するだけで、呼び出し側のコードを実行しない。
同じプロセス内のコードによる改変（monkeypatch等）までは防がない。
将来の実取得の入口は、別の承認・実行契約を持つ別関数として設計する。その際のHTTP transportの契約案:
各requestのtimeoutは `min(上限秒, 残り時間)`、応答本文の読み込みも残り時間内、`Retry-After` は残り時間内に
収まる場合だけ従い、収まらなければ停止する、SDK内部のretryは無効化して外側の予算だけで数える、認証情報は台帳に残さない。
実取得を行う場合は別作業として、次を確認し所有者の承認を得る。

1. J-Quants V2の最新公式仕様（日付指定の日足で全銘柄が返ること、ページ送り、項目）
2. 実行日のFree提供範囲（2026-09-24閲覧時点で「12週間前〜2年12週間前」）とレート制限
3. 実際のrequest数・ページ数・所要時間・保存容量の見積もり
4. 6月限定試験の保存データ・取得台帳・口座DB・承認記録をコピー・参照しないこと

## 出力

`write_census_bundle` は新しいディレクトリへ `census_symbols.csv` と `census_summary.json` を一括公開する。
出力先には「出力先の保護」と同じ検査を適用し、同じ親の下に作った一時ディレクトリから、公開直前に出力先が
未作成であることを確かめてrenameする。

## 集計値が意味すること・意味しないこと

- 意味すること: R時点で、指定条件のもとで1単元を買える銘柄数と、履歴・データ・現行処理の各条件との重なり。
- 意味しないこと: 評価期間の約定銘柄数・完了取引数・収益、正式Universe、Candidate選択の結果、
  当時の配信値との一致、単元株数の歴史的な検証（仮定した場合）。

資金上限やCandidate選択機構を変えると、標本数だけでなく損益・リスク・戦略全体が変わる。
実測後に設定を変える場合は、変更理由、参照したデータ、影響する仕様、新しい検証区間を記録する。
