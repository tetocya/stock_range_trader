"""Artificial research entry and Stage2 transactional orchestration."""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pandas as pd

from delayed_replay.audit_models import EventCommand, StreamIdentity
from delayed_replay.event_store import EventStore
from delayed_replay.serialization import JsonObject, decimal_text, digest
from delayed_replay.signal_adapter import SignalAdapter
from delayed_replay.validation import ReplayContractError

from .input_adapter import ProxyInputStore, ProxyPacket, SyntheticRecipe
from .policy import ASSUMPTIONS, CAPABILITY, DailyOpenProxyPolicy
from .reducer import ProxyReducer, visible_rows


def observation_view(state, wall, end, *, signal):
    """Detached phase-limited view; future rows never enter the pipeline."""
    expected = {
        symbol + "|" + day
        for symbol in state["identity"]["symbols"]
        for day in state["identity"]["observation_sessions"]
        if day < end
    }
    if not expected <= set(state["inputs"]):
        raise ReplayContractError("proxy_incomplete_history")
    rows = []
    for item in visible_rows(state, wall).values():
        if item["session"] >= end:
            continue
        if any(item[k] is None for k in ("open", "high", "low", "close", "volume")):
            raise ReplayContractError("proxy_incomplete_history")
        row = dict(
            date=pd.Timestamp(item["session"]),
            **{k: float(item[k]) for k in ("open", "high", "low", "close", "volume")},
        )
        if not signal:
            row.update(
                symbol=item["symbol"],
                provider="jquants",
                fetched_at=pd.Timestamp(
                    state["inputs"][item["symbol"] + "|" + item["session"]][
                        "fetched_at"
                    ]
                ),
                dividend=0.0,
                stock_split=0.0,
                adjustment_factor=float(item["adjustment_factor"]),
                turnover_value=row["close"] * row["volume"],
            )
            for prefix in ("raw", "adjusted"):
                row.update(
                    {
                        prefix + "_" + k: row[k]
                        for k in ("open", "high", "low", "close", "volume")
                    }
                )
        rows.append((item["symbol"], row))
    return rows


@dataclass(frozen=True)
class ProxyPlan:
    run_id: str
    source_identity: str
    recipe: SyntheticRecipe
    policy: DailyOpenProxyPolicy
    signals: SignalAdapter
    run_sessions: tuple[str, ...]
    fixed_candidate: str
    monthly: object | None = None
    mode: str = "offline_synthetic"

    def __post_init__(self):
        if (
            type(self.recipe) is not SyntheticRecipe
            or type(self.signals) is not SignalAdapter
        ):
            raise ReplayContractError(
                "proxy_artificial_source_and_existing_strategy_required"
            )
        self.policy.gate(self.mode, self.recipe.origin)
        if (
            type(self.run_sessions) is not tuple
            or not self.run_sessions
            or tuple(sorted(set(self.run_sessions))) != self.run_sessions
            or not set(self.run_sessions) <= set(self.recipe.sessions)
        ):
            raise ReplayContractError("proxy_run_calendar")
        self.signals.config(self.fixed_candidate)
        a = self.policy.require()
        cfg = self.signals.base_config
        if (
            float(a.commission_rate) != cfg.commission_rate
            or float(a.slippage_pct) != cfg.slippage_pct
            or cfg.lot_size != 100
        ):
            raise ReplayContractError("proxy_signal_cost_contract")
        if self.monthly is not None and (
            self.monthly.catalog != self.signals.catalog
            or self.monthly.evaluator.base_config != cfg
        ):
            raise ReplayContractError("proxy_monthly_signal_contract")

    def identity(self, packet_hash):
        return dict(
            schema="proxy-plan-v1",
            mode=self.mode,
            origin=self.recipe.origin,
            source_identity=self.source_identity,
            terms=self.policy.terms.to_dict(),
            rules=self.policy.rules.to_dict(),
            policy_hash=self.policy.sha256,
            availability_model_hash=self.policy.availability_hash,
            capability=CAPABILITY,
            assumptions=list(ASSUMPTIONS),
            run_sessions=list(self.run_sessions),
            observation_sessions=list(self.recipe.sessions),
            symbols=list(self.recipe.symbols),
            initial_packet=packet_hash,
            fixed_candidate=self.fixed_candidate,
            monthly_identity=None if self.monthly is None else self.monthly.identity,
            candidate_hashes={
                c.candidate_id: self.signals.config_hash(c.candidate_id)
                for c in self.signals.catalog.candidates
            },
        )

    def initial(self, packet):
        p = packet.payload.to_dict()
        if p["origin"] != self.recipe.origin:
            raise ReplayContractError("proxy_initial_source")
        v = dict(
            parent=None,
            packet=packet.payload.sha256,
            status="initial",
            lane="proxy-input-lane-v1",
        )
        head = digest(v)
        return JsonObject.from_value(
            dict(
                schema="proxy-state-v1",
                capability=CAPABILITY,
                identity=self.identity(packet.payload.sha256),
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
                inputs={
                    k: dict(
                        row=r,
                        packet=packet.payload.sha256,
                        acquired_at=p["recipe"]["acquired_at"],
                        first_observed_at=p["recipe"]["acquired_at"],
                        fetched_at=p["recipe"]["acquired_at"],
                    )
                    for k, r in p["records"].items()
                },
                input_head=head,
                versions={head: v},
                index=0,
                phase="select",
                status="running",
                reason=None,
                selected_before="0001-01-01",
                decided_through="0001-01-01",
            )
        )

    def stream(self, packet):
        initial = self.initial(packet)
        return StreamIdentity.create(
            stream_id=self.run_id,
            purpose="draft_audit",
            config_hash=digest(initial.to_dict()["identity"]),
            protocol_hash=self.policy.sha256,
            source_identity=self.source_identity,
            reducer_identity=ProxyReducer.identity,
            initial_state=initial,
        )


