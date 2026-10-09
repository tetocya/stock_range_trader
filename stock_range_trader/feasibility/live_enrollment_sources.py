"""I1b strict source codecs. No filesystem lookup, secret reads or acquisition.

Legacy plan/approval constructors inspect physical output paths. This decoder
explicitly reconstructs their immutable values after equivalent lexical/content
validation. Physical custody of the declared body root remains an I3 obligation.
"""

from dataclasses import fields
from datetime import datetime
from functools import wraps

from . import http_contract as hc
from . import live_preflight as c1
from . import live_store as st
from .live_plan_enrollment import _validate_plan


def _require(ok):
    if not ok:
        raise hc.HttpContractError("i1b_source_invalid")


def _roundtrip(value, raw):
    _require(st.canonical_bytes(value.to_dict()) == st.canonical_bytes(raw))
    return value


def _time(raw):
    return datetime.fromisoformat(st._time(raw))


def _plain(cls, raw):
    _require(type(raw) is dict and set(raw) == {f.name for f in fields(cls)})
    return cls(**raw)


def _decoder(function):
    @wraps(function)
    def checked(*args):
        try:
            return function(*args)
        except hc.HttpContractError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
            raise hc.HttpContractError("i1b_source_invalid") from exc

    return checked


@_decoder
def decode_registry(content, digest):
    raw = st._load(content, digest)
    _require(
        set(raw)
        == {
            "schema",
            "canonical_store_root",
            "active_records",
            "fixed_at",
            "decision_reference",
        }
    )
    _require(type(raw["active_records"]) is list)
    value = c1.LiveAccountIdentityRegistry(
        raw["canonical_store_root"],
        tuple(_identity(r) for r in raw["active_records"]),
        _time(raw["fixed_at"]),
        raw["decision_reference"],
    )
    return _roundtrip(value, raw)


def _identity(raw):
    _require(type(raw) is dict)
    expected = {"schema", *(f.name for f in fields(c1.LiveAccountIdentityRecord))}
    _require(set(raw) == expected)
    _require(
        raw["identity_verified"] is False and raw["secret_material_persisted"] is False
    )
    values = {
        f.name: raw[f.name] for f in fields(c1.LiveAccountIdentityRecord) if f.init
    }
    values["registered_at"] = _time(values["registered_at"])
    return _roundtrip(c1.LiveAccountIdentityRecord(**values), raw)


@_decoder
def decode_preflight(content, digest):
    raw = st._load(content, digest)
    _require(
        set(raw)
        == {
            "schema",
            "registry",
            "identity",
            "operating_policy",
            "clock_policy",
            "rate_policy",
            "fixed_at",
            "decision_reference",
            "canonical_ledger_path",
        }
    )
    registry = st.canonical_bytes(raw["registry"])
    value = c1.LiveAccountPreflightContract(
        decode_registry(registry, st.digest_bytes(registry)),
        _identity(raw["identity"]),
        _plain(c1.LiveAccountOperatingPolicy, raw["operating_policy"]),
        _plain(c1.LiveClockPolicy, raw["clock_policy"]),
        _plain(hc.AccountRatePolicy, raw["rate_policy"]),
        _time(raw["fixed_at"]),
        raw["decision_reference"],
    )
    return _roundtrip(value, raw)


@_decoder
def decode_plan(content, digest):
    raw = st._load(content, digest)
    _require(
        set(raw)
        == {
            "schema",
            "purpose",
            "provider",
            "api_version",
            "artifact_id",
            "scope",
            "output_dir",
            "not_before",
            "expires_at",
            "limits",
            "retry",
            "account_ref",
            "auth_reference",
            "resume_policy",
            "expiry_policy",
        }
    )
    scope = raw["scope"]
    _require(
        type(scope) is dict
        and set(scope)
        == {
            "kind",
            "reference_date",
            "calendar_start",
            "calendar_end",
            "master_date",
            "daily_dates",
            "calendar_source_sha256",
            "calendar_source_reference",
            "allowed_endpoints",
            "queries",
            "pagination",
        }
    )
    _require(type(scope["daily_dates"]) is list)
    # Deliberate pure reconstruction, followed by the complete lexical checks
    # and exact derived-scope/canonical comparison below. No physical claim.
    value = object.__new__(hc.HttpAcquisitionPlan)
    for f in fields(hc.HttpAcquisitionPlan):
        source = scope if f.name in scope else raw
        item = source[f.name]
        if f.name == "daily_dates":
            item = tuple(item)
        elif f.name in {"not_before", "expires_at"}:
            item = _time(item)
        elif f.name == "limits":
            item = _plain(hc.HttpLimits, item)
        elif f.name == "retry":
            item = _plain(hc.RetryRules, item)
        object.__setattr__(value, f.name, item)
    for key, item in hc._derived_scope(value, hc._derive_queries(value)).items():
        object.__setattr__(value, key, item)
    _validate_plan(value)
    if value.calendar_source_reference is not None:
        hc._text(value.calendar_source_reference, "calendar_source_reference")
    hc._add_seconds(value.expires_at, hc.MAX_ELAPSED_SECONDS + hc.MAX_WAIT_SECONDS)
    _require(value.sha256 == digest)
    return _roundtrip(value, raw)


@_decoder
def decode_approval(content, digest, plan):
    raw = st._load(content, digest)
    _require(
        set(raw)
        == {
            "schema",
            "authenticity_claim",
            *(f.name for f in fields(hc.OwnerApprovalClaim)),
        }
    )
    value = object.__new__(hc.OwnerApprovalClaim)
    for f in fields(hc.OwnerApprovalClaim):
        item = raw[f.name]
        if f.name in {"valid_from", "valid_until"}:
            item = _time(item)
        elif f.name in {"daily_dates", "allowed_endpoints"}:
            _require(type(item) is list)
            item = tuple(item)
        elif f.name == "limits":
            item = _plain(hc.HttpLimits, item)
        elif f.name == "retry":
            item = _plain(hc.RetryRules, item)
        object.__setattr__(value, f.name, item)
    # Exact plan alignment supplies date, endpoint, path, limits and retry checks.
    hc._check_approval_fields(plan, value)
    for key in (
        "artifact_id",
        "approver_id",
        "approval_event_id",
        "evidence_reference",
    ):
        hc._text(getattr(value, key), key)
    _require(value.valid_from < value.valid_until)
    _require(plan.not_before <= value.valid_from < value.valid_until <= plan.expires_at)
    return _roundtrip(value, raw)
