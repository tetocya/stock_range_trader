"""Immutable account inputs and validated state, separate from Phase 1 Portfolio."""

from dataclasses import asdict, dataclass
from datetime import date
from decimal import localcontext
from zoneinfo import ZoneInfo

from .account_policy import AccountError, AccountPolicy, D, quantity
from .serialization import (
    JsonObject,
    parse_time,
    require_fields,
    require_hash,
    require_text,
)


def session(value: str) -> date:
    if type(value) is not str:
        raise AccountError("session_string_required")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise AccountError("invalid_session") from None
    if result.isoformat() != value:
        raise AccountError("noncanonical_session")
    return result


def market_date(value: str) -> date:
    return parse_time(value).astimezone(ZoneInfo("Asia/Tokyo")).date()


@dataclass(frozen=True, slots=True)
class OrderRequest:
    order_id: str
    instrument_id: str
    side: str
    target_session: str
    signal_at: str
    decision_at: str
    reference_session: str
    reference_available_at: str
    reference_price: str
    range_score: str
    candidate_id: str
    entry_config_hash: str
    exit_config_hash: str
    snapshot_hash: str
    lot_evidence_hash: str
    lot_size: int
    requested_shares: int | None

    def __post_init__(self) -> None:
        for value in (self.order_id, self.instrument_id, self.candidate_id):
            require_text(value)
        for value in (
            self.entry_config_hash,
            self.exit_config_hash,
            self.snapshot_hash,
            self.lot_evidence_hash,
        ):
            require_hash(value)
        if (
            self.side not in ("BUY", "SELL")
            or type(self.lot_size) is not int
            or self.lot_size != 100
        ):
            raise AccountError("unsupported_order")
        if D(self.reference_price) <= 0 or not 0 <= D(self.range_score) <= 100:
            raise AccountError("invalid_reference")
        if session(self.target_session) <= market_date(self.decision_at) or session(
            self.reference_session
        ) > market_date(self.decision_at):
            raise AccountError("noncausal_reference_session")
        if (
            not parse_time(self.reference_available_at)
            <= parse_time(self.signal_at)
            <= parse_time(self.decision_at)
        ):
            raise AccountError("noncausal_decision")
        if market_date(self.reference_available_at) < session(self.reference_session):
            raise AccountError("reference_before_session")
        if self.side == "BUY" and self.requested_shares is not None:
            raise AccountError("buy_quantity_is_sized_not_requested")
        if self.side == "SELL":
            quantity(self.requested_shares)

    @classmethod
    def from_dict(cls, value: dict) -> "OrderRequest":
        require_fields(value, cls)
        return cls(**value)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Reservation:
    cash: str
    shares: int
    released: bool

    def __post_init__(self) -> None:
        if D(self.cash) < 0 or type(self.released) is not bool:
            raise AccountError("invalid_reservation")
        quantity(self.shares, zero=True)
        if self.released and (D(self.cash) or self.shares):
            raise AccountError("released_reservation_not_zero")


