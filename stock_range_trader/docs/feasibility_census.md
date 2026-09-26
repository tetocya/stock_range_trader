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
取得plan、承認の範囲照合と台帳への束縛、台帳イベントの状態遷移、待機・累積予算・試行ごとの許容量、
原本一覧と外部receiptの内容照合、Calendar anchorの照合を人工データだけで扱う。
HTTP client、認証情報の読込み、ファイル保存・外部固定点への送信、実取得入口はない
（ファイルシステムへのアクセスは、plan・承認の構築時に出力パスの各要素をlstatすることだけ）。
既存 `run_offline_fixture_acquisition` の具象Transport型検査は変えていない。

### 固定planとCalendarの3方式

- `calendar_discovery`: Calendarだけを対象とする独立plan。後続のDaily日付や予算を自動生成・拡張しない。
  Calendar出典の欄は `None` だけを受け付ける（空文字列は拒否し、同じ意味の別hashを作らない）。
- `predeclared_daily`: 取得前に出典hash付きの営業日集合を固定し、Calendar・Master・Dailyを対象とする。
  出典は**申告値・未検証**（Gate理由 `calendar_source_declared_unverified`）。
- `calendar_anchored_daily`: 完了済みの `calendar_discovery` 成果物から導いたanchorで営業日集合を
  **新planとして**固定し、Master・Dailyを対象とする。anchorの状態は次の2つを区別する。
  - 申告のみ（証拠未提示）: `calendar_anchor_declared_unverified`。
  - 内容照合済み: `verify_calendar_anchor()` が、Calendar planの台帳（承認込み）の完了、receiptの内容一致、
    渡された原本が完了ページの原本と過不足なく一致すること、全暦日が1回ずつあること、ページ送りキーの一致を確認し、
    営業日集合（HolDiv 1・2、`census` と同じ規則）からanchor hashを再計算する。Daily planの
    `calendar_source_sha256` がanchor hashと、`calendar_source_reference` が
    `calendar-discovery:<Calendarの成果物ID>` と一致し、対象窓がCalendar範囲内、Rと全Daily日付が営業日であることを要求する。
    別成果物・別plan・未完了・改変Calendar・非営業日を含むDaily planは拒否する。
  Calendarの取得結果から既存Daily planの対象日・予算を自動生成・拡張する関数はない。

各planはR、対象日付、Calendar出典の参照先とhash、endpoint、逐次ページ送り、最大試行・ページ・時間、転送／展開／保存容量、
1ページ上限、Retry規則、使用するアカウントの識別ラベル（`account_ref`）、専用出力先、成果物ID、有効期間を
正規化JSONのSHA-256で固定する。
重複日付・不正な型・曖昧なtimezone・指定root外を拒否する。待機・timeout・Retry-Afterは1日（86400秒）、
累積時間は366日を上限とし、有効期間の終わりにそれらを足した時刻が表現できないplanも拒否する。日付順序や同じ瞬間のtimezone表記差は
同じhashとなる。出力識別子はこのcheckoutの `outputs/feasibility/http/<artifact_id>` に限定するが、
これは**契約時のパス照合だけ**であり、ファイルの安全な作成は次PRの課題である（plan hashにはこのcheckoutの
絶対パスが入るため、別checkoutでは別hashになる）。
6月試験worktree、`.delayed_replay`、既存DB・台帳・承認ファイルは今回の出力対象にならない。

**取得範囲の固定。** planは作成時に対象query・位置・endpoint・hashを一度だけ導出し、変更できない値
（タプルと読み取り専用の対応表）として保持する。入力の日付は変更できないタプルだけを受け付ける。
台帳は開くたびにplanのフィールドから範囲とhashを再導出して保持値と照合し（不一致は `plan_fixed_scope_inconsistent`）、
以後の予約・resume・完了判定はすべてこの照合済みの範囲で行う。範囲外の予約は `page_outside_fixed_plan` で拒否し、
台帳・予算の状態は変わらない。同一プロセス内で `object.__setattr__` 等により意図的に改変されたplanは、
台帳を開く時点で検出するが、それ以外の実行時改変（monkeypatch等）までは防がない。

