# C2: partial-response evidence / hold / live journal contract

## Scope and authority

Base: `f6f121f5a53f49260325f4fc30714f24aff4790d` (C1 integration).
C2 adds immutable in-memory contracts, serialization, deterministic replay and
artificial tests. It performs no HTTP, filesystem or SQLite I/O, credential read,
registration, settlement or order execution. It is not a live acquisition Store.
Existing artificial v2, C1, HTTP/storage/runner/body code and the Live Gate are
unchanged. **Live acquisition and Formal Real OOS remain prohibited.**

Matching evidence establishes internal consistency, not account identity, owner
approval authenticity, physical Store ownership or send permission. Scope hashes,
clock references and manual-review references are declarations at this layer.
Their authenticity and the actual account/clock must be verified by I1/I2.
`LiveReconciliation` authority fields and the restriction result's
`live_send_permitted` are `init=False`, always False.

## Version boundary

| Object | New schema |
| --- | --- |
| Plan events | `historical-feasibility-live-plan-journal-v1` |
| Account events | `historical-feasibility-live-account-rate-ledger-v1` |
| Header snapshot | `historical-feasibility-live-header-observation-v1` |

`historical-feasibility-journal-v2` and
`historical-feasibility-account-rate-ledger-v2` retain their existing bytes and
meaning. C2 neither writes nor migrates v2. New readers reject v2; old readers
reject live schemas. Missing historical raw headers, holds and generation facts
cannot be reconstructed by relabeling a v2 journal. I1 must reject a v2 DB at a
live Store path, without migration, reinterpretation or overwrite.

## Public values and operations

- `LivePlanScope`: fixed plan, C1 preflight, approval and account references;
  plan window, plan/account wait rules and C1 clock/lease policy.
- `AttemptBinding`: plan/preflight/approval/account, attempt/slot, holder and
  generation. All attempt events carry this complete binding.
- `capture_headers` / `HeaderObservation`: bounded immutable capture and derived
  Retry-After interpretation; no network capture is performed here.
- `LiveJournal.create`, `append`, `to_bytes`, `from_bytes`: immutable append and
  strict replay. `append` returns a new value; caller-owned dictionaries are
  serialized immediately, not retained as mutable state.
- `LiveProjection`, `AttemptState`, `LiveHold`, `HoldState`: replayed facts.
- `required_attempt_hold`: minimum restriction implied by retained evidence.
- `reconcile_live_journals`: compare all declared related plan journals and the
  authoritative account journal; never repair or copy evidence automatically.
- `assess_live_restrictions`: informational restrictions and rule-release
  candidates, never a send authorization.
- `check_body_generation`: pure prerequisite rejection, not a body commit API.

All plan scopes are declared at journal opening in this contract version; one
plan journal has one scope, an account journal can contain several. Scopes on the
same account must agree on account/preflight/account-policy facts. The canonical
`policy_sha` binds retry/account/clock rules; the opening event separately hashes
the complete scope, including its plan/approval references. Arbitrary new plan enrollment
into an existing persisted Store is not implemented.

## Events and causal order

Plan kinds: `journal_opened`, `attempt_reserved`, `attempt_sent`,
`response_headers_observed`, `response_received`, `outcome_unknown`.

Account kinds: `ledger_opened`, `slot_reserved`, `slot_sent`,
`slot_headers_observed`, `slot_settled`, `slot_reclaimed`, `hold_entered`,
`hold_extended`, `hold_released`.

Every event has exact keys `kind`, `transition_id`, `recorded_at`, `binding`,
`data`. Opening/hold events have no attempt binding; attempt events must have one.
The same logical plan/account update uses the same transition ID and normalized
payload. Recording times may differ. There are no circular cross-journal hash
references. Logical reservation/send/header/settlement times are separately
checked against recording times and each other; event recording time cannot
regress. Audit updates after plan expiry are allowed; new reservations/sends are
not. Control operations cannot use a time before previously recorded evidence.

Transport progression is `reserved -> sent -> headers_observed -> response`, or
`sent/headers_observed -> unknown`. Observation completeness and active holds are
independent dimensions. One attempt has at most one header snapshot; an incomplete
snapshot cannot later be overwritten or upgraded to complete.

Reserved-but-never-sent attempts cannot receive HTTP `unknown`. A lease-expired
C1 manual reclaim produces `pre_send_reclaim` on the account side, retaining the
plan reservation without inventing an HTTP response. It still requires manual
recovery and a conservative cooldown. A sent reclaim retains any headers and
requires matching plan-side unknown evidence; missing/mismatched evidence remains
pending/inconsistent, not success.

## Header facts and Retry-After

The snapshot records observation ID, status, `header_block_complete`, fixed
`observed_at`, presence, raw fields, parsed representation and capture diagnostics.
Final response statuses are integers 200 through 599, never bool. Incomplete
capture can lack a status; a complete snapshot cannot. Presence is `present`,
`absent` or `undetermined`; only a complete snapshot may declare absent.

