# I1a-3: local transaction authority

Fixed base: `05103c5461f2623d6d693a747f0576a823e210a2`.
This increment adds a local kernel within an already validated operational
StoreSession. It does not install an activation, enrollment, HTTP or recovery
business entry point. The only mutation adapter is in the artificial test module.
No live acquisition or Formal Real OOS permission is granted.

## Public boundary

`live_store_transactions.py` exposes four evidence reads:

| API | Validation and result |
| --- | --- |
| `read_catalog(session)` | Global validation; immutable canonical catalog snapshot |
| `read_document(session, document_sha)` | Global validation; immutable original document |
| `assess_operation(session, operation_id, intent)` | Validated session and global state, then target receipt evidence |
| `read_committed_receipt(authority, operation_id, intent)` | Exclusive custody, core/runtime and target evidence only; no new session |

None returns a SQLite connection, cursor, mutable catalog or mutation capability.
The separate receipt reader accepts dirty/stopped runtime state, not broken
runtime evidence. It does not append `session_started`, increment epochs, clear
stops, mark clean, complete pending operations, execute absent operations or
regenerate receipts. SQLite may perform hot-journal rollback when acquiring the
database; this is not application recovery or business completion.

Assessment classifications are `committed_receipt`, `absent`,
`pending_recovery_required`, `conflict` and `inconsistent`. Core/schema/FK/physical
errors raise rather than returning a successful assessment. No classification
is an invitation to send HTTP or automatically retry a pending operation.

## Internal kernel and authority

`_execute` is private. There is no exported handler registration or generic SQL
callback. `_prepare` and `_rules_for` deliberately reject every production
business command until reviewed typed handlers are supplied in a later stage.
Tests install a bounded, test-only adapter with fixed command kinds and roles;
it takes no SQL, arbitrary events, connection, caller head or receipt builder.

The private persistence primitive accepts fully replayable typed C2 journal
proposals from that internal dispatch, preserves every existing prefix, derives
catalog changes from their projections, and reconciles all account/plan evidence.
It is not an I1b enrollment or activation public API. There is no document-only
operation, empty-parts receipt, or dummy event used to register documents.

New operations use this order:

1. Verify PID/thread/session, physical custody, OS lock and SQLite PRAGMAs.
2. Require no outstanding SQLite transaction; start `BEGIN IMMEDIATE`.
3. Globally validate Store/runtime/catalog/current heads and session/fence.
4. Assess the deployment-global operation ID. A committed exact intent returns
   the original bytes without a clock event; a conflict or pending returns its
   distinct assessment. Before an absent operation can mutate, all existing
   operations must be committed and locally complete, and C2 reconciliation
   must be consistent.
5. Capture and persist the Store-owned start clock inside the outer transaction.
6. Open a business savepoint. Resolve/register immutable documents, derive
   capacity from the complete DB catalog, insert pending operation, append all
   events/parts, CAS current heads, and update catalog projections.
7. Validate the pending business evidence. Capture the end clock.
8. Create the original six-field receipt with the end observation UTC, insert it
   and mark the operation committed. Validate all evidence and target receipt.
9. Recheck physical/PRAGMA/session/fence/runtime state immediately before commit.
10. Commit, then return. An uncertain commit/return failure invalidates the handle;
    no successful receipt is inferred. A later evidence-only read decides.

Start clock anomalies prevent business work and persist a sticky stop if possible.
End anomalies roll back the business savepoint before recording the stop in the
outer transaction. Stop persistence failure rolls back and invalidates the
session. Deterministic capacity/record-size/source-conflict rejection rolls back
normal observations too; a revalidated session may continue. No nested `BEGIN`
is introduced. Timestamps are Store observations, not exact physical disk-commit
times. Existing clock/physical TOCTOU limitations are unchanged.

## Operation and document originals

The namespace is `(deployment_id, operation_id)`, not account/plan/kind-local.
Canonical intent bytes and SHA are both compared. Intent contains semantic
kind, target and payload/document identity, not current heads, revision, session,
fence or current time. Execution preconditions separately bind the DB-derived
source heads, all historical dependency heads, catalog digest, session/fence,
runtime start head, Store start UTC and document dependencies. Receipt validation
reconstructs the historical account/plan set at those heads and reruns C2
reconciliation, including plans that the target operation did not itself modify.

Required role order and journal kinds are internal command rules. Account and
plan parts may share a transition ID. Multiple events in one journal have one
pre-operation source and one final result reference. Head CAS uses the old
DB-read hash/count and requires one affected row. Normal local transactions do
not leave a persistent pending operation or one-sided event/head/receipt changes.

Private document registration resolves the DB original. Exact kind/schema/content/
source metadata is idempotent and preserves original registration time. Same
digest with different source metadata rejects; same source reference with a
different content digest remains a separate immutable document. Registration
time is the Store start observation; callers cannot specify it.

Capacity uses `enrolled_revision IS NOT NULL` across the full account catalog.
Stopped enrollments count; prepared-only plans do not. An already enrolled plan
is not counted twice. StorePolicy supplies the limit and UTF-8 byte limit; no
production numeric N is chosen here. Test N=2 is explicitly artificial. Capacity,
catalog and all operation evidence are in one transaction. I1b must still connect
its real typed handler to this kernel and receive its own review.

