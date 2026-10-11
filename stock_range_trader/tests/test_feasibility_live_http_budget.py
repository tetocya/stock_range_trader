"""Budget contract tests: artificial immutable originals; no Store or transport."""

import ast
import inspect
from dataclasses import fields, replace
from datetime import timedelta

import pytest
from test_feasibility_http_contract import approval, exchange, journal_for, reserve
from test_feasibility_live_body_contract import DIGEST, NOW, Scenario, stamp
from test_feasibility_live_store_i2_0 import release

from feasibility import http_contract as hc
from feasibility import live_body_contract as c3
from feasibility import live_http_budget as b
from feasibility import live_http_evidence as c2
from feasibility.live_enrollment_sources import decode_plan

IDENTITY = b.Identity("deployment", "store", "c3-artificial-account")


class Fixture(Scenario):
    def __init__(self):
        super().__init__(c3.StoragePolicy(1000, 10000, 10000, 10000, 10000))
        raw = self.c.plan.content
        raw["limits"].update(
            max_transfer_bytes=1000, max_decoded_bytes=1000, max_saved_bytes=1000
        )
        snapshot = c3.FixedPlanSnapshot(c3._canonical(raw), c3._hash(raw))
        self.fixed = decode_plan(snapshot.canonical_plan.encode(), snapshot.plan_sha)
        self.claim = approval(
            self.fixed, valid_from=NOW, valid_until=NOW + timedelta(hours=1)
        )
        self.c = replace(self.c, plan=snapshot)
        self.scope = replace(
            self.scope, plan_sha=snapshot.plan_sha, approval_sha=self.claim.sha256
        )
        self.p = c2.LiveJournal.create(
            c2.LIVE_PLAN_SCHEMA, (self.scope,), recorded_at=NOW
        )
        self.a = c2.LiveJournal.create(
            c2.LIVE_ACCOUNT_SCHEMA, (self.scope,), recorded_at=NOW
        )
        self.root = replace(self.root, root_binding_sha=self.c.root_binding_sha)
        self.j = c3.LiveBodyJournal(self.c, self.root)
        self.measurements = []

    def begin(self, page_index=0, cursor=None):
        for hs in self.a.projection.holds:
            if hs.released_at is None:
                self.n = max(self.n, int((hs.hold.not_before - NOW).total_seconds()))
                self.a = self.a.append(
                    "hold_released",
                    recorded_at=self.tick(),
                    transition_id="release-" + str(self.n),
                    hold_id=hs.hold.hold_id,
                    expected_hold_sha256=hs.hold.hold_version_sha256,
                    clock_reference="clock",
                    account_state_reference="state",
                    manual_review_reference="review"
                    if hs.hold.release_mode == "manual"
                    else None,
                    repair_reference=None,
                )
        self.index += 1
        page = hc.PageRequest(self.fixed.queries[0], page_index, cursor)
        number = sum(m.page.page_index == page_index for m in self.measurements) + 1
        self.b = c2.AttemptBinding(
            self.c.plan.plan_sha,
            self.c.preflight_sha,
            self.scope.account_ref,
            self.scope.approval_sha,
            hc.make_attempt_id(self.fixed, page, number),
            f"slot-{self.index}",
            "holder",
            self.a.projection.generation + 1,
        )
        at = self.tick()
        self.pair("attempt_reserved", "slot_reserved", at, reserved_at=stamp(at))
        self.identity = c3.ObjectIdentity(
            self.b,
            c3.LivePageIdentity(
                self.c.plan.plan_sha,
                c3._canonical(self.fixed.queries[0].to_dict()),
                page_index,
                cursor,
            ),
        )
        self.oid = self.identity.object_id
        self.j = self.j.reserve(
            self.identity,
            at=at,
            operation_id=f"reserve-{self.index}",
            account=self.a,
            plans=(self.p,),
        )
        return self

    def measurement(
        self, completeness="complete", transfer=400, decoded=60, sink=60, saved=False
    ):
        m = b.MeasurementOriginal(
            IDENTITY,
            self.b,
            self.identity.page,
            sum(x.page == self.identity.page for x in self.measurements) + 1,
            completeness,
            transfer,
            decoded,
            sink,
            self.j.object(self.oid).receipt.size if saved else None,
            self.j.object(self.oid).receipt.final_digest if saved else None,
            self.tick(),
            "artificial-capture",
            "artificial-clock",
            self.a.head_sha,
            self.p.head_sha,
            self.j.head_sha,
        )
        self.measurements.append(m)
        return m

    def sources(self, **changes):
        result = dict(
            account=self.a,
            plans=(self.p,),
            bodies=(self.j,),
            originals=tuple(self.measurements),
            at=NOW + timedelta(seconds=self.n + 1),
        )
        result.update(changes)
        return result

    def reduce(self, **changes):
        return b.reduce_history(self.c.plan, **self.sources(**changes))


