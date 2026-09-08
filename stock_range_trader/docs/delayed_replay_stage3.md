# Delayed replay Stage 3: synthetic shared account

Status: DRAFT / NOT REGISTERED. This is an offline shared-account implementation,
not authorization to start OOS. No real market/Broker API or live test is used.

## Compatibility and scope

The existing Phase 1–3 Engine/Portfolio, settings, defaults and result schemas,
and Stage 1/2 code are unchanged. The new reducer is not a loop over independent
symbol backtests. It stores all instruments, reservations and risk in one Stage 2
stream. Stage 1's previous-session-only `history()` is unchanged.

No monthly selector, signal generator, replay scheduler, real price-publication
gate, checkpoint judge, benchmark, registration manifest, corporate-action
adjustment, dividend/tax accounting, partial fills or limit orders are implemented.
There is no deposit/withdrawal command beyond the initial JPY 200,000 state.

## Interfaces and explicit policy

- `AccountPolicy`: every field is required; unknown/missing modes are rejected.
- `OrderRequest`, `Order`, `Reservation`, `Position`, `RiskState`, `Valuation`,
  `SharedAccountState`: immutable inputs/state, with detached JSON exports.
- `size_buy(reference_price, decision_equity, available_cash, policy)`: pure
  previous-information sizing, with no next-open/high/low/close/volume parameter.
- `ExecutionOpenEvidence`, `ExecutionMarkEvidence`: minimal synthetic execution
  prices, price-basis/provenance, session and availability evidence.
- `evaluate_fill(...)`: fixed-quantity all-or-reject calculation.
- `AccountReducer`: pure state transition using Stage 2's reducer contract.
- `AccountService`: builds audit commands and delegates atomic commits to
  `EventStore`; it is not a runner or a registration service.

Account policies are currently explicitly labelled `purpose=synthetic_test`.
The schema supports only JPY 200,000 initial capital, long-only 100-share lots,
one Position per instrument, no additional BUY and full EXIT. Required modes:

| Axis | Supported explicit value |
| --- | --- |
| Priority | `sell_then_score_desc_instrument` |
| Sale proceeds | `hold_until_later_decision_session` |
| Fill | `all_or_reject` |
| Position | `single_position_full_exit` |
| Expiry | `explicit_target_session_only` |
| Cost | `proportional` |
| Dividends | `excluded` |

Supporting these modes is **not** approving them for the OOS protocol. Allocation,
maximum positions, commission, slippage, reservation buffer, money/price quantum,
rounding and price-basis evidence hash are all explicit. No 10%/5-position or cost
defaults are added to production code. Tests explicitly choose synthetic values.
Stage 1 unresolved `None` values and `validate_for_execution()` semantics remain
unchanged. No DD threshold or new PASS/FAIL rule is introduced.

## Exact decimal and rounding contract

Price/money/rate inputs are normalized finite decimal **strings**, not float,
bool or implicitly converted numeric objects. Input magnitudes are bounded to
18 integer and 12 fractional digits. Quantities are integer multiples of 100.
Pure calculations use a local 128-digit Decimal context. Persistent values use
Stage 2 canonical JSON with decimal strings. Descriptive DD is recorded to 12
fractional digits and is not used as a new stop condition.

The supplied synthetic price quantum is not a verified exchange tick table.
Supported rounding names are `floor`, `ceiling`, `half_even`. The policy requires
individual choices for BUY price, SELL price, fee, reservation, final amount and
budget. Budget rounding explicitly supports `floor` only. Rounding is performed
to an actual multiple of the quantum, not just to its number of decimal places.

Sizing sequence:

1. Budget = floor-money(last confirmed equity × max_position_pct).
2. Reservation unit price = BUY-price-round(reference × (1+slippage) × (1+buffer)).
3. Gross = amount-round(unit price × shares).
4. Fee = fee-round(gross × commission_rate).
5. Reservation = reservation-round(gross + fee).
6. Choose the largest 100-share multiple within both budget and available cash.

Binary search is bounded; there is no upward lot rounding. Zero quantity is a
reasoned rejection. Price 300/100/200 under the explicit synthetic 10%, no-cost
policy yields 0/200/100 shares respectively; positive costs can make 200 unaffordable.
Neither that example nor account availability guarantees 20 stocks/100 episodes.

At fill, only open × (1±slippage) is price-rounded: the buffer is not an actual
fee. Gross and commission use the same amount/fee rounding as above. The rate is
strictly below one; computing fees from rounded gross and the same money quantum
prevents a negative SELL net from fee rounding. Nonpositive rounded executions
are rejected. No hypothetical exit fee is charged on marks.

## Shared-account invariants

```text
available_cash = cash − reserved_cash
reserved_cash  = active BUY cash reservations + proceeds holds
equity        = cash + sum(shares × execution mark)
cash + open cost bases = 200000 + cumulative realized net profit
```

Cash/available/reserved are nonnegative. Reservations do not debit cash or lower
Equity. SELL share locks equal the full held quantity; double locks are rejected.
Slots are the union of held instruments and pending BUY instruments. A planned
SELL does not release a slot. Additional BUY is unsupported, not silently netted.

Entry cost basis is BUY gross plus entry commission. On full SELL, net profit is
net proceeds minus that basis. Slippage is already in price and is never deducted
again. Original reservation (`initial_reserved_cash`) and frozen budget remain on
the order after its active reservation is released. Position/episode IDs derive
from the unique entry order ID; no random IDs are generated. Entry Candidate,
config/EXIT hashes and all original order references remain immutable.

