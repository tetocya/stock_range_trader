"""Stage 5 exact measurements and explicit, independently finalized evidence."""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, localcontext

from .replay_calendar import JST, ReplayCalendar, shift_month
from .replay_policy import audit_value
from .serialization import (
    JsonObject,
    decimal_text,
    digest,
    require_hash,
    require_sequence,
)
from .validation import ReplayContractError, calendar_date, timestamp

CHECKPOINT_SCHEMA = "delayed-checkpoint-1"
JUDGE_VERSION = "delayed-protocol-judge-1"
REGISTRATION_STATUS = "draft_not_registered"


def exact(value):
    """No bool/float coercion, non-finite values or display rounding."""
    normalized = decimal_text(value)
    if normalized != value:
        raise ReplayContractError("normalized_exact_decimal_required")
    return Decimal(value)


def ratio(numerator, denominator):
    a, b = exact(numerator), exact(denominator)
    if b <= 0:
        raise ReplayContractError("positive_denominator_required")
    with localcontext() as ctx:
        ctx.prec = 64
        return rendered(a / b)


def net_return(equity, initial):
    with localcontext() as ctx:
        ctx.prec = 256
        base = exact(initial)
        if base <= 0:
            raise ReplayContractError("positive_denominator_required")
        return rendered((exact(equity) - base) / base)


def rendered(value):
    if not value.is_finite():
        raise ReplayContractError("nonfinite_computed_measurement")
    result = format(value, "f")
    return (
        "0"
        if value == 0
        else result.rstrip("0").rstrip(".")
        if "." in result
        else result
    )


@dataclass(frozen=True, slots=True)
class CheckpointSchedule:
    start: date
    calendar: ReplayCalendar

    def __post_init__(self):
        calendar_date(self.start, "start")
        if self.start.day != 1 or not isinstance(self.calendar, ReplayCalendar):
            raise ReplayContractError("explicit_month_start_calendar_required")
        for months in (1, 3):
            if not self.sessions(months):
                raise ReplayContractError("checkpoint_has_no_expected_session")

    def boundary(self, months):
        if type(months) is not int or months not in (1, 3):
            raise ReplayContractError("checkpoint_horizon_must_be_1_or_3")
        return shift_month(self.start, months)

    def ends_at(self, months):
        return datetime.combine(self.boundary(months), time.min, JST)

    def sessions(self, months):
        return self.calendar.between(self.start, self.boundary(months))

    def expected(self, months):
        return self.sessions(months)[-1]


@dataclass(frozen=True, slots=True)
class FinalizationPolicy:
    scheduled_available_at: datetime | None
    publication_policy_hash: str | None
    deadline: datetime | None
    basis_code: str | None

    def __post_init__(self):
        for value in (self.scheduled_available_at, self.deadline):
            if value is not None:
                timestamp(value, "finalization_time")
        if self.publication_policy_hash is not None:
            require_hash(self.publication_policy_hash)
        if self.basis_code is not None and (
            not isinstance(self.basis_code, str)
            or not self.basis_code
            or not self.basis_code.replace("_", "").isalnum()
        ):
            raise ReplayContractError("invalid_deadline_basis_code")
        if (
            self.deadline is not None
            and self.scheduled_available_at is not None
            and self.deadline < self.scheduled_available_at
        ):
            raise ReplayContractError("deadline_precedes_scheduled_availability")

    @property
    def complete(self):
        return (
            self.deadline is not None
            and self.basis_code is not None
            and (
                self.scheduled_available_at is not None
                or self.publication_policy_hash is not None
            )
        )


@dataclass(frozen=True, slots=True)
class FinalizationState:
    status: str
    reason: str
    observed_at: datetime | None
    finalized_at: datetime | None
    reference_hash: str

    def __post_init__(self):
        if self.status not in (
            "pending",
            "available",
            "unavailable_external",
            "invalid",
        ):
            raise ReplayContractError("unknown_evidence_status")
        if (
            not isinstance(self.reason, str)
            or not self.reason
            or not self.reason.replace("_", "").isalnum()
        ):
            raise ReplayContractError("invalid_evidence_reason_code")
        require_hash(self.reference_hash)
        for value in (self.observed_at, self.finalized_at):
            if value is not None:
                timestamp(value, "evidence_time")
        if (self.status == "pending") != (self.finalized_at is None):
            raise ReplayContractError("evidence_finalization_mismatch")
        if self.status == "available" and self.observed_at is None:
            raise ReplayContractError("available_evidence_requires_observed_time")
        if (
            self.finalized_at is not None
            and self.observed_at is not None
            and self.finalized_at < self.observed_at
        ):
            raise ReplayContractError("finalized_before_observed")