Raw Retry-After fields remain separate, never comma-joined. Each field is at most
1024 octets, at most four fields, printable ASCII 0x20 through 0x7e only. Invalid
capture becomes bounded diagnostics (reason, index, length/unit, at most 64-octet
hex prefix), not dropped evidence. Diagnostics must not be decoded into logs.
Known valid fields alongside invalid/duplicate fields retain their interpretable
wait floors, while the anomaly still requires indefinite manual hold.

Parse states are `absent`, `delta_seconds`, `http_date`, `past_http_date`,
`malformed`, `out_of_range`, `duplicate_field`, `capture_rejected`, `incomplete`.
Parsed values are rederived from raw plus fixed observation time on construction
and read; caller-supplied serialized parsed values must match. Current wall time
is never consulted. Numeric length/range is checked before integer conversion.
The automatic delta/date-delay limit is 86400 seconds, never a clipping target.

M-C2-1 separates that automatic limit from known lower bounds. A syntactically
valid delta above 86400 retains its exact `delay_seconds` with `out_of_range`
classification when it fits both int64 and the remaining UTC datetime range at
the fixed observation time. Digit count and lexical magnitude are checked before
integer conversion. Thus 86401, 99999 and 100000 retain their full wait floors;
manual review cannot shorten them. A valid future HTTP-date above the automatic
limit likewise remains `out_of_range` and retains the absolute date as a floor.

An unrepresentable delta (for example 1024 significant digits) is
`capture_rejected`, with no numeric `delay_seconds` or fabricated server deadline.
Bounded raw/diagnostic evidence and indefinite manual hold remain. A hold may
still contain an independently known local rule floor: that is not a substitute
interpretation of the unparsed server value. If a representable delta cannot be
added to a later settlement timestamp without UTC overflow, deadline derivation
fails closed rather than wrapping, clipping or silently losing the bound.

HTTP dates support IMF-fixdate, obsolete RFC850 and asctime English forms, with
UTC and calendar/weekday validation. RFC850's two-digit-year interpretation uses
the fixed observation timestamp and the 50-year boundary, not today's date.
Past dates remain evidence, with zero additional Retry-After wait; ordinary plan
and account cooldowns remain applicable.

## Owner decisions and holds

**OD-C2-01:** malformed, out-of-range, duplicate or incomplete capture creates an
indefinite manual hold. It never becomes a finite 120/86400-second shortcut.

**OD-C2-02:** sent unknown requires all known wait floors plus manual review.
Unknown describes an unresolved final outcome, not absence of header evidence.
For example, observed 429/600 followed by body timeout retains 429/600, with manual
release prohibited before the resulting lower bound. Only a complete known
response can qualify for ordinary rule-based release.

The account event stream is the hold authority. Multiple holds may coexist.
Each contains ID, reason, source refs, `not_before`, release mode, indefinite flag,
policy SHA and recording time. A derived attempt hold references its reservation,
header observation/transition and settlement, where available. Missing or weaker
hold projections prevent consistent usable evidence and further account control.
Intermediate one-sided journal values are representable for crash analysis; they
are not safe-to-send states.

For delta Retry-After the lower bound is at least
`max(header.observed_at, settled_at) + delta`; before settlement it is
`header.observed_at + delta`. Plan, account, parsed Retry-After and additional
holds take their maximum. Unknown includes the maximum of minimum interval,
network-error and 429 rules from both plan and account. Header-derived 429/5xx
restrictions remain even when the final outcome is unknown.

Extensions may only strengthen: later deadline, added source refs,
rule-based-to-manual or finite-to-indefinite. Hold identity/reason/policy cannot
change, and recording time cannot regress. Deadline passage does not remove a
hold. Explicit `hold_released` records clock/account references; manual release
also requires a review reference and cannot undercut known deadlines. An
inconsistency hold additionally requires a repair reference. These are evidence
contracts, not proof that the referenced human review or repair truly occurred.
Open attempts or insufficient derived holds prevent release. Other active manual
holds suppress automatic rule-release candidates.

M-C2-2 binds every release (manual or rule-based) to the current hold snapshot.
`LiveHold.hold_version_sha256` hashes its complete canonical content: ID, reason,
source refs, deadline, mode, indefinite flag, policy SHA and recording time.
`hold_released.data.expected_hold_sha256` is mandatory. Replay first locates the
active hold and checks this hash, then evaluates deadlines and other release
conditions. Extension changes the version; a release with the previous version
fails with `live_hold_version_mismatch` even after the new deadline. Reopening
reconstructs the same version. Missing version fields are rejected, not migrated.
The hash identifies the reviewed content only: it does not authenticate the
reviewer, approval, or the claim that a review actually occurred. C1's authenticity
boundary is unchanged.

## Generation and reclaim

Account generation starts at zero, increments on reservation and reclaim, never
reuses or regresses. Plan generations can skip values used by other plans.
Send/header/settlement must use the active attempt's current generation. Reclaim
uses C1 evidence and requires the exact lease boundary and eligible manual
decision; it invalidates old callbacks. Old-generation header/settlement/body
prerequisites fail with `stale_generation_manual_hold_review_required`.

