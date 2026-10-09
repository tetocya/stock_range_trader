# I1b: local account opening and plan enrollment

Fixed base: `e2cb1ec7465c1ae0b50fd07e9ed54cd8e5b2f94a`, tree
`5dcfd08fd03f55d8a3bec69906ba86c1901d5bb3`.

I1b connects the I1a-3 private transactional kernel to typed C1/C2/C3 source
contracts. The public operations are `open_enrollment_account(session,
operation_id)` and `enroll_plan(session, operation_id, request)` in
`live_store_enrollment.py`. They return the existing `OperationAssessment` with
the saved original receipt bytes after commit. Neither function accepts a
caller head, count, catalog, clock, SQL, callback, completed event or receipt.

## Typed sources and authority

`PlanEnrollmentRequest` is a versioned immutable value containing two
`EnrollmentSource` originals (plan and approval), an explicit
`LiveBodyStorageContract`, explicit `StoragePolicy`, and explicit
`PhysicalRootIdentity`. Source content is canonical bytes, with declared
source_identity and source_reference. Registration time is supplied by the
Store's start observation. No reference is dereferenced or fetched.

All account identity/preflight/registry/StorePolicy originals are re-read from
the Store. C1 registry/preflight decoders validate exact fields, nested registry
identity, canonical UTC, schema and digest. Plan and approval decoders in
`live_enrollment_sources.py` reproduce the lexical/content invariants of their
legacy constructors, reconstruct fixed queries and compare canonical content.
They reject extra fields, duplicate JSON keys, noncanonical bytes, wrong schema,
digest, account, scope, policy and approval binding. These pure decoders do not
invoke the legacy constructors' filesystem symlink checks. This is an explicit
boundary: declared output paths are validated lexically; physical root custody
is not proved by source decoding.

The C3 contract, policy and root claim are strictly re-decoded. Contract plan
bytes must match the plan original; preflight, root binding, path and owner
Store UUID must agree. A future root observation is rejected. Retained and
cumulative per-plan storage claims cannot exceed the StorePolicy ceilings.
No device/inode/epoch/policy value is synthesized. A claim is not a physical
measurement or external authenticity proof. Physical root verification and
writer authority remain I3 work. No body directory, file or object is created.

## Operations and transaction ordering

`i1b-account-open-v1` creates an account-v2 `ledger_opened` event, a prepared
account catalog entry, its journal/head, one account-opening part and receipt.
It enrolls no plan and creates no C3 journal. A second operation ID cannot open
the same account again. A later enrollment failure preserves this committed
account opening.

`i1b-plan-enroll-v1` creates three fixed parts, in order:

1. `account-enrollment`: C2 account `plan_enrolled`.
2. `plan-opening`: C2 plan `journal_opened`.
3. `body-initialization`: C3 initial `audit`.

C2 uses transition_id; C3 uses operation_id. Both are validated against the
same Store operation ID without modifying either event schema. The body journal
has exactly one initial audit, object_id=None, c2_heads=None and
evidence_ref=initialization manifest SHA. Its projection has no object, writer
or reservation.

The existing kernel owns BEGIN IMMEDIATE, session/fence/physical/PRAGMA guards,
operation lookup, the start/end observations, business savepoint, immutable
receipt insertion and final commit. For a new I1b operation it registers and
re-reads documents, revalidates typed sources, obtains current heads and the
complete prior-plan set from DB, checks capacity, constructs the C2 pair and
manifest/C3 audit, and atomically stores catalog, journals, parts and heads.
The end observation rechecks the approval window before receipt creation.

The approval rule is the existing C2 rule:
`plan.start <= approval.start <= observation < approval.end <= plan.end`.
Both start and end observations must satisfy it. Expiry at the end rolls back
the entire business operation and its normal clock observations, preserving a
usable session if revalidation succeeds. Clock anomalies retain the existing
sticky-stop/savepoint behavior instead. There is no automatic retry.

I1b has a fixed internal rejection taxonomy for invalid source, validity-window
failure, duplicate account/plan, account not open, record oversize, source
conflict and capacity exceeded. Store corruption, session/fence/physical/clock
failures and uncertain commit invalidate the session. No caller callback can
expand the recoverable set. I1a-3's existing-kind behavior is preserved.

Historically enrolled plans are counted from the entire DB catalog; stopped
plans count and prepared-only plans do not. Same-plan/same-operation retries
return the original receipt. A new operation ID cannot re-enroll that plan.
Different intent under the same deployment-global ID returns conflict. Store
N and byte limits are applied in the same transaction as evidence persistence.
Formal production N remains undecided; artificial N=2 is not adopted.

## Manifest, original bytes and historical validation

New operations use `store-transaction-preconditions-v2`, containing the canonical
`historical-feasibility-enrollment-initialization-v1` manifest. It binds:

- deployment/Store/account, operation ID and semantic intent SHA;
- C1 preflight/registry, StorePolicy and owner pin SHA;
- source document SHA/kind/schema and source_identity/source_reference/registered_at;
- the complete source heads, source account head and sorted prior-plan heads;
- result account and initial plan heads;
- original C3 contract/root claim contents and digests, and body journal identity;
- runtime session/fence/start observation and runtime head.

