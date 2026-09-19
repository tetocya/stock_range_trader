"""Read-only, offline research account viewer. No execution or recovery."""

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import localcontext
from pathlib import Path

from delayed_replay.serialization import JsonObject

from .arithmetic import number, text
from .order_audit import OrderAuditBundle, OrderAuditReader, _price_evidence
from .reader import ObservationError


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ObservationError("invalid_saved_session")
    return value


def _money(value):
    result = number(value)
    if result < 0:
        raise ObservationError("negative_saved_balance")
    return result


@dataclass(frozen=True)
class AccountReadModel:
    payload: JsonObject
    audit: OrderAuditBundle

    @classmethod
    def read(cls, trial_root, account=None):
        projections = []

        def observe(state, stored, root, files, cache):
            projections.append(_project(state, root, files, cache))

        audit = OrderAuditReader().read(trial_root, account, _observer=observe)
        return cls(JsonObject.from_value(projections[0]), audit)


def _project(s, root, files, cache):
    """Only saved values and accounting identities, never a state transition."""
    days = s["identity"]["run_sessions"]
    if (
        not isinstance(days, list)
        or not days
        or days != sorted(set(_day(d) for d in days))
        or type(s["index"]) is not int
        or not 0 <= s["index"] <= len(days)
        or type(s["valuation_complete"]) is not bool
        or s["phase"] not in ("select", "resolve", "mark", "decide", "finish")
        or s["status"]
        not in ("running", "waiting_for_input", "completed", "stopped_contract")
        or not isinstance(s["marks"], dict)
        or not isinstance(s["positions"], dict)
    ):
        raise ObservationError("invalid_saved_view_structure")
    with localcontext() as context:
        context.prec = 128
        cash, hold = _money(s["cash"]), _money(s["proceeds_hold"])
        reserved = sum(
            (_money(o["reserved_cash"]) for o in s["orders"].values()), number("0")
        )
        if reserved + hold > cash:
            raise ObservationError("saved_available_cash_negative")
        attempted = set()
        for item in s["phase_observations"].values():
            attempted.add(_day(item["session_date"]))
        if not attempted <= set(days) or not set(s["marks"]) <= set(days):
            raise ObservationError("saved_session_outside_schedule")
        # Scheduled dates supply gaps, not prices; exclude unattempted future dates.
        seen = set(days[: s["index"]]) | attempted | set(s["marks"])
        cutoff = max(seen) if seen else None
        history = []
        for day in days:
            if cutoff is None or day > cutoff:
                break
            mark = s["marks"].get(day)
            row = dict(
                session=day,
                cash=None,
                equity=None,
                position_value=None,
                reserved_total=None,
                valuation_status="missing_saved_mark",
            )
            if mark is not None:
                if set(mark) != {"equity", "cash", "reserved_cash"}:
                    raise ObservationError("unsupported_saved_mark_fields")
                equity, marked_cash = _money(mark["equity"]), _money(mark["cash"])
                if equity < marked_cash or _money(mark["reserved_cash"]) > marked_cash:
                    raise ObservationError("invalid_saved_mark_balance")
                row.update(
                    cash=text(marked_cash),
                    equity=text(equity),
                    position_value=text(equity - marked_cash),
                    reserved_total=text(_money(mark["reserved_cash"])),
                    valuation_status="saved_mark_not_replayed",
                )
            history.append(row)
        last = max(s["marks"], default=None)
        positions, evidence, missing = [], set(), []
        value = number("0")
        for symbol, pos in sorted(s["positions"].items()):
            if (
                type(pos["shares"]) is not int
                or pos["shares"] <= 0
                or pos["shares"] % 100
            ):
                raise ObservationError("invalid_saved_position_quantity")
            price = None
            reason = "current_valuation_incomplete"
            if s["valuation_complete"] and last:
                item = s["inputs"].get(symbol + "|" + last)
                if item is not None and item["row"].get("close") is not None:
                    bar, hashes, absent = _price_evidence(
                        root, s, symbol, last, files, cache
                    )
                    evidence.update(hashes)
                    missing.extend(absent)
                    if not absent:
                        price = _money(bar["close"])
                        if price == 0:
                            raise ObservationError("invalid_saved_valuation_price")
                        reason = "verified_saved_close"
                if price is None:
                    reason = "insufficient_valuation_evidence"
                    missing.append("valuation_price:" + symbol)
            elif s["valuation_complete"]:
                reason = "insufficient_valuation_evidence"
                missing.append("valuation_mark:" + symbol)
            market_value = None if price is None else price * pos["shares"]
            if market_value is not None:
                value += market_value
            positions.append(
                dict(
                    symbol=symbol,
                    shares=pos["shares"],
                    cost_basis=text(_money(pos["cost_basis"])),
                    entry_session=_day(pos["entry_session"]),
                    valuation_price=text(price),
                    valuation_session=last if price is not None else None,
                    position_value=text(market_value),
                    valuation_status=reason,
                )
            )
        complete = s["valuation_complete"] and all(
            p["position_value"] is not None for p in positions
        )
        saved_equity = _money(s["equity"])
        if complete and saved_equity != cash + value:
            raise ObservationError("saved_equity_accounting_mismatch")
        return dict(
            schema="account-read-v1",
            account=dict(
                cash=text(cash),
                order_reserved_cash=text(reserved),
                proceeds_hold=text(hold),
                reserved_total=text(reserved + hold),
                available_cash=text(cash - reserved - hold),
                position_value=text(value) if complete else None,
                equity=text(saved_equity) if complete else None,
                saved_equity_last_recorded=text(saved_equity),
                valuation_complete=complete,
                saved_valuation_complete=s["valuation_complete"],
                last_valuation_session=last,
                last_mark=s["marks"].get(last),
                realized_profit=text(number(s["realized_profit"])),
            ),
            positions=positions,
            history=history,
            replay=dict(
                index=s["index"],
                phase=s["phase"],
                status=s["status"],
                reason=s["reason"],
                current_session=days[s["index"]] if s["index"] < len(days) else None,
                last_finalized_session=days[s["index"] - 1] if s["index"] else None,
            ),
            missing_evidence=sorted(set(missing)),
            evidence_hashes=sorted(evidence),
        )