### 承認の識別と台帳・receiptへの束縛

`OwnerApprovalClaim` はplan hash・範囲・endpoint・予算・出力先・期間と、承認者・承認イベントID・証拠参照の
**自己申告メタデータ**である。`OwnerApprovalClaim.sha256` は正規化した内容のhash、`approval_event_id` は
承認イベントの識別子で、どちらも**誰が承認したかの証明ではない**。既存コードに独立した所有者署名／承認台帳の
検証基盤はなく、`check_approval_scope()` は真正性を `False` のまま返す。今回、所有者承認資料は作成していない。

- 台帳（`EvidenceJournal(plan, data, approval=...)`）は承認claimと一緒に開く。各 `attempt_reserved` は
  `approval_sha256` を必須とし、開いた承認のhashと一致しなければ拒否する（差替え検出）。
  承認なしで予約を含む台帳は開けない。承認識別子を持たない旧形式（journal v1）の台帳は受け付けず、
  承認を推測で補完しない。
- 予約時刻は plan と承認の両方の有効期間内（開始を含み、終了を含まない）でなければならない。
  送信（`attempt_sent`）時にも、plan・承認の期間と累積deadlineを再検査する。
- `ExternalReceiptClaim` は `approval_sha256` と `approval_event_id` を必須とし、台帳を開いた承認と一致しなければ拒否する。
  承認期限外の試行を含む台帳は開く段階で拒否されるため、正常な証拠として照合されない。

### 待機規則（rate limit）

予約は、直前の試行の応答受信または結果不明の記録時刻（送信開始時刻の上界）から、planに固定した待機を
経過していなければ拒否する（`attempt_before_rate_limit_wait`）。待機は
`max(min_interval_seconds, 種類別の待機, 応答のRetry-After秒)` で、種類別の待機は429・5xx・結果不明それぞれの規則を使う。
サーバーのRetry-Afterは待機を長くするだけで、短くはしない。結果不明の試行も試行数・待機規則から除外しない。

**結果不明の待機。** 結果不明（応答の喪失、timeout、クラッシュ、送信枠の回収）には、観測できなかった429や、より長い
Retry-Afterが含まれている可能性がある。どちらの台帳も「要求が送られなかった」ことを証明できないため、結果不明の
待機は送信記録の有無にかかわらず `max(min_interval_seconds, min_wait_after_network_error_seconds, min_wait_after_429_seconds)`
とし、planの規則とアカウント規則の両方に適用する（実効待機はそのうち長いほう以上）。観測していない結果や
Retry-Afterを台帳に記録することはなく、より長い待機は実効待機や他の記録からだけ加わり、短くなることはない。
**429用の最小待機が経過したことは、実HTTP送信を安全に再開できることの証明ではない。** 実取得の再開条件は、
HTTP・保存PRで所有者が承認する運用規則と実際の送信制御で別途確定する未充足条件であり、今回の実取得Gateは閉じたままである。
応答はplanの `timeout_seconds` 以内、かつ累積deadline以内でなければ記録できない（それを超えたら結果不明として記録する）。

`budget_snapshot()` は `earliest_next_attempt_at`、次に予約すべきページと試行番号、試行ごとの許容量、
機械可読な `blocking_reasons` を返す。`plan_allows_next_attempt` は**このplan内の**予算・期間・待機だけを表し、
理由が空のときだけ真になる（旧名 `can_reserve_next_attempt` は、アカウント全体の送信許可と誤読されないよう廃止）。
評価時刻が `earliest_next_attempt_at` より前なら `rate_limit_wait`、待機明けの時刻がplan・承認の期限や累積deadline以降なら
それぞれ `*_before_next_allowed_attempt` を返す。時計の逆行・timezoneなしの時刻は拒否し、表現できない次回時刻は
例外ではなく `next_attempt_time_unrepresentable` として返す。

### アカウント単位の共有rate limit

同じアカウントへの要求制限は、CalendarとDailyのようにplanが分かれていても共有される。そのためplan内台帳とは別に、
アカウントごとの共有台帳 `AccountRateLedger`（hash鎖、schema `historical-feasibility-account-rate-ledger-v2`）を定める。

