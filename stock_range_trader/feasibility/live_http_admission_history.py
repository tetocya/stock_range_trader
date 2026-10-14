"""I2-1A-2C pure sequential admission over explicitly artificial originals.

This is an internal-consistency verifier, not Store custody or receipt issuance.
Both entry points re-audit every supplied reservation. No caller bypass/cache is
accepted. Incremental indexes are local to one invocation and never authoritative.
"""

from dataclasses import dataclass, field
from hashlib import sha256
from typing import ClassVar

from . import http_contract as hc
from . import live_body_contract as c3
from . import live_http_budget as v1
from . import live_http_budget_v2 as v2
from . import live_http_evidence as c2
from . import live_store as store
from . import live_store_runtime as rt
from .live_enrollment_sources import decode_approval, decode_plan, decode_preflight
from .live_http_reservation_boundary import RESERVATION_KIND, semantic_request_identity

PREFIX = "historical-feasibility-artificial-admission-"
EVIDENCE_KIND = "artificial-evidence-operation-v1"
SETTLEMENT_KIND = "artificial-settlement-operation-v1"
ENROLLMENT_KIND = "artificial-enrollment-operation-v1"
ENROLLMENT_AUDIT_PREFIX = "artificial-enrollment:"
LOCAL_ELIGIBILITY_PEER_ONLY = frozenset({"byte_budget_exhausted"})
PEER_BLOCKER = frozenset(
    {
        "sticky_overrun",
        "open_unsettled",
        "measurement_not_committed",
        "conflicting_measurement",
        "unsupported_recovery_operation",
        "response_200_body_uncommitted",
        "body_not_stable",
        "body_not_committed_or_quarantined",
        "untouched_send_evidence",
        "body_measurement_missing",
        "object_attempt_missing",
        "attempt_multiple_objects",
        "final_digest_size_conflict",
        "successful_digest_duplicate",
        "successful_measurement_mismatch",
        "quarantine_outcome_invalid",
        "quarantine_is_not_http_saved",
        "quarantine_measurement_mismatch",
    }
)
BUDGET_REASON_REGISTRY = LOCAL_ELIGIBILITY_PEER_ONLY | PEER_BLOCKER
require = v2.require


def digest(data):
    require(type(data) is bytes, "bytes_required")
    return sha256(data).hexdigest()


def _json(text):
    raw = c3._load(text.encode())
    require(c3._canonical(raw) == text, "noncanonical_original")
    return raw


@dataclass(frozen=True)
class ValidationLimitsV1(c3.Value):
    """Explicit provisional input limits. No default/public numerical profile."""

    schema: ClassVar[str] = PREFIX + "validation-limits-v1"
    original_records: int
    total_canonical_bytes: int
    reservations: int
    plans: int
    references: int
    single_record_bytes: int

    def __post_init__(self):
        super().__post_init__()
        require(all(x > 0 for x in vars(self).values()), "positive_limits_required")


@dataclass(frozen=True)
class HeadReferenceV1(c3.Value):
    schema: ClassVar[str] = PREFIX + "head-v1"
    journal_id: str
    count: int
    head_sha: str

    def __post_init__(self):
        super().__post_init__()
        c3._sha(self.head_sha)
        require((self.count == 0) == (self.head_sha == c3.ZERO_HASH), "empty_head")


@dataclass(frozen=True)
class HistoricalCutV1(c3.Value):
    schema: ClassVar[str] = PREFIX + "cut-v1"
    heads: tuple[HeadReferenceV1, ...]
    runtime_sequence: int
    runtime_head: str
    store_policy_sha: str

    def __post_init__(self):
        super().__post_init__()
        ids = tuple(h.journal_id for h in self.heads)
        require(ids == tuple(sorted(set(ids))), "cut_head_order")
        c3._sha(self.runtime_head)
        c3._sha(self.store_policy_sha)


@dataclass(frozen=True)
class OperationPartV1(c3.Value):
    schema: ClassVar[str] = PREFIX + "part-v1"
    role: str
    ordinal: int
    journal_id: str
    sequence: int
    event_hash: str
    result_head: str


@dataclass(frozen=True)
class OperationOriginalV1(c3.Value):
    """Synthetic operation envelope; receipt uses the existing six-field shape.

    Its presence proves only consistency of this supplied artificial source pool.
    It never represents an operation committed by the production Store.
    """

    schema: ClassVar[str] = PREFIX + "operation-original-v1"
    operation_id: str
    kind: str
    plan_sha: str
    session_id: str
    fence_epoch: int
    intent_json: str
    preconditions_json: str
    source: HistoricalCutV1
    result: HistoricalCutV1
    parts: tuple[OperationPartV1, ...]
    receipt_json: str

    def __post_init__(self):
        super().__post_init__()
        for label in (self.operation_id, self.kind, self.session_id):
            c2._label(label)
        c3._sha(self.plan_sha)
        for value in (self.intent_json, self.preconditions_json, self.receipt_json):
            _json(value)
        require(self.fence_epoch > 0, "fence_required")


@dataclass(frozen=True)
class MeasurementAvailabilityV1(c3.Value):
    schema: ClassVar[str] = PREFIX + "measurement-availability-v1"
    measurement_sha: str
    operation_sha: str
    audit_event_sha: str


@dataclass(frozen=True)
class HistoricalReservationV1(c3.Value):
    schema: ClassVar[str] = PREFIX + "reservation-v1"
    operation_sha: str
    core_json: str
    slot_id: str

    def __post_init__(self):
        super().__post_init__()
        c3._sha(self.operation_sha)
        c3._sha(self.slot_id)
        _json(self.core_json)


@dataclass(frozen=True)
class JournalOriginalV1(c3.Value):
    schema: ClassVar[str] = PREFIX + "journal-original-v1"
    journal_id: str
    kind: str
    plan_sha: str | None
    journal_schema: str
    records: tuple[str, ...]

    def __post_init__(self):
        super().__post_init__()
        require(self.kind in {"account", "plan", "body"}, "journal_kind")
        require((self.kind == "account") == (self.plan_sha is None), "journal_scope")


@dataclass(frozen=True)
class PlanOriginalV1(c3.Value):
    schema: ClassVar[str] = PREFIX + "plan-original-v1"
    contract: c3.LiveBodyStorageContract
    root: c3.PhysicalRootIdentity
    approval_json: str
    preflight_json: str
    catalog_state: str

    def __post_init__(self):
        super().__post_init__()
        require(self.catalog_state in {"enrolled", "stopped"}, "catalog_state")
        _json(self.approval_json)
        _json(self.preflight_json)


