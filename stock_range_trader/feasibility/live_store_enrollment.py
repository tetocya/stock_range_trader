"""I1b local typed operations and historical evidence. No sender or body writer."""

from dataclasses import dataclass, field
from datetime import datetime

from . import live_body_contract as c3
from . import live_store as st
from . import live_store_transactions as tx
from .http_contract import HttpContractError, check_approval_scope
from .live_enrollment_sources import (
    decode_approval,
    decode_plan,
    decode_preflight,
    decode_registry,
)
from .live_http_evidence import LiveJournal
from .live_plan_enrollment import (
    authority_from_preflight,
    derive_enrollment_intent,
    propose_plan_enrollment,
)

OPEN = "i1b-account-open-v1"
ENROLL = "i1b-plan-enroll-v1"
KINDS = frozenset({OPEN, ENROLL})
PRECONDITIONS = "store-transaction-preconditions-v2"
MANIFEST = "historical-feasibility-enrollment-initialization-v1"
REQUEST = "historical-feasibility-store-enrollment-request-v1"


class _BusinessRejection(HttpContractError):
    """Only fixed deterministic reasons are eligible for session continuation."""


def _reject(reason):
    if reason not in {
        "invalid_source",
        "approval_expired",
        "duplicate_account",
        "duplicate_plan",
        "account_not_open",
        "record_oversize",
    }:
        raise HttpContractError("i1b_rejection_taxonomy_invalid")
    raise _BusinessRejection("i1b_" + reason)


@dataclass(frozen=True)
class EnrollmentSource:
    kind: str
    content: bytes
    source_identity: str
    source_reference: str

    def __post_init__(self):
        tx._check(self.kind in {"plan", "approval"}, "source_kind_invalid")
        raw = st._load(self.content)
        tx._check(
            raw.get("schema") == st._DOCUMENT_SCHEMAS[self.kind],
            "source_schema_invalid",
        )
        st._label(self.source_identity)
        tx._check(
            type(self.source_reference) is str
            and 0 < len(self.source_reference) <= 1024,
            "source_reference_invalid",
        )

    @property
    def sha256(self):
        return st.digest_bytes(self.content)

    def to_dict(self):
        return {
            "kind": self.kind,
            "content": st._load(self.content),
            "source_identity": self.source_identity,
            "source_reference": self.source_reference,
        }


@dataclass(frozen=True)
class PlanEnrollmentRequest:
    plan: EnrollmentSource
    approval: EnrollmentSource
    body_contract: c3.LiveBodyStorageContract
    storage_policy: c3.StoragePolicy
    root_claim: c3.PhysicalRootIdentity
    schema: str = field(default=REQUEST, init=False)

    def __post_init__(self):
        tx._check(
            type(self.plan) is EnrollmentSource
            and self.plan.kind == "plan"
            and type(self.approval) is EnrollmentSource
            and self.approval.kind == "approval",
            "typed_sources_required",
        )
        tx._check(
            type(self.body_contract) is c3.LiveBodyStorageContract
            and type(self.storage_policy) is c3.StoragePolicy
            and type(self.root_claim) is c3.PhysicalRootIdentity,
            "root_policy_required",
        )
        contract = c3.LiveBodyStorageContract.from_bytes(self.body_contract.to_bytes())
        root = c3.PhysicalRootIdentity.from_bytes(self.root_claim.to_bytes())
        c3.LiveBodyJournal(contract, root)
        tx._check(
            contract.policy == self.storage_policy
            and contract.plan.plan_sha == self.plan.sha256
            and contract.plan.canonical_plan.encode() == self.plan.content,
            "body_source_binding",
        )

    def to_dict(self):
        return {
            "schema": REQUEST,
            "plan": self.plan.to_dict(),
            "approval": self.approval.to_dict(),
            "body_contract": self.body_contract.to_dict(),
            "storage_policy": self.storage_policy.to_dict(),
            "root_claim": self.root_claim.to_dict(),
        }

    @classmethod
    def from_bytes(cls, content):
        raw = st._load(content)
        tx._check(
            set(raw)
            == {
                "schema",
                "plan",
                "approval",
                "body_contract",
                "storage_policy",
                "root_claim",
            }
            and raw["schema"] == REQUEST,
            "request_schema_invalid",
        )
        sources = []
        for kind in ("plan", "approval"):
            source = raw[kind]
            tx._check(
                type(source) is dict
                and set(source)
                == {"kind", "content", "source_identity", "source_reference"},
                "source_fields_invalid",
            )
            sources.append(
                EnrollmentSource(
                    source["kind"],
                    st.canonical_bytes(source["content"]),
                    source["source_identity"],
                    source["source_reference"],
                )
            )
        result = cls(
            *sources,
            c3.LiveBodyStorageContract.from_dict(raw["body_contract"]),
            c3.StoragePolicy.from_dict(raw["storage_policy"]),
            c3.PhysicalRootIdentity.from_dict(raw["root_claim"]),
        )
        tx._check(
            st.canonical_bytes(result.to_dict()) == content, "request_noncanonical"
        )
        return result

    def intent(self, account_ref):
        return st.OperationIntent(
            ENROLL, account_ref, self.plan.sha256, st.canonical_bytes(self.to_dict())
        )


