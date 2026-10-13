"""Saved policy B and capacity diagnostics for artificial historical evidence.

No admission authority. The admission builders live in live_http_admission_history
and always reconstruct the complete supplied history. Budget v1 is unchanged.
"""

from dataclasses import dataclass, field
from typing import ClassVar

from . import live_body_contract as c3
from . import live_http_budget as v1
from . import live_http_evidence as c2
from . import live_store as store
from .http_contract import HttpContractError

PREFIX = "historical-feasibility-http-budget-"


def require(condition, reason):
    if not condition:
        raise HttpContractError("budget_v2_" + reason)


def total(values):
    return c3._sum(values)


@dataclass(frozen=True)
class BudgetPolicyV2(c3.Value):
    schema: ClassVar[str] = PREFIX + "policy-v2"
    rule: str = "success-digest-unique-quarantine-independent-full-page-unknown"

    def __post_init__(self):
        super().__post_init__()
        require(
            self.rule
            == "success-digest-unique-quarantine-independent-full-page-unknown",
            "policy_invalid",
        )


@dataclass(frozen=True)
class SavedCompatibilityV2(c3.Value):
    schema: ClassVar[str] = PREFIX + "saved-compatibility-v2"
    compatible: bool
    saved_total: int
    completed_pages: int
    reasons: tuple[str, ...]
    producer_authenticated: bool = field(default=False, init=False)
    reservation_permitted: bool = field(default=False, init=False)
    live_send_permitted: bool = field(default=False, init=False)


def _saved(projection, attempts, plan_attempts, originals):
    """Internal already-replayed projection path shared by both verifiers."""
    reasons, success, final_sizes, seen = set(), set(), {}, set()
    by_attempt = {a.binding: a for a in attempts}
    peer = {a.binding: a for a in plan_attempts}
    measurements = {m.binding: m for m in originals}
    require(len(by_attempt) == len(attempts), "duplicate_attempt")
    require(len(measurements) == len(originals), "duplicate_measurement")
    saved = pages = 0
    for obj in projection.objects:
        binding = obj.identity.binding
        if binding in seen:
            reasons.add("attempt_multiple_objects")
        seen.add(binding)
        a, p, m = by_attempt.get(binding), peer.get(binding), measurements.get(binding)
        if a is None or p is None:
            reasons.add("object_attempt_missing")
            continue
        untouched = (
            obj.state == "reserved"
            and obj.writer.state == "not_started"
            and obj.observed_size == obj.retained_lower_bound == 0
        )
        if untouched:
            if any(
                x.sent_at is not None
                or x.send_transition is not None
                or x.header is not None
                or x.outcome in {"response", "unknown"}
                for x in (a, p)
            ):
                reasons.add("untouched_send_evidence")
            continue
        if not obj.stable or obj.observed_size is None:
            reasons.add("body_not_stable")
            continue
        digest, size = obj.stability.digest, obj.stability.size
        if digest in final_sizes and final_sizes[digest] != size:
            reasons.add("final_digest_size_conflict")
        final_sizes[digest] = size
        if m is None or m.page != obj.identity.page:
            reasons.add("body_measurement_missing")
            continue
        if obj.state == "committed":
            if digest in success:
                reasons.add("successful_digest_duplicate")
            success.add(digest)
            r = obj.receipt
            if (
                a.outcome != "response"
                or a.header is None
                or a.header.status != 200
                or m.completeness != "complete"
                or (m.persisted_size, m.persisted_digest) != (r.size, r.final_digest)
                or m.sink_accepted_bytes != r.size
                or (size, digest) != (r.size, r.final_digest)
            ):
                reasons.add("successful_measurement_mismatch")
            saved = total((saved, r.size))
            pages += 1
        elif obj.state == "quarantine":
            # _replay verifies the receipt against the PRE-quarantine version,
            # including its provisional digest and writer, not this final state.
            if not (
                (a.outcome == "unknown" and m.completeness != "complete")
                or (
                    a.outcome == "response"
                    and a.header is not None
                    and a.header.status != 200
                    and m.completeness == "complete"
                )
            ):
                reasons.add("quarantine_outcome_invalid")
            if m.persisted_size is not None:
                reasons.add("quarantine_is_not_http_saved")
            if m.sink_accepted_bytes is not None and m.sink_accepted_bytes != size:
                reasons.add("quarantine_measurement_mismatch")
        else:
            reasons.add("body_not_committed_or_quarantined")
    for a in attempts:
        if a.outcome == "response" and a.header and a.header.status == 200:
            matches = [o for o in projection.objects if o.identity.binding == a.binding]
            if len(matches) != 1 or matches[0].state != "committed":
                reasons.add("response_200_body_uncommitted")
    return SavedCompatibilityV2(not reasons, saved, pages, tuple(sorted(reasons)))


