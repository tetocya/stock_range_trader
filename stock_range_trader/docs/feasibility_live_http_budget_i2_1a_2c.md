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

## VL-01〜06：tests/docs限定の補強（2026-10-15）

この節は上記R1〜R5当時の結果とは別の、Owner承認済みローカル検証を記録する。
固定baseは`d307fad9cd1791a3ac206cf3f8420ec279c3500e`、treeは
`06add56d67af3ef8398375f03ffbc4c0f302fce7`。
承認原文24,340 bytesのSHA-256は
`1072ae4c2de72c20f4104e4209d0c514da22ac478aa4bb2c14838e011b3f5e65`。
Owner承認ヘッダーの対象はtests/docs・人工benchmark・ローカル1コミットまでであり、
原文に列挙された数値候補を公開上限やSLAとして承認したものではない。

### L-RV-1：reason生成経路の監査

既存のliteral `.add()`検査を削除せず、2C専用の
`test_feasibility_live_http_validation_coverage.py`へ補助検査を分離した。
Historyの状態構築と、静的検査器自身のmutantを独立に読めるようにするための追加である。

Budget/Savedの二つのsinkを対象に、literal add/update、集合union、`|=`、
初期set/tuple/list、constructorのreason引数を収集し、固定registryと完全比較する。
Capacity専用reasonと例外文言をBudget registryへ混入させない。
`saved.reasons`の伝播は既知のedgeとして扱い、target/peer × Budget/Savedの
4通りの動的注入で両verifierと`build_admission_v2`の拒否も確認する。

新helper、alias、動的文字列、comprehension、未知のreturn経路は推測で通さない。
既知moduleのtop-level AST unitごとの固定fingerprintを併用し、constructor、
import、module assignmentを含む未監査の構造変更を要レビューとして検出する。
fingerprintは実行時生成で追随せず、更新にはreason経路の差分監査を要する。
Python 3.12で追加された空の`type_params`だけを正規化する。
これは任意Pythonの完全な静的解析や、悪意ある同一processからの防御ではない。
reasonに無関係な構造変更も保守的にレビューを要求する制約がある。

### L-RV-2：mixed-plan fixture

plan選択とcatalogのenrolled/stoppedを分離し、既定の人工peerもenrolledとする。
既存のstopped-peer試験は停止を明示することで以前の意味を維持した。
不正予約を作る試験補助経路では、過去operationをcommit順にたどり、receipt/parts/auditを
確認した後だけmeasurementを追加する。allowanceは全planのbody-reservationから
完全AttemptBindingで復元し、現在選択中のbodyや最終measurement一覧へ依存しない。

正常control、A/B交替後の404不正retry、Bの違反後にAが成功する場合、正常peer枯渇、
peer sticky overrun、stopped retained容量の160/159境界、長い履歴の末尾違反を追加した。
役割を交換したケースも検証し、canonical一致だけでなく正しいreasonと最初の違反位置を
固定期待値でassertする。retained境界ではcumulative容量を別設定にして、意図しない
別の容量理由が先に出るfixtureを避けた。これらは人工設定であり公開既定値ではない。

### 測定の位置付けと原本

人工原本はrepo外のcanonical JSONとして保持し、各original hash、pool commitment、
manifest/harness SHAを固定する。pickleや既存の市場データは使用しない。
この承認では最終コミット前に測定結果を文書へまとめるため、base production SHAと
変更後のfixture test SHAを各測定行に記録し、最後にtests/docsだけを1コミットとする。
上記R3当時のcommit後測定と混同しない。

Profile Bの候補六上限は300 /500,000 /7 /2 /850 /32,768、
Aは1,500 /4,000,000 /24 /4 /5,000 /32,768。
順にrecords、canonical bytes、reservations、plans、references、最大単一原本bytesである。
どちらもOwner未承認の人工測定用caller limitsで、production constantを追加していない。

