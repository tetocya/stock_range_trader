# I2-1A-2A: typed reservation boundary and nonterminal lifecycle

## Scope and fixed base

Base: `c3458a7221ff1feed8e55f8003e85b1039ad603d` (PR #25 merge).
Tree: `229409b4241d9cd1e1e1067e652979caeda7d850`.
Branch: `real-oos/i2-1a-2a-lifecycle-boundary`.

This change fixes option B as the artificial development boundary. It adds pure
request/role schemas and tests the existing runtime's dirty termination behavior.
It does not register an Atomic Reservation handler or persist reservation events.
The prior budget and atomic-reservation branches retain their original commits.

StoreSession `_begin()` / `close()`, `open_store()`, session/fence checks,
persistent stop, physical pin, OS flock, SQLite guards, I2-0 terminal assessment
and receipt-only read remain unchanged. No suspension, resume, cancel, recovery,
repin or stop-clear API is introduced. Existing Store DDL, runtime schemas,
RuntimePolicy, C1/C2/C3 schemas, receipt six-field format, operation handlers,
clean profiles and Budget v1 are unchanged.

## Typed request

`HTTPReservationRequestV1` uses schema
`historical-feasibility-http-reservation-request-v1` and exactly four fields:

| Field | Meaning |
|---|---|
| `plan_sha` | Fixed plan content SHA-256 |
| `query_id` | A fixed query's SHA-256 |
| `page_index` | Zero-based page index, nonnegative int64 |
| `semantic_request_sha` | Structured hash of plan/query/page |

The semantic hash uses the existing Budget v1 domain
`http-reservation-semantic-v1` and the canonical value containing `plan_sha`,
`query_id`, `page_index`. Tests compare it with Budget AdmissionCore and a separate
stdlib JSON/SHA-256 calculation. It does not include a cursor, attempt number,
clock, holder, generation, slot, receipt, or its own digest.

The frozen value uses the existing strict canonical codec: unknown/missing and
duplicate fields, unknown schema, noncanonical bytes, invalid digests, bool as
integer, negative/overflow page numbers and oversized records are rejected.
Its semantic hash is recomputed on construction and decoding. The record limit
is 32 KiB; integer validation uses the existing int64 limit.

`validate_request(request, snapshot)` revalidates the request, decodes the fixed
plan without filesystem access, checks exact plan binding, membership of the
query in that plan, and `page_index < max_pages_per_query`. Success returns
`None`, not a capability. A well-formed later page is allowed by this structural
boundary; authoritative history must determine whether that page is next.

`validate_operation_id()` checks the existing Store/C2 label syntax separately
from the semantic request. It does not prove uniqueness or idempotency. Those
checks remain the responsibility of a future Store transaction. There is no
request-to-executable-OperationIntent conversion or public execution function.

No public input accepts caller heads, holder, attempt number, generation, slot,
cursor, time, SQL, callback or permission override. The future handler must
derive these from authoritative originals; the caller cannot provide them here.

## Fixed kind and roles

`ReservationBoundaryV1` uses schema
`historical-feasibility-http-reservation-boundary-v1`. Its kind is exactly
`i2-http-reserve-v1` and its ordered role list is exactly:

| Role | Journal | Event |
|---|---|---|
| `account-reservation` | C2 account | `slot_reserved` |
| `plan-reservation` | C2 plan | `attempt_reserved` |
| `body-reservation` | C3 body | `reserved` |

Each role is a typed `historical-feasibility-http-reservation-role-v1` value.
The enclosing boundary rejects missing, extra, reordered or altered roles and
altered kinds. Only one body-reservation role is allowed. Both
`reservation_permitted` and `live_send_permitted` are fixed false.

This is a future contract, not a kernel registration. Existing `_rules_for()`
rejects the kind; `_prepare()` remains unchanged. A test submits the kind to the
existing kernel on an artificial idle Store and verifies rejection without any
business or runtime row changes. The new module has no Store/SQLite/HTTP entry.

## Option B and evidence limits

Clean close and dirty termination have distinct meanings. If complete terminal
evidence cannot be proved, close must not write `session_closed`. Failure
invalidates the handle, rolls back its uncommitted changes and releases the OS
lock. The last durable runtime state remains running until a subsequent business
open detects `restart_uncertain`, records persistent stop and leaves the runtime
stopped. Later business opens reject persistent stop. No recovery is automatic.

The tests establish the following separately:

1. **Pure reserved oracle:** genuine in-memory C2 account v2 / plan and C3 reserved
   evidence yields I2-0 `incomplete`; reservation originals and C3 reserved
   capacity remain byte-for-byte unchanged. It grants no reservation or recovery
   permission.
2. **Close failure handling:** a test-only private seam supplies that oracle to
   the existing I2-0 clean checker during real StoreSession close. The checker
   rejects it, the runtime writes no close/checkpoint marker, the handle becomes
   invalid, locks are released, and the original I1b receipt remains readable.
   This Store itself contains only valid idle I1b enrollment. No fake committed
   reservation operation or receipt is inserted.
3. **Real process termination:** a spawned process opens/enrolls an artificial
   Store, then exits without close. A second process cannot open or read receipts
   while its lock is held. After exit, the original enrollment receipt can be
   read without changing Store contents. Business open then records
   `restart_uncertain`; persistent stop blocks later opens, while the same
   historical receipt remains readable. Absent and conflicting operation reads
   are not reported as successful receipts.
4. **Idle positive control:** I1b enrollment can clean-close, reopen with an
   advanced fence, and return the identical original receipt.
5. **Guards:** stale fence and replacement of an artificial pinned lock inode
   reject close without evidence changes; backwards clock persists stop and
   cannot produce a clean marker.

There is **no integrated valid Store reservation operation or reservation
receipt in 2A**. End-to-end verification of persisted reservation + original
reservation receipt + dirty lifecycle must be completed in 2B after its separate
approval. The oracle seam is test-only and does not authenticate synthetic
evidence as production evidence or weaken the production close checker.

Receipt-only reads retain their existing requirements: physical authority,
exclusive lock, schema/runtime core validity and historical receipt verification.
They do not reopen a business session, clear stop, retry, reclaim or clean up.

## Findings and implementation gates

| Finding | Status |
|---|---|
| M-BUD-1 | Open for production; option B tested within the artificial boundary |
| M-BUD-2 | Partially addressed by pure Core/slot; Store event/receipt binding pending |
| M-BUD-3 | Partially addressed by pure Saved predicate; Store enforcement pending |
| L-BUD-4 | Open |
| L-BUD-5 | Open: historical replay performance |
| L-BUD-6 | Open: non-success-body Saved policy v2 |
| L-BUD-7 | Open: sequential historical reservation admission verification |
| L-I1A3-1 | Open |

Other findings retain their status. Completing 2A does not resolve M-BUD-1 for
production. I2-1A-2B/2C, Durable Sent, Transport, Settlement and Physical Writer
remain outside this change. Live Gate remains closed; remote account identity
is unverified. Actual HTTP/J-Quants acquisition and Formal Real OOS are not
permitted. No frozen June worktree, saved evidence or approval is used.

## Validation and timing interpretation

The dedicated tests use pure fixtures and independent artificial Store roots
from the existing runtime test fixtures. Live opt-ins are disabled and market
credentials are removed from the test environment. The Store fixtures preserve
C1's temporary-root rejection by creating unique private artificial directories
through the existing fixture, not by relaxing root validation.

Local environment: macOS 15.7.7 arm64, Python 3.13.3, SQLite 3.49.1, APFS
(confirmed using the existing native filesystem probe).

One dedicated run (42 passed, 3.01 s) measured the following with
`time.perf_counter()` around individual API calls on one-account/one-plan idle
enrollment fixtures: clean close 0.0853 s, original receipt read 0.0197 s,
clean reopen 0.0386 s. After a spawned process exited dirty, receipt-only read
was 0.0205 s and restart rejection 0.0358 s. These are single local observations,
not a production SLA or a performance acceptance threshold.

The prior review's Budget replay samples (97 records about 0.98 s, 192 about
3.88 s, 382 about 16 s) are reported historical observations, not rerun or
improved by 2A. L-BUD-5 remains open. Existing Linux/macOS CI definitions and
targeted runtime tests are unchanged; new 2A tests are not claimed to be part of
the existing macOS targeted job.

