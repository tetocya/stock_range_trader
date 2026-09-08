"""Explicit synthetic fixtures, never approved production execution defaults."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from data.price_policy import provider_price_basis
from delayed_replay.account_models import OrderRequest, SharedAccountState
from delayed_replay.account_policy import AccountPolicy
from delayed_replay.account_reducer import AccountReducer
from delayed_replay.account_service import AccountService
from delayed_replay.audit_models import StreamIdentity
from delayed_replay.event_store import EventStore
from delayed_replay.execution import ExecutionMarkEvidence, ExecutionOpenEvidence
from delayed_replay.serialization import time_text


def policy(**changes):
    values = dict(
        purpose="synthetic_test",
        initial_capital="200000",
        lot_size=100,
        max_position_pct="0.1",
        max_positions=5,
        commission_rate="0",
        slippage_pct="0",
        reservation_buffer_pct="0",
        price_quantum="0.01",
        money_quantum="0.01",
        buy_price_rounding="ceiling",
        sell_price_rounding="floor",
        fee_rounding="ceiling",
        reservation_rounding="ceiling",
        amount_rounding="half_even",
        budget_rounding="floor",
        priority_mode="sell_then_score_desc_instrument",
        proceeds_mode="hold_until_later_decision_session",
        fill_mode="all_or_reject",
        position_mode="single_position_full_exit",
        expiry_mode="explicit_target_session_only",
        cost_model="proportional",
        dividend_policy="excluded",
        basis_evidence_hash="f" * 64,
    )
    values.update(changes)
    return AccountPolicy(**values)


def at(day, hour=8):
    return datetime.fromisoformat(f"{day}T{hour:02}:00:00+00:00")


def request(
    order_id="buy-A", instrument="A", day="2024-01-02", target="2024-01-03", **changes
):
    values = dict(
        order_id=order_id,
        instrument_id=instrument,
        side="BUY",
        target_session=target,
        signal_at=time_text(at(day)),
        decision_at=time_text(at(day)),
        reference_session=day,
        reference_available_at=time_text(at(day, 7)),
        reference_price="100",
        range_score="70",
        candidate_id="synthetic-candidate",
        entry_config_hash="a" * 64,
        exit_config_hash="b" * 64,
        snapshot_hash="c" * 64,
        lot_evidence_hash="d" * 64,
        lot_size=100,
        requested_shares=None,
    )
    values.update(changes)
    return OrderRequest(**values)


def evidence(instrument, day, price="100", *, mark=False, **changes):
    values = dict(
        instrument_id=instrument,
        session=day,
        provider="jquants",
        provider_price_basis=provider_price_basis("jquants"),
        snapshot_hash="e" * 64,
        basis_evidence_hash="f" * 64,
        corporate_action_supported=True,
        split_ratio="1",
        purpose="synthetic_test",
        market_available_at=time_text(at(day, 7 if mark else 0)),
    )
    if mark:
        values.update(mark_price=price, quality="complete")
        values.update(changes)
        return ExecutionMarkEvidence(**values)
    values.update(open_price=price, tradability="synthetic_open_executable")
    values.update(changes)
    return ExecutionOpenEvidence(**values)


class Harness:
    def __init__(self, path, explicit_policy=None):
        self.policy = explicit_policy or policy()
        initial = SharedAccountState.initial(
            self.policy, time_text(at("2024-01-01", 7))
        )
        self.identity = StreamIdentity.create(
            stream_id="synthetic-account",
            purpose="synthetic_test",
            config_hash=self.policy.sha256,
            protocol_hash="9" * 64,
            source_identity="synthetic-account-tests",
            reducer_identity=AccountReducer.identity,
            initial_state=initial.value,
        )
        self.path = path
        self.store = EventStore.create(
            path, self.identity, initial.value, snapshot_initial=True
        )
        self.service = AccountService(self.store)
        self.count = 0
        self.last_command = None

    @property
    def state(self):
        state = self.service.state
        # Validate conservation and reservations after every inspected transition.
        assert state.available_cash >= 0
        return state.to_dict()

    def send(self, kind, payload, day, hour=8, *, save_snapshot=True, fault=None):
        self.count += 1
        payload = dict(payload)
        payload.setdefault("operation_id", f"op-{self.count}")
        cmd = self.service.command(
            event_id=f"event-{self.count}",
            event_type="account." + kind,
            payload=payload,
            market_decision_at=at(day, hour),
            replayed_at=datetime(2024, 9, 1, tzinfo=UTC)
            + timedelta(seconds=self.count),
            input_snapshot_hashes=("c" * 64, "d" * 64, "e" * 64),
        )
        self.last_command = cmd
        result = self.service.commit(
            cmd, self.store.read().head, save_snapshot=save_snapshot, _fault_hook=fault
        )
        _ = self.state  # Assert accounting invariants after every transition.
        return result

    def submit(self, *requests):
        return self.send(
            "submit",
            {"requests": [asdict(r) for r in requests]},
            requests[0].decision_at[:10],
        )

    def execute(self, day, *proofs, **kw):
        ids = [
            key
            for key, o in self.state["orders"].items()
            if o["status"] == "pending" and o["request"]["target_session"] == day
        ]
        return self.send(
            "execute",
            {"session": day, "order_ids": ids, "evidence": [asdict(e) for e in proofs]},
            day,
            0,
            **kw,
        )

    def mark(self, day, *proofs):
        return self.send(
            "mark", {"session": day, "marks": [asdict(e) for e in proofs]}, day, 7
        )

    def reopen(self):
        self.store.close()
        self.store = EventStore.resume(self.path, self.identity, AccountReducer())
        self.service = AccountService(self.store)

    def close(self):
        self.store.close()
