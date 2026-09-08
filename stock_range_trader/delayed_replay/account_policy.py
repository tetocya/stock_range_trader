"""Explicit synthetic account policies; no operational defaults or float coercion."""

import re
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from .audit_errors import InvalidEvent
from .serialization import decimal_text, digest, require_fields, require_hash


class AccountError(InvalidEvent):
    """Invalid account contract, not a protocol outcome classifier."""


def D(value: str) -> Decimal:
    if type(value) is not str or not re.fullmatch(r"-?\d{1,18}(?:\.\d{1,12})?", value):
        raise AccountError("exact_decimal_string_required")
    if decimal_text(value) != value:
        raise AccountError("normalized_decimal_required")
    return Decimal(value)


def M(value: Decimal) -> str:
    return decimal_text(format(value, "f"))


def quantity(value: int, *, zero: bool = False) -> None:
    if (
        type(value) is not int
        or value < (0 if zero else 100)
        or value % 100
        or value >= 10**12
    ):
        raise AccountError("invalid_100_share_quantity")


ROUNDINGS = {
    "floor": ROUND_FLOOR,
    "ceiling": ROUND_CEILING,
    "half_even": ROUND_HALF_EVEN,
}


def rounded(value: Decimal, quantum: str, mode: str) -> Decimal:
    q = D(quantum)
    return (value / q).to_integral_value(rounding=ROUNDINGS[mode]) * q


@dataclass(frozen=True, slots=True)
class AccountPolicy:
    purpose: str
    initial_capital: str
    lot_size: int
    max_position_pct: str
    max_positions: int
    commission_rate: str
    slippage_pct: str
    reservation_buffer_pct: str
    price_quantum: str
    money_quantum: str
    buy_price_rounding: str
    sell_price_rounding: str
    fee_rounding: str
    reservation_rounding: str
    amount_rounding: str
    budget_rounding: str
    priority_mode: str
    proceeds_mode: str
    fill_mode: str
    position_mode: str
    expiry_mode: str
    cost_model: str
    dividend_policy: str
    basis_evidence_hash: str

    def __post_init__(self) -> None:
        fixed = dict(
            purpose="synthetic_test",
            initial_capital="200000",
            lot_size=100,
            priority_mode="sell_then_score_desc_instrument",
            proceeds_mode="hold_until_later_decision_session",
            fill_mode="all_or_reject",
            position_mode="single_position_full_exit",
            expiry_mode="explicit_target_session_only",
            cost_model="proportional",
            dividend_policy="excluded",
            budget_rounding="floor",
        )
        for name, expected in fixed.items():
            value = getattr(self, name)
            if type(value) is not type(expected) or value != expected:
                raise AccountError("unsupported_account_policy")
        if type(self.max_positions) is not int or not 1 <= self.max_positions <= 100000:
            raise AccountError("invalid_max_positions")
        if not 0 < D(self.max_position_pct) <= 1:
            raise AccountError("invalid_allocation_ratio")
        for value in (
            self.commission_rate,
            self.slippage_pct,
            self.reservation_buffer_pct,
        ):
            if not 0 <= D(value) < 1:
                raise AccountError("invalid_cost_ratio")
        for value in (self.price_quantum, self.money_quantum):
            if D(value) <= 0:
                raise AccountError("invalid_quantum")
        for value in (
            self.buy_price_rounding,
            self.sell_price_rounding,
            self.fee_rounding,
            self.reservation_rounding,
            self.amount_rounding,
        ):
            if value not in ROUNDINGS:
                raise AccountError("unknown_rounding")
        require_hash(self.basis_evidence_hash)

    @property
    def sha256(self) -> str:
        return digest(asdict(self))

    @classmethod
    def from_dict(cls, value: dict) -> "AccountPolicy":
        require_fields(value, cls)
        return cls(**value)

    def money(self, value: Decimal, mode: str | None = None) -> Decimal:
        return rounded(
            value, self.money_quantum, self.amount_rounding if mode is None else mode
        )

    def price(self, value: Decimal, side: str) -> Decimal:
        return rounded(
            value,
            self.price_quantum,
            self.buy_price_rounding if side == "BUY" else self.sell_price_rounding,
        )

    def cost(self, price: Decimal, shares: int) -> tuple[Decimal, Decimal]:
        gross = self.money(price * shares)
        fee = self.money(gross * D(self.commission_rate), self.fee_rounding)
        return gross, fee