def complete(transfer=400):
    f = Fixture().begin().complete(next_cursor="page-2")
    f.measurement(transfer=transfer, saved=True)
    return f


@pytest.mark.parametrize("transfer", [0, 400, 900, 1000, 1001])
def test_known_arithmetic_matches_legacy_evidence_journal(transfer):
    f = Fixture().begin().opened().received(status=503)
    f.measurement(transfer=transfer, decoded=0, sink=0)
    actual = f.reduce()
    old = exchange(
        journal_for(f.fixed, f.claim),
        hc.PageRequest(f.fixed.queries[0], 0),
        1,
        NOW + timedelta(seconds=1),
        status=503,
        transfer=transfer,
        decoded=0,
    )
    assert actual.result.known_transfer == old._state.transfer_bytes
    assert actual.result.allowances.transfer == max(
        0, old.next_reservation_allowances[0]
    )
    assert actual.evidence[0].classification == (
        "known_zero" if transfer == 0 else "known_complete"
    )
    assert actual.result.overrun == (transfer > 1000)
    if transfer == 1000:
        assert "byte_budget_exhausted" in actual.result.blocking_reasons


@pytest.mark.parametrize("status", [429, 500, 503])
def test_non200_measurements_are_charged(status):
    f = Fixture().begin().opened().received(status=status)
    f.measurement(transfer=400, decoded=60, sink=0)
    r = f.reduce()
    assert (
        r.result.known_transfer,
        r.result.known_decoded,
        r.result.unknown_count,
    ) == (400, 60, 0)
    assert not r.result.saved.compatible


@pytest.mark.parametrize("partial", [None, 0, 50, 1000, 1001])
def test_unknown_full_page_no_double_charge_and_overshoot(partial):
    f = Fixture().begin().opened().received(outcome="unknown")
    f.measurement(
        "unobserved" if partial is None else "partial", partial, partial, None
    )
    r = f.reduce()
    old = journal_for(f.fixed, f.claim)
    ev = reserve(
        old, hc.PageRequest(f.fixed.queries[0], 0), 1, NOW + timedelta(seconds=1)
    )
    old = old.append(ev).append(
        hc.JournalEvent(
            "outcome_unknown", NOW + timedelta(seconds=2), attempt_id=ev.attempt_id
        )
    )
    assert r.result.unknown_count == old._state.unknown == 1
    assert r.result.unknown_transfer_charge == r.result.unknown_decoded_charge == 1000
    assert (
        r.result.known_transfer
        == r.result.known_decoded
        == r.result.saved.saved_total
        == 0
    )
    assert (
        r.result.allowances.transfer,
        r.result.allowances.decoded,
        r.result.allowances.saved,
    ) == old.next_reservation_allowances
    assert r.result.overrun == (partial == 1001)
    assert r.evidence[0].classification == "valid_unknown"


def test_missing_response_measurement_is_not_unknown_or_zero():
    f = Fixture().begin().opened().received()
    f.measurement("unobserved", None, None, None)
    r = f.reduce()
    assert r.evidence[0].classification == "missing"
    assert r.result.unknown_count == 0
    assert "missing" in r.result.blocking_reasons
    assert "response_200_body_uncommitted" in r.result.blocking_reasons


def test_complete_unknown_claim_conflicts():
    f = Fixture().begin().opened().received(outcome="unknown")
    f.measurement()
    assert f.reduce().evidence[0].classification == "conflicting"


def test_open_attempt_and_headers_never_complete():
    f = Fixture().begin()
    f.measurement("unobserved", None, None, None)
    r = f.reduce()
    assert r.evidence[0].classification == "open_unsettled"
    assert "open_unsettled" in r.result.blocking_reasons
    assert r.result.saved.compatible  # Zero, untouched reservation is compatible.


