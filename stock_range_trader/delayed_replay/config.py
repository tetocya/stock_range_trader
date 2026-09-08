"""Draft-only configuration. None means unresolved, never an execution default."""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import date

from .validation import (
    ReplayContractError,
    calendar_date,
    number,
    positive_int,
    text,
)


@dataclass(frozen=True, slots=True)
class ReplayPolicies:
    """Unapproved decisions, including versioned references to complex policies.

    A reference's presence does not prove approval or that its implementation
    exists. Later registration must resolve and verify these references.
    """

    max_position_pct: float | None = None
    max_positions: int | None = None
    lookback_months: int | None = None
    warmup_sessions: int | None = None
    selection_capital: float | None = None
    commission_rate: float | None = None
    slippage_pct: float | None = None
    reservation_buffer_pct: float | None = None
    finalization_grace_sessions: int | None = None
    market_start: date | None = None
    same_open_sale_proceeds_reuse: bool | None = None
    allocation_basis_policy_id: str | None = None
    existing_position_exit_policy_id: str | None = None
    order_priority_policy_id: str | None = None
    reservation_policy_id: str | None = None
    universe_snapshot_id: str | None = None
    calendar_version: str | None = None
    checkpoint_policy_id: str | None = None
    availability_policy_id: str | None = None
    tick_rounding_policy_id: str | None = None
    dividend_policy_id: str | None = None
    corporate_action_policy_id: str | None = None
    missing_price_policy_id: str | None = None
    open_position_valuation_policy_id: str | None = None
    benchmark_policy_id: str | None = None
    candidate_catalog_id: str | None = None
    selection_policy_id: str | None = None
    account_risk_policy_id: str | None = None

    def __post_init__(self) -> None:
        for item in fields(self):
            name = item.name
            value = getattr(self, name)
            if value is None:
                continue
            if name in {
                "max_positions",
                "lookback_months",
                "warmup_sessions",
                "finalization_grace_sessions",
            }:
                positive_int(value, name)
            elif name in {
                "max_position_pct",
                "commission_rate",
                "slippage_pct",
                "reservation_buffer_pct",
            }:
                number(value, name, minimum=0, maximum=1)
                if name == "max_position_pct" and value == 0:
                    raise ReplayContractError("max_position_pct must be positive")
            elif name == "selection_capital":
                number(value, name, minimum=0, maximum=1e15)
                if value == 0:
                    raise ReplayContractError("selection_capital must be positive")
            elif name == "market_start":
                calendar_date(value, name)
            elif name == "same_open_sale_proceeds_reuse":
                if type(value) is not bool:
                    raise ReplayContractError(f"{name} must be bool or None")
            else:
                text(value, name)

    @property
    def unresolved_fields(self) -> tuple[str, ...]:
        return tuple(
            item.name for item in fields(self) if getattr(self, item.name) is None
        )

    def require_complete(self) -> None:
        """Check schema completeness only; never approve, register or start OOS."""
        if self.unresolved_fields:
            raise ReplayContractError(
                "unresolved policies: " + ", ".join(self.unresolved_fields)
            )


@dataclass(frozen=True, slots=True)
class DelayedReplayConfig:
    """Only explicitly confirmed conditions have concrete defaults."""

    execution_mode: str = "delayed_oos_replay"
    provider: str = "jquants"
    subscription: str = "free"
    initial_capital: int = 200_000
    account_model: str = "single_shared"
    lot_size: int = 100
    odd_lots_allowed: bool = False
    reselection_frequency: str = "monthly"
    policies: ReplayPolicies = field(default_factory=ReplayPolicies)

    def __post_init__(self) -> None:
        confirmed = {
            "execution_mode": "delayed_oos_replay",
            "provider": "jquants",
            "subscription": "free",
            "initial_capital": 200_000,
            "account_model": "single_shared",
            "lot_size": 100,
            "odd_lots_allowed": False,
            "reselection_frequency": "monthly",
        }
        for name, expected in confirmed.items():
            actual = getattr(self, name)
            if type(actual) is not type(expected) or actual != expected:
                raise ReplayContractError(f"{name} must be {expected!r}")
        if not isinstance(self.policies, ReplayPolicies):
            raise ReplayContractError("policies must be ReplayPolicies")

    def validate_for_execution(self) -> None:
        """Reject incomplete execution configuration without authorizing a run.

        Passing this schema prerequisite is not approval, price-basis evidence,
        policy-reference resolution, registration, or OOS readiness. Those gates
        and the runner do not exist in Stage 1.
        """
        self.policies.require_complete()

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "DelayedReplayConfig":
        if not isinstance(values, Mapping):
            raise ReplayContractError("config must be a mapping")
        copied = dict(values)
        unknown = set(copied) - {item.name for item in fields(cls)}
        if unknown:
            raise ReplayContractError(
                "unknown config fields: " + repr(sorted(unknown, key=str))
            )
        if "policies" in copied:
            policy_values = copied["policies"]
            if not isinstance(policy_values, Mapping):
                raise ReplayContractError("policies must be a mapping")
            unknown = set(policy_values) - {
                item.name for item in fields(ReplayPolicies)
            }
            if unknown:
                raise ReplayContractError(
                    "unknown policy fields: " + repr(sorted(unknown, key=str))
                )
            copied["policies"] = ReplayPolicies(**policy_values)
        return cls(**copied)