The first full-suite run completed with 2875 passed, 13 skipped and one failure:
the unchanged `test_replay_performance_samples[1000]` in the existing I1a-2B
suite raised `store_clock_uncertain` during its second real-clock close. The
same test passed on immediate isolated rerun (1 passed, 2.16 s); it had also
passed in the targeted regression and feasibility runs. The captured traceback
does not distinguish UTC regression, monotonic regression or excessive drift;
the exact environmental cause is not established. No runtime guard, clock
policy, existing test or timeout was changed to obtain a pass. The first failure
remains part of the verification record, rather than being counted as success.

Final local results:

| Check | Result |
|---|---|
| 2A dedicated (`-W error`) | 42 passed |
| I1a/I1b/I2-0/Budget targeted regression | 616 passed |
| Feasibility suite | 1393 passed |
| Full pytest, unchanged-code recheck | 2876 passed, 13 skipped, 0 failed (733.01 s) |
| Ruff check | Passed |
| Ruff format check | 305 files already formatted |
| Git diff whitespace check | Passed |

The full recheck ran with the same code and test selection as the first run.
Seven Live tests and six browser tests are skipped and unverified. The 28
warnings originate in the unchanged urllib3 Retry configuration in
`data/providers/jquants_v2.py`. GitHub CI and independent implementation review
have not been performed for 2A.

Commands (from the Python project directory, with market credentials removed
and Live/browser opt-ins disabled):

```text
python -B -m pytest -q -p no:cacheprovider tests/test_feasibility_live_http_reservation_boundary.py -W error
python -B -m pytest -q -p no:cacheprovider tests/test_feasibility_live_store_i1a1.py tests/test_feasibility_live_store_i1a2a.py tests/test_feasibility_live_store_i1a2b.py tests/test_feasibility_live_store_i1a3.py tests/test_feasibility_live_store_i1b.py tests/test_feasibility_live_store_i2_0.py tests/test_feasibility_live_http_budget.py
python -B -m pytest -q -p no:cacheprovider tests/test_feasibility*
python -B -m pytest -q -p no:cacheprovider
ruff check --no-cache .
ruff format --check --no-cache .
git diff --check
```
