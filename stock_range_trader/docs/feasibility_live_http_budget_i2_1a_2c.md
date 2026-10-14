# I2-1A-2C Saved Compatibility V2 / Sequential Historical Admission

Owner承認版OD-2C-01〜14（approval-draft-1、2026-10-13）に基づくpure contract。
承認Packet内の「Exact approved source text」以下のUTF-8 bytesを照合した。
原文SHA-256は `d46d9db95a3ab4475c2ca129ce6bfb119944b4ce84300d4828dc91ff497ff23b`。
Packet全体のSHAとは異なる。原文の歴史的PROPOSED/Pending表記にはPacket冒頭の承認記録を適用する。

固定baseは `a3f6c96acf38dcfdad4d549fd692f28051a968bb`、treeは
`5d41190e158390c6efd1c916b635b64a64f04f5b`。既存ファイル・persisted schema・Budget v1・Store handler・Runtime guardは変更しない。

## 入出力と権限

`live_http_budget_v2.py` はSaved案B・HTTP会計・plan/account容量の診断を担当する。
`live_http_admission_history.py` は人工原本、cut、operation、measurement availability、逐次監査、Core v2を担当する。

- `SourcePoolV1`：identity、StorePolicy原文、既存RuntimeContext/全Runtime event bytes、全planのcontract/root/approval/preflight原文、全C2/C3 journal、initial cut、operation原本、reservation原本、measurement/availability原本。
- `HistoricalCutV1`：その時点でenroll済みの全plan/account/body head、Runtime sequence/hash、StorePolicy hash。後からenrollしたplanを過去cutに混入させない。stopped planをinventoryから除かない。
- `OperationOriginalV1`：人工kind、intent/preconditions、session/fence、source/result cut、全parts、既存の6-field形状のreceipt原文。既存Storeでcommitされたと認定する型ではない。
- `MeasurementAvailabilityV1`：MeasurementOriginal v1のSHA、人工settlement原本SHA、C3 audit event SHA。
- `HistoricalReservationV1`：operation SHA、Core v2 canonical bytes、slot ID。
- `SequentialHistoryValidationV1`：全予約の有効性、最初の違反位置（0起点）、stable reason、検査済みcutごとの判定、最終HTTP会計・C3容量、入力commitment。
- `build_admission_v2()`／`verify_admission_v2()`：毎回全historical reservationを内部監査する。事前検証済みフラグ・callback・永続cacheは受け取らない。

`saved_compatible`、`historical_admissions_valid`、`retry_eligible`、`restrictions_satisfied`、`capacity_satisfied`を区別する。
`producer_authenticated`、`reservation_permitted`、`live_send_permitted`は常にfalse。
`saved_compatibility_v2()`単独は診断であり、admissionやproduction権限へ変換する入口ではない。

## Saved案Bと容量

成功savedは正規200 committed、stable、complete measurement、receiptのsize/digestが一致したbodyだけを加算する。
同じfixed planの成功同士の同digestは拒否する。quarantine同士、成功とquarantineの同digestは独立object/attemptが合法なら許容する。
同digestの異なるfinal sizeと同attemptの複数objectは拒否する。物理容量をdigestでdeduplicateしない。

非成功response／unknownは正規C3 quarantine、closed、stable、既知size、measurementの整合を要求する。
C3既存replayがquarantine直前のversion/writer/path/size/digestを検査する。後のstable claimを以前のquarantine receiptへ遡及適用しない。
MeasurementOriginal v1のpersisted fieldsはquarantineに流用しない。
Untouchedはreserved/not_started/size 0/retained lower 0かつ両C2 prefixに送信証拠がない場合に限る。物理的0 byteの認証ではない。

HTTP unknownは各full-page transfer/decoded chargeを保持し、partialを二重加算しない。missing、unknown、known zeroを別扱いにし、allowance+1の観測はsticky overrun。int64積・和を検査する。
pre-send reclaimはplan unknownを捏造せずfull-page chargeとC3 unresolved chargeを保持する。
authoritative recovery operationが未定義であるため、その後の新admissionはfail-closed（L-2C-7）。