T（spy/tracemallocなし）、M（process RSS）、P（Python allocation）、D（diagnostic）、
R（直接canonical比較）、N（拒否費用）を別fresh processで測定する。
import、fixture load/hash確認、verifier本体を分離する。低頻度の探索停止用SIGALRMは
各modeにあり、60秒/512MiBはSLAではない。Dの包含時間は単純加算しない。
Mの開始後HWM差は厳密な増分RSSではなく、Pのpeakとも別の量である。

Linux x86_64のPython 3.13環境は今回準備できていない。同じcanonical fixture bytesを
渡すrunbookを残すが、Linux性能・Linuxでの新テスト・新HEADのCIは未検証とする。
macOSのみの結果や既存Linux CIをLinux性能の証拠へ読み替えない。

公開profile ID/version/digest・六上限・数値SLAの確定、独立レビュー、2C正式再受入は
引き続き別判断である。L-RV-1/2は本補強の独立レビュー後に解消判定する。
Store/Runtime、C1/C2/C3、Budget v1/v2、production verifierは変更しない。
permission/authenticationは固定false、Live Gateはclosed。
Push/PR/Merge、2B/I2-1B、実HTTP/J-Quants、Formal Real OOSへ進まない。

### VLローカル品質検査

macOS 15.7.7 arm64 / Apple M4 / 16 GiB / Python 3.13.3。
既存venvを読み取り使用し、import先は今回の独立worktreeへ固定した。
新しい依存の導入や既存環境の変更はない。

| 検査 | 結果 |
|---|---|
| 2C専用（3ファイル、`-W error`） | 156 passed / 82.03秒 |
| macOS Store対象4ファイル | 289 passed / 58.28秒 |
| feasibility全体 | 1,549 passed / 192.42秒 |
| 全pytest | 3,032 passed / 13 skipped / 28既存warnings / 827.41秒 |

baselineの2,997件を維持して35件追加した。既存試験の削除・skip追加はない。
Live 7件・browser 6件は未検証で、既存urllib3 warning 28件も変更していない。
Python 3.11/3.12のローカル実行は未実施。

初回の制限付き全pytestは133 failed / 260 errors / 2,639 passedだった。
既存人工Store試験のHOME配下temp作成がsandboxに拒否され、F0試験では
明示した`/private/tmp`が標準tempの許可root外だった。加えてlocalhost socketを使う
既存試験には実行権限が必要だった。productionやテストの検査条件は緩めず、
標準`tempfile` rootと必要な権限で上記全体を再実行した結果を最終結果とする。
既存試験が生成する専用人工Storeとloopback応答以外の市場API・保存口座は使用しない。

### VL人工fixtureの到達範囲

| fixture | records | canonical bytes | 予約数 | plan数 | references | 最大原本bytes |
|---|---:|---:|---:|---:|---:|---:|
| B：7予約 | 236 | 430,177 | 7 | 2 | 673 | 14,981 |
| B：複合 | 262 | 500,000 | 7 | 2 | 733 | 32,768 |
| A：24予約 | 774 | 1,657,075 | 24 | 4 | 2,993 | 17,923 |
| A：複合 | 1,477 | 3,861,772 | 24 | 4 | 4,991 | 19,109 |

Bは六上限を同時に満たす7予約・2 planの合法な複合fixtureを構成できた。
ただしrecords=300/references=850を同時に飽和させるfixtureは未作成で、
上表は測定した構成であって全入力構造の数学的最大や性能保証ではない。
Aも六軸すべてのexact境界を網羅したとは扱わない。

六軸の機構確認は、合法なB複合の実測値をexact limitsとし、各軸だけを1下げた
別ケースとして実行する。これと公開候補値の実境界試験を混同しない。
候補境界にはrecords 300/301、bytes 500,000/500,001、予約7/8、plan 2/3、
単一原本32,768、raw runtime原本32,769を用いる。最後は入口でのsize拒否の証拠であり、
合法C3原本ではない（C3の型付き生成API自体が32,769 bytesを既に拒否する）。
referencesはこのappend-only familyの奇数カウントにより849/851で候補850を挟む。
exact-850の合法fixtureは未検証。0予約のケースでは診断helperが1へ丸めるため、
manifestに別記した`actual_reservations`を真の件数として使う。