def test_saved_commit_positive_and_roundtrip():
    f = complete()
    r = f.reduce()
    assert r.result.saved.compatible
    assert r.result.saved.saved_total == 60
    assert r.result.allowances == b.Allowances(600, 940, 940)
    for value in (r.result, *r.evidence, r.result.source_commitment):
        assert type(value).from_bytes(value.to_bytes()) == value
    assert (
        b.verify_history(
            f.c.plan,
            claimed_result=r.result,
            claimed_evidence=r.evidence,
            **f.sources(),
        )
        == r
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("persisted_size", 59),
        ("persisted_digest", "f" * 64),
        ("sink_accepted_bytes", 59),
    ],
)
def test_saved_mismatch(field, value):
    f = complete()
    f.measurements[0] = replace(f.measurements[0], **{field: value})
    assert "saved_measurement_mismatch" in f.reduce().result.blocking_reasons


def test_measurement_omission_duplicate_and_order_rejected():
    f = complete()
    for originals in ((), tuple(f.measurements * 2)):
        with pytest.raises(hc.HttpContractError, match="coverage_or_order"):
            f.reduce(originals=originals)


@pytest.mark.parametrize(
    "field,value",
    [
        ("transfer_bytes", True),
        ("decoded_bytes", -1),
        ("sink_accepted_bytes", 2**63),
        ("attempt_number", False),
        ("observed_at", NOW.replace(tzinfo=None)),
    ],
)
def test_bad_typed_measurement(field, value):
    f = complete()
    with pytest.raises(hc.HttpContractError):
        replace(f.measurements[0], **{field: value})


@pytest.mark.parametrize("value", [True, -1, 2**63, 1.0, "1"])
def test_integer_and_unknown_multiplication(value):
    f = Fixture()
    with pytest.raises(hc.HttpContractError):
        b.calculate_allowances(f.fixed.limits, 0, 0, value, 0)


def test_eight_times_two_to_62_overflow():
    lim = replace(
        Fixture().fixed.limits, max_transfer_bytes=2**62, max_page_transfer_bytes=2**62
    )
    with pytest.raises(hc.HttpContractError, match="integer_range"):
        b.calculate_allowances(lim, 0, 0, 8, 0)


def test_canonical_unknown_duplicate_fields_and_permission_rejected():
    m = complete().measurements[0]
    raw = m.to_dict()
    raw["extra"] = 1
    with pytest.raises(hc.HttpContractError):
        b.MeasurementOriginal.from_dict(raw)
    data = m.to_bytes()
    with pytest.raises(hc.HttpContractError):
        b.MeasurementOriginal.from_bytes(b'{"schema":"duplicate",' + data[1:])
    e = complete().reduce().evidence[0]
    raw = e.to_dict()
    raw["live_permission"] = True
    with pytest.raises(hc.HttpContractError):
        b.BudgetEvidence.from_dict(raw)


def test_historical_verifier_detects_forged_arithmetic_and_commitment():
    f = complete()
    r = f.reduce()
    for forged in (
        replace(r.result, known_transfer=399),
        replace(r.result, source_commitment=b.SourceCommitment(1, "a" * 64)),
    ):
        with pytest.raises(hc.HttpContractError, match="historical_claim_mismatch"):
            b.verify_history(
                f.c.plan,
                claimed_result=forged,
                claimed_evidence=r.evidence,
                **f.sources(),
            )
    with pytest.raises(hc.HttpContractError, match="historical_claim_mismatch"):
        b.verify_history(
            f.c.plan,
            claimed_result=r.result,
            claimed_evidence=(replace(r.evidence[0], charged_transfer=1),),
            **f.sources(),
        )


def test_future_prefix_and_future_measurement_rejected():
    f = complete()
    with pytest.raises(hc.HttpContractError, match="future_source"):
        f.reduce(at=NOW)
    with pytest.raises(hc.HttpContractError, match="measurement_time"):
        f.reduce(
            originals=(replace(f.measurements[0], observed_at=NOW + timedelta(days=1)),)
        )


def test_wrong_slot_and_body_inventory_omission():
    f = complete()
    with pytest.raises(hc.HttpContractError, match="coverage_or_order"):
        f.reduce(
            originals=(
                replace(f.measurements[0], binding=replace(f.b, slot_id="wrong")),
            )
        )
    with pytest.raises(hc.HttpContractError, match="body_source_coverage"):
        f.reduce(bodies=())


def test_expiry_elapsed_and_original_result_stable():
    f = complete()
    r = f.reduce()
    assert (
        "elapsed_budget_exhausted"
        in f.reduce(at=NOW + timedelta(seconds=1201)).result.blocking_reasons
    )
    assert (
        "scope_expired_or_not_started"
        in f.reduce(at=NOW + timedelta(hours=1)).result.blocking_reasons
    )
    assert f.reduce() == r


