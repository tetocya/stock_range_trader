# I1a-1: Central live Store schema and registries

Fixed base: `ff6a12464bcb445ddcbaec0abbb1dc18aa5f0c9d`.
Branch: `real-oos/i1a1-live-store-schema-registry`.

This stage defines the persistent format and read-only validation for a future
central live Store. It supplies a private initializer for artificial SQLite
connections and tests. It does not provide a physical Store open/bootstrap,
activation, current mutation, reservation, recovery, clock or sender API.
All tests use memory or independently created temporary artificial databases.

## Schema identity and authority

The Store identity is `historical-feasibility-live-store-v1`. Canonical internal
metadata records that identity and its deployment. Validation checks the exact
table/trigger definitions, metadata, foreign keys and contents. `user_version`
alone is not authority. Missing metadata, unknown schemas, altered definitions
and legacy artificial databases are rejected; there is no migration/fallback.

A singleton deployment binds an exact ASCII store/deployment/account identity,
owner-registry document, preflight document, policy digest and canonical UTC
creation field. Its lifecycle is `prepared`. These are persisted claims, not
proof of physical custody, remote identity or owner authenticity. Authority IDs
follow the existing lowercase ASCII label alphabet; case folding and Unicode
normalization are not applied. Display/source references are separate metadata
and are never used as primary or unique authority keys.

`PRAGMA foreign_keys=ON` and `PRAGMA recursive_triggers=ON` are prerequisites for
initialization and validation. The initializer rejects an existing database or
transaction and rolls back DDL and metadata together on failure. It neither
chooses a filesystem path nor returns an operational readiness/permission flag.
The operational durability wrapper remains a later task: retain the owner
decision **DELETE + EXTRA**, with the actual runtime validation in I1a-2/3.

## Policy and historical capacity

`historical-feasibility-live-store-policy-v1` has four required positive integer
fields, canonical bytes and a calculated SHA-256:

- `max_enrolled_plans_per_account`
- `max_record_bytes`, no greater than 32 KiB
- `max_retained_body_bytes`
- `max_cumulative_acquired_bytes`

There are no defaults. Formal N is not decided here. Tests explicitly choose
N=2; the observed approximately 202-plan practical limit is not adopted as policy.
The capacity fields are formats, not physical storage measurement/enforcement.

Deployment and account bind the fixed policy digest. Each plan also stores its
historical policy digest. Policy rows cannot be replaced, changed or deleted.
An additional policy row does not reinterpret existing records. Enrollment
capacity includes every plan with an enrollment revision, including stopped
plans; prepared candidates do not consume slots. The candidate helper checks
the complete historical set plus actual canonical UTF-8 record length, rejecting
oversize without truncation. Store validation obtains the set from its entire
catalog and verifies revision continuity and correspondence to v2 account
enrollment history. Later enrollment mutation must use both N and the actual
32 KiB bound inside its authoritative transaction.

## Relational layout

| Table | Identity and responsibility |
| --- | --- |
| `store_metadata` | Singleton schema/deployment binding |
| `policies` | Immutable digest, canonical policy and explicit capacities |
| `deployments` | Singleton store/deployment/account, source documents and policy |
| `accounts` | Deployment/account key, schema, policy, journal, enrollment revision and request generation |
| `plans` | Deployment/plan digest, account, typed plan/preflight/approval documents, policy, journal and historical enrollment revision |
| `documents` | Deployment/digest key, kind/schema, exact canonical bytes, source identity/reference and registered UTC field |
| `journals` | Deployment/journal key, account/plan relationship, kind/schema/status |
| `events` | Journal/sequence key and journal/event-hash unique key, exact bytes, previous hash, recorded UTC, transition and optional session/fence fields |
| `journal_heads` | Separate authoritative current head/count, referencing the last event |
| `operations` | Deployment-global operation ID, kind, semantic intent, separate execution preconditions, status and receipt link |
| `event_parts` | One operation to many journal events; role/ordinal uniqueness, exact event FK and result head |
| `operation_head_references` | Source/result historical event-head references, separate from semantic intent |
| `receipts` | One immutable canonical receipt per operation, schema/digest/commit UTC and result parts |
| `control_state` | Reserved relational stop-state/head reference; no new physical event interpretation |

Foreign keys reject orphan plans, documents, event parts, heads and receipts.
Composite document references bind the declared kind and schema as well as the
digest. Deployment-wide operation IDs cannot be reused for another plan or kind.
Event transition IDs are deliberately **not** globally unique: C2 account
enrollment and plan opening share a logical transition ID in separate journals.
One logical operation can therefore reference both events.

Statuses describe persisted facts. Deployment is prepared; account/journal rows
can describe prepared/stopped records; plans distinguish prepared, enrolled and
stopped historical facts; operations distinguish pending/committed. No status
transition or activation API is exposed. Control/body schema extension requires
explicit later versioned validation; unknown physical events are not a generic
escape hatch. `L-C3-2` remains open.

## Original documents and journals

Document kinds include plan, preflight, approval and owner registry, with an
explicit supported schema per kind. `CanonicalDocument` requires actual canonical
UTF-8 JSON bytes, recalculates SHA-256 and checks the declared schema. SHA-only,
malformed/noncanonical JSON, duplicate keys, nonfinite values, unsupported schema
and mismatched digest are rejected. Same source reference with distinct canonical
documents produces distinct identities. Existing bytes are stored/read unchanged;
no parse/re-dump is used to substitute for the original.

