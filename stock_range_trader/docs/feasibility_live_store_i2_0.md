# I2-0: terminal evidence assessment and quiescence

Fixed integration base: `27a20aa032be604cbf5a19d97d5cc06995718043`.
Fixed tree: `8de43353c69b5f68e3d32a95122de26c041d7982`.

This increment implements logical assessment and the versioned close boundary,
not HTTP mutation or a physical evidence producer. OD-I2-01/02/03/04/11/12 apply:
I2/I3 separation, independent I2-0, fully terminal evidence with required holds
released, artificial-only physical claims, full validation before optimization,
and no recovery implementation.

## Authority and API

`live_store_terminal.assess_terminal_state(session)` is a typed, read-only public
operation. Only an existing `StoreSession` is accepted. There is no connection,
SQL, callback, caller head/catalog/clock, clean flag, or physical attestation
argument. It reads under the existing session transaction/lock and rolls back
without modifying runtime events, session/fence, stops, journals or receipts.
The result is immutable evidence, not a capability. Send, reservation, writer,
recovery and stop-clear permission fields are always false.

The existing `_begin`, runtime, physical pin, PID/thread, flock, SQLite PRAGMA,
session/fence and global I1b validation are unchanged. If those checks reject a
pending/corrupt/unsupported Store before assessment, the public API raises and
invalidates the handle according to the existing read contract. It does not
promise a diagnostic value for a Store it cannot safely read. The private pure
journal oracle can classify pending artificial states without admitting any
business operation. There is no `allow_pending` switch.

## Logical and physical assessment

The private oracle reconstructs C2 and C3 journals from their original records,
then reconciles the complete account, all plans and all corresponding bodies.
Ordering of plans/inventory/dependencies is canonical. It retains header
evidence, outcomes, exact heads, hold versions/releases, C3 root/contract digests
and the entire storage projection, including conservative outstanding charges.
Duplicate/unrelated/missing inventories do not establish completeness.

`logical_status` and `physical_status` are distinct. Final `classification` is
one of `terminal_proven`, `incomplete`, `inconsistent`, `recovery_required`,
`stop_blocked`, or `unsupported_physical_evidence`. Only `terminal_proven` is an
informational clean candidate; close always performs its own authoritative read.

`PhysicalDependency(kind, subject, evidence_sha)` names a required proof bound
to exact evidence. Kinds are transport termination, root/inventory custody,
writer stability and review authenticity. These are dependency identities,
**not attestations**. I2-0 accepts no producer ID, producer schema, proof payload
or caller assertion as physical authority. I3's formal proof format and trusted
producer registration are not implemented. Every attempt (including pre-send
reclaim) needs transport/worker evidence. Sent attempts missing a body history
are logically incomplete. Closed/stable body claims still need actual FD
termination, root identity, recount/rehash and stable bytes from a future trusted
producer. Synthetic complete evidence therefore remains physically unsupported.

Unknowns retain their original outcome. A historically valid manual release can
make the logical journal terminal, but it does not prove the review authentic,
the worker stopped, or the outcome successful. No automatic outcome repair or
promotion occurs. Persistent stop and control recovery remain blockers.

## Holds and historical semantics

Account journal is the sole hold authority. Original C2 replay checks hold ID,
latest version SHA, expected hold hash, recorded release time, known wait floor,
manual review reference and inconsistency repair reference. Required coverage
must dominate each attempt's final known lower bound. Merely waiting until a
deadline is not release. Release is evaluated at its recorded historical time,
not against today's date, and cannot clear an open attempt.

For pre-send reclaim, the matching plan reservation may remain reserved only
under C2's existing reconciled reclaim rule. An unresolved C3 reservation or
writer still blocks logical completion; even the no-writer synthetic case needs
external termination/review evidence before production clean admission.

## State matrix

