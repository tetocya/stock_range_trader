"""Single-root account + cursor reducer; no evaluator, price lookup or I/O."""

import math
from dataclasses import dataclass, replace

from .account_models import SharedAccountState
from .account_policy import AccountPolicy, D
from .account_reducer import AccountReducer
from .audit_models import EventCommand
from .monthly_selection import SelectionEpoch
from .serialization import JsonObject, digest, parse_time, time_text
from .validation import ReplayContractError

PHASES = ("select", "open", "mark", "prepare", "decide", "finish")


@dataclass(frozen=True, slots=True)
class ReplayState:
    value: JsonObject

    def __post_init__(self):
        data = self.value.to_dict()
        if (
            set(data)
            != {
                "schema",
                "identity",
                "account",
                "cursor",
                "status",
                "reason",
                "epochs",
                "position_states",
                "decisions",
                "operations",
                "visible",
            }
            or data["schema"] != "synthetic-replay-4-1"
        ):
            raise ReplayContractError("invalid_replay_state_schema")
        SharedAccountState(JsonObject.from_value(data["account"]))
        cursor = data["cursor"]
        if (
            set(cursor) != {"index", "phase"}
            or type(cursor["index"]) is not int
            or cursor["index"] < 0
            or cursor["phase"] not in PHASES
        ):
            raise ReplayContractError("invalid_replay_cursor")
        if data["status"] not in (
            "running",
            "waiting_for_input",
            "completed",
            "stopped_contract",
        ):
            raise ReplayContractError("invalid_replay_status")
        if set(data["position_states"]) != set(data["account"]["positions"]):
            raise ReplayContractError("position_strategy_state_mismatch")
        for epoch in data["epochs"].values():
            SelectionEpoch(JsonObject.from_value(epoch))

    def to_dict(self):
        return self.value.to_dict()


