"""I2-0 read-only terminal assessment. No producer, sender or recovery authority.

C2/C3 replay proves logical evidence only. The private journal evaluator also
serves artificial tests; it is not a Store admission API. Physical dependencies
are requests for future I3 evidence, never caller-supplied attestations.
"""

from dataclasses import asdict, dataclass, field

from . import live_body_contract as c3
from . import live_http_evidence as c2
from . import live_store as st
from . import live_store_runtime as rt
from . import live_store_transactions as tx
from .http_contract import HttpContractError

PROFILE = "local-http-terminal-evidence-v1"
SNAPSHOT = "historical-feasibility-runtime-clean-validation-v4"
ASSESSMENT = "historical-feasibility-terminal-assessment-v1"


def _stage(name):
    """Private failure-injection seam, not a public callback."""


@dataclass(frozen=True)
class PhysicalDependency:
    """Identity of a required proof. No producer schema is accepted in I2-0."""

    kind: str
    subject: str
    evidence_sha: str

    def __post_init__(self):
        tx._check(
            self.kind in {"transport", "root_inventory", "writer_stability", "review"},
            "physical_dependency_kind_invalid",
        )
        st._label(self.subject)
        st._sha(self.evidence_sha)


@dataclass(frozen=True)
class TerminalAssessment:
    classification: str
    logical_status: str
    physical_status: str
    reasons: tuple[str, ...]
    evidence: bytes
    physical_dependencies: tuple[PhysicalDependency, ...]
    schema: str = field(default=ASSESSMENT, init=False)
    live_send_permitted: bool = field(default=False, init=False)
    reservation_permitted: bool = field(default=False, init=False)
    writer_permitted: bool = field(default=False, init=False)
    recovery_permitted: bool = field(default=False, init=False)
    stop_clear_permitted: bool = field(default=False, init=False)

    @property
    def clean_eligible(self):
        # Even this informational value is not a capability: close re-reads DB.
        return self.classification == "terminal_proven"

    @property
    def canonical(self):
        result = asdict(self)
        result["evidence"] = st._load(self.evidence)
        return st.canonical_bytes(result)


def _result(logical, reasons, evidence, dependencies=()):
    dependencies = tuple(
        sorted(set(dependencies), key=lambda x: (x.kind, x.subject, x.evidence_sha))
    )
    physical = "unsupported_physical_evidence" if dependencies else "not_required"
    classification = (
        physical if logical == "terminal_proven" and dependencies else logical
    )
    if dependencies:
        reasons = (*reasons, "trusted_physical_producer_not_implemented")
    return TerminalAssessment(
        classification,
        logical,
        physical,
        tuple(sorted(set(reasons))),
        st.canonical_bytes(evidence),
        dependencies,
    )


def _assess_journals(account, plans, bodies):
    try:
        return _evaluate_journals(account, plans, bodies)
    except HttpContractError as exc:
        return _result("inconsistent", (str(exc),), {"invalid_evidence": True})


