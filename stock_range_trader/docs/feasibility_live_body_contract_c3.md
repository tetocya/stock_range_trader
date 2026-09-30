# C3: pure live body/storage contract

Fixed implementation base: `b54bd86b21efbaaed1338cd17d13d44ce5bf308d`.
This is an in-memory evidence contract, not a live Store or acquisition runner.
No filesystem observation/write, SQL, process control, HTTP, credential read,
clock read, or live permission is implemented. Existing C1, C2 and artificial
Account Ledger v2 remain unchanged; their databases are not migrated or reinterpreted.

## Owner decisions (OD-C3-01 through OD-C3-08)

| Decision | Contract |
| --- | --- |
| 01 | Each attempt/request has its own independent write-once object. No content-addressed deduplication. |
| 02 | Quarantine does not refund retained capacity. |
| 03 | Reserve the entire required `per_object_max_bytes`, including unknown/untrusted Content-Length. No incremental chunk reservation. |
| 04 | Open/unknown/stale writer or missing post-close recount/rehash blocks new reservations. |
| 05 | No deletion API, automatic GC, or deletion-based budget refund. |
| 06 | At most one authoritative committed object per logical page; other attempt artifacts may remain. |
| 07 | Same operation ID and identical canonical evidence is idempotent; conflicting evidence is rejected. |
| 08 | Metadata, receipts, journals and SQLite are outside the retained-body budget. Actual disk occupancy and its operational limit are separate. |

Policy limits are mandatory positive int64 values, not implicit execution defaults.
Tests use explicitly artificial values. None of these decisions modifies strategy,
selection, June trial configuration, a checkpoint, or any Real OOS protocol.

## Module and API boundary

The implementation is `feasibility/live_body_contract.py`. Main immutable values:

- `FixedPlanSnapshot`, `StoragePolicy`, `LiveBodyStorageContract`.
- `LivePageIdentity`, `ObjectIdentity`, `LiveBodyObject`.
- `PhysicalRootIdentity`, `WriterEvidence`, `StableBodyEvidence`.
- `LiveBodyReceipt`, `QuarantineEvidence`, `BodyEvent`, `C2Heads`.
- `LiveStorageProjection`, `BodyAssessment`, `LiveBodyJournal`.

Journal operations return new values: `reserve`, `writer`, `observe`, `stabilize`,
`acquired`, `commit`, `quarantine_object`, `audit`. `append` is an evidence replay
primitive, not a Store authorization API. Constructing or replaying events can
never authorize a send or attest their real-world authenticity. Before using a
projection, call `reconcile_live_bodies` with all required C2 journals.

## Identities and M4

Query identity hashes the canonical endpoint/params from the fixed plan. Page
identity binds plan SHA, canonical query, page index, and requested cursor. The
cursor-revisit key deliberately excludes page index: incrementing the index does
not disguise revisiting a cursor. Retries of the same unfinished logical page
are distinct from advancing to another page. A noninitial page requires the
authoritative previous page's declared next cursor.

Object identity hashes plan, C1 preflight, C2 attempt/slot, holder, generation,
page, and the fixed `decoded_response_body` role. It is not derived from content.
One attempt/slot cannot obtain another object by changing page identity. A retry
uses a new attempt and object; it does not reopen an old object for appending.

The SHA-256 content digest covers persisted **decompressed payload bytes**, not
reformatted JSON. Two different pages may have the same digest and both commit,
provided their cursor progression is valid. Both acquisitions and both physical
objects count in full. A repeated next cursor can still be a loop even when the
body digest changes. Legacy v2 deduplication/rejection rules are untouched.

## Root authority and M5

`FixedPlanSnapshot.from_plan` accepts an existing `HttpAcquisitionPlan`, verifies
its fixed scope, and detaches its canonical content and hash. Loading C3 values
does not instantiate a legacy plan, call its filesystem path checks, or expand
its allowed acquisition scope. The snapshot is a hash-bound copy of an already
validated plan, not a second plan-authoring validator or approval authenticator.
Journal loading requires an externally expected contract and physical-root
claim; a self-consistent hash alone is not an external trust anchor.

There is no caller-supplied body-root argument. The fixed layout is:

```text
plan.output_dir/live-body-v1/
  staging/
  objects/
  quarantine/
  receipts/
```

All partial/staging locations, final object locations and quarantine destinations
are derived within that tree. An orphan may be a physically published object
whose commit was interrupted. A path name alone never proves completion.

Lexical input rejects relative paths, `/`, dot/dotdot components, repeated or
trailing separators, backslashes, expansion forms, URI-like forms, percent-based
alias forms and FD pseudo-paths. Inputs are rejected rather than silently
normalized. C1's owner central ledger root is a distinct authority; C3 neither
changes it nor assumes a body directory is that central Store.

