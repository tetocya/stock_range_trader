"""Pure C1 contracts for a future live historical-feasibility sender.

This module defines account identity, canonical ledger location, exclusive-use,
clock/lease and manual-reclaim rules. It does not read credentials, open a
network connection, create the canonical store, or authorize live acquisition.
Existing localhost/artificial contracts and SQLite data retain their old meaning.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .http_contract import (
    HTTP_OUTPUT_ROOT,
    MAX_WAIT_SECONDS,
    AccountRatePolicy,
    HttpAcquisitionPlan,
    HttpContractError,
)

LIVE_ACCOUNT_IDENTITY_SCHEMA = "historical-feasibility-live-account-identity-v1"
LIVE_ACCOUNT_REGISTRY_SCHEMA = "historical-feasibility-live-account-registry-v1"
LIVE_ACCOUNT_PREFLIGHT_SCHEMA = "historical-feasibility-live-account-preflight-v1"
LIVE_LEDGER_FILENAME = "account-rate-ledger.sqlite3"

_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_ENV_REFERENCE = re.compile(r"env:[A-Z_][A-Z0-9_]*\Z")


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _label(value: object, name: str) -> str:
    if type(value) is not str or not _LABEL.fullmatch(value):
        raise HttpContractError(f"{name}_invalid")
    return value


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or len(value) > 256:
        raise HttpContractError(f"{name}_must_be_nonempty_text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HttpContractError(f"{name}_contains_control_character")
    return value


def _utc(value: object, name: str) -> str:
    if type(value) is not datetime or value.tzinfo is None:
        raise HttpContractError(f"{name}_must_be_aware_datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _bounded_seconds(value: object, name: str, *, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        suffix = "nonnegative" if allow_zero else "positive"
        raise HttpContractError(f"{name}_must_be_{suffix}_integer")
    if value > MAX_WAIT_SECONDS:
        raise HttpContractError(f"{name}_out_of_range")
    return value


def _canonical_owner_store_root(value: object) -> str:
    """Validate the declared root without claiming physical path custody."""

    if type(value) is not str or not value:
        raise HttpContractError("canonical_store_root_must_be_absolute")
    if not value.startswith("/"):
        raise HttpContractError("canonical_store_root_must_be_absolute")
    if value.startswith("//") or value != os.path.normpath(value):
        raise HttpContractError("canonical_store_root_must_be_absolute_canonical_path")
    raw = Path(value)
    if raw.parent == raw:
        raise HttpContractError("canonical_store_root_too_broad")
    checkout_root = Path(os.path.abspath(__file__)).parents[2]
    forbidden_roots = (
        checkout_root,
        checkout_root / "stock_range_trader",
        HTTP_OUTPUT_ROOT.parent,
        Path("/tmp"),
        Path("/var/tmp"),
        Path("/private/tmp"),
        Path("/private/var/folders"),
    )
    for forbidden in forbidden_roots:
        if raw == forbidden or raw.is_relative_to(forbidden):
            raise HttpContractError("canonical_store_root_inside_forbidden_root")
    return value


@dataclass(frozen=True)
class LiveAccountIdentityRecord:
    """Owner-registered identity claim, not proof of authenticated account identity.

    ``credential_reference`` names an environment variable, never its value.
    """

    record_id: str
    account_ref: str
    credential_reference: str
    registered_at: datetime
    evidence_reference: str
    account_scope: str = "all_credentials_for_authenticated_account"
    store_scope: str = "owner_managed_outside_worktrees"
    identity_verified: bool = field(default=False, init=False)
    secret_material_persisted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _label(self.record_id, "identity_record_id")
        _label(self.account_ref, "account_ref")
        if type(self.credential_reference) is not str or not _ENV_REFERENCE.fullmatch(
            self.credential_reference
        ):
            raise HttpContractError("credential_reference_invalid")
        _utc(self.registered_at, "identity_registered_at")
        _text(self.evidence_reference, "identity_evidence_reference")
        if self.account_scope != "all_credentials_for_authenticated_account":
            raise HttpContractError("live_account_scope_invalid")
        if self.store_scope != "owner_managed_outside_worktrees":
            raise HttpContractError("live_store_scope_invalid")

    @property
    def sha256(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict:
        return {
            "schema": LIVE_ACCOUNT_IDENTITY_SCHEMA,
            "record_id": self.record_id,
            "account_ref": self.account_ref,
            "credential_reference": self.credential_reference,
            "registered_at": _utc(self.registered_at, "identity_registered_at"),
            "evidence_reference": self.evidence_reference,
            "account_scope": self.account_scope,
            "store_scope": self.store_scope,
            "identity_verified": False,
            "secret_material_persisted": False,
        }


@dataclass(frozen=True)
class LiveAccountIdentityRegistry:
    """One immutable owner snapshot; authenticity remains an I1 responsibility."""

    canonical_store_root: str
    active_records: tuple[LiveAccountIdentityRecord, ...]
    fixed_at: datetime
    decision_reference: str

    def __post_init__(self) -> None:
        _canonical_owner_store_root(self.canonical_store_root)
        fixed_at = _utc(self.fixed_at, "live_registry_fixed_at")
        _text(self.decision_reference, "live_registry_decision_reference")
        if type(self.active_records) is not tuple or not self.active_records:
            raise HttpContractError("live_registry_active_records_required")
        seen: set[str] = set()
        for record in self.active_records:
            if type(record) is not LiveAccountIdentityRecord:
                raise HttpContractError("live_account_identity_record_required")
            if record.account_ref in seen:
                raise HttpContractError("live_registry_duplicate_account_ref")
            if _utc(record.registered_at, "identity_registered_at") > fixed_at:
                raise HttpContractError("live_registry_before_identity_registration")
            seen.add(record.account_ref)

    def resolve(self, account_ref: str) -> LiveAccountIdentityRecord:
        for record in self.active_records:
            if record.account_ref == account_ref:
                return record
        raise HttpContractError("live_registry_account_ref_not_active")

    def canonical_ledger_path(self, account_ref: str) -> str:
        self.resolve(account_ref)
        return str(
            Path(self.canonical_store_root)
            / "accounts"
            / account_ref
            / LIVE_LEDGER_FILENAME
        )

    @property
    def sha256(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict:
        return {
            "schema": LIVE_ACCOUNT_REGISTRY_SCHEMA,
            "canonical_store_root": self.canonical_store_root,
            "active_records": [record.to_dict() for record in self.active_records],
            "fixed_at": _utc(self.fixed_at, "live_registry_fixed_at"),
            "decision_reference": self.decision_reference,
        }


@dataclass(frozen=True)
class LiveAccountOperatingPolicy:
    """Owner decision: all clients for the authenticated account are exclusive."""

    account_scope: str = "all_credentials_for_authenticated_account"
    sender_topology: str = "single_canonical_sender"
    external_client_policy: str = "prohibited_during_live_acquisition"
    violation_policy: str = "hold_and_stop"

    def __post_init__(self) -> None:
        expected = {
            "account_scope": "all_credentials_for_authenticated_account",
            "sender_topology": "single_canonical_sender",
            "external_client_policy": "prohibited_during_live_acquisition",
            "violation_policy": "hold_and_stop",
        }
        if any(getattr(self, name) != value for name, value in expected.items()):
            raise HttpContractError("live_account_operating_policy_invalid")


@dataclass(frozen=True)
class LiveClockPolicy:
    """Clock and lease invariants for the future persistent live sender."""

    max_reservation_to_send_seconds: int
    termination_guard_seconds: int
    persistent_clock_source: str = "store_utc"
    runtime_timeout_clock_source: str = "monotonic"
    caller_event_time_policy: str = "forbidden"
    reclaim_policy: str = "manual_evidence_only"
    clock_anomaly_policy: str = "hold_for_manual_recovery"
    restart_policy: str = "hold_for_manual_recovery"

    def __post_init__(self) -> None:
        _bounded_seconds(
            self.max_reservation_to_send_seconds,
            "max_reservation_to_send_seconds",
            allow_zero=True,
        )
        _bounded_seconds(self.termination_guard_seconds, "termination_guard_seconds")
        expected = {
            "persistent_clock_source": "store_utc",
            "runtime_timeout_clock_source": "monotonic",
            "caller_event_time_policy": "forbidden",
            "reclaim_policy": "manual_evidence_only",
            "clock_anomaly_policy": "hold_for_manual_recovery",
            "restart_policy": "hold_for_manual_recovery",
        }
        if any(getattr(self, name) != value for name, value in expected.items()):
            raise HttpContractError("live_clock_policy_invalid")

    def minimum_lease_seconds(self, policy: AccountRatePolicy) -> int:
        if type(policy) is not AccountRatePolicy:
            raise HttpContractError("account_rate_policy_required")
        # Strict form of S + T_http + G < R + L, allowing S-R up to the
        # configured reservation-to-send bound.
        required = (
            self.max_reservation_to_send_seconds
            + policy.request_timeout_seconds
            + self.termination_guard_seconds
            + 1
        )
        if required > MAX_WAIT_SECONDS:
            raise HttpContractError("live_lease_requirement_out_of_range")
        return required

    def validate_rate_policy(self, policy: AccountRatePolicy) -> None:
        if type(policy) is not AccountRatePolicy:
            raise HttpContractError("account_rate_policy_required")
        if policy.slot_lease_seconds < self.minimum_lease_seconds(policy):
            raise HttpContractError("live_slot_lease_guard_insufficient")


@dataclass(frozen=True)
class LiveAccountPreflightContract:
    """Versioned C1 declaration; matching content is not live authorization."""

    registry: LiveAccountIdentityRegistry
    identity: LiveAccountIdentityRecord
    operating_policy: LiveAccountOperatingPolicy
    clock_policy: LiveClockPolicy
    rate_policy: AccountRatePolicy
    fixed_at: datetime
    decision_reference: str

    def __post_init__(self) -> None:
        if type(self.registry) is not LiveAccountIdentityRegistry:
            raise HttpContractError("live_account_identity_registry_required")
        if type(self.identity) is not LiveAccountIdentityRecord:
            raise HttpContractError("live_account_identity_record_required")
        if self.registry.resolve(self.identity.account_ref) != self.identity:
            raise HttpContractError("live_preflight_identity_registry_mismatch")
        if type(self.operating_policy) is not LiveAccountOperatingPolicy:
            raise HttpContractError("live_account_operating_policy_required")
        if type(self.clock_policy) is not LiveClockPolicy:
            raise HttpContractError("live_clock_policy_required")
        if type(self.rate_policy) is not AccountRatePolicy:
            raise HttpContractError("account_rate_policy_required")
        self.clock_policy.validate_rate_policy(self.rate_policy)
        fixed_at = _utc(self.fixed_at, "live_preflight_fixed_at")
        if fixed_at < _utc(
            self.registry.fixed_at, "live_registry_fixed_at"
        ) or fixed_at < _utc(self.identity.registered_at, "identity_registered_at"):
            raise HttpContractError("live_preflight_before_identity_registry")
        _text(self.decision_reference, "live_preflight_decision_reference")

    @property
    def canonical_ledger_path(self) -> str:
        return self.registry.canonical_ledger_path(self.identity.account_ref)

    @property
    def sha256(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict:
        return {
            "schema": LIVE_ACCOUNT_PREFLIGHT_SCHEMA,
            "registry": self.registry.to_dict(),
            "identity": self.identity.to_dict(),
            "operating_policy": asdict(self.operating_policy),
            "clock_policy": asdict(self.clock_policy),
            "rate_policy": asdict(self.rate_policy),
            "fixed_at": _utc(self.fixed_at, "live_preflight_fixed_at"),
            "decision_reference": self.decision_reference,
            "canonical_ledger_path": self.canonical_ledger_path,
        }


@dataclass(frozen=True)
class LiveAccountContractAlignment:
    """Informational C1 result; all not-yet-implemented live checks stay false."""

    content_matches: bool
    canonical_ledger_path: str
    account_identity_verified: bool = field(default=False, init=False)
    canonical_store_implemented: bool = field(default=False, init=False)
    external_client_exclusivity_verified: bool = field(default=False, init=False)
    authoritative_clock_implemented: bool = field(default=False, init=False)
    live_send_permitted: bool = field(default=False, init=False)


def check_live_account_preflight(
    plan: HttpAcquisitionPlan, contract: LiveAccountPreflightContract
) -> LiveAccountContractAlignment:
    """Bind a fixed plan to C1 declarations without authorizing any I/O."""

    if type(plan) is not HttpAcquisitionPlan:
        raise HttpContractError("plan_required")
    if type(contract) is not LiveAccountPreflightContract:
        raise HttpContractError("live_account_preflight_contract_required")
    plan.verify_fixed_scope()
    if plan.account_ref != contract.identity.account_ref:
        raise HttpContractError("live_account_ref_mismatch")
    if not contract.rate_policy.covers(plan.retry):
        raise HttpContractError("live_account_policy_weaker_than_plan")
    contract.clock_policy.validate_rate_policy(contract.rate_policy)
    return LiveAccountContractAlignment(True, contract.canonical_ledger_path)


@dataclass(frozen=True)
class LiveReclaimEvidence:
    """Evidence for a future manual recovery path, never an authorization token."""

    lease_expired_at: datetime
    observed_at: datetime
    clock_status: str
    previous_transport_termination_reference: str | None
    operator_review_reference: str | None
    process_restart_detected: bool

    def __post_init__(self) -> None:
        _utc(self.lease_expired_at, "live_reclaim_lease_expired_at")
        _utc(self.observed_at, "live_reclaim_observed_at")
        if self.clock_status not in {"verified", "uncertain", "regressed"}:
            raise HttpContractError("live_reclaim_clock_status_invalid")
        for name in (
            "previous_transport_termination_reference",
            "operator_review_reference",
        ):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)
        if type(self.process_restart_detected) is not bool:
            raise HttpContractError("process_restart_detected_must_be_boolean")


@dataclass(frozen=True)
class LiveReclaimAssessment:
    """Informational only; a future entry point must re-check raw evidence."""

    reasons: tuple[str, ...]
    automatic_reclaim_permitted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if type(self.reasons) is not tuple or any(
            type(reason) is not str for reason in self.reasons
        ):
            raise HttpContractError("live_reclaim_reasons_invalid")

    @property
    def manual_reclaim_eligible(self) -> bool:
        return not self.reasons


def assess_live_reclaim(
    policy: LiveClockPolicy, evidence: LiveReclaimEvidence
) -> LiveReclaimAssessment:
    """C1 decision table: automatic reclaim is forbidden under every state."""

    if type(policy) is not LiveClockPolicy or type(evidence) is not LiveReclaimEvidence:
        raise HttpContractError("live_reclaim_raw_evidence_required")
    reasons: list[str] = []
    observed = evidence.observed_at.astimezone(UTC)
    lease_expired = evidence.lease_expired_at.astimezone(UTC)
    if observed < lease_expired:
        reasons.append("live_lease_not_expired")
    if evidence.clock_status != "verified":
        reasons.append(f"live_clock_{evidence.clock_status}")
    if evidence.previous_transport_termination_reference is None:
        reasons.append("previous_transport_termination_unverified")
    if evidence.operator_review_reference is None:
        reasons.append("manual_recovery_review_missing")
    return LiveReclaimAssessment(tuple(reasons))