def open_enrollment_account(session, operation_id):
    tx._check(type(session) is tx.op.StoreSession, "session_required")
    a = session._resources.authority
    intent = st.OperationIntent(
        OPEN,
        session.identity.account_ref,
        None,
        st.canonical_bytes(
            {
                "schema": OPEN,
                "preflight_sha": a.preflight.sha256,
                "registry_sha": a.preflight.registry.sha256,
                "policy_sha": a.policy.sha256,
            }
        ),
    )
    return tx._execute(session, operation_id, intent)


def enroll_plan(session, operation_id, request):
    tx._check(
        type(session) is tx.op.StoreSession and type(request) is PlanEnrollmentRequest,
        "typed_enrollment_required",
    )
    return tx._execute(
        session, operation_id, request.intent(session.identity.account_ref)
    )


def _rules(kind):
    if kind == OPEN:
        return tx._Rules((("account-opening", "account"),))
    if kind == ENROLL:
        return tx._Rules(
            (
                ("account-enrollment", "account"),
                ("plan-opening", "plan"),
                ("body-initialization", "body"),
            )
        )
    raise HttpContractError("i1b_kind_unsupported")


def _sources(c):
    d = tx._rows(c, "deployments")[0]
    pre = tx._document(c, d["preflight_sha"])
    reg = tx._document(c, d["owner_registry_sha"])
    p = decode_preflight(pre.content, pre.sha256)
    r = decode_registry(reg.content, reg.sha256)
    tx._check(
        p.registry == r and p.identity.account_ref == d["account_ref"],
        "source_registry_binding",
    )
    return d, p, (pre, reg)


def _decode_request(intent):
    request = PlanEnrollmentRequest.from_bytes(intent.payload)
    plan = decode_plan(request.plan.content, request.plan.sha256)
    approval = decode_approval(request.approval.content, request.approval.sha256, plan)
    tx._check(
        plan.account_ref == intent.account_ref and plan.sha256 == intent.plan_sha,
        "intent_plan_binding",
    )
    return request, plan, approval


def _metadata(document):
    return {
        "document_sha": document.sha256,
        "kind": document.kind,
        "schema": document.schema,
        "source_identity": document.source_identity,
        "source_reference": document.source_reference,
        "registered_at": document.registered_at,
    }


def _manifest(
    c,
    identity,
    intent,
    operation_id,
    observed,
    runtime_head,
    docs,
    account,
    plan,
    body_id,
    request,
):
    d = tx._rows(c, "deployments")[0]
    projection = tx.rt._validate_runtime_contents(c)
    heads = {
        r["journal_id"]: {"sequence": r["last_sequence"], "head_sha": r["head_sha"]}
        for r in tx._rows(c, "journal_heads")
        if r["event_count"]
    }
    return {
        "schema": MANIFEST,
        "deployment_id": d["deployment_id"],
        "store_id": d["store_id"],
        "account_ref": d["account_ref"],
        "operation_id": operation_id,
        "intent_sha": intent.sha256,
        "preflight_sha": d["preflight_sha"],
        "registry_sha": d["owner_registry_sha"],
        "policy_sha": d["policy_sha"],
        "owner_pin_sha": projection.owner_pin_sha,
        "documents": sorted(
            (_metadata(doc) for doc in docs), key=lambda x: x["document_sha"]
        ),
        "source_heads": heads,
        "source_account_head": None,
        "prior_plan_heads": sorted(
            (j.plan_sha, j.value.head_sha)
            for j in tx._journal_values(c)
            if j.kind == "plan"
        ),
        "result_account_head": account.head_sha,
        "initial_plan_head": None if plan is None else plan.head_sha,
        "body_contract": None if request is None else request.body_contract.to_dict(),
        "root_claim": None if request is None else request.root_claim.to_dict(),
        "body_contract_sha": None if request is None else request.body_contract.sha256,
        "root_claim_sha": None if request is None else request.root_claim.sha256,
        "body_journal_id": body_id,
        "session_id": identity.session_id,
        "fence_epoch": identity.fence_epoch,
        "runtime_head": runtime_head,
        "started_at": observed,
    }