class ProxyService:
    def __init__(self, store, plan, artifacts):
        self.store, self.plan, self.artifacts = store, plan, artifacts

    @classmethod
    def create(cls, path, plan, artifacts, initial_packet):
        from pathlib import Path

        if Path(path).exists():
            raise ReplayContractError("proxy_separate_new_database_required")
        if (
            type(artifacts) is not ProxyInputStore
            or type(initial_packet) is not ProxyPacket
        ):
            raise ReplayContractError("proxy_input_adapter_required")
        artifacts.publish(initial_packet)
        return cls(
            EventStore.create(
                path, plan.stream(initial_packet), plan.initial(initial_packet)
            ),
            plan,
            artifacts,
        )

    @classmethod
    def resume(cls, path, plan, artifacts, initial_hash):
        packet = artifacts.load(initial_hash)
        store = EventStore.resume(path, plan.stream(packet), ProxyReducer())
        result = cls(store, plan, artifacts)
        try:
            result.verify_files(store.read().current_state.to_dict())
        except BaseException:
            store.close()
            raise
        return result

    @property
    def state(self):
        return self.store.read().current_state.to_dict()

    def verify_files(self, state):
        for version in state["versions"].values():
            p = self.artifacts.load(version["packet"]).payload.to_dict()
            if p["origin"] != self.plan.recipe.origin:
                raise ReplayContractError("proxy_stored_source_mismatch")

    def command(self, records, action, data, business_id, wall, event_id=None):
        state = records.current_state.to_dict()
        self.verify_files(state)
        ident = self.store.identity
        day = self.plan.run_sessions[
            min(state["index"], len(self.plan.run_sessions) - 1)
        ]
        # Calendar anchor, NOT an exchange execution/publication timestamp.
        at = datetime.combine(date.fromisoformat(day), datetime.min.time(), UTC)
        refs = {v["packet"] for v in state["versions"].values()}
        if action == "extension":
            refs.add(JsonObject.from_value(data["packet"]).sha256)
        return EventCommand(
            ident.stream_id,
            ident.genesis_hash,
            ident.config_hash,
            ident.reducer_identity,
            event_id or business_id,
            "proxy." + action,
            at,
            wall,
            JsonObject.from_value(
                dict(
                    schema="proxy-event-v1",
                    business_id=business_id,
                    action=action,
                    data=data,
                )
            ),
            tuple(sorted(refs)),
        )

    def accept(self, packet_hash, extension_id, parent, wall, *, fault=None):
        records = self.store.read()
        packet = self.artifacts.load(packet_hash)
        data = dict(parent=parent, packet=packet.payload.to_dict())
        command = self.command(
            records, "extension", data, "extension:" + extension_id, wall
        )
        return self.store.commit_event(
            command, records.head, ProxyReducer(), _fault_hook=fault
        )

    def advance(self, wall, *, fault=None):
        records = self.store.read()
        s = records.current_state.to_dict()
        if s["status"] in ("completed", "stopped_contract"):
            return s["status"]
        self.verify_files(s)
        result = None
        day = self.plan.run_sessions[s["index"]]
        phase = s["phase"]
        if phase == "select":
            month = day[:7] + "-01"
            candidate = self.plan.fixed_candidate
            evidence = None
            if month not in s["epochs"] and self.plan.monthly is not None:
                rows = observation_view(s, wall, month, signal=False)
                frame = pd.DataFrame([r for _, r in rows])
                epoch = self.plan.monthly.evaluate(
                    frame,
                    date.fromisoformat(month),
                    datetime.combine(date.fromisoformat(day), datetime.min.time(), UTC),
                    self.plan.recipe.symbols,
                )
                evidence = epoch.value.to_dict()
                candidate = evidence["candidate_id"]
            result = dict(
                boundary=month,
                candidate=candidate,
                selection_evidence=evidence,
                selection_mode="fixed_test_candidate"
                if self.plan.monthly is None
                else "existing_monthly_selection",
            )
        elif phase == "decide":
            from datetime import timedelta

            current = {
                v["row"]["symbol"]: v["row"]
                for v in s["inputs"].values()
                if v["row"]["session"] == day
            }
            if all(
                sym in current
                and all(
                    current[sym][k] is not None
                    for k in ("open", "high", "low", "close", "volume")
                )
                for sym in self.plan.recipe.symbols
            ):
                view = observation_view(
                    s,
                    wall,
                    (date.fromisoformat(day) + timedelta(days=1)).isoformat(),
                    signal=True,
                )
                result = {}
                for symbol in self.plan.recipe.symbols:
                    pos = s["positions"].get(symbol)
                    candidate = (
                        pos["candidate_id"]
                        if pos
                        else s["epochs"][day[:7] + "-01"]["candidate"]
                    )
                    if candidate is None:
                        result[symbol] = dict(
                            action="hold",
                            score="0",
                            holding_sessions=0,
                            breakdown_streak=0,
                            signal_entry_price=None,
                        )
                        continue
                    frame = pd.DataFrame([r for sym, r in view if sym == symbol])
                    decision = self.plan.signals.decide(
                        frame,
                        candidate,
                        date.fromisoformat(self.plan.run_sessions[0]),
                        pos,
                    )
                    result[symbol] = dict(
                        action=decision.action,
                        score=decimal_text(format(decision.range_score, ".12f"))
                        if math.isfinite(decision.range_score)
                        else "0",
                        holding_sessions=decision.holding_sessions,
                        breakdown_streak=decision.breakdown_streak,
                        signal_entry_price=decision.signal_entry_price,
                    )
        data = dict(
            index=s["index"], phase=phase, input_head=s["input_head"], result=result
        )
        business = "phase:" + str(s["index"]) + ":" + phase + ":" + digest(data)
        command = self.command(records, "phase", data, business, wall)
        self.store.commit_event(
            command, records.head, ProxyReducer(), _fault_hook=fault
        )
        return self.state["status"]

    def run(self, wall):
        while self.advance(wall) == "running":
            pass
        return self.state

    def checkpoint(self):
        raise ReplayContractError("proxy_formal_checkpoint_not_supported")
