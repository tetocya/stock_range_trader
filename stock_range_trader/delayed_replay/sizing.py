"""Pure prior-information sizing; no next open or daily-bar input."""

from dataclasses import dataclass
from decimal import localcontext

from .account_policy import AccountError, AccountPolicy, D, M, quantity


@dataclass(frozen=True, slots=True)
class SizingResult:
    shares: int
    frozen_budget: str
    reserved_cash: str

    def __post_init__(self):
        quantity(self.shares, zero=True)
        if not 0 <= D(self.reserved_cash) <= D(self.frozen_budget) or (
            self.shares == 0 and D(self.reserved_cash)
        ):
            raise AccountError("invalid_sizing_result")


def size_buy(
    reference_price: str,
    decision_equity: str,
    available_cash: str,
    policy: AccountPolicy,
) -> SizingResult:
    with localcontext() as context:
        context.prec = 128
        ref, equity, available = (
            D(reference_price),
            D(decision_equity),
            D(available_cash),
        )
        if min(ref, equity, available) < 0 or ref == 0:
            raise ValueError("invalid_sizing_input")
        budget = policy.money(
            equity * D(policy.max_position_pct), policy.budget_rounding
        )
        unit = policy.price(
            ref * (1 + D(policy.slippage_pct)) * (1 + D(policy.reservation_buffer_pct)),
            "BUY",
        )
        if unit <= 0:
            return SizingResult(0, M(budget), "0")
        cap = min(budget, available)

        def reserve(k):
            gross, fee = policy.cost(unit, k * 100)
            return policy.money(gross + fee, policy.reservation_rounding)

        # Include one money quantum for down-rounded gross; binary search is
        # bounded and avoids a loop over individual shares.
        low, high = (
            0,
            min(int((cap + D(policy.money_quantum)) / (unit * 100)) + 1, 10**10 - 1),
        )
        while low < high:
            mid = (low + high + 1) // 2
            if reserve(mid) <= cap:
                low = mid
            else:
                high = mid - 1
        amount = reserve(low)
        if amount <= 0:
            return SizingResult(0, M(budget), "0")
        return SizingResult(low * 100, M(budget), M(amount))