@dataclass(frozen=True)
class SourcePoolV1:
    """Immutable supplied originals. The commitment is not an external anchor.

    Large pool/journal containers are bounded by explicit ValidationLimits, not
    the 32 KiB event limit. Individual canonical originals remain bounded.
    """

    identity: v1.Identity
    store_policy_json: str
    runtime_context: rt.RuntimeContext
    runtime_records: tuple[bytes, ...]
    plans: tuple[PlanOriginalV1, ...]
    journals: tuple[JournalOriginalV1, ...]
    initial: HistoricalCutV1
    operations: tuple[OperationOriginalV1, ...]
    reservations: tuple[HistoricalReservationV1, ...]
    measurements: tuple[v1.MeasurementOriginal, ...]
    availability: tuple[MeasurementAvailabilityV1, ...]

    def __post_init__(self):
        for name, expected in (
            ("identity", v1.Identity),
            ("runtime_context", rt.RuntimeContext),
            ("initial", HistoricalCutV1),
        ):
            require(type(getattr(self, name)) is expected, "source_type")
        for name, expected in (
            ("runtime_records", bytes),
            ("plans", PlanOriginalV1),
            ("journals", JournalOriginalV1),
            ("operations", OperationOriginalV1),
            ("reservations", HistoricalReservationV1),
            ("measurements", v1.MeasurementOriginal),
            ("availability", MeasurementAvailabilityV1),
        ):
            values = getattr(self, name)
            require(
                type(values) is tuple and all(type(v) is expected for v in values),
                "source_type",
            )
        _json(self.store_policy_json)

    def records(self):
        """Canonical originals, with container identity/ordering committed too."""
        return tuple(self._iter_records())

    def _iter_records(self):
        """Stream canonical records after the inexpensive input guard."""
        ctx = self.runtime_context
        yield from (
            self.identity.to_bytes(),
            self.store_policy_json.encode(),
            ctx.deployment.canonical,
            ctx.runtime_policy.canonical,
            ctx.owner_intent.canonical,
            *(p.canonical for p in ctx.owner_pins),
            self.initial.to_bytes(),
        )
        yield from self.runtime_records
        for p in self.plans:
            yield p.to_bytes()
        for j in self.journals:
            yield c3._canonical(
                {
                    "schema": j.schema,
                    "journal_id": j.journal_id,
                    "kind": j.kind,
                    "plan_sha": j.plan_sha,
                    "journal_schema": j.journal_schema,
                    "count": len(j.records),
                }
            ).encode()
            for r in j.records:
                yield r.encode()
        for values in (
            self.operations,
            self.reservations,
            self.measurements,
            self.availability,
        ):
            for v in values:
                yield v.to_bytes()

    @property
    def commitment(self):
        return c3._hash(
            {
                "domain": PREFIX + "source-pool-v1",
                "records": [digest(r) for r in self.records()],
            }
        )


@dataclass(frozen=True)
class AdmissionCoreV2(c3.Value):
    schema: ClassVar[str] = "historical-feasibility-http-budget-admission-core-v2"
    identity: v1.Identity
    plan_sha: str
    approval_sha: str
    preflight_sha: str
    policy: v2.BudgetPolicyV2
    page: c3.LivePageIdentity
    attempt_number: int
    semantic_request_sha: str
    cut_sha: str
    prior_operations_sha: str
    budget_sha: str
    capacity_sha: str
    allowance: v1.Allowances
    producer_authenticated: bool = field(default=False, init=False)
    reservation_permitted: bool = field(default=False, init=False)
    live_send_permitted: bool = field(default=False, init=False)

    def __post_init__(self):
        super().__post_init__()
        for name in (
            "plan_sha",
            "approval_sha",
            "preflight_sha",
            "semantic_request_sha",
            "cut_sha",
            "prior_operations_sha",
            "budget_sha",
            "capacity_sha",
        ):
            c3._sha(getattr(self, name))
        require(
            self.attempt_number > 0 and self.plan_sha == self.page.plan_sha,
            "core_binding",
        )
        require(
            self.semantic_request_sha
            == semantic_request_identity(
                self.plan_sha, self.page.query_id, self.page.page_index
            ),
            "semantic_request",
        )


def derive_slot_id_v2(core, operation_id):
    require(type(core) is AdmissionCoreV2, "core_v2_required")
    c2._label(operation_id)
    return c3._hash(
        {
            "domain": "http-reservation-slot-v2",
            "core_sha": core.sha256,
            "operation_id": operation_id,
        }
    )


@dataclass(frozen=True)
class AdmissionAssessmentV2(c3.Value):
    schema: ClassVar[str] = PREFIX + "assessment-v2"
    saved_compatible: bool
    retry_eligible: bool
    restrictions_satisfied: bool
    capacity_satisfied: bool
    budget: v2.BudgetResultV2
    capacity: v2.CapacityAssessmentV2
    reasons: tuple[str, ...]
    producer_authenticated: bool = field(default=False, init=False)
    reservation_permitted: bool = field(default=False, init=False)
    live_send_permitted: bool = field(default=False, init=False)


@dataclass(frozen=True)
class ReservationValidationV1(c3.Value):
    schema: ClassVar[str] = PREFIX + "reservation-validation-v1"
    operation_id: str
    assessment: AdmissionAssessmentV2


@dataclass(frozen=True)
class SequentialHistoryValidationV1(c3.Value):
    schema: ClassVar[str] = PREFIX + "sequential-validation-v1"
    historical_admissions_valid: bool
    first_violation: int | None
    reason: str | None
    input_commitment: str
    checks: tuple[ReservationValidationV1, ...]
    final_budgets: tuple[v2.BudgetResultV2, ...]
    final_capacity: tuple[v2.PlanCapacityV2, ...]
    producer_authenticated: bool = field(default=False, init=False)
    reservation_permitted: bool = field(default=False, init=False)
    live_send_permitted: bool = field(default=False, init=False)

    def to_bytes(self):
        # A bounded aggregate, not a persisted event. Limits bound its inputs.
        return c3._canonical(self.to_dict()).encode()


@dataclass
class ReplayStatistics:
    """Instrumentation only; never input authority or part of canonical results."""

    full_replays: int = 0
    indexed_events: int = 0
    prefix_hits: int = 0
    retained_prefixes: int = 0


