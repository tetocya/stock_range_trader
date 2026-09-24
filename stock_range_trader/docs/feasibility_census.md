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
- `lot_evidence_status`（`all_instruments_lot_verified`／`includes_assumed_unknown_or_unsupported_lots`／
  対象銘柄A＝0なら `no_target_instruments`）と、銘柄別の `c_lot_basis`（`verified_lot`／`assumed_lot_unverified`）。
  検証済み・未検証の件数は `lot.verified_count`／`lot.unverified_count`。
- Cの件数は `c_purchasable_verified_lot`、`c_purchasable_assumed_lot_reference_only`、
  `c_not_evaluable_lot_unknown_or_unsupported` に分ける。単一の確定的な購入可能件数は出さない。
- `result_kind` は、対象銘柄A＝0なら条件の承認状態にかかわらず `no_target_instruments_nothing_evaluated`
  （何も評価していない。購入可能Universeの検証完了ではない）。A＞0では、暫定条件なら
  `reference_only_provisional_terms`、承認参照があっても単元株数が未検証・不明を含めば
  `reference_only_unverified_or_unknown_lots`、両方そろった場合だけ `census_recorded_owner_terms_and_lot_evidence`。
  空集合に対する全称判定から肯定的な結論を出さない。LotEvidenceの出典も構造的な記録であり、原資料の真正性は検証しない。

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
- 各許可ルートは「実パス」と「その同じ場所として受け付ける表記」を持つ。一時ディレクトリは
  `tempfile.gettempdir()` が返す表記とその実パスの2つだけを受け付ける（macOSの `/var/folders/...` と
  `/private/var/folders/...`）。別名を信頼するのはこの基点そのものだけで、基点より下の要素はすべて実ディレクトリで
  なければならない。基点以外のsymlink・別名を経由する表記は、到達先が許可範囲内でも拒否する。
  プロジェクトの出力ルートは、それ自体がsymlink経由でない場合だけ信頼する。
- 以降の場所の検査はすべて実パスに対して行うため、信頼した別名から保護対象に到達することはない。
- 相対パス、`..`、基点より下のsymlink・別名、隠し名、既存パスを拒否する。
- 祖先に `.delayed_replay` を持つ木、および最も近いGit checkoutがこのcheckoutと異なる場所（6月限定試験の
  worktree全体を含む）を拒否する。ディレクトリ名の部分一致を主要な仕組みにしない。
- Storeの新規作成と再開の両方に同じ検査を適用する。ディレクトリは排他的に作成した直後に再検査し、
  ファイルは `O_EXCL | O_NOFOLLOW` で作成する。
- 残る制約: 検査と作成の間に祖先ディレクトリ（一時ディレクトリの基点の別名を含む）を書き換えられる並行プロセスは
  完全には排除できない（ディレクトリfd相対I/Oが必要で、今回は対象外）。`TMPDIR` を書き換えられる利用者は基点自体を
  変えられるが、その場合も実パスに対する別checkout・`.delayed_replay`・既存パスの拒否は働く。
  これは誤操作に対する防護であり、完全なファイルシステム分離ではない。

## 取得（F1）の境界

- `feasibility.acquisition` は日付指定の `/equities/master`（`date`）、`/equities/bars/daily`（`date`のみ、`code`なし）、
  `/markets/calendar`（`from`/`to`）だけを扱う。
- plan（対象日・API・範囲・上限）を一度だけ書き、台帳は追記専用のhash鎖。送信前に試行を記録し、
  ページ数・件数・受信日時・応答hashを残す。request数・所要時間・保存容量・ページ数の上限を強制する。
- 保存容量は各送信の前にも検査する。保存済みの応答（孤立ファイルを含む）がすでに上限に達していれば、
  送信を始めずに `storage_budget_exhausted_before_request` で停止する（再開直後も同じ）。孤立ファイルは削除・上書きしない。
  応答受信後の検査（`storage_budget_exceeded`）も従来どおり残す。
- ページ送りの重複・ループ、行の重複、要求日と異なる行、未対応スキーマで安全に停止する。
  再開時はplan・台帳・応答ファイルの一致を検査する。終端の停止は自動再開しない。
- 台帳に秘密情報、ヘッダ、例外メッセージを保存しない（例外はクラス名のみ）。
- Null行は保持し、銘柄をまとめて除外しない。
- 所要時間の上限は、待機前・待機後・送信直前に検査する。待機が予定より長くなって期限を超えた場合は送信しない。
- 応答は項目ごとに型と値を検査する（日付、5桁の銘柄コード、文字列項目、数値の型・有限性・正負、`HolDiv`、`ExRT`）。
  不正値は例外で落ちずに `invalid_response_value:<項目>` として台帳に記録して停止する。
  数値はfloatへの変換で桁あふれする巨大整数（例: `10**400`）、非有限値、型違いを不正値として扱い、0や欠測値に
  置き換えない。Nullは欠測として保持する。Pythonの整数桁数上限を超える数字列を含む応答は `invalid_json_response`。
- 台帳は、空であるか改行で終わること、各行が正規形のJSON（`canonical_json`）であることを、hash鎖の検査とあわせて
  追記前に確認する。末尾の改行欠落・途中切断は `ledger_incomplete_final_line`、行の破損・非正規形・空行は
  `ledger_chain_broken` として `LedgerIntegrityError` を送出し、台帳を補修・上書き・切り詰め・追記しない
  （不完全な台帳には停止記録も書かない）。例外の `evidence` に台帳のバイト数・sha256・完全な行数と位置
  （破損行の番号）を残し、未変更の台帳から同じ結論を再構成できる。検証後に台帳の長さが変わった場合も追記しない
  （`ledger_changed_since_verification`）。