@dataclass(frozen=True, slots=True)
class Order:
    value: JsonObject

    def __post_init__(self) -> None:
        v = self.value.to_dict()
        if set(v) != {
            "request",
            "shares",
            "frozen_budget",
            "initial_reserved_cash",
            "reservation",
            "decision_equity",
            "equity_at",
            "policy_hash",
            "status",
            "reason",
            "fill",
        }:
            raise AccountError("order_fields_mismatch")
        OrderRequest.from_dict(v["request"])
        quantity(v["shares"], zero=True)
        Reservation(**v["reservation"])
        require_hash(v["policy_hash"])
        parse_time(v["equity_at"])
        for name in ("frozen_budget", "initial_reserved_cash", "decision_equity"):
            if D(v[name]) < 0:
                raise AccountError("negative_order_amount")
        if v["status"] not in ("pending", "filled", "rejected", "canceled"):
            raise AccountError("invalid_order_status")
        if (v["status"] == "pending") == v["reservation"]["released"]:
            raise AccountError("order_reservation_status_mismatch")
        if v["status"] == "pending" and v["shares"] == 0:
            raise AccountError("pending_zero_shares")
        if v["status"] in ("rejected", "canceled"):
            require_text(v["reason"])
        if (v["status"] == "filled") != (v["fill"] is not None):
            raise AccountError("fill_status_mismatch")
        if parse_time(v["equity_at"]) > parse_time(v["request"]["decision_at"]):
            raise AccountError("future_equity_in_order")
        if v["fill"] is not None:
            fill = v["fill"]
            if set(fill) != {
                "price",
                "gross",
                "commission",
                "net_amount",
                "session",
                "snapshot_hash",
            }:
                raise AccountError("fill_fields_mismatch")
            if (
                D(fill["price"]) <= 0
                or min(D(fill["gross"]), D(fill["commission"]), D(fill["net_amount"]))
                < 0
            ):
                raise AccountError("invalid_fill_amount")
            require_hash(fill["snapshot_hash"])
            if fill["session"] != v["request"]["target_session"]:
                raise AccountError("fill_session_mismatch")


@dataclass(frozen=True, slots=True)
class Position:
    value: JsonObject

    def __post_init__(self) -> None:
        v = self.value.to_dict()
        if set(v) != {
            "instrument_id",
            "position_id",
            "episode_id",
            "entry_order_id",
            "shares",
            "entry_gross",
            "entry_commission",
            "cost_basis",
            "candidate_id",
            "entry_config_hash",
            "exit_config_hash",
            "realized_profit",
        }:
            raise AccountError("position_fields_mismatch")
        for name in (
            "instrument_id",
            "position_id",
            "episode_id",
            "entry_order_id",
            "candidate_id",
        ):
            require_text(v[name])
        quantity(v["shares"])
        for name in ("entry_config_hash", "exit_config_hash"):
            require_hash(v[name])
        if min(D(v["entry_gross"]), D(v["entry_commission"]), D(v["cost_basis"])) < 0:
            raise AccountError("negative_cost_basis")
        if D(v["cost_basis"]) != D(v["entry_gross"]) + D(v["entry_commission"]):
            raise AccountError("cost_basis_mismatch")
        D(v["realized_profit"])


@dataclass(frozen=True, slots=True)
class RiskState:
    value: JsonObject

    def __post_init__(self) -> None:
        v = self.value.to_dict()
        if set(v) != {
            "buy_enabled",
            "halted",
            "stop_reasons",
            "last_equity",
            "equity_at",
            "high_water",
            "drawdown",
        }:
            raise AccountError("risk_fields_mismatch")
        if (
            type(v["buy_enabled"]) is not bool
            or type(v["halted"]) is not bool
            or type(v["stop_reasons"]) is not list
        ):
            raise AccountError("risk_types_invalid")
        for reason in v["stop_reasons"]:
            require_text(reason)
        if v["stop_reasons"] and v["buy_enabled"]:
            raise AccountError("risk_stop_not_enforced")
        if v["halted"] and (v["buy_enabled"] or not v["stop_reasons"]):
            raise AccountError("accounting_halt_not_enforced")
        if (
            min(D(v["last_equity"]), D(v["high_water"])) < 0
            or not 0 <= D(v["drawdown"]) <= 1
        ):
            raise AccountError("invalid_risk_value")
        parse_time(v["equity_at"])


@dataclass(frozen=True, slots=True)
class Valuation:
    value: JsonObject

    def __post_init__(self) -> None:
        v = self.value.to_dict()
        if set(v) != {
            "session",
            "complete",
            "marks",
            "display_equity",
            "unrealized_profit",
            "reasons",
        }:
            raise AccountError("valuation_fields_mismatch")
        session(v["session"])
        if (
            type(v["complete"]) is not bool
            or type(v["marks"]) is not dict
            or type(v["reasons"]) is not list
        ):
            raise AccountError("invalid_valuation")
        for name in ("display_equity", "unrealized_profit"):
            if v[name] is not None:
                D(v[name])
        if v["complete"] and (v["display_equity"] is None or v["reasons"]):
            raise AccountError("incomplete_valuation_claimed_complete")


