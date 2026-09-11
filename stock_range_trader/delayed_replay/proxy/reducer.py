"""Pure Proxy state machine; all effects live in one Stage2 transaction."""

from datetime import date
from decimal import localcontext

from delayed_replay.account_policy import D, M, quantity
from delayed_replay.serialization import JsonObject, digest, parse_time, time_text
from delayed_replay.sizing import size_buy
from delayed_replay.validation import ReplayContractError

from .input_adapter import ProxyPacket
from .policy import CAPABILITY, DailyOpenProxyPolicy
from .resolution import FrozenProxyOrder, resolve_batch


def reserved(s):
    return sum((D(o["reserved_cash"]) for o in s["orders"].values()), D("0")) + D(
        s["proceeds_hold"]
    )


def validate_state(s):
    if s["schema"] != "proxy-state-v1" or s["capability"] != CAPABILITY:
        raise ReplayContractError("proxy_state_metadata")
    if D(s["cash"]) < reserved(s):
        raise ReplayContractError("proxy_cash_conservation")
    for p in s["positions"].values():
        quantity(p["shares"])
    return s


def visible_rows(s, replayed_at):
    result = {}
    for key, item in s["inputs"].items():
        if parse_time(item["acquired_at"]) > replayed_at:
            raise ReplayContractError("proxy_input_not_acquired")
        result[key] = item["row"]
    return result