def _dependency_count(pool, maximum=None):
    """Bound explicit operation edges plus nested journal/measurement links."""
    count = sum(
        len(o.source.heads) + len(o.result.heads) + len(o.parts) + 2
        for o in pool.operations
    )
    count += (
        len(pool.availability) * 3
        + len(pool.reservations) * 2
        + len(pool.initial.heads)
    )
    count += (
        len(pool.measurements) * 3 + len(pool.runtime_records) + len(pool.plans) * 4
    )

    def bounded(value):
        value = c3._integer(value)
        require(maximum is None or value <= maximum, "limit_references")
        return value

    bounded(count)
    for journal in pool.journals:
        count += len(journal.records)  # previous-hash edges
        bounded(count)
        for line in journal.records:
            row = _json(line)
            event = row.get("event")
            require(type(event) is dict, "dependency_event_shape")
            require(type(event.get("kind")) is str, "dependency_event_shape")
            if journal.kind == "body":
                require("c2_heads" in event, "dependency_heads_shape")
                heads = event["c2_heads"]
                if heads is not None:
                    require(
                        type(heads) is dict and type(heads.get("plan_heads")) is list,
                        "dependency_heads_shape",
                    )
                    count += 1 + len(heads["plan_heads"])
            if event["kind"] == "plan_enrolled":
                # Enrollment originals already validate this mapping's shape.
                data = event.get("data")
                require(type(data) is dict, "dependency_enrollment_shape")
                prior = data.get("prior_plan_heads", ())
                require(type(prior) in {list, tuple}, "dependency_enrollment_shape")
                count += len(prior) + 1
            bounded(count)
    return bounded(count)


def _cheap_limits(pool, limits):
    """No parsing, canonical expansion or replay before count/raw byte bounds.

    The caller's already-created Python objects are outside this resource guard.
    UTF-8 encoding is bounded by first checking the string's character count.
    """
    require(
        type(pool) is SourcePoolV1 and type(limits) is ValidationLimitsV1,
        "limits_required",
    )
    for name in ("plans", "reservations"):
        require(len(getattr(pool, name)) <= getattr(limits, name), "limit_" + name)
    count = 6 + len(pool.runtime_context.owner_pins) + len(pool.runtime_records)
    count += len(pool.plans) + len(pool.journals)
    count += sum(len(j.records) for j in pool.journals)
    count += sum(
        len(values)
        for values in (
            pool.operations,
            pool.reservations,
            pool.measurements,
            pool.availability,
        )
    )
    require(count <= limits.original_records, "limit_original_records")
    raw_total = 0

    def charge(raw):
        nonlocal raw_total
        require(len(raw) <= limits.single_record_bytes, "limit_single_record_bytes")
        size = len(raw.encode()) if type(raw) is str else len(raw)
        require(size <= limits.single_record_bytes, "limit_single_record_bytes")
        raw_total += size
        require(
            raw_total <= limits.total_canonical_bytes, "limit_total_canonical_bytes"
        )

    charge(pool.store_policy_json)
    for raw in pool.runtime_records:
        charge(raw)
    for journal in pool.journals:
        for raw in journal.records:
            charge(raw)
    return count


def _limits(pool, limits):
    count = _cheap_limits(pool, limits)
    total_bytes = largest = emitted = 0
    for raw in pool._iter_records():
        size = len(raw)
        require(size <= limits.single_record_bytes, "limit_single_record_bytes")
        total_bytes = v2.total((total_bytes, size))
        require(
            total_bytes <= limits.total_canonical_bytes, "limit_total_canonical_bytes"
        )
        largest = max(largest, size)
        emitted += 1
    require(emitted == count, "record_count_mismatch")
    refs = _dependency_count(pool, limits.references)
    actual = (
        count,
        total_bytes,
        len(pool.reservations),
        len(pool.plans),
        refs,
        largest,
    )
    for key, count in zip(vars(limits), actual, strict=True):
        require(count <= getattr(limits, key), "limit_" + key)
    return actual


def measure_input_size(pool):
    """Read-only sizing helper; no defaults or limit approval."""
    rows = pool.records()
    return ValidationLimitsV1(
        len(rows),
        v2.total(len(r) for r in rows),
        max(1, len(pool.reservations)),
        max(1, len(pool.plans)),
        max(1, _dependency_count(pool)),
        max(map(len, rows)),
    )


class _Index:
    def __init__(self, pool, incremental, stats):
        self.pool, self.incremental, self.stats = pool, incremental, stats
        self.sources = {j.journal_id: j for j in pool.journals}
        require(len(self.sources) == len(pool.journals), "duplicate_journal")
        require(tuple(self.sources) == tuple(sorted(self.sources)), "journal_order")
        self.plans = {p.contract.plan.plan_sha: p for p in pool.plans}
        require(len(self.plans) == len(pool.plans), "duplicate_plan")
        require(tuple(self.plans) == tuple(sorted(self.plans)), "plan_order")
        self.rows, self.views = {}, {}
        for j in pool.journals:
            require(j.kind == "account" or j.plan_sha in self.plans, "catalog_coverage")
            self.rows[j.journal_id] = tuple(_json(r) for r in j.records)
            self.stats.full_replays += 1
            if j.kind == "body":
                p = self.plans[j.plan_sha]
                require(j.journal_schema == c3.EVENT_SCHEMA, "body_schema")
                c3.LiveBodyJournal(p.contract, p.root, j.records)
            else:
                require(
                    j.journal_schema
                    in (
                        {c2.LIVE_ACCOUNT_SCHEMA_V1, c2.LIVE_ACCOUNT_SCHEMA_V2}
                        if j.kind == "account"
                        else {c2.LIVE_PLAN_SCHEMA}
                    ),
                    "c2_schema",
                )
                c2.LiveJournal(j.journal_schema, j.records)
            if incremental:
                self._index(j)
        accounts = [j for j in pool.journals if j.kind == "account"]
        require(len(accounts) == 1, "account_journal_required")
        self.account_id = accounts[0].journal_id
        self.plan_ids = {
            j.plan_sha: j.journal_id for j in pool.journals if j.kind == "plan"
        }
        self.body_ids = {
            j.plan_sha: j.journal_id for j in pool.journals if j.kind == "body"
        }
        require(
            len(self.sources) == 1 + 2 * len(self.plans)
            and set(self.plan_ids) == set(self.body_ids) == set(self.plans),
            "catalog_coverage",
        )

    def _index(self, j):
        views = []
        if j.kind == "body":
            objects = {}
            p = self.plans[j.plan_sha]
            views.append(c3._projection(objects))
            for raw in self.rows[j.journal_id]:
                c3._apply(
                    p.contract, p.root, objects, c3.BodyEvent.from_dict(raw["event"])
                )
                views.append(c3._projection(objects))
                self.stats.indexed_events += 1
        else:
            reducer = (
                c2._AccountReducerV2()
                if j.journal_schema == c2.LIVE_ACCOUNT_SCHEMA_V2
                else c2._Reducer(j.journal_schema)
            )
            views.append(None)
            for raw in self.rows[j.journal_id]:
                if isinstance(reducer, c2._AccountReducerV2):
                    reducer.previous_hash, reducer.event_hash = (
                        raw["previous_hash"],
                        raw["event_hash"],
                    )
                reducer.apply(raw["event"])
                views.append(reducer.projection())
                self.stats.indexed_events += 1
        self.views[j.journal_id] = views
        self.stats.retained_prefixes += len(views)

    def get(self, ref):
        require(ref.journal_id in self.sources, "unknown_journal")
        j, rows = self.sources[ref.journal_id], self.rows[ref.journal_id]
        require(ref.count <= len(rows), "prefix_count")
        require(
            ref.head_sha
            == (rows[ref.count - 1]["event_hash"] if ref.count else c3.ZERO_HASH),
            "prefix_head",
        )
        if self.incremental:
            self.stats.prefix_hits += 1
            return self.views[j.journal_id][ref.count]
        self.stats.full_replays += 1
        if j.kind == "body":
            p = self.plans[j.plan_sha]
            return c3.LiveBodyJournal(
                p.contract, p.root, j.records[: ref.count]
            ).projection
        return (
            c2.LiveJournal(j.journal_schema, j.records[: ref.count]).projection
            if ref.count
            else None
        )

    def cut(self, cut):
        selected = {h.journal_id: self.get(h) for h in cut.heads}
        require(
            self.account_id in selected and selected[self.account_id] is not None,
            "cut_account_missing",
        )
        known = {s.plan_sha for s in selected[self.account_id].scopes}
        require(known <= set(self.plans), "cut_plan_unknown")
        expected = (
            {self.account_id}
            | {self.plan_ids[s] for s in known}
            | {self.body_ids[s] for s in known}
        )
        require(set(selected) == expected, "cut_coverage")
        # Plans enrolled later have no journal at this cut. Their final originals
        # cannot alter an earlier Core/capacity commitment.
        defaults = {jid: None for jid in self.plan_ids.values()}
        defaults.update({jid: c3._projection({}) for jid in self.body_ids.values()})
        return {**defaults, **selected}


