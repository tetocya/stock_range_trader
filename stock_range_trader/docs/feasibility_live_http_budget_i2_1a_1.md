# I2-1A-1: pure historical HTTP budget contract

Fixed base: `c121ab8aef765ae24a3a03206732acba3f9327f5`.
Fixed tree: `5587f30cdedb1cc34bb35aa189b8b28e66d8c674`.

This module implements immutable budget claims, historical arithmetic and
verification. It does not register a Store operation or produce reservation,
dispatch, recovery, writer or Live authority. C2/C3 inputs are replayed, never
appended. Store DDL, transaction kernel, existing HTTP arithmetic, persisted
C1/C2/C3 schemas, original receipts and clean profiles are unchanged.

No formal OD-BUDGET document was found in the repository. This contract follows
the owner's I2-1A-1 implementation instruction; it does not invent an earlier
approval record or claim that artificial input is authenticated production data.

## Values and schemas

All new schemas use `historical-feasibility-http-budget-` and a `v1` suffix:

| Value | Meaning |
|---|---|
| BudgetPolicy | Legacy full-page unknown charge; no flat saved charge |
| Identity | Deployment, Store and account identity claims |
| MeasurementOriginal | Independent, bounded artificial capture original |
| BudgetEvidence | Derived classification, allowance and accounting charges |
| SourceCommitment | Source count and domain-separated ordered chain head |
| Allowances | Nonnegative transfer, decoded and saved limits |
| SavedCompatibility | Compatibility result, distinct-digest total and reasons |
| PageCount | Attempt count for a particular page request |
| BudgetResult | Historical totals, origin, allowances, sources and blockers |
| CapacityResult | Separate C3 retained/reserved/acquired/unresolved totals |
| AdmissionCore | Structured reservation-admission commitment, not permission |

The existing strict pure C3 value codec is reused. Unknown/duplicate fields,
noncanonical JSON, bool-as-int, negative integers, non-UTC timestamps and int64
overflow are rejected. No new decoder silently normalizes evidence.

`MeasurementOriginal` separates measured transfer, measured decoded bytes,
`sink_accepted_bytes`, and `persisted_size`/`persisted_digest`. Sink acceptance
alone proves neither physical stability nor a committed object. Complete
measurement needs all three measured counts. Unobserved measurement cannot
carry counts or saved claims. A producer reference such as `transport` is only
a label; `producer_authenticated` remains false.

## Historical sources and coverage

`reduce_history()` accepts an explicit fixed-plan snapshot, C2 account prefix,
all related C2 plan prefixes, all C3 body prefixes, measurement originals and an
explicit historical evaluation timestamp. There is no implicit current clock.
The fixed plan is reconstructed with the existing pure source decoder, avoiding
the legacy constructor's filesystem checks.

The account prefix enumerates enrolled scopes and attempts. C2 reconciliation
requires the complete related plan inventory; the body inventory must match
that scope set exactly. Measurement originals must occur exactly once per
target-plan attempt in account reservation order. Missing, duplicate, reordered,
wrong-slot and cross-Store measurements are rejected. Missing *measurement
values* in an explicitly covered attempt remain the separate `missing` class.

All source records are replayed and hashed in schema-defined order: account,
plans ordered by plan SHA, bodies ordered by plan SHA, then ordered originals.
Every element is bound with its index and previous digest using a domain-separated
canonical hash. Dependencies additionally bind C3 contract and root identities.
The commitment has fixed-size count/head fields; the offline input limit is
4096 records including measurement originals. Canonical values use the existing
32 KiB codec limit, including AdmissionCore at construction. These are offline
format limits, not an approved production plan-count or throughput target.

Measurement references must exist within supplied prefixes, refer to the same
attempt, and not postdate the measurement. A settled outcome cannot be justified
by a prefix that predates settlement. Claimed persisted bytes require a committed
receipt in the referenced body prefix. An earlier measurement must already exist
at the next reservation, rather than being backfilled from future capture.

Completeness is relative to the externally selected historical prefixes. A pure
verifier cannot prove that a caller supplied the authentic latest Store head or
an authentic measurement. Future Store integration must choose those originals
authoritatively and bind the original AdmissionCore. If both sources and claims
are replaced consistently without an external trust anchor, this module cannot
authenticate the original reality.

## Accounting and classification

The V1 rule matches the existing HTTP journal:

```
remaining_transfer = total_transfer - known_transfer - unknown_count * page_transfer
remaining_decoded  = total_decoded  - known_decoded  - unknown_count * page_decoded
remaining_saved    = total_saved   - compatible_distinct_digest_saved_total
allowance          = max(0, min(page_limit, remaining))
```

Zero allowance is accompanied by an exhaustion blocker, not permission.
Negative balances produce overrun/exhaustion. Inputs, products and cumulative
sums must fit signed int64; Python arbitrary precision is not used to silently
accept an unpersistable result. `8 * 2**62` is rejected.

Classification follows C2 outcome independently of capture completeness:

| Class | Condition |
|---|---|
| known_complete | C2 response plus complete measurement |
| known_zero | Same, with transfer/decoded/sink counts all zero |
| valid_unknown | C2 unknown or pre-send reclaim; partial/unobserved capture |
| open_unsettled | Reserved/sent/header-only attempt without completion |
| missing | Settled response lacking complete measurement |
| conflicting | Complete measurement claimed for unknown/open outcome |

Missing is never converted to zero or unknown. Diagnostic zero known sums in a
blocked result do not certify zero actual usage. A complete header block and
Content-Length are not measured body counts.

