# Delayed replay Stage 2: local audit persistence and verified recovery

Status: DRAFT / NOT REGISTERED. This implements storage for synthetic transitions
and draft audit purposes, not an OOS runner or a trading system.

## Compatibility and scope

Stage 1's schema, unresolved `None` policy values, `validate_for_execution()`
meaning, clocks, price snapshots, and previous-session-only history are unchanged.
Phase 1–3 interfaces, defaults, candidate selection and results are unchanged.
The new modules do not implement shared-account accounting, cash reservations,
fills, monthly selection, checkpoint classification, registration, or network I/O.
No proposed allocation or other unresolved parameter is approved by creating a
synthetic stream. A complete config is not execution permission.

State snapshots (`audit_models.StateSnapshot`) and Stage 1 price snapshots
(`snapshot.PriceSnapshot`) are different types and schemas. A state snapshot
does not authorize execution or prove a provider's historical price basis.

## Interfaces and records

- `serialization.JsonObject`: immutable canonical JSON text, detached exports.
- `StreamIdentity.create(...)`: explicitly supplied config/protocol hashes,
  source/reducer identities, initial-state hash, schema versions and purpose.
  Supported purposes are `synthetic_test` and `draft_audit`, not formal OOS.
- `EventCommand`: caller's stable event ID, event type, payload, input snapshot
  hashes, identity references, market/replay clocks and optional correction ID.
- `AuditEvent`: command plus sequence, previous event hash, before/after state
  hashes and recomputed event hash.
- `Head`: sequence and event hash; sequence zero points to the identity's genesis.
- `CommitReceipt`: the immutable original event and its resulting head.
- `StateSnapshot`: complete stream identity, sequence/event hash, state/schema,
  state hash and recomputed snapshot hash.
- `EventStore.create(path, identity, initial_state, ...)`: explicitly create a
  stream, optionally with a genesis snapshot. A duplicate stream is rejected.
- `EventStore.resume(path, identity, reducer, expected_head=...)`: open an existing
  stream and fully verify/replay it before returning a usable store.
- `commit_event(command, expected_head, reducer, save_snapshot=False)`:
  atomic append. Snapshot choice is a storage option, not part of command meaning.
- `read()`, `lookup(event_id)`: consistent, structurally validated immutable
  records. `recover(reducer, expected_head=...)` additionally replays state.

Use create/resume and close the store, preferably with a context manager.
Resume uses SQLite `mode=rw`; it never implicitly creates a missing database or
stream. Existing unknown schemas are refused; no migration/repair is attempted.

## Serialization and hash vocabulary

All persistent objects use sorted string keys, comma/colon separators without
whitespace, UTF-8 with `ensure_ascii=False`, and explicit JSON null. Array order
is meaningful; key order is not. Unicode text is retained without normalization.
Accepted payload/state values are dict, list, string, null, bool and signed
64-bit integers. Top-level payload/state must be an object. Float, NaN, Infinity,
tuple, arbitrary objects, pickle and non-string keys are rejected recursively.
Duplicate JSON keys, noncanonical encoding, missing/extra record fields and
unknown schema versions are rejected on read.

Booleans are distinct JSON fields, never sequences. Reducers must validate typed
payload fields: the artificial test reducer rejects bool as its integer delta.
Precision-sensitive quantities must use explicitly typed decimal strings;
`decimal_text()` normalizes finite strings without binary float or decimal-context
rounding. It does not reinterpret every arbitrary JSON string as a monetary value.
No existing Phase 3 numeric representation is changed.

Timestamps are supplied by the caller, timezone-aware and normalized to UTC
`YYYY-MM-DDTHH:MM:SS.ffffff+00:00`. No automatic current time or random event ID
is generated. Original mutable tzinfo objects are not retained. Both event clocks
are nondecreasing; equal market times are allowed. Market time cannot exceed
replay time. Replayed time is part of command meaning and must remain unchanged
on retry. There is no separately generated DB receipt timestamp.

The genesis digest covers identity metadata (including all schema versions and
initial-state hash), excluding genesis_hash itself. Event digest covers the
complete canonical command, sequence, previous hash and both state hashes,
excluding event_hash. State-snapshot digest covers identity, sequence/event hash,
schema, state payload and state hash, excluding snapshot_hash. Direct construction
and DB loading recalculate supplied digests. Input mappings/arrays are serialized
immediately; every export returns a detached tree.

## Transaction and retry contract