def _prepare(c, session, intent, operation_id, observed, runtime_head):
    d, preflight, originals = _sources(c)
    tx._check(
        preflight == session._resources.authority.preflight,
        "preflight_authority_mismatch",
    )
    at = datetime.fromisoformat(observed)
    if len(intent.canonical) > session._resources.authority.policy.max_record_bytes:
        _reject("record_oversize")
    if preflight.fixed_at > at:
        _reject("invalid_source")
    journals = tx._journal_values(c)
    account = next((j for j in journals if j.kind == "account"), None)
    if intent.kind == OPEN:
        expected = {
            "schema": OPEN,
            "preflight_sha": preflight.sha256,
            "registry_sha": preflight.registry.sha256,
            "policy_sha": d["policy_sha"],
        }
        tx._check(
            st._load(intent.payload) == expected and intent.plan_sha is None,
            "opening_intent_mismatch",
        )
        if tx._rows(c, "accounts"):
            _reject("duplicate_account")
        value = LiveJournal.create_account_v2(
            authority_from_preflight(
                preflight, canonical_preflight=originals[0].content.decode()
            ),
            recorded_at=at,
            transition_id=operation_id,
        )
        manifest = _manifest(
            c,
            session.identity,
            intent,
            operation_id,
            observed,
            runtime_head,
            originals,
            value,
            None,
            None,
            None,
        )
        return tx._Proposal(
            (),
            (tx._Journal("i1b-account", "account", None, value),),
            st.canonical_bytes(manifest),
        )
    if account is None:
        _reject("account_not_open")
    if any(
        r["plan_sha"] == intent.plan_sha and r["enrolled_revision"] is not None
        for r in tx._rows(c, "plans")
    ):
        _reject("duplicate_plan")
    try:
        request, plan, approval = _decode_request(intent)
        derivation = derive_enrollment_intent(
            plan,
            preflight,
            approval,
            canonical_plan=request.plan.content.decode(),
            canonical_preflight=originals[0].content.decode(),
            canonical_approval=request.approval.content.decode(),
            at=at,
        )
        tx._check(
            request.body_contract.preflight_sha == preflight.sha256,
            "body_preflight_mismatch",
        )
        tx._check(
            request.root_claim.owner_store_ref == session.identity.store_id
            and request.root_claim.observed_at <= at,
            "body_owner_claim_mismatch",
        )
        policy = session._resources.authority.policy
        tx._check(
            request.storage_policy.retained_body_budget
            <= policy.max_retained_body_bytes
            and request.storage_policy.cumulative_budget
            <= policy.max_cumulative_acquired_bytes,
            "storage_policy_exceeds_store",
        )
    except (HttpContractError, KeyError, TypeError, ValueError, OverflowError) as exc:
        _reject(
            "approval_expired"
            if str(exc) == "approval_outside_validity_window"
            else "invalid_source"
        )
    tx._check(
        derivation.authority == account.value.projection.authority,
        "account_authority_mismatch",
    )
    docs = tuple(
        tx._register_document(
            c,
            d["deployment_id"],
            tx._Document(s.kind, s.content, s.source_identity, s.source_reference),
            observed,
        )
        for s in (request.plan, request.approval)
    )
    tx._stage("documents_registered")
    # Re-read registered originals before proposal generation.
    plan = decode_plan(tx._document(c, plan.sha256).content, plan.sha256)
    approval = decode_approval(
        tx._document(c, approval.sha256).content, approval.sha256, plan
    )
    plans = tuple(
        j.value
        for j in sorted(journals, key=lambda j: j.plan_sha or "")
        if j.kind == "plan"
    )
    pair = propose_plan_enrollment(
        account.value,
        plans,
        expected_account_head=account.value.head_sha,
        operation_id=operation_id,
        at=at,
        plan=plan,
        preflight=preflight,
        approval=approval,
        canonical_plan=request.plan.content.decode(),
        canonical_preflight=originals[0].content.decode(),
        canonical_approval=request.approval.content.decode(),
    )
    tx._capacity(c, plan.sha256, pair.account.records[-1].encode())
    body_id = "i1b-body-" + plan.sha256
    manifest = _manifest(
        c,
        session.identity,
        intent,
        operation_id,
        observed,
        runtime_head,
        (*originals, *docs),
        pair.account,
        pair.plan,
        body_id,
        request,
    )
    manifest["source_account_head"] = account.value.head_sha
    body = c3.LiveBodyJournal(request.body_contract, request.root_claim).append(
        c3.BodyEvent(
            operation_id,
            "audit",
            at,
            None,
            st.canonical_bytes(
                {"evidence_ref": st.digest_bytes(st.canonical_bytes(manifest))}
            ).decode(),
            None,
        )
    )
    return tx._Proposal(
        (),
        (
            tx._Journal(account.journal_id, "account", None, pair.account),
            tx._Journal("i1b-plan-" + plan.sha256, "plan", plan.sha256, pair.plan),
            tx._Journal(body_id, "body", plan.sha256, body),
        ),
        st.canonical_bytes(manifest),
    )


