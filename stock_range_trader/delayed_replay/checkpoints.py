"""Read-only Stage 4 ledger adapter; no trading, selection or market requests."""

from dataclasses import dataclass
from decimal import Decimal, localcontext

from .audit_errors import AuditError, IntegrityError, StoreBusy
from .audit_models import Head
from .checkpoint_models import (
    CheckpointEvidence,
    CheckpointSchedule,
    ExternalAbsence,
    FinalizationState,
    exact,
    finalize,
    rendered,
)
from .recovery import StoredRecords, recover_records
from .replay_state import ReplayReducer, ReplayState
from .serialization import JsonObject, digest, time_text
from .validation import ReplayContractError, timestamp


def normalized(value):
    return rendered(value)


def unique_records(records, key):
    """Same ID/same contents is idempotent; conflicting duplicate is corruption."""
    result = {}
    for record in records:
        identity = record[key]
        if not isinstance(identity, str) or not identity:
            raise ReplayContractError("record_id_required")
        if identity in result and result[identity] != record:
            raise ReplayContractError("same_id_different_record")
        result[identity] = record
    return tuple(result[key] for key in sorted(result))


def sample_counts(account, start, end, universe):
    with localcontext() as ctx:
        ctx.prec = 128
        return _sample_counts(account, start, end, universe)


def _sample_counts(account, start, end, universe):
    filled = {
        key: order
        for key, order in account["orders"].items()
        if order["status"] == "filled" and start <= order["fill"]["session"] < end
    }
    symbols = {order["request"]["instrument_id"] for order in filled.values()}
    if any(
        type(order["shares"]) is not int
        or order["shares"] <= 0
        or order["shares"] % 100
        for order in filled.values()
    ):
        raise ReplayContractError("invalid_filled_share_quantity")
    if not symbols <= set(universe):
        raise ReplayContractError("executed_instrument_outside_fixed_universe")
    episodes = {}
    for key, episode in account["episodes"].items():
        position = episode["position"]
        if key != position["episode_id"] or key != position["entry_order_id"]:
            raise ReplayContractError("episode_reference_mismatch")
        exit_id = episode["exit_order_id"]
        exit_order = account["orders"].get(exit_id)
        entry_order = account["orders"].get(position["entry_order_id"])
        if (
            exit_order is None
            or entry_order is None
            or exit_order["status"] != "filled"
            or entry_order["status"] != "filled"
            or exit_order["request"]["side"] != "SELL"
            or entry_order["request"]["side"] != "BUY"
            or exit_order["request"]["instrument_id"] != position["instrument_id"]
            or exit_order["shares"] != position["shares"]
            or exact(episode["net_profit"])
            != exact(exit_order["fill"]["net_amount"]) - exact(position["cost_basis"])
        ):
            raise ReplayContractError("completed_episode_accounting_mismatch")
        if start <= exit_order["fill"]["session"] < end:
            episodes[key] = episode
    return tuple(sorted(symbols)), tuple(sorted(filled)), episodes


def secondary_metrics(equities, expected_sessions, account, episodes):
    """Descriptive only, MDD nonnegative magnitude including the initial peak."""
    with localcontext() as ctx:
        ctx.prec = 64
        initial = exact(account["policy"]["initial_capital"])
        peak, drawdown = initial, Decimal(0)
        for day in sorted(equities):
            equity = exact(equities[day])
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak)
        missing = sorted(set(expected_sessions) - set(equities))
        profits = [exact(item["net_profit"]) for item in episodes.values()]
        wins = sum((p for p in profits if p > 0), Decimal(0))
        losses = -sum((p for p in profits if p < 0), Decimal(0))
        return JsonObject.from_value(
            dict(
                maximum_drawdown=normalized(drawdown) if not missing else None,
                observed_maximum_drawdown=normalized(drawdown),
                maximum_drawdown_reason="missing_valuation_sessions"
                if missing
                else None,
                expected_valuation_count=len(expected_sessions),
                observed_valuation_count=len(equities),
                missing_valuation_sessions=missing,
                completed_episode_count=len(profits),
                open_episode_count=len(account["positions"]),
                net_expectancy=normalized(sum(profits) / len(profits))
                if profits
                else None,
                net_expectancy_reason=None if profits else "no_completed_trades",
                win_rate=normalized(Decimal(sum(p > 0 for p in profits)) / len(profits))
                if profits
                else None,
                win_rate_reason=None if profits else "no_completed_trades",
                profit_factor=normalized(wins / losses) if losses else None,
                profit_factor_reason=None
                if losses
                else "no_losing_trades"
                if wins
                else "undefined_zero_denominator",
            )
        )