def _reconcile(index, views):
    ap = views[index.account_id]
    require(ap is not None, "account_missing")
    scopes = {s.plan_sha: s for s in ap.scopes}
    present = {sha for sha, jid in index.plan_ids.items() if views[jid] is not None}
    require(set(scopes) == present, "enrollment_coverage")
    for sha in present:
        pp = views[index.plan_ids[sha]]
        require(pp.scopes == (scopes[sha],), "scope_binding")
        aa = tuple(a for a in ap.attempts if a.binding.plan_sha == sha)
        require(len(aa) == len(pp.attempts), "attempt_pair_missing")
        for a, p in zip(aa, pp.attempts, strict=True):
            for name in (
                "binding",
                "reserved_at",
                "reservation_transition",
                "sent_at",
                "send_transition",
                "header",
                "header_transition",
            ):
                require(getattr(a, name) == getattr(p, name), "attempt_pair_mismatch")
            if a.outcome == "pre_send_reclaim":
                require(p.state == "reserved" and p.sent_at is None, "reclaim_pair")
            else:
                require(
                    (a.outcome, a.settled_at, a.settlement_transition)
                    == (p.outcome, p.settled_at, p.settlement_transition),
                    "settlement_pair_mismatch",
                )
    require(c2._holds_cover(ap), "hold_coverage")
    return ap


def _chain_start():
    return c3._hash({"domain": PREFIX + "operation-chain-v1", "empty": True})


def _chain_next(head, operation):
    return c3._hash(
        {
            "domain": PREFIX + "operation-chain-v1",
            "previous": head,
            "original": operation.sha256,
        }
    )


def _registered_budget_reasons(budget, *, peer):
    reasons = set(budget.reasons) | set(budget.saved.reasons)
    require(reasons <= BUDGET_REASON_REGISTRY, "unknown_budget_reason")
    return reasons - LOCAL_ELIGIBILITY_PEER_ONLY if peer else reasons


def _assessment(pool, index, cut, page, available, allowances):
    views = index.cut(cut)
    ap = _reconcile(index, views)
    source = index.plans[page.plan_sha]
    plan = decode_plan(source.contract.plan.canonical_plan.encode(), page.plan_sha)
    scope = ap.scope(page.plan_sha)
    at = c2._time(
        rt.RuntimeEvent.from_bytes(
            pool.runtime_records[cut.runtime_sequence]
        ).observation.utc
    )
    pp = views[index.plan_ids[page.plan_sha]]
    bp = views[index.body_ids[page.plan_sha]]
    attempts = tuple(a for a in ap.attempts if a.binding.plan_sha == page.plan_sha)
    originals = tuple(m for m in available if m.binding.plan_sha == page.plan_sha)
    budget = v2.budget_result(plan, bp, attempts, pp.attempts, originals, allowances)
    capacity = v2.assess_capacity_v2(
        {s.plan_sha: index.plans[s.plan_sha].contract for s in ap.scopes},
        {s.plan_sha: views[index.body_ids[s.plan_sha]] for s in ap.scopes},
        target_plan_sha=page.plan_sha,
        store_policy=store.StorePolicy.from_bytes(pool.store_policy_json.encode()),
    )
    reasons = _registered_budget_reasons(budget, peer=False) | set(capacity.reasons)
    restrictions = set()
    if (
        not scope.not_before <= at < scope.expires_at
        or not plan.not_before <= at < plan.expires_at
    ):
        restrictions.add("scope_expired_or_not_started")
    if any(a.outcome is None for a in ap.attempts):
        restrictions.add("account_open_attempt")
    for hold in ap.holds:
        if hold.released_at is None:
            restrictions.add("hold_unreleased")
        if hold.hold.not_before is not None and at < hold.hold.not_before:
            restrictions.add("known_wait_not_elapsed")
    if (
        attempts
        and (at - attempts[0].reserved_at).total_seconds()
        >= plan.limits.max_elapsed_seconds
    ):
        restrictions.add("elapsed_budget_exhausted")
    retry = set()
    receipts = [o.receipt for o in bp.objects if o.receipt]
    try:
        query = v1._page_progress(plan, page, receipts)
        request = hc.PageRequest(query, page.page_index, page.cursor)
    except hc.HttpContractError as exc:
        retry.add(str(exc))
        request = None
    related = [
        a
        for a in attempts
        if any(
            o.identity.binding == a.binding and o.identity.page == page
            for o in bp.objects
        )
    ]
    if related:
        a = related[-1]
        if (
            a.outcome == "response"
            and a.header
            and a.header.status not in {429}
            and not 500 <= a.header.status < 600
        ):
            retry.add("previous_attempt_not_retryable")
    number = len(related) + 1
    if number > plan.retry.max_attempts_per_page:
        retry.add("retry_budget_exceeded")
    if len(attempts) >= plan.limits.max_attempts:
        retry.add("attempt_budget_exhausted")
    if (
        page.page_index >= plan.limits.max_pages_per_query
        or len(receipts) >= plan.limits.max_pages_total
    ):
        retry.add("page_budget_exhausted")
    for sha, jid in index.body_ids.items():
        other = views[jid]
        if other.unstable or other.retained_body_bytes is None:
            restrictions.add("unstable_body_inventory")
        # No other plan's unresolved success/measurement may be silently hidden.
        other_attempts = tuple(a for a in ap.attempts if a.binding.plan_sha == sha)
        other_originals = tuple(m for m in available if m.binding.plan_sha == sha)
        p = views[index.plan_ids[sha]]
        if p is not None:
            other_plan = decode_plan(
                index.plans[sha].contract.plan.canonical_plan.encode(), sha
            )
            other_budget = v2.budget_result(
                other_plan,
                other,
                other_attempts,
                p.attempts,
                other_originals,
                allowances,
            )
            restrictions.update(
                _registered_budget_reasons(other_budget, peer=sha != page.plan_sha)
            )
    reasons |= restrictions | retry
    assessment = AdmissionAssessmentV2(
        budget.saved.compatible,
        not retry,
        not restrictions,
        capacity.satisfied,
        budget,
        capacity,
        tuple(sorted(reasons)),
    )
    return assessment, number, request, scope, ap


