"""Pure C1 tests. No credential, network, persistent Store or live permission."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from feasibility.http_contract import (
    ACCOUNT_LEDGER_SCHEMA,
    HTTP_OUTPUT_ROOT,
    AccountRatePolicy,
    HttpAcquisitionPlan,
    HttpContractError,
    HttpLimits,
    RetryRules,
)
from feasibility.live_preflight import (
    LIVE_ACCOUNT_IDENTITY_SCHEMA,
    LIVE_ACCOUNT_PREFLIGHT_SCHEMA,
    LIVE_ACCOUNT_REGISTRY_SCHEMA,
    LiveAccountContractAlignment,
    LiveAccountIdentityRecord,
    LiveAccountIdentityRegistry,
    LiveAccountOperatingPolicy,
    LiveAccountPreflightContract,
    LiveClockPolicy,
    LiveReclaimAssessment,
    LiveReclaimEvidence,
    assess_live_reclaim,
    check_live_account_preflight,
)

NOW = datetime(2026, 9, 28, 2, tzinfo=UTC)
ACCOUNT = "artificial-account"
DIGEST = "a" * 64
LIVE_STORE_ROOT = "/var/lib/stock-range-trader/live-feasibility"
MICRO = timedelta(microseconds=1)


def seconds(value: float) -> timedelta:
    return timedelta(seconds=value)


def retry(**changes) -> RetryRules:
    values = dict(
        max_attempts_per_page=3,
        min_interval_seconds=13,
        min_wait_after_429_seconds=120,
        min_wait_after_5xx_seconds=13,
        min_wait_after_network_error_seconds=13,
        timeout_seconds=30,
    )
    values.update(changes)
    return RetryRules(**values)


def limits() -> HttpLimits:
    return HttpLimits(
        max_attempts=8,
        max_pages_total=5,
        max_pages_per_query=2,
        max_elapsed_seconds=1200,
        max_transfer_bytes=1000,
        max_decoded_bytes=1200,
        max_saved_bytes=900,
        max_page_transfer_bytes=500,
        max_page_decoded_bytes=600,
        max_page_saved_bytes=450,
    )


def plan(**changes) -> HttpAcquisitionPlan:
    values = dict(
        artifact_id="artificial-c1",
        kind="predeclared_daily",
        reference_date="2025-03-04",
        calendar_start="2025-03-03",
        calendar_end="2025-03-04",
        master_date="2025-03-04",
        daily_dates=("2025-03-04", "2025-03-03"),
        calendar_source_sha256=DIGEST,
        calendar_source_reference="urn:artificial:calendar-source",
        output_dir=str(HTTP_OUTPUT_ROOT / "artificial-c1"),
        not_before=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        limits=limits(),
        retry=retry(),
        account_ref=ACCOUNT,
    )
    values.update(changes)
    return HttpAcquisitionPlan(**values)


def account_policy(**changes) -> AccountRatePolicy:
    values = dict(
        min_interval_seconds=13,
        min_wait_after_429_seconds=120,
        min_wait_after_5xx_seconds=13,
        min_wait_after_network_error_seconds=13,
        request_timeout_seconds=30,
        slot_lease_seconds=60,
    )
    values.update(changes)
    return AccountRatePolicy(**values)


def identity(**changes) -> LiveAccountIdentityRecord:
    values = dict(
        record_id="owner-account-record",
        account_ref=ACCOUNT,
        credential_reference="env:JQUANTS_API_KEY",
        registered_at=NOW - seconds(300),
        evidence_reference="urn:owner:account-identity-record",
    )
    values.update(changes)
    return LiveAccountIdentityRecord(**values)


def registry(*records, **changes) -> LiveAccountIdentityRegistry:
    values = dict(
        canonical_store_root=LIVE_STORE_ROOT,
        active_records=records or (identity(),),
        fixed_at=NOW - seconds(60),
        decision_reference="urn:owner:registry-decision",
    )
    values.update(changes)
    return LiveAccountIdentityRegistry(**values)


def clock(**changes) -> LiveClockPolicy:
    values = dict(max_reservation_to_send_seconds=5, termination_guard_seconds=10)
    values.update(changes)
    return LiveClockPolicy(**values)


def preflight(
    fixed=None,
    *,
    account_identity=None,
    account_registry=None,
    rate_policy=None,
    clock_policy=None,
    **changes,
) -> LiveAccountPreflightContract:
    fixed = fixed or plan()
    selected_identity = account_identity or identity(account_ref=fixed.account_ref)
    values = dict(
        identity=selected_identity,
        registry=account_registry or registry(selected_identity),
        operating_policy=LiveAccountOperatingPolicy(),
        clock_policy=clock_policy or clock(),
        rate_policy=rate_policy or account_policy(),
        fixed_at=NOW,
        decision_reference="owner-decisions:OD-01-OD-03-OD-08-OD-09",
    )
    values.update(changes)
    return LiveAccountPreflightContract(**values)


def reclaim_evidence(**changes) -> LiveReclaimEvidence:
    values = dict(
        lease_expired_at=NOW,
        observed_at=NOW + seconds(1),
        clock_status="verified",
        previous_transport_termination_reference="urn:process-exit:runner-a",
        operator_review_reference="urn:operator-review:reclaim-a",
        process_restart_detected=False,
    )
    values.update(changes)
    return LiveReclaimEvidence(**values)


def test_registry_derives_one_ledger_path_from_root_and_account_ref():
    first = identity(record_id="owner-record-a")
    second = identity(record_id="owner-record-b", credential_reference="env:OTHER_KEY")
    owner_registry = registry(first)
    first_path = owner_registry.canonical_ledger_path(first.account_ref)
    assert first_path == (
        LIVE_STORE_ROOT + "/accounts/artificial-account/account-rate-ledger.sqlite3"
    )
    assert registry(second).canonical_ledger_path(second.account_ref) == first_path
    other = identity(account_ref="other-account")
    assert registry(first, other).canonical_ledger_path(other.account_ref) != first_path
    assert not hasattr(first, "canonical_store_root")
    assert not hasattr(first, "canonical_ledger_path")
    assert not first.identity_verified and not first.secret_material_persisted
    assert {"api_key", "refresh_token", "secret"}.isdisjoint(first.to_dict())


def test_registry_rejects_duplicate_account_and_preflight_foreign_identity():
    first = identity()
    with pytest.raises(HttpContractError, match="live_registry_duplicate_account_ref"):
        registry(first, identity(record_id="second"))
    owner_registry = registry(first)
    with pytest.raises(
        HttpContractError, match="live_preflight_identity_registry_mismatch"
    ):
        preflight(
            account_registry=owner_registry,
            account_identity=identity(record_id="foreign"),
        )
    with pytest.raises(TypeError):
        preflight(canonical_store_root="/srv/other")
    assert preflight(account_registry=owner_registry).canonical_ledger_path == (
        owner_registry.canonical_ledger_path(ACCOUNT)
    )


@pytest.mark.parametrize(
    "reference",
    ["secret", "sk_live_xxx", "", "env:", "env:lower", "env:API-KEY", "env:API\nKEY"],
)
def test_identity_rejects_secret_or_malformed_credential_reference(reference):
    with pytest.raises(HttpContractError, match="credential_reference_invalid"):
        identity(credential_reference=reference)


@pytest.mark.parametrize(
    "root,reason",
    [
        ("relative/store", "canonical_store_root_must_be_absolute"),
        ("/var/lib/a/../b", "canonical_store_root_must_be_absolute_canonical_path"),
        ("/", "canonical_store_root_too_broad"),
        ("/srv/x/", "canonical_store_root_must_be_absolute_canonical_path"),
        ("/srv//x", "canonical_store_root_must_be_absolute_canonical_path"),
        ("/srv/./x", "canonical_store_root_must_be_absolute_canonical_path"),
        ("//srv/x", "canonical_store_root_must_be_absolute_canonical_path"),
        (
            str(HTTP_OUTPUT_ROOT / "central-ledger"),
            "canonical_store_root_inside_forbidden_root",
        ),
        (str(HTTP_OUTPUT_ROOT.parent), "canonical_store_root_inside_forbidden_root"),
        (
            str(HTTP_OUTPUT_ROOT.parent / "other"),
            "canonical_store_root_inside_forbidden_root",
        ),
        (
            str(Path(__file__).resolve().parents[2]),
            "canonical_store_root_inside_forbidden_root",
        ),
        (
            str(Path(__file__).resolve().parents[1]),
            "canonical_store_root_inside_forbidden_root",
        ),
        ("/tmp", "canonical_store_root_inside_forbidden_root"),
        ("/var/tmp/live", "canonical_store_root_inside_forbidden_root"),
        ("/private/tmp/live", "canonical_store_root_inside_forbidden_root"),
        ("/private/var/folders/jx/live", "canonical_store_root_inside_forbidden_root"),
    ],
)
def test_owner_store_root_rejects_noncanonical_or_plan_output_paths(root, reason):
    with pytest.raises(HttpContractError, match=reason):
        registry(canonical_store_root=root)


def test_operating_policy_fixes_account_wide_exclusive_use():
    policy = LiveAccountOperatingPolicy()
    assert policy.sender_topology == "single_canonical_sender"
    assert policy.external_client_policy == "prohibited_during_live_acquisition"
    assert policy.violation_policy == "hold_and_stop"
    with pytest.raises(
        HttpContractError, match="live_account_operating_policy_invalid"
    ):
        LiveAccountOperatingPolicy(external_client_policy="allowed")


def test_clock_policy_requires_strict_request_guard_inside_lease():
    policy = clock()
    assert policy.minimum_lease_seconds(account_policy()) == 46
    preflight(clock_policy=policy)
    with pytest.raises(HttpContractError, match="live_slot_lease_guard_insufficient"):
        preflight(
            clock_policy=policy,
            rate_policy=account_policy(slot_lease_seconds=45),
        )
    preflight(rate_policy=account_policy(slot_lease_seconds=46))
    with pytest.raises(HttpContractError, match="account_rate_policy_required"):
        policy.validate_rate_policy("not-a-policy")
    with pytest.raises(HttpContractError, match="live_clock_policy_invalid"):
        clock(caller_event_time_policy="allowed")


def test_plan_alignment_is_informational_and_never_live_permission():
    fixed = plan()
    contract = preflight(fixed)
    aligned = check_live_account_preflight(fixed, contract)
    assert isinstance(aligned, LiveAccountContractAlignment)
    assert aligned.content_matches
    assert aligned.canonical_ledger_path == contract.canonical_ledger_path
    assert not aligned.account_identity_verified
    assert not aligned.canonical_store_implemented
    assert not aligned.external_client_exclusivity_verified
    assert not aligned.authoritative_clock_implemented
    assert not aligned.live_send_permitted
    with pytest.raises(HttpContractError, match="live_account_ref_mismatch"):
        check_live_account_preflight(plan(account_ref="other-account"), contract)


def test_preflight_rejects_account_policy_weaker_than_plan():
    fixed = plan(retry=retry(timeout_seconds=40))
    contract = preflight(
        fixed,
        rate_policy=account_policy(request_timeout_seconds=30),
    )
    with pytest.raises(HttpContractError, match="live_account_policy_weaker_than_plan"):
        check_live_account_preflight(fixed, contract)


def test_contracts_are_versioned_without_changing_v2_account_ledger():
    account_identity = identity()
    contract = preflight(account_identity=account_identity)
    assert account_identity.to_dict()["schema"] == LIVE_ACCOUNT_IDENTITY_SCHEMA
    assert contract.registry.to_dict()["schema"] == LIVE_ACCOUNT_REGISTRY_SCHEMA
    assert contract.to_dict()["schema"] == LIVE_ACCOUNT_PREFLIGHT_SCHEMA
    assert contract.sha256 != account_identity.sha256
    assert ACCOUNT_LEDGER_SCHEMA == "historical-feasibility-account-rate-ledger-v2"


def test_preflight_cannot_predate_registry_or_identity():
    with pytest.raises(
        HttpContractError, match="live_preflight_before_identity_registry"
    ):
        preflight(fixed_at=NOW - seconds(61))
    later_identity = identity(registered_at=NOW + seconds(1))
    later_registry = registry(later_identity, fixed_at=NOW + seconds(2))
    with pytest.raises(
        HttpContractError, match="live_preflight_before_identity_registry"
    ):
        preflight(account_identity=later_identity, account_registry=later_registry)
    preflight(
        account_identity=later_identity,
        account_registry=later_registry,
        fixed_at=NOW + seconds(2),
    )


def test_reclaim_is_never_automatic_and_requires_explicit_safe_evidence():
    safe = assess_live_reclaim(clock(), reclaim_evidence())
    assert safe.manual_reclaim_eligible
    assert safe.reasons == ()
    assert not safe.automatic_reclaim_permitted

    early = assess_live_reclaim(clock(), reclaim_evidence(observed_at=NOW - MICRO))
    assert not early.manual_reclaim_eligible
    assert "live_lease_not_expired" in early.reasons

    uncertain = assess_live_reclaim(clock(), reclaim_evidence(clock_status="uncertain"))
    assert not uncertain.manual_reclaim_eligible
    assert "live_clock_uncertain" in uncertain.reasons

    missing = assess_live_reclaim(
        clock(),
        reclaim_evidence(
            previous_transport_termination_reference=None,
            process_restart_detected=True,
        ),
    )
    assert not missing.manual_reclaim_eligible
    assert "previous_transport_termination_unverified" in missing.reasons
    assert not missing.automatic_reclaim_permitted


def test_reclaim_eligibility_is_derived_and_automatic_reclaim_cannot_be_set():
    assert LiveReclaimAssessment(()).manual_reclaim_eligible
    assert not LiveReclaimAssessment(("missing",)).manual_reclaim_eligible
    with pytest.raises(TypeError):
        LiveReclaimAssessment(("missing",), manual_reclaim_eligible=True)
    with pytest.raises(TypeError):
        LiveReclaimAssessment((), manual_reclaim_eligible=False)
    with pytest.raises(TypeError):
        LiveReclaimAssessment((), automatic_reclaim_permitted=True)


def test_restart_requires_manual_review_and_transport_termination_evidence():
    held = assess_live_reclaim(
        clock(),
        reclaim_evidence(
            process_restart_detected=True,
            previous_transport_termination_reference=None,
        ),
    )
    assert not held.manual_reclaim_eligible

    reviewed = assess_live_reclaim(
        clock(), reclaim_evidence(process_restart_detected=True)
    )
    assert reviewed.manual_reclaim_eligible
    assert not reviewed.automatic_reclaim_permitted