def test_admission_structured_commitment_and_no_hash_cycle():
    f = Fixture()
    page = c3.LivePageIdentity(
        f.fixed.sha256, c3._canonical(f.fixed.queries[0].to_dict()), 0, None
    )
    args = dict(
        identity=IDENTITY,
        page=page,
        start_observation_reference="artificial-start",
        **f.sources(),
    )
    core = b.build_admission(f.c.plan, **args)
    assert b.AdmissionCore.from_bytes(core.to_bytes()) == core
    assert not {"slot_id", "object_id", "receipt", "manifest_digest"} & {
        x.name for x in fields(core)
    }
    slot = b.derive_slot_id(core, "operation")
    assert len(slot) == 64 and c2._label(slot) == slot
    assert slot != b.derive_slot_id(core, "other")
    assert (
        b.verify_admission(
            f.c.plan,
            claimed_core=core,
            claimed_slot=slot,
            operation_id="operation",
            **args,
        )
        == core
    )
    with pytest.raises(hc.HttpContractError, match="admission_mismatch"):
        b.verify_admission(
            f.c.plan,
            claimed_core=replace(core, allowances=b.Allowances(1, 1, 1)),
            claimed_slot=slot,
            operation_id="operation",
            **args,
        )
    with pytest.raises(hc.HttpContractError, match="slot_mismatch"):
        b.verify_admission(
            f.c.plan,
            claimed_core=core,
            claimed_slot="a" * 64,
            operation_id="operation",
            **args,
        )
    assert not core.live_permission


def test_source_count_bound():
    with pytest.raises(hc.HttpContractError, match="source_limit"):
        b._commit("test", [None] * (b.MAX_SOURCES + 1))


def test_no_io_or_store_mutation_imports():
    tree = ast.parse(inspect.getsource(b))
    imports = {x.module for x in ast.walk(tree) if isinstance(x, ast.ImportFrom)}
    assert not imports & {
        "sqlite3",
        "socket",
        "os",
        "requests",
        "live_store",
        "live_store_transactions",
        "live_store_operational",
    }
    calls = [x for x in ast.walk(tree) if isinstance(x, ast.Call)]
    assert not any(
        isinstance(x.func, ast.Attribute)
        and x.func.attr in {"append", "reserve", "commit"}
        and isinstance(x.func.value, ast.Name)
        and x.func.value.id in {"account", "body", "target"}
        for x in calls
    )
    assert not any(
        isinstance(x.func, ast.Name) and x.func.id in {"open", "getenv"} for x in calls
    )


def test_duplicate_digest_blocks_saved_compatibility_without_changing_c3():
    f = complete(100)
    f.begin(1, "page-2").complete()
    f.measurement(transfer=100, saved=True)
    before = f.j.to_bytes()
    r = f.reduce()
    assert "saved_digest_or_attempt_duplicate" in r.result.blocking_reasons
    assert r.result.saved.saved_total == 60  # HTTP distinct-digest total.
    assert f.j.projection.retained_body_bytes == 120  # C3 physical objects.
    assert f.j.to_bytes() == before
    with pytest.raises(hc.HttpContractError, match="coverage_or_order"):
        f.reduce(originals=tuple(reversed(f.measurements)))


def test_next_page_positive_and_legacy_saved_total():
    f = complete(100)
    old = exchange(
        journal_for(f.fixed, f.claim),
        hc.PageRequest(f.fixed.queries[0], 0),
        1,
        NOW + timedelta(seconds=1),
        transfer=100,
        decoded=60,
    )
    from test_feasibility_http_contract import finish_page

    old = finish_page(
        old, NOW + timedelta(seconds=2), digest=DIGEST, saved=60, next_key="page-2"
    )
    r = f.reduce()
    assert r.result.saved.saved_total == old._state.saved_total == 60
    assert tuple(vars(r.result.allowances).values()) == old.next_reservation_allowances
    release(f)
    page = replace(f.identity.page, page_index=1, cursor="page-2")
    core = b.build_admission(
        f.c.plan,
        identity=IDENTITY,
        page=page,
        start_observation_reference="start",
        **f.sources(),
    )
    assert core.attempt_number == 1
    assert core.capacity.retained == core.capacity.acquired == 60
    assert core.capacity.unresolved == 0
    with pytest.raises(hc.HttpContractError, match="cursor_progression"):
        b.build_admission(
            f.c.plan,
            identity=IDENTITY,
            page=replace(page, cursor="wrong"),
            start_observation_reference="start",
            **f.sources(),
        )