| Evidence | Logical assessment | Production clean close |
|---|---|---|
| Existing valid idle I1b | terminal_proven | Existing v3 profile |
| Reserved / sent / headers without terminal result | incomplete | Reject |
| Complete response with required hold still active | incomplete | Reject |
| Active manual hold / unresolved unknown | recovery_required | Reject |
| Complete 200, stable committed body, valid released hold | terminal_proven | Unsupported physical producer; reject |
| Complete non-200, stable quarantined body, released hold | terminal_proven | Unsupported physical producer; reject |
| Unknown with reviewed release and stable quarantine | Logical candidate only | Unsupported physical producer; reject |
| Pre-send reclaim, unresolved C3 reservation | incomplete | Reject |
| Pre-send reclaim without body history, valid review/release | Logical candidate only | Unsupported physical producer; reject |
| Writer open / closed without stability | incomplete | Reject |
| Writer unknown / partial or orphan body | recovery_required | Reject |
| Corrupt/stale release, contradictory journals | inconsistent or guard exception | Reject |
| Pending operation / missing event receipt coverage | incomplete | Reject |
| Persistent stop / wrong fence | stop_blocked or guard exception | Reject |

C3 committed/quarantine are terminal *logical* object states only after closed
writer and stability declarations. Quarantine is not a refund: retained bytes,
cumulative acquisition and unresolved acquisition charges remain in the hashed
inventory. Extra audit/history is not accepted as proof of no physical change.

## Store completeness and close

The new profile requires every persisted event to have an event part belonging
to a committed operation with a valid original receipt. A self-consistent
synthetic hold/release inserted directly into journals without an operation is
not sufficient. I2-1/2/3 must introduce reviewed historical operation rules;
I2-0 installs none of them. Unknown operation kinds cannot bypass receipt replay.

`StoreSession.close()` selects the new profile only for attempt/hold history or
noninitial body history. Inside the existing SQLite transaction it re-reads and
validates current evidence. Failure produces no `session_closed` marker, rolls
back uncommitted clock/close evidence and invalidates the session. Dirty restart
and original receipt-only retrieval retain their prior meaning. No recovery,
repin or stop-clear is added. Existing `_reconcile` and `_validate_current` have
not been weakened or modified.

Profile: `local-http-terminal-evidence-v1`.
Snapshot: `historical-feasibility-runtime-clean-validation-v4`.

Snapshot binds deployment/Store/account, session/fence, runtime head, owner pin,
physical Store pin, full catalog, all journal heads, operation/receipt-set digest,
account-wide assessment (including account and plan heads), C3 inventory digest,
historical hold/release evidence and physical dependency identities. Canonical
snapshot bytes are hashed into the existing `validation_sha`; the runtime event
schema is unchanged. No snapshot or receipt is rewritten retroactively.

Empty v1, I1a-3 nonempty v2 and I1b initial-C3 v3 selectors/validators and snapshot
bytes are preserved. The v4 builder is tested on an eligible idle artificial
Store through a private test seam; this is not a claim that production HTTP
history can close in I2-0. The production selector retains v3 for that idle case.

Unchanged: Store v1/v2 DDL, Store schema version, RuntimePolicy/runtime events,
C1/C2/C3 persisted schemas, six-field receipts and all historical I1a/I1b kinds.

## Verification and measurement

Dedicated tests use pure synthetic journals and private temporary physical Stores
with the real StoreSession. They cover matrix classification, release boundaries,
tamper, missing body/receipt coverage, pending operations, persistent stop/fence,
read-only behavior, old v3 byte equality, reopen/original receipt, exclusive flock
and spawned process crashes. Failures are injected during terminal/hold/inventory
validation, snapshot construction, before session_closed, before commit and
after commit/before return. Before commit no partial marker is durable; after
commit existing runtime rules determine reopening. No writer is physically
implemented or authenticated in these tests.

Performance tests retain full validation. They separately time global Store
validation, runtime replay, I1b manifests, prefix replay, receipt validation, C2,
C3, terminal assessment, snapshot canonicalization and total close. Each component
has three repeats; close is measured once per independent artificial Store.
Printouts include actual counts and seconds, not just a favorable aggregate.

