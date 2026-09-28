"""Pure C1 tests. No credential, network, persistent Store or live permission."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    LiveAccountContractAlignment,
    LiveAccountIdentityRecord,
    LiveAccountOperatingPolicy,
    LiveAccountPreflightContract,
    LiveClockPolicy,
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
        canonical_store_root=LIVE_STORE_ROOT,
        registered_at=NOW - seconds(300),
        evidence_reference="urn:owner:account-identity-record",
    )
    values.update(changes)
    return LiveAccountIdentityRecord(**values)


def clock(**changes) -> LiveClockPolicy:
    values = dict(max_reservation_to_send_seconds=5, termination_guard_seconds=10)
    values.update(changes)
    return LiveClockPolicy(**values)


def preflight(
    fixed=None, *, account_identity=None, rate_policy=None, clock_policy=None, **changes
) -> LiveAccountPreflightContract:
    fixed = fixed or plan()
    values = dict(
        identity=account_identity or identity(account_ref=fixed.account_ref),
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


def test_identity_derives_one_ledger_path_from_root_and_account_ref():
    first = identity(record_id="owner-record-a")
    second = identity(record_id="owner-record-b")
    assert first.canonical_ledger_path == second.canonical_ledger_path
    assert first.canonical_ledger_path == (
        LIVE_STORE_ROOT + "/accounts/artificial-account/account-rate-ledger.sqlite3"
    )
    assert identity(account_ref="other-account").canonical_ledger_path != (
        first.canonical_ledger_path
    )
    assert not first.identity_verified and not first.secret_material_persisted
    assert {"api_key", "refresh_token", "secret"}.isdisjoint(first.to_dict())


@pytest.mark.parametrize(
    "root,reason",
    [
        ("relative/store", "canonical_store_root_must_be_absolute"),
        ("/var/lib/a/../b", "canonical_store_root_must_be_absolute_canonical_path"),
        ("/", "canonical_store_root_too_broad"),
        (
            str(HTTP_OUTPUT_ROOT / "central-ledger"),
            "canonical_store_root_inside_plan_output_root",
        ),
    ],
)
def test_owner_store_root_rejects_noncanonical_or_plan_output_paths(root, reason):
    with pytest.raises(HttpContractError, match=reason):
        identity(canonical_store_root=root)


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
    assert contract.to_dict()["schema"] == LIVE_ACCOUNT_PREFLIGHT_SCHEMA
    assert contract.sha256 != account_identity.sha256
    assert ACCOUNT_LEDGER_SCHEMA == "historical-feasibility-account-rate-ledger-v2"


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