def _end_check(intent, at):
    if intent.kind == ENROLL:
        _, plan, approval = _decode_request(intent)
        try:
            check_approval_scope(plan, approval, now=datetime.fromisoformat(at))
        except HttpContractError:
            _reject("approval_expired")


def _body_prefix(c, journal_id, sequence):
    meta = tx._rows(c, "journals", " WHERE journal_id=?", (journal_id,))
    tx._check(len(meta) == 1 and meta[0]["kind"] == "body", "body_journal_missing")
    initial = tx._rows(c, "events", " WHERE journal_id=? AND sequence=0", (journal_id,))
    tx._check(len(initial) == 1, "body_initial_missing")
    operation = tx._rows(
        c, "operations", " WHERE operation_id=?", (initial[0]["transition_id"],)
    )
    tx._check(
        len(operation) == 1 and operation[0]["kind"] == ENROLL,
        "body_initial_operation_invalid",
    )
    pre = st._load(operation[0]["execution_preconditions"])
    tx._check(pre["schema"] == PRECONDITIONS, "body_preconditions_invalid")
    manifest = pre["initialization_manifest"]
    contract = c3.LiveBodyStorageContract.from_dict(manifest["body_contract"])
    root = c3.PhysicalRootIdentity.from_dict(manifest["root_claim"])
    rows = tx._rows(
        c,
        "events",
        " WHERE journal_id=? AND sequence<=? ORDER BY sequence",
        (journal_id, sequence),
    )
    tx._check(len(rows) == sequence + 1, "body_prefix_incomplete")
    value = c3.LiveBodyJournal(
        contract, root, tuple(r["canonical"].decode() for r in rows)
    )
    for i, row in enumerate(rows):
        raw = st._load(row["canonical"])
        event = raw["event"]
        tx._check(
            row["sequence"] == i == raw["sequence"]
            and row["event_hash"] == raw["event_hash"]
            and row["previous_hash"] == raw["previous_hash"]
            and row["schema"] == raw["schema"] == meta[0]["schema"]
            and row["recorded_at"] == event["recorded_at"]
            and row["transition_id"] == event["operation_id"],
            "body_event_metadata_invalid",
        )
    return meta[0], rows, value