def _core(pool, index, cut, page, available, allowances, prior):
    assessment, number, request, scope, ap = _assessment(
        pool, index, cut, page, available, allowances
    )
    core = AdmissionCoreV2(
        pool.identity,
        page.plan_sha,
        scope.approval_sha,
        scope.preflight_sha,
        v2.BudgetPolicyV2(),
        page,
        number,
        semantic_request_identity(page.plan_sha, page.query_id, page.page_index),
        cut.sha256,
        prior,
        assessment.budget.sha256,
        assessment.capacity.sha256,
        assessment.budget.allowance,
    )
    return core, assessment, request, ap


def _runtime(pool, stats):
    stats.full_replays += 1
    rt.replay_runtime(pool.runtime_context, pool.runtime_records)
    events = tuple(rt.RuntimeEvent.from_bytes(r) for r in pool.runtime_records)
    require(
        all(
            e.canonical == r for e, r in zip(events, pool.runtime_records, strict=True)
        ),
        "runtime_noncanonical",
    )
    return events


def _cut_runtime(pool, cut, events):
    require(cut.runtime_sequence < len(events), "runtime_cut_missing")
    e = events[cut.runtime_sequence]
    require(e.event_hash == cut.runtime_head, "runtime_cut_mismatch")
    require(
        cut.store_policy_sha == digest(pool.store_policy_json.encode()),
        "store_policy_binding",
    )
    return e


def _validate_sources(pool, index, events):
    d = pool.runtime_context.deployment
    require(
        (pool.identity.deployment_id, pool.identity.store_id, pool.identity.account_ref)
        == (d.deployment_id, d.store_id, d.account_ref),
        "runtime_identity",
    )
    policy = store.StorePolicy.from_bytes(pool.store_policy_json.encode())
    require(d.store_policy_sha == policy.sha256, "deployment_policy")
    require(
        len(pool.plans) <= policy.max_enrolled_plans_per_account, "store_plan_count"
    )
    last_account = index.sources[index.account_id]
    account = c2.LiveJournal(last_account.journal_schema, last_account.records)
    plans = tuple(
        c2.LiveJournal(index.sources[j].journal_schema, index.sources[j].records)
        for j in index.plan_ids.values()
    )
    require(
        c2.reconcile_live_journals(account, plans).classification == "consistent",
        "full_c2_inconsistent",
    )
    require(
        set(index.plans) == {s.plan_sha for s in account.projection.scopes},
        "catalog_enrollment_coverage",
    )
    for sha, source in index.plans.items():
        plan = decode_plan(source.contract.plan.canonical_plan.encode(), sha)
        scope = account.projection.scope(sha)
        approval = decode_approval(
            source.approval_json.encode(), scope.approval_sha, plan
        )
        preflight = decode_preflight(
            source.preflight_json.encode(), source.contract.preflight_sha
        )
        require(
            scope.preflight_sha == d.preflight_sha == source.contract.preflight_sha,
            "preflight_binding",
        )
        require(
            scope.account_ref
            == plan.account_ref
            == pool.identity.account_ref
            == preflight.identity.account_ref,
            "account_binding",
        )
        require(
            scope.not_before == max(plan.not_before, approval.valid_from)
            and scope.expires_at == min(plan.expires_at, approval.valid_until),
            "approval_window",
        )
        require(
            scope.retry == plan.retry
            and scope.account_policy == preflight.rate_policy
            and scope.clock_policy == preflight.clock_policy,
            "scope_policy_binding",
        )
        source.contract.to_bytes()
    _cut_runtime(pool, pool.initial, events)
    initial = index.cut(pool.initial)
    ap = _reconcile(index, initial)
    require(not ap.attempts and not ap.holds, "initial_history_not_empty")
    require(
        all(not initial[j].objects for j in index.body_ids.values()),
        "initial_body_not_empty",
    )
    # Initial bootstrap/enrollment is supplied evidence, never a hidden attempt.
    require(
        all(
            len({m.sha256 for m in values}) == len(values)
            for values in (pool.measurements, pool.availability)
        ),
        "duplicate_original",
    )
    require(
        len({o.operation_id for o in pool.operations}) == len(pool.operations),
        "operation_id_duplicate",
    )
    require(
        all(
            o.kind
            in {RESERVATION_KIND, EVIDENCE_KIND, SETTLEMENT_KIND, ENROLLMENT_KIND}
            for o in pool.operations
        ),
        "unsupported_recovery_operation",
    )
    require(
        len({r.operation_sha for r in pool.reservations}) == len(pool.reservations),
        "reservation_duplicate",
    )
    require(
        len({a.measurement_sha for a in pool.availability}) == len(pool.availability),
        "availability_duplicate",
    )
    require(
        {a.measurement_sha for a in pool.availability}
        == {m.sha256 for m in pool.measurements},
        "availability_coverage",
    )
    require(
        len({m.binding for m in pool.measurements}) == len(pool.measurements),
        "duplicate_attempt_measurement",
    )
    require(
        {r.operation_sha for r in pool.reservations}
        == {o.sha256 for o in pool.operations if o.kind == RESERVATION_KIND},
        "reservation_coverage",
    )
    require(
        all(
            a.operation_sha in {o.sha256 for o in pool.operations}
            for a in pool.availability
        ),
        "measurement_operation_missing",
    )