各予約で対象contractのper-object上限Mを用いる。

| 範囲 | 条件 |
|---|---|
| Account retained | 全planのΣ(retained + reserved) + M ≤ StorePolicy上限 |
| Account acquired | 全planのΣ(acquired + unresolved) + M ≤ StorePolicy上限 |
| 対象plan retained | retained + reserved + M ≤ C3 retained上限 |
| 対象plan reserved | reserved + M ≤ C3 reserved上限 |
| 対象plan acquired | acquired + unresolved + M ≤ C3 cumulative上限 |
| 他plan | Mを加えず各自のC3 policyを検査 |

stoppedは現在の新admissionを拒否するcatalog属性。後日のstopを過去の予約へ遡及適用せず、過去cutの容量は必ず残す。
unknown retained sizeを0に変換しない（L-2C-8）。

## commit-orderと全履歴監査

入力operationはRuntime sequence順であり、operation IDの文字列順・UTC sortを使わない。
各operationは開始/終了clock_observedを持ち、同一session/fence、隣接sequence、合法Runtime chain、receiptのcommitted_atとparts/source/result headが一致する必要がある。
その間の全journal appendはpartsで一度ずつ被覆する。予約をgeneric evidence operationに隠せない。

人工operation kindは次の3つだけを認める。

- `i2-http-reserve-v1`：固定account/plan/body 3 roles。Core v2、slot、attempt number/ID、generation、イベントを独立再計算する。
- `artificial-evidence-operation-v1`：送信・header・hold・writer等の非予約証拠。予約・settlement・reclaimを隠せない。
- `artificial-settlement-operation-v1`：measurementのintent/precondition bindingとC3 auditを要求する。

measurementはsettlement原本・receipt・全parts・auditを検証し終えてから利用可能になる。
後続予約のstartより前にsettlement endがなければ利用しない。observed_atが古くても後発commitを過去cutへ持ち込めない。
clock-only/stop-onlyのcommitはbusiness commitを証明しない（L-2C-6）。未知のrecovery kindを自己申告で正規化しない。
同じUTCでもRuntime sequenceとhash dependencyにより順序が証明できれば受理する。

全予約でretryability、page/cursor順序、attempt/page count、HTTP会計、hold/wait、scope/elapsed、Saved、全plan容量を検査する。
404→quarantine→hold release→不正retry→429→200の最初の不正予約を拒否する。
最後のopen attemptは過去の合法予約自体を無効化しないが、次のadmissionは拒否する。
現在のCore作成では最新Runtime headを要求し、後発stopを古いcutで隠せない。

## ReferenceとIncremental

Referenceは各cutの原本prefixを既存C2/C3で独立replayする。
Incrementalは入力ごとに全原本を検証し、既存C2 reducer/C3 applyによるimmutable prefix projectionを一度構築する。同じ呼出し内のcutとC3 dependencyで再利用する。
既存private reducerへの依存があるため、C2/C3の更新時はこの比較試験を必須とする。

結果のcanonical bytes（最初の違反位置、reason、HTTP/Saved/C3容量、input commitment含む）を比較する。
`ReplayStatistics`の論理的replay入口数に加え、テストでは実際のC2/C3 `_replay`をspyして再利用を確認する。統計は検証結果や権限の入力ではない。
結果・projectionの保持は無限規模の性能保証ではない。

## ValidationLimits：人工試験用の暫定値

公開defaultは存在せず、全APIで明示指定を要求する。Ownerが数値を承認するまでMerge candidate No。
人工テストでは次を明示指定する。

| 項目 | 暫定上限 |
|---|---:|
| original records | 1,500 |
| total canonical bytes | 4,000,000 |
| reservations | 24 |
| plans | 4 |
| dependency references | 5,000 |
| single record bytes | 32,768 |

record数はRuntime・journal行・operation等とcontext/metadata原本を含む。
reference数はoperation cut/part、availability、reservation、measurement prefix、Runtime/journal chain、C3→C2、enrollment prior-head、plan documentの明示的なリンクを保守的に加算する。
exact/+1、bool/NaN/負値/型違い/int64 overflowの人工反例を検査する。
各上限は資源制限の提案であり、この組合せの最悪ケース性能を保証しない。4096 recordsやproduction SLAは未検証。