The single local SQLite database contains `streams` (identity, initial/current
state, current hash and head), `events` and `snapshots`. Event sequence and ID
are unique per stream in DB constraints; snapshot sequence is unique per stream.
There is no event UPDATE/DELETE API. Corrections reference an already committed
event within the same stream and are new transitions from the *current* state.
This does not implement trading correction or P&L compensation semantics.

`BEGIN IMMEDIATE` serializes writes. The default writer wait is a finite 1000 ms,
configurable within 0–60000 ms; busy/locked conditions raise `StoreBusy`.
SQLite DELETE journal mode is required and verified (the new DB default);
synchronous is explicitly FULL and foreign keys are enabled. An existing DB
in another journal mode is rejected, not converted during resume. Supported
deployment is a single logical writer on a local filesystem, not NFS or a
distributed multiwriter service.

Within the transaction:

1. Validate stored records and search the command's event ID.
2. Same ID and canonical command: return original receipt, without reducer
   execution, head advancement or a later snapshot addition. Old expected heads
   are permitted on this retry path. Different content: `IdempotencyConflict`.
3. For a new ID, require exact expected head; otherwise `HeadConflict`.
4. Check identity, clocks and correction reference. Run the pure reducer on a
   detached state copy and freeze its result.
5. INSERT event, UPDATE current state/hash/head, and optionally INSERT snapshot.
6. COMMIT, then return receipt.

The event ID must identify a business operation and remain stable on retry; it
must not be derived from time alone. Different IDs for the same business operation
are not automatically deduplicated. Changing replayed time on retry is a content
conflict. Snapshot requests on retries do not change a committed receipt/history.

Reducer code must be deterministic, validate its event types, and perform no
I/O, random reads, current-time reads or other external side effects. This is a
caller contract, not a Python sandbox. Only the synthetic counter reducer under
tests is supplied. Exceptions are mapped to stable reason codes; raw exception
messages, paths, keys, prices or environment data are not automatically persisted.
The generic payload API is not a secret scanner: callers must supply approved,
minimal payloads and input hashes, not raw market data or credentials.

## Recovery and tamper limits

Resume compares the expected complete StreamIdentity. In one consistent read
transaction it validates all record digests, row indexes, sequences, IDs, hash
links, before/after hashes, clocks, correction references, snapshots and stored
head/current state. It then replays **every event from genesis**, checks every
snapshot at its sequence, and independently replays the latest snapshot's tail.
Both paths must match current state and head before recovery is returned.
Snapshot absence is allowed; a corrupt snapshot is not ignored or regenerated.
Recovery does not alter logical records or receipt times. SQLite may perform its
normal hot-journal rollback after process failure; this is transaction recovery,
not application history repair.

An optional externally retained `expected_head` must match the recovered head
exactly. This detects a coherent older backup. Without a trusted external head,
an internally consistent old database or coherently rewritten chain may be
undetectable. External anchors and source/reducer identity references must be
protected separately. Hash chains are not signatures or protection against an
administrator replacing the whole DB and its local metadata. No automatic repair,
migration, event invention or protocol INVALID classifier is implemented.

Stable exception categories are `InvalidEvent`, `IdempotencyConflict`,
`HeadConflict`, `IntegrityError`, `IdentityMismatch`, and `StoreBusy`. A successful
`read()` verifies stored structural consistency, not the reducer's semantics;
use resume/recover for full state verification.

## Files and offline verification

Use a caller-selected path under `.delayed_replay/` for runtime DBs, journals and
backups. That directory is ignored; unrelated SQLite fixtures are not globally
ignored. Tests create real databases exclusively in pytest temporary directories.
No market/Broker API is called; no live validation is part of these tests.

Tests cover mutable input/export isolation, strict types/hashes, two independent
connections contending for one head/lock, identity and correction isolation,
no/middle/genesis/final snapshots, corrupted records including snapshot tails,
coherent backup rollback, and continuous versus restarted logical equivalence.
Actual transaction fault points are before event INSERT, after event INSERT,
after state/head UPDATE, after snapshot INSERT, and after COMMIT before receipt.
The first four leave committed state unchanged; the last commits once and permits
idempotent receipt recovery. Separate subprocesses use `os._exit` at bounded
pre/post-COMMIT hooks, then another process reopens and verifies the database.
These are process-termination tests, **not power-loss tests**. OS/filesystem/device
failure, faulty fsync, external side effects, backup durability and malicious
administrator changes are outside the tested transaction guarantee.
