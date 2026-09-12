"""Separately authorized research stream using only shared proxy arithmetic."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import localcontext
from pathlib import Path

import pandas as pd

from delayed_replay.account_policy import D, quantity
from delayed_replay.audit_models import EventCommand, StreamIdentity
from delayed_replay.event_store import EventStore
from delayed_replay.proxy.reducer import ProxyReducer, reserved
from delayed_replay.proxy.resolution import _resolve_batch
from delayed_replay.serialization import JsonObject, decimal_text, digest, parse_time
from delayed_replay.validation import ReplayContractError

from .inputs import SavedProxyInputs
from .models import SCOPE, LimitedProxyTrialPlan, ScopedResearchAuthorization
from .preflight import LimitedTrialPreflight


@dataclass(frozen=True)
class _Resolution:
    value: JsonObject


class _LimitedReducer(ProxyReducer):
    identity = "limited-daily-open-proxy-reducer-v1"

    def _resolve(self, orders, today, policy, cash, other):
        # Reuse numerical decisions, not the synthetic evidence envelope/Gate.
        def factory(batch_hash, status, reason, results):
            return _Resolution(
                JsonObject.from_value(
                    dict(
                        schema="limited-resolution-v1",
                        scope=SCOPE,
                        batch_hash=batch_hash,
                        status=status,
                        reason=reason,
                        results=results,
                    )
                )
            )

        return _resolve_batch(
            orders, today, policy, cash, other, result_factory=factory
        )

    def __call__(self, raw, command):
        with localcontext() as context:
            context.prec = 128
            s = JsonObject.from_value(raw).to_dict()
            identity = s["identity"]
            plan = LimitedProxyTrialPlan(JsonObject.from_value(identity["plan"]))
            auth = ScopedResearchAuthorization(
                JsonObject.from_value(identity["authorization"])
            )
            auth.require(plan)
            if s["schema"] != "limited-proxy-state-v1" or s["capability"] != dict(
                **SCOPE,
                model_approval="unapproved",
                approval_status=auth.payload.to_dict()["status"],
                provenance=plan.payload.to_dict()["provenance"],
            ):
                raise ReplayContractError("limited_state_metadata")
            if (
                command.reducer_identity != self.identity
                or command.config_hash != digest(identity)
                or command.correction_of is not None
            ):
                raise ReplayContractError("limited_command_identity")
            if parse_time(auth.payload.to_dict()["recorded_at"]) > command.replayed_at:
                raise ReplayContractError("limited_authorization_not_yet_recorded")
            p = command.payload.to_dict()
            if (
                set(p) != {"schema", "business_id", "action", "data"}
                or p["schema"] != "limited-proxy-event-v1"
                or command.event_type != "limited." + p["action"]
            ):
                raise ReplayContractError("limited_event_contract")
            meaning, business = digest(p), p["business_id"]
            if business in s["operations"]:
                if s["operations"][business] != meaning:
                    raise ReplayContractError("limited_business_conflict")
                return s
            if p["action"] == "phase":
                self.phase(s, p["data"], command, plan.policy)
            elif p["action"] == "extension":
                d = p["data"]
                if (
                    d["parent"] != s["input_head"]
                    or d["packet"] not in identity["catalog"]
                    or (
                        digest(d["rows"]) != identity["catalog"][d["packet"]]
                        or d["packet"] not in command.input_snapshot_hashes
                    )
                ):
                    raise ReplayContractError("limited_extension_parent_or_content")
                if d["packet"] not in s["accepted_packets"]:
                    if any(k in s["inputs"] for k in d["rows"]):
                        raise ReplayContractError("limited_past_revision_forbidden")
                    if any(
                        parse_time(v["fetched_at"]) > command.replayed_at
                        or v["row"]["session"] <= s["decided_through"]
                        for v in d["rows"].values()
                    ):
                        raise ReplayContractError("limited_extension_time_boundary")
                    s["inputs"].update(d["rows"])
                    s["accepted_packets"].append(d["packet"])
                    version = dict(
                        parent=s["input_head"], packet=d["packet"], status="accepted"
                    )
                    s["input_head"] = digest(version)
                    s["versions"][s["input_head"]] = version
            else:
                raise ReplayContractError("limited_unknown_action")
            s["operations"][business] = meaning
            if D(s["cash"]) < reserved(s):
                raise ReplayContractError("limited_cash_conservation")
            for pos in s["positions"].values():
                quantity(pos["shares"])
            return s


def signal_view(state, symbol, end, wall):
    """Adjusted-only detached view. Immutable raw input remains in the ledger."""
    rows = []
    expected = [d for d in state["identity"]["observation_sessions"] if d < end]
    for day in expected:
        item = state["inputs"].get(symbol + "|" + day)
        if item is None:
            raise ReplayContractError("limited_signal_history_missing")
        if parse_time(item["fetched_at"]) > wall:
            raise ReplayContractError("limited_input_not_acquired")
        rows.append(
            dict(
                date=pd.Timestamp(day),
                **dict(
                    zip(
                        ("open", "high", "low", "close", "volume"),
                        map(float, item["adjusted"]),
                        strict=True,
                    )
                ),
            )
        )
    return pd.DataFrame(rows)


class LimitedTrialService:
    def __init__(self, store, plan, authorization, packet_root, evidence_root):
        self.store, self.plan, self.authorization = store, plan, authorization
        self.packet_root, self.evidence_root = Path(packet_root), Path(evidence_root)

    @staticmethod
    def _prepare(plan, authorization, packet_root, evidence_root):
        if type(authorization) is not ScopedResearchAuthorization:
            raise ReplayContractError("limited_approval_required")
        authorization.require(plan)
        diagnosis = LimitedTrialPreflight.evaluate(
            plan, packet_root, evidence_root, authorization
        ).to_dict()
        if not diagnosis["ready"]:
            raise ReplayContractError(
                "limited_trial_not_ready:" + ",".join(diagnosis["reasons"])
            )
        return SavedProxyInputs.load(plan, packet_root, evidence_root)

    @staticmethod
    def _initial(plan, authorization, bundle, style):
        p = plan.payload.to_dict()
        if style not in ("continuous", "split_resume"):
            raise ReplayContractError("limited_repetition_scope")
        rows = bundle.rows.to_dict()
        first = p["history_packets"] + (
            p["packets"] if style == "continuous" else p["packets"][:1]
        )
        signal = plan.signals()
        initial_input = {k: v for k, v in rows.items() if v["packet"] in first}
        v = dict(parent=None, packets=first)
        head = digest(v)
        identity = dict(
            plan=p,
            authorization=authorization.payload.to_dict(),
            plan_hash=plan.sha256,
            style=style,
            source_identity=p["source_identity"],
            run_sessions=list(bundle.sessions),
            symbols=[SCOPE["symbol"]],
            observation_sessions=sorted({v["row"]["session"] for v in rows.values()}),
            candidate_hashes={
                c.candidate_id: signal.config_hash(c.candidate_id)
                for c in signal.catalog.candidates
            },
            catalog={
                sha: digest({k: v for k, v in rows.items() if v["packet"] == sha})
                for sha in p["history_packets"] + p["packets"]
            },
        )
        return JsonObject.from_value(
            dict(
                schema="limited-proxy-state-v1",
                identity=identity,
                capability=dict(
                    **SCOPE,
                    model_approval="unapproved",
                    approval_status=authorization.payload.to_dict()["status"],
                    provenance=p["provenance"],
                ),
                cash="200000",
                equity="200000",
                peak_equity="200000",
                realized_profit="0",
                proceeds_hold="0",
                valuation_complete=True,
                positions={},
                orders={},
                episodes={},
                epochs={},
                batches={},
                marks={},
                decisions={},
                operations={},
                phase_observations={},
                inputs=initial_input,
                input_head=head,
                versions={head: v},
                accepted_packets=first,
                index=0,
                phase="select",
                status="running",
                reason=None,
                selected_before="0001-01-01",
                decided_through="0001-01-01",
            )
        )

    @classmethod
    def create(cls, path, plan, authorization, packet_root, evidence_root, style):
        bundle = cls._prepare(plan, authorization, packet_root, evidence_root)
        if Path(path).exists():
            raise ReplayContractError("limited_new_stream_required")
        initial = cls._initial(plan, authorization, bundle, style)
        identity = StreamIdentity.create(
            stream_id="limited-" + plan.sha256 + "-" + style,
            purpose="draft_audit",
            config_hash=digest(initial.to_dict()["identity"]),
            protocol_hash=plan.sha256,
            source_identity=plan.payload.to_dict()["source_identity"],
            reducer_identity=_LimitedReducer.identity,
            initial_state=initial,
        )
        return cls(
            EventStore.create(path, identity, initial),
            plan,
            authorization,
            packet_root,
            evidence_root,
        )

    @classmethod
    def resume(cls, path, plan, authorization, packet_root, evidence_root, style):
        bundle = cls._prepare(plan, authorization, packet_root, evidence_root)
        initial = cls._initial(plan, authorization, bundle, style)
        identity = StreamIdentity.create(
            stream_id="limited-" + plan.sha256 + "-" + style,
            purpose="draft_audit",
            config_hash=digest(initial.to_dict()["identity"]),
            protocol_hash=plan.sha256,
            source_identity=plan.payload.to_dict()["source_identity"],
            reducer_identity=_LimitedReducer.identity,
            initial_state=initial,
        )
        return cls(
            EventStore.resume(path, identity, _LimitedReducer()),
            plan,
            authorization,
            packet_root,
            evidence_root,
        )

    @property
    def state(self):
        return self.store.read().current_state.to_dict()

    def command(self, records, action, data, business, wall, event_id=None):
        s = records.current_state.to_dict()
        day = s["identity"]["run_sessions"][
            min(s["index"], len(s["identity"]["run_sessions"]) - 1)
        ]
        i = self.store.identity
        return EventCommand(
            i.stream_id,
            i.genesis_hash,
            i.config_hash,
            i.reducer_identity,
            event_id or business,
            "limited." + action,
            datetime.combine(date.fromisoformat(day), datetime.min.time(), UTC),
            wall,
            JsonObject.from_value(
                dict(
                    schema="limited-proxy-event-v1",
                    business_id=business,
                    action=action,
                    data=data,
                )
            ),
            tuple(sorted(s["identity"]["catalog"])),
        )

    def accept(self, sha, extension_id, parent, wall):
        bundle = SavedProxyInputs.load(self.plan, self.packet_root, self.evidence_root)
        records = self.store.read()
        rows = {k: v for k, v in bundle.rows.to_dict().items() if v["packet"] == sha}
        command = self.command(
            records,
            "extension",
            dict(packet=sha, rows=rows, parent=parent),
            "extension:" + extension_id,
            wall,
        )
        return self.store.commit_event(command, records.head, _LimitedReducer())

    def advance(self, wall, *, fault=None):
        SavedProxyInputs.load(self.plan, self.packet_root, self.evidence_root)
        records = self.store.read()
        s, p = records.current_state.to_dict(), self.plan.payload.to_dict()
        if s["status"] in ("completed", "stopped_contract"):
            return s["status"]
        day, result = s["identity"]["run_sessions"][s["index"]], None
        if s["phase"] == "select":
            result = dict(
                boundary=day[:7] + "-01",
                candidate=p["candidate_id"],
                selection_evidence=None,
                selection_mode="predeclared_single_candidate_not_performance_selected",
            )
        elif s["phase"] == "decide" and SCOPE["symbol"] + "|" + day in s["inputs"]:
            symbol = SCOPE["symbol"]
            pos = s["positions"].get(symbol)
            candidate = pos["candidate_id"] if pos else p["candidate_id"]
            decision = self.plan.signals().decide(
                signal_view(
                    s,
                    symbol,
                    (date.fromisoformat(day) + timedelta(days=1)).isoformat(),
                    wall,
                ),
                candidate,
                date.fromisoformat(SCOPE["start"]),
                pos,
            )
            import math

            if not math.isfinite(decision.range_score):
                raise ReplayContractError(
                    "limited_nonfinite_signal_insufficient_history"
                )
            result = {
                symbol: dict(
                    action=decision.action,
                    score=decimal_text(format(decision.range_score, ".12f")),
                    holding_sessions=decision.holding_sessions,
                    breakdown_streak=decision.breakdown_streak,
                    signal_entry_price=decision.signal_entry_price,
                )
            }
        data = dict(
            index=s["index"],
            phase=s["phase"],
            input_head=s["input_head"],
            result=result,
        )
        command = self.command(records, "phase", data, "phase:" + digest(data), wall)
        self.store.commit_event(
            command, records.head, _LimitedReducer(), _fault_hook=fault
        )
        return self.state["status"]

    def run(self, wall):
        while self.advance(wall) == "running":
            pass
        return self.state

    def report(self):
        s = self.state
        fills = sum(o["status"] == "filled" for o in s["orders"].values())
        reasons = sorted(
            {
                o["resolution"]["reason"]
                for o in s["orders"].values()
                if o["status"] == "rejected"
            }
        )
        if not s["decisions"]:
            reasons.append("not_evaluated")
        elif not s["orders"] and s["status"] != "completed":
            reasons.append("processing_incomplete")
        elif not s["orders"]:
            reasons.append(
                "no_signal"
                if not any(
                    d[SCOPE["symbol"]]["action"] == "buy"
                    for d in s["decisions"].values()
                )
                else "insufficient_lot_budget"
            )
        return JsonObject.from_value(
            dict(
                schema="limited-trial-report-v1",
                scope=SCOPE,
                plan_hash=self.plan.sha256,
                capability=s["capability"],
                status=s["status"],
                reason=s["reason"],
                fill_count=fills,
                reasons=reasons,
                input_head=s["input_head"],
                accepted_packets=s["accepted_packets"],
                model_hash=self.plan.payload.to_dict()["model_hash"],
                zero_fill_is_not_clearing_verification=fills == 0,
                formal_checkpoint="unsupported",
            )
        )
