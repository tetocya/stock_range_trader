# I1a-2B: exclusive operational Store v2

固定baseは `9c2aa97360d50b37feb2be607ad876008b1df7f7`。
I1a-2Aのschema、canonical bytes、runtime reducerを変更せず、
filesystem、OS lock、SQLite、Store内部の時計へ接続する。
実装は `feasibility/live_store_operational.py` に隔離する。

これはruntime persistence専用の実装である。business mutation、plan activation、
enrollment、reservation/send、HTTP、credential読取、receipt commit、
manual recovery/repin、stop clear、Formal Real OOSは実装しない。
Live GateとC1/C2/C3、Store v1/v2の契約は変更しない。

## Authorityと公開API

`OperationalAuthority(preflight, intent, policy, runtime_policy, pin=None)` は、
検証済みのC1 preflightとowner snapshotを受け取る。
型、canonical content、registry/identity/preflight/policyのhash、account、
root/path、intent/pinのbindingを再検証する。owner署名やremote account identityの
真正性は確認しない。owner管理ファイルのloaderやstorage pathを新しいauthorityにはしない。
呼出側は事前検証したsnapshotを渡し、通常openではowner原本を書き換えない。

| API | 結果・制限 |
|---|---|
| `create_bootstrap(authority)` | intentのみ。排他的にlock/DBを作成し、pin候補を返す。sessionは返さない |
| `finalize_bootstrap(authority)` | owner作成のpinが必須。明示的にpreparedへ遷移する。sessionは返さない |
| `open_store(authority)` | prepared/clean、physical、schema、runtimeの検証後にだけStoreSessionを返す |
| `StoreSession.identity/status` | 読取専用。session identityはfrozen value |
| `StoreSession.checkpoint_clock()` | production clock observationまたはsticky stopをatomicに記録 |
| `StoreSession.close()` | 条件を証明できる場合のみclean markerを記録してresourceを解放 |

public open/bootstrap APIに任意path、clock、session ID、Store UUID、connectionを渡す
引数はない。SQL、enroll、reserve、send、body commit、repin、permissionのAPIはない。
private internalsへの同一process内の任意コードアクセスまでをsecurity boundaryとはしない。

## Physical root・custody・filesystem

既存C1規則から次のpathだけを導出する。

```text
<canonical root>/accounts/<account_ref>/account-rate-ledger.sqlite3
<canonical root>/accounts/<account_ref>/account-rate-ledger.lock
```

ownerがroot、`accounts`、account directoryを事前に用意する。
この層はそれらを自動作成しない。相対path、`..`、checkout内、一時rootなどは
既存C1で拒否する。C1が返したcanonical pathを、`/`からdirfd相対の
`O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC`で一段ずつ開く。
`Path.resolve()`の結果をauthorityにすることはない。

root/accounts/accountはdirectory、DB/lockはregular fileを要求する。
current effective UID所有、group/other write禁止、DB/lockの`st_nlink == 1`を確認する。
祖先directoryはroot UIDまたはcurrent effective UID所有かつgroup/other write禁止。
全FDはnon-inheritableで、root/accounts/account/DB/lockのFDをsession中保持する。

pin照合はdevice/inode/object kindを使用する。regular fileの単一link条件は常に確認する。
**directoryのlink countは永続identityにしない。** APFSではSQLiteの一時journal entryでも
directory link countが変わるためである。既存PhysicalBinding bytesは変更せず、
記録値を保持したまま物理照合だけをdevice/inode/kindに限定する。

native `fstatfs`でpositive local filesystemを確認する。

| OS | 受理条件 |
|---|---|
| macOS 64-bit | `MNT_LOCAL`かつAPFS/HFS。Darwin SDKの`__DARWIN_STRUCT_STATFS64`配置を使用 |
| Linux 64-bit | native `f_type`がext2/3/4、XFS、Btrfsの既知magic |

network/FUSE/overlay/未知type、非対応OS/ABI、probe失敗は拒否する。
Linuxはnative long alignmentを持つ4096-byteのbufferで`statfs`結果を受け、先頭の
`f_type`だけを読む。新規dependency、mount操作、network probeはない。
これはOSが報告するlocal FS typeの確認であり、背後のblock deviceの真正性、
network-backed block deviceの不存在、電源断耐久性を証明しない。

## Lock・DB open

lockはdedicated persistent fileで、`flock(LOCK_EX | LOCK_NB)`を使う。
競合時は`store_lock_unavailable`で直ちに拒否する。
TTL、PID不存在、session age、wall clock、new epochをtakeover根拠にしない。
normal openは既存lock/DBを要求し、欠落時に再作成しない。
close時もlock fileは削除しない。

