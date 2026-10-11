"""I2-1A-1 pure historical budget claims; no Store or production authority.

Measurement originals are artificial inputs. Reconciliation proves consistency,
not producer authenticity or completeness beyond the externally chosen prefixes.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

from . import http_contract as hc
from . import live_body_contract as c3
from . import live_http_evidence as c2
from .live_enrollment_sources import decode_plan

PREFIX = "historical-feasibility-http-budget-"
POLICY = PREFIX + "policy-v1"
MAX_SOURCES = 4096  # Offline contract bound, not an approved production scale.


def _check(ok, reason):
    if not ok:
        raise hc.HttpContractError("budget_" + reason)


def _int(value):
    _check(type(value) is int and 0 <= value <= c3.MAX_INT64, "integer_range")
    return value


def _hash(domain, value):
    return c3._hash({"domain": domain, "value": value})


@dataclass(frozen=True)
class SourceCommitment(c3.Value):
    schema: ClassVar[str] = PREFIX + "sources-v1"
    count: int
    head: str

    def __post_init__(self):
        super().__post_init__()
        _check(self.count <= MAX_SOURCES, "source_limit")
        c3._sha(self.head)


def _commit(domain, elements):
    _check(len(elements) <= MAX_SOURCES, "source_limit")
    head = _hash(domain + ":empty", {})
    for index, element in enumerate(elements):
        head = _hash(domain + ":item", {"index": index, "prior": head, "item": element})
    return SourceCommitment(len(elements), head)


@dataclass(frozen=True)
class BudgetPolicy(c3.Value):
    schema: ClassVar[str] = POLICY
    rule: str = "legacy-http-full-page-unknown-no-saved-charge"

    def __post_init__(self):
        super().__post_init__()
        _check(self.rule == "legacy-http-full-page-unknown-no-saved-charge", "policy")


DEFAULT_POLICY = BudgetPolicy()


@dataclass(frozen=True)
class Identity(c3.Value):
    schema: ClassVar[str] = PREFIX + "identity-v1"
    deployment_id: str
    store_id: str
    account_ref: str

    def __post_init__(self):
        super().__post_init__()
        for value in (self.deployment_id, self.store_id, self.account_ref):
            c2._label(value)


@dataclass(frozen=True)
class MeasurementOriginal(c3.Value):
    """Bounded capture claim, independently supplied to verify derived evidence.

    sink_accepted_bytes is not stable saved size. persisted_size/digest are
    separate claims which must match a committed C3 receipt before compatibility.
    """

    schema: ClassVar[str] = PREFIX + "measurement-original-v1"
    identity: Identity
    binding: c2.AttemptBinding
    page: c3.LivePageIdentity
    attempt_number: int
    completeness: str
    transfer_bytes: int | None
    decoded_bytes: int | None
    sink_accepted_bytes: int | None
    persisted_size: int | None
    persisted_digest: str | None
    observed_at: datetime
    producer_reference: str
    timing_reference: str
    account_head: str
    plan_head: str
    body_head: str

    def __post_init__(self):
        super().__post_init__()
        _check(self.attempt_number > 0, "attempt_number")
        _check(
            self.completeness in {"complete", "partial", "unobserved"}, "completeness"
        )
        _check(self.identity.account_ref == self.binding.account_ref, "account_binding")
        _check(self.page.plan_sha == self.binding.plan_sha, "plan_binding")
        for digest in (self.account_head, self.plan_head, self.body_head):
            c3._sha(digest)
        if self.persisted_digest is not None:
            c3._sha(self.persisted_digest)
        _check(
            (self.persisted_size is None) == (self.persisted_digest is None),
            "saved_pair",
        )
        sizes = (self.transfer_bytes, self.decoded_bytes, self.sink_accepted_bytes)
        if self.completeness == "complete":
            _check(all(x is not None for x in sizes), "complete_measurement_missing")
        if self.completeness == "unobserved":
            _check(
                all(x is None for x in sizes) and self.persisted_size is None,
                "unobserved_values",
            )
        if self.decoded_bytes is not None and self.sink_accepted_bytes is not None:
            _check(
                self.sink_accepted_bytes <= self.decoded_bytes, "sink_exceeds_decoded"
            )


@dataclass(frozen=True)
class Allowances(c3.Value):
    schema: ClassVar[str] = PREFIX + "allowances-v1"
    transfer: int
    decoded: int
    saved: int


def calculate_allowances(limits, known_transfer, known_decoded, unknown_count, saved):
    """Legacy arithmetic, with explicit int64 checks and zero exhausted allowance.

    Negative balances never become permission. The reducer separately records
    overrun/exhaustion; clamping here only matches budget_snapshot presentation.
    """
    _check(type(limits) is hc.HttpLimits, "limits_type")
    for value in vars(limits).values():
        _int(value)
    for value in (known_transfer, known_decoded, unknown_count, saved):
        _int(value)
    t = _int(known_transfer + _int(unknown_count * limits.max_page_transfer_bytes))
    d = _int(known_decoded + _int(unknown_count * limits.max_page_decoded_bytes))
    return Allowances(
        max(0, min(limits.max_page_transfer_bytes, limits.max_transfer_bytes - t)),
        max(0, min(limits.max_page_decoded_bytes, limits.max_decoded_bytes - d)),
        max(0, min(limits.max_page_saved_bytes, limits.max_saved_bytes - saved)),
    )


@dataclass(frozen=True)
class BudgetEvidence(c3.Value):
    schema: ClassVar[str] = PREFIX + "evidence-v1"
    original: MeasurementOriginal
    classification: str
    policy: BudgetPolicy
    reservation_allowances: Allowances
    charged_transfer: int
    charged_decoded: int
    overrun: bool
    producer_authenticated: bool = field(default=False, init=False)
    live_permission: bool = field(default=False, init=False)

    def __post_init__(self):
        super().__post_init__()
        _check(
            self.classification
            in {
                "known_complete",
                "known_zero",
                "valid_unknown",
                "open_unsettled",
                "missing",
                "conflicting",
            },
            "classification",
        )


@dataclass(frozen=True)
class SavedCompatibility(c3.Value):
    schema: ClassVar[str] = PREFIX + "saved-compatibility-v1"
    compatible: bool
    saved_total: int
    completed_pages: int
    reasons: tuple[str, ...]


def saved_compatibility(body, attempts, evidence):
    """Whole historical C3 inventory, never digest-deduplicate physical objects."""
    _check(type(body) is c3.LiveBodyJournal, "body_type")
    body = c3.LiveBodyJournal(body.contract, body.root, body.records)
    _check(type(attempts) is tuple and type(evidence) is tuple, "tuple_required")
    _check(len({a.binding for a in attempts}) == len(attempts), "duplicate_attempt")
    _check(
        len({e.original.binding for e in evidence}) == len(evidence),
        "duplicate_evidence",
    )
    by_attempt = {a.binding: a for a in attempts}
    by_evidence = {e.original.binding: e for e in evidence}
    reasons, digests, committed = set(), {}, set()
    total = pages = 0
    for obj in body.projection.objects:
        a = by_attempt.get(obj.identity.binding)
        e = by_evidence.get(obj.identity.binding)
        if a is None or e is None or obj.identity.page != e.original.page:
            reasons.add("object_attempt_evidence_missing")
        if (
            obj.state == "reserved"
            and obj.writer.state == "not_started"
            and obj.observed_size == 0
            and obj.retained_lower_bound == 0
        ):
            continue
        if obj.state != "committed" or not obj.stable or obj.observed_size is None:
            reasons.add("saved_inventory_incompatible")
            continue
        r = obj.receipt
        if r.final_digest in digests or obj.identity.binding in committed:
            reasons.add("saved_digest_or_attempt_duplicate")
        if r.final_digest in digests and digests[r.final_digest] != r.size:
            reasons.add("saved_digest_size_conflict")
        digests[r.final_digest] = r.size
        committed.add(obj.identity.binding)
        if (
            a is None
            or a.outcome != "response"
            or a.header is None
            or a.header.status != 200
            or e is None
            or e.classification not in {"known_complete", "known_zero"}
            or e.original.completeness != "complete"
        ):
            reasons.add("saved_response_evidence_invalid")
        elif (
            (e.original.persisted_size, e.original.persisted_digest)
            != (r.size, r.final_digest)
            or e.original.sink_accepted_bytes != r.size
            or obj.observed_size != r.size
        ):
            reasons.add("saved_measurement_mismatch")
        pages += 1
    for a in attempts:
        if (
            a.outcome == "response"
            and a.header is not None
            and a.header.status == 200
            and a.binding not in committed
        ):
            reasons.add("response_200_body_uncommitted")
    total = _int(sum(digests.values()))
    return SavedCompatibility(not reasons, total, pages, tuple(sorted(reasons)))


@dataclass(frozen=True)
class PageCount(c3.Value):
    schema: ClassVar[str] = PREFIX + "page-count-v1"
    request_id: str
    count: int


@dataclass(frozen=True)
class BudgetResult(c3.Value):
    schema: ClassVar[str] = PREFIX + "result-v1"
    policy: BudgetPolicy
    attempt_count: int
    page_counts: tuple[PageCount, ...]
    completed_pages: int
    known_transfer: int
    known_decoded: int
    unknown_count: int
    unknown_transfer_charge: int
    unknown_decoded_charge: int
    saved: SavedCompatibility
    allowances: Allowances
    first_reservation_at: datetime | None
    evaluated_at: datetime
    source_commitment: SourceCommitment
    dependency_commitment: SourceCommitment
    overrun: bool
    blocking_reasons: tuple[str, ...]
    live_permission: bool = field(default=False, init=False)


@dataclass(frozen=True)
class HistoricalBudget:
    result: BudgetResult
    evidence: tuple[BudgetEvidence, ...]


def _validated_sources(snapshot, account, plans, bodies, originals, at):
    _check(type(snapshot) is c3.FixedPlanSnapshot, "snapshot_type")
    _check(
        type(account) is c2.LiveJournal
        and type(plans) is tuple
        and type(bodies) is tuple
        and type(originals) is tuple,
        "source_types",
    )
    _check(
        all(type(p) is c2.LiveJournal for p in plans)
        and all(type(b) is c3.LiveBodyJournal for b in bodies)
        and all(type(m) is MeasurementOriginal for m in originals),
        "source_types",
    )
    count = (
        len(account.records)
        + sum(len(p.records) for p in plans)
        + sum(len(b.records) for b in bodies)
        + len(originals)
    )
    _check(count <= MAX_SOURCES, "source_limit")
    c2._utc(at)
    plan = decode_plan(snapshot.canonical_plan.encode(), snapshot.plan_sha)
    account = c2.LiveJournal.from_bytes(
        account.to_bytes(), expected_schema=account.schema
    )
    plans = tuple(
        c2.LiveJournal.from_bytes(p.to_bytes(), expected_schema=p.schema) for p in plans
    )
    bodies = tuple(c3.LiveBodyJournal(b.contract, b.root, b.records) for b in bodies)
    _check(
        c2.reconcile_live_journals(account, plans).classification == "consistent",
        "c2_inconsistent",
    )
    expected = {s.plan_sha for s in account.projection.scopes}
    _check(
        len(bodies) == len(expected)
        and {b.contract.plan.plan_sha for b in bodies} == expected,
        "body_source_coverage",
    )
    for b in bodies:
        _check(
            c3.reconcile_live_bodies(
                b,
                account=account,
                plans=plans,
                related_bodies=tuple(x for x in bodies if x is not b),
            ).classification
            != "inconsistent",
            "c3_inconsistent",
        )
    # Source order is fixed by schema: account, plans by SHA, bodies by SHA,
    # then measurement originals in canonical account reservation order.
    _check(
        list(plans) == sorted(plans, key=lambda p: p.projection.scopes[0].plan_sha)
        and list(bodies) == sorted(bodies, key=lambda b: b.contract.plan.plan_sha),
        "source_order",
    )
    entries = []
    for kind, journals in (("account", (account,)), ("plan", plans), ("body", bodies)):
        for journal in journals:
            for line in journal.records:
                raw = c3._load(line.encode())
                _check(c2._time(raw["event"]["recorded_at"]) <= at, "future_source")
                entries.append({"kind": kind, "record": raw})
    target = next(
        (p for p in plans if p.projection.scopes[0].plan_sha == plan.sha256), None
    )
    _check(target is not None, "target_plan_missing")
    body = next(b for b in bodies if b.contract.plan.plan_sha == plan.sha256)
    _check(body.contract.plan == snapshot, "body_plan_binding")
    attempts = tuple(
        a for a in account.projection.attempts if a.binding.plan_sha == plan.sha256
    )
    _check(
        tuple(m.binding for m in originals) == tuple(a.binding for a in attempts),
        "measurement_coverage_or_order",
    )
    identities = {m.identity for m in originals}
    _check(len(identities) <= 1, "store_identity_conflict")
    for m, a in zip(originals, attempts, strict=True):
        MeasurementOriginal.from_bytes(m.to_bytes())
        _check(a.reserved_at <= m.observed_at <= at, "measurement_time")
        ah, ph = account.prefix(m.account_head), target.prefix(m.plan_head)
        _check(
            ah.projection.attempt(*a.key).binding == a.binding
            and ph.projection.attempt(*a.key).binding == a.binding,
            "measurement_source_binding",
        )
        for prefix in (ah, ph):
            _check(
                c2._time(c3._load(prefix.records[-1].encode())["event"]["recorded_at"])
                <= m.observed_at,
                "measurement_source_time",
            )
        _check(
            ah.projection.attempt(*a.key).outcome == a.outcome,
            "measurement_outcome_prefix",
        )
        if a.settled_at is not None:
            _check(a.settled_at <= m.observed_at, "measurement_before_settlement")
        _check(
            any(c3._load(r.encode())["event_hash"] == m.body_head for r in body.records)
            or m.body_head == c3.ZERO_HASH,
            "measurement_body_head",
        )
        body_records = (
            ()
            if m.body_head == c3.ZERO_HASH
            else body.records[
                : next(
                    i + 1
                    for i, r in enumerate(body.records)
                    if c3._load(r.encode())["event_hash"] == m.body_head
                )
            ]
        )
        bp = c3.LiveBodyJournal(body.contract, body.root, body_records)
        if body_records:
            _check(
                c2._time(c3._load(body_records[-1].encode())["event"]["recorded_at"])
                <= m.observed_at,
                "measurement_source_time",
            )
        if m.persisted_size is not None:
            matches = [
                o.receipt
                for o in bp.projection.objects
                if o.identity.binding == m.binding and o.receipt
            ]
            _check(len(matches) == 1, "saved_receipt_source_missing")
        entries.append({"kind": "measurement", "record": m.to_dict()})
    deps = [{"kind": "account", "head": account.head_sha}]
    deps += [
        {"kind": "plan", "plan": p.projection.scopes[0].plan_sha, "head": p.head_sha}
        for p in plans
    ]
    deps += [
        {
            "kind": "body",
            "plan": b.contract.plan.plan_sha,
            "contract": b.contract.sha256,
            "root": b.root.sha256,
            "head": b.head_sha,
        }
        for b in bodies
    ]
    return (
        plan,
        account,
        target,
        body,
        attempts,
        _commit("budget-source-set-v1", entries),
        _commit("budget-dependencies-v1", deps),
    )


def _page_progress(plan, page, receipts):
    query = next(
        (q for q in plan.queries if c3._canonical(q.to_dict()) == page.query_json), None
    )
    _check(query is not None, "query_unknown")
    for prior_query in plan.queries[: plan.queries.index(query)]:
        prior = [
            r
            for r in receipts
            if r.identity.page.query_json == c3._canonical(prior_query.to_dict())
        ]
        _check(any(r.next_cursor is None for r in prior), "prior_query_incomplete")
    history = sorted(
        (r for r in receipts if r.identity.page.query_json == page.query_json),
        key=lambda r: r.identity.page.page_index,
    )
    _check(
        [r.identity.page.page_index for r in history] == list(range(len(history))),
        "page_history_gap",
    )
    _check(page.page_index == len(history), "page_progression")
    _check(
        not history
        or (
            history[-1].next_cursor is not None
            and history[-1].next_cursor == page.cursor
        ),
        "cursor_progression",
    )
    _check(
        page.cursor is None
        or all(r.identity.page.cursor != page.cursor for r in history),
        "cursor_cycle",
    )
    return query


def reduce_history(
    snapshot, *, account, plans, bodies, originals, at, policy=DEFAULT_POLICY
):
    """Recompute from explicit historical prefixes and explicit evaluation time."""
    _check(type(policy) is BudgetPolicy, "policy_type")
    plan, account, target, body, attempts, sources, deps = _validated_sources(
        snapshot, account, plans, bodies, originals, at
    )
    lim = plan.limits
    reasons, counts, evidence = set(), {}, []
    for source_body in bodies:
        state = c3.reconcile_live_bodies(
            source_body,
            account=account,
            plans=plans,
            related_bodies=tuple(x for x in bodies if x is not source_body),
        )
        if state.classification != "consistent":
            reasons.add("c3_pending")
    if any(
        a.state in {"reserved", "sent", "headers_observed"}
        for a in account.projection.attempts
    ):
        reasons.add("account_open_attempt")
    known_t = known_d = unknown = 0
    overrun = False
    receipts = [o.receipt for o in body.projection.objects if o.receipt is not None]
    first = attempts[0].reserved_at if attempts else None
    for index, (m, a) in enumerate(zip(originals, attempts, strict=True)):
        prior_receipts = [r for r in receipts if r.committed_at <= a.reserved_at]
        _check(
            all(prev.observed_at <= a.reserved_at for prev in originals[:index]),
            "measurement_unavailable_at_reservation",
        )
        if any(
            prior.outcome == "response"
            and prior.header.status == 200
            and not any(r.identity.binding == prior.binding for r in prior_receipts)
            for prior in attempts[:index]
        ):
            reasons.add("prior_response_200_body_uncommitted")
        prior_saved = sum({r.final_digest: r.size for r in prior_receipts}.values())
        allowed = calculate_allowances(lim, known_t, known_d, unknown, prior_saved)
        query = _page_progress(plan, m.page, prior_receipts)
        page = hc.PageRequest(query, m.page.page_index, m.page.cursor)
        key = page.request_id
        counts[key] = counts.get(key, 0) + 1
        _check(
            m.attempt_number == counts[key]
            and a.binding.attempt_id
            == hc.make_attempt_id(plan, page, m.attempt_number),
            "attempt_identity",
        )
        history = sorted(
            (
                r
                for r in prior_receipts
                if r.identity.page.query_json == m.page.query_json
            ),
            key=lambda r: r.identity.page.page_index,
        )
        _check(m.page.page_index == len(history), "page_progression")
        _check(
            not history or history[-1].next_cursor == m.page.cursor,
            "cursor_progression",
        )
        if (
            any(r.identity.page.cursor == m.page.cursor for r in history)
            and m.page.cursor is not None
        ):
            reasons.add("cursor_cycle")
        if counts[key] > plan.retry.max_attempts_per_page:
            reasons.add("retry_budget_exceeded")
        if page.page_index >= lim.max_pages_per_query:
            reasons.add("page_budget_exceeded")
        if m.completeness == "complete" and a.outcome == "response":
            classification = (
                "known_zero"
                if m.transfer_bytes == m.decoded_bytes == m.sink_accepted_bytes == 0
                else "known_complete"
            )
        elif a.outcome in {"unknown", "pre_send_reclaim"}:
            classification = (
                "conflicting" if m.completeness == "complete" else "valid_unknown"
            )
        elif a.state in {"reserved", "sent", "headers_observed"}:
            classification = (
                "open_unsettled" if m.completeness != "complete" else "conflicting"
            )
        else:
            classification = "missing"
        exceeded = any(
            v is not None and v > cap
            for v, cap in (
                (m.transfer_bytes, allowed.transfer),
                (m.decoded_bytes, allowed.decoded),
                (m.sink_accepted_bytes, allowed.saved),
            )
        )
        overrun |= exceeded
        if classification in {"known_complete", "known_zero"}:
            known_t = _int(known_t + m.transfer_bytes)
            known_d = _int(known_d + m.decoded_bytes)
            ct, cd = m.transfer_bytes, m.decoded_bytes
        elif classification == "valid_unknown":
            unknown = _int(unknown + 1)
            ct, cd = lim.max_page_transfer_bytes, lim.max_page_decoded_bytes
        else:
            reasons.add(classification)
            ct = cd = 0  # Not known-zero: blocked incomplete/conflicting accounting.
        evidence.append(
            BudgetEvidence(m, classification, policy, allowed, ct, cd, exceeded)
        )
    saved = saved_compatibility(body, attempts, tuple(evidence))
    reasons.update(saved.reasons)
    allowance = calculate_allowances(lim, known_t, known_d, unknown, saved.saved_total)
    tc, dc = (
        _int(unknown * lim.max_page_transfer_bytes),
        _int(unknown * lim.max_page_decoded_bytes),
    )
    overrun |= (
        known_t + tc > lim.max_transfer_bytes
        or known_d + dc > lim.max_decoded_bytes
        or saved.saved_total > lim.max_saved_bytes
    )
    if overrun:
        reasons.add("sticky_overrun")
    if not all((allowance.transfer, allowance.decoded, allowance.saved)):
        reasons.add("byte_budget_exhausted")
    if len(attempts) >= lim.max_attempts:
        reasons.add("attempt_budget_exhausted")
    if saved.completed_pages >= lim.max_pages_total:
        reasons.add("page_budget_exhausted")
    scope = target.projection.scopes[0]
    if evidence and evidence[-1].classification not in {
        "open_unsettled",
        "missing",
        "conflicting",
    }:
        last = attempts[-1]
        if (
            last.outcome == "response"
            and last.header.status not in {200, 429}
            and not 500 <= last.header.status < 600
        ):
            reasons.add("previous_attempt_not_retryable")
    if (
        not scope.not_before <= at < scope.expires_at
        or not plan.not_before <= at < plan.expires_at
    ):
        reasons.add("scope_expired_or_not_started")
    if first is not None and (at - first).total_seconds() >= lim.max_elapsed_seconds:
        reasons.add("elapsed_budget_exhausted")
    for h in account.projection.holds:
        if h.released_at is None:
            reasons.add("hold_unreleased")
    result = BudgetResult(
        policy,
        len(attempts),
        tuple(PageCount(k, v) for k, v in sorted(counts.items())),
        saved.completed_pages,
        known_t,
        known_d,
        unknown,
        tc,
        dc,
        saved,
        allowance,
        first,
        at,
        sources,
        deps,
        overrun,
        tuple(sorted(reasons)),
    )
    return HistoricalBudget(result, tuple(evidence))


@dataclass(frozen=True)
class CapacityResult(c3.Value):
    """Separate C3 accounting, never substituted for HTTP byte allowances."""

    schema: ClassVar[str] = PREFIX + "capacity-result-v1"
    retained: int | None
    reserved: int
    acquired: int
    unresolved: int
    projection_commitment: SourceCommitment


def _capacity_result(bodies):
    projections = tuple(b.projection for b in bodies)
    return CapacityResult(
        None
        if any(p.retained_body_bytes is None for p in projections)
        else _int(sum(p.retained_body_bytes for p in projections)),
        _int(sum(p.reserved_remaining_bytes for p in projections)),
        _int(sum(p.cumulative_acquired_bytes for p in projections)),
        _int(sum(p.unresolved_acquisition_charge for p in projections)),
        _commit(
            "budget-c3-capacity-v1",
            [
                {"plan": b.contract.plan.plan_sha, "projection": b.projection.to_dict()}
                for b in bodies
            ],
        ),
    )


@dataclass(frozen=True)
class AdmissionCore(c3.Value):
    schema: ClassVar[str] = PREFIX + "admission-core-v1"
    identity: Identity
    plan_sha: str
    approval_sha: str
    preflight_sha: str
    policy: BudgetPolicy
    semantic_request_sha: str
    page: c3.LivePageIdentity
    attempt_number: int
    source_heads: SourceCommitment
    dependency_heads: SourceCommitment
    budget_sources: SourceCommitment
    first_reservation_at: datetime
    start_observation_reference: str
    budget_result: BudgetResult
    capacity_projection_sha: str
    capacity: CapacityResult
    allowances: Allowances
    live_permission: bool = field(default=False, init=False)

    def __post_init__(self):
        super().__post_init__()
        for sha in (
            self.plan_sha,
            self.approval_sha,
            self.preflight_sha,
            self.semantic_request_sha,
            self.capacity_projection_sha,
        ):
            c3._sha(sha)
        _check(
            self.attempt_number > 0 and self.page.plan_sha == self.plan_sha,
            "core_binding",
        )
        self.to_bytes()


def build_admission(
    snapshot,
    *,
    identity,
    page,
    start_observation_reference,
    account,
    plans,
    bodies,
    originals,
    at,
):
    history = reduce_history(
        snapshot,
        account=account,
        plans=plans,
        bodies=bodies,
        originals=originals,
        at=at,
    )
    _check(
        not history.result.blocking_reasons,
        "admission_blocked:" + ",".join(history.result.blocking_reasons),
    )
    plan = decode_plan(snapshot.canonical_plan.encode(), snapshot.plan_sha)
    scope = account.projection.scope(plan.sha256)
    _check(
        type(identity) is Identity
        and identity.account_ref == scope.account_ref
        and all(m.identity == identity for m in originals),
        "identity_binding",
    )
    _check(
        type(page) is c3.LivePageIdentity and page.plan_sha == plan.sha256,
        "page_binding",
    )
    body = next(b for b in bodies if b.contract.plan.plan_sha == plan.sha256)
    query = _page_progress(
        plan, page, [o.receipt for o in body.projection.objects if o.receipt]
    )
    request = hc.PageRequest(query, page.page_index, page.cursor)
    receipts = sorted(
        (
            o.receipt
            for o in body.projection.objects
            if o.receipt and o.identity.page.query_json == page.query_json
        ),
        key=lambda r: r.identity.page.page_index,
    )
    _check(
        page.page_index == len(receipts)
        and (not receipts or receipts[-1].next_cursor == page.cursor),
        "page_progression",
    )
    number = (
        next(
            (
                p.count
                for p in history.result.page_counts
                if p.request_id == request.request_id
            ),
            0,
        )
        + 1
    )
    _check(
        number <= plan.retry.max_attempts_per_page
        and page.page_index < plan.limits.max_pages_per_query,
        "page_retry_exhausted",
    )
    semantic = _hash(
        "http-reservation-semantic-v1",
        {
            "plan_sha": plan.sha256,
            "query_id": query.query_id,
            "page_index": page.page_index,
        },
    )
    capacity = _hash(
        "c3-capacity-prefixes-v1", [b.projection.to_dict() for b in bodies]
    )
    return AdmissionCore(
        identity,
        plan.sha256,
        scope.approval_sha,
        scope.preflight_sha,
        BudgetPolicy(),
        semantic,
        page,
        number,
        history.result.dependency_commitment,
        history.result.dependency_commitment,
        history.result.source_commitment,
        history.result.first_reservation_at or at,
        start_observation_reference,
        history.result,
        capacity,
        _capacity_result(bodies),
        history.result.allowances,
    )


def derive_slot_id(core, operation_id):
    _check(type(core) is AdmissionCore, "core_type")
    c2._label(operation_id)
    return _hash(
        "http-reservation-slot-v1",
        {
            "deployment_id": core.identity.deployment_id,
            "operation_id": operation_id,
            "admission_digest": core.sha256,
        },
    )


def verify_history(snapshot, *, claimed_evidence, claimed_result, **sources):
    actual = reduce_history(snapshot, **sources)
    _check(
        type(claimed_evidence) is tuple
        and all(type(e) is BudgetEvidence for e in claimed_evidence)
        and type(claimed_result) is BudgetResult,
        "claim_type",
    )
    _check(
        tuple(e.to_bytes() for e in claimed_evidence)
        == tuple(e.to_bytes() for e in actual.evidence)
        and claimed_result.to_bytes() == actual.result.to_bytes(),
        "historical_claim_mismatch",
    )
    return actual


def verify_admission(snapshot, *, claimed_core, claimed_slot, operation_id, **sources):
    _check(type(claimed_core) is AdmissionCore, "core_type")
    actual = build_admission(snapshot, **sources)
    _check(claimed_core.to_bytes() == actual.to_bytes(), "admission_mismatch")
    _check(claimed_slot == derive_slot_id(actual, operation_id), "slot_mismatch")
    return actual