The hand fixture is: BUY 100 shares at 100 with fee 0.001 → cash 189990 and basis
10010; mark 110 → equity 200990, unrealized 990; SELL 110 → cash 200979, realized
979. A 10989 proceeds hold changes available cash, not equity.

## Commands and state machine

All commands require `operation_id`; use `AccountService.command()` with explicit
audit event ID, market/replay timestamps and input hashes. Event types:

| Type | Additional payload fields | Effect |
| --- | --- | --- |
| `account.submit` | requests | Sort fixed batch, accept/reserve or record rejection |
| `account.execute` | session, order_ids, evidence | Process the frozen pending set once |
| `account.cancel` | order_id, reason=`user_cancel` | Cancel pending order and release reservation |
| `account.finalize` | session | Explicitly end session; cancel pending orders without evidence |
| `account.release` | decision_session, hold_ids, reason=`next_eligible_decision` | Release eligible sale proceeds |
| `account.mark` | session, marks | Publish one atomic account valuation batch |
| `account.stop` | reason | Explicit persistent BUY stop; no automatic resume |

Orders move `pending → filled/rejected/canceled`. Invalid schemas/duplicate IDs
reject the command without a transition; normal business refusals (no lot budget,
slots, additional BUY, partial/unheld/double SELL) produce rejected orders with
stable reasons. Terminal orders cannot change; immutable audit retries still work.

Acceptance batches must have one decision timestamp and no duplicate instrument
or order ID. They are sorted SELL first, then BUY Range Score descending and stable
instrument ID ascending. Quantities/budgets/reservations are fixed before the target
session. SELL retains entry provenance; changed EXIT/Candidate references reject.

Execution requires exactly the pending order-ID set already stored for the session.
No new order or extra quantity is added when another fill releases funds or slots.
BUY net must fit its original reservation, frozen budget and cash excluding other
holds; excess is never topped up from free cash. Gap up rejects the full order;
gap down does not increase quantity. Known BUY Risk stops also reject pending BUYs.
All SELL net receipts are cash plus a proceeds hold. Release is a separate event
on a strictly later, explicitly supplied decision session with complete valuation.
This is not brokerage settlement simulation. Release does not itself permit BUY.

No evidence leaves an order pending; only explicit session finalization marks it
`canceled/no_execution_evidence`. It cannot roll to a later session. One execution
batch and one valuation batch per session are supported. An incomplete valuation
is retained, not silently revised by a second same-session batch. Later publication
and amendment/retry scheduling require a future contract, not an implicit repair.

## Evidence and causality limits

Order requests contain reference price/session/availability, signal/decision time,
next target session, lot size and lot-evidence hash. No full daily bar is accepted.
The target session follows the decision date; evidence timestamps are validated.
Confirmed equity cannot be from the future. When positions exist, its valuation
session must match the requested reference session; otherwise a new BUY is refused.
A real exchange calendar and proof that the supplied session is the actual next
eligible session remain responsibilities of the future publication/scheduling layer.

Open evidence is instrument/session-specific and contains only Open, provider
basis, snapshot/evidence hashes, corporate-action status and explicit tradability.
Yfinance, unknown basis/provider, mismatched references/session/instrument and
unverified split adjustment are refused before arithmetic. A J-Quants name alone
is insufficient: the synthetic evidence purpose and policy-pinned basis-evidence
hash must match. That hash is a reference, not independent proof of real prices.
The execution command's market timestamp must equal the evidence's Open timestamp,
preventing an after-close decision from retroactively processing the earlier Open.
The actual wall replay timestamp remains separate and may be months later.

`no_trade` gives no fill. Today's final volume is never passed to sizing/priority,
and positive daily volume is not proof of executable opening liquidity. Constructing
no-trade evidence from real zero-volume bars would be retrospective fill validation
and is **not implemented**. Close/high/low/final volume are not Open evidence fields.
Same-day post-close signal publication is also not implemented or enabled in Stage 1.

For deterministic recovery, account event payloads retain the **minimal synthetic
execution/mark scalar**, explicit policy in initial state, and immutable hashes.
This is the Stage 3 replay input contract, not a copy of a raw market dataset.
Full OHLCV series, API responses, credentials, paths or exception text are not
automatically logged. Resolving/verifying real referenced artifacts is not added.

## Valuation, Risk and persistence

Marks are execution-lane evidence, not Signal prices. All held instruments are
evaluated as one batch before updating equity/high-water. Missing/stale evidence
keeps positions and prior display marks, sets `complete=false` and reasons, and
does not update confirmed equity/high-water or allow new sizing. Missing price
with no prior mark yields null display, never a fabricated zero-valued position.
Unsupported corporate action on a held instrument halts accounting, keeps the
position and disables further fills; no inferred share/cash adjustment or liquidation.

Risk stops persist across sessions, months and restart. Ordinary BUY stops still
permit full SELL; accounting-halted state is stricter. DD is descriptive only.
There is no month reset, no risk auto-resume, and no checkpoint PASS/FAIL logic.

`AccountReducer` is pure, operates on detached state, and validates conservation,
cash/slots/locks, budget, fill costs and equity. Stage 2 commits event + full account
state/head + optional snapshot in one transaction. Separate event IDs with the same
business operation are no-ops only when canonical meaning matches; conflicts reject.
Orders, completed execution/valuation sessions and released hold IDs independently
prevent duplicate work under a fresh operation ID. Old policy is never replaced.

Stage 2's full genesis replay and snapshot-tail verification run with the Production
account reducer. Tests cover pending BUY/SELL, open position, proceeds hold and Risk
stop recovery, commit-before failure and commit-after response loss. Existing Stage 2
subprocess termination tests remain part of the full suite. Power loss and coherent
DB rollback without an external anchor retain Stage 2's documented limitations.