正常terminal・解除済みhold・peer枯渇・stopped retainedと、末尾404不正retry、
peer sticky overrun、cross-plan part scope、未来settlement commit、未知reasonも
人工原本に含める。negativeでは想定reasonと最初の違反位置を固定し、
別の欠損・receipt不整合による拒否を意図した検証結果へ読み替えない。

### VL macOS性能実測

各case/modeは5回、fresh processで測定した。TはIncremental public verifier、
RSSはM、Python peakはPから取り、計装時間をTへ混ぜていない。
Python peakはloadとverifierを含む最大値、RSSはprocess全体のHWMである。

| fixture | T中央値 / 最大（秒） | RSS最大（MiB） | Python peak最大（MiB） |
|---|---:|---:|---:|
| baseline（1予約・1 plan） | 0.02244 / 0.02260 | 54.97 | 0.61 |
| B：7予約 | 0.19037 / 0.19160 | 55.30 | 2.90 |
| B：複合 | 0.22200 / 0.22313 | 58.98 | 3.21 |
| A：24予約 | 1.13740 / 1.16039 | 58.48 | 8.49 |
| A：複合 | 2.27078 / 2.27284 | 75.70 | 16.56 |

A複合のPython peakは17,366,222 bytes（16.56 MiB）で、未承認候補16 MiBを上回った。
verifier単体のlive allocation peakも16,797,859 bytes（16.02 MiB）である。
verifier開始時baselineからの増分peakは6,173,941 bytes（5.89 MiB）だが、
これを総peakと置き換えて候補以内とは報告しない。公開SLAの合否は未判定。
baselineと大規模fixtureの比率は規模差であり、変更前後の回帰倍率ではない。
1.25/1.5倍等の未承認回帰閾値を満たす証拠にもしていない。

| 境界超過・不正入力 | N中央値 / 最大（ms） |
|---|---:|
| records 301 | 0.0189 / 0.0267 |
| bytes 500,001 | 1.5663 / 1.8225 |
| reservations 8 | 0.0167 / 0.0172 |
| plans 3 | 0.0178 / 0.0217 |
| references 851 | 2.3378 / 2.4020 |
| raw single record 32,769 | 0.0198 / 0.0217 |
| 末尾の不正retry（位置8） | 229.3635 / 233.8315 |
| peer sticky overrun（位置3） | 79.9144 / 80.8983 |
| cross-plan part scope（位置0） | 5.9084 / 6.0300 |
| future measurement commit（位置1） | 23.2371 / 23.5668 |
| 未知reason（位置0） | 5.2006 / 5.3301 |

安価な入口上限と、履歴を読んで初めて検出する意味違反は別の費用である。
全negativeを一律50ms以内とは主張しない。50ms自体も公開承認されていない。

Dの内部logical full_replaysはB複合6、A複合10だったが、実関数call数とは異なる。
spyで観測したC3 replay / C2 replay / JSON parseはそれぞれ
B複合2 /24 /427、A複合4 /50 /3,100。
bytes超過はJSON/replayとも0、references超過はJSON 75 / replay 0で停止した。
これは現fixture上の観測であり、任意入力での計算量や物理Storeの性能保証ではない。

A複合のRは5回とも60秒の探索上限で停止し、Referenceとの一致は未確認。
Incremental側は全5回で有効な結果を返したが、Reference未完了を成功とは数えない。
Linux同一bytes比較、OS負荷・thermalの統制、公開上限のOwner判断は残る。

補助比較では、同じ4 plan・24予約でauditレコードを減らしたA原本を使った。
このRは5回ともcanonical一致、合計検証時間の中央値28.70873秒 / 最大28.74817秒、
Reference単独の中央値27.55805秒 / 最大27.60022秒だった。
B複合もRは5回とも一致。全体では30 case・680測定行、直接比較50回一致、
上記A複合5回は未比較である。上限超過の260行は期待どおりの拒否で、正常入力の
成功件数へ足さない。mode/反復間で返却済みのcanonical結果hashも一致した。