@dataclass(frozen=True, slots=True)
class SharedAccountState:
    value: JsonObject

    @classmethod
    def initial(cls, policy: AccountPolicy, at: str) -> "SharedAccountState":
        parse_time(at)
        return cls(
            JsonObject.from_value(
                dict(
                    schema_version="shared-account-1",
                    policy=asdict(policy),
                    cash=policy.initial_capital,
                    positions={},
                    orders={},
                    proceeds_holds={},
                    realized_profit="0",
                    episodes={},
                    operations={},
                    executed_sessions=[],
                    closed_sessions=[],
                    risk=dict(
                        buy_enabled=True,
                        halted=False,
                        stop_reasons=[],
                        last_equity=policy.initial_capital,
                        equity_at=at,
                        high_water=policy.initial_capital,
                        drawdown="0",
                    ),
                    valuation=dict(
                        session=market_date(at).isoformat(),
                        complete=True,
                        marks={},
                        display_equity=policy.initial_capital,
                        unrealized_profit="0",
                        reasons=[],
                    ),
                )
            )
        )

    def __post_init__(self) -> None:
        if type(self.value) is not JsonObject:
            raise AccountError("immutable_account_required")
        with localcontext() as context:
            context.prec = 128
            self._validate()

    def _validate(self) -> None:
        v = self.value.to_dict()
        if (
            set(v)
            != {
                "schema_version",
                "policy",
                "cash",
                "positions",
                "orders",
                "proceeds_holds",
                "realized_profit",
                "episodes",
                "operations",
                "executed_sessions",
                "closed_sessions",
                "risk",
                "valuation",
            }
            or v["schema_version"] != "shared-account-1"
        ):
            raise AccountError("account_schema_mismatch")
        policy = AccountPolicy.from_dict(v["policy"])
        for name in ("positions", "orders", "proceeds_holds", "episodes", "operations"):
            if type(v[name]) is not dict:
                raise AccountError("account_collection_invalid")
        for name in ("executed_sessions", "closed_sessions"):
            if type(v[name]) is not list or len(set(v[name])) != len(v[name]):
                raise AccountError("duplicate_session")
            for day in v[name]:
                session(day)
        cash = D(v["cash"])
        reserved = D("0")
        slots = set(v["positions"])
        buy_instruments = set()
        sell_locks = {}
        for instrument, pos in v["positions"].items():
            Position(JsonObject.from_value(pos))
            if pos["instrument_id"] != instrument:
                raise AccountError("position_key_mismatch")
        for order_id, order in v["orders"].items():
            Order(JsonObject.from_value(order))
            req = order["request"]
            if req["order_id"] != order_id or order["policy_hash"] != policy.sha256:
                raise AccountError("order_identity_mismatch")
            if req["side"] == "BUY" and order["shares"]:
                if D(order["frozen_budget"]) != policy.money(
                    D(order["decision_equity"]) * D(policy.max_position_pct),
                    policy.budget_rounding,
                ):
                    raise AccountError("frozen_budget_mismatch")
                if (
                    not 0
                    < D(order["initial_reserved_cash"])
                    <= D(order["frozen_budget"])
                ):
                    raise AccountError("original_reservation_mismatch")
            if order["fill"] is not None:
                fill = order["fill"]
                gross, fee = policy.cost(D(fill["price"]), order["shares"])
                net = gross + fee if req["side"] == "BUY" else gross - fee
                if (gross, fee, net) != (
                    D(fill["gross"]),
                    D(fill["commission"]),
                    D(fill["net_amount"]),
                ):
                    raise AccountError("fill_accounting_mismatch")
                if req["side"] == "BUY" and net > min(
                    D(order["initial_reserved_cash"]), D(order["frozen_budget"])
                ):
                    raise AccountError("fill_exceeds_frozen_limits")
            if order["status"] != "pending":
                continue
            instrument = req["instrument_id"]
            if req["side"] == "BUY":
                if (
                    instrument in slots
                    or instrument in buy_instruments
                    or order["reservation"]["shares"]
                ):
                    raise AccountError("duplicate_buy_exposure")
                buy_instruments.add(instrument)
                reserved += D(order["reservation"]["cash"])
                if not 0 < D(order["reservation"]["cash"]) <= D(order["frozen_budget"]):
                    raise AccountError("reservation_budget_mismatch")
                if order["reservation"]["cash"] != order["initial_reserved_cash"]:
                    raise AccountError("pending_reservation_changed")
            else:
                if instrument in sell_locks or instrument not in v["positions"]:
                    raise AccountError("duplicate_or_unheld_sell")
                sell_locks[instrument] = order["reservation"]["shares"]
                if (
                    sell_locks[instrument] != v["positions"][instrument]["shares"]
                    or sell_locks[instrument] != order["shares"]
                    or D(order["reservation"]["cash"])
                ):
                    raise AccountError("sell_lock_mismatch")
        for hold in v["proceeds_holds"].values():
            if (
                set(hold) != {"amount", "sale_session", "sale_at"}
                or D(hold["amount"]) < 0
            ):
                raise AccountError("invalid_proceeds_hold")
            session(hold["sale_session"])
            parse_time(hold["sale_at"])
            reserved += D(hold["amount"])
        if (
            cash < 0
            or reserved < 0
            or cash < reserved
            or len(slots | buy_instruments) > policy.max_positions
        ):
            raise AccountError("cash_or_slot_invariant")
        RiskState(JsonObject.from_value(v["risk"]))
        Valuation(JsonObject.from_value(v["valuation"]))
        val = v["valuation"]
        if val["display_equity"] is not None:
            if any(
                instrument not in val["marks"]
                or val["marks"][instrument]["price"] is None
                for instrument in v["positions"]
            ):
                raise AccountError("missing_position_in_display_equity")
            marked_value = sum(
                (
                    pos["shares"] * D(val["marks"][instrument]["price"])
                    for instrument, pos in v["positions"].items()
                ),
                D("0"),
            )
            if D(val["display_equity"]) != cash + marked_value:
                raise AccountError("equity_accounting_mismatch")
            if D(val["unrealized_profit"]) != marked_value - sum(
                (D(pos["cost_basis"]) for pos in v["positions"].values()), D("0")
            ):
                raise AccountError("unrealized_profit_mismatch")
        if val["complete"]:
            if any(
                val["marks"][instrument]["quality"] != "complete"
                or val["marks"][instrument]["session"] != val["session"]
                for instrument in v["positions"]
            ):
                raise AccountError("stale_mark_claimed_complete")
            if val["display_equity"] != v["risk"]["last_equity"]:
                raise AccountError("confirmed_equity_mismatch")
        if D(v["realized_profit"]) != sum(
            (D(episode["net_profit"]) for episode in v["episodes"].values()), D("0")
        ):
            raise AccountError("realized_profit_mismatch")
        # Book-value conservation, independent of marks or reservations.
        if cash + sum(
            (D(pos["cost_basis"]) for pos in v["positions"].values()), D("0")
        ) != D(policy.initial_capital) + D(v["realized_profit"]):
            raise AccountError("accounting_conservation_failed")

    @property
    def reserved_cash(self):
        with localcontext() as context:
            context.prec = 128
            v = self.value.to_dict()
            return sum(
                (D(o["reservation"]["cash"]) for o in v["orders"].values()), D("0")
            ) + sum((D(h["amount"]) for h in v["proceeds_holds"].values()), D("0"))

    @property
    def available_cash(self):
        with localcontext() as context:
            context.prec = 128
            return D(self.value.to_dict()["cash"]) - self.reserved_cash

    def to_dict(self) -> dict:
        return self.value.to_dict()