- 記録: アカウントの識別ラベル、アカウント共通の待機規則（`AccountRatePolicy`）、送信枠ID（plan台帳の試行IDと同じ）、
  対応するplan hash、送信枠の保持者ID、予約・送信・確定の時刻、結果（`response`／`429`／`5xx`／`unknown`）、
  応答で観測したRetry-After（`observed_retry_after_seconds`）、アカウントが適用する実効待機（`effective_wait_seconds`）、
  各記録の連番・直前hash・記録hash。APIキーは識別子にも台帳にも保存しない。
- 送信枠は**アカウント全体で同時に1つ**だけで、確定前に別planが予約することはできない（`account_slot_in_use`）。
  次の予約は、直前に確定した枠（どのplanでも）の確定時刻から実効待機を経過した後だけ（`account_rate_limit_wait`）。
- 観測したRetry-Afterと実効待機は別の値である。観測値はplan台帳の値をそのまま写し、変更しない。実効待機は
  観測値・アカウント規則の結果別待機以上でなければ記録できず（`account_effective_wait_below_rule`）、
  より長い値を採用してもよい。plan側の規則以上であることは照合で確認する。
  アカウント規則は各planの規則以上に厳しくなければならない（`account_policy_weaker_than_plan`）。

#### 1回の試行の書込み順序と照合

`reconcile_account_and_plan()` は、同じplan hash・同じ試行ID（＝送信枠ID）を持つ記録だけを同一の試行として照合し、
`consistent`（一致）／`pending`（片側だけ進んだ途中状態）／`inconsistent`（矛盾）のいずれかを返す。
書込み順序の契約は次のとおり。

1. アカウント台帳 `slot_reserved`
2. plan台帳 `attempt_reserved`
3. plan台帳 `attempt_sent`
4. アカウント台帳 `slot_sent`
5. HTTP要求（両台帳への送信記録の後）
6. plan台帳 `response_received`（状態コード・観測Retry-After）または `outcome_unknown`
7. アカウント台帳 `slot_settled`（6の結果と観測Retry-Afterを写し、実効待機を付ける）

照合するのは結果と時刻の因果関係で、記録の種類が違う時刻に完全一致は要求しない（両台帳の追記時刻が違っても、
順序が成り立てば一致とする）。各時刻の意味は次のとおり。

| 記録 | 表す事象 |
| --- | --- |
| 共有側 `slot_reserved`／plan側 `attempt_reserved` | 送信枠・試行の予約（HTTPより前） |
| plan側 `attempt_sent`／共有側 `slot_sent` | 送信直前の記録（実際の送信はこの両方より後） |
| plan側 `response_received` | 応答を観測した時刻 |
| plan側 `outcome_unknown` | その試行の結果を不明と宣言した時刻（以後、その試行は送信されない） |
| 共有側 `slot_settled` | 保持者がplan側の記録を写して確定した時刻（plan側の記録以降） |
| 共有側 `slot_reclaimed` | 別の保持者がlease満了後に回収した時刻（plan側の記録より前でもよい） |

- plan側に確定したHTTP結果があれば、共有側の確定結果が同じ種類（429／5xx／それ以外）でなければ矛盾
  （`account_slot_outcome_mismatch`。共有側 `unknown` も矛盾として扱い、短い待機に丸めない）。
- 観測Retry-Afterは両台帳で一致しなければ矛盾（`account_retry_after_mismatch`。片側だけの記録を含む）。
- 実効待機がplan規則・アカウント規則・観測Retry-Afterの最大値より短ければ矛盾（`account_effective_wait_below_plan_rule`）。
- 時刻: 送信枠の予約はplanの予約以前、共有側の送信記録はplanの送信記録以降かつ応答の観測以前
  （`account_send_after_plan_response`／`account_sent_before_plan_send`）、共有側の確定は応答の観測以降
  （`account_settled_before_plan_result`）、応答の観測は送信枠のlease内（`plan_result_after_account_lease`）。
