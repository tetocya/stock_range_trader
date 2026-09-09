"""Versioned input acceptance in one account/cursor ledger, research-only."""

from dataclasses import dataclass, replace
from datetime import date

from .audit_models import EventCommand, StreamIdentity
from .clock import ReplayClock
from .event_store import EventStore
from .input_artifacts import InputArtifactStore, InputPacket
from .market_view import MarketView, WaitingForInput
from .replay_engine import ReplayEngine, UnsupportedReplayInput
from .replay_state import ReplayReducer, ReplayState
from .serialization import (
    JsonObject,
    digest,
    parse_time,
    require_hash,
    require_text,
    time_text,
)
from .validation import ReplayContractError

INPUT_CAPABILITY = {
    "mode": "research_only",
    "model_approval": "unapproved",
    "fill_kind": "simulated_fill",
    "auction_execution_evidence": "unavailable",
    "daily_open_execution": "unsupported",
    "registration_status": "draft_not_registered",
}


@dataclass(frozen=True, slots=True)
class InputVersion:
    payload: JsonObject
    version_hash: str

    def __post_init__(self):
        p = self.payload.to_dict()
        if (
            self.version_hash != self.payload.sha256
            or set(p)
            != {
                "schema",
                "parent",
                "packet",
                "status",
                "accepted_at",
                "rows",
                "references",
            }
            or p["schema"] != "input-version-7a-1"
        ):
            raise ReplayContractError("input_version_integrity")
        if p["parent"] is not None:
            require_hash(p["parent"])
        require_hash(p["packet"])
        if p["status"] not in (
            "initial",
            "accepted",
            "refetched",
            "quarantined_revision",
            "quarantined_past",
        ):
            raise ReplayContractError("input_version_status")
        if p["accepted_at"] is not None:
            parse_time(p["accepted_at"])
        for key, value in p["rows"].items():
            lane, symbol, day = key.split("|")
            if lane not in ("open", "price"):
                raise ReplayContractError("input_lane")
            require_text(symbol)
            date.fromisoformat(day)
            require_hash(value)
        for value in p["references"]:
            require_hash(value)


@dataclass(frozen=True, slots=True)
class InputExtensionEvent:
    extension_id: str
    parent_version: str
    packet_hash: str
    accepted_at: str

    def __post_init__(self):
        require_text(self.extension_id)
        require_hash(self.parent_version)
        require_hash(self.packet_hash)
        parse_time(self.accepted_at)


@dataclass(frozen=True, slots=True)
class _ResolvedMarketView(MarketView):
    """Per-row ownership, retaining the original immutable snapshot hash."""

    owners: tuple[tuple[str, str], ...]

    def __post_init__(self):
        for snapshot in self.snapshots + self.open_snapshots:
            replace(snapshot)

    def observations(self, clock, start, end, symbols):
        owners = dict(self.owners)
        result = []
        for snapshot in self.snapshots:
            selected = tuple(
                s
                for s in symbols
                if any(
                    b.symbol == s
                    and start <= b.session_date < end
                    and owners.get(f"price|{b.symbol}|{b.session_date}")
                    == snapshot.payload_sha256
                    for b in snapshot.observations
                )
            )
            for bar, source in MarketView((snapshot,), ()).observations(
                clock, start, end, selected
            ):
                if (
                    owners.get(f"price|{bar.symbol}|{bar.session_date}")
                    == snapshot.payload_sha256
                ):
                    result.append((bar, source))
        return tuple(
            sorted(result, key=lambda item: (item[0].symbol, item[0].session_date))
        )

    def opens(self, clock, session, symbols):
        owners = dict(self.owners)
        result = []
        for snapshot in self.open_snapshots:
            chosen = tuple(
                e
                for e in snapshot.evidence
                if e.session == session
                and e.instrument_id in symbols
                and owners.get(f"open|{e.instrument_id}|{e.session}") == snapshot.sha256
            )
            if chosen:
                result.extend(
                    MarketView((), (replace(snapshot, evidence=chosen),)).opens(
                        clock, session, {e.instrument_id for e in chosen}
                    )
                )
        if {e.instrument_id for e in result} != set(symbols):
            raise WaitingForInput("missing_explicit_open_evidence")
        return tuple(sorted(result, key=lambda e: e.instrument_id))


def descriptor(packet):
    return dict(
        packet=packet.payload.sha256,
        rows=packet.rows(),
        references=list(packet.market.references),
        receipts=[
            dict(
                first_observed_at=time_text(s.first_observed_at),
                fetched_at=time_text(s.fetched_at),
            )
            for s in packet.market.snapshots + packet.market.open_snapshots
        ],
    )