@dataclass(frozen=True, slots=True)
class ExternalAbsence:
    evidence_id: str
    evidence_hash: str
    observed_at: datetime
    reason_code: str

    def __post_init__(self):
        require_hash(self.evidence_hash)
        timestamp(self.observed_at, "external_observed_at")
        if not isinstance(self.evidence_id, str) or not self.evidence_id:
            raise ReplayContractError("external_evidence_id_required")
        if self.reason_code not in (
            "provider_outage",
            "market_data_not_published",
            "external_market_interruption",
        ):
            raise ReplayContractError("external_missing_reason_required")


def finalize(
    *,
    value_available,
    external_missing,
    invalid,
    observed_at,
    reference_hash,
    period_end,
    policy,
    now,
    previous=None,
):
    """Explicit now and causal evidence, never reads a clock or invents a deadline."""
    for flag in (value_available, external_missing, invalid):
        if type(flag) is not bool:
            raise ReplayContractError("evidence_flag_requires_bool")
    timestamp(now, "now")
    timestamp(period_end, "period_end")
    if policy.deadline is not None and policy.deadline < period_end:
        raise ReplayContractError("deadline_precedes_market_boundary")
    if observed_at is not None:
        timestamp(observed_at, "observed_at")
        if observed_at > now:
            raise ReplayContractError("evidence_not_observed_yet")
    if previous is not None and previous.status != "pending":
        if reference_hash != previous.reference_hash:
            raise ReplayContractError("finalized_evidence_amendment_not_supported")
        return previous
    if invalid:
        status, reason = "invalid", "invalid_accounting_or_audit"
    elif now < period_end:
        status, reason = "pending", "market_period_not_complete"
    elif not policy.complete:
        status, reason = "pending", "finalization_policy_unresolved"
    elif value_available:
        status, reason = "available", "required_evidence_available"
    elif external_missing and now >= policy.deadline:
        status, reason = (
            "unavailable_external",
            "external_evidence_unavailable_at_deadline",
        )
    else:
        status, reason = "pending", "awaiting_required_evidence"
    return FinalizationState(
        status,
        reason,
        observed_at,
        None if status == "pending" else now,
        reference_hash,
    )


