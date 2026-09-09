"""Pre-result registration candidates, always DRAFT; no registration operation."""

import re
from dataclasses import dataclass
from datetime import date, datetime

from data.price_policy import provider_price_basis

from .account_policy import AccountPolicy
from .checkpoint_models import REGISTRATION_STATUS, FinalizationPolicy
from .replay_calendar import shift_month
from .replay_policy import ReplayPolicy, audit_value
from .serialization import JsonObject, digest, require_hash, time_text
from .validation import ReplayContractError, calendar_date, timestamp

CANDIDATE_SCHEMA = "delayed-registration-candidate-1"


def public_payload(value):
    """Reject unsafe payloads, never silently redact values included in an ID."""
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(
                r"(api.?key|password|secret|token|git_root|raw_market_data)", key, re.I
            ):
                raise ReplayContractError("private_field_not_exportable")
            public_payload(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            public_payload(item)
    elif isinstance(value, str):
        if value.startswith(("/", "~", "file:")) or re.search(
            r"(?:[A-Za-z]:\\|/Users/|/home/|Bearer\s|gh[pousr]_[A-Za-z0-9])", value
        ):
            raise ReplayContractError("private_value_not_exportable")


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    state: str
    commit: str | None
    tree: str | None

    def __post_init__(self):
        if self.state not in ("clean", "dirty", "git_unavailable"):
            raise ReplayContractError("unknown_source_state")
        for value in (self.commit, self.tree):
            if value is not None and (
                not isinstance(value, str)
                or re.fullmatch("[0-9a-f]{40}|[0-9a-f]{64}", value) is None
            ):
                raise ReplayContractError("invalid_source_hash")
        if self.state == "git_unavailable" and (
            self.commit is not None or self.tree is not None
        ):
            raise ReplayContractError("unavailable_source_has_git_identity")


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    payload: JsonObject
    payload_sha256: str

    def __post_init__(self):
        if self.payload.sha256 != self.payload_sha256:
            raise ReplayContractError("verification_digest_mismatch")
        value = self.payload.to_dict()
        if (
            set(value)
            != {
                "schema",
                "evidence_id",
                "kind",
                "source_commit",
                "source_tree",
                "subject_hash",
                "result",
            }
            or value["schema"] != "delayed-verification-1"
        ):
            raise ReplayContractError("verification_schema_mismatch")
        if value["kind"] not in (
            "synthetic_test",
            "historical_test",
            "live_price_basis",
            "live_lot",
            "live_calendar",
            "universe_point_in_time",
            "operational_approval",
        ) or value["result"] not in ("passed", "failed", "unverified"):
            raise ReplayContractError("invalid_verification_contract")
        if not isinstance(value["evidence_id"], str) or not value["evidence_id"]:
            raise ReplayContractError("verification_id_required")
        SourceIdentity("clean", value["source_commit"], value["source_tree"])
        if value["source_commit"] is None or value["source_tree"] is None:
            raise ReplayContractError("verification_source_required")
        require_hash(value["subject_hash"])
        public_payload(value)


@dataclass(frozen=True, slots=True)
class RegistrationInputs:
    protocol_hash: str | None
    source: SourceIdentity
    account_policy: AccountPolicy | None
    replay_policy: ReplayPolicy | None
    config_hash: str | None
    catalog_hash: str | None
    selection_policy_hash: str | None
    price_contract_hash: str | None
    universe: tuple[str, ...] | None
    universe_reference_date: date | None
    calendar_hash: str | None
    planned_start: date | None
    one_month_finalization: FinalizationPolicy | None
    three_month_finalization: FinalizationPolicy | None
    history_snapshot_hashes: tuple[str, ...]
    verification: tuple[VerificationEvidence, ...]
    unresolved_operational_fields: tuple[str, ...]

    def __post_init__(self):
        for name in (
            "protocol_hash",
            "config_hash",
            "catalog_hash",
            "selection_policy_hash",
            "price_contract_hash",
            "calendar_hash",
        ):
            value = getattr(self, name)
            if value is not None:
                require_hash(value)
        if not isinstance(self.source, SourceIdentity):
            raise ReplayContractError("typed_source_required")
        for name, kind in (
            ("account_policy", AccountPolicy),
            ("replay_policy", ReplayPolicy),
            ("one_month_finalization", FinalizationPolicy),
            ("three_month_finalization", FinalizationPolicy),
        ):
            if getattr(self, name) is not None and not isinstance(
                getattr(self, name), kind
            ):
                raise ReplayContractError("typed_preconditions_required")
        for day in (self.universe_reference_date, self.planned_start):
            if day is not None:
                calendar_date(day, "precondition_date")
        if self.planned_start is not None and self.planned_start.day != 1:
            raise ReplayContractError("planned_month_boundary_required")
        for values in (
            self.history_snapshot_hashes,
            self.verification,
            self.unresolved_operational_fields,
        ):
            if type(values) is not tuple:
                raise ReplayContractError("immutable_preconditions_required")
        for value in self.history_snapshot_hashes:
            require_hash(value)
        for value in self.unresolved_operational_fields:
            if (
                not isinstance(value, str)
                or re.fullmatch("[a-z][a-z0-9_]*", value) is None
            ):
                raise ReplayContractError("invalid_unresolved_field")
        if self.universe is not None and (
            type(self.universe) is not tuple
            or not self.universe
            or any(not isinstance(s, str) or not s for s in self.universe)
            or len(set(self.universe)) != len(self.universe)
        ):
            raise ReplayContractError("invalid_fixed_universe")
        seen = {}
        for proof in self.verification:
            if not isinstance(proof, VerificationEvidence):
                raise ReplayContractError("typed_verification_required")
            p = proof.payload.to_dict()
            if (p["source_commit"], p["source_tree"]) != (
                self.source.commit,
                self.source.tree,
            ):
                raise ReplayContractError("verification_source_mismatch")
            if (
                p["evidence_id"] in seen
                and seen[p["evidence_id"]] != proof.payload_sha256
            ):
                raise ReplayContractError("conflicting_verification_id")
            seen[p["evidence_id"]] = proof.payload_sha256


@dataclass(frozen=True, slots=True)
class RegistrationCandidate:
    payload: JsonObject
    payload_sha256: str
    candidate_id: str
    generated_at: datetime

    def __post_init__(self):
        timestamp(self.generated_at, "generated_at")
        if (
            self.payload.sha256 != self.payload_sha256
            or self.candidate_id != "registration-candidate-" + self.payload_sha256
        ):
            raise ReplayContractError("registration_candidate_digest_mismatch")
        value = self.payload.to_dict()
        if (
            value["schema"] != CANDIDATE_SCHEMA
            or value["registration_status"] != REGISTRATION_STATUS
            or value["formal_registration_performed"] is not False
        ):
            raise ReplayContractError("registration_not_permitted")
        fixed = {
            "initial_capital": "200000",
            "account_mode": "single_shared",
            "lot_size": 100,
            "direction": "long_only",
            "reselection": "monthly",
            "provider": "jquants",
            "provider_plan": "free",
            "execution_mode": "delayed_oos_replay",
        }
        if any(
            value.get(key) != expected or type(value.get(key)) is not type(expected)
            for key, expected in fixed.items()
        ):
            raise ReplayContractError("registration_fixed_contract_mismatch")
        public_payload(value)

    def to_dict(self):
        return {
            **self.payload.to_dict(),
            "candidate_id": self.candidate_id,
            "payload_sha256": self.payload_sha256,
            "generated_at": time_text(self.generated_at),
        }


def build_registration_candidate(inputs: RegistrationInputs, *, generated_at):
    """No checkpoint, outcome, equity, store, evaluator, or result argument."""
    if not isinstance(inputs, RegistrationInputs):
        raise ReplayContractError("registration_inputs_required")
    missing = []
    for name in (
        "protocol_hash",
        "account_policy",
        "replay_policy",
        "config_hash",
        "catalog_hash",
        "selection_policy_hash",
        "price_contract_hash",
        "universe",
        "universe_reference_date",
        "calendar_hash",
        "planned_start",
    ):
        if getattr(inputs, name) is None:
            missing.append("unresolved_" + name)
    if inputs.source.state != "clean":
        missing.append("source_" + inputs.source.state)
    if inputs.source.commit is None or inputs.source.tree is None:
        missing.append("source_hash_missing")
    for name in ("one_month_finalization", "three_month_finalization"):
        policy = getattr(inputs, name)
        if policy is None or not policy.complete:
            missing.append("unresolved_" + name)
    missing.extend(
        "unapproved_" + name for name in inputs.unresolved_operational_fields
    )
    if not any(
        p.payload.to_dict()["kind"] == "operational_approval"
        and p.payload.to_dict()["result"] == "passed"
        and p.payload.to_dict()["subject_hash"] == inputs.config_hash
        for p in inputs.verification
    ):
        missing.append("operational_approval_unverified")
    missing.extend(
        (
            "real_daily_open_evidence_unsupported",
            "additional_input_events_unsupported",
            "live_price_basis_unverified",
            "live_lot_unverified",
            "live_calendar_unverified",
            "formal_future_period_not_registered",
        )
    )
    # A supplied boolean or synthetic result cannot turn these capabilities on.
    # Verification artifacts are retained, not promoted to actual live execution.
    if not inputs.history_snapshot_hashes:
        missing.append("required_history_references_missing")
    universe_hash = (
        None
        if inputs.universe is None
        else digest({"instruments": sorted(inputs.universe)})
    )
    verified_pit = any(
        p.payload.to_dict()["kind"] == "universe_point_in_time"
        and p.payload.to_dict()["result"] == "passed"
        and p.payload.to_dict()["subject_hash"] == universe_hash
        for p in inputs.verification
    )
    if not verified_pit:
        missing.append("universe_point_in_time_unverified")
    value = audit_value(inputs)
    value["universe"] = None if inputs.universe is None else sorted(inputs.universe)
    value["universe_hash"] = universe_hash
    value["history_snapshot_hashes"] = sorted(set(inputs.history_snapshot_hashes))
    value["unresolved_operational_fields"] = sorted(
        set(inputs.unresolved_operational_fields)
    )
    value["verification"] = [
        {"payload": p.payload.to_dict(), "payload_sha256": p.payload_sha256}
        for p in sorted(
            {p.payload_sha256: p for p in inputs.verification}.values(),
            key=lambda p: p.payload_sha256,
        )
    ]
    value.update(
        schema=CANDIDATE_SCHEMA,
        registration_status=REGISTRATION_STATUS,
        formal_registration_performed=False,
        execution_mode="delayed_oos_replay",
        price_conventions={
            "provider_price_basis": provider_price_basis("jquants"),
            "signal_lane": "adjusted_ohlcv",
            "execution_lane": "raw_ohlcv",
            "dividends": "not_added",
            "taxes": "before_tax",
        },
        input_policy="pinned_history_no_future_market_hash_required",
        provider="jquants",
        provider_plan="free",
        initial_capital="200000",
        account_mode="single_shared",
        lot_size=100,
        direction="long_only",
        reselection="monthly",
        one_month_boundary=None
        if inputs.planned_start is None
        else shift_month(inputs.planned_start, 1).isoformat(),
        three_month_boundary=None
        if inputs.planned_start is None
        else shift_month(inputs.planned_start, 3).isoformat(),
        structural_unmet=sorted(set(missing)),
        capabilities={
            "synthetic_replay": True,
            "real_daily_open_evidence": False,
            "additional_input_events": False,
        },
        accounting_constraints=[
            "dividends_not_added",
            "before_tax",
            "no_hypothetical_exit_costs",
        ],
        thresholds={
            "one_month_minimum_return": "-0.05",
            "minimum_unique_symbols": 20,
            "minimum_completed_trades": 100,
            "three_month_return_strictly_above": "0",
            "mdd_secondary_only": True,
        },
    )
    payload = JsonObject.from_value(value)
    return RegistrationCandidate(
        payload,
        payload.sha256,
        "registration-candidate-" + payload.sha256,
        generated_at,
    )