def _evaluate_journals(account, plans, bodies):
    """Pure, private logical oracle. Replays every original; no clock inference.

    Released holds are validated at their recorded release, not re-evaluated
    against today's time. Account is the sole hold authority.
    """
    _stage("terminal_validation")
    # Constructors revalidate original canonical records (not cached projections).
    account = c2.LiveJournal(account.schema, account.records)
    plans = tuple(c2.LiveJournal(p.schema, p.records) for p in plans)
    bodies = tuple(c3.LiveBodyJournal(b.contract, b.root, b.records) for b in bodies)
    q = account.projection
    evidence = {
        "account_head": account.head_sha,
        "plan_heads": sorted(
            (p.projection.scopes[0].plan_sha, p.head_sha) for p in plans
        ),
        "body_heads": sorted((b.contract.plan.plan_sha, b.head_sha) for b in bodies),
        "inventory": sorted(
            (
                {
                    "plan_sha": b.contract.plan.plan_sha,
                    "contract_sha": b.contract.sha256,
                    "root_sha": b.root.sha256,
                    "projection": b.projection.to_dict(),
                }
                for b in bodies
            ),
            key=lambda x: x["plan_sha"],
        ),
        "holds": [
            {
                "hold": h.hold.to_dict(),
                "version_sha": h.hold.hold_version_sha256,
                "released_at": None
                if h.released_at is None
                else c2._stamp(h.released_at),
            }
            for h in sorted(q.holds, key=lambda x: x.hold.hold_id)
        ],
        # Preserve the release references and the actual historical version.
        "hold_events": [
            st._load(r.encode())
            for r in account.records
            if st._load(r.encode())["event"]["kind"].startswith("hold_")
        ],
        "attempts": [],
    }
    bad, incomplete, recovery = [], [], []
    dependencies = []
    pair = c2.reconcile_live_journals(account, plans)
    if pair.classification != "consistent":
        (bad if pair.classification == "inconsistent" else incomplete).extend(
            pair.reasons
        )
    _stage("hold_validation")
    for h in q.holds:
        if h.released_at is None:
            (recovery if h.hold.release_mode == "manual" else incomplete).append(
                "hold_not_released"
            )
        if h.hold.release_mode == "manual":
            dependencies.append(
                PhysicalDependency("review", h.hold.hold_id, h.hold.hold_version_sha256)
            )
    for a in q.attempts:
        if a.sent_at is not None and not any(
            obj.identity.binding == a.binding
            for body in bodies
            for obj in body.projection.objects
        ):
            incomplete.append("sent_attempt_body_evidence_missing")
        required = c2.required_attempt_hold(q.scope(a.binding.plan_sha), a)
        held = next(
            (h for h in q.holds if required and h.hold.hold_id == required.hold_id),
            None,
        )
        if required and (held is None or not held.hold.dominates(required)):
            bad.append("required_hold_coverage_missing")
        if a.state in {"reserved", "sent", "headers_observed"}:
            incomplete.append("attempt_" + a.state)
        elif a.outcome in {"unknown", "pre_send_reclaim"}:
            if held is None or held.released_at is None:
                recovery.append("unknown_or_reclaim_review_required")
        elif a.outcome != "response":
            bad.append("unsupported_terminal_outcome")
        raw = {
            "binding": asdict(a.binding),
            "state": a.state,
            "outcome": a.outcome,
            "settlement_transition": a.settlement_transition,
            "required_hold": None if required is None else required.to_dict(),
            "header": None if a.header is None else a.header.to_dict(),
        }
        evidence["attempts"].append(raw)
        # Includes never-sent/reclaimed reservations: absence of a send event
        # is not physical proof that an old worker/socket has terminated.
        dependencies.append(
            PhysicalDependency(
                "transport", a.binding.slot_id, st.digest_bytes(st.canonical_bytes(raw))
            )
        )
    evidence["attempts"].sort(
        key=lambda x: (x["binding"]["plan_sha"], x["binding"]["attempt_id"])
    )
    _stage("inventory_validation")
    expected = {s.plan_sha for s in q.scopes}
    supplied = [b.contract.plan.plan_sha for b in bodies]
    if len(supplied) != len(set(supplied)) or set(supplied) - expected:
        bad.append("body_inventory_unrelated_or_duplicate")
    if expected - set(supplied):
        incomplete.append("body_inventory_missing")
    for b in bodies:
        result = c3.reconcile_live_bodies(
            b,
            account=account,
            plans=plans,
            related_bodies=tuple(other for other in bodies if other is not b),
        )
        if result.classification != "consistent":
            (bad if result.classification == "inconsistent" else incomplete).extend(
                result.reasons
            )
        projection = b.projection
        initial_only = (
            not projection.objects
            and len(b.records) <= 1
            and all(st._load(r.encode())["event"]["kind"] == "audit" for r in b.records)
        )
        if q.attempts or not initial_only:
            dependencies.append(
                PhysicalDependency(
                    "root_inventory",
                    b.contract.plan.plan_sha,
                    st.digest_bytes(
                        st.canonical_bytes(
                            {
                                "head": b.head_sha,
                                "contract": b.contract.sha256,
                                "root": b.root.sha256,
                                "projection": projection.to_dict(),
                            }
                        )
                    ),
                )
            )
        for obj in projection.objects:
            if obj.writer.state in {"not_started", "open"}:
                incomplete.append("writer_" + obj.writer.state)
            if obj.writer.state == "unknown":
                recovery.append("writer_unknown")
            if not obj.stable:
                incomplete.append("writer_stability_unproven")
            if obj.state not in {"committed", "quarantine"}:
                (recovery if obj.state in {"partial", "orphan"} else incomplete).append(
                    "body_" + obj.state
                )
            # Conservative unknown acquisition charges are retained in the
            # inventory, even for stable quarantined objects; never refunded.
            dependencies.append(
                PhysicalDependency(
                    "writer_stability", obj.identity.object_id, obj.sha256
                )
            )
    status = (
        "inconsistent"
        if bad
        else "recovery_required"
        if recovery
        else "incomplete"
        if incomplete
        else "terminal_proven"
    )
    return _result(status, (*bad, *recovery, *incomplete), evidence, dependencies)


def _requires_terminal_profile(c):
    """Select without changing the old v1/v2/v3 validators or their bytes."""
    for j in tx._journal_values(c):
        if j.kind != "body" and (
            j.value.projection.attempts or j.value.projection.holds
        ):
            return True
        if j.kind == "body" and (
            len(j.value.records) != 1 or j.value.projection.objects
        ):
            return True
    return False