@dataclass(frozen=True, slots=True)
class LedgerCheckpointSource:
    """One immutable consistent read, full pure-reducer verification, trusted head."""

    records: StoredRecords
    expected_head: Head

    def __post_init__(self):
        if not isinstance(self.expected_head, Head):
            raise ReplayContractError("expected_head_required")
        recover_records(self.records, ReplayReducer(), expected_head=self.expected_head)
        if self.records.identity.purpose != "synthetic_test":
            raise ReplayContractError("non_synthetic_replay_not_supported")

    @classmethod
    def from_store(cls, store, expected_head):
        return cls(store.read(), expected_head)

    def collect(
        self,
        schedule: CheckpointSchedule,
        policies,
        *,
        now,
        previous=None,
        external_absence=None,
    ):
        timestamp(now, "now")
        initial = ReplayState(self.records.initial_state).to_dict()
        identity = initial["identity"]
        if (
            identity["policy"]["run_start"] != schedule.start.isoformat()
            or identity["calendar_hash"] != schedule.calendar.sha256
        ):
            raise ReplayContractError("checkpoint_schedule_identity_mismatch")
        if set(policies) != {1, 3}:
            raise ReplayContractError("both_finalization_policies_required")
        absence = {} if external_absence is None else external_absence
        for months, proof in absence.items():
            if (
                months not in (1, 3)
                or not isinstance(proof, ExternalAbsence)
                or proof.observed_at > now
            ):
                raise ReplayContractError("invalid_external_absence_evidence")
        # Reconstruct only stored transitions, not ReplayEngine/Evaluator/Selector.
        state = initial
        timeline = []
        for event in self.records.events:
            state = ReplayReducer()(state, event.command)
            if event.command.replayed_at <= now:
                timeline.append((event, state))
        result = []
        for months in (1, 3):
            end = schedule.boundary(months).isoformat()
            start = schedule.start.isoformat()
            expected = schedule.expected(months)
            scoped = [
                (event, s)
                for event, s in timeline
                if start
                <= event.command.market_decision_at.astimezone(expected.close_at.tzinfo)
                .date()
                .isoformat()
                < end
            ]
            account = scoped[-1][1]["account"] if scoped else initial["account"]
            equities = {}
            valuation_event = None
            finished = False
            invalid = False
            monthly = {}
            for event, s in scoped:
                payload = event.command.payload.to_dict()
                if s["status"] == "stopped_contract":
                    invalid = True
                if payload["action"] != "commit":
                    continue
                if (
                    payload["phase"] == "select"
                    and payload["data"]["epoch"] is not None
                ):
                    epoch = payload["data"]["epoch"]
                    monthly[epoch["boundary"]] = epoch["candidate_id"]
                if payload["phase"] == "mark":
                    valuation = s["account"]["valuation"]
                    if valuation["complete"]:
                        equities[valuation["session"]] = valuation["display_equity"]
                        if valuation["session"] == expected.day.isoformat():
                            valuation_event = event
                if (
                    payload["phase"] == "finish"
                    and s["visible"]["session"] == expected.day.isoformat()
                ):
                    finished = True
            symbols, order_ids, episodes = sample_counts(
                account, start, end, identity["universe"]
            )
            reference = dict(
                account_stream_id=self.records.identity.stream_id,
                genesis_hash=self.records.identity.genesis_hash,
                expected_head=dict(
                    sequence=self.expected_head.sequence,
                    event_hash=self.expected_head.event_hash,
                ),
                verified_head_ancestry=[
                    dict(sequence=e.sequence, event_hash=e.event_hash)
                    for e in self.records.events
                ],
                candidate_id=None,
                source_identity_hash=digest({"source": identity["source_identity"]}),
                checkpoint_event_hashes=[e.event_hash for e, _ in scoped],
                valuation_event_hash=None
                if valuation_event is None
                else valuation_event.event_hash,
                filled_order_ids=list(order_ids),
                completed_episode_ids=sorted(episodes),
                episodes_sha256=digest(episodes),
                input_evidence_hashes=sorted(
                    {h for e, _ in scoped for h in e.command.input_snapshot_hashes}
                ),
            )
            refs = JsonObject.from_value(reference)
            if months in absence:
                reference["external_absence"] = {
                    "evidence_id": absence[months].evidence_id,
                    "hash": absence[months].evidence_hash,
                    "reason_code": absence[months].reason_code,
                    "observed_at": time_text(absence[months].observed_at),
                }
                refs = JsonObject.from_value(reference)
            observed = (
                None if valuation_event is None else valuation_event.command.replayed_at
            )
            old = None if previous is None else previous.get(months)
            if (
                old is not None
                and any(e.status != "pending" for e in (old.valuation, old.samples))
                and old.policy != policies[months]
            ):
                raise ReplayContractError("finalized_policy_amendment_not_supported")
            vref = digest(
                dict(
                    event=reference["valuation_event_hash"],
                    invalid=invalid,
                    expected_session=expected.day.isoformat(),
                )
            )
            valuation = finalize(
                value_available=valuation_event is not None,
                external_missing=months in absence,
                invalid=invalid,
                observed_at=observed,
                reference_hash=vref,
                period_end=schedule.ends_at(months),
                policy=policies[months],
                now=now,
                previous=None if old is None else old.valuation,
            )
            sref = digest(
                dict(
                    finished=finished,
                    orders=list(order_ids),
                    episodes=episodes,
                    invalid=invalid,
                )
            )
            samples = finalize(
                value_available=finished,
                external_missing=months in absence,
                invalid=invalid,
                observed_at=scoped[-1][0].command.replayed_at if scoped else None,
                reference_hash=sref,
                period_end=schedule.ends_at(months),
                policy=policies[months],
                now=now,
                previous=None if old is None else old.samples,
            )
            secondary = secondary_metrics(
                equities,
                [s.day.isoformat() for s in schedule.sessions(months)],
                account,
                episodes,
            ).to_dict()
            secondary["monthly_candidates"] = monthly
            secondary["monthly_completed_trades"] = {
                month: sum(
                    account["orders"][e["exit_order_id"]]["fill"]["session"].startswith(
                        month
                    )
                    for e in episodes.values()
                )
                for month in sorted(
                    {s.day.strftime("%Y-%m") for s in schedule.sessions(months)}
                )
            }
            evidence = CheckpointEvidence(
                months,
                schedule.start,
                schedule.boundary(months),
                expected.day,
                None if valuation_event is None else expected.day,
                None
                if valuation_event is None
                else valuation_event.command.market_decision_at,
                policies[months],
                valuation,
                samples,
                account["policy"]["initial_capital"],
                equities.get(expected.day.isoformat())
                if valuation.status == "available"
                else None,
                len(symbols) if samples.status == "available" else None,
                len(episodes) if samples.status == "available" else None,
                "synthetic",
                refs,
                JsonObject.from_value(secondary),
            )
            result.append(evidence)
            if (
                old is not None
                and old.valuation.status != "pending"
                and old.samples.status != "pending"
            ):
                result[-1] = old
        return tuple(result)