- 結果不明の経路: plan側が不明を宣言した後に共有側が同じ試行の送信を記録していれば矛盾
  （`account_send_after_plan_unknown`）。保持者自身の `unknown` 確定はplan側の記録以降でなければならず、
  planが未確定のまま保持者が `unknown` で確定した場合も矛盾（`account_settled_before_plan_result`）。
  plan側より前の確定を認めるのは、明示的な回収（`slot_reclaimed`）だけである。

**回収の証拠。** 別の保持者が送信枠を閉じる方法は `slot_reclaimed` だけで（`slot_settled` は保持者本人に限る）、
回収者（`holder_id`）、元の保持者（`previous_holder_id`、送信枠の保持者と一致）、依拠したlease期限
（`lease_expired_at`、送信枠の予約時刻＋leaseと一致）、理由（`reclaim_reason`、現在は `lease_expired` のみ）、
実効待機を必須とし、回収時刻はlease期限以降でなければならない。回収は常に結果不明を意味する。
証拠が欠ける・一致しない回収は記録できず、時刻の逆転を回収と推測して受理することはない。
回収後の送信枠に古い保持者が送信・確定を記録することもできない（`account_slot_not_open`）。

`assess_account_slot()` は、共有台帳に送信枠を持つすべてのplanと、渡されたすべてのplan台帳を照合する。
台帳の欠落（`account_plan_journal_missing`）、途中状態（`account_ledger_pending:<code>`）、矛盾
（`account_ledger_inconsistent:<code>`）が1つでもあれば、アカウント上の**どのplanにも**送信枠を与えない。
共有台帳の直近イベントだけで判定し、別planの既知の結果を見落とすことはない。
すべての組が一致したときだけ次回予約可能時刻を1つに決め、共有台帳の待機期限と、各plan側の記録から計算した
待機期限（例: 回収の後にplan側が記録した結果不明の時刻＋待機）の遅いほうを使う。共有台帳だけで予約された送信枠が
直前の試行のplan側待機より早ければ矛盾とする（`account_slot_reserved_before_plan_wait`）。
一致しない・未確定の状態では次回予約可能時刻を返さない（`earliest_next_slot_at=None`）。Gateと入口は、対象plan以外の
関係plan台帳を `related_journals` として受け取り、bytesから開き直して照合する。

#### 片側だけ更新された状態（クラッシュ）と回復

| 停止位置 | 照合結果 | 回復に必要な証拠と追記 |
| --- | --- | --- |
| 1の後（planに試行なし） | `pending_plan_attempt_record` | 送信記録がないので、同じ保持者が送信枠を `unknown` で確定する（送信なしの放棄として一致） |
| 2〜4の後（両側とも未確定） | `pending_settlement_on_both_ledgers` | plan台帳に `outcome_unknown`、共有台帳に `unknown` の確定を追記する |
| 6の後（planに結果、共有は未確定） | `pending_account_settlement` | plan台帳の結果と観測Retry-Afterを**そのまま**写した確定を追記する。推測や `unknown` への置換は矛盾になる |
| 共有側がlease後に `slot_reclaimed` で回収され、planは送信中 | `pending_plan_settlement` | plan台帳に `outcome_unknown` を追記する（次の予約は両台帳の待機期限の遅いほうから） |
| 保持者が、planの記録より前に `unknown` で確定 | 矛盾（`account_settled_before_plan_result`） | 自動回復しない |
| 共有側に結果があり、planに結果がない／planが `unknown` | 矛盾（`account_result_without_plan_result`／`account_result_contradicts_plan_unknown`） | 自動回復しない。所有者が原本・通信記録を確認する |
| planに結果があり、共有側が `unknown`・別結果・Retry-After欠落 | 矛盾 | 自動回復しない（lease回収後に古い実行が結果を書いた場合を含む） |
| 元の保持者がplanに結果（例: 429・Retry-After 600秒）を残して停止し、別の保持者が回収 | 矛盾（`account_slot_outcome_mismatch`） | 別の保持者は結果を写せず（`slot_settled` は保持者本人のみ）、回収は `unknown` になるため自動回復しない。手動調査が必要（結果確定権限の委譲・証拠の引継ぎは設けていない） |
| 共有側に送信記録があり、planに試行がない | 矛盾（`account_sent_without_plan_attempt`） | 自動回復しない |