### VL測定成果物と引継ぎ

repo外の`stock-range-i2c-vl.7wkA2J/verified-v2`へcanonical原本、各original SHA、
pool commitment、environment、raw、summary JSON/CSVを保存した。
測定script、再現runbook、品質検査log、最終local commitとの対応receiptを
まとめたarchiveを別途引き渡す。一時領域のため、独立レビューまで保全を要する。

| 成果物 | SHA-256 |
|---|---|
| fixture-manifest.json | `64b723faff1a1b72a5f05fd0465d16dde17d690da57e006a695f51ed49181bf5` |
| benchmark.py | `64ab8c9147c7b9230546fb4d76faec5d78dccbde32669d9773a6cbcded4f5b6b` |
| raw-timings.jsonl（675行） | `44e41b0dbe908dac9748820f9bd9cfbb4c69214bf1788eba5c56566af8984490` |
| supplement-R.jsonl（5行） | `6c2bd29cd91ee307c0c38e11def03211600ad00375ba2e310c731f6eeb28ec59` |
| summary.json | `dffe7e9c58915a5388b915330269723285a553b8530c519d87f12e412771072b` |

今回の観測では修正を要するproduction不具合を確認していない。ただし、L-RV-1/2の
独立再レビュー、Linux同一bytes実測、exact-850 fixture、A複合Referenceの未完了、
Python peakの候補超過は引継ぎ事項である。未承認値の緩和やfixtureの結果隠蔽で
解消したとは扱わない。公開ValidationLimits/SLAと2C正式再受入は未確定のまま。

## VL Review Follow-up：Owner承認済み限定是正（2026-10-16）

対象は`9ccba1440947190bca7fa23c729f52e90a73a93c`、treeは
`19f9f3574389790eae2a597d2e7c81b69ede84a3`。
親は固定integration `d307fad9cd1791a3ac206cf3f8420ec279c3500e`で、開始時clean。
原文SHA-256 `1072ae4c2de72c20f4104e4209d0c514da22ac478aa4bb2c14838e011b3f5e65`
を再照合し、追加Owner承認のM-VL-1 / L-VL-1 / L-VL-2だけを扱う。
旧commitのamendや原本の上書きは行わず、追加ローカル1コミットとする。

### M-VL-1：固定canonical AST表現

原因は`ast.dump()`の空list表示差である。実Python 3.11.5で旧式を再計算すると、
21単位中12単位のfingerprintが不一致だった。空`type_params`の文字列置換だけでは
`arguments`やdecorator等の空list差を吸収できなかった。

監査器が存在する既存`test_feasibility_live_http_validation_coverage.py`も
tests内部の修正対象に含めた。`ast.dump()`に依存しない固定node/field表と
canonical formatterを導入し、**既存21個の期待hash literalは一つも変更していない**。
実行時生成で期待値を更新する経路もない。

既知listの空値、既知optional fieldのNoneは固定規則で省略する。
3.11で未定義のFunctionDef/ClassDef.type_paramsは空listと同じ意味に扱うが、
非空type_paramsは再監査を要求する。Constant(None)やlist要素のNoneは残す。
未知node/field/attribute/metadata、欠落必須field、不正list値は理由付きで拒否する。
新しいPython schemaを無条件に許可するfallbackはない。
コメント・位置情報はfingerprintに影響しないが、docstringは従来どおり監査対象。

全21単位の固定hash検査、ast.dump呼出禁止、empty/None/type_params境界、未知構造、
getattr/setattr経路を追加した。既存add/update/union/constructor、alias、動的reason、
新helperのnegative testsと、target/peer × Budget/Savedの実行時拒否試験は維持する。
これは限定grammarの変更guardであり、任意Pythonの完全な静的解析ではない。

### L-VL-1／2：Profile B内のrepository回帰試験

固定人工root `/__stock_range_trader_vl_artificial__/http`を使い、checkoutの長さで
bytesが変わらないようにした。通常の人工append/receipt APIで毎回構築し、
scratchpadのJSONを盲目的に取り込まない。rootはlexical claimのみで作成しない。
レビューの参考bytesとの差は固定root・fixture構成の違いによる。