def _validate_manifest(c, row, pre):
    """Reconstruct initialization from original documents and historical prefixes.

    No current catalog/window is used to reinterpret an older receipt.
    """
    tx._check(
        pre.get("schema") == PRECONDITIONS, "initialization_preconditions_invalid"
    )
    m = pre["initialization_manifest"]
    d, preflight, originals = _sources(c)
    intent = st.OperationIntent.from_bytes(row["canonical_intent"], row["intent_sha"])
    at = datetime.fromisoformat(pre["started_at"])
    tx._check(preflight.fixed_at <= at, "preflight_from_future")
    runtime = tx.rt._validate_runtime_contents(c)
    source = pre["dependency_heads"]
    historical = [
        tx._evidence_prefix(c, jid, head["sequence"]) for jid, head in source.items()
    ]
    for (jid, head), (meta, _, value) in zip(source.items(), historical, strict=True):
        tx._check(
            value.head_sha == head["head_sha"]
            and meta["journal_id"] == jid
            and meta["account_ref"] == d["account_ref"],
            "manifest_source_invalid",
        )
    accounts = [value for meta, _, value in historical if meta["kind"] == "account"]
    plans = tuple(value for meta, _, value in historical if meta["kind"] == "plan")
    # Prior-plan completeness is checked by C2 against the historical account.
    authority = authority_from_preflight(
        preflight, canonical_preflight=originals[0].content.decode()
    )
    docs = originals
    request = None
    initial_plan = None
    body_id = None
    source_account_head = None
    if intent.kind == OPEN:
        tx._check(
            not source and not historical and intent.plan_sha is None,
            "opening_history_invalid",
        )
        tx._check(
            st._load(intent.payload)
            == {
                "schema": OPEN,
                "preflight_sha": d["preflight_sha"],
                "registry_sha": d["owner_registry_sha"],
                "policy_sha": d["policy_sha"],
            },
            "opening_intent_invalid",
        )
        account = LiveJournal.create_account_v2(
            authority, recorded_at=at, transition_id=row["operation_id"]
        )
    else:
        tx._check(
            intent.kind == ENROLL and len(accounts) == 1, "enrollment_history_invalid"
        )
        request, plan, approval = _decode_request(intent)
        registered = tuple(
            tx._document(c, s.sha256) for s in (request.plan, request.approval)
        )
        for supplied, original in zip(
            (request.plan, request.approval), registered, strict=True
        ):
            tx._check(
                (original.content, original.source_identity, original.source_reference)
                == (
                    supplied.content,
                    supplied.source_identity,
                    supplied.source_reference,
                ),
                "provenance_mismatch",
            )
            tx._check(
                original.registered_at <= row["created_at"], "document_from_future"
            )
        docs = (*originals, *registered)
        source_account_head = accounts[0].head_sha
        pair = propose_plan_enrollment(
            accounts[0],
            plans,
            expected_account_head=source_account_head,
            operation_id=row["operation_id"],
            at=at,
            plan=plan,
            preflight=preflight,
            approval=approval,
            canonical_plan=request.plan.content.decode(),
            canonical_preflight=originals[0].content.decode(),
            canonical_approval=request.approval.content.decode(),
        )
        tx._check(not pair.replayed, "enrollment_duplicate_history")
        account, initial_plan = pair.account, pair.plan
        body_id = "i1b-body-" + plan.sha256
        policy = st.StorePolicy.from_bytes(
            tx._rows(c, "policies", " WHERE policy_sha=?", (d["policy_sha"],))[0][
                "canonical"
            ]
        )
        tx._check(
            request.body_contract.preflight_sha == preflight.sha256
            and request.root_claim.owner_store_ref == d["store_id"]
            and request.root_claim.observed_at <= at
            and request.storage_policy.retained_body_budget
            <= policy.max_retained_body_bytes
            and request.storage_policy.cumulative_budget
            <= policy.max_cumulative_acquired_bytes,
            "body_claim_binding_invalid",
        )
    expected = {
        "schema": MANIFEST,
        "deployment_id": d["deployment_id"],
        "store_id": d["store_id"],
        "account_ref": d["account_ref"],
        "operation_id": row["operation_id"],
        "intent_sha": intent.sha256,
        "preflight_sha": d["preflight_sha"],
        "registry_sha": d["owner_registry_sha"],
        "policy_sha": d["policy_sha"],
        "owner_pin_sha": runtime.owner_pin_sha,
        "documents": sorted(
            (_metadata(doc) for doc in docs), key=lambda x: x["document_sha"]
        ),
        "source_heads": source,
        "source_account_head": source_account_head,
        "prior_plan_heads": sorted(
            (p.projection.scopes[0].plan_sha, p.head_sha) for p in plans
        ),
        "result_account_head": account.head_sha,
        "initial_plan_head": None if initial_plan is None else initial_plan.head_sha,
        "body_contract": None if request is None else request.body_contract.to_dict(),
        "root_claim": None if request is None else request.root_claim.to_dict(),
        "body_contract_sha": None if request is None else request.body_contract.sha256,
        "root_claim_sha": None if request is None else request.root_claim.sha256,
        "body_journal_id": body_id,
        "session_id": row["session_ref"],
        "fence_epoch": row["fence_ref"],
        "runtime_head": pre["runtime_head"],
        "started_at": row["created_at"],
    }
    tx._check(
        st.canonical_bytes(m) == st.canonical_bytes(expected),
        "initialization_manifest_mismatch",
    )
    tx._check(
        pre["documents"] == [x["document_sha"] for x in expected["documents"]],
        "manifest_documents_mismatch",
    )
    refs = tx._rows(
        c,
        "operation_head_references",
        " WHERE operation_id=? AND phase='result'",
        (row["operation_id"],),
    )
    expected_values = {"account": account}
    if initial_plan is not None:
        expected_values["plan"] = initial_plan
        body = c3.LiveBodyJournal(request.body_contract, request.root_claim).append(
            c3.BodyEvent(
                row["operation_id"],
                "audit",
                at,
                None,
                st.canonical_bytes(
                    {"evidence_ref": st.digest_bytes(st.canonical_bytes(m))}
                ).decode(),
                None,
            )
        )
        expected_values["body"] = body
    seen = set()
    for ref in refs:
        meta, _, value = tx._evidence_prefix(c, ref["journal_id"], ref["sequence"])
        tx._check(
            meta["kind"] not in seen and meta["kind"] in expected_values,
            "initial_journal_duplicate",
        )
        seen.add(meta["kind"])
        tx._check(
            value.records == expected_values[meta["kind"]].records
            and value.head_sha == ref["head_sha"],
            "initial_journal_mismatch",
        )
        if meta["kind"] == "body":
            tx._check(
                meta["journal_id"] == body_id and meta["plan_sha"] == intent.plan_sha,
                "body_catalog_mismatch",
            )
    tx._check(seen == set(expected_values), "initial_journal_missing")