BenchmarkはmacOS 15.7.7 arm64、Python 3.13.3で、人工operationだけを対象とする。
CPU時間、wall時間、tracemalloc peak、実replay数を計測する。生成時間と検証時間は分離する。
2026-10-13のローカル実測は下表のとおり。各ケース1回、fixture生成時間を除外し、
`time.perf_counter` / `time.process_time` / `tracemalloc`を使用した。
既存C2/C3の`_replay`をwrapして実呼出しを数えたため、その計測用オブジェクトもpeakに含む。
全ケースでReference/Incrementalのcanonical結果が一致し、全予約が有効となった。

| 人工ケース | records / bytes / references | Reference wall / CPU秒 / peak bytes | Incremental wall / CPU秒 / peak bytes | 実replay C2/C3：Ref → Inc |
|---|---|---|---|---|
| 1予約 + audit 0 | 45 / 68,083 / 83 | 0.3631 / 0.3629 / 468,962 | 0.1358 / 0.1355 / 286,636 | 54/17 → 14/1 |
| 1予約 + audit 8 | 77 / 119,666 / 179 | 1.6924 / 1.6924 / 708,148 | 0.1883 / 0.1877 / 256,932 | 118/49 → 14/1 |
| 1予約 + audit 32 | 173 / 274,543 / 467 | 7.1744 / 7.1623 / 919,103 | 0.3442 / 0.3434 / 362,839 | 310/145 → 14/1 |
| 1予約 + audit 64 | 301 / 481,229 / 851 | 18.9277 / 18.3495 / 1,462,076 | 0.5358 / 0.5358 / 526,660 | 566/273 → 14/1 |
| 1予約 | 45 / 68,083 / 83 | 0.3623 / 0.3620 / 268,765 | 0.1293 / 0.1293 / 199,273 | 54/17 → 14/1 |
| 3予約・1成功page | 107 / 174,744 / 245 | 2.7941 / 2.7941 / 617,386 | 0.3885 / 0.3883 / 399,233 | 138/49 → 14/1 |
| 6予約・2成功page | 200 / 335,283 / 487 | 11.0488 / 11.0282 / 1,134,091 | 0.8406 / 0.8403 / 708,009 | 262/97 → 14/1 |

再現入力は専用テストの`History`を用いる。audit軸は`History().begin().settle()`に対し、
各回`clock`→`cut`→`j.audit(evidence_ref="benchmark", operation_id=f"bench-{i}", at=tick())`
→`finish(cut, EVIDENCE_KIND, f"bench-{i}")`を追加する。
予約軸は各pageで503→429→200、成功時size=1、digestは`sha256(f"body-{n}".encode())`、
第1page成功のcursorを`second`とし、1/3/6予約時点のpoolを測る。
各verifierへ同じ`pool`と上記暫定`ValidationLimitsV1`を渡す。

この測定は1 planの小規模人工履歴である。2 planの正常・容量境界は機能テストで確認したが、
4 plan・24予約・1,500 records・4 MBの組合せのworst caseを実測したものではない。
暫定profileは試験用の上限にとどめ、公開profileの数値確定は独立レビューとOwner承認まで保留する。
長いReference検証をproduction性能の根拠にせず、Incrementalも線形時間やSLAを保証しない。

## 受入試験と引継ぎ

| 条件 | 専用テストの対象 |
|---|---|
| A | 404後の不正retryを最初の違反として検出、後続429/200では消えない |
| B/C/D | quarantine重複許容、成功重複拒否、成功savedと物理容量の分離 |
| E/F | future settlement、同一UTC、clock/stop-only、receipt/part/measurement binding |
| G/H | 別plan・stopped planを含むaccount容量、exact/+1、欠落拒否 |
| I/J | unknown full-page、partial overrun、pre-send reclaimと未定義recovery拒否 |
| K/L | quarantine時点のversion、closed/stable条件、不正遷移・未commit200拒否 |
| M/N | v1 literal/API維持、Core v1拒否、schema/permission/cut/fence/source改変 |
| O/P | Reference/Incremental canonical一致・実replay計数、IO trap |

