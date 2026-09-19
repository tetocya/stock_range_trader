"""Independent Decimal audit, not a call to sizing/clearing/account policies."""

import re
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext

from .reader import ObservationError

VERIFICATION_MODEL = "order-audit-daily-open-proxy-v1"
SUPPORTED_TERM_FIELDS = frozenset(
    "initial_capital lot_size max_position_pct max_positions commission_rate slippage_pct "
    "reservation_buffer_pct price_quantum money_quantum buy_price_rounding sell_price_rounding "
    "fee_rounding reservation_rounding amount_rounding budget_rounding priority_mode proceeds_mode "
    "fill_mode position_mode expiry_mode cost_model dividend_policy basis_evidence_hash".split()
)
ROUNDINGS = {
    "ceiling": ROUND_CEILING,
    "floor": ROUND_FLOOR,
    "half_even": ROUND_HALF_EVEN,
}


def number(value):
    if type(value) is not str or not re.fullmatch(r"-?\d{1,18}(?:\.\d{1,12})?", value):
        raise ObservationError("invalid_exact_decimal")
    return Decimal(value)


def text(value):
    if value is None:
        return None
    if value == 0:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


class OrderArithmeticVerifier:
    def verify(self, frozen, terms, model_id, opening, saved_resolution, status):
        with localcontext() as context:
            context.prec = 128
            return self._verify(
                frozen, terms, model_id, opening, saved_resolution, status
            )

    def _verify(self, o, t, model_id, opening, resolution, status):
        result = dict(
            model=VERIFICATION_MODEL,
            status="verified",
            mismatches=[],
            missing_evidence=[],
            calculated={},
            constraint_diagnosis="not_applicable",
        )
        required = (
            "price_quantum",
            "money_quantum",
            "commission_rate",
            "slippage_pct",
            "reservation_buffer_pct",
            "max_position_pct",
            "buy_price_rounding",
            "sell_price_rounding",
            "fee_rounding",
            "amount_rounding",
            "budget_rounding",
            "reservation_rounding",
            "cost_model",
            "fill_mode",
            "proceeds_mode",
        )
        if any(k not in t or t[k] is None for k in required):
            result.update(
                status="insufficient_evidence", missing_evidence=["fixed_cost_terms"]
            )
            return result
        if (
            model_id != "daily_open_proxy_v1"
            or set(t) != SUPPORTED_TERM_FIELDS
            or type(t["lot_size"]) is not int
            or t["lot_size"] != 100
            or t["cost_model"] != "proportional"
            or t["fill_mode"] != "all_or_reject"
            or t["proceeds_mode"] != "hold_until_later_decision_session"
            or any(t[k] not in ROUNDINGS for k in required if k.endswith("rounding"))
        ):
            result["status"] = "unsupported_verification_model"
            return result
        q = o["shares"]
        if type(q) is not int or q <= 0 or q % 100 or o["side"] not in ("BUY", "SELL"):
            raise ObservationError("invalid_fixed_quantity_or_side")
        price_q, money_q = number(t["price_quantum"]), number(t["money_quantum"])
        if min(price_q, money_q) <= 0:
            raise ObservationError("invalid_quantum")
        fee, slip, buffer, ratio = (
            number(t[k])
            for k in (
                "commission_rate",
                "slippage_pct",
                "reservation_buffer_pct",
                "max_position_pct",
            )
        )
        if not (0 <= fee < 1 and 0 <= slip < 1 and 0 <= buffer < 1 and 0 < ratio <= 1):
            raise ObservationError("invalid_fixed_cost_terms")

        def rounded(value, quantum, mode):
            return (value / quantum).to_integral_value(
                rounding=ROUNDINGS[mode]
            ) * quantum

        def money(value, field="amount_rounding"):
            return rounded(value, money_q, t[field])

        def unit(value, side):
            return rounded(value, price_q, t[side.lower() + "_price_rounding"])

        def compare(name, actual, expected):
            if actual is None or number(actual) != expected:
                result["mismatches"].append(name)

        c = result["calculated"]
        if o["side"] == "BUY":
            reservation_price = unit(
                number(o["reference_price"]) * (1 + slip) * (1 + buffer), "BUY"
            )
            gross = money(reservation_price * q)
            commission = money(gross * fee, "fee_rounding")
            reserve = money(gross + commission, "reservation_rounding")
            budget = money(number(o["decision_equity"]) * ratio, "budget_rounding")
            c.update(
                reservation_unit=text(reservation_price),
                reservation_gross=text(gross),
                reservation_commission=text(commission),
                reservation_total=text(reserve),
                equity_allocation_budget=text(budget),
            )
            compare("frozen_reserved", o["reserved"], reserve)
            compare("frozen_budget", o["budget"], budget)
        else:
            c.update(
                reservation_unit=None,
                reservation_gross=None,
                reservation_commission=None,
                reservation_total=None,
                equity_allocation_budget=None,
            )
            compare("sell_cash_reservation", o["reserved"], Decimal(0))
        if opening is None:
            result["missing_evidence"].append("target_open")
        else:
            price = unit(
                number(opening) * (1 + slip if o["side"] == "BUY" else 1 - slip),
                o["side"],
            )
            gross = money(price * q)
            commission = money(gross * fee, "fee_rounding")
            net = gross + commission if o["side"] == "BUY" else gross - commission
            c.update(
                execution_unit=text(price),
                execution_gross=text(gross),
                hypothetical_commission=text(commission),
                required_amount=text(net) if o["side"] == "BUY" else None,
                sell_net_proceeds=text(net) if o["side"] == "SELL" else None,
            )
            if o["side"] == "BUY":
                reserve_gap, budget_margin = (
                    net - number(o["reserved"]),
                    number(o["budget"]) - net,
                )
                c.update(
                    required_minus_reserved=text(reserve_gap),
                    budget_minus_required=text(budget_margin),
                )
                result["constraint_diagnosis"] = (
                    "both_exceeded"
                    if reserve_gap > 0 and budget_margin < 0
                    else "reservation_only_exceeded"
                    if reserve_gap > 0
                    else "budget_only_exceeded"
                    if budget_margin < 0
                    else "within_both_limits"
                )
            if resolution is not None:
                # no-trade/volume rejection has no priced resolution in v1.
                priced = resolution.get("reason") in (
                    None,
                    "frozen_reservation_or_budget_exceeded",
                    "nonpositive_model_amount",
                )
                if priced:
                    for name, expected in (
                        ("price", price),
                        ("gross", gross),
                        ("commission", commission),
                        ("net", net),
                    ):
                        compare("resolution_" + name, resolution.get(name), expected)
                if resolution.get("shares") != q:
                    result["mismatches"].append("resolution_shares")
                if (status == "filled") != (resolution.get("filled") is True):
                    result["mismatches"].append("status_fill_flag")
                if (
                    o["side"] == "BUY"
                    and result["constraint_diagnosis"] != "within_both_limits"
                    and status == "filled"
                ):
                    result["mismatches"].append("filled_despite_frozen_constraint")
                if (
                    resolution.get("reason") == "frozen_reservation_or_budget_exceeded"
                    and result["constraint_diagnosis"] == "within_both_limits"
                ):
                    result["mismatches"].append("stored_constraint_reason")
        if status in ("filled", "rejected") and resolution is None:
            result["missing_evidence"].append("saved_resolution")
        if resolution is not None:
            if resolution.get("order_id") != o["order_id"]:
                result["mismatches"].append("resolution_order_id")
            if type(resolution.get("filled")) is not bool:
                result["mismatches"].append("resolution_fill_flag_type")
            if resolution.get("actual_trade_at") is not None:
                result["mismatches"].append("proxy_actual_trade_at_not_null")
            if resolution.get("reason") not in (
                None,
                "frozen_reservation_or_budget_exceeded",
                "nonpositive_model_amount",
                "no_trade",
                "instrument_batch_volume_exceeded",
            ):
                result["missing_evidence"].append("unsupported_saved_resolution_reason")
        result["status"] = (
            "mismatch"
            if result["mismatches"]
            else "insufficient_evidence"
            if result["missing_evidence"]
            else "verified"
        )
        return result