class ReplayReducer:
    identity = "synthetic-replay-reducer-4-1"

    def __call__(self, raw, command: EventCommand):
        state = ReplayState(JsonObject.from_value(raw)).to_dict()
        identity = state["identity"]
        if (
            command.event_type != "replay.phase"
            or command.reducer_identity != self.identity
            or command.config_hash != digest(identity)
            or command.correction_of is not None
        ):
            raise ReplayContractError("replay_command_identity_mismatch")
        p = command.payload.to_dict()
        if set(p) != {"index", "phase", "action", "data"}:
            raise ReplayContractError("invalid_phase_command")
        if (
            type(p["index"]) is not int
            or p["index"] < 0
            or p["phase"] not in PHASES
            or type(p["data"]) is not dict
        ):
            raise ReplayContractError("invalid_phase_payload_types")
        key = f"{p['index']}:{p['phase']}:{p['action']}"
        canonical = digest(
            {
                "payload": p,
                "market_time": time_text(command.market_decision_at),
                "inputs": list(command.input_snapshot_hashes),
            }
        )
        if key in state["operations"]:
            if state["operations"][key] != canonical:
                raise ReplayContractError("phase_business_identity_conflict")
            return state
        if (
            p["index"] != state["cursor"]["index"]
            or p["phase"] != state["cursor"]["phase"]
            or state["status"] in ("completed", "stopped_contract")
        ):
            raise ReplayContractError("unexpected_replay_phase")
        session = identity["sessions"][p["index"]]
        phase = p["phase"]
        expected_time = (
            session["selection_at"]
            if phase == "select"
            else session["open_at"]
            if phase == "open"
            else session["close_at"]
        )
        if time_text(command.market_decision_at) != expected_time or tuple(
            command.input_snapshot_hashes
        ) != tuple(identity["references"]):
            raise ReplayContractError("phase_time_or_input_mismatch")
        data = p["data"]
        if p["action"] in ("wait", "stop"):
            if (
                set(data) != {"reason"}
                or not isinstance(data["reason"], str)
                or not data["reason"]
            ):
                raise ReplayContractError("phase_reason_required")
            state["reason"] = data["reason"]
            state["status"] = (
                "waiting_for_input" if p["action"] == "wait" else "stopped_contract"
            )
            if p["action"] == "stop":
                state["account"]["risk"]["buy_enabled"] = False
                state["account"]["risk"]["halted"] = True
                state["account"]["risk"]["stop_reasons"].append(data["reason"])
            # Waiting can be repeated with changing reasons; not a completed phase.
            if p["action"] == "stop":
                state["operations"][key] = canonical
            return ReplayState(JsonObject.from_value(state)).to_dict()
        if p["action"] != "commit":
            raise ReplayContractError("unknown_phase_action")
        state["status"], state["reason"] = "running", None
        day = session["day"]

        def account(kind, payload):
            internal = replace(
                command,
                event_type="account." + kind,
                config_hash=AccountPolicy.from_dict(state["account"]["policy"]).sha256,
                reducer_identity=AccountReducer.identity,
                payload=JsonObject.from_value(
                    {"operation_id": key + ":" + kind, **payload}
                ),
            )
            state["account"] = AccountReducer()(state["account"], internal)

        if phase == "select":
            if set(data) != {"epoch"}:
                raise ReplayContractError("invalid_selection_commit")
            boundary = day[:7] + "-01"
            due = boundary not in state["epochs"]
            if due != (data["epoch"] is not None):
                raise ReplayContractError("monthly_selection_count_mismatch")
            if due:
                epoch = SelectionEpoch(
                    JsonObject.from_value(data["epoch"])
                ).value.to_dict()
                if (
                    epoch["boundary"] != boundary
                    or epoch["executed_at"] != expected_time
                    or epoch["configuration_hash"] != identity["selection_hash"]
                ):
                    raise ReplayContractError("selection_epoch_identity_mismatch")
                if (
                    epoch["candidate_id"] is not None
                    and epoch["candidate_id"] not in identity["candidate_configs"]
                ):
                    raise ReplayContractError("selection_candidate_not_in_catalog")
                state["epochs"][boundary] = epoch
        elif phase == "open":
            if set(data) != {"evidence"}:
                raise ReplayContractError("invalid_open_commit")
            orders = state["account"]["orders"]
            pending = sorted(
                k
                for k, o in orders.items()
                if o["status"] == "pending" and o["request"]["target_session"] == day
            )
            required = {orders[k]["request"]["instrument_id"] for k in pending} | set(
                state["account"]["positions"]
            )
            if {e["instrument_id"] for e in data["evidence"]} != required or any(
                not e["corporate_action_supported"]
                for e in data["evidence"]
                if e["instrument_id"] in state["account"]["positions"]
            ):
                raise ReplayContractError("incomplete_or_unsupported_open_batch")
            # Stage 3 consumes order evidence only. Held evidence above is a
            # separate causal safety gate, never a fabricated pending order.
            targets = {orders[k]["request"]["instrument_id"] for k in pending}
            account(
                "execute",
                {
                    "session": day,
                    "order_ids": pending,
                    "evidence": [
                        e for e in data["evidence"] if e["instrument_id"] in targets
                    ],
                },
            )
            state["position_states"] = {
                s: meta
                for s, meta in state["position_states"].items()
                if s in state["account"]["positions"]
            }
            for symbol, pos in state["account"]["positions"].items():
                if symbol not in state["position_states"]:
                    state["position_states"][symbol] = dict(
                        entry_session=day,
                        candidate_id=pos["candidate_id"],
                        config_hash=pos["entry_config_hash"],
                        signal_entry_price=None,
                        holding_sessions=0,
                        breakdown_streak=0,
                        last_decision_session=None,
                    )
        elif phase == "mark":
            if (
                set(data) != {"marks"}
                or {m["instrument_id"] for m in data["marks"]}
                != set(state["account"]["positions"])
                or any(
                    m["quality"] != "complete" or not m["corporate_action_supported"]
                    for m in data["marks"]
                )
            ):
                raise ReplayContractError("atomic_complete_mark_batch_required")
            account("mark", {"session": day, "marks": data["marks"]})
        elif phase == "prepare":
            if (
                data
                or not state["account"]["valuation"]["complete"]
                or state["account"]["valuation"]["session"] != day
            ):
                raise ReplayContractError("close_preparation_requires_complete_mark")
            # Stage 4 opt-in bridge: same-session *completed close*, not same
            # Open reuse. Do not spoof a future date to Stage 3 account.release.
            if (
                identity["policy"]["proceeds_mode"]
                != "release_after_sale_open_at_complete_close"
            ):
                raise ReplayContractError("close_release_policy_required")
            holds = state["account"]["proceeds_holds"]
            for hold_id in tuple(holds):
                if parse_time(holds[hold_id]["sale_at"]) < command.market_decision_at:
                    del holds[hold_id]
            if D(state["account"]["risk"]["drawdown"]) >= D(
                identity["policy"]["maximum_drawdown"]
            ):
                account("stop", {"reason": "maximum_drawdown"})
        elif phase == "decide":
            if set(data) != {"requests", "decisions", "position_states"}:
                raise ReplayContractError("invalid_decision_commit")
            next_day = (
                identity["sessions"][p["index"] + 1]["day"]
                if p["index"] + 1 < len(identity["sessions"])
                else None
            )
            candidate = state["epochs"][day[:7] + "-01"]["candidate_id"]
            for req in data["requests"]:
                if req["target_session"] != next_day or req["reference_session"] != day:
                    raise ReplayContractError("order_outside_next_session")
                expected_candidate = (
                    candidate
                    if req["side"] == "BUY"
                    else state["account"]["positions"][req["instrument_id"]][
                        "candidate_id"
                    ]
                )
                if (
                    expected_candidate is None
                    or req["candidate_id"] != expected_candidate
                    or req["entry_config_hash"]
                    != identity["candidate_configs"][expected_candidate]
                    or req["exit_config_hash"] != req["entry_config_hash"]
                ):
                    raise ReplayContractError("decision_candidate_mismatch")
            if set(data["position_states"]) != set(state["position_states"]):
                raise ReplayContractError("missing_position_strategy_updates")
            for symbol, meta in data["position_states"].items():
                old = state["position_states"][symbol]
                if (
                    set(meta) != set(old)
                    or meta["holding_sessions"] != old["holding_sessions"] + 1
                    or type(meta["holding_sessions"]) is not int
                    or type(meta["breakdown_streak"]) is not int
                    or meta["breakdown_streak"] < 0
                    or meta["last_decision_session"] != day
                    or any(
                        meta[k] != old[k]
                        for k in ("entry_session", "candidate_id", "config_hash")
                    )
                    or (
                        old["signal_entry_price"] is not None
                        and old["signal_entry_price"] != meta["signal_entry_price"]
                    )
                    or not isinstance(meta["signal_entry_price"], str)
                    or not math.isfinite(float(meta["signal_entry_price"]))
                    or float(meta["signal_entry_price"]) <= 0
                ):
                    raise ReplayContractError("invalid_position_strategy_transition")
            account("submit", {"requests": data["requests"]})
            state["position_states"] = data["position_states"]
            state["decisions"][day] = data["decisions"]
        else:
            if data:
                raise ReplayContractError("invalid_finish_commit")
            account("finalize", {"session": day})
        state["operations"][key] = canonical
        state["visible"] = {
            "session": day,
            "phase": phase,
            "market_time": expected_time,
            "batch_hash": digest(data),
        }
        if phase == "finish":
            state["cursor"] = {"index": p["index"] + 1, "phase": "select"}
            if state["cursor"]["index"] == len(identity["sessions"]):
                state["status"] = "completed"
        else:
            state["cursor"]["phase"] = PHASES[PHASES.index(phase) + 1]
        return ReplayState(JsonObject.from_value(state)).to_dict()
