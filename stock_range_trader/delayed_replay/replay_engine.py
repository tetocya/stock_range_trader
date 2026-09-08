"""Synthetic staged replay orchestration; computations stay outside reducers."""

import math
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from .account_models import OrderRequest, SharedAccountState
from .account_policy import AccountPolicy, M
from .audit_models import EventCommand, StreamIdentity
from .clock import ReplayClock
from .event_store import EventStore
from .execution import ExecutionMarkEvidence
from .market_view import MarketView, WaitingForInput
from .monthly_selection import MonthlySelection
from .replay_calendar import JST, ReplayCalendar, month_boundary, shift_month
from .replay_policy import ReplayPolicy, audit_value, fingerprint
from .replay_state import ReplayReducer, ReplayState
from .serialization import JsonObject, digest, require_hash, require_text, time_text
from .signal_adapter import SignalAdapter
from .validation import ReplayContractError


def money(value):
    """Synthetic numeric input to Stage 3 decimal strings, no account float math."""
    return M(Decimal(str(value)))


@dataclass(frozen=True, slots=True)
class ReplayPlan:
    run_id: str
    source_identity: str
    protocol_hash: str
    policy: ReplayPolicy
    account_policy: AccountPolicy
    calendar: ReplayCalendar
    market: MarketView
    universe: tuple[str, ...]
    lot_evidence_hash: str
    monthly: MonthlySelection
    signals: SignalAdapter

    def __post_init__(self):
        require_text(self.run_id)
        require_text(self.source_identity)
        require_hash(self.protocol_hash)
        require_hash(self.lot_evidence_hash)
        if (
            type(self.universe) is not tuple
            or not self.universe
            or any(not isinstance(s, str) or not s for s in self.universe)
            or len(set(self.universe)) != len(self.universe)
        ):
            raise ReplayContractError("explicit_unique_universe_required")
        object.__setattr__(self, "universe", tuple(sorted(self.universe)))
        if not self.sessions:
            raise ReplayContractError("no_in_run_sessions")
        if (
            self.monthly.policy != self.policy
            or self.monthly.catalog != self.signals.catalog
        ):
            raise ReplayContractError("selection_signal_contract_mismatch")
        # Same strategy/cost convention, but fixed independent Validation capital
        # is deliberately not the changing shared account equity.
        if self.monthly.evaluator.base_config != self.signals.base_config:
            raise ReplayContractError("validation_signal_base_config_mismatch")
        config = self.signals.base_config
        if (
            config.lot_size != 100
            or money(config.slippage_pct) != self.account_policy.slippage_pct
            or money(config.commission_rate) != self.account_policy.commission_rate
        ):
            raise ReplayContractError("fixed_cost_or_lot_contract_mismatch")
        covered = {month_boundary(s.day) for s in self.sessions}
        boundary = self.policy.run_start
        while boundary < self.policy.run_end:
            if boundary not in covered:
                raise ReplayContractError("calendar_month_without_session")
            boundary = shift_month(boundary, 1)

    @property
    def sessions(self):
        return self.calendar.between(self.policy.run_start, self.policy.run_end)

    @property
    def identity(self):
        return dict(
            run_id=self.run_id,
            source_identity=self.source_identity,
            protocol_hash=self.protocol_hash,
            policy=audit_value(self.policy),
            account_policy_hash=self.account_policy.sha256,
            calendar_hash=self.calendar.sha256,
            sessions=audit_value(self.sessions),
            universe=list(self.universe),
            snapshots=self.market.identity,
            selection_hash=self.monthly.identity,
            catalog_hash=fingerprint(self.signals.catalog),
            candidate_configs={
                c.candidate_id: self.signals.config_hash(c.candidate_id)
                for c in self.signals.catalog.candidates
            },
            references=sorted(
                set(
                    self.market.references
                    + (self.lot_evidence_hash, self.account_policy.basis_evidence_hash)
                )
            ),
        )

    def initial(self):
        at = datetime.combine(self.policy.run_start - timedelta(days=1), time.min, JST)
        return ReplayState(
            JsonObject.from_value(
                dict(
                    schema="synthetic-replay-4-1",
                    identity=self.identity,
                    account=SharedAccountState.initial(
                        self.account_policy, time_text(at)
                    ).to_dict(),
                    cursor=dict(index=0, phase="select"),
                    status="running",
                    reason=None,
                    epochs={},
                    position_states={},
                    decisions={},
                    operations={},
                    visible={},
                )
            )
        )

    def stream_identity(self):
        return StreamIdentity.create(
            stream_id=self.run_id,
            purpose="synthetic_test",
            config_hash=digest(self.identity),
            protocol_hash=self.protocol_hash,
            source_identity=self.source_identity,
            reducer_identity=ReplayReducer.identity,
            initial_state=self.initial().value,
        )