途中状態・矛盾はいずれも、推測で正常完了に変えない。回復は、上表の証拠から一意に決まる1記録の追記だけで、
それ以外（原本・イベントの欠落）は停止する。拒否された追記は台帳を変更しない（台帳は不変値で、追記は新しい値を返す）。
**両台帳への書込みは今回も別々で、2台帳を原子的に更新する仕組みはない。実HTTP環境でのクラッシュ耐性は
保存PRで実装・検証するまで完成していない。**

#### leaseと並行実行の限界

- **lease満了は、元のHTTP通信が終わった証拠ではない。** 契約上は、送信記録をlease内に要求timeoutが収まる時刻に
  限っているが、元の実行が通信中に停止・遅延し（OSの停止、時計のずれ、timeoutの実装不備など）、lease回収後に
  復帰して結果を書く可能性は残る。今回の契約は、回収後の送信枠への古い実行による確定を拒否し（`account_slot_not_open`）、
  古い実行がplan台帳に書いた結果を矛盾として検出して全planを停止させるだけで、実際の通信を止めることはできない。
  HTTP・保存PRでは、通信timeout、送信枠の所有権、古い実行による確定の拒否を一体で実装する必要があり、
  永続的な送信枠には所有権の世代（fencing token。例: 予約記録のhashや連番）を持たせ、送信・確定の記録にその世代を
  要求する設計を検討する。
- **比較付き追記（`commit_account_ledger`）は、渡された旧状態との差分を判定する契約にすぎない。**
  複数プロセスが同じ実ファイルへ同時にアクセスするときの原子的な読込み・比較・追記、ロック、永続化、
  クラッシュ後の回復は今回のコードでは保証しない。HTTP・保存PRで実装・検証する必須事項である。

`assess_account_slot()` の `account_allows_next_attempt` はアカウント全体の状態だけを表す。plan内の可否とは別の状態であり、
実HTTP送信の可否は `require_live_acquisition_permission()` だけが扱う（今回は常に閉じる）。

Gateはアカウント台帳を必須の証拠とし、欠落（`account_rate_state_missing`）、改変・不正（`account_rate_state_invalid:<code>`）、
関係plan台帳の欠落・途中状態・矛盾・待機中・枠使用中（`account_rate_blocked:<code>`）を理由として返す。
さらに `account_identity_unverified`（ラベルと実際の認証アカウントの一致は証明されない）と
`account_shared_store_not_implemented`（共有台帳の永続化・排他ロック・原子的更新は未実装）を常に返す。

実HTTP送信では、契約上の予約に加えて**送信直前にも**plan台帳とアカウント台帳の両方の制限を検査する必要がある
（plan台帳は `attempt_sent` で期間とdeadlineを、アカウント台帳は `slot_sent` でleaseを再検査する）。
アカウント台帳が把握できるのは、この台帳を通した要求だけである。同じアカウントを使う外部アプリ・別端末・
台帳を使わない別プロセスの要求は検出できないため、アカウントの排他運用は所有者の運用判断として別に確定する。

### 試行ごとの許容量と累積予算

予約時に、転送・展開後本文・保存の各予算について「1ページ上限と残り総予算の小さい方」を計算し、
`allowed_*_bytes` として台帳に固定する（申告値が計算値と異なれば拒否）。残量が0以下なら予約できない。
許容量を超えた応答は超過として記録できるが、その試行の本文保存・ページ完了・以後の予約・run完了はできない。
保存量が許容量を超える `body_saved` は拒否する。結果不明の試行は、転送・展開について1ページ上限分を
保守的に仮計上し続ける。孤児・途中ファイルは保存容量に含め、残る間は次の試行を予約可能と表示しない。
resumeは既存台帳から予算・待機・deadlineを再構成し、別plan hashを拒否する。完了済みplanは新試行を受け付けない。
**今回実装したのは容量の契約であり、実HTTP本文の逐次読込み中に許容量で打ち切る処理は次PRの対象である。**

### 原本一覧とreceipt