def saved_compatibility_v2(body, *, account, plans, originals):
    """Diagnostic only; no operation commit-order or historical admission proof."""
    require(type(body) is c3.LiveBodyJournal, "body_type")
    require(type(account) is c2.LiveJournal and type(plans) is tuple, "journal_type")
    require(type(originals) is tuple, "originals_type")
    require(
        all(type(m) is v1.MeasurementOriginal for m in originals), "measurement_type"
    )
    body = c3.LiveBodyJournal(body.contract, body.root, body.records)
    require(
        c2.reconcile_live_journals(account, plans).classification == "consistent",
        "c2_inconsistent",
    )
    require(
        c3.reconcile_live_bodies(body, account=account, plans=plans).classification
        != "inconsistent",
        "c3_inconsistent",
    )
    sha = body.contract.plan.plan_sha
    peer = next((p for p in plans if p.projection.scopes[0].plan_sha == sha), None)
    require(peer is not None, "plan_missing")
    return _saved(
        body.projection,
        tuple(a for a in account.projection.attempts if a.binding.plan_sha == sha),
        peer.projection.attempts,
        originals,
    )


@dataclass(frozen=True)
class BudgetEvidenceV2(c3.Value):
    schema: ClassVar[str] = PREFIX + "evidence-v2"
    measurement_sha: str
    classification: str
    allowance: v1.Allowances
    charged_transfer: int
    charged_decoded: int
    overrun: bool
    producer_authenticated: bool = field(default=False, init=False)


@dataclass(frozen=True)
class BudgetResultV2(c3.Value):
    schema: ClassVar[str] = PREFIX + "result-v2"
    policy: BudgetPolicyV2
    plan_sha: str
    attempts: int
    known_transfer: int
    known_decoded: int
    unknown_count: int
    charged_transfer: int
    charged_decoded: int
    saved: SavedCompatibilityV2
    allowance: v1.Allowances
    evidence: tuple[BudgetEvidenceV2, ...]
    reasons: tuple[str, ...]
    producer_authenticated: bool = field(default=False, init=False)
    reservation_permitted: bool = field(default=False, init=False)
    live_send_permitted: bool = field(default=False, init=False)