def classify(rows, state):
    existing = state["inputs"]["rows"]
    replay = state["replay"]
    # Selection histories are immutable even before the first trade decision.
    selected_before = max(replay["epochs"], default="0001-01-01")
    decided_through = max(replay["decisions"], default="0001-01-01")
    opened_through = max(replay["account"]["executed_sessions"], default="0001-01-01")
    for key, value in rows.items():
        if key in existing and existing[key]["semantic_hash"] != value:
            return "quarantined_revision"
    new = set(rows) - set(existing)
    for key in new:
        lane, _, day = key.split("|")
        if (lane == "price" and (day < selected_before or day <= decided_through)) or (
            lane == "open" and day <= opened_through
        ):
            return "quarantined_past"
    return "accepted" if new else "refetched"


class InputReplayReducer:
    identity = "input-replay-reducer-7a-1"

    def __call__(self, raw, command):
        state = JsonObject.from_value(raw).to_dict()
        if (
            state["schema"] != "input-replay-7a-1"
            or command.reducer_identity != self.identity
            or command.config_hash != state["base_identity_hash"]
        ):
            raise ReplayContractError("input_replay_identity")
        if state["capability"] != INPUT_CAPABILITY:
            raise ReplayContractError("unapproved_input_model")
        replay = state["replay"]
        ReplayState(JsonObject.from_value(replay))
        p = command.payload.to_dict()
        if command.event_type == "input.extension":
            event = InputExtensionEvent(**p["event"])
            if event.extension_id in state["extensions"]:
                if state["extensions"][event.extension_id] != p:
                    raise ReplayContractError("extension_id_conflict")
                return state
            if (
                event.parent_version != state["inputs"]["head"]
                or time_text(command.replayed_at) != event.accepted_at
            ):
                raise ReplayContractError("input_parent_or_time_conflict")
            d = p["descriptor"]
            if event.packet_hash != d["packet"]:
                raise ReplayContractError("extension_packet_mismatch")
            if any(
                parse_time(r["first_observed_at"]) > parse_time(r["fetched_at"])
                or parse_time(r["fetched_at"]) > command.replayed_at
                for r in d["receipts"]
            ):
                raise ReplayContractError("input_not_yet_observed")
            for key in d["rows"]:
                if key.split("|")[1] not in replay["identity"]["universe"]:
                    raise ReplayContractError("unknown_extension_symbol")
            status = classify(d["rows"], state)
            v = JsonObject.from_value(
                dict(
                    schema="input-version-7a-1",
                    parent=event.parent_version,
                    packet=d["packet"],
                    status=status,
                    accepted_at=event.accepted_at,
                    rows=d["rows"],
                    references=d["references"],
                )
            )
            version = InputVersion(v, v.sha256)
            state["inputs"]["versions"][version.version_hash] = v.to_dict()
            state["inputs"]["head"] = version.version_hash
            if status == "accepted":
                for key, value in d["rows"].items():
                    state["inputs"]["rows"].setdefault(
                        key, dict(semantic_hash=value, packet=d["packet"])
                    )
                state["inputs"]["references"] = sorted(
                    set(state["inputs"]["references"] + d["references"])
                )
            state["extensions"][event.extension_id] = p
        elif command.event_type == "input.phase":
            if p["input_head"] != state["inputs"]["head"]:
                raise ReplayContractError("phase_input_head_mismatch")
            # Temporary reference expansion, never a mutation of the base plan.
            original_identity = replay["identity"]
            replay["identity"] = {
                **original_identity,
                "references": sorted(
                    set(original_identity["references"] + state["inputs"]["references"])
                ),
            }
            inner = replace(
                command,
                event_type="replay.phase",
                reducer_identity=ReplayReducer.identity,
                config_hash=digest(replay["identity"]),
                payload=JsonObject.from_value(p["phase"]),
                input_snapshot_hashes=tuple(replay["identity"]["references"]),
            )
            replay = ReplayReducer()(replay, inner)
            replay["identity"] = original_identity
            state["replay"] = replay
            # Freeze complete month-end measurements in the same transaction.
            phase = p["phase"]
            if phase["phase"] == "finish" and phase["action"] == "commit":
                from .replay_calendar import shift_month

                start = date.fromisoformat(original_identity["policy"]["run_start"])
                sessions = original_identity["sessions"]
                for months in (1, 3):
                    boundary = shift_month(start, months).isoformat()
                    if boundary > original_identity["policy"]["run_end"]:
                        continue
                    days = [s["day"] for s in sessions if s["day"] < boundary]
                    if days and replay["visible"]["session"] == max(days):
                        from .checkpoints import sample_counts

                        account = replay["account"]
                        symbols, orders, episodes = sample_counts(
                            account,
                            start.isoformat(),
                            boundary,
                            tuple(original_identity["universe"]),
                        )
                        measurement = dict(
                            schema="input-checkpoint-measurement-7a-1",
                            months=months,
                            boundary=boundary,
                            session=max(days),
                            equity=account["valuation"]["display_equity"],
                            unique_symbols=len(symbols),
                            completed_trades=len(episodes),
                            order_ids=list(orders),
                            input_head=p["input_head"],
                            event_id=command.event_id,
                            synthetic=True,
                            registered=False,
                        )
                        old = state["measurements"].setdefault(str(months), measurement)
                        if old != measurement:
                            raise ReplayContractError("frozen_measurement_changed")
        else:
            raise ReplayContractError("unknown_input_event")
        return state