def test_pre_send_reclaim_charges_unknown_without_plan_unknown_event():
    f = Fixture().begin()
    at = NOW + timedelta(seconds=61)
    f.n = 61
    f.a = f.a.append(
        "slot_reclaimed",
        recorded_at=at,
        transition_id="reclaimed",
        binding=f.b,
        evidence={
            "lease_expired_at": stamp(at),
            "observed_at": stamp(at),
            "clock_status": "verified",
            "previous_transport_termination_reference": "artificial-termination",
            "operator_review_reference": "artificial-review",
            "process_restart_detected": True,
        },
    )
    f.sync_hold()
    f.measurement("unobserved", None, None, None)
    r = f.reduce()
    assert r.result.unknown_count == 1
    assert r.result.unknown_transfer_charge == r.result.unknown_decoded_charge == 1000
    assert f.p.projection.attempts[0].outcome is None
    assert f.j.projection.unresolved_acquisition_charge == 1000
    release(f)
    assert f.reduce().result.unknown_transfer_charge == 1000
    assert f.j.projection.unresolved_acquisition_charge == 1000


@pytest.mark.parametrize(
    "state,size", [("partial", 20), ("orphan", 20), ("staging", None)]
)
def test_bad_saved_states_and_unknown_size(state, size):
    f = (
        Fixture()
        .begin()
        .opened()
        .observed(size, state=state)
        .received(outcome="unknown")
    )
    f.measurement("partial", size, size, size)
    r = f.reduce()
    assert not r.result.saved.compatible
    assert "saved_inventory_incompatible" in r.result.blocking_reasons


def test_unknown_quarantine_no_refund():
    f = (
        Fixture()
        .begin()
        .opened()
        .observed(20, state="partial")
        .received(outcome="unknown")
        .closed()
        .stabilized()
    )
    q = f.quarantine_evidence()
    f.j = f.j.quarantine_object(q)
    f.measurement("partial", 20, 20, 20)
    release(f)
    r = f.reduce()
    assert r.result.unknown_transfer_charge == 1000
    assert r.result.saved.saved_total == 0 and not r.result.saved.compatible
    assert f.j.projection.retained_body_bytes == 20
    assert f.j.projection.unresolved_acquisition_charge == 1000


def test_retries_count_and_limit():
    f = Fixture()
    for _ in range(4):
        f.begin().opened().observed(0, state="partial").received(
            status=503
        ).closed().stabilized().acquired(0)
        f.j = f.j.quarantine_object(f.quarantine_evidence())
        f.measurement(transfer=10, decoded=0, sink=0)
    r = f.reduce()
    assert r.result.attempt_count == 4
    assert r.result.page_counts[0].count == 4
    assert r.result.known_transfer == 40
    assert "retry_budget_exceeded" in r.result.blocking_reasons


def test_old_core_invalid_when_sources_advance_and_old_prefix_still_valid():
    f = Fixture()
    page = c3.LivePageIdentity(
        f.fixed.sha256, c3._canonical(f.fixed.queries[0].to_dict()), 0, None
    )
    before = f.sources()
    args = dict(identity=IDENTITY, page=page, start_observation_reference="start")
    core = b.build_admission(f.c.plan, **args, **before)
    f.begin()
    f.measurement("unobserved", None, None, None)
    with pytest.raises(hc.HttpContractError, match="admission_blocked"):
        b.verify_admission(
            f.c.plan,
            claimed_core=core,
            claimed_slot=b.derive_slot_id(core, "op"),
            operation_id="op",
            **args,
            **f.sources(),
        )
    assert (
        b.verify_admission(
            f.c.plan,
            claimed_core=core,
            claimed_slot=b.derive_slot_id(core, "op"),
            operation_id="op",
            **args,
            **before,
        )
        == core
    )


def test_source_head_tamper_and_saved_receipt_prefix_missing():
    f = complete()
    for m in (
        replace(f.measurements[0], account_head="f" * 64),
        replace(f.measurements[0], body_head=c3.ZERO_HASH),
    ):
        with pytest.raises(hc.HttpContractError):
            f.reduce(originals=(m,))


def test_observed_before_settlement_cannot_supply_complete_values():
    f = complete()
    m = replace(f.measurements[0], observed_at=NOW + timedelta(seconds=2))
    with pytest.raises(
        hc.HttpContractError,
        match="measurement_source_time|measurement_before_settlement",
    ):
        f.reduce(originals=(m,))