bootstrapだけはlockとDBを`O_CREAT | O_EXCL | O_RDWR | O_NOFOLLOW | O_CLOEXEC`、
mode 0600で作成する。既存のempty/partial DBも上書き・削除・再初期化しない。
途中失敗で残ったfileは証拠としてそのまま残し、再試行時は拒否する。
directory/file fsyncを行うが、電源断を含む全障害の耐久性検証は別途必要。

DBをno-follow FDで先にpinし、SQLiteはURI `mode=rw`、`isolation_level=None`、
`timeout=0`で接続する。SQLiteの暗黙create、`immutable=1`、`nolock=1`は使わない。
接続後とruntime mutationの前後でpath/held FD/pinを照合する。
Python sqlite3には既存FDを接続へ渡すAPIがないため、同一権限の悪意processによる
swap-and-restoreを含む全TOCTOUを防げるとは主張しない。

## SQLiteの実設定

実connectionの`sqlite_version()`が3.31.0以上であることを確認する。
各connectionに以下を設定し、readback不一致なら拒否する。

| PRAGMA | 値 |
|---|---|
| foreign_keys / recursive_triggers | ON |
| journal_mode | DELETE |
| synchronous | EXTRA (3) |
| busy_timeout | 0 |
| locking_mode | NORMAL |
| trusted_schema / ignore_check_constraints | OFF |
| fullfsync | macOSではON必須。Linuxでは耐久保証条件にしない |

normal openでは既存journal modeがDELETE以外なら**変換せず拒否**する。
FK、synchronous等はconnection-localな設定であり、他connectionの設定値を
永続DB属性とは解釈しない。新connectionへ強制設定後に検証し、session内の変更も
guardで拒否する。WAL変換による救済はない。

`quick_check`、`foreign_key_check`、v2 exact DDL検証、canonical content検証、
runtime projection replayを行う。application repair/migrationはしない。
SQLite自身が行うhot-journal rollbackはapplication repairとは区別する。

## Bootstrapとruntime transaction

createはowner intent検証、custody/lock/DB作成後、内部`uuid4()`でStore UUIDを作る。
I1a-2Aのartificial initializerを呼ばず、同じDDL定義を使用したproduction用bootstrapを
`BEGIN IMMEDIATE`で行い、`bootstrap_created`とprojectionをcommitする。
`BootstrapPinCandidate`にはdeployment/account/UUID/PhysicalBinding/bootstrap hash/
owner intent hash/Store policy hash/runtime policy hashを含める。

ownerが別途作成したpinを明示finalizationへ渡す。physical identity、SQLite、
original evidenceを再確認し、pin recordと`bootstrap_finalized`を同一transactionで記録する。
通常openはvalid pinがあってもbootstrap_pendingを自動finalizeしない。

runtime mutationの順序は次のとおり。

```text
PID/thread + physical/lock guard
→ BEGIN IMMEDIATE
→ DB原本・projection・現session/epoch/head再検証
→ Store clock capture
→ pure candidate construction
→ event + projection + session rows
→ validation + physical guard
→ COMMIT
```

caller supplied headをauthorityにしない。session IDも内部`uuid4()`で生成する。
`session_started`commit後にだけhandleを返す。commit後・返却前のcrashでも
sessionはunclosedとして残り、次openをdirty restartでblockする。

## 時計・stop・process guard

capture順序は `monotonic_ns(before) → datetime.now(UTC) → monotonic_ns(after)`。
ClockObservationには実測した**after**の値を使い、中点などの架空sampleは作らない。
内部capture自体のmonotonic後退も拒否する。public時計注入はなく、人工テストの
private monkeypatchだけを認める。

exact ±5秒、UTC-backward、monotonic-backward、sticky stopの意味は2Aを維持する。
clock異常は`runtime_stop_entered`をpersistしてhandleをinvalidにする。
persist失敗時も継続・clean closeを許さず、未commit分をrollbackしてresourceを解放する。
次openは残ったunclosed sessionを検出する。

dirty restartでは新processのmonotonicを旧anchorと比較しない。
可能なら`restart_uncertain`をpersistし、失敗してもopenは失敗する。
既存stopはepoch更新・時計正常化・lock再取得で消えない。
startup時のUTC後退もsession開始前にsticky clock stopとして記録する。

各操作は作成PIDとthreadを要求する。fork childからcheckpoint/closeを呼んでも
parent SQLiteを操作・closeせず、`LOCK_UN`しない。spawn/execを推奨する。
fork後のFD継承そのものを消す仕組みではなく、childが長時間生存するとlock lifetimeを
延ばし得る。child試験は`os._exit()`で終了させる。destructorによるclean closeはない。
path replacement後はhandleをinvalidにし、lockの取り直しや自動repinをしない。

## Clean closeの証明範囲