This validates content encoding, digest and schema identity, not the entire
typed document semantics or external authenticity. **I1b** must reread these
fixed originals, rebuild the existing typed plan/preflight/approval contracts and
recheck all source/approval/account bindings during activation. `L-C2-4` remains
open until that work is verified.

Event rows retain original canonical envelopes and replay metadata. Read-only
validation checks contiguous sequence, previous/event hash, canonical bytes,
timestamp/transition columns, nonregressing event UTC and the exact current-head
pointer. C2 account/plan records also replay through their existing reducers;
catalog generation/enrollment metadata must agree. C3 body envelopes retain their
contract/root identity evidence unchanged; this stage checks envelope integrity,
not C3 physical writer state or full physical reconciliation. Current validation
and cross-journal operational reconciliation belong to later atomic entry points.

Historical event heads remain independently referenceable. A historical prefix
cannot replace the current pointer while later events exist. Session/fence fields
are references only; runtime fencing and Store-clock acquisition are not present.
UTC fields use exact `YYYY-MM-DDTHH:MM:SS.ffffff+00:00` encoding.

## Operations, parts and receipts

`OperationIntent` serializes an explicit kind/account/optional-plan/payload using
`historical-feasibility-live-store-operation-intent-v1`. Execution-time expected
heads, timestamps and session/fence references are separate fields; they are not
automatically added to semantic intent. The payload is the caller's explicit
semantic contract, not an inferred new strategy or operational specification.

The deployment/operation ID primary key spans all plans and operation kinds.
Immutable intent columns reject updates/replacements. Parts have unique roles,
ordinals and journal event identities, share the logical operation's transition
ID, and maintain account/plan scope. Required roles are fixed alongside the intent.

Committed operations require a receipt/digest/timestamp via checks and a deferred
composite receipt FK. Read-only validation additionally checks all required parts,
ordinal completeness, receipt digest, exact receipt contents and result-head
references. SQLite cannot prove these canonical-byte constraints itself, so the
validator is a required contract for future transaction entry points. Pending
records can represent incomplete work; they cannot be treated as successful.

Receipts, documents, original events, event parts, head references and policies
have update/delete/replacement guards. `INSERT OR REPLACE` cannot silently bypass
them. A committed operation is immutable. These are application-level SQLite
integrity constraints, not protection from an actor who can rewrite the database
or remove triggers. Altered definitions/content fail validation; no external
signature/custody anchor is claimed.

`assess_operation_replay` is a read-only schema demonstration:

- same ID, same canonical intent, committed: return original receipt bytes;
- same ID, different intent/kind: conflict;
- same ID, pending: recovery required;
- corrupt receipt/schema/chain: reject;
- unrelated later pending operation: preserve the earlier committed receipt.

This does not implement I1a-3's transactional retrieval/retry behavior. Tests use
private SQL fixtures to construct records and test rollback; production mutation
APIs remain absent.

## Verification and handoff

Artificial tests cover initialization/round trips, version rejection, FK/unique
constraints, deployment-wide operation conflicts, multi-event parts, immutable
receipts, corruption without repair, historical/current heads, UTF-8 32 KiB
boundaries, historical plan count, policy binding and transactional rollback.
Existing C1/C2/enrollment/C3 bytes, schemas, golden values and L-CMP-1 semantics
are unchanged. Live/browser skip cases remain unverified.

Finding status is deliberately partial:

| Finding | I1a-1 contribution | Required later evidence |
| --- | --- | --- |
| L-EN-2 | Explicit versioned N and record-size contract; full catalog/history | I1a-3/I1b actual enrollment enforcement |
| L-EN-3 | Immutable receipt and replay retrieval formats | I1a-3 transactional receipt/idempotency behavior |
| L-C2-3 | Deployment-global operation namespace | I1a-3 atomic multi-journal operation path |
| L-C2-4 | Original document bytes/digest/source binding storage | I1b typed rebuild and activation checks |

These findings remain open. L-EN-1, L-C2-2/6/7 and L-C3-2 are outside this stage.
I1a-2 must add physical canonical Store identity, runtime/version/durability
validation, owner clock, locking/fence/session responsibility and startup policy
according to its separately approved scope. No Store activation/send/acquisition
permission is granted here. HTTPS/J-Quants and Formal Real OOS remain unavailable.

Local verification of this implementation (Python 3.13.3, SQLite 3.49.1,
pytest 9.1.1, Ruff 0.16.8):

| Check | Result |
| --- | --- |
| I1a-1 dedicated suite, also with `-W error` | 97 passed |
| Feasibility including existing C1/C2/enrollment/C3 | 832 passed |
| Full pytest | 2315 passed / 13 skipped / 0 failed |
| Ruff lint / format | pass; 283 files formatted |

The full run has the existing 28 urllib3 warnings from `jquants_v2.py:411`.
Live 7 and browser 6 skips are unverified. Live/UI opt-ins were disabled for
these runs. GitHub CI and independent review are not performed by this local
implementation task. The results do not grant activation, send or acquisition
permission.