def _assess_db(c, identity, p):
    """Already guarded connection only. No writes and no pending admission."""
    rt.validate_store_v2_contents(c)
    if p.persistent_stop:
        return _result("stop_blocked", ("persistent_stop",), {})
    tx._check(
        (p.current_session_id, p.fence_epoch)
        == (identity.session_id, identity.fence_epoch)
        and p.session_state == "running",
        "session_fence_mismatch",
    )
    if tx._rows(c, "control_state"):
        return _result("recovery_required", ("control_recovery_unsupported",), {})
    operations = tx._rows(c, "operations")
    if any(r["status"] != "committed" for r in operations):
        return _result("incomplete", ("pending_operation",), {})
    for row in operations:
        tx._receipt(c, row, p)
    journals = tx._journal_values(c)
    accounts = [j.value for j in journals if j.kind == "account"]
    plans = tuple(j.value for j in journals if j.kind == "plan")
    bodies = tuple(j.value for j in journals if j.kind == "body")
    if (
        not journals
        and not operations
        and not tx._rows(c, "accounts")
        and not tx._rows(c, "plans")
    ):
        return _result("terminal_proven", (), {"empty": True})
    tx._check(len(accounts) == 1, "terminal_account_incomplete")
    result = _assess_journals(accounts[0], plans, bodies)
    # Future HTTP evidence needs committed, historically valid operation
    # receipts too. Logical journal consistency alone is not Store authority.
    events = {
        (r["journal_id"], r["sequence"], r["event_hash"]) for r in tx._rows(c, "events")
    }
    parts = {
        (r["journal_id"], r["sequence"], r["event_hash"])
        for r in tx._rows(c, "event_parts")
    }
    if events != parts:
        return _result(
            "inconsistent" if result.logical_status == "inconsistent" else "incomplete",
            (*result.reasons, "event_operation_receipt_coverage_missing"),
            st._load(result.evidence),
            result.physical_dependencies,
        )
    # Keep full I1b/current reconciliation as the authority. The private oracle
    # can classify pending fixtures, but never admits pending business writes.
    if result.logical_status == "terminal_proven":
        tx._reconcile(c)
    return result


def assess_terminal_state(session):
    """Guarded read only; broken guards/global evidence raise, never relax them.

    No physical attestation, connection, caller head, clock or SQL is accepted.
    This function does not advance runtime or grant close/send authority.
    """
    tx._check(type(session) is tx.op.StoreSession, "session_required")
    return tx._read_session(
        session,
        lambda c: _assess_db(c, session.identity, rt._validate_runtime_contents(c)),
    )


def _snapshot(c, identity, p, assessment):
    _stage("snapshot_build")
    operations = tx._rows(c, "operations")
    receipts = tx._rows(c, "receipts")
    return {
        "schema": SNAPSHOT,
        "quiescence_profile": PROFILE,
        "store_schema": rt.STORE_SCHEMA_V2,
        **asdict(identity),
        "runtime_head": p.head_sha,
        "owner_pin_sha": p.owner_pin_sha,
        "physical_sha": p.physical_sha,
        "catalog": st._load(tx._catalog(c).canonical),
        "journal_heads": sorted(
            tx._rows(c, "journal_heads"), key=lambda r: r["journal_id"]
        ),
        "operation_receipt_set_sha": st.digest_bytes(
            st.canonical_bytes(
                {
                    "operations": sorted(
                        (r["operation_id"], r["intent_sha"], r["receipt_sha"])
                        for r in operations
                    ),
                    "receipts": sorted(
                        (r["operation_id"], r["receipt_sha"]) for r in receipts
                    ),
                }
            )
        ),
        "terminal_assessment": st._load(assessment.canonical),
        "c2_reconciled_heads": st._load(assessment.evidence).get("plan_heads", []),
        "c3_inventory_sha": st.digest_bytes(
            st.canonical_bytes(st._load(assessment.evidence).get("inventory", []))
        ),
        "physical_dependencies": [asdict(d) for d in assessment.physical_dependencies],
    }


def _clean_snapshot(c, identity, p):
    result = _assess_db(c, identity, p)
    if not result.clean_eligible:
        raise HttpContractError(
            "store_transaction_quiescence_unproven:"
            + result.classification
            + ":"
            + ",".join(result.reasons)
        )
    # No production physical-evidence producer is registered or injectable.
    # Only evidence needing no such producer can reach this line in I2-0.
    return _snapshot(c, identity, p, result)


def _select_clean_snapshot(c, identity, p):
    if _requires_terminal_profile(c):
        return _clean_snapshot(c, identity, p)
    return tx._clean_snapshot(c, identity, p)