`PhysicalRootIdentity` binds the storage root hash, owner Store reference,
canonical realpath, device, inode, mount reference, root epoch, observation UTC
and evidence reference. They are caller-provided claims only. A changed identity
cannot continue the same journal, even if its path string is unchanged. Inode
alone is not sufficient. Physical alias detection and root stability are I1/I3
responsibilities. Missing physical checks are not supplied by a C3 success.

## Lifecycle, writers and N3

Storage states: `reserved`, `staging`, `partial`, `orphan`, `committed`, `quarantine`.
Writer states: `not_started`, `open`, `closed`, `unknown`. These are separate axes.

```text
reserved -> staging -> committed
                    -> partial -> quarantine
                    -> orphan  -> quarantine
```

Each observation and transition records a monotonically increasing object
version. Objects in committed state reject further writes, digest/size changes,
page reassignment and quarantine. Only committed objects carry an authoritative
success receipt. Quarantine cannot be promoted to committed.

Writer evidence binds the C2 attempt, holder and generation, writer/session ID,
state, observation time/size, close/termination reference, and optional lease or
reclaim reference. Stale generation does not imply a closed OS file descriptor.
The same applies to an old FD after rename or hard-link/unlink quarantine.

Finalization requires declared evidence in this order:

1. Writer closed/terminated (all relevant writers, as an I3 claim).
2. Physical object/root identity checked.
3. Byte stability observed.
4. Size recounted.
5. Digest recomputed.
6. C2 complete response, page and current generation reconciled.
7. Authoritative receipt committed.

Writer closure alone does not release unused reservation or permit finalization.
`StableBodyEvidence` binds the exact closed-writer evidence hash, root and object,
ordered timestamps, measured size/digest and physical/stability/recount/rehash
references. C3 never performs these measurements itself. Final digest is taken
only from this evidence; staging/observed hashes are not final hashes.

Receipt binding includes contract/plan, C1 preflight, C2 attempt/slot, holder,
generation, page/object, root identity, stability evidence, size/digest, committed
UTC, C2 journal heads, next cursor and transition ID. C2's response outcome alone
is insufficient for an authoritative body: C3 also requires status 200.

## Capacity model and N2

Separate projection fields:

- `cumulative_acquired_bytes`: complete decoded body acquisitions, including
  complete non-200 responses, counted once per attempt, never discounted by digest.
- `retained_body_bytes`: current observed bytes in all object states. It is
  `None` if any object's size is unknown, not a misleading zero.
- `retained_observed_minimum_bytes`: lower bound preserving earlier observations
  when a later size becomes unknown. This is not a certified actual occupancy.
- `reserved_remaining_bytes`: capacity not yet transferred to retained bytes.
- `unresolved_acquisition_charge`: maximum acquisition allowance for attempts
  whose completion is unresolved; not reported as actually acquired bytes.
- `unstable`, `unknown_size_objects`, and immutable object states.

For reservation amount M (always the per-object maximum):

```text
retained + reserved_remaining + M <= retained_body_budget
reserved_remaining + M <= reserved_capacity_budget
cumulative_acquired + unresolved_acquisition_charge + M <= cumulative_budget
```

Before writing, M is reserved. After writing 20 out of 100 bytes, retained is 20
and remaining reservation is 80, not 100. After confirmed close and recount/rehash,
unused reservation can be released; retained bytes remain. This is not a refund
for quarantine or discarded content.

An unknown outcome retains its maximum acquisition charge after writer closure,
stabilization and quarantine. C3 deliberately has no evidence-free API to mark
unknown as never acquired or refund that charge. A future explicit contract would
be needed for a proven non-acquisition release. This conservative cost is distinct
from releasing unused retained-capacity reservation.

Over-budget discovered bytes are recorded, not silently clipped or discarded.
Reconciliation reports over-budget state and blocks new work. Unstable writers
block reservations even if a numeric lower bound appears within budget. All
integer inputs reject bool, negatives and values above int64; summed projections
and reservation additions also check overflow.

For example, two independently acquired 60-byte objects with identical digest
contribute 120 cumulative and 120 retained bytes. Quarantining either does not
change those totals. A subsequent 20-byte partial object with a 100-byte reservation
adds 20 retained and 80 reservation, and its unresolved acquisition charge remains
100 until a complete acquisition is actually evidenced.

Physical directory scanning is not implemented. I3 must detect unregistered or
unattributed files, account for their bytes and fail closed; it may not claim this
journal is a complete physical inventory merely because replay is consistent.

`operational_disk_limit` is an explicit, positive policy value, but C3 does not
measure/enforce actual disk occupancy. DB, metadata, receipt/journal growth,
filesystem block allocation and temporary physical copies remain I1/I3 concerns.
Do not equate the retained-body projection with actual disk usage.

## Quarantine and N4

Quarantine evidence binds operation ID, full object identity/version, contract,
original and target derived paths, physical root identity, observed size/digest,
provisional/final classification, prior state, reason/evidence, writer and UTC.
Only partial/orphan objects can enter quarantine. Unknown size stays unknown.

