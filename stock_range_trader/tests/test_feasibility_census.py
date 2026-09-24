"""Offline tests for the static purchasable-universe census (artificial data)."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from config.settings import load_strategy_config
from delayed_replay.account_policy import AccountPolicy
from delayed_replay.sizing import size_buy
from feasibility.acquisition import (
    CALENDAR,
    DAILY,
    MASTER,
    AcquisitionLimits,
    AcquisitionPlan,
    AcquisitionStore,
    DateQuery,
    OfflineFixtureTransport,
    TransportResponse,
    fixture_key,
    load_completed_rows,
    run_offline_fixture_acquisition,
)
from feasibility.census import (
    CensusData,
    CensusError,
    CensusRequest,
    CensusTerms,
    LotEvidence,
    ViewedPeriod,
    one_lot_requirement,
    run_census,
    sessions_from_calendar,
    write_census_bundle,
)
from feasibility.paths import UnsafeOutputPath

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = load_strategy_config(PROJECT / "config" / "strategy.yaml")
SESSIONS = tuple(d.date() for d in pd.bdate_range("2024-10-01", periods=110))
R = SESSIONS[100]
N = 79  # first finite Range Score of the current pipeline needs 79 sessions


def terms(**overrides) -> CensusTerms:
    values = dict(
        status="provisional",
        approval_reference=None,
        max_position_pct="0.10",
        max_positions=5,
        commission_rate="0.001",
        slippage_pct="0.001",
        reservation_buffer_pct="0.01",
        price_quantum="0.01",
        money_quantum="0.01",
        buy_price_rounding="ceiling",
        amount_rounding="half_even",
        fee_rounding="ceiling",
        reservation_rounding="ceiling",
        budget_rounding="floor",
    )
    values.update(overrides)
    return CensusTerms(**values)


ZERO_COST = dict(commission_rate="0", slippage_pct="0", reservation_buffer_pct="0")


def master(code: str, *, prod: str = "011", market: str = "0112") -> dict:
    return {
        "Date": R.isoformat(),
        "Code": code,
        "CoName": f"Artificial {code}",
        "Mkt": market,
        "MktNm": "m",
        "S17": "1",
        "S17Nm": "s",
        "S33": "0050",
        "S33Nm": "s",
        "ProdCat": prod,
    }


def bar(
    code: str,
    day: date,
    price: float,
    *,
    factor: float = 1.0,
    adj: float = 1.0,
    null: bool = False,
    volume: int = 100_000,
) -> dict:
    if null:
        return {
            "Date": day.isoformat(),
            "Code": code,
            **dict.fromkeys(("O", "H", "L", "C", "Va", "AdjO", "AdjH", "AdjL", "AdjC")),
            "Vo": 0,
            "AdjVo": 0,
            "AdjFactor": 1,
        }
    raw = round(price, 1)
    return {
        "Date": day.isoformat(),
        "Code": code,
        "O": raw, "H": round(raw * 1.01, 1), "L": round(raw * 0.99, 1), "C": raw,
        "Vo": volume, "Va": raw * volume, "AdjFactor": factor,
        "AdjO": raw * adj, "AdjH": round(raw * 1.01, 1) * adj,
        "AdjL": round(raw * 0.99, 1) * adj, "AdjC": raw * adj,
        "AdjVo": volume / adj,
    }  # fmt: skip


def series(code: str, base: float, days=SESSIONS, **kwargs) -> list[dict]:
    rows = []
    for i, day in enumerate(days):
        price = base * (1 + 0.04 * math.sin(i / 3))
        if day == R and "close_at_r" in kwargs:
            price = kwargs["close_at_r"]
        rows.append(
            bar(
                code,
                day,
                price,
                **{k: v for k, v in kwargs.items() if k != "close_at_r"},
            )
        )
    return rows


def artificial_market():
    rows = []
    rows += series("10010", 150, close_at_r=150)  # C1 D1 E0 F1
    rows += series("10020", 300, close_at_r=300)  # C0 D1
    rows += series("10030", 150, days=SESSIONS[70:], close_at_r=150)  # new listing
    rows += series("10040", 300, days=SESSIONS[70:], close_at_r=300)  # C0 and D0
    rows += series("10050", 150, close_at_r=150)
    rows[-15] = bar("10050", SESSIONS[95], 0, null=True)  # no-trade/halt Null day
    split = series("10060", 150, close_at_r=150)
    for i, row in enumerate(split):  # 2:1 split effective at SESSIONS[90]
        if i < 90:
            row.update(bar("10060", SESSIONS[i], row["C"] * 2, adj=0.5))
        if i == 90:
            row["AdjFactor"] = 0.5
    rows += split
    halted = series("10070", 150)
    halted[100] = bar("10070", R, 0, null=True)
    rows += halted
    zero = series("10080", 150, close_at_r=150)
    zero[99]["Vo"] = 0
    rows += zero
    masters = [
        master(c)
        for c in (
            "10010",
            "10020",
            "10030",
            "10040",
            "10050",
            "10060",
            "10070",
            "10080",
        )
    ]
    masters += [master("20010", prod="012"), master("20020", market="0105")]
    return masters, rows


def request(**overrides) -> CensusRequest:
    values = dict(
        reference_date=R,
        terms=terms(),
        strategy_config=CONFIG,
        minimum_history_sessions=N,
        lot_policy="assume_100_unverified",
        viewed_periods=(
            ViewedPeriod(
                "limited_proxy_2026_05_06", date(2026, 5, 1), date(2026, 7, 1)
            ),
        ),
        reconstruction_assumptions=("artificial_fixture",),
    )
    values.update(overrides)
    return CensusRequest(**values)


def data(masters=None, rows=None, acquired=SESSIONS) -> CensusData:
    base_masters, base_rows = artificial_market()
    return CensusData(
        master_rows=tuple(masters if masters is not None else base_masters),
        daily_rows=tuple(rows if rows is not None else base_rows),
        sessions=SESSIONS,
        acquired_daily_dates=tuple(acquired),
        acquisition_provenance={"source": "artificial_fixture"},
    )


def by_code(result) -> dict:
    return {s["jquants_code"]: s for s in result.symbols}


# ---------------------------------------------------------------- purchasability


@pytest.mark.parametrize(
    ("price", "cost_terms", "expected"),
    [
        ("200", ZERO_COST, True),  # exactly 20,000 = 10% of 200,000
        ("200.01", ZERO_COST, False),
        ("197.00", {}, True),  # 199.17 x 100 + 19.92 = 19,936.92
        ("197.70", {}, False),  # 199.88 x 100 + 19.99 = 20,007.99
        ("199", ZERO_COST, True),
        (
            "199",
            {
                "reservation_buffer_pct": "0.01",
                "commission_rate": "0",
                "slippage_pct": "0",
            },
            False,
        ),  # buffer alone flips it
        ("199.85", ZERO_COST, True),
        (
            "199.85",
            {
                "commission_rate": "0.001",
                "slippage_pct": "0",
                "reservation_buffer_pct": "0",
            },
            False,
        ),  # fee alone flips it
    ],
)
def test_one_lot_boundary_under_ten_percent_cap(price, cost_terms, expected):
    result = one_lot_requirement(Decimal(price), terms(**cost_terms), lot_size=100)
    assert result["cap"] == "20000.00"
    assert result["purchasable"] is expected


def test_one_lot_order_matches_delayed_replay_size_buy():
    t = terms()
    policy = AccountPolicy(
        purpose="synthetic_test", initial_capital="200000", lot_size=100,
        max_position_pct="0.1", max_positions=5, commission_rate="0.001",
        slippage_pct="0.001", reservation_buffer_pct="0.01", price_quantum="0.01",
        money_quantum="0.01", buy_price_rounding="ceiling", sell_price_rounding="floor",
        fee_rounding="ceiling", reservation_rounding="ceiling", amount_rounding="half_even",
        budget_rounding="floor", priority_mode="sell_then_score_desc_instrument",
        proceeds_mode="hold_until_later_decision_session", fill_mode="all_or_reject",
        position_mode="single_position_full_exit", expiry_mode="explicit_target_session_only",
        cost_model="proportional", dividend_policy="excluded", basis_evidence_hash="0" * 64,
    )  # fmt: skip
    for cents in range(19500, 20100, 7):
        price = Decimal(cents) / 100
        ours = one_lot_requirement(price, t, lot_size=100)
        sized = size_buy(format(price, "f"), "200000", "200000", policy)
        assert ours["purchasable"] == (sized.shares >= 100), price
        if sized.shares == 100:
            assert Decimal(ours["required"]) == Decimal(sized.reserved_cash)


def test_terms_separate_provisional_from_owner_approved():
    with pytest.raises(CensusError, match="approved_terms_require_approval_reference"):
        terms(status="owner_approved")
    with pytest.raises(
        CensusError, match="provisional_terms_have_no_approval_reference"
    ):
        terms(approval_reference="x")
    with pytest.raises(CensusError, match="initial_capital_is_protocol_adopted"):
        terms(initial_capital="1000000")
    approved = terms(status="owner_approved", approval_reference="owner-decision-1")
    summary = run_census(
        request(terms=approved, lot_policy="require_evidence", lot_evidence={}), data()
    ).summary
    assert summary["result_kind"] == "reference_only_unverified_or_unknown_lots"
    summary = run_census(request(), data()).summary
    assert summary["result_kind"] == "reference_only_provisional_terms"


# ---------------------------------------------------------------- census


def test_categories_are_counted_individually_with_overlaps():
    result = run_census(request(), data())
    s = by_code(result)
    assert set(s) == {
        "10010",
        "10020",
        "10030",
        "10040",
        "10050",
        "10060",
        "10070",
        "10080",
    }
    flags = {c: (r["c_purchasable"], r["d_history_sufficient"],
                 r["e_data_issue_affected"], r["f_current_processing_acceptable"])
             for c, r in s.items()}  # fmt: skip
    assert flags["10010"] == (True, True, False, True)
    assert flags["10020"] == (False, True, False, True)
    assert flags["10030"][:3] == (True, False, True)
    assert flags["10040"][:3] == (False, False, True)
    assert s["10040"]["c_reason"] == "one_lot_exceeds_position_cap"
    assert s["10040"]["d_reason"] == "history_window_incomplete"

    counts = result.summary["counts"]
    assert counts["a_domestic_common_stock"] == 8  # ProdCat 012 / market 0105 excluded
    assert counts["c_purchasable_assumed_lot_reference_only"] == 5
    assert counts["c_purchasable_verified_lot"] == 0
    overlaps = result.summary["overlaps"]
    assert overlaps["pairwise"]["cost_cap_exceeded_and_not_d"] == 1
    assert sum(overlaps["cdef_pattern_counts"].values()) == 8


def test_null_no_trade_zero_volume_and_corporate_action_are_classified():
    s = by_code(run_census(request(), data()))
    assert "null_ohlc_no_trade_or_halt_cause_unknown@" in s["10050"]["e_issues"]
    assert s["10050"]["d_history_sufficient"] is False
    assert s["10050"]["f_reason"].startswith("canonical_rejected:")
    assert s["10050"]["c_purchasable"] is True  # not excluded from the census

    assert "adjustment_factor_not_one@" in s["10060"]["e_issues"]
    assert s["10060"]["d_history_sufficient"] is True  # adjusted lane stays consistent
    assert s["10060"]["f_reason"] == "executable_contract_unsupported_corporate_action"

    assert s["10070"]["b_price_and_lot_known"] is False
    assert s["10070"]["c_reason"] == "price_not_observed_at_r"

    assert "zero_volume@" in s["10080"]["e_issues"]

    # New listing: rows missing on acquired sessions; current processing does not notice.
    assert "no_row_on_acquired_session@" in s["10030"]["e_issues"]
    assert s["10030"]["f_current_processing_acceptable"] is True
    assert s["10030"]["f_notes"] == "session_gaps_not_detected_by_current_processing"


def test_unacquired_session_is_recorded_not_guessed():
    acquired = tuple(d for d in SESSIONS if d != SESSIONS[90])
    result = run_census(request(), data(acquired=acquired))
    assert result.summary["e_issue_symbol_counts"]["session_not_acquired"] == 8
    assert result.summary["counts"]["d_history_sufficient"] == 0


def test_lot_size_unknown_unsupported_and_verified():
    evidence = {
        "10010": LotEvidence(100, "issuer-charter-sha", date(2020, 1, 1)),
        "10020": LotEvidence(1000, "issuer-charter-sha", date(2020, 1, 1)),
        "10080": LotEvidence(100, "later-doc", R + timedelta(days=1)),  # not valid at R
    }
    result = run_census(
        request(lot_policy="require_evidence", lot_evidence=evidence), data()
    )
    s = by_code(result)
    assert (s["10010"]["lot_status"], s["10010"]["c_purchasable"]) == ("verified", True)
    assert s["10020"]["c_reason"] == "lot_size_not_100_unsupported"
    assert s["10080"]["lot_status"] == "unknown"
    assert s["10080"]["c_reason"] == "lot_size_unknown"
    lot = result.summary["lot"]
    assert lot["status_counts"]["unknown"] == 6
    assert lot["unverified_count"] == 7
    assumed = run_census(request(), data()).summary["lot"]
    assert assumed["status_counts"] == {"assumed_100_unverified": 8}


def test_future_rows_do_not_change_reference_date_result():
    masters, rows = artificial_market()
    base = run_census(request(), data(masters, rows))
    future_days = tuple(
        d.date() for d in pd.bdate_range(R + timedelta(days=1), periods=5)
    )
    future = [dict(r) for r in rows]
    for row in future:
        if date.fromisoformat(row["Date"]) > R and row["C"] is not None:
            row.update(C=row["C"] * 10, AdjFactor=0.1)
    for code in ("10010", "10020"):
        future += [
            bar(code, d, 999, factor=0.5) for d in future_days if d > SESSIONS[-1]
        ]
    changed = run_census(request(), data(masters, future))
    assert changed.symbols == base.symbols
    for key in (
        "counts",
        "overlaps",
        "reasons",
        "input_hashes",
        "market_data_period_used",
    ):
        assert changed.summary[key] == base.summary[key]
    assert changed.summary["future_rows_supplied_and_ignored"] > 0


def test_used_input_hash_changes_when_history_changes():
    masters, rows = artificial_market()
    base = run_census(request(), data(masters, rows)).summary["input_hashes"]
    rows[5] = dict(rows[5], C=rows[5]["C"] + 0.1)
    changed = run_census(request(), data(masters, rows)).summary["input_hashes"]
    assert changed["daily_rows_used_sha256"] != base["daily_rows_used_sha256"]
    assert changed["master_rows_sha256"] == base["master_rows_sha256"]


def test_provenance_and_claims_are_recorded():
    summary = run_census(request(), data()).summary
    assert summary["reference_date"] == R.isoformat()
    assert summary["claims"]["estimates_future_executions_or_trades"] is False
    assert (
        summary["claims"]["adjusted_series_basis"]
        == "provider_adjusted_as_of_retrieval"
    )
    assert (
        summary["claims"]["candidate_means"]
        == "strategy_parameter_candidate_not_instrument"
    )
    assert summary["history_window"]["minimum_sessions"] == N
    assert summary["conditions"]["evaluated_provisional"] == {
        "max_position_pct": "0.10",
        "max_positions": 5,
    }
    assert summary["viewed_period_overlaps"] == []  # artificial 2025 data vs 2026 trial
    assert summary["reconstruction_assumptions"] == ["artificial_fixture"]
    json.dumps(summary)  # serializable


def test_reference_date_must_be_a_session_and_master_must_match():
    with pytest.raises(CensusError, match="reference_date_is_not_a_session"):
        run_census(request(reference_date=date(2025, 3, 1)), data())
    masters, _ = artificial_market()
    masters[0] = dict(masters[0], Date=SESSIONS[99].isoformat())
    with pytest.raises(ValueError, match="must exactly match as_of_date"):
        run_census(request(), data(masters=masters))


# ---------------------------------------------------------------- output


def test_bundle_is_written_once_and_never_into_trial_paths(tmp_path):
    result = run_census(request(), data())
    out = write_census_bundle(result, tmp_path / "census-v1")
    with open(out / "census_symbols.csv", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 8
    assert json.loads((out / "census_summary.json").read_text())["schema"] == (
        "feasibility-static-census-v1"
    )
    with pytest.raises(UnsafeOutputPath, match="already_exists"):
        write_census_bundle(result, tmp_path / "census-v1")
    trial = tmp_path / "stock_range_trader" / ".delayed_replay" / "june_trial" / "x"
    with pytest.raises(UnsafeOutputPath, match="trial_evidence_tree"):
        write_census_bundle(result, trial)
    with pytest.raises(UnsafeOutputPath, match="protected_root"):
        write_census_bundle(
            result, tmp_path / "june" / "o", protected_roots=[tmp_path / "june"]
        )
    other = tmp_path / "other_checkout"
    (other / ".git").mkdir(parents=True)
    with pytest.raises(UnsafeOutputPath, match="other_git_checkout"):
        write_census_bundle(result, other / "census")
    assert not list(tmp_path.glob("*.staging-*"))


# ---------------------------------------------------------------- F1 -> F2


def test_offline_acquisition_feeds_census_with_retrieval_provenance(tmp_path):
    days = SESSIONS[98:101]
    calendar_params = {"from": days[0].isoformat(), "to": days[-1].isoformat()}
    responses = {
        fixture_key(CALENDAR, calendar_params): [TransportResponse(200, json.dumps(
            {"data": [{"Date": d.isoformat(), "HolDiv": "1"} for d in days]}).encode())],
        fixture_key(MASTER, {"date": R.isoformat()}): [TransportResponse(200, json.dumps(
            {"data": [master("10010")]}).encode())],
    }  # fmt: skip
    for d in days:
        responses[fixture_key(DAILY, {"date": d.isoformat()})] = [
            TransportResponse(
                200, json.dumps({"data": [bar("10010", d, 150)]}).encode()
            )
        ]

    plan = AcquisitionPlan(
        "f1-to-f2",
        (DateQuery(CALENDAR, start=days[0].isoformat(), end=days[-1].isoformat()),
         DateQuery(MASTER, market_date=R.isoformat()),
         *(DateQuery(DAILY, market_date=d.isoformat()) for d in days)),
        AcquisitionLimits(20, 1200, 100_000, 3, 2, 13),
    )  # fmt: skip
    clock = [datetime(2026, 9, 24, tzinfo=UTC)]

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    run_offline_fixture_acquisition(
        tmp_path / "acq",
        plan,
        OfflineFixtureTransport(responses),
        clock=lambda: clock[0],
        sleep=sleep,
    )
    rows = load_completed_rows(AcquisitionStore.open(tmp_path / "acq", plan))
    census = run_census(
        request(minimum_history_sessions=3),
        CensusData(
            master_rows=rows.master,
            daily_rows=rows.daily,
            sessions=sessions_from_calendar(rows.calendar),
            acquired_daily_dates=tuple(
                date.fromisoformat(d) for d in rows.acquired_daily_dates
            ),
            acquisition_provenance=rows.provenance,
        ),
    )
    s = by_code(census)["10010"]
    assert s["c_purchasable"] is True
    assert s["d_reason"] == "indicators_not_finite_at_r"  # 3 sessions: not enough
    provenance = census.summary["acquisition_provenance"]
    assert provenance["plan_sha256"] == plan.sha256
    assert provenance["received_at_min"].startswith("2026-09-24")
    assert census.summary["market_data_period_used"]["last"] == R.isoformat()


def test_census_request_rejects_unknown_policy():
    with pytest.raises(CensusError, match="unknown_lot_policy"):
        replace(request(), lot_policy="guess")


# ---------------------------------------------------------------- review fixes


def test_every_event_on_one_row_is_recorded_independently():
    masters, rows = artificial_market()
    for row in rows:
        if row["Code"] == "10050" and row["C"] is None:
            row.update(AdjFactor=0.5)
    result = run_census(request(), data(masters, rows))
    issues = by_code(result)["10050"]["e_issues"].split(";")
    day = SESSIONS[95].isoformat()
    for kind in (
        "null_ohlc_no_trade_or_halt_cause_unknown",
        "adjusted_values_missing",
        "zero_volume",
        "adjustment_factor_not_one",
    ):
        assert f"{kind}@{day}" in issues
    summary = result.summary
    assert summary["e_rows_with_multiple_issues"] >= 1
    assert summary["e_issue_event_counts"]["adjustment_factor_not_one"] == 2
    # 10050 and 10070 (Null bars also carry Vo=0) and 10080 (prices with Vo=0)
    assert summary["e_issue_symbol_counts"]["zero_volume"] == 3
    combo = (
        "adjusted_values_missing+adjustment_factor_not_one+"
        "null_ohlc_no_trade_or_halt_cause_unknown+zero_volume"
    )
    assert summary["e_issue_combination_symbol_counts"][combo] == 1


def test_price_rows_outside_declared_acquisition_are_not_used():
    acquired = tuple(d for d in SESSIONS if d != R)
    result = run_census(request(), data(acquired=acquired))
    s = by_code(result)
    assert s["10010"]["c_purchasable"] is False
    assert s["10010"]["c_reason"] == "reference_date_not_acquired"
    assert s["10010"]["reference_close"] == ""
    consistency = result.summary["acquisition_consistency"]
    assert consistency["reference_date_acquired"] is False
    assert consistency["rows_outside_declared_acquisition"][R.isoformat()] == 8
    # declared-but-empty and non-session declarations are detected too
    extra = (*SESSIONS, date(2025, 1, 4))  # a Saturday
    masters, rows = artificial_market()
    rows = [r for r in rows if r["Date"] != SESSIONS[10].isoformat()]
    consistency = run_census(request(), data(masters, rows, acquired=extra)).summary[
        "acquisition_consistency"
    ]
    assert consistency["acquired_dates_not_sessions"] == ["2025-01-04"]
    assert SESSIONS[10].isoformat() in consistency["acquired_dates_without_rows"]


def test_owner_approved_terms_and_lot_verification_are_separate_states():
    approved = terms(status="owner_approved", approval_reference="owner-decision-1")
    assumed = run_census(request(terms=approved), data()).summary
    assert assumed["terms_approval"] == {
        "status": "owner_approved",
        "approval_reference_recorded": True,
        "authenticity_verified_by_code": False,
    }
    assert (
        assumed["lot_evidence_status"] == "includes_assumed_unknown_or_unsupported_lots"
    )
    assert assumed["result_kind"] == "reference_only_unverified_or_unknown_lots"
    assert assumed["counts"]["c_purchasable_verified_lot"] == 0
    assert assumed["counts"]["c_purchasable_assumed_lot_reference_only"] == 5
    assert assumed["claims"]["lot_evidence_source_authenticity_verified"] is False

    codes = ("10010", "10020", "10030", "10040", "10050", "10060", "10070", "10080")
    evidence = {c: LotEvidence(100, "reviewed-doc", date(2020, 1, 1)) for c in codes}
    verified = run_census(
        request(terms=approved, lot_policy="require_evidence", lot_evidence=evidence),
        data(),
    ).summary
    assert verified["result_kind"] == "census_recorded_owner_terms_and_lot_evidence"
    assert verified["counts"]["c_purchasable_verified_lot"] == 5
    assert verified["counts"]["c_purchasable_assumed_lot_reference_only"] == 0


@pytest.mark.parametrize("price", ["0.001", "0.004", "0.009"])
def test_nonpositive_rounded_unit_is_never_purchasable_and_matches_size_buy(price):
    tiny = terms(buy_price_rounding="floor", **ZERO_COST)
    result = one_lot_requirement(Decimal(price), tiny, lot_size=100)
    assert result["unit_price"] == "0.00"
    assert result["nonpositive_amount"] is True
    assert result["purchasable"] is False
    policy = AccountPolicy(
        purpose="synthetic_test", initial_capital="200000", lot_size=100,
        max_position_pct="0.1", max_positions=5, commission_rate="0",
        slippage_pct="0", reservation_buffer_pct="0", price_quantum="0.01",
        money_quantum="0.01", buy_price_rounding="floor", sell_price_rounding="floor",
        fee_rounding="ceiling", reservation_rounding="ceiling", amount_rounding="half_even",
        budget_rounding="floor", priority_mode="sell_then_score_desc_instrument",
        proceeds_mode="hold_until_later_decision_session", fill_mode="all_or_reject",
        position_mode="single_position_full_exit", expiry_mode="explicit_target_session_only",
        cost_model="proportional", dividend_policy="excluded", basis_evidence_hash="0" * 64,
    )  # fmt: skip
    assert size_buy(price, "200000", "200000", policy).shares == 0


def test_census_reason_for_nonpositive_rounded_unit():
    masters, rows = artificial_market()
    for row in rows:
        if row["Code"] == "10010" and row["Date"] == R.isoformat():
            row.update(C=0.004)
    tiny = terms(buy_price_rounding="floor", **ZERO_COST)
    s = by_code(run_census(request(terms=tiny), data(masters, rows)))["10010"]
    assert (s["c_purchasable"], s["c_reason"]) == (
        False,
        "nonpositive_rounded_unit_or_amount",
    )


# ---------------------------------------- re-review: lot evidence with A = 0

APPROVED = dict(status="owner_approved", approval_reference="owner-decision-1")
CODES = ("10010", "10020", "10030", "10040", "10050", "10060", "10070", "10080")


def test_no_target_instruments_is_never_reported_as_lot_verified():
    masters, rows = artificial_market()
    non_targets = [m for m in masters if m["Code"].startswith("2")]  # ETF, PRO Market
    summary = run_census(
        request(
            terms=terms(**APPROVED), lot_policy="require_evidence", lot_evidence={}
        ),
        data(non_targets, rows),
    ).summary
    assert summary["counts"]["a_domestic_common_stock"] == 0
    assert summary["result_kind"] == "no_target_instruments_nothing_evaluated"
    assert summary["lot_evidence_status"] == "no_target_instruments"
    assert summary["terms_approval"]["status"] == "owner_approved"
    assert summary["lot"]["verified_count"] == summary["lot"]["unverified_count"] == 0
    assert summary["counts"]["c_purchasable_verified_lot"] == 0
    assert "census_recorded" not in json.dumps(summary)
    assert "all_instruments_lot_verified" not in json.dumps(summary)


@pytest.mark.parametrize(
    ("evidence_codes", "status", "kind", "verified", "c_verified", "c_unknown"),
    [
        (CODES, "all_instruments_lot_verified",
         "census_recorded_owner_terms_and_lot_evidence", 8, 5, 0),
        (CODES[:4], "includes_assumed_unknown_or_unsupported_lots",
         "reference_only_unverified_or_unknown_lots", 4, 2, 4),
    ],
)  # fmt: skip
def test_lot_evidence_status_for_fully_and_partly_verified_targets(
    evidence_codes, status, kind, verified, c_verified, c_unknown
):
    evidence = {
        c: LotEvidence(100, "reviewed-doc", date(2020, 1, 1)) for c in evidence_codes
    }
    summary = run_census(
        request(
            terms=terms(**APPROVED),
            lot_policy="require_evidence",
            lot_evidence=evidence,
        ),
        data(),
    ).summary
    assert summary["counts"]["a_domestic_common_stock"] == 8
    assert (summary["lot_evidence_status"], summary["result_kind"]) == (status, kind)
    assert summary["lot"]["verified_count"] == verified
    assert summary["lot"]["unverified_count"] == 8 - verified
    assert summary["counts"]["c_purchasable_verified_lot"] == c_verified
    assert summary["counts"]["c_not_evaluable_lot_unknown_or_unsupported"] == c_unknown
    assert summary["counts"]["c_purchasable_assumed_lot_reference_only"] == 0