将来2BのStore preconditionsはCore v2のみ受理し、Core v1を明示拒否する（L-2C-5）。
現在のkind `i2-http-reserve-v1`および3 rolesのversionとCore schema versionは独立である。
2CではStore kind registry・handlerを変更していない。

提示poolとanchorを整合的に短縮する欠落はpure verifierだけでは検出できない。
2Bはtransaction内でauthoritative head、全journal、catalog、policy、Runtime/fence、全plan inventoryを選択し、その完全性を束縛する必要がある。
人工operation原本を実Store commitの証拠として使わない。producer名やstable hashはproducer/物理真正性を認証しない。

M-2C-1〜3、L-2C-5〜8、L-BUD-6/7は本変更の実装・反例対象だが、独立レビュー受入まではresolved認定を保留する。
M-BUD-1、M-BUD-2/3のStore enforcement、L-BUD-4/5、L-I1A3-1、L-C3-2は残る。
特にL-BUD-5は有限数値profileのOwner承認と性能判断がMerge条件である。

Live Gate closed、remote account identity未検証。実HTTP/J-Quants、Formal Real OOS、2B/I2-1B/production dispatchは不可。
次はClaude Code / Opus 5.5 / Highによる独立コードレビュー。push/PR/Mergeは別承認を要する。

## ローカル検証環境

Python 3.13.3、pytest 9.1.1、Ruff 0.16.8、pandas 3.0.6、numpy 2.5.3。
既存の独立venvを使用し、依存を追加・更新していない。
全suiteは`caffeinate -i`でhost sleepを避け、`JQUANTS_API_KEY`、
`RUN_LIVE_JQUANTS_TESTS`、`RUN_LIVE_YFINANCE_TESTS`を子processから除外して実行した。
`-B`および`-p no:cacheprovider`を使用し、既存の時計guardを緩和しない。

```sh
.venv/bin/python -B -m pytest -q -W error -p no:cacheprovider tests/test_feasibility_live_http_budget_v2.py tests/test_feasibility_live_http_admission_history.py
.venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_feasibility*.py
.venv/bin/python -B -m pytest -q -rs -p no:cacheprovider
.venv/bin/ruff check --no-cache .
.venv/bin/ruff format --check --no-cache .
git diff --cached --check
```

2026-10-13の実行結果：

| 検査 | 結果 |
|---|---|
| 2C専用（`-W error`） | 85 passed / 14.09秒 |
| 既存C1/C2/C3/I1a/I1b/I2-0/Budget v1/2A関連 | 1,036 passed / 82.25秒 |
| feasibility全体・最終差分 | 1,478 passed / 120.08秒 |
| 全pytest | 2,961 passed / 13 skipped / 28 warnings / 770.27秒 |
| Ruff check | pass |
| Ruff format | 310 files already formatted |
| 差分検査 | pass（新規5ファイルのみ） |

既存baseline 2,876件に対して85件増加、failed/errorは0。
skipはLive 7件・headless browser 6件で、いずれも未検証。
warningsは既存`data/providers/jquants_v2.py:411`のurllib3 `allowed_methods`に関する28件のみ。
今回の専用試験に新warningはなく、時計guardの緩和・新skip/xfailもない。
Linux Python 3.11/3.12/3.13 CI、独立レビューは未実施。push/PR/Mergeは行わない。

独立レビューでは特に、人工operationの完全な被覆とcommit-order、later enrollmentを含むcut、
停止済みplanの容量、measurement/body receiptの後発原本拒否、C2/C3 private reducer依存、
ReferenceとIncrementalが共有する契約条件の誤りを重点確認する。
本実装者によるテスト成功を独立したCritical/High/Medium/Lowの0件認定とはしない。
数値profile未承認のためMerge candidateはNo。2B/I2-1B/production dispatchもNo。

## 2026-10-14 Post-Merge remediation（OD-2C-R1〜R5）

