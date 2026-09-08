"""Thin Stage 2 adapter, not a replay runner or registration service."""

from dataclasses import dataclass

from .account_models import SharedAccountState
from .account_policy import AccountError, AccountPolicy
from .account_reducer import AccountReducer
from .audit_models import EventCommand, Head
from .event_store import EventStore
from .serialization import JsonObject


@dataclass
class AccountService:
    store: EventStore

    def __post_init__(self):
        if (
            self.store.identity.config_hash != self.policy_hash
            or self.store.identity.reducer_identity != AccountReducer.identity
            or self.store.identity.purpose != "synthetic_test"
        ):
            raise AccountError("account_policy_identity_mismatch")

    @property
    def state(self) -> SharedAccountState:
        return SharedAccountState(self.store.read().current_state)

    @property
    def policy_hash(self) -> str:
        return AccountPolicy.from_dict(self.state.to_dict()["policy"]).sha256

    def command(
        self,
        *,
        event_id,
        event_type,
        payload,
        market_decision_at,
        replayed_at,
        input_snapshot_hashes,
    ) -> EventCommand:
        identity = self.store.identity
        return EventCommand(
            stream_id=identity.stream_id,
            stream_identity_hash=identity.genesis_hash,
            config_hash=identity.config_hash,
            reducer_identity=AccountReducer.identity,
            event_id=event_id,
            event_type=event_type,
            market_decision_at=market_decision_at,
            replayed_at=replayed_at,
            payload=JsonObject.from_value(payload),
            input_snapshot_hashes=tuple(sorted(input_snapshot_hashes)),
        )

    def commit(
        self,
        command: EventCommand,
        expected_head: Head,
        *,
        save_snapshot=False,
        _fault_hook=None,
    ):
        return self.store.commit_event(
            command,
            expected_head,
            AccountReducer(),
            save_snapshot=save_snapshot,
            _fault_hook=_fault_hook,
        )
