"""Pure shared-account transitions; no signals, scheduler, network or clock reads."""

from decimal import localcontext

from .account_models import (
    Order,
    OrderRequest,
    SharedAccountState,
    market_date,
    session,
)
from .account_policy import AccountError, AccountPolicy, D, M
from .audit_models import EventCommand
from .execution import ExecutionMarkEvidence, ExecutionOpenEvidence, evaluate_fill
from .serialization import JsonObject, digest, parse_time, require_text, time_text
from .sizing import size_buy


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise AccountError("account_command_fields_mismatch")


def _reserved(state):
    return sum(
        (D(o["reservation"]["cash"]) for o in state["orders"].values()), D("0")
    ) + sum((D(h["amount"]) for h in state["proceeds_holds"].values()), D("0"))


def _stop(state, reason, *, halt=False):
    state["risk"]["buy_enabled"] = False
    if reason not in state["risk"]["stop_reasons"]:
        state["risk"]["stop_reasons"].append(reason)
    if halt:
        state["risk"]["halted"] = True
        state["valuation"]["complete"] = False
        state["valuation"]["reasons"] = ["accounting_halted"]


def _display(state):
    val = state["valuation"]
    missing = any(
        instrument not in val["marks"] or val["marks"][instrument]["price"] is None
        for instrument in state["positions"]
    )
    if missing:
        val["display_equity"] = val["unrealized_profit"] = None
        return
    value = sum(
        (
            pos["shares"] * D(val["marks"][instrument]["price"])
            for instrument, pos in state["positions"].items()
        ),
        D("0"),
    )
    basis = sum((D(pos["cost_basis"]) for pos in state["positions"].values()), D("0"))
    val["display_equity"] = M(D(state["cash"]) + value)
    val["unrealized_profit"] = M(value - basis)


def _terminal(order, status, reason):
    if order["status"] != "pending":
        raise AccountError("terminal_order")
    order["status"], order["reason"] = status, reason
    order["reservation"] = dict(cash="0", shares=0, released=True)