def initial_state(plan, packet):
    d = descriptor(packet)
    v = JsonObject.from_value(
        dict(
            schema="input-version-7a-1",
            parent=None,
            packet=d["packet"],
            status="initial",
            accepted_at=None,
            rows=d["rows"],
            references=d["references"],
        )
    )
    InputVersion(v, v.sha256)
    return JsonObject.from_value(
        dict(
            schema="input-replay-7a-1",
            capability=INPUT_CAPABILITY,
            base_identity_hash=digest(plan.identity),
            replay=plan.initial().to_dict(),
            inputs=dict(
                head=v.sha256,
                versions={v.sha256: v.to_dict()},
                rows={
                    k: dict(semantic_hash=h, packet=d["packet"])
                    for k, h in d["rows"].items()
                },
                references=d["references"],
            ),
            extensions={},
            measurements={},
        )
    )


class ExtensibleReplayEngine(ReplayEngine):
    """Explicit synthetic opt-in; Stage 4 streams and public methods unchanged.

    Reuses Stage 4's protected _compute/_decide methods only. No daily-open proxy
    is enabled. Real daily observations cannot enter synthetic OpenSnapshot.
    """

    def __init__(self, store, base_plan, artifacts):
        self.store, self.base_plan, self.artifacts = store, base_plan, artifacts
        self.plan = base_plan

    @classmethod
    def _identity(cls, plan):
        packet = InputPacket(plan.market)
        initial = initial_state(plan, packet)
        identity = StreamIdentity.create(
            stream_id=plan.run_id,
            purpose="synthetic_test",
            config_hash=digest(plan.identity),
            protocol_hash=plan.protocol_hash,
            source_identity=plan.source_identity,
            reducer_identity=InputReplayReducer.identity,
            initial_state=initial,
        )
        return packet, initial, identity

    @classmethod
    def create(cls, path, plan, artifacts):
        if not isinstance(artifacts, InputArtifactStore):
            raise ReplayContractError("artifact_store_required")
        packet, initial, identity = cls._identity(plan)
        artifacts.publish(packet)
        store = EventStore.create(path, identity, initial, snapshot_initial=True)
        return cls(store, plan, artifacts)

    @classmethod
    def resume(cls, path, plan, artifacts, *, expected_head=None):
        _, _, identity = cls._identity(plan)
        store = EventStore.resume(
            path, identity, InputReplayReducer(), expected_head=expected_head
        )
        engine = cls(store, plan, artifacts)
        try:
            engine._verified_market()
        except Exception:
            store.close()
            raise
        return engine

    @property
    def state(self):
        return ReplayState(
            JsonObject.from_value(self.store.read().current_state.to_dict()["replay"])
        )

    @property
    def input_head(self):
        return self.store.read().current_state.to_dict()["inputs"]["head"]

    def _verified_market(self, records=None):
        records = self.store.read() if records is None else records
        root = records.current_state.to_dict()
        if digest(self.base_plan.identity) != records.identity.config_hash:
            raise ReplayContractError("base_plan_changed")
        packets = {}
        for version_hash, raw in root["inputs"]["versions"].items():
            InputVersion(JsonObject.from_value(raw), version_hash)
            packet = self.artifacts.load(raw["packet"])
            packets[raw["packet"]] = packet
            if (
                packet.rows() != raw["rows"]
                or list(packet.market.references) != raw["references"]
            ):
                raise ReplayContractError("accepted_file_descriptor_mismatch")
        for raw in root["extensions"].values():
            if descriptor(packets[raw["event"]["packet_hash"]]) != raw["descriptor"]:
                raise ReplayContractError("accepted_receipt_mismatch")
        prices, opens, owners = [], [], []
        index = root["inputs"]["rows"]
        # Keep only the original owner of each semantic row, not the last fetch.
        for sha, packet in packets.items():
            for snapshot in packet.market.snapshots:
                bars = tuple(
                    b
                    for b in snapshot.observations
                    if index.get(f"price|{b.symbol}|{b.session_date}", {}).get("packet")
                    == sha
                )
                if bars:
                    prices.append(snapshot)
                    owners.extend(
                        (f"price|{b.symbol}|{b.session_date}", snapshot.payload_sha256)
                        for b in bars
                    )
            for snapshot in packet.market.open_snapshots:
                evidence = tuple(
                    e
                    for e in snapshot.evidence
                    if index.get(f"open|{e.instrument_id}|{e.session}", {}).get(
                        "packet"
                    )
                    == sha
                )
                if evidence:
                    opens.append(snapshot)
                    owners.extend(
                        (f"open|{e.instrument_id}|{e.session}", snapshot.sha256)
                        for e in evidence
                    )
        return _ResolvedMarketView(tuple(prices), tuple(opens), tuple(owners))

    def accept(self, event: InputExtensionEvent, *, fault=None):
        if not isinstance(event, InputExtensionEvent):
            raise ReplayContractError("typed_extension_required")
        records = self.store.read()
        self._verified_market(records)
        packet = self.artifacts.load(
            event.packet_hash
        )  # Never accepts an unsaved file.
        root = records.current_state.to_dict()
        payload = JsonObject.from_value(
            dict(
                event={k: getattr(event, k) for k in event.__dataclass_fields__},
                descriptor=descriptor(packet),
            )
        )
        old = root["extensions"].get(event.extension_id)
        if old is not None:
            if old != payload.to_dict():
                raise ReplayContractError("extension_id_conflict")
            return self.store.lookup("extension:" + event.extension_id)
        if event.parent_version != root["inputs"]["head"]:
            raise ReplayContractError("stale_input_parent")
        # Structural/basis/symbol checks reject the WHOLE batch before acceptance.
        for key in packet.rows():
            _, symbol, day = key.split("|")
            if symbol not in self.base_plan.universe or date.fromisoformat(day) not in {
                s.day for s in self.base_plan.calendar.sessions
            }:
                raise ReplayContractError("extension_symbol_or_calendar_mismatch")
        for s in packet.market.open_snapshots:
            if any(
                e.basis_evidence_hash
                != self.base_plan.account_policy.basis_evidence_hash
                or e.purpose != "synthetic_test"
                for e in s.evidence
            ):
                raise ReplayContractError("extension_open_basis_mismatch")
        previous = (
            records.events[-1].command.market_decision_at
            if records.events
            else self.base_plan.sessions[0].selection_at
        )
        command = EventCommand(
            stream_id=records.identity.stream_id,
            stream_identity_hash=records.identity.genesis_hash,
            config_hash=records.identity.config_hash,
            reducer_identity=InputReplayReducer.identity,
            event_id="extension:" + event.extension_id,
            event_type="input.extension",
            market_decision_at=previous,
            replayed_at=parse_time(event.accepted_at),
            payload=payload,
            input_snapshot_hashes=(event.packet_hash,),
        )
        return self.store.commit_event(
            command, records.head, InputReplayReducer(), _fault_hook=fault
        )

    def advance(self, replayed_at, *, fault=None):
        records = self.store.read()
        self.plan = replace(self.base_plan, market=self._verified_market(records))
        root = records.current_state.to_dict()
        state = root["replay"]
        if records.events and replayed_at < records.events[-1].command.replayed_at:
            raise ReplayContractError("replay_clock_before_accepted_input")
        if state["status"] in ("completed", "stopped_contract"):
            return state["status"]
        index, phase = state["cursor"]["index"], state["cursor"]["phase"]
        session = self.plan.sessions[index]
        market_at = (
            session.selection_at
            if phase == "select"
            else session.open_at
            if phase == "open"
            else session.close_at
        )
        clock = ReplayClock(market_at, replayed_at)
        try:
            data, action = self._compute(state, session, phase, clock), "commit"
        except WaitingForInput as error:
            data, action = {"reason": str(error)}, "wait"
        except UnsupportedReplayInput as error:
            data, action = {"reason": str(error)}, "stop"
        p = dict(index=index, phase=phase, action=action, data=data)
        command = EventCommand(
            stream_id=records.identity.stream_id,
            stream_identity_hash=records.identity.genesis_hash,
            config_hash=records.identity.config_hash,
            reducer_identity=InputReplayReducer.identity,
            event_id=f"phase-{records.head.sequence + 1}",
            event_type="input.phase",
            market_decision_at=market_at,
            replayed_at=replayed_at,
            payload=JsonObject.from_value(
                dict(input_head=root["inputs"]["head"], phase=p)
            ),
            input_snapshot_hashes=tuple(sorted(set(self.plan.market.references))),
        )
        self.store.commit_event(
            command,
            records.head,
            InputReplayReducer(),
            save_snapshot=phase == "select",
            _fault_hook=fault,
        )
        return self.state.to_dict()["status"]

    def commit(self, *args, **kwargs):
        raise ReplayContractError("use_accept_or_advance_for_versioned_stream")
