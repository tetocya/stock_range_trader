# C2 versioned plan enrollment contract

Base: `d20cd70095fe8af0fa8b2c091b558c22c260b6b5`.

This is an in-memory pure evidence contract, not I1, a Store, activation, an
authenticated approval mechanism, or permission for HTTP / Formal Real OOS.
No filesystem, SQLite, socket, credential read, subprocess or actual clock read
is added. All timestamps are supplied evidence. No migration is provided.

## Schema boundary and compatibility

| Role | Exact schema | Opening |
| --- | --- | --- |
| Existing live account | `historical-feasibility-live-account-rate-ledger-v1` | Original fixed nonempty scopes |
| New live account | `historical-feasibility-live-account-rate-ledger-v2` | Authority, opened=true, empty enrolled set |
| Plan | `historical-feasibility-live-plan-journal-v1` | Original single-scope journal opening |
| Artificial account | `historical-feasibility-account-rate-ledger-v2` | Unchanged, not a live schema |

`LIVE_ACCOUNT_SCHEMA` remains the v1 alias. V2 is explicitly named
`LIVE_ACCOUNT_SCHEMA_V2`. Readers require the exact expected schema; neither
decode failures nor matching suffixes select another reader. V1 canonical
records, hash, projection, event allowlist and opening semantics are unchanged.
V1 rejects `plan_enrolled`. V2 uses an independent `opened` flag and a dedicated
reducer, not the emptiness of scopes as the opening test.

## Authority and source binding

`LiveAccountAuthority` binds account_ref, preflight SHA, account rate policy and
clock policy in v2 `ledger_opened`. Its initial enrollment revision and request
generation are both zero. All plans, including the first, require enrollment.
This is one declared account authority, not proof of physical one-Store-per-account.

`live_plan_enrollment` depends on C2 / C1 / HTTP contracts, never vice versa.
`authority_from_preflight` revalidates C1 canonical content, identity registry,
selected identity and policies. `derive_enrollment_intent` re-derives fixed plan
scope, checks canonical plan content and digest, checks canonical approval
content and all approval fields, account binding and policy coverage.
It uses lexical dedicated output-directory validation without running the
legacy constructors' filesystem checks. C1 canonical content must equal the
supplied snapshot; comparing caller-supplied SHA strings alone is insufficient.

Enrollment time T satisfies:

`plan.not_before <= approval.valid_from <= T < approval.valid_until <= plan.expires_at`.

The derived execution `LivePlanScope` uses the narrower approval time window.
Preflight evidence must already exist at T. Prepared catalogs are not implemented.
Content consistency does not attest registry or approval authenticity.

## Enrollment event and pair

V2 alone accepts `plan_enrolled` with null attempt binding. Its data contains
exactly `scope`, `plan_journal_initial_head`, and `prior_plan_heads`.
The last field lists every already-enrolled plan as `{plan_sha, head}` in sorted
plan-SHA order, with no omission, extra entry, duplicate, truncation or summary.
The existing 32 KiB record limit applies, including the full list.

The plan v1 opening is calculated first. Its head is included in the account
event. Both parts share scope, operation/transition ID and recorded_at. The plan
does not reference the resulting account hash, avoiding circular hashing.
Receipt event-part identities are `account_enrollment` and `plan_opening`.

`LiveAccountProjectionV2` extends the unchanged v1 projection with opened,
authority, enrollments and enrollment_revision. Revision increments once per
enrollment, independent of request generation. Existing attempts, holds, hold
versions, source references, waits, rate history and generation are retained.
Enrollment emits no hold release or reset events.

## Proposal, idempotency and atomic boundary

`propose_plan_enrollment` accepts complete current journals, source documents,
expected_account_head, operation ID and supplied time. It returns candidate
journals and an immutable receipt, with live permissions fixed false.
The intent binds authority, operation kind and a freshly derived scope whose
plan/preflight/approval digests bind the canonical source documents.