def _enrollment_profile(index, op, before, after, additions):
    """One new B: account enrollment, B plan opening, B initial body audit."""
    sha = op.plan_sha
    plan_id, body_id = index.plan_ids[sha], index.body_ids[sha]
    prior = before[index.account_id]
    current = after[index.account_id]
    require(
        sha not in {s.plan_sha for s in prior.scopes}
        and {s.plan_sha for s in current.scopes}
        == {s.plan_sha for s in prior.scopes} | {sha}
        and before[plan_id] is None
        and not before[body_id].objects,
        "enrollment_prior_scope",
    )
    ids = (index.account_id, plan_id, body_id)
    require(
        len(additions) == 3 and {jid for jid, _ in additions} == set(ids),
        "enrollment_event_profile",
    )
    rows = dict(additions)
    require(
        tuple(rows[jid]["event"]["kind"] for jid in ids)
        == ("plan_enrolled", "journal_opened", "audit")
        and rows[plan_id]["sequence"] == rows[body_id]["sequence"] == 0,
        "enrollment_event_profile",
    )
    require(
        tuple((p.journal_id, p.role) for p in op.parts)
        == tuple(
            zip(
                ids,
                ("account-enrollment", "plan-enrollment", "body-enrollment"),
                strict=True,
            )
        ),
        "enrollment_part_profile",
    )
    account_event, plan_event, body_event = (rows[jid]["event"] for jid in ids)
    require(
        account_event["recorded_at"]
        == plan_event["recorded_at"]
        <= body_event["recorded_at"],
        "enrollment_event_order",
    )
    body_event = c3.BodyEvent.from_dict(body_event)
    require(
        body_event.object_id is None
        and body_event.c2_heads is None
        and _json(body_event.payload_json)
        == {"evidence_ref": ENROLLMENT_AUDIT_PREFIX + sha},
        "enrollment_body_audit",
    )
    # Existing C2 enrollment replay verifies document/owner/previous-head and
    # paired opening bindings. C3 replay verifies the supplied contract/root.
    _reconcile(index, after)


def _operation(pool, index, op, previous, events):
    """Validate an entire atomic artificial operation, including every part."""
    require(
        op.kind in {RESERVATION_KIND, EVIDENCE_KIND, SETTLEMENT_KIND, ENROLLMENT_KIND},
        "unsupported_recovery_operation",
    )
    require(op.plan_sha in index.plans, "operation_plan")
    require(op.source.heads == previous.heads, "operation_source_gap")
    start = _cut_runtime(pool, op.source, events)
    end = _cut_runtime(pool, op.result, events)
    require(
        start.sequence > previous.runtime_sequence
        and end.sequence == start.sequence + 1,
        "operation_commit_order",
    )
    require(
        start.kind == end.kind == "clock_observed", "committed_business_clock_required"
    )
    require(
        (start.session_id, start.fence_epoch)
        == (end.session_id, end.fence_epoch)
        == (op.session_id, op.fence_epoch),
        "stale_fence_or_session",
    )
    require(start.observation.utc <= end.observation.utc, "commit_clock_order")
    before, after = index.cut(op.source), index.cut(op.result)
    require(set(before) == set(after), "operation_cut_coverage")
    intent, pre = _json(op.intent_json), _json(op.preconditions_json)
    require(
        set(intent)
        == {"schema", "kind", "account_ref", "plan_sha", "measurement_shas", "core_sha"}
        and intent["schema"] == PREFIX + "intent-v1",
        "intent_schema",
    )
    require(
        (intent["kind"], intent["account_ref"], intent["plan_sha"])
        == (op.kind, pool.identity.account_ref, op.plan_sha),
        "intent_binding",
    )
    require(
        pre
        == {
            "schema": PREFIX + "preconditions-v1",
            "source": op.source.to_dict(),
            "session_id": op.session_id,
            "fence_epoch": op.fence_epoch,
            "measurement_shas": intent["measurement_shas"],
            "core_sha": intent["core_sha"],
        },
        "preconditions_binding",
    )
    expected_measurements = sorted(
        a.measurement_sha for a in pool.availability if a.operation_sha == op.sha256
    )
    require(
        intent["measurement_shas"] == expected_measurements,
        "measurement_precondition_binding",
    )
    require(
        (op.kind == SETTLEMENT_KIND) == bool(expected_measurements),
        "settlement_measurement_required",
    )
    if op.kind != RESERVATION_KIND:
        require(intent["core_sha"] is None, "unexpected_core")
    additions = []
    source_heads = {h.journal_id: h for h in op.source.heads}
    for h in op.result.heads:
        prior = source_heads.get(
            h.journal_id, HeadReferenceV1(h.journal_id, 0, c3.ZERO_HASH)
        )
        require(h.count >= prior.count, "head_regressed")
        for row in index.rows[h.journal_id][prior.count : h.count]:
            require(
                start.observation.utc
                <= row["event"]["recorded_at"]
                <= end.observation.utc,
                "event_commit_time",
            )
            additions.append((h.journal_id, row))
    require(
        bool(additions) and len(op.parts) == len(additions), "operation_parts_missing"
    )
    allowed = {
        index.account_id,
        index.plan_ids[op.plan_sha],
        index.body_ids[op.plan_sha],
    }
    require(
        all(p.journal_id in allowed for p in op.parts)
        and all(jid in allowed for jid, _ in additions),
        "part_scope_mismatch",
    )
    if op.kind == ENROLLMENT_KIND:
        _enrollment_profile(index, op, before, after, additions)
    else:
        require(
            not any(
                row["event"]["kind"] in {"plan_enrolled", "journal_opened"}
                for _, row in additions
            )
            and {s.plan_sha for s in before[index.account_id].scopes}
            == {s.plan_sha for s in after[index.account_id].scopes},
            "enrollment_operation_required",
        )
        for jid, row in additions:
            if index.sources[jid].kind == "body" and row["event"]["kind"] == "audit":
                event = c3.BodyEvent.from_dict(row["event"])
                require(
                    not _json(event.payload_json)["evidence_ref"].startswith(
                        ENROLLMENT_AUDIT_PREFIX
                    ),
                    "enrollment_operation_required",
                )
    reservation_events = {"slot_reserved", "attempt_reserved", "reserved"}
    require(
        (op.kind == RESERVATION_KIND)
        == any(r["event"]["kind"] in reservation_events for _, r in additions),
        "reservation_operation_required",
    )
    if op.kind == EVIDENCE_KIND:
        require(
            not any(
                r["event"]["kind"]
                in {
                    "response_received",
                    "outcome_unknown",
                    "slot_settled",
                    "slot_reclaimed",
                }
                for _, r in additions
            ),
            "settlement_operation_required",
        )
    actual_parts = {(p.journal_id, p.sequence, p.event_hash) for p in op.parts}
    require(
        len(actual_parts) == len(op.parts)
        and actual_parts
        == {(jid, r["sequence"], r["event_hash"]) for jid, r in additions},
        "operation_part_binding",
    )
    require(
        tuple(p.ordinal for p in op.parts) == tuple(range(len(op.parts))), "part_order"
    )
    if op.kind in {EVIDENCE_KIND, SETTLEMENT_KIND}:
        require(
            tuple((p.journal_id, p.sequence, p.role) for p in op.parts)
            == tuple(
                (jid, sequence, f"evidence-{i}")
                for i, (jid, sequence) in enumerate(
                    sorted((jid, row["sequence"]) for jid, row in additions)
                )
            ),
            "evidence_part_profile",
        )
    require(all(p.result_head == p.event_hash for p in op.parts), "part_result_head")
    receipt = _json(op.receipt_json)
    require(
        receipt
        == {
            "schema": store.RECEIPT_SCHEMA,
            "deployment_id": pool.identity.deployment_id,
            "operation_id": op.operation_id,
            "intent_sha": digest(op.intent_json.encode()),
            "committed_at": end.observation.utc,
            "parts": [
                {k: v for k, v in p.to_dict().items() if k != "schema"}
                for p in op.parts
            ],
        },
        "original_receipt_mismatch",
    )
    # Validate each recorded C3 dependency against a prefix available in this
    # operation. A later-generation/body receipt cannot be smuggled into a cut.
    for jid, row in additions:
        j = index.sources[jid]
        if j.kind != "body":
            continue
        event = c3.BodyEvent.from_dict(row["event"])
        if event.c2_heads is None:
            continue
        refs = []
        for peer_id in (index.account_id, *index.plan_ids.values()):
            heads = (
                {event.c2_heads.account_head}
                if peer_id == index.account_id
                else set(event.c2_heads.plan_heads)
            )
            matches = [
                r
                for r in index.rows[peer_id][
                    : next(
                        (h.count for h in op.result.heads if h.journal_id == peer_id), 0
                    )
                ]
                if r["event_hash"] in heads
            ]
            if matches:
                require(len(matches) == 1, "body_dependency_ambiguous")
                r = matches[0]
                require(
                    c2._time(r["event"]["recorded_at"]) <= event.recorded_at,
                    "body_dependency_after_event",
                )
                refs.append(
                    HeadReferenceV1(peer_id, r["sequence"] + 1, r["event_hash"])
                )
        require(
            {r.head_sha for r in refs}
            == {event.c2_heads.account_head, *event.c2_heads.plan_heads},
            "body_dependency_future_or_missing",
        )
        selected = {r.journal_id: index.get(r) for r in refs}
        _reconcile(
            index, {**{jid: None for jid in index.plan_ids.values()}, **selected}
        )
        aa = selected[index.account_id]
        obj = after[jid].objects
        identity = next(
            o.identity for o in obj if o.identity.object_id == event.object_id
        )
        attempt = aa.attempt(identity.binding.plan_sha, identity.binding.attempt_id)
        require(attempt.binding == identity.binding, "body_binding")
        if event.kind in {"reserved", "committed"}:
            require(
                identity.binding.generation
                == aa.generation
                == after[index.account_id].generation,
                "body_generation",
            )
        if event.kind == "reserved":
            require(attempt.state == "reserved", "body_reservation_state")
        else:
            require(
                attempt.outcome == "response" and attempt.sent_at is not None,
                "body_complete_response",
            )
            if event.kind == "committed":
                require(attempt.header.status == 200, "body_success_response")
    _reconcile(index, after)
    return intent, additions