Rejecting a stale callback does **not** prove that its network operation stopped.
If it reveals a longer Retry-After, it is a manual-hold candidate; the future
coordinator must preserve that evidence and add a `stale_generation` manual hold,
not silently retry. C2 neither captures such callbacks nor automatically converts
rejected events into new evidence. Token/nonce redesign and actual fencing/socket
shutdown remain outside C2.

## Crash and reconciliation matrix

| Available evidence | Contract treatment |
| --- | --- |
| Reserved, never sent | C1 manual reclaim; not HTTP unknown |
| Sent, no persisted headers | Unknown candidate, conservative wait and manual review |
| Partial headers persisted | Retain bounded facts, indefinite manual hold |
| Complete headers observed but not persisted | No invented snapshot; sent unknown |
| Headers persisted, body stops | Unknown retains status/raw/parsed evidence |
| Body allegedly complete, no settlement | Do not infer success from body existence |
| One header copy, peer not advanced | `pending_header_pair` |
| One header copy, peer already settled | Inconsistent; manual investigation |
| Both headers and unknown/429/600 agree | Consistent only with adequate hold projection |

Reconciliation requires all opened account plan scopes, rejecting omitted,
duplicate or unrelated journals. It compares binding, transition IDs, status,
Retry-After/capture facts, observation identity/completeness and actual times.
Important reasons include `header_binding_mismatch`, `header_status_mismatch`,
`retry_after_evidence_mismatch`, `header_before_send`,
`settlement_before_header_evidence`, `hold_projection_mismatch` and
`generation_projection_mismatch`. Invalid local transitions are rejected during
append/read before reconciliation. No automatic repair is provided. An optional
claimed hold/generation projection is checked against account replay.

`consistent`, `pending`, `inconsistent` are evidence states, not permissions.
An inconsistent/pending result does not provide a reliable next-send timestamp.
Even a consistent result after all explicit releases cannot open the Live Gate.

## Serialization and integrity limits

Canonical records use UTF-8, sorted keys, compact separators, no NaN and canonical
UTC microseconds. Exact key sets, event/schema allowlists and JSON duplicate-key
rejection apply. Each event record is limited to 32 KiB before JSON parsing;
sequence/generation/count-like integers are bounded to signed 64-bit positive
ranges as applicable, with bool rejected. Negative/huge/non-finite JSON values
cannot bypass field validation. Records require a final newline.

The event digest covers schema, sequence, previous hash and normalized event,
excluding its own hash. Header parsed values are checked even if an attacker
recomputes the outer chain. A fully rewritten internally consistent history, or
a valid prefix with no trusted external head, cannot be authenticated by a hash
chain alone. External anchoring/signatures remain unimplemented.

Replay is intentionally simple: each append validates the complete prior
journal. Total construction cost can be quadratic, and total journal size is not
a streaming Store budget. Only bounded artificial workloads are tested; production
performance, persistent projections and bounded streaming reads belong to I1.

## Tests and deferred work

Dedicated tests cover complete/partial 429 and 600-second floors, 200/503 body
timeout models, sent unknown, pre-send reclaim, bounded anomalous capture, all
supported dates, fixed-time reopen, hash/raw/parsed tampering, causal order,
paired/pending/inconsistent journals, hold extension/release, generation replay,
stale callbacks/body prerequisites, plan expiry, legacy rejection and closed Gate.
These are pure artificial models, not localhost partial-response or live tests.

I1 must implement live SQLite schema, physical canonical Store, authentic registry,
Store-owned UTC, durable projection checks, v2-path rejection and suitable capacity
controls. I2 must capture actual headers, atomically append both journal events
and necessary holds, enforce ownership/timeout/monotonic budgets, close sockets
on callback failure and test real crash positions with localhost fixtures.

**L-C2-1 — mandatory I1/I2 coordination:** before any new account reservation,
send or hold release, the future live Store/runner must reconcile **all related
plan journals** with the account journal and require `consistent`. Account-only
state is insufficient; `pending` or `inconsistent` prohibits sending/control
authorization. Record `ledger_inconsistency` / `stale_generation` manual holds
when warranted. C2's account-journal reducer operations alone never authorize a
live send; this cross-journal execution interlock is not implemented in C2.

**L-C2-5 — mandatory I2 write-before-send ordering:** persistent reservation
must be followed by an atomic durable commit of both plan/account sent
transitions, successful commit confirmation, and only then the first HTTP request
byte. If sent evidence persistence fails, send no request. This prevents an
actually sent request from becoming `pre_send_reclaim` after restart. The inverse
crash window (sent evidence committed but no byte sent) remains conservatively
sent/unknown, not inferred unsent. C2 does not implement this I/O ordering.

Other Low findings are deliberately unchanged: L-C2-2 Unicode/bidi reference
hardening, L-C2-3 global transition-ID uniqueness, L-C2-4 pure cross-check against
the C1 preflight object, and L-C2-6 reason-code granularity.

C3 retains M4 duplicate-body semantics, M5 physical output-root binding and N2
quarantine/storage accounting. N3/N4 and remaining findings outside C2 are not
resolved here. M1/L3/L4 are addressed only at the pure-contract layer; existing
localhost v2 runtime behavior is unchanged. Passing C2 tests does not complete
I1/I2, authorize J-Quants acquisition or qualify Formal Real OOS.
