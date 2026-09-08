"""Synthetic typed execution evidence, not a market-data publication scheduler."""

from dataclasses import asdict, dataclass
from decimal import localcontext

from data.price_policy import provider_price_basis

from .account_models import Order, market_date, session
from .account_policy import AccountError, AccountPolicy, D, M
from .serialization import require_fields, require_hash, require_text


def _evidence_common(value) -> None:
    require_text(value.instrument_id)
    session(value.session)
    if (
        value.provider != "jquants"
        or value.provider_price_basis != provider_price_basis("jquants")
    ):
        raise AccountError("unsupported_execution_price_basis")
    for hashed in (value.snapshot_hash, value.basis_evidence_hash):
        require_hash(hashed)
    if type(value.corporate_action_supported) is not bool or D(value.split_ratio) <= 0:
        raise AccountError("invalid_corporate_action_evidence")
    if value.corporate_action_supported and D(value.split_ratio) != 1:
        raise AccountError("split_adjustment_disabled")
    if value.purpose != "synthetic_test":
        raise AccountError("live_publication_gate_not_implemented")
    if market_date(value.market_available_at) != session(value.session):
        raise AccountError("evidence_session_timestamp_mismatch")


@dataclass(frozen=True, slots=True)
class ExecutionOpenEvidence:
    instrument_id: str
    session: str
    open_price: str
    provider: str
    provider_price_basis: str
    snapshot_hash: str
    basis_evidence_hash: str
    corporate_action_supported: bool
    split_ratio: str
    purpose: str
    market_available_at: str
    tradability: str

    def __post_init__(self) -> None:
        _evidence_common(self)
        if D(self.open_price) <= 0 or self.tradability not in (
            "synthetic_open_executable",
            "no_trade",
        ):
            raise AccountError("invalid_open_evidence")

    @classmethod
    def from_dict(cls, value: dict) -> "ExecutionOpenEvidence":
        require_fields(value, cls)
        return cls(**value)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExecutionMarkEvidence:
    instrument_id: str
    session: str
    mark_price: str | None
    provider: str
    provider_price_basis: str
    snapshot_hash: str
    basis_evidence_hash: str
    corporate_action_supported: bool
    split_ratio: str
    purpose: str
    market_available_at: str
    quality: str

    def __post_init__(self) -> None:
        _evidence_common(self)
        if self.quality not in ("complete", "missing", "stale"):
            raise AccountError("unknown_mark_quality")
        if self.mark_price is not None and D(self.mark_price) <= 0:
            raise AccountError("invalid_mark_price")
        if self.quality == "complete" and self.mark_price is None:
            raise AccountError("complete_mark_without_price")

    @classmethod
    def from_dict(cls, value: dict) -> "ExecutionMarkEvidence":
        require_fields(value, cls)
        return cls(**value)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FillDecision:
    filled: bool
    reason: str | None
    price: str | None
    gross: str
    commission: str
    net_amount: str


def evaluate_fill(
    order: Order,
    evidence: ExecutionOpenEvidence,
    policy: AccountPolicy,
    available_to_order: str,
    buy_enabled: bool,
) -> FillDecision:
    """All-or-reject at already fixed quantity; no external state mutation."""
    with localcontext() as context:
        context.prec = 128
        if type(buy_enabled) is not bool or D(available_to_order) < 0:
            raise AccountError("invalid_fill_account_inputs")
        o = order.value.to_dict()
        req = o["request"]
        if (
            evidence.instrument_id != req["instrument_id"]
            or evidence.session != req["target_session"]
            or evidence.basis_evidence_hash != policy.basis_evidence_hash
        ):
            raise AccountError("order_evidence_mismatch")
        if o["status"] != "pending":
            raise AccountError("terminal_order")

        def reject(reason):
            return FillDecision(False, reason, None, "0", "0", "0")

        if not evidence.corporate_action_supported:
            return reject("unsupported_corporate_action")
        if evidence.tradability == "no_trade":
            return reject("no_trade_evidence")
        if req["side"] == "BUY" and not buy_enabled:
            return reject("buy_risk_stopped")
        multiplier = (
            1 + D(policy.slippage_pct)
            if req["side"] == "BUY"
            else 1 - D(policy.slippage_pct)
        )
        price = policy.price(D(evidence.open_price) * multiplier, req["side"])
        if price <= 0:
            return reject("nonpositive_rounded_price")
        gross, fee = policy.cost(price, o["shares"])
        net = gross + fee if req["side"] == "BUY" else gross - fee
        if net <= 0:
            return reject("nonpositive_fill_amount")
        if req["side"] == "BUY":
            if net > D(o["reservation"]["cash"]):
                return reject("reservation_exceeded")
            if net > D(o["frozen_budget"]):
                return reject("frozen_budget_exceeded")
            if net > D(available_to_order):
                return reject("shared_cash_insufficient")
        return FillDecision(True, None, M(price), M(gross), M(fee), M(net))