class AccountViewBuilder:
    def build(self, model):
        with localcontext() as context:
            context.prec = 128
            result = model.payload.to_dict()
            orders = [r.to_dict() for r in model.audit.records]
            counts = Counter(o["saved_status"] for o in orders)
            fees = [o["charged_commission"] for o in orders]
            issues = sorted(
                {
                    o["verification"]["status"]
                    for o in orders
                    if o["verification"]["status"] != "verified"
                }
            )
            metadata = model.audit.metadata.to_dict()
            if metadata["account_cash_reconciliation"] is not True:
                issues.append("cash_reconciliation_not_verified")
            if result["missing_evidence"]:
                issues.append("insufficient_valuation_evidence")
            if not result["account"]["valuation_complete"]:
                issues.append("current_valuation_incomplete")
            result.update(
                schema="account-view-v1",
                metadata=metadata,
                orders=orders,
                validation=dict(
                    status="attention_required"
                    if issues
                    else "saved_records_checked_not_replayed",
                    issues=issues,
                ),
                summary=dict(
                    position_count=len(result["positions"]),
                    order_count=len(orders),
                    rejected_count=counts["rejected"],
                    filled_count=counts["filled"],
                    status_counts=dict(sorted(counts.items())),
                    charged_commission=text(sum((number(f) for f in fees), number("0")))
                    if all(f is not None for f in fees)
                    else None,
                ),
            )
            return JsonObject.from_value(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--account", help="Relative saved DB; default comparison/continuous.sqlite"
    )
    args = parser.parse_args(argv)
    try:
        from .account_html import AccountHtmlWriter

        model = AccountReadModel.read(args.trial_root, args.account)
        AccountHtmlWriter().write(model, args.output)
        print(json.dumps(dict(status="view_written_not_execution")))
        return 0
    except (ValueError, OSError, KeyError, TypeError, OverflowError):
        print(
            json.dumps(
                dict(
                    status="blocked",
                    reason="input_schema_integrity_or_output_contract",
                    execution="not_invoked",
                )
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