Identical canonical evidence under the same operation ID returns the original
journal and receipt, even after further events; no event or capacity is added.
Changing time, generation, reason, digest, size or another bound field under that
operation ID is a conflict. A new observation after quarantine is a new event,
not a rewrite of the original receipt.

Quarantine is not immutability. With an open/unknown writer it remains unstable;
observations of further growth stay chargeable. Only explicit writer closure plus
subsequent measurement can stabilize it. No bytes are removed, no object is
promoted to success, and no automatic retry or send occurs.

## Replay, reconciliation and causal evidence

The append-only C3 event chain is the source of object and budget projections.
Record binding includes sequence, previous hash, contract/root identity and the
canonical event. Duplicate physical records are rejected; an API retry with an
identical operation ID/evidence returns the already recorded result.

Reservation, complete-acquisition and commit events reference immutable C2 account
and plan heads. Reconciliation obtains the matching historical prefixes from the
supplied full journals and re-runs C2 reconciliation and attempt checks. Later C2
generations do not invalidate correctly recorded historical commits. A **new**
commit must still match the current generation; a stale writer cannot finalize.

All C2 plans referenced by the account are required, along with their C3 journals
(empty is explicit before first use). Omitting another plan's body journal is
pending, not proof of a clean account. An unstable related plan blocks new work.
This coverage contract is not physical discovery or an atomic shared Store view.

`reconcile_live_bodies` reports `consistent`, `pending` or `inconsistent`:

- Missing C2 evidence, unstable writer or unknown size: pending.
- Conflicting/missing required plan sets, incorrect historical evidence, root
  changes, conflicting receipt or projections, budget violations: fail closed.
- Claimed retained/reserved/cumulative projections must equal replay.
- Two authoritative receipts for one page are rejected even if digest matches.

Audit/quarantine/cleanup evidence may be recorded after plan expiry. New body
reservation is forbidden outside the plan window. Finalizing a previously sent,
known complete response is evidence handling, not permission for another send.
Auditing/recording problems is possible without falsely repairing C2 evidence.

## Serialization and schemas

Main versioned schemas:

- `historical-feasibility-live-body-storage-contract-v1`
- `historical-feasibility-live-body-object-v1`
- `historical-feasibility-live-body-receipt-v1`
- `historical-feasibility-live-storage-budget-event-v1`
- `historical-feasibility-live-physical-root-identity-v1`

Nested snapshot/policy/page/writer/stability/quarantine/head/projection values and
the journal container have distinct live-v1 schemas. No artificial inventory
schema is extended. No artificial v2 DB is silently migrated.

Canonical UTF-8 JSON uses sorted keys and no insignificant whitespace. Unknown
schemas/fields, duplicate JSON keys, nonfinite/noninteger JSON numbers, invalid
Unicode/control/bidi references, noncanonical UTC and int64 overflow are rejected.
Record size is bounded at 32 KiB **before parsing**, including journal header and
each event. A missing final newline or broken hash chain fails closed.

Small pure codec helpers are local to C3; C2 private helpers were not refactored.
Self-consistent hash chains and caller-provided evidence do not authenticate an
owner, account, filesystem or external receipt. Independent anchoring is still
required in later work.

## Tests and later physical verification

Dedicated tests cover the requested 32 cases and additional boundaries: duplicate
digest/page/attempt semantics, all retained states, reservation/cumulative limits,
unknown size versus zero, idempotency, ordered writer evidence, stale generation,
root replacement models, complete non-200 bodies, related-journal coverage,
serialization corruption, overflow, and closed permission flags. Successful pure
tests do not establish that OS writers were stopped or bytes were immutable.

Local verification commands (no live opt-in):

```text
python -m pytest -q tests/test_feasibility_live_body_contract.py
python -m pytest -q tests/test_feasibility_*.py
python -m pytest -q
ruff check .
ruff format --check .
git diff --check
```

I3 must separately test symlink/hard-link aliases, device/inode/mount lookup,
directory replacement races, rename with open FD, old-writer post-quarantine
writes, process crashes, fsync/durability, publish-versus-DB crash windows,
physical quarantine movement, actual recount/rehash, disk occupancy and
two-process contention. C3 does none of those operations.

## Remaining work and permission boundary

M4/M5/N2/N3 and the idempotency part of N4 are specified as pure contracts, not
declared physically resolved. I1-I3 must implement authoritative Store/Clock,
atomic persistent updates, complete inventory, filesystem fencing and HTTP
streaming integration. C2 Low findings L-C2-2/3/4/6/7 remain; stricter new C3
inputs do not retroactively fix C2. Other legacy/external-account/authenticity
findings remain outside this change.

All BodyAssessment permission flags are `init=False`, permanently false:
live send, live acquisition, verified identity and implemented Store. There is
no Live Gate integration. Consistent evidence, stable bytes, available capacity
or a valid receipt **never grants a send permission**. Actual J-Quants acquisition,
HTTPS credentials and Formal Real OOS registration/execution remain prohibited.
