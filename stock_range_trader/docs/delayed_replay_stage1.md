# Delayed replay: Stage 1 contracts only

Status: DRAFT / NOT REGISTERED. No OOS has been started by these contracts.

## Scope and compatibility

The architecture is accepted, but proposed parameter values are not approved.
Only J-Quants Free, delayed replay, a single shared account with JPY 200,000,
100-share lots (no odd lots), and monthly reselection are confirmed here.
`DelayedReplayConfig` has defaults only for those conditions. Every field in
`ReplayPolicies` defaults to `None`, meaning unresolved, not a runtime default.
`require_complete()` reports missing choices; it does not approve references,
verify their implementations, register an experiment, or authorize execution.
Tests supply explicitly named synthetic policy references and numeric choices.
Those test choices are not proposed production configuration.

`DelayedReplayConfig.validate_for_execution()` rejects a draft with any
unresolved policy, including a single missing field in an otherwise complete
configuration. Passing it checks schema completeness only: approval, reference
resolution, data evidence and registration are separate future prerequisites.

This package does not implement accounts, reservations, fills, selection,
runners, checkpoints, registration, audit persistence, API access or a CLI.
Existing Phase 3 interfaces, YAML settings, and result formats are unchanged.
Package discovery adds only `delayed_replay*`.

## Clock and visibility

`ReplayClock` requires timezone-aware market decision and replay wall timestamps.
No implicit `now()`, timezone guessing, holiday rounding or inferred sessions
are used. Both clocks advance monotonically and market time cannot exceed wall
time. Session dates are explicit calendar dates; forward-session counting is
not implemented in this stage.

`ReplayDataView.history(clock, start=...)` returns immutable daily observations:

- session date in `[start, market decision date in Asia/Tokyo)`;
- market availability strictly before the market decision timestamp;
- snapshot fetched by replay wall time, including equality;
- first observation no later than fetch (validated by the snapshot).

Thus an entire current-session daily bar is unavailable, even at a decision
after that session's close. This conservative, previous-session-only interface
is intended for monthly selection and prior-day decisions, not intraday fills.
The restriction is common to every call to `history()`; Stage 1 has no event-mode
switch. In particular it cannot serve same-day post-close signal generation.
That future use requires a separate, explicit post-close visibility contract,
with tests proving the close is final and available, without weakening the
monthly-selection or pre-open boundaries. No such interface is added here.
Future execution must use a separate next-open event interface, only after order
quantity is frozen; it must never hand a full current-session bar to strategy.
Empty history is an explicit empty tuple. Missing dates are never filled.
Only returned history, never the archive/data-view object, should be passed to
selection/strategy. Runner-level enforcement belongs to a later stage.

## Snapshot evidence and limitations

`PriceObservation` stores separate provider-reported raw and adjusted OHLCV
lanes, explicit market availability, adjustment factor, split and dividend data.
Raw does not mean independently verified executable historical prices.
Snapshot construction performs no share adjustment or price reconstruction.

`PriceSnapshot` stores immutable observations, provider basis, original artifact
SHA-256 reference, data version, first-observed and fetched timestamps, optional
publication timestamp, and an optional price-basis evidence reference. Unknown
publication time requires a reason; it is never fabricated. An evidence reference
alone does not prove price validity. The original artifact itself is not read or
verified by this in-memory schema; future ingestion must check it against its hash.

The payload digest includes receipt metadata and all price fields; construction
recomputes it, including when a snapshot is built directly. Ordering is normalized
by symbol/date and timestamps by UTC. A revision produces another snapshot;
the view never silently selects the newest version. Visible overlapping
symbol/date records require an explicit version choice, even when prices match.
Retain the pinned snapshot IDs in later selection/audit provenance.

Mutable OHLCV lists and observation lists are rejected, not retained by reference.
Callers explicitly convert source data to immutable tuples/records. Mutating the
original input mapping or source lists afterward does not change the snapshot.
Returned history is a tuple of frozen records: direct mutation is rejected;
changing a caller-owned list or `dataclasses.asdict()` export cannot change the
snapshot or its digest. These are normal API guarantees, not a security sandbox
against deliberate Python reflection such as bypassing frozen attributes.

Future rows do not appear in history. This alone does not remove retrospective
adjustments to *past* rows. Immutable snapshots, explicit versions, and separate
price-basis verification remain necessary before executable use. This stage
does not authorize executable use, including when all schema fields are filled.
Yfinance and unknown providers are rejected by this dedicated J-Quants contract;
the existing Phase 3 yfinance executable prohibition remains unchanged.

Delayed replay cannot prove orders existed before the historical market open,
contemporaneous provider delivery, or that humans had not seen market outcomes.
Any future result must retain `execution_mode=delayed_oos_replay` and those limits.

## Later-stage boundaries

Monthly selection must use only preceding history. Validation open positions
are marked using the last available in-window close, not a future liquidation;
they are not completed trades. Only selection identity and evidence may flow to
the shared OOS account, never validation cash/positions/risk. These integration
rules remain requirements, not implemented Stage 1 behavior.

Approval is still required for allocation, maximum holdings, lookback/warm-up,
exit rules, order priority, proceeds reuse, reservation allowance, universe,
calendar/checkpoint/finalization, costs, rounding, missing data, corporate actions,
valuation, benchmark, candidate catalog, selection policy and account risk policy.
The stage neither tests capital feasibility nor promises 20 stocks/100 trades.