The manifest does not contain its own audit or receipt hash. The audit binds
the manifest SHA, avoiding circular references. Opening also records a manifest,
with no plan/body and no source account head.

The existing six-field receipt is unchanged. A new-kind historical validator
reconstructs the C2 proposal at the recorded start time from full original
documents and historical prefixes, reconstructs the exact initial C3 audit,
checks all parts and references, checks manifest metadata against the document
registry, then checks the recorded end approval condition. Current time and
later plan enrollments do not redefine an older execution. Metadata-only
changes invalidate the affected receipt even if the content digest is unchanged;
this does not authenticate the external provenance or issuer.

C2 and C3 prefix readers are separate. Current validation requires exactly one
C3 journal for every enrolled plan, including catalog completeness (an extra
empty journal cannot be hidden). It revalidates initialization and reconciles
all C2 plans and C3 journals. Historical receipt validation uses its historical
dependencies, permitting A receipt retrieval after B is enrolled or after
unrelated later changes. Core/schema/FK/runtime corruption still rejects.

`read_committed_receipt` retains its exclusive evidence-only dirty/stopped path.
It does not start a session, increment the fence, clear a stop, regenerate a
receipt or finish a pending operation. Committed retries after approval expiry
use the original execution times. A crash before commit yields no successful
receipt; a commit-before-return crash can retrieve the exact original three-part
receipt and manifest while normal writer open rejects the dirty restart.

## Clean close and compatibility

The new nonempty profile is `local-c2-idle-c3-initial-only-v1`, represented in
`historical-feasibility-runtime-clean-validation-v3`. It accepts valid opening/
enrollment states with complete committed receipts, reconciled current heads,
exact initial audits and no attempts, holds, body objects, reservations, writers,
control recovery or runtime uncertainty. The existing empty v1 and no-body v2
clean profiles remain unchanged.

Additional audits, active or released holds, reserved/sent/terminal attempts,
unknown recovery, body/writer state or unsupported events cannot receive a clean
marker. L-I1A3-1 remains open: a later reviewed profile is required before I2
can close Stores containing completed acquisition history.

Store v1/v2 DDL, runtime/receipt/C2/C3 persisted schemas, statuses, legacy
execution preconditions v1 and historical kind rules are unchanged. No migration
or reinterpretation is performed. `journals` supplies the C3 catalog binding;
the manifest stores full original C3 contract/root claims without a new table.
The initialization record is also constrained by the Store record-byte limit.

## Verification and remaining boundaries

Artificial tests exercise the complete bootstrap/open/close/reopen/A/close/
reopen/B/close/reopen/original-A sequence, narrow approval windows, expiry and
clock rollback, source/manifest corruption, exact roles, missing evidence,
capacity, prepared/stopped plans, process lock contention, serialized last-slot
enforcement and spawned crashes before commit and after commit/before return.
They assert original bytes, no partial persistence, and no reader mutation.
Physical tests use private artificial Store directories and explicit synthetic
body-root claims. Existing Store and all feasibility regressions remain required.
The macOS targeted job adds I1b with `-W error`; Linux's full matrix is retained.
Live 7 and browser 6 remain unverified when skipped.

Local verification on macOS 15.7.7 arm64, Python 3.13.3 and SQLite 3.49.1:

- I1b dedicated, with warnings treated as errors: 73 passed.
- Existing Store I1a-1/2A/2B/3 regression, with warnings treated as errors:
  419 passed.
- All feasibility tests, including C1/C2/enrollment/C3: 1227 passed.
- Full pytest: 2710 passed, 13 skipped, 0 failed. The 28 warnings originate
  from the existing `data/providers/jquants_v2.py` urllib3 configuration;
  they do not represent live network requests.
- Ruff lint, format check (296 Python files), and `git diff --check`: pass.

The skipped Live 7 and browser 6 tests are not verified. GitHub CI and Linux
matrix execution are not part of this local-only increment. These results do
not replace the pending independent review.

Resolution candidates pending independent review: L-EN-2 (handler capacity
connection, with formal N still open), L-EN-3 (end-to-end receipt), L-C2-3
(operation/three-part atomicity), L-C2-4 (typed original revalidation), and
L-I1A3-2 for newly created I1b evidence. Older metadata is not retroactively
bound. M-CMP-1/L-CMP-1 remain resolved.

Still open: L-I1A3-1/3, L-I1A2A-1/2, L-I1A2B-1/2/3, L-EN-1,
L-C2-2/6/7 and L-C3-2. Validation still performs repeated global/prefix replay;
small artificial timings are not a production SLA. Future handlers must retain
all historical kind/version rules. Same-process arbitrary private access is not
a security boundary. Actual external provenance/account/owner authenticity,
physical body custody, I2/I3, manual recovery, repin and stop-clear are outside
this increment. Live Gate remains closed. No credentials or market data are
read; actual J-Quants acquisition and Formal Real OOS remain prohibited.