class AccountReducer:
    identity = "shared-account-reducer-1"

    def __call__(self, raw: dict, command: EventCommand) -> dict:
        with localcontext() as context:
            context.prec = 128
            state = SharedAccountState(JsonObject.from_value(raw)).to_dict()
            policy = AccountPolicy.from_dict(state["policy"])
            if (
                command.config_hash != policy.sha256
                or command.reducer_identity != self.identity
            ):
                raise AccountError("account_identity_mismatch")
            if command.market_decision_at < parse_time(state["risk"]["equity_at"]):
                raise AccountError("decision_precedes_confirmed_equity")
            p = command.payload.to_dict()
            require_text(p.get("operation_id"))
            handlers = {
                "account.submit": self._submit,
                "account.execute": self._execute,
                "account.cancel": self._cancel,
                "account.finalize": self._finalize,
                "account.release": self._release,
                "account.mark": self._mark,
                "account.stop": self._risk_stop,
            }
            if command.event_type not in handlers or command.correction_of is not None:
                raise AccountError("unsupported_account_command")
            # Business operation identity is independent of the audit event ID.
            # Sorting candidate/evidence sets removes irrelevant input-row order.
            for name, key in (
                ("requests", "order_id"),
                ("evidence", "instrument_id"),
                ("marks", "instrument_id"),
            ):
                if name in p:
                    if type(p[name]) is not list:
                        raise AccountError("batch_list_required")
                    p[name] = sorted(p[name], key=lambda row: row[key])
            for name in ("order_ids", "hold_ids"):
                if name in p:
                    if type(p[name]) is not list or len(set(p[name])) != len(p[name]):
                        raise AccountError("invalid_id_set")
                    p[name] = sorted(p[name])
            key = command.event_type + ":" + p["operation_id"]
            meaning = digest(
                dict(
                    type=command.event_type,
                    payload=p,
                    market_decision_at=time_text(command.market_decision_at),
                    input_snapshot_hashes=sorted(command.input_snapshot_hashes),
                )
            )
            if key in state["operations"]:
                if state["operations"][key] != meaning:
                    raise AccountError("business_operation_conflict")
                return state
            handlers[command.event_type](state, p, command, policy)
            state["operations"][key] = meaning
            return SharedAccountState(JsonObject.from_value(state)).to_dict()

    def _submit(self, state, p, command, policy):
        _fields(p, "operation_id requests")
        requests = [OrderRequest.from_dict(row) for row in p["requests"]]
        ids = [r.order_id for r in requests]
        instruments = [r.instrument_id for r in requests]
        if (
            len(set(ids)) != len(ids)
            or len(set(instruments)) != len(instruments)
            or any(i in state["orders"] for i in ids)
        ):
            raise AccountError("duplicate_order_or_instrument")
        for req in requests:
            if (
                req.decision_at != time_text(command.market_decision_at)
                or req.snapshot_hash not in command.input_snapshot_hashes
                or req.lot_evidence_hash not in command.input_snapshot_hashes
            ):
                raise AccountError("decision_reference_mismatch")
            if (
                req.target_session in state["closed_sessions"]
                or req.target_session in state["executed_sessions"]
            ):
                raise AccountError("session_already_processed")
        requests.sort(
            key=lambda r: (r.side != "SELL", -D(r.range_score), r.instrument_id)
        )
        for req in requests:
            pending = [o for o in state["orders"].values() if o["status"] == "pending"]
            slots = set(state["positions"]) | {
                o["request"]["instrument_id"]
                for o in pending
                if o["request"]["side"] == "BUY"
            }
            reason = None
            shares, budget, reserved, locked = 0, "0", "0", 0
            if req.side == "BUY":
                if state["risk"]["halted"] or not state["risk"]["buy_enabled"]:
                    reason = "buy_risk_stopped"
                elif not state["valuation"]["complete"]:
                    reason = "valuation_incomplete"
                elif (
                    state["positions"]
                    and state["valuation"]["session"] != req.reference_session
                ):
                    reason = "valuation_stale_for_reference_session"
                elif req.instrument_id in slots:
                    reason = "additional_buy_not_supported"
                elif len(slots) >= policy.max_positions:
                    reason = "holding_slots_exhausted"
                else:
                    result = size_buy(
                        req.reference_price,
                        state["risk"]["last_equity"],
                        M(D(state["cash"]) - _reserved(state)),
                        policy,
                    )
                    shares, budget, reserved = (
                        result.shares,
                        result.frozen_budget,
                        result.reserved_cash,
                    )
                    if shares == 0:
                        reason = "insufficient_lot_budget"
            else:
                shares = req.requested_shares
                pos = state["positions"].get(req.instrument_id)
                if state["risk"]["halted"]:
                    reason = "accounting_halted"
                elif pos is None:
                    reason = "unheld_sell"
                elif any(
                    o["request"]["instrument_id"] == req.instrument_id
                    and o["request"]["side"] == "SELL"
                    for o in pending
                ):
                    reason = "duplicate_sell"
                elif shares != pos["shares"]:
                    reason = "partial_exit_not_supported"
                elif any(
                    getattr(req, name) != pos[name]
                    for name in (
                        "candidate_id",
                        "entry_config_hash",
                        "exit_config_hash",
                    )
                ):
                    reason = "entry_provenance_mismatch"
                else:
                    locked = shares
            state["orders"][req.order_id] = dict(
                request=req.to_dict(),
                shares=shares,
                frozen_budget=budget,
                initial_reserved_cash=reserved,
                reservation=dict(
                    cash="0" if reason else reserved,
                    shares=0 if reason else locked,
                    released=reason is not None,
                ),
                decision_equity=state["risk"]["last_equity"],
                equity_at=state["risk"]["equity_at"],
                policy_hash=policy.sha256,
                status="rejected" if reason else "pending",
                reason=reason,
                fill=None,
            )

    def _execute(self, state, p, command, policy):
        _fields(p, "operation_id session order_ids evidence")
        day = session(p["session"])
        if (
            market_date(time_text(command.market_decision_at)) != day
            or p["session"] in state["executed_sessions"]
            or p["session"] in state["closed_sessions"]
        ):
            raise AccountError("execution_session_invalid")
        orders = [
            o
            for o in state["orders"].values()
            if o["status"] == "pending"
            and o["request"]["target_session"] == p["session"]
        ]
        if set(p["order_ids"]) != {o["request"]["order_id"] for o in orders}:
            raise AccountError("execution_order_set_mismatch")
        evidence_list = [ExecutionOpenEvidence.from_dict(row) for row in p["evidence"]]
        evidence = {e.instrument_id: e for e in evidence_list}
        if len(evidence) != len(evidence_list) or set(evidence) - {
            o["request"]["instrument_id"] for o in orders
        }:
            raise AccountError("execution_instrument_mismatch")
        for e in evidence_list:
            self._check_evidence(e, p["session"], command, policy)
            if parse_time(e.market_available_at) != command.market_decision_at:
                raise AccountError("execution_clock_must_equal_open_clock")
            if (
                not e.corporate_action_supported
                and e.instrument_id in state["positions"]
            ):
                _stop(state, "unsupported_corporate_action", halt=True)
        orders.sort(
            key=lambda o: (
                o["request"]["side"] != "SELL",
                -D(o["request"]["range_score"]),
                o["request"]["instrument_id"],
            )
        )
        for order in orders:
            req = order["request"]
            e = evidence.get(req["instrument_id"])
            if e is None:
                continue  # Only explicit finalization expires unobserved orders.
            if state["risk"]["halted"]:
                _terminal(order, "rejected", "accounting_halted")
                continue
            allowance = (
                D(state["cash"]) - _reserved(state) + D(order["reservation"]["cash"])
            )
            fill = evaluate_fill(
                Order(JsonObject.from_value(order)),
                e,
                policy,
                M(allowance),
                state["risk"]["buy_enabled"],
            )
            if not fill.filled:
                _terminal(order, "rejected", fill.reason)
                continue
            instrument = req["instrument_id"]
            if req["side"] == "BUY":
                if instrument in state["positions"]:
                    raise AccountError("position_already_exists")
                state["cash"] = M(D(state["cash"]) - D(fill.net_amount))
                state["positions"][instrument] = dict(
                    instrument_id=instrument,
                    position_id=req["order_id"],
                    episode_id=req["order_id"],
                    entry_order_id=req["order_id"],
                    shares=order["shares"],
                    entry_gross=fill.gross,
                    entry_commission=fill.commission,
                    cost_basis=fill.net_amount,
                    candidate_id=req["candidate_id"],
                    entry_config_hash=req["entry_config_hash"],
                    exit_config_hash=req["exit_config_hash"],
                    realized_profit="0",
                )
                state["valuation"]["marks"][instrument] = dict(
                    price=fill.price,
                    session=p["session"],
                    quality="entry_open",
                    snapshot_hash=e.snapshot_hash,
                )
            else:
                pos = state["positions"].pop(instrument)
                profit = D(fill.net_amount) - D(pos["cost_basis"])
                if pos["episode_id"] in state["episodes"]:
                    raise AccountError("episode_already_closed")
                state["cash"] = M(D(state["cash"]) + D(fill.net_amount))
                state["realized_profit"] = M(D(state["realized_profit"]) + profit)
                pos["realized_profit"] = M(profit)
                state["episodes"][pos["episode_id"]] = dict(
                    position=pos, exit_order_id=req["order_id"], net_profit=M(profit)
                )
                state["proceeds_holds"][req["order_id"]] = dict(
                    amount=fill.net_amount,
                    sale_session=p["session"],
                    sale_at=time_text(command.market_decision_at),
                )
                state["valuation"]["marks"].pop(instrument, None)
            _terminal(order, "filled", None)
            order["fill"] = dict(
                price=fill.price,
                gross=fill.gross,
                commission=fill.commission,
                net_amount=fill.net_amount,
                session=p["session"],
                snapshot_hash=e.snapshot_hash,
            )
            state["valuation"]["complete"] = False
            state["valuation"]["reasons"] = ["awaiting_complete_mark"]
        state["executed_sessions"].append(p["session"])
        _display(state)

    @staticmethod
    def _check_evidence(e, day, command, policy):
        if (
            e.session != day
            or e.snapshot_hash not in command.input_snapshot_hashes
            or e.basis_evidence_hash != policy.basis_evidence_hash
            or parse_time(e.market_available_at) > command.market_decision_at
        ):
            raise AccountError("execution_evidence_reference_mismatch")

    def _cancel(self, state, p, command, policy):
        _fields(p, "operation_id order_id reason")
        if p["reason"] != "user_cancel" or p["order_id"] not in state["orders"]:
            raise AccountError("invalid_cancel")
        _terminal(state["orders"][p["order_id"]], "canceled", p["reason"])

    def _finalize(self, state, p, command, policy):
        _fields(p, "operation_id session")
        if (
            market_date(time_text(command.market_decision_at)) < session(p["session"])
            or p["session"] in state["closed_sessions"]
        ):
            raise AccountError("invalid_session_finalization")
        for order in state["orders"].values():
            if (
                order["status"] == "pending"
                and order["request"]["target_session"] == p["session"]
            ):
                _terminal(order, "canceled", "no_execution_evidence")
        state["closed_sessions"].append(p["session"])

    def _release(self, state, p, command, policy):
        _fields(p, "operation_id decision_session hold_ids reason")
        if (
            p["reason"] != "next_eligible_decision"
            or session(p["decision_session"])
            != market_date(time_text(command.market_decision_at))
            or not state["valuation"]["complete"]
        ):
            raise AccountError("ineligible_proceeds_release")
        for hold_id in p["hold_ids"]:
            hold = state["proceeds_holds"].get(hold_id)
            if hold is None or session(hold["sale_session"]) >= session(
                p["decision_session"]
            ):
                raise AccountError("proceeds_not_releasable")
        for hold_id in p["hold_ids"]:
            del state["proceeds_holds"][hold_id]

    def _mark(self, state, p, command, policy):
        _fields(p, "operation_id session marks")
        day = session(p["session"])
        if day != market_date(time_text(command.market_decision_at)) or day <= session(
            state["valuation"]["session"]
        ):
            raise AccountError("valuation_session_already_processed")
        marks = [ExecutionMarkEvidence.from_dict(row) for row in p["marks"]]
        indexed = {e.instrument_id: e for e in marks}
        if len(indexed) != len(marks) or set(indexed) - set(state["positions"]):
            raise AccountError("mark_instrument_mismatch")
        for e in marks:
            self._check_evidence(e, p["session"], command, policy)
        reasons = []
        for instrument in sorted(state["positions"]):
            e = indexed.get(instrument)
            if e is None:
                reasons.append(instrument + ":missing")
                if instrument in state["valuation"]["marks"]:
                    state["valuation"]["marks"][instrument]["quality"] = "stale"
                continue
            if not e.corporate_action_supported:
                _stop(state, "unsupported_corporate_action", halt=True)
                reasons.append(instrument + ":unsupported_corporate_action")
            elif e.quality != "complete":
                reasons.append(instrument + ":" + e.quality)
                if instrument in state["valuation"]["marks"]:
                    state["valuation"]["marks"][instrument]["quality"] = "stale"
            else:
                state["valuation"]["marks"][instrument] = dict(
                    price=e.mark_price,
                    session=p["session"],
                    quality="complete",
                    snapshot_hash=e.snapshot_hash,
                )
        state["valuation"]["session"] = p["session"]
        state["valuation"]["reasons"] = reasons or (
            ["accounting_halted"] if state["risk"]["halted"] else []
        )
        state["valuation"]["complete"] = not state["valuation"]["reasons"]
        _display(state)
        if state["valuation"]["complete"]:
            equity = D(state["valuation"]["display_equity"])
            high = max(D(state["risk"]["high_water"]), equity)
            state["risk"].update(
                last_equity=M(equity),
                equity_at=time_text(command.market_decision_at),
                high_water=M(high),
                drawdown=M(((high - equity) / high).quantize(D("0.000000000001")))
                if high
                else "0",
            )

    def _risk_stop(self, state, p, command, policy):
        _fields(p, "operation_id reason")
        require_text(p["reason"])
        _stop(state, p["reason"])