def collect_store_checkpoints(
    store,
    schedule,
    policies,
    *,
    expected_head,
    now,
    previous=None,
    external_absence=None,
):
    """Verified read boundary; corrupt audit is INVALID, wrong requested head rejects.

    Busy storage remains an operational error, never an invalid-performance claim.
    No exception text, database paths, or broken account values are exported.
    """
    if not isinstance(expected_head, Head):
        raise ReplayContractError("expected_head_required")
    if (
        store.identity.purpose != "synthetic_test"
        or store.identity.reducer_identity != ReplayReducer.identity
    ):
        raise ReplayContractError("unsupported_checkpoint_stream")
    timestamp(now, "now")
    if set(policies) != {1, 3}:
        raise ReplayContractError("both_finalization_policies_required")
    failed = False
    try:
        records = store.read()
    except StoreBusy:
        raise
    except AuditError:
        failed = True
    if not failed:
        if records.head != expected_head:
            raise IntegrityError("external_head_mismatch")
        try:
            source = LedgerCheckpointSource(records, expected_head)
        except AuditError:
            failed = True
    if not failed:
        return source.collect(
            schedule,
            policies,
            now=now,
            previous=previous,
            external_absence=external_absence,
        )
    refs = JsonObject.from_value(
        dict(
            account_stream_id=store.identity.stream_id,
            genesis_hash=store.identity.genesis_hash,
            expected_head=dict(
                sequence=expected_head.sequence, event_hash=expected_head.event_hash
            ),
            failure_code="audit_verification_failed",
        )
    )
    invalid = FinalizationState(
        "invalid", "audit_verification_failed", now, now, refs.sha256
    )
    return tuple(
        CheckpointEvidence(
            months,
            schedule.start,
            schedule.boundary(months),
            schedule.expected(months).day,
            None,
            None,
            policies[months],
            invalid,
            invalid,
            "200000",
            None,
            None,
            None,
            "synthetic",
            refs,
            JsonObject.from_value({"undefined_reason": "invalid_audit"}),
        )
        for months in (1, 3)
    )
