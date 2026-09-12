"""Whole immutable batch checked BEFORE any Fill is committed."""

from dataclasses import dataclass
from datetime import date
from decimal import localcontext

from delayed_replay.account_policy import D, M, quantity
from delayed_replay.serialization import JsonObject, digest, require_hash, require_text
from delayed_replay.validation import ReplayContractError

from .input_adapter import validate_row
from .policy import CAPABILITY


@dataclass(frozen=True)
class FrozenProxyOrder:
    value: JsonObject

    def __post_init__(self):
        o = self.value.to_dict()
        if set(o) != {
            "order_id",
            "episode_id",
            "symbol",
            "side",
            "target",
            "decision_session",
            "decision_phase",
            "candidate_id",
            "config_hash",
            "shares",
            "budget",
            "reserved",
            "reference_price",
            "decision_equity",
            "equity_at",
            "policy_hash",
            "rank",
            "score",
        }:
            raise ReplayContractError("proxy_order_schema")
        quantity(o["shares"])
        for k in ("order_id", "episode_id", "symbol", "candidate_id", "equity_at"):
            require_text(o[k])
        for k in ("decision_session", "target"):
            if type(o[k]) is not str or date.fromisoformat(o[k]).isoformat() != o[k]:
                raise ReplayContractError("proxy_order_date")
        if (
            o["side"] not in ("BUY", "SELL")
            or o["decision_session"] >= o["target"]
            or o["decision_phase"] != "after_close"
        ):
            raise ReplayContractError("proxy_order_information_boundary")
        for k in ("policy_hash", "config_hash"):
            require_hash(o[k])
        for k in ("budget", "reserved", "reference_price", "decision_equity", "score"):
            if D(o[k]) < 0:
                raise ReplayContractError("proxy_order_number")
        if type(o["rank"]) is not int or o["rank"] < 0:
            raise ReplayContractError("proxy_order_rank")
        if D(o["reference_price"]) <= 0 or D(o["reserved"]) > D(o["budget"]):
            raise ReplayContractError("proxy_order_reference_or_reservation")
        if o["side"] == "SELL" and (o["reserved"] != "0" or o["budget"] != "0"):
            raise ReplayContractError("proxy_sell_cash_reservation")


@dataclass(frozen=True)
class ProxyResolution:
    value: JsonObject

    def __post_init__(self):
        p = self.value.to_dict()
        if (
            set(p)
            != {"schema", "capability", "batch_hash", "status", "reason", "results"}
            or p["capability"] != CAPABILITY
            or p["schema"] != "proxy-resolution-v1"
        ):
            raise ReplayContractError("proxy_resolution_metadata")
        require_hash(p["batch_hash"])
        if p["status"] not in ("resolved", "waiting", "stopped_contract"):
            raise ReplayContractError("proxy_resolution_status")


def resolve_batch(orders, observations, policy, cash, other_reserved="0"):
    with localcontext() as context:
        context.prec = 128
        return _resolve_batch(orders, observations, policy, cash, other_reserved)


def _resolve_batch(
    orders, observations, policy, cash, other_reserved, *, result_factory=None
):
    arithmetic = policy.require()
    frozen = [
        FrozenProxyOrder(JsonObject.from_value(o)).value.to_dict() for o in orders
    ]
    if (
        len({o["order_id"] for o in frozen}) != len(frozen)
        or len({o["target"] for o in frozen}) > 1
    ):
        raise ReplayContractError("proxy_batch_order_identity")
    if any(o["policy_hash"] != policy.sha256 for o in frozen):
        raise ReplayContractError("proxy_batch_policy")
    batch_hash = digest(dict(orders=frozen))

    def answer(status, reason, results=None):
        if result_factory is not None:
            return result_factory(batch_hash, status, reason, results or [])
        return ProxyResolution(
            JsonObject.from_value(
                dict(
                    schema="proxy-resolution-v1",
                    capability=CAPABILITY,
                    batch_hash=batch_hash,
                    status=status,
                    reason=reason,
                    results=results or [],
                )
            )
        )

    reasons = {}
    for symbol in sorted({o["symbol"] for o in frozen}):
        row = observations.get(symbol)
        if row is None:
            return answer("waiting", "missing_observation")
        validate_row(row)
        if row["symbol"] != symbol or any(
            o["target"] != row["session"] for o in frozen if o["symbol"] == symbol
        ):
            raise ReplayContractError("proxy_resolution_session")
        if row["adjustment_factor"] != "1":
            return answer("stopped_contract", "unsupported_corporate_action")
        if row["halt"] == "full":
            if (row["open"] is not None and D(row["open"]) > 0) or (
                row["volume"] is not None and D(row["volume"]) > 0
            ):
                return answer("stopped_contract", "contradictory_full_halt")
            reasons[symbol] = "no_trade"
            continue
        if row["volume"] is None:
            return answer("waiting", "missing_volume")
        if D(row["volume"]) == 0:
            reasons[symbol] = "no_trade"
            continue
        if row["open"] is None:
            return answer("waiting", "missing_open")
        if D(row["open"]) <= 0:
            return answer("stopped_contract", "invalid_open")
        if all(row[k] is not None for k in ("high", "low", "close")) and not D(
            row["low"]
        ) <= min(D(row["open"]), D(row["close"])) <= max(
            D(row["open"]), D(row["close"])
        ) <= D(row["high"]):
            return answer("stopped_contract", "invalid_ohlc")
        total = sum(o["shares"] for o in frozen if o["symbol"] == symbol)
        if total > D(row["volume"]):
            reasons[symbol] = "instrument_batch_volume_exceeded"
    # All instruments have passed/waited/rejected quality BEFORE any fill.
    available = D(cash) - D(other_reserved) - sum(D(o["reserved"]) for o in frozen)
    if available < 0:
        raise ReplayContractError("proxy_negative_available")
    results = []
    for o in sorted(frozen, key=lambda x: x["rank"]):
        reason = reasons.get(o["symbol"])
        price = gross = fee = net = "0"
        if reason is None:
            mul = (
                1 + D(arithmetic.slippage_pct)
                if o["side"] == "BUY"
                else 1 - D(arithmetic.slippage_pct)
            )
            p = arithmetic.price(D(observations[o["symbol"]]["open"]) * mul, o["side"])
            g, f = arithmetic.cost(p, o["shares"])
            n = g + f if o["side"] == "BUY" else g - f
            price, gross, fee, net = map(M, (p, g, f, n))
            if p <= 0 or n <= 0:
                reason = "nonpositive_model_amount"
            elif o["side"] == "BUY" and n > min(
                D(o["reserved"]), D(o["budget"]), available + D(o["reserved"])
            ):
                reason = "frozen_reservation_or_budget_exceeded"
        if o["side"] == "BUY":
            available += D(o["reserved"]) - (D(net) if reason is None else 0)
        results.append(
            dict(
                order_id=o["order_id"],
                filled=reason is None,
                reason=reason,
                price=price,
                gross=gross,
                commission=fee,
                net=net,
                shares=o["shares"],
                actual_trade_at=None,
            )
        )
    return answer("resolved", None, results)