Scale axes: prepared plans +2; pending operations +2; account events +4 (two
hold/release pairs); body audits +2; runtime events +2. Receipt growth uses A
versus A/B: 2 versus 3 committed operations/receipts, 1 versus 2 enrolled plans,
4 versus 7 total journal events and 7 versus 9 runtime events. Committed receipts
cannot be increased independently of operations under the existing one-to-one
schema; this coupling is explicitly reported rather than fabricated. Negative
pending/unowned-event workloads measure rejection, not successful close.

These small artificial timings do not establish production throughput or an SLA.
One dedicated-test measurement on macOS 15.7.7 arm64/APFS, Python 3.13.3,
SQLite 3.49.1 produced the following milliseconds. Component values are medians
of three runs; close is one observation. A has 2 operations/receipts, 1 plan,
4 journal events, 1 body event and 7 runtime events. A/B has 3 operations/receipts,
2 plans, 7 journal events, 2 body events and 9 runtime events.

| Component | A | A/B |
|---|---:|---:|
| Store global validation | 2.369 | 3.273 |
| Runtime replay | 1.239 | 1.433 |
| I1b manifests | 9.493 | 20.078 |
| Journal prefix replay | 1.248 | 2.381 |
| Receipts | 16.841 | 36.879 |
| C2 reconciliation | 0.444 | 1.017 |
| C3 reconciliation | 1.190 | 3.726 |
| Terminal assessment | 35.773 | 84.055 |
| Snapshot canonicalization | 0.151 | 0.199 |
| Close (legacy v3 success) | 83.845 | 164.409 |

The six scale axes were measured at baseline and +2 variants. Prepared-plan
growth produced a 36.592 ms terminal median; runtime growth 36.922 ms. Negative
workloads had early-rejection terminal medians of 2.447 ms (pending operations),
25.416 ms (unowned C2 hold/release events), and 25.351 ms (unowned extra body
audits). Their shorter times do not indicate a faster successful validation.
The per-component measurements overlap internally and must not be summed as
exclusive timings. Complete samples/counts are emitted by the dedicated test.

No persistent validation cache is introduced. Global replay is not optimized
away. Linux full matrix is retained and the macOS targeted job includes I2-0 with
`-W error`. CI is not claimed executed by this local-only change. Live 7/browser 6
remain unverified when skipped.

### Local verification

Final local verification on the macOS/APFS environment above:

- I2-0 dedicated tests: 61 passed, with `-W error`.
- I1a Store regression: 419 passed; I1b regression: 73 passed. These ran
  with the then-current 59 I2-0 tests (551 passed, with `-W error`); the
  final two additional I2-0 tests passed in the dedicated and full suites.
- Feasibility suite: 1288 passed.
- Full pytest: 2771 passed, 13 skipped, 28 warnings in 739.15 seconds.
- Ruff lint, Ruff format check and `git diff --check`: passed.

The 13 skipped tests are 7 Live and 6 headless-browser tests, not verified
successes. The 28 warnings originate from the existing urllib3 Retry
configuration in `data/providers/jquants_v2.py`. Linux CI and the updated
macOS CI job have not been executed for this local-only change. No production
physical writer or real HTTP acquisition was tested.

## Findings and next boundary

L-I1A3-1 is partially addressed: logical terminal/quiescence foundation and
versioned close boundary exist, but true HTTP/physical end-to-end closure requires
reviewed future operation rules and an I3 producer. It is **not resolved**.
L-I1A2A-2, L-I1A3-3 and L-I1B-1 performance findings remain open. All other prior
findings retain their status; this change makes no new resolved claim.

I2-1 reservation, I2-2 Transport, I2-3 outcome/hold mutation and I3 physical writer
are not implemented. No credentials or market data are read. Live Gate remains
closed, remote account identity remains unverified, actual J-Quants acquisition
and Formal Real OOS registration/execution remain prohibited. Frozen June
worktree and artifacts are untouched. Independent review is required before
acceptance; a local synthetic terminal result is never live permission.