## Receipt evidence scope

The receipt schema remains exactly:
`schema`, `deployment_id`, `operation_id`, `intent_sha`, `committed_at`, `parts`.
Its original bytes are returned, never a reconstruction from current state.
It is not standalone proof of kind/account/plan/Store/head context.

The target validator checks operation original/intent, fixed kind roles, complete
ordered parts, source/result references, each required historical journal prefix,
referenced immutable documents, session/fence, and consecutive start/end runtime
observations. Referenced events must match both canonical envelopes and DB
columns. Original receipt bytes/digest and operation columns must agree.

It does not require an old result head to equal today's current head, today's
catalog to equal the old catalog, or unrelated later operations to be absent.
An old receipt survives a later valid operation, unrelated pending operation,
unrelated later journal logical corruption, or a current head moved to a past
valid prefix. New mutation/global open still rejects invalid current state.
Structural, FK, schema, owner/pin, core and runtime corruption remain global
rejection conditions. Target-dependent corruption returns no receipt.

The existing global validators retain their prior accepted/rejected semantics.
Their only changes are private extraction of core/runtime checks; the public
global path still executes the complete relational and runtime validation.

## Clean close

The empty-business `historical-feasibility-runtime-clean-validation-v1` profile
is unchanged. Nonempty state uses
`historical-feasibility-runtime-clean-validation-v2` and the conservative
`local-c2-no-attempts-no-holds-no-body-v1` quiescence profile.

It requires globally valid current state, account journal evidence, all operations
committed with complete target receipts, C2 reconciliation consistent, no attempt
or hold state, no body journal and no control recovery state. Merely having a
catalog is not a rejection reason. An account without its journal evidence,
pending/inconsistent operation, any attempt/hold, or body state is
`quiescence_unproven` (or a stricter integrity error), never a clean marker.
Even an empty body journal is conservatively unsupported. More permissive
physical quiescence requires a later reviewed profile, not an envelope-only claim.

The canonical snapshot binds Store/deployment/account, session/fence, runtime
head, pin, physical identity, sorted catalog, sorted current heads and a digest
of the operation/receipt set. Only its digest goes into existing `validation_sha`.
Store v2 DDL, indexes, statuses, runtime schemas and receipt schemas are unchanged.

## Tests and measurement limits

Artificial tests cover original receipt replay across later operations and clean
sessions, committed-before-return and precommit subprocess exits, dirty/stopped
receipt-only reads, immutability, missing/corrupt target evidence, unrelated later
corruption, pending rejection, clock stop/savepoint rollback, stop-write failure,
session guards, head/catalog authority, byte boundaries, stopped/prepared capacity,
serialized processes, unsupported body state and deterministic close snapshots.
Existing I1a-1/2A/2B and feasibility regression suites remain required.

The macOS targeted job includes I1a-3 with warnings treated as errors. Linux's
full Python matrix is unchanged. No GitHub job is claimed executed by this local
implementation. Live seven and browser six skipped tests remain unverified.

Timing observations are not pass/fail thresholds. Dedicated fixtures independently
vary: runtime events (9/29); catalog plans (1/17, with 16 additional prepared
plans); operations (3/4 at 7 business events and 11 runtime events, by batching
versus splitting the same artificial reserve/send evidence); and business events
(5/7 at 3 operations and 9 runtime events). Each reports new mutation, receipt,
capacity and close timings. For in-flight artificial evidence, close timing is
the expected `quiescence_unproven` rejection, not successful quiescence. No network
request occurs. These are small local measurements, not production throughput
or a formal N recommendation. Large-scale performance and runtime replay
optimization remain outside this increment.

On local macOS/arm64, illustrative dedicated-run observations were approximately
30–73 ms per new artificial operation, 6–10 ms per original receipt assessment,
3–4 ms for capacity, and 38–54 ms for successful nonempty close. These values are
environment-dependent, include repeated validation, and are not timing gates.

## Findings and next boundary

Resolution candidates, not yet independently accepted: L-I1A1-1 (scoped receipt),
L-I1A1-2 (DB capacity authority), L-EN-3 (original replay), and the Store side of
L-C2-3 (global operation IDs/atomic parts). L-EN-2 is partial: formal N and I1b
integration are undecided/unimplemented. Independent review must determine their
final classification; this document does not declare them resolved.

Remain open: L-EN-1, L-C2-2/4/6/7, L-C3-2, L-I1A2A-1/2,
L-I1A2B-1/2/3. M-CMP-1 and L-CMP-1 remain resolved without contract changes.
The receipt-only API depends on internal versioned kind rules; future handlers
must retain historical rules for original receipts, not silently reinterpret them.
The boundary is the public API. Arbitrary Python code in the same process can
force access to private internals; this is not claimed as a security sandbox.

No actual account identity verification, HTTP callback/transport, credential
handling, physical body writer/recovery, repin or stop-clear is added. Live Gate
remains closed. Real J-Quants acquisition and Formal Real OOS remain prohibited.