closeはguard、production clock checkpoint、full validationを同一transactionで行う。
I1a-3のauthoritative reconciliationが未実装のため、**business catalogが空の場合だけ**
quiescenceを認める。accounts/plans/journals/events/operations/receipts/control_state/
journal_headsのいずれかに行があれば、たとえ一見preparedでもclean markerを作らない。
これは意図した保守的制限であり、将来の非空catalog対応はI1a-3側で設計する。

`validation_sha`はStore内部で計算するversioned canonical snapshotのdigest。
Store schema/deployment/store/account/session/fence/runtime policy/current runtime head/
owner pin hash/physical hash/空journal heads/business row countsを束縛する。
callerが任意SHAやquiescent=Trueを渡すAPIはない。

`session_closed`commit後にSQLite、pin FDs、最後にOS lockを解放する。
clean marker直後・resource解放前のcrashは、OS解放後の次openで再検証して
clean candidateとして扱える。stop/validation失敗時にはclean markerを作らない。

## 検証・性能

人工Storeはhome直下の専用TemporaryDirectoryに作り、終了時にそのdirectoryだけを
cleanupする。C1のtemporary/checkout root拒否をpatchしない。既存6月環境は使用しない。
実subprocess/pipeでlock contention、bootstrap race、dirty exit、start commit直後と
clean marker直後のcrashを検証する。固定sleepによる同期はしない。
symlink/hardlink/copy replacement、owner/pin mismatch、connection PRAGMA、WAL、
clock境界、stop persist失敗、projection tamper、fork/thread拒否を含む。

ローカル測定環境はmacOS 15.7.7 arm64、Python 3.13.3、SQLite 3.49.1。
APFS、flock、no-follow、stat/fstat、fullfsync readback、production clockを実際に使用した。

| 追加人工clock events | pure replay秒 | operational startup秒 |
|---|---:|---:|
| 10 | 0.000928 | 0.034740 |
| 100 | 0.005420 | 0.063923 |
| 1000 | 0.048657 | 0.419756 |

単回の参考値で、最適化・production timing閾値・正式Nは導入していない。
full-chain replayはO(N)のままで、繰返しappend全体はO(N²)となり得る。

`.github/workflows/ci.yml`にmacOS/Python 3.13の専用`-W error`jobを追加した。
既存Ubuntu Python 3.11/3.12/3.13 full matrixは変更していない。
pushしない本段階では新jobもLinux上の2Bも未実行であり、local macOSの成功で
CI成功やLinux実環境検証済みとは表示しない。

### ローカル品質ゲート（2026-10-07）

| 検証 | 結果 |
|---|---|
| I1a-2B専用 | 79 passed、skip 0 |
| I1a-2B + I1a-2A + I1a-1、`-W error` | 343 passed、warning 0 |
| feasibility全体 | 1078 passed、warning 0 |
| 全pytest | 2561 passed / 13 skipped / 0 failed、694.54秒 |
| Ruff lint / format | 成功 / 289 files already formatted |
| diff whitespace検査 | 成功 |

全pytestの28 warningsは既存`jquants_v2.py:411`のurllib3警告のみ。
新規warningは0。13 skippedはLive 7件、browser 6件で、未検証として維持する。
初回の全feasibility試験で、pytest runnerに先行テストのthreadが残った状態の
forkに警告が出たため、fork試験をfresh spawn processへ隔離した。
warningを抑制せず、subprocessでもerrorとして再検証した。

自己点検ではarbitrary path、symlink/hardlink、owner pin、inode replacement、
lock最後解放、PID/fork/thread、WAL、fullfsync、dirty restart、stop保存失敗、
clean marker、repin/stop-clear不在を確認した。新たなCritical/High/Mediumは
検出していないが、これは実装者の点検であり独立レビュー完了を意味しない。
以下の既知Lowと制約をresolvedへ変更しない。

## 残存制約・handoff

- L-I1A2A-1はopen。primary stop reasonをschema変更で多値化しない。
- L-I1A2A-2はopen。operational ceiling、長い履歴、manual repin/recovery境界は未確定。
- L-I1A1-1/2とbusiness current-head/capacity/receipt transactionはI1a-3へ引継ぐ。
- L-EN-1/2/3、L-C2-2/3/4/6/7、L-C3-2もopen。M-CMP-1/L-CMP-1はresolved維持。
- 正式`max_enrolled_plans_per_account=N`は未決定。人工fixtureのN=2を採用値としない。
- physical custodyはowner管理local FSと協調clientが前提。同一UIDの悪意コード、
  mount namespaceやblock device真正性、任意FD強制操作までの防御は主張しない。
- registry/owner approval/remote account identityの真正性、Store外の操作との全調停、
  power-loss injection、完全なsuspend検出、automatic recoveryは未実装。
- StoreSessionはruntime handleでありacquisition/send/Formal OOS permissionではない。
  Live Gateはclosedのまま。実J-Quants通信・Formal Real OOSは不可。
