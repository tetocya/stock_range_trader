# I1a-2A: Store v2 runtime persistence contract

Fixed base: `d38ad713a97c0b049ccfd1985cf930f4a4537d40`.
Branch: `real-oos/i1a2a-live-store-runtime-contract`.

This stage implements canonical value types, pure replay and validation of an
artificial SQLite Store v2. The private initializer takes an already supplied
fresh artificial connection. It does not select/open a production path, capture
physical identities or clocks, acquire a lock, or return an operational session.
Owner-approved decisions in the I1a-2A instruction are the policy authority.

## Version boundary

`historical-feasibility-live-store-v2` is a complete new schema, not an addition
to an existing v1 database. The v1 public initializer/validator and original DDL
remain separate. Both validators reject the other version. There is no ALTER,
metadata relabeling, migration, event reserialization or hash regeneration.

The private shared relational validator retains the I1a-1 policy, document,
business event, head, operation and receipt rules. Only the expected deployment
envelope is version-specific. v2 uses deployment envelope
`historical-feasibility-live-store-deployment-v2`, with immutable initial lifecycle
`bootstrap_pending`; current lifecycle comes from runtime replay. This avoids
claiming that a pending bootstrap is already prepared. Existing StorePolicy v1
and its four explicit capacities are unchanged. Formal N remains undecided;
tests use N=2, not a production default.

v2 adds six tables: `runtime_policies`, `owner_deployment_records`,
`runtime_bindings`, `runtime_events`, `runtime_current`, `runtime_sessions`.
The complete schema has 20 tables and 39 triggers. Exact DDL/object definitions,
metadata and foreign keys are validated. Unknown, missing, changed or additional
objects are rejected without repair. Original runtime policy, owner records,
binding and event rows have UPDATE/DELETE/replacement guards. Current/session
rows are projections checked byte-for-byte against replay, not authority.

## Runtime policy

`historical-feasibility-live-store-runtime-policy-v1` stores canonical UTF-8 bytes
and SHA-256. The accepted values form a fixed contract; an unapproved threshold,
source, platform, takeover or SQLite option is rejected.

| Responsibility | Fixed policy |
| --- | --- |
| Persistent UTC | Store-owned UTC, captured only in I1a-2B |
| Elapsed time | process-local `monotonic_ns()` |
| Drift stop | absolute anchor drift strictly greater than 5,000,000,000 ns |
| Independent clock stops | UTC or monotonic regression |
| Session suspend | unsupported; complete detection is not guaranteed |
| Restart | dirty restart requires manual block; automatic takeover false |
| Lock | dedicated persistent file; exclusive nonblocking flock in I1a-2B |
| Platforms/filesystems | macOS and Linux, owner-managed local filesystem only |
| SQLite minimum | 3.31.0 plus actual capability/readback checks in I1a-2B |
| SQLite configuration | foreign_keys ON, recursive_triggers ON, DELETE, EXTRA, busy_timeout 0, NORMAL, trusted_schema OFF, ignore_check_constraints OFF |
| fullfsync | required ON on macOS; not a Linux durability guarantee |

I1a-2A only requires FK/recursive-trigger safety on its supplied artificial
connection. Applying all runtime PRAGMAs, including platform checks and failure
without fallback, is I1a-2B work. The persistent lock file is not deleted on
session close. TTL, absent PID or elapsed time never authorizes takeover.

## Owner and physical formats

`historical-feasibility-live-store-owner-deployment-v1` binds deployment/account,
C1 registry/preflight/identity digests, Store/runtime policy digests, canonical
root, DB/lock paths, bootstrap intent, policy decision reference and approval
status. Approval status is a declaration, not acquisition permission or proof
of authenticity. The C1 registry schema is unchanged.

The account path is derived lexically from the C1 owner root as
`accounts/<account_ref>/account-rate-ledger.sqlite3`; the dedicated sibling lock
is `account-rate-ledger.lock`. Relative paths, `..`, noncanonical paths and C1
forbidden roots are rejected. Arbitrary DB paths are not runtime authority.
Stored originals must agree with the declared C1 root, ledger path, registry,
account and identity digest. I1b still owns complete typed source reconstruction
and activation binding checks; L-C2-4 is not closed here.

Intent records contain no allocated Store UUID, physical binding or pin evidence.
Pinned records require a canonical UUID, physical identities, created-event hash,
owner pin reference and approved fixed policy decision. Intent/pin bindings must
agree exactly. Policy approval must already be fixed in the intent before pinning;
updating a pending policy decision is not a runtime operation in this stage.

`historical-feasibility-live-store-physical-binding-v1` contains device/inode,
object kind and link count for root, account directory, DB and lock. The first
two must be directories; DB/lock must be regular files with link count exactly 1.
The four claimed device/inode pairs must be distinct. Values are validated only:
no stat/realpath/open is performed, and physical existence/custody is not proved.
Physical replacement or root movement cannot silently update a pin.

## Runtime evidence and transitions

`historical-feasibility-live-store-runtime-event-v1` is separate from C1/C2/C3
journals. Sequence, previous hash and event hash determine order. Canonical
fields bind deployment, Store UUID, runtime policy, optional session, fence epoch,
observed UTC/monotonic ns, transition ID and a strict kind-specific payload.
Event kind plus schema version determines payload semantics; unknown fields and
kinds are rejected. Transition IDs cannot repeat within a runtime chain.

