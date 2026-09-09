"""Artificial V2 response values only. No market API or model approval."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from delayed_replay_e2e_helpers import network_guard as network_guard
from delayed_replay_stage4_helpers import WALL, fixture_plan

from data.price_policy import provider_price_basis
from delayed_replay.daily_evidence import (
    JQUANTS_RESPONSE_SCHEMA,
    DailyOpenObservation,
    UnsupportedDailyExecution,
    adapt_daily_response,
    response_payload,
)
from delayed_replay.reference_evidence import (
    CalendarEvidence,
    LotEvidence,
    ReferenceReview,
)
from delayed_replay.serialization import JsonObject
from delayed_replay.validation import ReplayContractError

pytestmark = pytest.mark.usefixtures("network_guard")
DAY = date(2024, 6, 3)


def row(**changes):
    return dict(
        Date=DAY.isoformat(),
        Code="TEST0",
        O=100.0,
        H=110.0,
        L=90.0,
        C=101.0,
        Vo=1000.0,
        AdjO=100.0,
        AdjH=110.0,
        AdjL=90.0,
        AdjC=101.0,
        AdjVo=1000.0,
        AdjFactor=1.0,
        **changes,
    )


def adapt(data, **changes):
    args = dict(
        provider="jquants",
        basis=provider_price_basis("jquants"),
        response_schema=JQUANTS_RESPONSE_SCHEMA,
        symbol="TEST0",
        session=DAY,
        first_observed_at=WALL,
        fetched_at=WALL,
        snapshot_hash=response_payload(data).sha256,
    )
    args.update(changes)
    return adapt_daily_response(data, **args)


@pytest.mark.parametrize(
    "changes,status",
    [
        ({}, "reported"),
        ({"O": None}, "missing"),
        ({k: None for k in ("O", "H", "L", "C", "Vo")}, "missing"),
        ({"Vo": 0}, "no_trade_reported"),
        ({"O": -1}, "invalid"),
        ({"H": 80}, "invalid"),
        ({"AdjFactor": 0.5}, "unsupported"),
        ({"ExRT": "3"}, "unsupported"),
    ],
)
def test_reported_values_never_mean_auction_or_fill(changes, status):
    data = row()
    data.update(changes)
    observation = adapt(data)
    assert observation.to_dict()["quality"] == status
    assert observation.to_dict()["trade_at"] is None
    assert observation.to_dict()["kind"] == "provider_daily_open"
    with pytest.raises(UnsupportedDailyExecution):
        observation.require_execution()
    # Null cannot distinguish halt from no-trade or incomplete publication.
    if status == "missing":
        assert observation.to_dict()["reason"] == "null_reported_value_cause_unknown"


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "yfinance"),
        ("provider", "unknown"),
        ("basis", "unknown"),
        ("response_schema", "v1"),
        ("symbol", "OTHER"),
        ("session", DAY + timedelta(days=1)),
        ("snapshot_hash", "0" * 64),
        ("first_observed_at", WALL + timedelta(seconds=1)),
    ],
)
def test_adapter_contract_fail_closed(field, value):
    with pytest.raises((ValueError, ReplayContractError)):
        adapt(row(), **{field: value})


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), "wrong", {}])
def test_invalid_not_missing(bad):
    data = row()
    data["O"] = bad
    with pytest.raises((ValueError, ReplayContractError)):
        adapt(data)


def test_observation_integrity_and_missing_column():
    data = row()
    del data["AdjVo"]
    with pytest.raises(ReplayContractError):
        adapt(data)
    original = adapt(row())
    changed = original.to_dict()
    changed["trade_at"] = WALL.isoformat()
    p = JsonObject.from_value(changed)
    with pytest.raises(ReplayContractError):
        DailyOpenObservation(p, p.sha256)
    with pytest.raises(ReplayContractError):
        replace(original, payload_sha256="0" * 64)


@pytest.mark.parametrize("lot", [None, 1, 50, 100])
def test_lot_review_instrument_and_period(lot):
    item = LotEvidence(
        "TEST0",
        DAY,
        DAY + timedelta(days=30),
        lot,
        ReferenceReview("explicit-local-source", "a" * 64, None),
    )
    with pytest.raises(ReplayContractError):
        item.require("TEST0", DAY)
    item = replace(
        item,
        review=ReferenceReview("explicit-local-source", item.subject_hash, "b" * 64),
    )
    if lot == 100:
        assert item.require("TEST0", DAY) == 100
    else:
        with pytest.raises(ReplayContractError):
            item.require("TEST0", DAY)
    for instrument, day in (("OTHER", DAY), ("TEST0", DAY + timedelta(days=30))):
        with pytest.raises(ReplayContractError):
            item.require(instrument, day)


def test_calendar_explicit_review_no_weekday_inference():
    calendar = fixture_plan().calendar
    start, end = date(2024, 4, 1), date(2024, 10, 1)
    item = CalendarEvidence(
        calendar,
        start,
        end,
        ReferenceReview("local-reviewed-calendar", "a" * 64, None),
        "TSE",
    )
    with pytest.raises(ReplayContractError):
        item.require(start, end)
    item = replace(
        item,
        review=ReferenceReview("local-reviewed-calendar", item.subject_hash, "b" * 64),
    )
    assert item.require(start, end) == calendar.sessions
    with pytest.raises(ReplayContractError):
        item.require(start, end + timedelta(days=1))
    with pytest.raises(ReplayContractError):
        replace(item, market="OSE")


def test_master_lot_not_inferred_and_calendar_response_coverage():
    from delayed_replay.reference_evidence import calendar_from_rows, lot_from_master

    master = lot_from_master(
        {"Code": "TEST0", "Date": DAY.isoformat(), "Unit": 100},
        instrument="TEST0",
        requested_date=DAY,
        effective_end=DAY + timedelta(days=1),
        source="jquants-master",
    )
    assert master.lot_size is None
    with pytest.raises(ReplayContractError):
        master.require("TEST0", DAY)
    with pytest.raises(ReplayContractError):
        lot_from_master(
            {"Code": "TEST0", "Date": "2024-06-04"},
            instrument="TEST0",
            requested_date=DAY,
            effective_end=DAY + timedelta(days=2),
            source="jquants-master",
        )
    session = next(s for s in fixture_plan().calendar.sessions if s.day == DAY)
    rows = (
        {"Date": DAY.isoformat(), "HolDiv": "1"},
        {"Date": "2024-06-04", "HolDiv": "3"},
    )
    args = dict(
        start=DAY,
        end=DAY + timedelta(days=2),
        sessions=(session,),
        review=ReferenceReview("calendar", "a" * 64, None),
    )
    result = calendar_from_rows(rows, **args)
    assert len(result.calendar.sessions) == 1
    with pytest.raises(ReplayContractError):
        result.require(args["start"], args["end"])
    with pytest.raises(ReplayContractError, match="incomplete"):
        calendar_from_rows(rows[:1], **args)
    with pytest.raises(ReplayContractError):
        calendar_from_rows((rows[0], {"Date": "2024-06-04", "HolDiv": "wrong"}), **args)


def test_partial_missing_does_not_hide_invalid_price():
    data = row()
    data.update(O=None, H=-1)
    assert adapt(data).to_dict()["quality"] == "invalid"
