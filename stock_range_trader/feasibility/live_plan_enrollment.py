"""Pure enrollment proposals. No persistence, activation, clocks or live permission."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime

from .http_contract import (
    AUTH_REFERENCE,
    HTTP_OUTPUT_ROOT,
    HttpAcquisitionPlan,
    HttpLimits,
    OwnerApprovalClaim,
    RetryRules,
    _iso_day,
    check_approval_scope,
)
from .live_http_evidence import (
    LIVE_ACCOUNT_SCHEMA_V2,
    LIVE_PLAN_SCHEMA,
    LiveAccountAuthority,
    LiveJournal,
    LivePlanScope,
    PlanEnrollment,
    _canonical,
    _digest_ref,
    _fail,
    _hash,
    _label,
    _time,
    _utc,
    reconcile_live_journals,
    require_enrollment_idle,
)
from .live_preflight import LiveAccountPreflightContract, check_live_account_preflight


def _validate_plan(plan: HttpAcquisitionPlan) -> None:
    """Recheck fixed fields lexically, without the legacy filesystem constructor."""
    if type(plan) is not HttpAcquisitionPlan:
        _fail("enrollment_fixed_plan_required")
    plan.verify_fixed_scope()
    if type(plan.artifact_id) is not str or not re.fullmatch(
        r"[a-z0-9][a-z0-9._-]{0,79}", plan.artifact_id
    ):
        _fail("enrollment_artifact_invalid")
    if plan.output_dir != str(HTTP_OUTPUT_ROOT) + "/" + plan.artifact_id:
        _fail("enrollment_output_dir_invalid")
    if plan.auth_reference != AUTH_REFERENCE:
        _fail("enrollment_auth_reference_invalid")
    _label(plan.account_ref)
    start = _iso_day(plan.calendar_start, "calendar_start")
    end = _iso_day(plan.calendar_end, "calendar_end")
    reference = _iso_day(plan.reference_date, "reference_date")
    if not start <= reference <= end:
        _fail("enrollment_plan_dates_invalid")
    if type(plan.daily_dates) is not tuple:
        _fail("enrollment_plan_dates_invalid")
    dates = tuple(_iso_day(d, "daily_date") for d in plan.daily_dates)
    if dates != tuple(sorted(set(dates))):
        _fail("enrollment_plan_dates_invalid")
    if plan.kind == "calendar_discovery":
        if dates or any(
            v is not None
            for v in (
                plan.master_date,
                plan.calendar_source_sha256,
                plan.calendar_source_reference,
            )
        ):
            _fail("enrollment_plan_dates_invalid")
    elif plan.kind in {"predeclared_daily", "calendar_anchored_daily"}:
        if (
            reference not in dates
            or plan.master_date != reference
            or any(not start <= d <= reference for d in dates)
        ):
            _fail("enrollment_plan_dates_invalid")
        _digest_ref(plan.calendar_source_sha256)
        if (
            type(plan.calendar_source_reference) is not str
            or not plan.calendar_source_reference
        ):
            _fail("enrollment_calendar_reference_required")
    else:
        _fail("enrollment_plan_kind_invalid")
    if type(plan.limits) is not HttpLimits or type(plan.retry) is not RetryRules:
        _fail("enrollment_plan_policy_invalid")
    replace(plan.limits)
    replace(plan.retry)
    if len(plan.queries) > min(plan.limits.max_attempts, plan.limits.max_pages_total):
        _fail("enrollment_plan_budget_invalid")
    if _utc(plan.not_before) >= _utc(plan.expires_at):
        _fail("enrollment_plan_window_invalid")


def authority_from_preflight(
    preflight: LiveAccountPreflightContract, *, canonical_preflight: str
) -> LiveAccountAuthority:
    """Validate C1 contents, including registry resolution; authenticity is unproven."""
    if type(preflight) is not LiveAccountPreflightContract:
        _fail("enrollment_preflight_required")
    identity = replace(preflight.identity)
    registry = replace(
        preflight.registry,
        active_records=tuple(replace(r) for r in preflight.registry.active_records),
    )
    checked = replace(
        preflight,
        identity=identity,
        registry=registry,
        operating_policy=replace(preflight.operating_policy),
        rate_policy=replace(preflight.rate_policy),
        clock_policy=replace(preflight.clock_policy),
    )
    if (
        canonical_preflight != _canonical(checked.to_dict())
        or _hash(json.loads(canonical_preflight)) != preflight.sha256
    ):
        _fail("enrollment_preflight_content_mismatch")
    if registry.resolve(identity.account_ref) != identity:
        _fail("enrollment_identity_mismatch")
    return LiveAccountAuthority(
        identity.account_ref, checked.sha256, checked.rate_policy, checked.clock_policy
    )


@dataclass(frozen=True)
class EnrollmentIntent:
    authority: LiveAccountAuthority
    scope: LivePlanScope
    operation_kind: str = field(default="plan_enrolled", init=False)

    def __post_init__(self) -> None:
        if (
            type(self.authority) is not LiveAccountAuthority
            or type(self.scope) is not LivePlanScope
            or not self.authority.matches(self.scope)
        ):
            _fail("plan_enrollment_binding_mismatch")

    @property
    def sha256(self) -> str:
        # Plan, preflight and approval digests are re-derived from full source
        # content by the builder; no bare SHA-only enrollment entry point.
        return _hash(
            {
                "authority": self.authority.to_dict(),
                "scope": self.scope.to_dict(),
                "operation_kind": self.operation_kind,
            }
        )


def derive_enrollment_intent(
    plan: HttpAcquisitionPlan,
    preflight: LiveAccountPreflightContract,
    approval: OwnerApprovalClaim,
    *,
    canonical_plan: str,
    canonical_preflight: str,
    canonical_approval: str,
    at: datetime,
) -> EnrollmentIntent:
    _validate_plan(plan)
    if (
        canonical_plan != _canonical(plan.to_dict())
        or _hash(json.loads(canonical_plan)) != plan.sha256
    ):
        _fail("enrollment_plan_content_mismatch")
    authority = authority_from_preflight(
        preflight, canonical_preflight=canonical_preflight
    )
    check_live_account_preflight(plan, preflight)
    if type(approval) is not OwnerApprovalClaim or canonical_approval != _canonical(
        approval.to_dict()
    ):
        _fail("enrollment_approval_content_mismatch")
    check_approval_scope(plan, approval, now=at)
    if _utc(preflight.fixed_at) > _utc(at):
        _fail("enrollment_preflight_from_future")
    # The execution scope is the narrower approved validity window.
    scope = LivePlanScope(
        plan.sha256,
        preflight.sha256,
        plan.account_ref,
        approval.sha256,
        approval.valid_from,
        approval.valid_until,
        plan.retry,
        preflight.rate_policy,
        preflight.clock_policy,
    )
    return EnrollmentIntent(authority, scope)


@dataclass(frozen=True)
class EnrollmentReceipt:
    operation_id: str
    intent_sha: str
    recorded_at: datetime
    previous_account_head: str
    prior_plan_heads: tuple[tuple[str, str], ...]
    initial_plan_head: str
    account_enrollment_head: str
    revision: int
    event_parts: tuple[str, str] = field(
        default=("account_enrollment", "plan_opening"), init=False
    )
    live_send_permitted: bool = field(default=False, init=False)
    live_acquisition_permitted: bool = field(default=False, init=False)


@dataclass(frozen=True)
class EnrollmentProposal:
    """Candidate evidence pair only, not an activated plan or permission token."""

    account: LiveJournal
    plan: LiveJournal
    receipt: EnrollmentReceipt
    replayed: bool
    live_send_permitted: bool = field(default=False, init=False)
    live_acquisition_permitted: bool = field(default=False, init=False)


def _receipt(
    item: PlanEnrollment, intent: EnrollmentIntent, revision: int
) -> EnrollmentReceipt:
    return EnrollmentReceipt(
        item.transition_id,
        intent.sha256,
        item.recorded_at,
        item.previous_account_head,
        item.prior_plan_heads,
        item.initial_head,
        item.account_enrollment_head,
        revision,
    )


def propose_plan_enrollment(
    account: LiveJournal,
    plan_journals: tuple[LiveJournal, ...],
    *,
    expected_account_head: str,
    operation_id: str,
    at: datetime,
    plan: HttpAcquisitionPlan,
    preflight: LiveAccountPreflightContract,
    approval: OwnerApprovalClaim,
    canonical_plan: str,
    canonical_preflight: str,
    canonical_approval: str,
) -> EnrollmentProposal:
    """Revalidate supplied evidence and return an immutable atomic-activation proposal.

    I1 must compare expected_account_head against its locked Store state. Pure
    values cannot detect that the caller supplied an entirely stale universe.
    """
    _label(operation_id)
    now = _utc(at)
    if type(account) is not LiveJournal or account.schema != LIVE_ACCOUNT_SCHEMA_V2:
        _fail("enrollment_account_v2_required")
    if account.head_sha != _digest_ref(expected_account_head):
        _fail("enrollment_account_head_stale")
    if reconcile_live_journals(account, plan_journals).classification != "consistent":
        _fail("plan_enrollment_reconciliation_required")
    projection = account.projection
    existing = next(
        (e for e in projection.enrollments if e.transition_id == operation_id), None
    )
    # Idempotent receipt retrieval revalidates source evidence at the ORIGINAL
    # operation time. A later caller timestamp cannot rewrite or extend it.
    intent = derive_enrollment_intent(
        plan,
        preflight,
        approval,
        canonical_plan=canonical_plan,
        canonical_preflight=canonical_preflight,
        canonical_approval=canonical_approval,
        at=existing.recorded_at if existing else now,
    )
    if intent.authority != projection.authority:
        _fail("plan_enrollment_binding_mismatch")
    if existing:
        if intent.scope != existing.scope:
            _fail("enrollment_operation_intent_conflict")
        original = next(
            p
            for p in plan_journals
            if p.projection.scopes[0].plan_sha == existing.scope.plan_sha
        )
        return EnrollmentProposal(
            account,
            original,
            _receipt(existing, intent, projection.enrollments.index(existing) + 1),
            True,
        )
    if any(
        json.loads(line)["event"]["transition_id"] == operation_id
        for j in (account, *plan_journals)
        for line in j.records
    ):
        _fail("enrollment_operation_id_collision")
    if intent.scope.plan_sha in {s.plan_sha for s in projection.scopes}:
        _fail("plan_already_enrolled")
    require_enrollment_idle(projection)
    if any(
        _time(json.loads(j.records[-1])["event"]["recorded_at"]) > now
        for j in (account, *plan_journals)
    ):
        _fail("live_recorded_clock_regressed")
    initial = LiveJournal.create(
        LIVE_PLAN_SCHEMA, (intent.scope,), recorded_at=now, transition_id=operation_id
    )
    prior = sorted((p.projection.scopes[0].plan_sha, p.head_sha) for p in plan_journals)
    updated = account.append(
        "plan_enrolled",
        recorded_at=now,
        transition_id=operation_id,
        scope=intent.scope.to_dict(),
        plan_journal_initial_head=initial.head_sha,
        prior_plan_heads=[{"plan_sha": sha, "head": head} for sha, head in prior],
    )
    if (
        reconcile_live_journals(updated, (*plan_journals, initial)).classification
        != "consistent"
    ):
        _fail("plan_enrollment_reconciliation_required")
    return EnrollmentProposal(
        updated,
        initial,
        _receipt(
            updated.projection.enrollments[-1],
            intent,
            updated.projection.enrollment_revision,
        ),
        False,
    )