Same operation + same verified intent returns the original receipt without
appending events or changing timestamps/revision. Revalidation for receipt
retrieval uses the original operation time, even after approval expiry; it is
not a renewed enrollment. Different intent with the same ID is rejected.
A different operation for the same plan is also rejected. Raw malformed events
are never interpreted as idempotent retries.

Receipt fixes original time, previous account head, prior plan heads, initial
plan head, account enrollment hash, intent SHA and revision. Later history is
not rolled back when a receipt is retrieved. Pure receipt values are evidence,
not independently authenticated capabilities.

I1 activation must atomically commit account enrollment, matching plan opening,
matching C3 initialization and catalog active state. This PR implements none of
that persistence or activation. Account-only or plan-only states are inconsistent.
Caller head mismatches are rejected. Detecting an entirely stale caller snapshot
requires I1 to compare expected_account_head against its locked authoritative
Store and obtain all related journals from the DB, not trust a caller list.
Store-global operation uniqueness remains I1 work; C2 L-C2-3 is not solved here.

## In-flight and holds

Enrollment rejects authoritative reserved/sent/headers-observed slots,
unrecovered unknown outcomes, and pending/inconsistent related evidence.
Unknown requires its required hold to have completed the existing explicit
release/review process. It is not blocked forever after recovery.

A historical plan reservation abandoned by a valid account pre_send_reclaim
does not permanently block enrollment when reconciliation is consistent.
Completed 429 wait holds and malformed-Retry-After manual holds alone may remain
while another plan is enrolled. Their IDs, bounds, release modes, indefinite
flags, references and version hashes are unchanged. Enrollment does not permit
a request while those holds are active. Future clock/Store recovery holds must
block ordinary mutation at I1, independently of this pure enrollment rule.

## Replay and reconciliation

`LiveJournal.prefix(H)` replays only records through H. E(H) contains exactly
plans enrolled by that prefix; later B is not retroactively required at A's head.
Current reconciliation requires all currently enrolled plans, rejecting caller
omissions, duplicate journals and unrelated plans. Enrollment receipts bind
prior plan prefixes; their attempts/holds are checked against the account prefix
immediately before enrollment. Current journals must still reconcile separately.

The original consistent/pending/inconsistent classifications are retained.
V2 adds these inconsistent reasons:

- `plan_enrollment_missing_journal`
- `plan_journal_not_enrolled`
- `plan_enrollment_binding_mismatch`
- `plan_enrollment_initial_head_mismatch`
- `plan_enrollment_transition_mismatch`
- `plan_enrollment_time_mismatch`
- `plan_enrollment_prior_heads_mismatch`

Raw account replay can check prior-set shape and hash-chain validity, but cannot
prove referenced plan contents without those journals. Consumers must use full
reconciliation and the binding builder; replay alone is not activation.

## C3 and remaining work

C3 files and schema v1 are unchanged. Existing C3 tests continue on account v1.
C3 account-v2 compatibility, saved fields, receipts and historical-head meaning
require a separate PR and independent review; no such compatibility is claimed
by this change. C3 initialization is only an I1 atomic-boundary requirement here.

Physical canonical Store, authoritative clock, root/device/inode checks,
persistent schema, locks, crash recovery, HTTPS, credentials, authentication and
Live Gate permission remain unimplemented. Enrollment / active / consistent
never imply permission. Formal Real OOS and J-Quants acquisition remain prohibited.

## Verification

Artificial tests cover empty opening, A/B enrollment, historical prefixes,
source/authority conflicts, approval boundaries, in-flight rejection, recovered
unknown and pre-send reclaim, hold and rate invariance, generation progression,
idempotency, paired-state omissions/mismatches, prior-head completeness/order,
record overflow, canonical tamper, exact schema readers and pure/no-I/O behavior.
V1 golden byte and head hashes were independently calculated from the fixed
base's module; they are pinned in regression tests. Existing C2/C3, feasibility
and full pytest suites are also required. Live 7 and browser 6 skips remain
unverified; no market API or actual activation is exercised.