def budget_result(plan, projection, attempts, peers, measurements, prior_allowances):
    saved = _saved(projection, attempts, peers, measurements)
    reasons = set(saved.reasons)
    known_t = known_d = unknown = 0
    evidence = []
    by_binding = {m.binding: m for m in measurements}
    for a in attempts:
        m = by_binding.get(a.binding)
        if a.outcome == "pre_send_reclaim":
            unknown += 1
            reasons.add("unsupported_recovery_operation")
            continue
        if a.outcome is None:
            reasons.add("open_unsettled")
            continue
        if m is None:
            reasons.add("measurement_not_committed")
            continue
        allowed = prior_allowances.get(a.binding)
        require(allowed is not None, "historical_allowance_missing")
        overrun = any(
            n is not None and n > cap
            for n, cap in (
                (m.transfer_bytes, allowed.transfer),
                (m.decoded_bytes, allowed.decoded),
                (m.sink_accepted_bytes, allowed.saved),
            )
        )
        if overrun:
            reasons.add("sticky_overrun")
        if a.outcome == "unknown" and m.completeness != "complete":
            unknown = total((unknown, 1))
            t, d, classification = (
                plan.limits.max_page_transfer_bytes,
                plan.limits.max_page_decoded_bytes,
                "valid_unknown",
            )
        elif a.outcome == "response" and m.completeness == "complete":
            t, d = m.transfer_bytes, m.decoded_bytes
            known_t, known_d = total((known_t, t)), total((known_d, d))
            classification = (
                "known_zero"
                if t == d == m.sink_accepted_bytes == 0
                else "known_complete"
            )
        else:
            reasons.add("conflicting_measurement")
            t = d = 0
            classification = "conflicting"
        evidence.append(
            BudgetEvidenceV2(m.sha256, classification, allowed, t, d, overrun)
        )
    t = total((known_t, c3._integer(unknown * plan.limits.max_page_transfer_bytes)))
    d = total((known_d, c3._integer(unknown * plan.limits.max_page_decoded_bytes)))
    if (
        t > plan.limits.max_transfer_bytes
        or d > plan.limits.max_decoded_bytes
        or saved.saved_total > plan.limits.max_saved_bytes
    ):
        reasons.add("sticky_overrun")
    allowance = v1.calculate_allowances(
        plan.limits, known_t, known_d, unknown, saved.saved_total
    )
    if not all((allowance.transfer, allowance.decoded, allowance.saved)):
        reasons.add("byte_budget_exhausted")
    return BudgetResultV2(
        BudgetPolicyV2(),
        plan.sha256,
        len(attempts),
        known_t,
        known_d,
        unknown,
        t,
        d,
        saved,
        allowance,
        tuple(evidence),
        tuple(sorted(reasons)),
    )


@dataclass(frozen=True)
class PlanCapacityV2(c3.Value):
    schema: ClassVar[str] = PREFIX + "plan-capacity-v2"
    plan_sha: str
    retained: int
    reserved: int
    acquired: int
    unresolved: int
    projection_sha: str


@dataclass(frozen=True)
class CapacityAssessmentV2(c3.Value):
    schema: ClassVar[str] = PREFIX + "capacity-v2"
    satisfied: bool
    plans: tuple[PlanCapacityV2, ...]
    requested: int
    retained_with_reservation: int
    acquired_with_reservation: int
    store_policy_sha: str
    reasons: tuple[str, ...]


def assess_capacity_v2(contracts, projections, *, target_plan_sha, store_policy):
    """All catalog plans, including stopped plans; no physical digest dedup."""
    require(type(store_policy) is store.StorePolicy, "store_policy_type")
    require(
        set(contracts) == set(projections) and target_plan_sha in contracts,
        "capacity_coverage",
    )
    m = contracts[target_plan_sha].policy.per_object_max_bytes
    entries, reasons = [], set()
    for sha in sorted(contracts):
        p, policy = projections[sha], contracts[sha].policy
        require(p.retained_body_bytes is not None, "unknown_retained_size")
        entry = PlanCapacityV2(
            sha,
            p.retained_body_bytes,
            p.reserved_remaining_bytes,
            p.cumulative_acquired_bytes,
            p.unresolved_acquisition_charge,
            p.sha256,
        )
        entries.append(entry)
        extra = m if sha == target_plan_sha else 0
        if total((entry.retained, entry.reserved, extra)) > policy.retained_body_budget:
            reasons.add("plan_retained_capacity")
        if total((entry.reserved, extra)) > policy.reserved_capacity_budget:
            reasons.add("plan_reserved_capacity")
        if total((entry.acquired, entry.unresolved, extra)) > policy.cumulative_budget:
            reasons.add("plan_cumulative_capacity")
    retained = total((*[total((e.retained, e.reserved)) for e in entries], m))
    acquired = total((*[total((e.acquired, e.unresolved)) for e in entries], m))
    if retained > store_policy.max_retained_body_bytes:
        reasons.add("account_retained_capacity")
    if acquired > store_policy.max_cumulative_acquired_bytes:
        reasons.add("account_cumulative_capacity")
    return CapacityAssessmentV2(
        not reasons,
        tuple(entries),
        m,
        retained,
        acquired,
        store_policy.sha256,
        tuple(sorted(reasons)),
    )