`BodyInventory` は**申告値**で、実ディスクを検査しない。各ファイルは状態（`committed`／`orphan`／`partial`）に
かかわらず、現在のバイト列のサイズとhashを持つ。`body_set_sha256` は全ファイルの object_id・状態・サイズ・hashから計算するため、
途中ファイルの追加、状態の変更、原本の追加・削除・改変はreceiptとの照合で検出される。
`committed` は台帳にページ完了がある本文だけに認め、ページ完了のない保存本文は `orphan` として申告しなければならない。
同じ内容を複数のファイル・状態で二重に計上できない。同じqueryの別ページで同じ本文hashが保存された場合はループとして拒否する
（別queryどうしの同一本文は1ファイルとして共有する）。

receipt照合が示すのは、入力された台帳・原本一覧・承認claim・receipt間の**内容の整合**だけで、
外部保管先の独立性・署名の真正性は検証しない。台帳の完全な末尾削除は、独立して固定されたreceiptがなければ検出できない。

### Gateと結果オブジェクトの信頼境界

`assess_live_acquisition_gate()` は、承認・receiptの真正性、Calendar出典の検証、HTTP入口が未実装のため常に閉じる。
台帳は渡されたbytesをplanと承認で開き直して評価し、不整合な証拠は例外ではなく機械可読な理由
（例: `budget_state_invalid:<code>`、`journal_invalid:<code>`）として返す。

`LiveAcquisitionGate.permitted`、`ApprovalScopeCheck.authenticity_verified`、`ReceiptAlignment.independent_custody_verified`、
`CalendarAnchor.independent_custody_verified`、`AccountSlotAssessment.account_identity_verified` は
呼出側が設定できない（常に `False`）。将来のHTTP実行入口は `require_live_acquisition_permission()` だけを予約前に呼び、
生の証拠（plan、承認claim、台帳、原本一覧、receipt、Calendar証拠、アカウント台帳）を
渡して、その場で評価させなければならない。結果オブジェクトを許可の証拠として受け取らない（型が違えば
`raw_evidence_required`）。この入口は今回常に `LiveAcquisitionClosed` を送出する。同一プロセス内のmonkeypatch等は防がない。

### 台帳の処理時間

台帳を開く・resumeするときは全行を検証し、`append` は検証済みの状態の複製に新しいイベントだけを適用する
（両者が同じbytes・状態になることをテストで確認）。人工測定（2510イベント・約2 MB）で、append全体0.15秒、
全件検証0.07秒。5010イベントで0.40秒・0.14秒。appendは状態の複製とタプル連結のため件数に比例してわずかに遅くなる。
アカウント台帳は1507イベントでappend全体0.04秒・全件検証0.02秒、2510イベントのplan台帳との照合（`reconcile_account_and_plan`）は
約0.4ミリ秒（いずれも人工測定）。

### 次PRと所有者判断

HTTP・保存PRで、実通信の総deadline、送信直前の制限の再検査、暗黙Retry無効化、本文の逐次・圧縮前後容量と
許容量での打切り、実ファイルと台帳の照合、fd相対I/O、クラッシュ後の安全な再開、Calendar原本の実取得と保存、
アカウント台帳の永続化・排他ロック・原子的な比較付き追記を実装・検証する。
J-Quantsの公式レート制限はFree 5回／分のsliding windowで、429には通常 `Retry-After` が付かず、
公式は少なくとも1分（確実性のため約2分）の待機を案内する。契約値としては13秒間隔と429後120秒を
下限とし、結果不明にも429後の待機を適用するが、**今回のコードは待機・timeoutを実行しない**（台帳が時刻の規則を検査するだけ）。

所有者はR・対象営業日集合・各予算、承認の独立した真正性検証方式、外部receiptの保管先・署名方式・
固定頻度、実出力rootの運用、アカウントの識別ラベルとアカウント規則、実アカウントとの対応を確かめる方法、
同一アカウントの排他運用（外部アプリ・別端末を使わない運用を含む）を別途確定する必要がある。
契約の人工テストは `python -m pytest -q tests/test_feasibility_http_contract.py` で再実行できる。
この結果を実アカウント権限、実HTTP接続、市場データ、正式Real OOSの検証成功として扱わない。