@dataclass(frozen=True, slots=True)
class CheckpointEvidence:
    months: int
    start: date
    boundary: date
    expected_session: date
    actual_session: date | None
    market_at: datetime | None
    policy: FinalizationPolicy
    valuation: FinalizationState
    samples: FinalizationState
    initial_equity: str
    equity: str | None
    unique_symbols: int | None
    completed_trades: int | None
    evidence_kind: str
    references: JsonObject
    secondary: JsonObject

    def __post_init__(self):
        for name in ("start", "boundary", "expected_session"):
            calendar_date(getattr(self, name), name)
        if (
            type(self.months) is not int
            or self.months not in (1, 3)
            or self.start.day != 1
            or self.boundary != shift_month(self.start, self.months)
            or not self.start <= self.expected_session < self.boundary
        ):
            raise ReplayContractError("invalid_checkpoint_interval")
        if exact(self.initial_equity) != 200000:
            raise ReplayContractError("shared_initial_equity_must_be_200000")
        if self.equity is not None:
            exact(self.equity)
        if self.evidence_kind not in ("synthetic", "historical", "delayed_replay"):
            raise ReplayContractError("invalid_evidence_kind")
        if (
            not isinstance(self.policy, FinalizationPolicy)
            or not isinstance(self.valuation, FinalizationState)
            or not isinstance(self.samples, FinalizationState)
        ):
            raise ReplayContractError("typed_checkpoint_evidence_required")
        if self.actual_session is not None:
            calendar_date(self.actual_session, "actual_session")
        if self.market_at is not None:
            timestamp(self.market_at, "market_at")
        if self.valuation.status == "available" and (
            self.equity is None
            or self.actual_session != self.expected_session
            or self.market_at is None
            or self.market_at.astimezone(JST).date() != self.expected_session
        ):
            raise ReplayContractError("available_valuation_must_match_expected_session")
        if self.valuation.status != "available" and self.equity is not None:
            raise ReplayContractError("nonavailable_valuation_has_numeric_value")
        for count in (self.unique_symbols, self.completed_trades):
            if count is not None:
                require_sequence(count, zero=True)
        if (self.samples.status == "available") != (
            self.unique_symbols is not None and self.completed_trades is not None
        ):
            raise ReplayContractError("sample_availability_mismatch")
        if self.samples.status != "available" and (
            self.unique_symbols is not None or self.completed_trades is not None
        ):
            raise ReplayContractError("missing_samples_are_not_zero")
        if not isinstance(self.references, JsonObject) or not isinstance(
            self.secondary, JsonObject
        ):
            raise ReplayContractError("immutable_evidence_payload_required")
        period_end = datetime.combine(self.boundary, time.min, JST)
        if self.policy.deadline is not None and self.policy.deadline < period_end:
            raise ReplayContractError("deadline_precedes_market_boundary")
        for evidence in (self.valuation, self.samples):
            if evidence.status in ("available", "unavailable_external"):
                if not self.policy.complete or evidence.finalized_at < period_end:
                    raise ReplayContractError("checkpoint_finalized_before_ready")
            if (
                evidence.status == "unavailable_external"
                and evidence.finalized_at < self.policy.deadline
            ):
                raise ReplayContractError("external_missing_finalized_before_deadline")
        if (
            self.valuation.status == "available"
            and self.valuation.observed_at < self.market_at
        ):
            raise ReplayContractError("valuation_observed_before_market_close")
        refs = self.references.to_dict()
        if (
            not {"account_stream_id", "genesis_hash", "expected_head"} <= set(refs)
            or not isinstance(refs["account_stream_id"], str)
            or not refs["account_stream_id"]
        ):
            raise ReplayContractError("checkpoint_ledger_references_required")
        require_hash(refs["genesis_hash"])
        head = refs["expected_head"]
        if type(head) is not dict or set(head) != {"sequence", "event_hash"}:
            raise ReplayContractError("checkpoint_head_required")
        require_sequence(head["sequence"], zero=True)
        require_hash(head["event_hash"])

    @property
    def return_value(self):
        return (
            None
            if self.equity is None
            else net_return(self.equity, self.initial_equity)
        )

    def to_dict(self):
        value = audit_value(self)
        value["references"] = self.references.to_dict()
        value["secondary"] = self.secondary.to_dict()
        value["return"] = self.return_value
        value["schema"] = CHECKPOINT_SCHEMA
        return value

    @property
    def sha256(self):
        return digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class ProtocolResult:
    payload: JsonObject
    payload_sha256: str

    def __post_init__(self):
        if self.payload.sha256 != self.payload_sha256:
            raise ReplayContractError("protocol_result_digest_mismatch")
        value = self.payload.to_dict()
        if (
            value["registration_status"] != REGISTRATION_STATUS
            or value["policy_version"] != JUDGE_VERSION
            or value["schema"] != "delayed-protocol-result-1"
            or value["formal_registration_performed"] is not False
        ):
            raise ReplayContractError("invalid_protocol_result_contract")
        label = value["label"]
        if (
            label not in ("INVALID", "PENDING", "PASS", "FAIL", "INCONCLUSIVE")
            or value["outcome"]
            != (label if label in ("PASS", "FAIL", "INCONCLUSIVE") else "N/A")
            or value["lifecycle"] != ("pending" if label == "PENDING" else "finalized")
            or value["validity"] != ("invalid" if label == "INVALID" else "valid")
        ):
            raise ReplayContractError("inconsistent_protocol_result_states")

    def to_dict(self):
        return {**self.payload.to_dict(), "payload_sha256": self.payload_sha256}