上の2026-10-13の測定・検証値は是正前の履歴であり、今回の結果へ読み替えない。
PR #27のMergeとCI成功は確認済みだが、公開数値profileの承認を含む従前の
Merge Gateが成立したことを意味せず、遡及的なPASSとは扱わない。

今回の固定baseは`efbc67cbf28b8bf736d8822880916a84652dc199`、
treeは`aee5ef3262f43632b4924cd3cb85422534f4a71d`。
Owner承認済み原文14,978 bytesのSHA-256は
`54b5432f0165cfc4cb5727cdad4ea9b92fe04a2c1230dfec901c9246c30facb3`。
承認packetの全文を照合し、専用branch
`real-oos/i2-1a-2c-postmerge-remediation`でpure実装だけを是正する。
既存Store、Runtime、C1/C2/C3、Budget v1/v2、保存schemaは変更しない。

### R1：書込みscopeと後続enrollment

operationの書込みpartと実際に進むjournalは、account、当該`operation.plan_sha`
のplan/bodyに限定する。他planへの書込みは`part_scope_mismatch`で拒否する。
他planをdependency/sourceとして読み、そのheadを進めない操作は従来どおり許可する。

後続Bのenrollmentは、B自身に帰属する専用人工kind
`artificial-enrollment-operation-v1`で表す。これは本番Storeのkindやhandlerではない。
sourceにBのenrollmentは存在せず、resultへ次の3 eventsだけを追加する。

| 順序 | journal | role | event |
|---|---|---|---|
| 0 | account | account-enrollment | plan_enrolled |
| 1 | 新B plan | plan-enrollment | journal_opened、sequence 0 |
| 2 | 新B body | body-enrollment | audit、sequence 0 |

account/planの時刻は同一、body auditはそれ以降とし、auditはobject/c2_headsを持たず、
payloadを`{"evidence_ref":"artificial-enrollment:<B plan SHA>"}`に固定する。
既存C2の正規replay/reconciliationとC3の正規root/contract検証により、
documents、prior heads、openingの対、body初期状態の整合を検査する。
専用kindでも追加のauditや他plan書込みは許可しない。
汎用evidence/settlement/reservationへenrollment eventsや専用auditを紛れ込ませることは拒否する。
`pool.initial`のbootstrapとは区別し、将来Bの原本がpool内にあるだけでは過去のenrollmentにならない。
C2/C3のtransition IDを外側operation IDへ改変するような架空のmetadataは追加しない。

### R2：peerの正常な枯渇だけを除外

固定registryをhistory側へ置く。完全一致で分類し、prefixによる推測はしない。

- `LOCAL_ELIGIBILITY_PEER_ONLY`：`byte_budget_exhausted`のみ。
- `PEER_BLOCKER`：`sticky_overrun`、`open_unsettled`、`measurement_not_committed`、
  `conflicting_measurement`、`unsupported_recovery_operation`、`response_200_body_uncommitted`、
  `body_not_stable`、`body_not_committed_or_quarantined`、`untouched_send_evidence`、
  `body_measurement_missing`、`object_attempt_missing`、`attempt_multiple_objects`、
  `final_digest_size_conflict`、`successful_digest_duplicate`、`successful_measurement_mismatch`、
  `quarantine_outcome_invalid`、`quarantine_is_not_http_saved`、`quarantine_measurement_mismatch`。

target自身の理由は除外せず、peerでは最初の集合だけを除外する。
未知reasonは`unknown_budget_reason`でfail-closed。最終budget集計にもregistry検査を適用する。
静的テストでBudget v2の全reason emitterとregistryの一致を検査し、未知reasonの動的注入も拒否する。
account/plan capacity、未解除hold、wait、過去の不正予約は別経路で引き続き強制する。
停止済みpeerのretained bytesもaccount容量に算入し続ける。
合法だった過去予約を、後のpeer枯渇だけで遡及的に不正扱いしない。

### R3：早期limitsと是正HEADの人工実測