- 完了済みplanの再実行では完了イベントを追加しない。

クラッシュ地点と再開時の扱い:

| クラッシュ地点 | 再開時の扱い |
| --- | --- |
| 送信前の試行記録後 | 試行は予算消費として残る |
| 台帳への1行の書き込み途中 | 末尾が不完全な台帳として再開を拒否（自動補修しない。所有者が状況を確認する） |
| 応答ファイル保存後・page記録前 | 参照のない応答を孤立ファイルとして `orphan_detected` に記録。容量は保存容量の予算に算入し、上限に達していれば送信前に停止。同一内容の再取得時は既存ファイルを再利用（上書きしない） |
| 最終page記録後・query完了記録前 | 再取得せずに完了を記録 |
| 台帳・plan・応答の改変、非正規ファイル・symlink、別plan | 再開を拒否 |

hash鎖は台帳の外に固定点を持たないため、完全な行単位で末尾の記録を削除した台帳は、それ自体からは検出できない。
正式な取得では、台帳末尾のhashを別の記録（receipt等）に残す必要がある（F3以降の課題）。

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

## 後続の事前実測HTTP取得に向けた契約（通信機能ではない）

`feasibility/http_contract.py` は **historical_feasibility 専用の純粋な値・検証契約**を定義する。
取得plan、承認の範囲照合、台帳イベントの状態遷移、累積予算、原本一覧と外部receiptの内容照合を
人工データだけで扱う。HTTP client、認証情報の読込み、ファイル保存・外部固定点への送信、実取得入口はない。
既存 `run_offline_fixture_acquisition` の具象Transport型検査は変えていない。

### 固定planとCalendarの2方式

- `calendar_discovery`: Calendarだけを対象とする独立plan。後続のDaily日付や予算を自動生成・拡張しない。
- `predeclared_daily`: 取得前に出典hash付きの営業日集合を固定し、Calendar・Master・Dailyを対象とする。
- `calendar_anchored_daily`: 別途固定されたCalendar証拠hashから営業日集合を**新planとして**固定し、Master・Dailyを対象とする。

各planはR、対象日付、Calendar出典の参照先とhash、endpoint、逐次ページ送り、最大試行・ページ・時間、転送／展開／保存容量、
1ページ上限、Retry規則、専用出力先、成果物ID、有効期間を正規化JSONのSHA-256で固定する。
重複日付・不正な型・曖昧なtimezone・指定root外を拒否する。日付順序や同じ瞬間のtimezone表記差は
同じhashとなる。出力識別子はこのcheckoutの `outputs/feasibility/http/<artifact_id>` に限定するが、
これは**契約時のパス照合だけ**であり、ファイルの安全な作成は次PRの課題である。
6月試験worktree、`.delayed_replay`、既存DB・台帳・承認ファイルは今回の出力対象にならない。

### 承認・予算・証拠の保証範囲

`OwnerApprovalClaim` はplan hash・範囲・endpoint・予算・出力先・期間と、承認者・イベント・証拠参照の
**自己申告メタデータ**を照合する。既存コードに独立した所有者署名／承認台帳の検証基盤は確認できず、
`check_approval_scope()` は真正性を `False` のまま返す。任意の承認者名や `approved=true`、環境変数だけで
実取得の許可は成立しない。今回、所有者承認資料は作成していない。

`EvidenceJournal` は、送信前の予約、送信、応答、結果不明、原本保存、ページ完了を順序付きhash鎖の
**バイト列契約**として検査する。予約時点で試行数を消費し、429・Retry・結果不明も累積数に残す。
結果不明の転送／展開量は、次PRで実体を照合するまで1ページ上限分を保守的に仮計上する。
`BodyInventory` は孤児・途中ファイルを保存容量へ含めるための申告値で、実ディスクを検査しない。
孤児・途中ファイルが残る間は、容量に余裕があっても次の試行を予約可能と表示しない。
resumeは既存台帳から予算を再構成し、別plan hashを拒否する。完了済みplanは新試行を受け付けない。

`ExternalReceiptClaim` は成果物ID、plan hash、台帳の行数・バイト数・head hash、原本集合hash、
固定時刻・発行主体の**内容一致**を検査する。外部保管先の独立性・署名の真正性は検証しない。
台帳の完全な末尾削除は、独立して固定されたreceiptがなければ検出できない。
`assess_live_acquisition_gate()` は承認・receiptの真正性とHTTP入口が未実装のため常に閉じる。

### 次PRと所有者判断

HTTP・保存PRで、実通信の総deadline、全HTTP試行の逐次rate limit、429の待機、暗黙Retry無効化、
本文の逐次・圧縮前後容量、実ファイルと台帳の照合、fd相対I/O、クラッシュ後の安全な再開を実装・検証する。
J-Quantsの公式レート制限はFree 5回／分のsliding windowで、429には通常 `Retry-After` が付かず、
公式は少なくとも1分（確実性のため約2分）の待機を案内する。契約値としては13秒間隔と429後120秒を
下限にしたが、**今回のコードは待機・timeoutを実行しない**。

所有者はR・対象営業日集合・各予算、承認の独立した真正性検証方式、外部receiptの保管先・署名方式・
固定頻度、実出力rootの運用、同一アカウントの排他運用を別途確定する必要がある。
契約の人工テストは `python -m pytest -q tests/test_feasibility_http_contract.py` で再実行できる。
この結果を実アカウント権限、実HTTP接続、市場データ、正式Real OOSの検証成功として扱わない。