def _available(pool, index, op, additions):
    available = []
    by_sha = {m.sha256: m for m in pool.measurements}
    for a in pool.availability:
        if a.operation_sha != op.sha256:
            continue
        m = by_sha[a.measurement_sha]
        require(
            m.identity == pool.identity and m.binding.plan_sha == op.plan_sha,
            "measurement_identity",
        )
        body_id, plan_id = index.body_ids[op.plan_sha], index.plan_ids[op.plan_sha]
        matched = [
            (jid, r) for jid, r in additions if r["event_hash"] == a.audit_event_sha
        ]
        require(
            len(matched) == 1 and matched[0][0] == body_id, "measurement_audit_missing"
        )
        audit = c3.BodyEvent.from_dict(matched[0][1]["event"])
        require(
            audit.kind == "audit"
            and _json(audit.payload_json) == {"evidence_ref": m.sha256},
            "measurement_audit_binding",
        )
        require(audit.recorded_at >= m.observed_at, "measurement_observed_after_audit")
        refs = []
        for jid, head in (
            (index.account_id, m.account_head),
            (plan_id, m.plan_head),
            (body_id, m.body_head),
        ):
            cutoff = next(h.count for h in op.result.heads if h.journal_id == jid)
            matches = [r for r in index.rows[jid][:cutoff] if r["event_hash"] == head]
            require(len(matches) == 1, "measurement_future_source")
            row = matches[0]
            require(
                c2._time(row["event"]["recorded_at"]) <= m.observed_at,
                "measurement_observation_order",
            )
            refs.append(HeadReferenceV1(jid, row["sequence"] + 1, head))
        aa, pp, bb = (index.get(r) for r in refs)
        attempt = aa.attempt(m.binding.plan_sha, m.binding.attempt_id)
        peer = pp.attempt(m.binding.plan_sha, m.binding.attempt_id)
        require(
            attempt.binding == peer.binding == m.binding
            and attempt.outcome == peer.outcome
            and attempt.outcome in {"response", "unknown"},
            "measurement_outcome_binding",
        )
        require(
            attempt.reserved_at <= m.observed_at
            and attempt.settled_at <= m.observed_at,
            "measurement_time",
        )
        prior_pages = [
            o
            for o in bb.objects
            if o.identity.page == m.page
            and o.identity.binding.generation <= m.binding.generation
        ]
        require(m.attempt_number == len(prior_pages), "measurement_attempt_number")
        objects = [o for o in bb.objects if o.identity.binding == m.binding]
        require(
            len(objects) == 1 and objects[0].identity.page == m.page,
            "measurement_object_binding",
        )
        obj = objects[0]
        if m.persisted_size is not None:
            require(
                obj.receipt is not None
                and (obj.receipt.size, obj.receipt.final_digest)
                == (m.persisted_size, m.persisted_digest),
                "measurement_receipt_missing",
            )
        available.append(m)
    return available