| fixture | records | canonical bytes | reservations | plans | references | single bytes |
|---|---:|---:|---:|---:|---:|---:|
| 合法850 exact | 246 | 465,412 | 3 | 2 | 850 | 14,949 |
| retry正常control | 205 | 371,992 | 6 | 2 | 579 | 14,981 |
| 末尾404不正retry | 205 | 371,971 | 6 | 2 | 579 | 14,981 |

六軸を固定期待値と比較し、人工Profile B候補の全上限内であることをassertする。
合法850は両verifierで受理、同じ入力をreferences=849で両方とも
`budget_v2_limit_references`として拒否する。受理時canonical bytesも一致する。
これは以前未確認だったexact-850の回帰を補うもので、旧benchmarkの測定結果を
書き換えたり公開ValidationLimitsを承認したりするものではない。

末尾retryはA/B/A/B/B/Aの順に6予約を作り、各planの試行は3回に限定する。
正常controlではAの途中応答を429、不正履歴では404とする。
最初の5予約までのprefixは両方とも正常。最後の不正A予約のみ、
`first_violation=5`、`budget_v2_previous_attempt_not_retryable`で拒否される。
assessmentの理由集合がこの1件だけであることと、全operationの構造検証が通ることも
確認し、retry回数上限等の別理由を誤って成功扱いしない。両verifierの結果は一致し、
pure authorityの3 flagsはすべてfalse。

### 実行環境と証拠の区別

Python 3.11.5（macOS x86_64）をPATH外の既存Homebrew Cellarで確認した。
pytestは未導入のため、対象worktreeのproduction moduleをimportし、同worktreeの
テストから監査定義だけを読み込むstdlib-only runnerで21 fingerprintと7 mutationを
実行した。3.13.3（macOS arm64）でも同じrunnerの結果が一致した。
これは3.11のpytest実行や全依存環境の互換性確認を意味しない。
3.12実行系は確認したローカル環境に見つからず、3.12は未実行。
新規依存の導入や既存venvの変更はしていない。

既存benchmark archiveは変更しない。SHA-256は引き続き
`f45e9225d2c50e7f9bca439d11d1c2c8beaf253f92fad3990cdc2dc2707af6e2`。
既存History class・mixed_history/mixed_resultのASTも旧commitと同一で、既存人工fixtureの
生成意味は不変。旧benchmarkは旧commitの証拠として保持し、新しい測定へ読み替えない。
Linux Python 3.13の性能benchmark、A複合Reference完了、候補Python peak超過、
公開上限/SLAのOwner判断は未完了のまま。

M-VL-1/L-VL-1/L-VL-2の最終解消判定は独立差分レビューへ引き継ぐ。
push/PR/CIはまだ実施せず、独立レビューと別の実行承認を待つ。
Merge・正式再受入はNo。Live Gateはclosed、実J-QuantsとFormal Real OOSは不可。

### 限定是正後のローカル検証

Python 3.13.3 / macOS arm64で次を実行した。新規回帰は35件で、production sourceの
差分はゼロ。Budget v1/v2の90件はfeasibility全体・全pytestに含まれている。

| 検査 | 結果 |
|---|---|
| 2C専用（warningをerror扱い） | 191 passed |
| feasibility全体 | 1584 passed |
| 全pytest | 3067 passed / 13 skipped / 0 failed / 0 errors |
| Ruff check | pass |
| Ruff format --check | pass（312 files） |
| git diff --check | pass |

skipはLive 7件・browser 6件で、未検証として維持する。全pytestの28 warningsは
既存`jquants_v2.py`のurllib3 `allowed_methods`に関するFutureWarningである。
通常pytestの成功をLinux性能測定や実HTTP取得の検証には読み替えない。
実行コマンド、ログ、3.11/3.13静的監査結果、commitとの対応は別のfollow-up証拠へ保存し、
既存benchmark原本・archiveの内容とSHAは変更していない。