class ReplayEngine:
    """One phase per transaction; explicit caller wall clock; no API clients."""

    def __init__(self, store: EventStore, plan: ReplayPlan):
        self.store, self.plan = store, plan
        if store.identity != plan.stream_identity():
            raise ReplayContractError("replay_plan_identity_mismatch")

    @classmethod
    def create(cls, path, plan):
        return cls(
            EventStore.create(
                path,
                plan.stream_identity(),
                plan.initial().value,
                snapshot_initial=True,
            ),
            plan,
        )

    @classmethod
    def resume(cls, path, plan, *, expected_head=None):
        return cls(
            EventStore.resume(
                path,
                plan.stream_identity(),
                ReplayReducer(),
                expected_head=expected_head,
            ),
            plan,
        )

    @property
    def state(self):
        return ReplayState(self.store.read().current_state)

    def command(self, *, replayed_at, action, data, event_id):
        state = self.state.to_dict()
        return self._command(
            state, replayed_at=replayed_at, action=action, data=data, event_id=event_id
        )

    def _command(self, state, *, replayed_at, action, data, event_id):
        index, phase = state["cursor"]["index"], state["cursor"]["phase"]
        session = self.plan.sessions[index]
        market_time = (
            session.selection_at
            if phase == "select"
            else session.open_at
            if phase == "open"
            else session.close_at
        )
        identity = self.store.identity
        return EventCommand(
            stream_id=identity.stream_id,
            stream_identity_hash=identity.genesis_hash,
            config_hash=identity.config_hash,
            reducer_identity=ReplayReducer.identity,
            event_id=event_id,
            event_type="replay.phase",
            market_decision_at=market_time,
            replayed_at=replayed_at,
            payload=JsonObject.from_value(
                dict(index=index, phase=phase, action=action, data=data)
            ),
            input_snapshot_hashes=tuple(self.plan.identity["references"]),
        )

    def commit(self, command, *, fault=None):
        # All externally computed effects and cursor commit together. Stage 2
        # recovery replays only ReplayReducer and the original stored payloads.
        return self.store.commit_event(
            command,
            self.store.read().head,
            ReplayReducer(),
            save_snapshot=command.payload.to_dict()["phase"] == "select",
            _fault_hook=fault,
        )

    def advance(self, replayed_at, *, fault=None):
        if self.store.identity != self.plan.stream_identity():
            raise ReplayContractError("pinned_plan_changed")
        records = self.store.read()
        state = ReplayState(records.current_state).to_dict()
        if state["status"] in ("completed", "stopped_contract"):
            return state["status"]
        index, phase = state["cursor"]["index"], state["cursor"]["phase"]
        session = self.plan.sessions[index]
        market_time = (
            session.selection_at
            if phase == "select"
            else session.open_at
            if phase == "open"
            else session.close_at
        )
        clock = ReplayClock(market_time, replayed_at)
        try:
            data = self._compute(state, session, phase, clock)
            action = "commit"
        except WaitingForInput as error:
            data, action = {"reason": str(error)}, "wait"
        except UnsupportedReplayInput as error:
            data, action = {"reason": str(error)}, "stop"
        # Other exceptions deliberately propagate, never no_eligible_candidate.
        command = self._command(
            state,
            replayed_at=replayed_at,
            action=action,
            data=data,
            event_id=f"phase-{records.head.sequence + 1}",
        )
        self.store.commit_event(
            command,
            records.head,
            ReplayReducer(),
            save_snapshot=phase == "select",
            _fault_hook=fault,
        )
        if action == "wait":
            return "waiting_for_input"
        if action == "stop":
            return "stopped_contract"
        return (
            "completed"
            if phase == "finish" and index + 1 == len(self.plan.sessions)
            else "running"
        )

    def run(self, replayed_at):
        while self.advance(replayed_at) == "running":
            pass
        return self.state

    def _compute(self, state, session, phase, clock):
        plan, account = self.plan, state["account"]
        day = session.day.isoformat()
        if phase == "select":
            boundary = month_boundary(session.day)
            if boundary.isoformat() in state["epochs"]:
                return {"epoch": None}
            start = shift_month(
                boundary, -plan.policy.lookback_months - plan.policy.warmup_months
            )
            bars = plan.market.history(clock, start, boundary, plan.universe)
            return {
                "epoch": plan.monthly.evaluate(
                    bars, boundary, session.selection_at, plan.universe
                ).value.to_dict()
            }
        if phase == "open":
            symbols = set(account["positions"]) | {
                o["request"]["instrument_id"]
                for o in account["orders"].values()
                if o["status"] == "pending" and o["request"]["target_session"] == day
            }
            evidence = plan.market.opens(clock, day, symbols)
            if any(
                e.basis_evidence_hash != plan.account_policy.basis_evidence_hash
                or e.market_available_at != time_text(session.open_at)
                for e in evidence
            ):
                raise ReplayContractError("open_evidence_basis_or_publication_mismatch")
            if any(
                not e.corporate_action_supported
                for e in evidence
                if e.instrument_id in account["positions"]
            ):
                raise UnsupportedReplayInput(
                    "held_unsupported_corporate_action_at_open"
                )
            return {"evidence": [asdict(e) for e in evidence]}
        if phase == "mark":
            visible = plan.market.observations(
                clock,
                session.day,
                session.day + timedelta(days=1),
                account["positions"],
            )
            if {bar.symbol for bar, _ in visible} != set(account["positions"]):
                raise WaitingForInput("incomplete_close_marks")
            marks = []
            for bar, snapshot in visible:
                if bar.stock_split not in (0, 1) or bar.adjustment_factor != 1:
                    raise UnsupportedReplayInput(
                        "held_unsupported_corporate_action_at_close"
                    )
                marks.append(
                    asdict(
                        ExecutionMarkEvidence(
                            instrument_id=bar.symbol,
                            session=day,
                            mark_price=money(bar.raw_ohlcv[3]),
                            provider=snapshot.provider,
                            provider_price_basis=snapshot.provider_price_basis,
                            snapshot_hash=snapshot.payload_sha256,
                            basis_evidence_hash=plan.account_policy.basis_evidence_hash,
                            corporate_action_supported=True,
                            split_ratio="1",
                            purpose="synthetic_test",
                            market_available_at=time_text(bar.market_available_at),
                            quality="complete",
                        )
                    )
                )
            return {"marks": marks}
        if phase in ("prepare", "finish"):
            return {}
        return self._decide(state, session, clock)

    def _decide(self, state, session, clock):
        plan, account = self.plan, state["account"]
        day = session.day.isoformat()
        candidate = state["epochs"][day[:7] + "-01"]["candidate_id"]
        symbols = set(account["positions"]) | (
            set(plan.universe) if candidate is not None else set()
        )
        start = shift_month(
            plan.policy.run_start,
            -plan.policy.lookback_months - plan.policy.warmup_months,
        )
        visible = plan.market.observations(
            clock, start, session.day + timedelta(days=1), symbols
        )
        today = {
            bar.symbol: (bar, snapshot)
            for bar, snapshot in visible
            if bar.session_date == session.day
        }
        if set(today) != symbols:
            raise WaitingForInput("missing_completed_signal_bar")
        next_index = state["cursor"]["index"] + 1
        next_day = (
            plan.sessions[next_index].day.isoformat()
            if next_index < len(plan.sessions)
            else None
        )
        requests, decisions, position_states = [], [], {}
        import pandas as pd

        for symbol in sorted(symbols):
            pos = account["positions"].get(symbol)
            selected = pos["candidate_id"] if pos is not None else candidate
            bar, snapshot = today[symbol]
            if bar.stock_split not in (0, 1) or bar.adjustment_factor != 1:
                if pos is not None:
                    raise UnsupportedReplayInput(
                        "held_unsupported_corporate_action_at_decision"
                    )
                decisions.append(
                    dict(
                        symbol=symbol,
                        action="excluded",
                        reason="unsupported_corporate_action",
                    )
                )
                continue
            history = pd.DataFrame(
                [
                    dict(
                        date=pd.Timestamp(b.session_date),
                        **dict(
                            zip(
                                ("open", "high", "low", "close", "volume"),
                                b.adjusted_ohlcv,
                                strict=True,
                            )
                        ),
                    )
                    for b, _ in visible
                    if b.symbol == symbol
                ]
            )
            meta = state["position_states"].get(symbol)
            decision = plan.signals.decide(
                history, selected, plan.policy.run_start, meta
            )
            if pos is not None:
                position_states[symbol] = {
                    **meta,
                    "holding_sessions": decision.holding_sessions,
                    "breakdown_streak": decision.breakdown_streak,
                    "signal_entry_price": decision.signal_entry_price,
                    "last_decision_session": day,
                }
            pending = any(
                o["status"] == "pending" and o["request"]["instrument_id"] == symbol
                for o in account["orders"].values()
            )
            reason = (
                "pending_order"
                if pending
                else "no_next_bar"
                if next_day is None and decision.action != "hold"
                else decision.exit_reason
            )
            decisions.append(
                dict(
                    symbol=symbol,
                    candidate_id=selected,
                    action=decision.action,
                    reason=reason,
                    range_score=None
                    if not math.isfinite(decision.range_score)
                    else repr(decision.range_score),
                )
            )
            if pending or decision.action == "hold" or next_day is None:
                continue
            config_hash = plan.signals.config_hash(selected)
            requests.append(
                asdict(
                    OrderRequest(
                        order_id=f"{day}:{symbol}:{decision.action}",
                        instrument_id=symbol,
                        side=decision.action.upper(),
                        target_session=next_day,
                        signal_at=time_text(session.close_at),
                        decision_at=time_text(session.close_at),
                        reference_session=day,
                        reference_available_at=time_text(bar.market_available_at),
                        reference_price=money(bar.raw_ohlcv[3]),
                        range_score=M(
                            Decimal(str(decision.range_score)).quantize(
                                Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN
                            )
                        ),
                        candidate_id=selected,
                        entry_config_hash=config_hash,
                        exit_config_hash=config_hash,
                        snapshot_hash=snapshot.payload_sha256,
                        lot_evidence_hash=plan.lot_evidence_hash,
                        lot_size=100,
                        requested_shares=None if pos is None else pos["shares"],
                    )
                )
            )
        return {
            "requests": requests,
            "decisions": decisions,
            "position_states": position_states,
        }


class UnsupportedReplayInput(ReplayContractError):
    """A causal unsupported event stops, without discarding held assets."""