def _validate_current(c, journals):
    operations = [r for r in tx._rows(c, "operations") if r["kind"] in KINDS]
    if not operations:
        return
    for row in operations:
        _validate_manifest(c, row, st._load(row["execution_preconditions"]))
    accounts = [j.value for j in journals if j.kind == "account"]
    plans = tuple(j.value for j in journals if j.kind == "plan")
    bodies = [j for j in journals if j.kind == "body"]
    body_catalog = tx._rows(c, "journals", " WHERE kind='body'")
    tx._check(
        len(body_catalog) == len(bodies)
        and {r["journal_id"] for r in body_catalog} == {j.journal_id for j in bodies},
        "body_catalog_incomplete",
    )
    tx._check(len(accounts) == 1, "enrollment_account_missing")
    expected = {p.projection.scopes[0].plan_sha for p in plans}
    tx._check(
        len(bodies) == len(expected) and {j.plan_sha for j in bodies} == expected,
        "body_catalog_incomplete",
    )
    enrolled = {r["plan_sha"] for r in operations if r["kind"] == ENROLL}
    tx._check(enrolled == expected, "enrollment_operation_missing")
    for journal in bodies:
        tx._check(
            journal.value.contract.plan.plan_sha == journal.plan_sha,
            "body_plan_mismatch",
        )
        result = c3.reconcile_live_bodies(
            journal.value,
            account=accounts[0],
            plans=plans,
            related_bodies=tuple(
                j.value for j in bodies if j.journal_id != journal.journal_id
            ),
        )
        tx._check(result.classification == "consistent", "body_reconciliation_failed")


def _initial_only(c, journals):
    _validate_current(c, journals)
    rows = tx._rows(c, "journals", " WHERE kind='body'")
    bodies = [j for j in journals if j.kind == "body"]
    tx._check(len(bodies) == len(rows) and bool(bodies), "quiescence_unproven")
    for journal in bodies:
        body = journal.value
        tx._check(
            len(body.records) == 1 and not body.projection.objects,
            "quiescence_unproven",
        )
        event = c3.BodyEvent.from_dict(st._load(body.records[0].encode())["event"])
        tx._check(
            event.kind == "audit"
            and event.object_id is None
            and event.c2_heads is None,
            "quiescence_unproven",
        )
