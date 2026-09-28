# Historical feasibility: live-preflight C1 contract

Base: `77184ff51532853e3f5b415053b5f215192d83e4`

Status: **pure preflight contract only**. This design does not enable J-Quants HTTP,
credential loading, the Live Gate, or Formal Real OOS.

## Scope

C1 fixes the owner-approved H1/M3 boundary before any live sender is implemented.
It covers OD-01〜OD-03 and the account/clock portions of OD-08〜OD-09:

- one authenticated account is operated through one canonical sender;
- all credentials/clients for that account belong to the same account scope;
- the canonical ledger path is derived from owner-managed root + `account_ref`, not caller input;
- persistent time is Store-owned UTC and in-flight timeout time is process-local monotonic;
- caller-supplied event timestamps are forbidden in the future live path;
- lease expiry alone never proves an old HTTP request ended;
- automatic reclaim is forbidden; manual recovery requires explicit evidence;
- C1 uses new versioned schemas and does not reinterpret the existing account-ledger v2 DB.

M1, M4, M5, N2/N3 and the event/schema migration work are deliberately deferred to C2/C3.

## H1: account identity and canonical Store

`LiveAccountIdentityRecord` is an owner-managed **claim**, not authentication proof.
It stores an opaque `credential_reference`; API keys, refresh tokens and secret material
must not be persisted in the contract, ledger, receipt or log.

The canonical ledger location is always:

```text
<canonical_store_root>/accounts/<account_ref>/account-rate-ledger.sqlite3
```

No database path is supplied as an argument to this derivation. Different identity-record
IDs or credential references for the same root and `account_ref` therefore resolve to the
same ledger path. C1 rejects relative/non-normalized roots and roots inside the existing
plan HTTP output tree.

C1 does **not** prove inode/device identity, symlink/hard-link/mount safety, permissions,
or that another worktree cannot alias the root. Those physical guarantees belong to I1.

`LiveAccountOperatingPolicy` fixes the owner operation rule:

- `single_canonical_sender`;
- external clients are `prohibited_during_live_acquisition`;
- an exclusivity violation or inability to verify it means `hold_and_stop`.

Therefore a second Mac, browser, notebook, shell, repository or other API-key path for the
same authenticated account must not be used during the acquisition. If that operational
condition cannot be met, the system must not claim account-wide rate-limit control.

## M3: authoritative clock, lease and reclaim

`LiveClockPolicy` fixes the clock responsibilities:

- persistent/audit decisions: Store-owned UTC;
- one in-flight transport deadline: process-local monotonic clock;
- arbitrary caller event timestamps: forbidden;
- clock anomaly or restart uncertainty: hold for manual recovery;
- automatic reclaim: never allowed.

For reservation time `R`, send time `S`, maximum HTTP duration `T`, guard `G`, and lease
length `L`, the contract requires the strict invariant:

```text
S + T + G < R + L
```

With a configured upper bound on `S-R`, C1 checks:

```text
slot_lease_seconds >=
    max_reservation_to_send_seconds
  + request_timeout_seconds
  + termination_guard_seconds
  + 1
```

This bound does not prove the old socket/process terminated. `LiveReclaimEvidence` and
`assess_live_reclaim()` therefore only identify a **manual-reclaim candidate** when all of
these are present:

1. the trusted observation is at/after lease expiry;
2. clock status is `verified`;
3. prior transport termination has evidence;
4. an operator review reference exists.

The returned assessment is not an authorization token. I1 must rebuild/recheck persistent
raw evidence inside the canonical Store transaction. A process restart is not positive
termination evidence by itself.

## Versioning and trust boundary

C1 introduces:

- `historical-feasibility-live-account-identity-v1`;
- `historical-feasibility-live-account-preflight-v1`.

It does not change `historical-feasibility-account-rate-ledger-v2`. Existing localhost
SQLite data must not be silently migrated or reinterpreted under the live-preflight
semantics.

The current threat boundary remains trusted application code. C1 prevents normal API
misconfiguration from turning an arbitrary DB path or arbitrary clock into live authority;
it does not attempt to protect against arbitrary malicious code with access to the same
Python process, credentials and files. Reservation nonce/capability hardening is an I1
implementation concern and does not replace H1 or owner-approval authenticity.

## Non-authorizing alignment

`check_live_account_preflight()` checks only declarative content alignment with a fixed
`HttpAcquisitionPlan`: `account_ref`, account-rate policy coverage and the lease guard.
The returned `LiveAccountContractAlignment` deliberately keeps these flags false:

- authenticated account identity verified;
- canonical physical Store implemented;
- external-client exclusivity verified;
- authoritative clock implemented;
- live send permitted.

Consequently the existing Live Gate remains closed. C1 is not permission to perform real
J-Quants requests or to register/start Formal Real OOS.

## Next work

After independent review of C1:

1. C2: partial-header/429 evidence, append-only hold/generation history and schema migration rules;
2. C3: duplicate-body semantics, `plan.output_dir` physical binding, body/quarantine budgets;
3. I1: implement identity registry, canonical Store, trusted Clock, path/inode enforcement,
   manual recovery and capability hardening—still with the Live Gate closed.