| Event | Preconditions and effect |
| --- | --- |
| bootstrap_created | first event only; intent, UUID, initial physical claim, epoch 0; becomes bootstrap_pending |
| bootstrap_finalized | pending only; explicit matching owner pin, created-event head and physical digest; becomes prepared |
| session_started | prepared, no stop, first/clean prior session, unused ID; epoch increases exactly by 1; records new clock anchor |
| clock_observed | running session, matching epoch/ID, valid clock sample; records accepted sample |
| runtime_stop_entered | records clock/restart/runtime-integrity uncertainty; sticky terminal stop |
| session_closed | running, valid clock, matching session/epoch/predecessor, quiescent assertion and validation digest; records clean quiescence |

There is no clear/recovered event and no active lifecycle. Normal replay never
finalizes bootstrap automatically. A saved owner pin is checked against created
evidence even before finalization. Stop forbids later starts, finalization,
observations and clean close; a new epoch or normal clock cannot clear it.

`historical-feasibility-live-store-runtime-projection-v1` retains lifecycle, head,
epoch, current session/state, sticky reason and uncertainty flags, last accepted
UTC/monotonic sample, last clean session and bootstrap/physical/pin references.
`historical-feasibility-live-store-session-v1` retains deployment/Store/session,
epoch, original start/close event references, startup anchor, policy and status.
Session ID reuse, skipped/decreased epochs and signed SQLite INTEGER overflow
are rejected. Epoch is distinct from request generation, enrollment revision
and logical operation ID. No randomness or PID authority is introduced here.

## Clock and restart semantics

ClockObservation uses exact aware-UTC microsecond ISO text and nonnegative integer
nanoseconds. Booleans/floats/NaN/negative samples are rejected. Delta arithmetic
uses integer timedelta days/seconds/microseconds converted to nanoseconds.
Positive and negative 4.999999 s and 5.000000 s drift are allowed by the drift
rule; 5.000001 s stops. UTC regression by 1 microsecond or monotonic regression
stops independently. Equal UTC remains equal: no invented microsecond is added.

`record_clock_observation` is a pure candidate constructor: normal samples append
clock_observed; anomalous samples append runtime_stop_entered with the original
observation and derived diagnostic reasons. Last accepted projection samples
remain unchanged after a stop. Raw backward UTC is retained in the runtime
original; existing C1/C2/C3 UTC nonregression is not relaxed.

Anchors reset only at a valid new session. During dirty restart, a new process's
monotonic sample cannot be compared to the prior process. The explicit restart
stop retains restart_uncertain, and additionally clock_uncertain if the persisted
UTC reference has regressed. Pure next-open assessment blocks every unclosed
running session, even if the start committed before its caller received a handle.
It does not auto-append evidence, rollback committed starts or clear a stop.

session_closed means application quiescence evidence, not proof of process exit.
Its assertion/digest do not perform business-pending verification here. I1a-3 must
verify quiescence before emitting it. A crash after a valid marker leaves a clean
restart candidate; I1a-2B still must validate physical identity, DB, OS lock,
unresolved state and prevention of new side effects by the old session. No TTL,
new fence, lock possession, normalized clock or process absence clears a dirty
restart. There is no recovery-clear API in I1a-2A.

Artificial UTC/monotonic discrepancy tests are not real suspend tests. Short
suspend or cancelling clock changes may escape this detector; I2 must address
suspend during real HTTP.

## Validation and handoff

Public functions are pure runtime replay, candidate event/clock construction,
startup assessment and read-only v2 schema/content validation. The initializer is
private and artificial-only. No operational StoreSession, raw-connection opening
API, physical bootstrap, lock acquisition, production clock, activation,
enrollment mutation, reservation, send or network path is exposed.

Tests exercise fresh in-memory/file databases, exact original round trips,
bootstrap pin failures, epoch/session/transition errors, clock boundaries,
sticky stop/restart, projection and schema tampering, immutable originals,
rollback, v1/v2 mutual rejection and unchanged business journals/receipts.
Real multiprocess/crash/lock, filesystem alias/TOCTOU, power loss and platform
PRAGMA capability tests remain I1a-2B or later. Tests do not touch frozen June
artifacts, market data, account databases or acquisition approvals.

Open findings remain L-I1A1-1/2, L-EN-1/2/3, L-C2-2/3/4/6/7 and L-C3-2.
L-I1A1-1/2 chiefly belong to I1a-3; source reconstruction L-C2-4 remains I1b.
M-CMP-1 and L-CMP-1 remain resolved. No physical contradiction event for L-C3-2
is added. Full physical Store, malicious administrator protection, snapshot
rollback detection, remote identity/owner authenticity and operational durability
are not established. Live Gate remains closed; actual J-Quants acquisition and
Formal Real OOS are unavailable.

## Local verification

Environment: Python 3.13.3, SQLite 3.49.1, pytest 9.1.1, Ruff 0.16.8.

| Check | Result |
| --- | --- |
| New I1a-2A cases | 167 passed |
| I1a-2A + unchanged I1a-1, with warnings treated as errors | 264 passed (167 + 97) |
| Feasibility, including C1/C2/enrollment/C3/compatibility | 999 passed |
| Full pytest | 2482 passed / 13 skipped / 0 failed |
| Ruff lint / format | pass |
| Diff whitespace validation | pass |

The full run took 688.02 seconds. Its 28 warnings are the existing urllib3
`allowed_methods` warning from `jquants_v2.py:411`; new warnings: zero.
Live 7 and browser 6 skips remain unverified. Live/UI opt-ins were disabled.
No market API, frozen June artifacts, or real-data settlement was used.
This is local self-verification; GitHub CI and independent review are not yet
performed for this change. No I1a-2B implementation or live permission follows
from these results.