Each regular unknown and pre-send reclaim charges one whole transfer page and
one whole decoded page. Reclaim does not append a fictional plan unknown event.
Partial counts are not added to that full-page charge. Partial measurement above
the page cap or the then-available allowance sets sticky overrun, including a
one-byte overshoot. The charged fields keep the legacy formula, not an undisclosed
`max(cap, partial)` effective-charge substitution. An unknown charge is not
refunded after hold release, stable quarantine or later verification.

Fixed-plan counts, page retries, ordered query progression, cursor chain,
completion, scope expiry and elapsed origin are also checked. The first actual
reservation establishes elapsed origin. Historical elapsed/expiry is evaluated
against the explicit supplied timestamp. Prior-page receipts must already be
committed before a later reservation; a terminal 200 with an uncommitted body
blocks further admission even if its transport measurement is complete.

## Saved compatibility and C3 accounting

`saved_compatibility()` accepts only committed/stable objects or untouched
reservations with writer `not_started`, observed size zero and retained lower
bound zero. Each object must map to one covered attempt. Committed objects need
response status 200, complete Budget Evidence, matching page/binding, matching
receipt digest and persisted size, and matching sink-accepted size. All final
digests must be unique. Every response-200 attempt must have its body committed.

Staging, partial, orphan, quarantine, unstable and unknown-size objects block
compatibility. Duplicate digest objects are legal C3 evidence but incompatible
with this bridge. Their HTTP diagnostic total is distinct-digest based while
their C3 physical accounting still counts both objects. Digest/size conflict is
also rejected. Incompatible results cannot build an AdmissionCore.

C3 retained, reserved, acquired and unresolved charge are preserved separately
in `CapacityResult`; no amount is substituted for HTTP transfer. Pre-send
reclaim can leave a full C3 unresolved charge despite zero observed bytes. This
module neither refunds it nor implements a physical capacity policy or Store
reservation. The physical authenticity of stable/closed claims remains unproven.

## AdmissionCore and historical verification

`build_admission()` recomputes the reducer and rejects any blockers. The core
contains plan/approval/preflight bindings, identity, policy, fixed semantic
request hash, query/page, attempt number, source/dependency/budget commitments,
first reservation timestamp (or proposed first start), start observation reference,
budget/capacity results and all three allowances. Currently the head sets for
source and dependency commitments are the same complete account inventory.

`derive_slot_id()` hashes canonical structured data with the domain
`http-reservation-slot-v1`, deployment ID, operation ID and admission digest.
There is no ambiguous string concatenation. The result is a C2-compatible
64-character hex label. The core has no own slot ID, object ID containing that
slot, result event hash, result receipt or self-digest, so there is no hash cycle.
Historical dependencies may of course refer to earlier slots.

`verify_history()` reconstructs classifications, totals, allowances and source
commitments from originals and compares canonical bytes with the submitted
Budget Evidence and BudgetResult. `verify_admission()` rebuilds the core and
slot from originals. Neither returns an execution capability. All permission
flags remain false. Successful verification is consistency of artificial claims.

## Review findings and next gate

| Finding | Status in this change |
|---|---|
| M-BUD-1 | Unresolved: nonterminal Store session lifecycle is not implemented |
| M-BUD-2 | Partially addressed: pure core/slot binding; kernel/receipt binding remains |
| M-BUD-3 | Partially addressed: saved predicate; Store enforcement remains |
| L-BUD-1 | Structured slot encoding implemented and independently hash-tested |
| L-BUD-2 | Int64 product/sum rejection implemented and tested |
| L-BUD-3 | Partial one-byte overshoot independently blocks admission |
| L-BUD-4 | Reclaim charge preserved in both separate accounting systems |
| L-BUD-5 | Bounded source commitment and offline input/record limits; production scale open |

No Medium is marked resolved. Independent implementation review is outstanding.
I2-1A-2 and I2-1B remain gated on their separate design/owner decisions. This
module does not resolve nonterminal close or authenticate a producer. Actual
measurement capture, durable sent/settlement evidence and authoritative Store
prefix selection belong to later work. Existing findings retain their status.

Live Gate remains closed; remote account identity is unverified. Credentials,
actual HTTP/J-Quants acquisition and Formal Real OOS are unavailable. No frozen
June evidence or worktree is used. Existing Linux and macOS CI definitions are
unchanged. Skipped Live and browser tests are unverified.

## Verification

Dedicated tests compare actual legacy `EvidenceJournal` calculations for
known 400/900, zero, exact exhaustion, overshoot, 429/5xx and unknown/partial
cases. They verify body save compatibility, duplicate digests, reclaim, retries,
pagination, source omission/order/time/binding, canonical parsing, integer
overflow and historical core/slot recomputation. Socket, SQLite and environment
access are trapped during a pure reduction. Existing regression suites are run
separately from these no-I/O contract tests.

Local verification completed on macOS 15.7.7 arm64, Python 3.13.3 and
SQLite 3.49.1:

| Check | Result |
|---|---|
| Dedicated budget tests (`-W error`) | 63 passed |
| Feasibility suite | 1351 passed |
| Full pytest | 2834 passed, 13 skipped, 0 failed |
| Ruff lint | Passed |
| Ruff format check | 302 files already formatted |
| Git diff whitespace check | Passed |

The feasibility and full runs include existing HTTP, C1/C2/C3 compatibility,
I1a/I1b and I2-0 regression tests; no separate new result count is claimed for
each subset. The full run emitted 28 existing urllib3 deprecation warnings
from `data/providers/jquants_v2.py`. The 13 skips comprise seven Live tests and
six browser tests and remain unverified. Market API credentials were removed
from the test environment and Live opt-ins were disabled. GitHub CI, including
Linux Python 3.11/3.12/3.13, was not run in this local-only change.
