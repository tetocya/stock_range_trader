"""Protocol table and exact boundaries: explicit synthetic evidence, not OOS."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest

from delayed_replay.checkpoint_models import (
    CheckpointEvidence,
    FinalizationPolicy,
    FinalizationState,
    ProtocolResult,
    finalize,
    net_return,
)
from delayed_replay.protocol_judge import ProtocolJudge
from delayed_replay.replay_calendar import JST, shift_month
from delayed_replay.serialization import JsonObject
from delayed_replay.validation import ReplayContractError

NOW = datetime(2025, 1, 1, tzinfo=UTC)
POLICY = FinalizationPolicy(
    NOW - timedelta(days=2), None, NOW + timedelta(days=2), "synthetic_explicit_dates"
)


def checkpoint(
    months,
    equity="200000",
    count=(20, 100),
    value_state="available",
    sample_state="available",
):
    start = date(2024, 1, 1)
    boundary = shift_month(start, months)
    day = boundary - timedelta(days=1)

    def state(status):
        return FinalizationState(
            status,
            "synthetic_fixture",
            NOW - timedelta(days=1),
            None if status == "pending" else NOW,
            "a" * 64,
        )

    return CheckpointEvidence(
        months,
        start,
        boundary,
        day,
        day if value_state == "available" else None,
        datetime.combine(day, time(15), JST) if value_state == "available" else None,
        replace(POLICY, deadline=NOW)
        if "unavailable_external" in (value_state, sample_state)
        else POLICY,
        state(value_state),
        state(sample_state),
        "200000",
        equity if value_state == "available" else None,
        count[0] if sample_state == "available" else None,
        count[1] if sample_state == "available" else None,
        "synthetic",
        JsonObject.from_value(
            dict(
                account_stream_id="unit-fixture",
                genesis_hash="a" * 64,
                expected_head={"sequence": 1, "event_hash": "b" * 64},
            )
        ),
        JsonObject.from_value({}),
    )


@pytest.mark.parametrize(
    "first,last,count,v1,v3,s3,label",
    [
        ("190000", "200001", (20, 100), "available", "available", "available", "PASS"),
        (
            "190000.0001",
            "200001",
            (20, 100),
            "available",
            "available",
            "available",
            "PASS",
        ),
        (
            "189999.9999",
            "220000",
            (20, 100),
            "available",
            "available",
            "available",
            "FAIL",
        ),
        ("188000", None, (2, 7), "available", "pending", "pending", "FAIL"),
        ("200000", "200000", (20, 100), "available", "available", "available", "FAIL"),
        (
            "200000",
            "199999.9999",
            (20, 100),
            "available",
            "available",
            "available",
            "FAIL",
        ),
        (
            "200000",
            "200000.0001",
            (20, 100),
            "available",
            "available",
            "available",
            "PASS",
        ),
        (
            "200000",
            "220000",
            (19, 100),
            "available",
            "available",
            "available",
            "INCONCLUSIVE",
        ),
        (
            "200000",
            "190000",
            (20, 99),
            "available",
            "available",
            "available",
            "INCONCLUSIVE",
        ),
        (
            None,
            "220000",
            (20, 100),
            "unavailable_external",
            "available",
            "available",
            "INCONCLUSIVE",
        ),
        ("200000", None, (20, 100), "available", "pending", "available", "PENDING"),
        (
            "200000",
            None,
            (20, 100),
            "available",
            "unavailable_external",
            "available",
            "INCONCLUSIVE",
        ),
        (None, "220000", (19, 99), "pending", "available", "available", "PENDING"),
        ("200000", None, (19, 99), "available", "pending", "available", "INCONCLUSIVE"),
        (
            "200000",
            "220000",
            (20, 100),
            "available",
            "available",
            "unavailable_external",
            "INCONCLUSIVE",
        ),
        (
            "188000",
            None,
            (0, 0),
            "available",
            "unavailable_external",
            "unavailable_external",
            "FAIL",
        ),
        ("200000", "220000", (20, 100), "invalid", "available", "available", "INVALID"),
        ("188000", None, (0, 0), "available", "invalid", "pending", "INVALID"),
    ],
)
def test_fixed_decision_table(first, last, count, v1, v3, s3, label):
    result = (
        ProtocolJudge()
        .evaluate(
            checkpoint(1, first, value_state=v1),
            checkpoint(3, last, count, value_state=v3, sample_state=s3),
            now=NOW,
        )
        .to_dict()
    )
    assert result["label"] == label
    assert result["outcome"] == (
        label if label not in ("INVALID", "PENDING") else "N/A"
    )
    assert result["registration_status"] == "draft_not_registered"
    assert result["evidence_kind"] == "synthetic"
    assert result["formal_registration_performed"] is False


def test_during_three_months_gate_failure_remains_pending():
    now = datetime(2024, 3, 1, tzinfo=UTC)
    one = checkpoint(1, "188000")
    one = replace(
        one,
        valuation=replace(one.valuation, observed_at=now, finalized_at=now),
        samples=replace(one.samples, observed_at=now, finalized_at=now),
    )
    three = checkpoint(3, None, value_state="pending", sample_state="pending")
    three = replace(
        three,
        valuation=replace(three.valuation, observed_at=None),
        samples=replace(three.samples, observed_at=None),
    )
    result = ProtocolJudge().evaluate(one, three, now=now).to_dict()
    assert result["label"] == "PENDING" and result["one_month_gate"] == "failed"


@pytest.mark.parametrize(
    "delta,status",
    [(-1, "pending"), (0, "unavailable_external"), (1, "unavailable_external")],
)
def test_deadline_exact_inclusive(delta, status):
    policy = replace(POLICY, deadline=NOW)
    result = finalize(
        value_available=False,
        external_missing=True,
        invalid=False,
        observed_at=None,
        reference_hash="a" * 64,
        period_end=NOW - timedelta(days=10),
        policy=policy,
        now=NOW + timedelta(microseconds=delta),
    )
    assert result.status == status


def test_early_finalization_draft_and_late_amendment():
    kw = dict(
        value_available=True,
        external_missing=False,
        invalid=False,
        observed_at=NOW,
        reference_hash="a" * 64,
        period_end=NOW - timedelta(days=10),
        policy=POLICY,
        now=NOW,
    )
    original = finalize(**kw)
    assert original.status == "available"
    assert finalize(**kw, previous=original) == original
    with pytest.raises(ReplayContractError, match="amendment"):
        finalize(**{**kw, "reference_hash": "b" * 64}, previous=original)
    draft = FinalizationPolicy(None, None, None, None)
    assert finalize(**{**kw, "policy": draft}).status == "pending"
    assert (
        finalize(
            **{**kw, "value_available": False, "invalid": True, "policy": draft}
        ).status
        == "invalid"
    )


@pytest.mark.parametrize(
    "bad",
    [
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        "NaN",
        "Infinity",
        None,
        0,
    ],
)
def test_invalid_equity_and_denominator_not_missing(bad):
    with pytest.raises((ValueError, TypeError)):
        checkpoint(1, bad)
    with pytest.raises((ValueError, TypeError)):
        net_return("200000", bad)


@pytest.mark.parametrize("bad", [True, -1, 1.5, "20", float("nan")])
def test_invalid_sample_counts(bad):
    with pytest.raises((ValueError, TypeError)):
        checkpoint(3, count=(bad, 100))


def test_secondary_cannot_change_outcome_or_gate():
    one, three = checkpoint(1, "190000"), checkpoint(3, "210000")
    baseline = ProtocolJudge().evaluate(one, three, now=NOW).to_dict()
    changed = replace(
        three,
        secondary=JsonObject.from_value(
            {"maximum_drawdown": "0.99", "profit_factor": None}
        ),
    )
    actual = ProtocolJudge().evaluate(one, changed, now=NOW).to_dict()
    assert actual["label"] == baseline["label"] == "PASS"
    assert actual["one_month_gate"] == baseline["one_month_gate"]


def test_digest_and_account_mismatch_rejected():
    one, three = checkpoint(1), checkpoint(3)
    result = ProtocolJudge().evaluate(one, three, now=NOW)
    with pytest.raises(ReplayContractError):
        replace(result, payload_sha256="0" * 64)
    value = result.payload.to_dict()
    value["outcome"] = "PASS"
    with pytest.raises(ReplayContractError):
        ProtocolResult(JsonObject.from_value(value), result.payload_sha256)
    refs = three.references.to_dict()
    refs["account_stream_id"] = "different"
    with pytest.raises(ReplayContractError):
        ProtocolJudge().evaluate(
            one, replace(three, references=JsonObject.from_value(refs)), now=NOW
        )