def _evaluate(pool, limits, incremental, stats):
    _limits(pool, limits)
    commitment = pool.commitment
    checks, position = [], 0
    try:
        index = _Index(pool, incremental, stats)
        events = _runtime(pool, stats)
        _validate_sources(pool, index, events)
        previous, prior = pool.initial, _chain_start()
        available, allowances = [], {}
        reservations = {r.operation_sha: r for r in pool.reservations}
        for op in pool.operations:
            # No measurement becomes available before its whole operation,
            # receipt, parts, clock pair and C3 audit have passed validation.
            intent, additions = _operation(pool, index, op, previous, events)
            if op.kind == RESERVATION_KIND:
                r = reservations[op.sha256]
                claimed = AdmissionCoreV2.from_bytes(r.core_json.encode())
                require(claimed.plan_sha == op.plan_sha, "reservation_plan_binding")
                expected, assessment, request, ap = _core(
                    pool,
                    index,
                    op.source,
                    claimed.page,
                    tuple(available),
                    allowances,
                    prior,
                )
                checks.append(ReservationValidationV1(op.operation_id, assessment))
                require(
                    not assessment.reasons,
                    assessment.reasons[0] if assessment.reasons else "admission",
                )
                require(
                    claimed == expected and intent["core_sha"] == expected.sha256,
                    "historical_core_mismatch",
                )
                require(
                    r.slot_id == derive_slot_id_v2(expected, op.operation_id),
                    "historical_slot_mismatch",
                )
                expected_roles = (
                    ("account-reservation", index.account_id, "slot_reserved"),
                    (
                        "plan-reservation",
                        index.plan_ids[op.plan_sha],
                        "attempt_reserved",
                    ),
                    ("body-reservation", index.body_ids[op.plan_sha], "reserved"),
                )
                require(len(additions) == len(op.parts) == 3, "reservation_three_parts")
                binding = None
                for part, (role, jid, kind) in zip(
                    op.parts, expected_roles, strict=True
                ):
                    require(
                        part.role == role and part.journal_id == jid,
                        "reservation_roles",
                    )
                    row = index.rows[jid][part.sequence]
                    event = row["event"]
                    require(event["kind"] == kind, "reservation_event_kind")
                    if kind == "reserved":
                        obj = c3.ObjectIdentity.from_dict(_json(event["payload_json"]))
                        require(
                            obj.binding == binding
                            and obj.page == claimed.page
                            and event["operation_id"] == op.operation_id,
                            "reservation_body_binding",
                        )
                    else:
                        b = c2.AttemptBinding.from_dict(event["binding"])
                        require(
                            b.slot_id == r.slot_id
                            and b.plan_sha == expected.plan_sha
                            and b.preflight_sha == expected.preflight_sha
                            and b.approval_sha == expected.approval_sha
                            and b.account_ref == pool.identity.account_ref
                            and b.generation == ap.generation + 1
                            and b.attempt_id
                            == hc.make_attempt_id(
                                decode_plan(
                                    index.plans[
                                        op.plan_sha
                                    ].contract.plan.canonical_plan.encode(),
                                    op.plan_sha,
                                ),
                                request,
                                expected.attempt_number,
                            ),
                            "reservation_attempt_binding",
                        )
                        require(
                            event["transition_id"] == op.operation_id,
                            "reservation_transition",
                        )
                        require(
                            binding is None or binding == b, "reservation_pair_binding"
                        )
                        binding = b
                allowances[binding] = expected.allowance
                position += 1
            available.extend(_available(pool, index, op, additions))
            previous, prior = op.result, _chain_next(prior, op)
        # No event/measurement/receipt may exist outside the operation coverage.
        require(
            {h.journal_id for h in previous.heads} == set(index.sources)
            and all(
                h.count == len(index.sources[h.journal_id].records)
                for h in previous.heads
            ),
            "uncommitted_journal_tail",
        )
        require(len(available) == len(pool.measurements), "measurement_not_committed")
        final_views = index.cut(previous)
        ap = _reconcile(index, final_views)
        budgets, capacity = [], []
        for sha in index.plans:
            pp = final_views[index.plan_ids[sha]]
            bp = final_views[index.body_ids[sha]]
            plan = decode_plan(
                index.plans[sha].contract.plan.canonical_plan.encode(), sha
            )
            if pp is not None:
                budgets.append(
                    v2.budget_result(
                        plan,
                        bp,
                        tuple(a for a in ap.attempts if a.binding.plan_sha == sha),
                        pp.attempts,
                        tuple(m for m in available if m.binding.plan_sha == sha),
                        allowances,
                    )
                )
                _registered_budget_reasons(budgets[-1], peer=False)
            # Unknown size has no invented zero-valued final capacity result.
            require(bp.retained_body_bytes is not None, "unknown_retained_size")
            capacity.append(
                v2.PlanCapacityV2(
                    sha,
                    bp.retained_body_bytes,
                    bp.reserved_remaining_bytes,
                    bp.cumulative_acquired_bytes,
                    bp.unresolved_acquisition_charge,
                    bp.sha256,
                )
            )
        result = SequentialHistoryValidationV1(
            True, None, None, commitment, tuple(checks), tuple(budgets), tuple(capacity)
        )
        return result, (index, previous, prior, tuple(available), allowances, events)
    except hc.HttpContractError as exc:
        return SequentialHistoryValidationV1(
            False, position, str(exc), commitment, tuple(checks), (), ()
        ), None


def verify_history_reference(pool, *, limits, statistics=None):
    """Independently replay each requested cut; small-fixture correctness oracle."""
    require(
        statistics is None or type(statistics) is ReplayStatistics, "statistics_type"
    )
    return _evaluate(pool, limits, False, statistics or ReplayStatistics())[0]


def verify_history_incremental(pool, *, limits, statistics=None):
    """Index immutable projections once per invocation, then reuse each prefix."""
    require(
        statistics is None or type(statistics) is ReplayStatistics, "statistics_type"
    )
    return _evaluate(pool, limits, True, statistics or ReplayStatistics())[0]


def build_admission_v2(pool, *, limits, cut, page):
    history, state = _evaluate(pool, limits, True, ReplayStatistics())
    require(
        history.historical_admissions_valid,
        "historical_admission_invalid:" + (history.reason or "unknown"),
    )
    index, previous, prior, available, allowances, events = state
    require(
        type(cut) is HistoricalCutV1 and type(page) is c3.LivePageIdentity,
        "admission_input_type",
    )
    require(page.plan_sha in index.plans, "target_plan_missing")
    require(
        index.plans[page.plan_sha].catalog_state != "stopped", "target_plan_stopped"
    )
    event = _cut_runtime(pool, cut, events)
    require(
        cut.heads == previous.heads
        and cut.runtime_sequence == len(events) - 1
        and cut.runtime_sequence >= previous.runtime_sequence
        and event.kind == "clock_observed",
        "current_cut",
    )
    require(
        not any(
            e.kind == "runtime_stop_entered" and e.sequence <= cut.runtime_sequence
            for e in events
        ),
        "runtime_stopped",
    )
    core, assessment, _, _ = _core(pool, index, cut, page, available, allowances, prior)
    require(not assessment.reasons, "admission_blocked:" + ",".join(assessment.reasons))
    return core


def verify_admission_v2(
    pool, *, limits, cut, page, claimed_core, claimed_slot, operation_id
):
    require(type(claimed_core) is AdmissionCoreV2, "core_v2_required")
    core = build_admission_v2(pool, limits=limits, cut=cut, page=page)
    require(
        claimed_core == core and claimed_slot == derive_slot_id_v2(core, operation_id),
        "admission_claim_mismatch",
    )
    return core
