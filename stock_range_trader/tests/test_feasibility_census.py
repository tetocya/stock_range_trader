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
    AcquisitionRunner,
    AcquisitionStore,
    DateQuery,
    TransportResponse,
    load_completed_rows,
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
    assert summary["result_kind"] == "census_under_owner_approved_terms"
    summary = run_census(request(), data()).summary
    assert (
        summary["result_kind"] == "reference_only_provisional_terms_or_unverified_lot"
    )


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
    assert counts["c_purchasable"] == 5
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

    assert "zero_volume_with_prices@" in s["10080"]["e_issues"]

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
    with pytest.raises(UnsafeOutputPath, match="protected_trial_area"):
        write_census_bundle(result, trial)
    with pytest.raises(UnsafeOutputPath, match="forbidden_root"):
        write_census_bundle(
            result, tmp_path / "june" / "o", forbidden_roots=[tmp_path / "june"]
        )
    assert not list(tmp_path.glob(".census-*"))


# ---------------------------------------------------------------- F1 -> F2


def test_offline_acquisition_feeds_census_with_retrieval_provenance(tmp_path):
    days = SESSIONS[98:101]
    responses = {
        (CALENDAR, days[0].isoformat(), None): [TransportResponse(200, json.dumps(
            {"data": [{"Date": d.isoformat(), "HolDiv": "1"} for d in days]}).encode())],
        (MASTER, R.isoformat(), None): [TransportResponse(200, json.dumps(
            {"data": [master("10010")]}).encode())],
    }  # fmt: skip
    for d in days:
        responses[(DAILY, d.isoformat(), None)] = [
            TransportResponse(
                200, json.dumps({"data": [bar("10010", d, 150)]}).encode()
            )
        ]

    class Transport:
        kind = "offline_fixture"

        def fetch(self, endpoint, params):
            key = (endpoint, params.get("date") or params.get("from"), None)
            return responses[key].pop(0)

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

    store = AcquisitionStore.create(tmp_path / "acq", plan)
    AcquisitionRunner(store, Transport(), clock=lambda: clock[0], sleep=sleep).run()
    rows = load_completed_rows(store)
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