def test_completed_query_cannot_be_reopened():
    f = Fixture().begin().complete()
    f.measurement(saved=True)
    release(f)
    page = replace(f.identity.page, page_index=1, cursor="invented")
    with pytest.raises(hc.HttpContractError, match="cursor_progression"):
        b.build_admission(
            f.c.plan,
            identity=IDENTITY,
            page=page,
            start_observation_reference="start",
            **f.sources(),
        )


def test_no_network_database_clock_or_environment_access(monkeypatch):
    import os
    import socket
    import sqlite3

    f = complete()

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected I/O")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    assert f.reduce().result.saved.compatible


@pytest.mark.parametrize("transfer,decoded,saved", [(1, 0, 0), (0, 1, 0), (0, 0, 1)])
def test_exact_and_plus_one_quota(transfer, decoded, saved):
    limits = Fixture().fixed.limits
    a = b.calculate_allowances(limits, 1000 + transfer, 1000 + decoded, 0, 1000 + saved)
    assert a == b.Allowances(0, 0, 0)


def test_partial_exceeds_historical_allowance_but_not_page_cap():
    f = (
        Fixture()
        .begin()
        .opened()
        .observed(0, state="partial")
        .received(status=503)
        .closed()
        .stabilized()
        .acquired(0)
    )
    f.j = f.j.quarantine_object(f.quarantine_evidence())
    f.measurement(transfer=900, decoded=0, sink=0)
    f.begin().opened().received(outcome="unknown")
    f.measurement("partial", 101, 0, 0)
    r = f.reduce()
    assert r.evidence[1].reservation_allowances.transfer == 100
    assert r.evidence[1].overrun
    assert r.result.known_transfer == 900 and r.result.unknown_transfer_charge == 1000


def test_prior_measurement_not_available_at_next_reservation():
    f = complete(100)
    f.begin(1, "page-2").complete()
    f.measurement(transfer=100, saved=True)
    f.measurements[0] = replace(
        f.measurements[0], observed_at=f.measurements[1].observed_at
    )
    with pytest.raises(
        hc.HttpContractError, match="measurement_unavailable_at_reservation"
    ):
        f.reduce()


def test_headers_are_not_measurement_completion():
    f = Fixture().begin().opened()
    at = f.tick()
    header = c2.capture_headers(
        observation_id="header",
        status=200,
        header_block_complete=True,
        observed_at=at,
        retry_after_fields=(),
    )
    f.pair(
        "response_headers_observed",
        "slot_headers_observed",
        at,
        observation=header.to_dict(),
    )
    f.sync_hold()
    f.measurement("partial", 10, 0, 0)
    assert f.reduce().evidence[0].classification == "open_unsettled"


def test_policy_scheme_and_empty_string_are_rejected():
    with pytest.raises(hc.HttpContractError, match="policy"):
        b.BudgetPolicy("refund-unknown")
    m = complete().measurements[0]
    with pytest.raises(hc.HttpContractError):
        replace(m, producer_reference="")


def test_slot_encoding_matches_independent_hash_calculation():
    import hashlib
    import json

    f = Fixture()
    page = c3.LivePageIdentity(
        f.fixed.sha256, c3._canonical(f.fixed.queries[0].to_dict()), 0, None
    )
    core = b.build_admission(
        f.c.plan,
        identity=IDENTITY,
        page=page,
        start_observation_reference="start",
        **f.sources(),
    )
    payload = {
        "domain": "http-reservation-slot-v1",
        "value": {
            "deployment_id": "deployment",
            "operation_id": "op",
            "admission_digest": hashlib.sha256(core.to_bytes()).hexdigest(),
        },
    }
    independent = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    assert b.derive_slot_id(core, "op") == independent
    assert core.budget_sources.count == len(f.a.records) + len(f.p.records) + len(
        f.j.records
    )


def test_producer_label_never_authenticates_measurement():
    f = complete()
    f.measurements[0] = replace(f.measurements[0], producer_reference="transport")
    r = f.reduce()
    assert not r.evidence[0].producer_authenticated
    assert not r.evidence[0].live_permission and not r.result.live_permission


def test_cross_store_identity_rejected():
    f = complete(100)
    f.begin(1, "page-2").complete()
    f.measurement(transfer=100, saved=True)
    f.measurements[1] = replace(
        f.measurements[1], identity=replace(IDENTITY, store_id="other")
    )
    with pytest.raises(hc.HttpContractError, match="store_identity_conflict"):
        f.reduce()