class ProxyReducer:
    identity = "daily-open-proxy-reducer-v1"

    def __call__(self, raw, command):
        with localcontext() as context:
            context.prec = 128
            return self.reduce(raw, command)

    def reduce(self, raw, command):
        s = JsonObject.from_value(raw).to_dict()
        validate_state(s)
        identity = s["identity"]
        if (
            command.reducer_identity != self.identity
            or command.config_hash != digest(identity)
            or command.correction_of is not None
        ):
            raise ReplayContractError("proxy_command_identity")
        policy = DailyOpenProxyPolicy(
            JsonObject.from_value(identity["terms"]),
            JsonObject.from_value(identity["rules"]),
        )
        policy.gate(identity["mode"], identity["origin"])
        p = command.payload.to_dict()
        if (
            set(p) != {"schema", "business_id", "action", "data"}
            or p["schema"] != "proxy-event-v1"
        ):
            raise ReplayContractError("proxy_event_schema")
        business = p["business_id"]
        meaning = digest(p)
        if business in s["operations"]:
            if s["operations"][business] != meaning:
                raise ReplayContractError("proxy_business_id_conflict")
            return s
        if command.event_type != "proxy." + p["action"]:
            raise ReplayContractError("proxy_event_type")
        if p["action"] == "extension":
            self.extend(s, p["data"], command)
        elif p["action"] == "phase":
            self.phase(s, p["data"], command, policy)
        else:
            raise ReplayContractError("proxy_unknown_action")
        s["operations"][business] = meaning
        return validate_state(s)

    def extend(self, s, d, command):
        packet = ProxyPacket(JsonObject.from_value(d["packet"]))
        p = packet.payload.to_dict()
        if d["parent"] != s["input_head"] or p["origin"] != s["identity"]["origin"]:
            raise ReplayContractError("proxy_input_parent_or_source")
        if parse_time(p["recipe"]["acquired_at"]) > command.replayed_at:
            raise ReplayContractError("proxy_input_not_acquired")
        if packet.payload.sha256 not in command.input_snapshot_hashes:
            raise ReplayContractError("proxy_input_reference")
        status = "refetched"
        changed = {}
        current = s["identity"]["run_sessions"][
            min(s["index"], len(s["identity"]["run_sessions"]) - 1)
        ]
        for key, row in p["records"].items():
            old = s["inputs"].get(key)
            if old and old["row"] == row:
                continue
            if old:
                differences = {k for k in row if row[k] != old["row"][k]}
                # Supplement unobserved fields only, never replace known values.
                allowed = (
                    {"close"}
                    if s["phase"] == "mark"
                    else set(row)
                    if s["phase"] in ("select", "resolve", "decide")
                    else set()
                )
                if (
                    row["session"] != current
                    or not differences <= allowed
                    or any(old["row"][k] is not None for k in differences)
                ):
                    status = "quarantined_revision"
                    break
            elif (
                row["session"] <= s["decided_through"]
                or row["session"] < s["selected_before"]
            ):
                status = "quarantined_past"
                break
            status = "accepted"
            changed[key] = dict(
                row=row,
                packet=packet.payload.sha256,
                acquired_at=p["recipe"]["acquired_at"],
                first_observed_at=old["first_observed_at"]
                if old
                else p["recipe"]["acquired_at"],
                fetched_at=p["recipe"]["acquired_at"],
            )
        version = dict(
            parent=s["input_head"],
            packet=packet.payload.sha256,
            status=status,
            accepted_at=command.replayed_at.isoformat(),
            lane="proxy-input-lane-v1",
        )
        head = digest(version)
        s["versions"][head] = version
        s["input_head"] = head
        if status == "accepted":
            s["inputs"].update(changed)

    def phase(self, s, d, command, policy):
        if (
            type(d["index"]) is not int
            or d["input_head"] != s["input_head"]
            or d["index"] != s["index"]
            or d["phase"] != s["phase"]
            or s["status"] in ("completed", "stopped_contract")
        ):
            raise ReplayContractError("proxy_phase_cursor_or_head")
        days = s["identity"]["run_sessions"]
        day = days[s["index"]]
        if command.market_decision_at.date() != date.fromisoformat(day):
            raise ReplayContractError("proxy_phase_session")
        rows = visible_rows(s, command.replayed_at)
        today = {r["symbol"]: r for r in rows.values() if r["session"] == day}
        phase = s["phase"]
        s["phase_observations"][day + ":" + phase] = dict(
            session_date=day,
            replayed_at=time_text(command.replayed_at),
            modeled_available_phase=phase,
            availability_model_hash=policy.availability_hash,
            input_head=s["input_head"],
            snapshot_hashes=list(command.input_snapshot_hashes),
            actual_trade_at=None,
        )
        s["status"] = "running"
        s["reason"] = None

        def wait(reason):
            s["status"] = "waiting_for_input"
            s["reason"] = reason

        if phase == "select":
            month = day[:7] + "-01"
            if month not in s["epochs"]:
                epoch = d["result"]
                if (
                    epoch["boundary"] != month
                    or epoch["candidate"] not in s["identity"]["candidate_hashes"]
                    and epoch["candidate"] is not None
                ):
                    raise ReplayContractError("proxy_selection_epoch")
                s["epochs"][month] = epoch
                s["selected_before"] = month
            s["phase"] = "resolve"
        elif phase == "resolve":
            orders = [
                o["frozen"]
                for o in s["orders"].values()
                if o["status"] == "pending" and o["frozen"]["target"] == day
            ]
            if any(
                r["adjustment_factor"] != "1"
                for sym, r in today.items()
                if sym in s["positions"]
            ):
                s["status"] = "stopped_contract"
                s["reason"] = "held_corporate_action"
                return
            other = reserved(s) - sum((D(o["reserved"]) for o in orders), D("0"))
            resolution = resolve_batch(
                orders, today, policy, s["cash"], M(other)
            ).value.to_dict()
            if resolution["status"] != "resolved":
                s["status"] = (
                    "waiting_for_input"
                    if resolution["status"] == "waiting"
                    else "stopped_contract"
                )
                s["reason"] = resolution["reason"]
                return
            if day in s["batches"]:
                raise ReplayContractError("proxy_batch_already_resolved")
            s["batches"][day] = resolution
            for result in resolution["results"]:
                entry = s["orders"][result["order_id"]]
                o = entry["frozen"]
                entry["reserved_cash"] = "0"
                entry["reserved_shares"] = 0
                entry["status"] = "filled" if result["filled"] else "rejected"
                entry["resolution"] = result
                if not result["filled"]:
                    continue
                if o["side"] == "BUY":
                    if o["symbol"] in s["positions"]:
                        raise ReplayContractError("proxy_additional_buy")
                    s["cash"] = M(D(s["cash"]) - D(result["net"]))
                    s["positions"][o["symbol"]] = dict(
                        shares=o["shares"],
                        cost_basis=result["net"],
                        episode_id=o["episode_id"],
                        candidate_id=o["candidate_id"],
                        config_hash=o["config_hash"],
                        entry_session=day,
                        holding_sessions=0,
                        breakdown_streak=0,
                        signal_entry_price=None,
                    )
                else:
                    pos = s["positions"].pop(o["symbol"])
                    if (
                        pos["shares"] != o["shares"]
                        or pos["config_hash"] != o["config_hash"]
                    ):
                        raise ReplayContractError("proxy_position_order_mismatch")
                    profit = M(D(result["net"]) - D(pos["cost_basis"]))
                    s["cash"] = M(D(s["cash"]) + D(result["net"]))
                    s["proceeds_hold"] = M(D(s["proceeds_hold"]) + D(result["net"]))
                    s["realized_profit"] = M(D(s["realized_profit"]) + D(profit))
                    s["episodes"][pos["episode_id"]] = dict(
                        profit=profit, entry=pos, exit=result
                    )
            s["valuation_complete"] = False
            s["phase"] = "mark"
        elif phase == "mark":
            if any(
                sym not in today or today[sym]["close"] is None
                for sym in s["positions"]
            ):
                wait("missing_close")
                return
            if any(today[sym]["adjustment_factor"] != "1" for sym in s["positions"]):
                s["status"] = "stopped_contract"
                s["reason"] = "held_corporate_action"
                return
            equity = D(s["cash"]) + sum(
                (
                    p["shares"] * D(today[sym]["close"])
                    for sym, p in s["positions"].items()
                ),
                D("0"),
            )
            s["equity"] = M(equity)
            s["valuation_complete"] = True
            s["marks"][day] = dict(
                equity=M(equity), cash=s["cash"], reserved_cash=M(reserved(s))
            )
            s["proceeds_hold"] = "0"  # Not subtracted from equity a second time.
            s["peak_equity"] = M(max(equity, D(s["peak_equity"])))
            s["phase"] = "decide"
        elif phase == "decide":
            if any(
                sym not in today
                or any(
                    today[sym][k] is None
                    for k in ("open", "high", "low", "close", "volume")
                )
                for sym in s["identity"]["symbols"]
            ):
                wait("missing_signal_bar")
                return
            decisions = d["result"]
            if set(decisions) != set(s["identity"]["symbols"]):
                raise ReplayContractError("proxy_decision_universe")
            a = policy.require()
            ranked = sorted(
                decisions.items(),
                key=lambda item: (
                    item[1]["action"] != "sell",
                    -D(item[1]["score"]),
                    item[0],
                ),
            )
            for rank, (symbol, decision) in enumerate(ranked):
                pos = s["positions"].get(symbol)
                if pos:
                    for k in (
                        "holding_sessions",
                        "breakdown_streak",
                        "signal_entry_price",
                    ):
                        pos[k] = decision[k]
                if s["index"] + 1 == len(days) or decision["action"] == "hold":
                    continue
                side = decision["action"].upper()
                if side not in ("BUY", "SELL"):
                    raise ReplayContractError("proxy_signal_action")
                pending = [v for v in s["orders"].values() if v["status"] == "pending"]
                if any(o["frozen"]["symbol"] == symbol for o in pending):
                    raise ReplayContractError("proxy_duplicate_pending_order")
                if side == "BUY":
                    if pos:
                        raise ReplayContractError("proxy_additional_buy")
                    if (
                        len(s["positions"])
                        + sum(o["frozen"]["side"] == "BUY" for o in pending)
                        >= a.max_positions
                    ):
                        continue
                    q = size_buy(
                        today[symbol]["close"],
                        s["equity"],
                        M(D(s["cash"]) - reserved(s)),
                        a,
                    )
                    if not q.shares:
                        continue
                    shares, budget, reservation = (
                        q.shares,
                        q.frozen_budget,
                        q.reserved_cash,
                    )
                    candidate = s["epochs"][day[:7] + "-01"]["candidate"]
                    if candidate is None:
                        continue
                    cfg = s["identity"]["candidate_hashes"][candidate]
                else:
                    if not pos:
                        raise ReplayContractError("proxy_unheld_sell")
                    shares, budget, reservation = pos["shares"], "0", "0"
                    candidate, cfg = pos["candidate_id"], pos["config_hash"]
                oid = day + ":" + symbol + ":" + side
                frozen = FrozenProxyOrder(
                    JsonObject.from_value(
                        dict(
                            order_id=oid,
                            episode_id=oid if side == "BUY" else pos["episode_id"],
                            symbol=symbol,
                            side=side,
                            target=days[s["index"] + 1],
                            decision_session=day,
                            decision_phase="after_close",
                            candidate_id=candidate,
                            config_hash=cfg,
                            shares=shares,
                            budget=budget,
                            reserved=reservation,
                            reference_price=today[symbol]["close"],
                            decision_equity=s["equity"],
                            equity_at=day + ":after_close",
                            policy_hash=policy.sha256,
                            rank=rank,
                            score=decision["score"],
                        )
                    )
                ).value.to_dict()
                s["orders"][oid] = dict(
                    frozen=frozen,
                    status="pending",
                    reserved_cash=reservation,
                    reserved_shares=shares if side == "SELL" else 0,
                )
            s["decisions"][day] = decisions
            s["decided_through"] = day
            s["phase"] = "finish"
        elif phase == "finish":
            s["index"] += 1
            s["phase"] = "select"
            if s["index"] == len(days):
                s["status"] = "completed"
                s["reason"] = "no_next_bar_no_forced_exit"
        else:
            raise ReplayContractError("proxy_unknown_phase")