順序は、型・plan/予約/原本件数、raw文字数/bytes、逐次canonical bytes、
bounded dependency parse/reference件数、深いreplay/逐次監査とする。
原本全体のcanonical tupleを先に展開せず、個別/総bytes超過時はその時点で停止する。
raw文字列はUTF-8 encodingより先に文字数を検査する。
既に呼出側が構築したPythonオブジェクトや、未承認の巨大なcaller limitsの
メモリ消費を保証するものではない。`measure_input_size`も診断用で、実行権限ではない。
`ReplayStatistics.retained_prefixes`は計測専用で、canonical結果やpersisted schemaには含めない。

人工fixtureの暫定六上限は引き続き1,500 records / 4,000,000 canonical bytes /
24 reservations / 4 plans / 5,000 references / 32,768 single-record bytesである。
公開値としては未承認。探索用に別のcaller limitsを使用した場合は、その値と理由を
benchmarkの各行へ記録し、公開profileへ昇格させない。

正式実測は是正をまとめたローカル1 commitのclean HEADで実施する。
人工pool、計測スクリプト、ログはrepo外に保持し、測定結果を入れるための追加commitは作らない。
fixture生成・loadと検証を分離し、5回以上のwall/CPU中央値・最大、process RSSと
tracemalloc peakを別々に報告する。guard/parse/replay計数・時間、prefix保持数も記録する。
ステージ時間は包含関係があるため単純加算しない。
高負荷のReferenceは60秒/512 MiB等の探索停止を認めるが、停止は一致確認ではない。
小規模同形fixture、prefix不変性、追加audit/planによるmetamorphic検証で補助する。
Linux実行系がない場合は未検証と記録し、macOS結果をLinux結果として扱わない。
単一軸の測定や資源保護用停止値からproduction SLAや複合上限の保証を導かない。

### R4/R5：権限と正式再受入

pure結果の`producer_authenticated`、`reservation_permitted`、`live_send_permitted`は固定false。
将来2BではOwner承認済みprofile ID/version/六上限/digest/approval referenceを
Store管理documents/preconditionsへ束縛し、authoritative headsと全plan inventoryを使用する。
未知profile、旧Core、source欠落を拒否する必要があるが、本変更では実装しない。

R1-A〜H、R2-A〜J、既存専用試験の意図、v1完全非回帰を検査する。
是正後実測を受けた六上限と許容wall/memory thresholdのOwner承認、
Claude Code / Opus 5.5 / Highの独立レビュー、別承認によるpush/PR/CI/通常Merge、
統合HEAD/tree/両親/profile確認を経て、2C正式再受入を別途判定する。
本実装・ローカルgreenだけではM-R1-1/L-2C-9/L-2C-10の独立受入を宣言しない。
L-BUD-5の数値・性能基準承認をはじめ既存のStore authority/真正性/物理実装の課題は残る。

local pure remediationとsynthetic benchmarkだけが許可済み。
Numeric public profile、push/PR/Merge、2B、I2-1B、production dispatchはNo。
Live Gate closed。実HTTP/J-Quants、Formal Real OOSは引き続き不可。

### 是正後のcommit前ローカル検証

macOS 15.7.7 arm64 / Python 3.13.3。依存の追加・更新はしていない。
既存の独立venvを読み取り使用し、importと作業先は新しい専用worktreeとした。

| 検査 | 結果 |
|---|---|
| 2C専用（`-W error`） | 121 passed / 24.10秒 |
| macOS対象Store 4ファイル | 289 passed / 58.01秒 |
| feasibility全体 | 1,514 passed / 137.04秒 |
| 全pytest | 2,997 passed / 13 skipped / 28既存warnings / 783.48秒 |
| Ruff check / format / diff check | pass |

baseの2,961 passedから36件増加。既存試験を削除したりskipへ変更したりしていない。
skipはLive 7件とheadless browser 6件で、未検証。
28 warningsは既存`jquants_v2.py:411`のurllib3 `allowed_methods`のみ。
Linux Python 3.11/3.12/3.13の実行系はローカルにないため未検証。
新HEADのGitHub CI、独立レビューは未実施であり、pushはしない。
正式benchmarkの結果はこのcommit後、同じHEADに対してrepo外の別レポートへ記録する。
