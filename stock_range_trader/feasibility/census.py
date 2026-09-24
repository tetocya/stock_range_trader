"""Static purchasable-universe census at one fixed reference date R (offline).

The census reports, for the domestic common-stock universe at R, which
instruments could buy one board lot under the given capital/cost terms, which
have enough indicator history, which are affected by data issues, and which
the current data-processing contracts accept. Every condition is counted on
its own and in combination; none is a sequential exclusion step, and no rule
here removes an instrument from the formal Universe.

It never estimates future executions or completed trades. "Candidate" in this
project means a strategy parameter candidate, never an instrument; this module
only speaks about instruments.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Decimal,
    InvalidOperation,
)
from pathlib import Path

import pandas as pd

from config.settings import StrategyConfig
from data.canonical import canonical_to_phase1, validate_canonical_bars
from data.price_policy import (
    UnsupportedCorporateActionError,
    validate_backtest_price_contract,
)
from data.providers.base import ProviderError
from data.providers.jquants_v2 import jquants_daily_to_canonical
from universe.japanese_equities import (
    DOMESTIC_COMMON_STOCK_PRODUCT_CODE,
    TSE_TARGET_MARKETS,
    build_japanese_equity_universe,
)

from .acquisition import canonical_json, sha256_text
from .paths import (
    UnsafeOutputPath,
    create_exclusive_dir,
    require_new_output_dir,
    write_new_file,
)

CENSUS_SCHEMA = "feasibility-static-census-v1"
ROUNDINGS = {
    "floor": ROUND_FLOOR,
    "ceiling": ROUND_CEILING,
    "half_even": ROUND_HALF_EVEN,
}
# Adopted in real_oos_protocol draft-0.3 Appendix A (not registered).
ADOPTED_CONDITIONS = {
    "initial_capital": "200000",
    "account": "single_shared_cash_account",
    "lot_size": 100,
    "candidate_reselection": "monthly_strategy_parameter_candidate",
}
LOT_POLICIES = frozenset({"require_evidence", "assume_100_unverified"})
SESSION_HOLIDAY_DIVISIONS = frozenset({"1", "2"})
ADJUSTED_FIELDS = ("AdjO", "AdjH", "AdjL", "AdjC", "AdjVo")
GAP_ISSUES = frozenset(
    {
        "session_not_acquired",
        "no_row_on_acquired_session",
        "null_ohlc_no_trade_or_halt_cause_unknown",
        "adjusted_values_missing",
    }
)
CSV_COLUMNS = (
    "reference_date",
    "jquants_code",
    "company_name",
    "market_segment_code",
    "a_domestic_common_stock",
    "yfinance_mapping_unresolved",
    "reference_close",
    "lot_status",
    "lot_size",
    "lot_source",
    "b_price_and_lot_known",
    "one_lot_required_amount",
    "position_cap_amount",
    "c_lot_basis",
    "c_purchasable",
    "c_reason",
    "complete_sessions_ending_at_r",
    "d_history_sufficient",
    "d_reason",
    "e_data_issue_affected",
    "e_issues",
    "f_current_processing_acceptable",
    "f_reason",
    "f_notes",
)


class CensusError(ValueError):
    """Invalid census inputs or terms (never an instrument-level outcome)."""


def dec(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise CensusError(f"{name}_must_be_numeric_text")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise CensusError(f"{name}_must_be_numeric_text") from error
    if not result.is_finite():
        raise CensusError(f"{name}_must_be_finite")
    return result


def rounded(value: Decimal, quantum: Decimal, mode: str) -> Decimal:
    return (value / quantum).to_integral_value(rounding=ROUNDINGS[mode]) * quantum


@dataclass(frozen=True)
class CensusTerms:
    """Capital and cost settings; ``status`` separates provisional from approved."""

    status: str
    approval_reference: str | None
    max_position_pct: str
    max_positions: int
    commission_rate: str
    slippage_pct: str
    reservation_buffer_pct: str
    price_quantum: str
    money_quantum: str
    buy_price_rounding: str
    amount_rounding: str
    fee_rounding: str
    reservation_rounding: str
    budget_rounding: str
    initial_capital: str = ADOPTED_CONDITIONS["initial_capital"]
    lot_size: int = ADOPTED_CONDITIONS["lot_size"]

    def __post_init__(self) -> None:
        if self.status == "provisional":
            if self.approval_reference is not None:
                raise CensusError("provisional_terms_have_no_approval_reference")
        elif self.status == "owner_approved":
            if (
                not isinstance(self.approval_reference, str)
                or not self.approval_reference.strip()
            ):
                raise CensusError("approved_terms_require_approval_reference")
        else:
            raise CensusError("unknown_terms_status")
        if self.initial_capital != ADOPTED_CONDITIONS["initial_capital"]:
            raise CensusError("initial_capital_is_protocol_adopted_200000")
        if self.lot_size != ADOPTED_CONDITIONS["lot_size"]:
            raise CensusError("lot_size_is_protocol_adopted_100")
        if type(self.max_positions) is not int or self.max_positions < 1:
            raise CensusError("max_positions_must_be_positive_integer")
        if not 0 < dec(self.max_position_pct, "max_position_pct") <= 1:
            raise CensusError("max_position_pct_out_of_range")
        for name in ("commission_rate", "slippage_pct", "reservation_buffer_pct"):
            if not 0 <= dec(getattr(self, name), name) < 1:
                raise CensusError(f"{name}_out_of_range")
        for name in ("price_quantum", "money_quantum"):
            if dec(getattr(self, name), name) <= 0:
                raise CensusError(f"{name}_must_be_positive")
        for name in (
            "buy_price_rounding",
            "amount_rounding",
            "fee_rounding",
            "reservation_rounding",
            "budget_rounding",
        ):
            if getattr(self, name) not in ROUNDINGS:
                raise CensusError(f"unknown_{name}")


def one_lot_requirement(
    reference_close: Decimal, terms: CensusTerms, *, lot_size: int
) -> dict[str, str | bool]:
    """Fixed calculation order, identical to the delayed-replay ``size_buy`` for one lot.

    1. cap = round(initial_capital x max_position_pct, money_quantum, budget_rounding),
       bounded by available cash (= initial capital at R, no positions)
    2. unit = round(reference_close x (1 + slippage) x (1 + buffer),
       price_quantum, buy_price_rounding)
    3. gross = round(unit x lot_size, money_quantum, amount_rounding)
    4. fee = round(gross x commission_rate, money_quantum, fee_rounding)
    5. required = round(gross + fee, money_quantum, reservation_rounding)
    6. purchasable iff 0 < unit, 0 < required and required <= cap
       (``size_buy`` returns zero shares for a non-positive unit or amount)
    """

    money_q = dec(terms.money_quantum, "money_quantum")
    capital = dec(terms.initial_capital, "initial_capital")
    budget = rounded(
        capital * dec(terms.max_position_pct, "max_position_pct"),
        money_q,
        terms.budget_rounding,
    )
    cap = min(budget, capital)
    unit = rounded(
        reference_close
        * (1 + dec(terms.slippage_pct, "slippage_pct"))
        * (1 + dec(terms.reservation_buffer_pct, "reservation_buffer_pct")),
        dec(terms.price_quantum, "price_quantum"),
        terms.buy_price_rounding,
    )
    gross = rounded(unit * lot_size, money_q, terms.amount_rounding)
    fee = rounded(
        gross * dec(terms.commission_rate, "commission_rate"),
        money_q,
        terms.fee_rounding,
    )
    required = rounded(gross + fee, money_q, terms.reservation_rounding)
    positive = unit > 0 and gross > 0 and required > 0
    return {
        "reference_close": format(reference_close, "f"),
        "unit_price": format(unit, "f"),
        "gross": format(gross, "f"),
        "fee": format(fee, "f"),
        "required": format(required, "f"),
        "cap": format(cap, "f"),
        "nonpositive_amount": not positive,
        "purchasable": positive and required <= cap,
    }


@dataclass(frozen=True)
class LotEvidence:
    """A reviewed trading-unit source valid for ``[valid_from, valid_to)``."""

    lot_size: int
    source_id: str
    valid_from: date
    valid_to: date | None = None

    def covers(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day < self.valid_to)


@dataclass(frozen=True)
class ViewedPeriod:
    """A market period whose outcomes were already seen, ``[start, end)``."""

    label: str
    start: date
    end: date


@dataclass(frozen=True)
class CensusRequest:
    reference_date: date
    terms: CensusTerms
    strategy_config: StrategyConfig
    minimum_history_sessions: int
    lot_policy: str
    lot_evidence: Mapping[str, LotEvidence] = field(default_factory=dict)
    viewed_periods: tuple[ViewedPeriod, ...] = ()
    reconstruction_assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.reference_date) is not date:
            raise CensusError("reference_date_must_be_date")
        if (
            type(self.minimum_history_sessions) is not int
            or self.minimum_history_sessions < 2
        ):
            raise CensusError("minimum_history_sessions_must_be_integer_at_least_2")
        if self.lot_policy not in LOT_POLICIES:
            raise CensusError("unknown_lot_policy")


@dataclass(frozen=True)
class CensusData:
    master_rows: tuple[dict, ...]
    daily_rows: tuple[dict, ...]
    sessions: tuple[date, ...]
    acquired_daily_dates: tuple[date, ...]
    acquisition_provenance: Mapping[str, object]


@dataclass(frozen=True)
class CensusResult:
    symbols: tuple[dict, ...]
    summary: dict


def sessions_from_calendar(rows: Iterable[Mapping[str, object]]) -> tuple[date, ...]:
    """Tokyo Stock Exchange sessions: HolDiv 1 (business day) and 2 (half day)."""

    return tuple(
        sorted(
            date.fromisoformat(str(row["Date"]))
            for row in rows
            if str(row["HolDiv"]) in SESSION_HOLIDAY_DIVISIONS
        )
    )


def _strip(row: Mapping[str, object]) -> dict:
    return {k: v for k, v in row.items() if not str(k).startswith("_")}


def _numeric(row: Mapping[str, object], name: str) -> Decimal | None:
    value = row.get(name)
    return None if value is None else dec(value, name)


def run_census(request: CensusRequest, data: CensusData) -> CensusResult:
    r = request.reference_date
    sessions = tuple(sorted(set(data.sessions)))
    if r not in sessions:
        raise CensusError("reference_date_is_not_a_session")
    master = pd.DataFrame.from_records([_strip(row) for row in data.master_rows])
    universe = build_japanese_equity_universe(master, as_of_date=r)

    acquired = set(data.acquired_daily_dates)
    used: dict[tuple[str, date], dict] = {}
    ignored_future_rows = 0
    undeclared: Counter = Counter()
    for row in data.daily_rows:
        day = date.fromisoformat(str(row["Date"]))
        if day > r:
            ignored_future_rows += 1
            continue
        if day not in acquired:
            # A row whose date is not declared as acquired has no provenance: unused.
            undeclared[day.isoformat()] += 1
            continue
        key = (str(row["Code"]).strip().upper(), day)
        if key in used:
            raise CensusError("duplicate_daily_row")
        used[key] = row
    consistency = {
        "reference_date_acquired": r in acquired,
        "rows_outside_declared_acquisition": dict(sorted(undeclared.items())),
        "acquired_dates_not_sessions": sorted(
            d.isoformat() for d in acquired if d not in set(sessions)
        ),
        "acquired_dates_without_rows": sorted(
            d.isoformat()
            for d in acquired
            if d <= r and not any(day == d for _, day in used)
        ),
    }
    upto_r = [s for s in sessions if s <= r]
    window = upto_r[-request.minimum_history_sessions :]
    window_short = len(window) < request.minimum_history_sessions

    symbols = []
    for _, member in universe.iterrows():
        code = str(member["jquants_code"])
        in_a = str(
            member["product_category"]
        ) == DOMESTIC_COMMON_STOCK_PRODUCT_CODE and (
            str(member["market_segment_code"]) in TSE_TARGET_MARKETS
        )
        if not in_a:
            continue
        symbols.append(
            _evaluate_symbol(
                request, member, code, used, acquired, upto_r, window, window_short
            )
        )
    summary = _summarize(request, data, symbols, used, ignored_future_rows, window)
    summary["acquisition_consistency"] = consistency
    return CensusResult(symbols=tuple(symbols), summary=summary)


def _evaluate_symbol(request, member, code, used, acquired, upto_r, window, short):
    r = request.reference_date
    terms = request.terms
    record: dict[str, object] = {
        "reference_date": r.isoformat(),
        "jquants_code": code,
        "company_name": str(member["company_name"]),
        "market_segment_code": str(member["market_segment_code"]),
        "a_domestic_common_stock": True,
        "yfinance_mapping_unresolved": str(member["exclusion_reason"]).startswith(
            "unresolved_symbol:"
        ),
    }
    row_r = used.get((code, r))
    close = _numeric(row_r, "C") if row_r is not None else None
    price_observed = close is not None and close > 0
    record["reference_close"] = format(close, "f") if price_observed else ""

    evidence = request.lot_evidence.get(code)
    if evidence is not None and evidence.covers(r):
        lot_status = "verified" if evidence.lot_size == 100 else "unsupported_lot_size"
        lot_size, lot_source = evidence.lot_size, evidence.source_id
    elif request.lot_policy == "assume_100_unverified":
        lot_status, lot_size, lot_source = "assumed_100_unverified", 100, "assumption"
    else:
        lot_status, lot_size, lot_source = "unknown", None, ""
    record.update(lot_status=lot_status, lot_size=lot_size or "", lot_source=lot_source)
    lot_usable = lot_status in ("verified", "assumed_100_unverified")
    record["b_price_and_lot_known"] = price_observed and lot_usable

    lot_basis = ""
    if r not in acquired:
        c_ok, c_reason, required, cap = False, "reference_date_not_acquired", "", ""
    elif not price_observed:
        c_ok, c_reason, required, cap = False, "price_not_observed_at_r", "", ""
    elif lot_status == "unknown":
        c_ok, c_reason, required, cap = False, "lot_size_unknown", "", ""
    elif lot_status == "unsupported_lot_size":
        c_ok, c_reason, required, cap = False, "lot_size_not_100_unsupported", "", ""
    else:
        cost = one_lot_requirement(close, terms, lot_size=terms.lot_size)
        c_ok = bool(cost["purchasable"])
        if c_ok:
            c_reason = ""
        elif cost["nonpositive_amount"]:
            c_reason = "nonpositive_rounded_unit_or_amount"
        else:
            c_reason = "one_lot_exceeds_position_cap"
        required, cap = cost["required"], cost["cap"]
        lot_basis = (
            "verified_lot" if lot_status == "verified" else "assumed_lot_unverified"
        )
    record.update(
        c_lot_basis=lot_basis,
        one_lot_required_amount=required,
        position_cap_amount=cap,
        c_purchasable=c_ok,
        c_reason=c_reason,
    )

    issues = []
    for day in window:
        row = used.get((code, day))
        if day not in acquired:
            issues.append(("session_not_acquired", day))
            continue
        if row is None:
            issues.append(("no_row_on_acquired_session", day))
            continue
        if any(row.get(f) is None for f in ("O", "H", "L", "C")):
            issues.append(("null_ohlc_no_trade_or_halt_cause_unknown", day))
        if any(row.get(f) is None for f in ADJUSTED_FIELDS):
            issues.append(("adjusted_values_missing", day))
        volume = _numeric(row, "Vo")
        if volume is None:
            issues.append(("volume_missing", day))
        elif volume == 0:
            issues.append(("zero_volume", day))
        factor = _numeric(row, "AdjFactor")
        if factor is None:
            issues.append(("adjustment_factor_missing", day))
        elif factor != 1:
            issues.append(("adjustment_factor_not_one", day))
        if row.get("ExRT") not in (None, "", "0"):
            issues.append(("ex_rights_flag", day))
    record["e_data_issue_affected"] = bool(issues)
    record["e_issues"] = ";".join(f"{kind}@{day.isoformat()}" for kind, day in issues)

    complete = 0
    for day in reversed(upto_r):
        row = used.get((code, day))
        if (
            row is None
            or day not in acquired
            or any(row.get(f) is None for f in ("O", "H", "L", "C", *ADJUSTED_FIELDS))
        ):
            break
        complete += 1
    record["complete_sessions_ending_at_r"] = complete
    if short:
        d_ok, d_reason = False, "calendar_history_shorter_than_requirement"
    elif any(kind in GAP_ISSUES for kind, _ in issues):
        d_ok, d_reason = False, "history_window_incomplete"
    else:
        d_ok, d_reason = _history_finite(
            request, code, used, upto_r[len(upto_r) - complete :]
        )
    record.update(d_history_sufficient=d_ok, d_reason=d_reason)

    f_ok, f_reason, f_notes = _current_processing(code, used, window)
    if f_ok and any(kind in GAP_ISSUES for kind, _ in issues):
        f_notes = "session_gaps_not_detected_by_current_processing"
    record.update(
        f_current_processing_acceptable=f_ok, f_reason=f_reason, f_notes=f_notes
    )
    return record


def _history_finite(request, code, used, days) -> tuple[bool, str]:
    rows = [used[(code, day)] for day in days]
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime([day.isoformat() for day in days]),
            "open": [float(_numeric(r, "AdjO")) for r in rows],
            "high": [float(_numeric(r, "AdjH")) for r in rows],
            "low": [float(_numeric(r, "AdjL")) for r in rows],
            "close": [float(_numeric(r, "AdjC")) for r in rows],
            "volume": [float(_numeric(r, "AdjVo")) for r in rows],
        }
    )
    frame["turnover_value"] = frame["close"] * frame["volume"]
    config = request.strategy_config
    try:
        features = config.create_scorer().transform(
            config.create_detector().transform(frame)
        )
    except ValueError:
        return False, "adjusted_ohlcv_rejected_by_indicator_validation"
    last = features.iloc[-1]
    if all(math.isfinite(float(last[c])) for c in ("sma", "atr", "adx", "range_score")):
        return True, ""
    return False, "indicators_not_finite_at_r"


def _current_processing(code, used, window) -> tuple[bool, str, str]:
    records = [_strip(used[(code, day)]) for day in window if (code, day) in used]
    if not records:
        return False, "no_rows_in_window", ""
    try:
        # fetched_at is metadata only; it does not affect these validators.
        frame = jquants_daily_to_canonical(
            records, fetched_at=datetime(1970, 1, 1, tzinfo=UTC)
        )
        validate_canonical_bars(
            frame, expected_provider="jquants", requested_symbols={code}
        )
    except (ValueError, ProviderError) as error:
        return False, f"canonical_rejected:{type(error).__name__}", ""
    try:
        validate_backtest_price_contract(canonical_to_phase1(frame, symbol=code))
    except UnsupportedCorporateActionError:
        return False, "executable_contract_unsupported_corporate_action", ""
    except ValueError as error:
        return False, f"phase1_adapter_rejected:{type(error).__name__}", ""
    return True, "", ""


def _flag_pattern(record) -> str:
    return "".join(
        f"{name}{int(bool(record[key]))}"
        for name, key in (
            ("C", "c_purchasable"),
            ("D", "d_history_sufficient"),
            ("E", "e_data_issue_affected"),
            ("F", "f_current_processing_acceptable"),
        )
    )


def _rows_digest(rows: Iterable[Mapping[str, object]]) -> str:
    ordered = sorted(
        (canonical_json(_strip(row)) for row in rows),
    )
    return sha256_text(canonical_json(ordered))


def _lot_evidence_status(symbols) -> str:
    verified = sum(1 for s in symbols if s["lot_status"] == "verified")
    if not symbols:
        return "no_target_instruments"
    if verified == len(symbols):
        return "all_instruments_lot_verified"
    return "includes_assumed_unknown_or_unsupported_lots"


def _summarize(request, data, symbols, used, ignored_future_rows, window) -> dict:
    count = len(symbols)

    def n(predicate) -> int:
        return sum(1 for s in symbols if predicate(s))

    issue_symbols: Counter = Counter()
    issue_events: Counter = Counter()
    combinations: Counter = Counter()
    multi_issue_rows = 0
    for s in symbols:
        items = [item.split("@") for item in s["e_issues"].split(";") if item]
        kinds = [kind for kind, _ in items]
        issue_events.update(kinds)
        issue_symbols.update(set(kinds))
        if kinds:
            combinations["+".join(sorted(set(kinds)))] += 1
        per_day = Counter(day for _, day in items)
        multi_issue_rows += sum(1 for count in per_day.values() if count > 1)
    used_dates = sorted({day for _, day in used})
    period_start = window[0] if window else None
    overlaps = []
    for viewed in request.viewed_periods:
        if period_start is None:
            break
        start = max(viewed.start, period_start)
        end = min(viewed.end, request.reference_date + timedelta(days=1))
        if start < end:
            overlaps.append(
                {
                    "label": viewed.label,
                    "overlap_start": start.isoformat(),
                    "overlap_end_exclusive": end.isoformat(),
                }
            )
    lot_sources = sorted({s["lot_source"] for s in symbols if s["lot_source"]})
    terms = asdict(request.terms)
    # No target instruments means nothing was evaluated: never a positive verdict
    # from an empty any()/all(); terms status is still reported separately.
    if count == 0:
        result_kind = "no_target_instruments_nothing_evaluated"
    elif request.terms.status != "owner_approved":
        result_kind = "reference_only_provisional_terms"
    elif any(s["lot_status"] != "verified" for s in symbols):
        result_kind = "reference_only_unverified_or_unknown_lots"
    else:
        result_kind = "census_recorded_owner_terms_and_lot_evidence"
    pairwise = {
        "c_and_d": n(lambda s: s["c_purchasable"] and s["d_history_sufficient"]),
        "c_and_not_d": n(
            lambda s: s["c_purchasable"] and not s["d_history_sufficient"]
        ),
        "not_c_and_d": n(
            lambda s: not s["c_purchasable"] and s["d_history_sufficient"]
        ),
        "cost_cap_exceeded_and_not_d": n(
            lambda s: (
                s["c_reason"] == "one_lot_exceeds_position_cap"
                and not s["d_history_sufficient"]
            )
        ),
        "c_and_e": n(lambda s: s["c_purchasable"] and s["e_data_issue_affected"]),
        "c_and_d_and_f": n(
            lambda s: (
                s["c_purchasable"]
                and s["d_history_sufficient"]
                and s["f_current_processing_acceptable"]
            )
        ),
        "d_and_not_f": n(
            lambda s: (
                s["d_history_sufficient"] and not s["f_current_processing_acceptable"]
            )
        ),
    }
    return {
        "schema": CENSUS_SCHEMA,
        "result_kind": result_kind,
        "terms_approval": {
            "status": request.terms.status,
            "approval_reference_recorded": request.terms.approval_reference is not None,
            "authenticity_verified_by_code": False,
        },
        "lot_evidence_status": _lot_evidence_status(symbols),
        "claims": {
            "estimates_future_executions_or_trades": False,
            "real_oos_registration": False,
            "historical_snapshot_equals_contemporaneous_delivery": False,
            "adjusted_series_basis": "provider_adjusted_as_of_retrieval",
            "instrument_universe_exclusion_rule_added": False,
            "candidate_means": "strategy_parameter_candidate_not_instrument",
            "owner_approval_authenticity_verified": False,
            "lot_evidence_source_authenticity_verified": False,
        },
        "reference_date": request.reference_date.isoformat(),
        "history_window": {
            "minimum_sessions": request.minimum_history_sessions,
            "first_session": period_start.isoformat() if period_start else None,
            "last_session": window[-1].isoformat() if window else None,
        },
        "market_data_period_used": {
            "first": used_dates[0].isoformat() if used_dates else None,
            "last": used_dates[-1].isoformat() if used_dates else None,
        },
        "future_rows_supplied_and_ignored": ignored_future_rows,
        "acquisition_provenance": dict(data.acquisition_provenance),
        "input_hashes": {
            "master_rows_sha256": _rows_digest(data.master_rows),
            "daily_rows_used_sha256": _rows_digest(used.values()),
            "sessions_sha256": sha256_text(
                canonical_json([d.isoformat() for d in sorted(set(data.sessions))])
            ),
            "lot_evidence_sha256": sha256_text(
                canonical_json(
                    {
                        code: {
                            **asdict(ev),
                            "valid_from": ev.valid_from.isoformat(),
                            "valid_to": ev.valid_to.isoformat()
                            if ev.valid_to
                            else None,
                        }
                        for code, ev in sorted(request.lot_evidence.items())
                    }
                )
            ),
        },
        "lot": {
            "policy": request.lot_policy,
            "sources": lot_sources,
            "status_counts": dict(Counter(s["lot_status"] for s in symbols)),
            "verified_count": n(lambda s: s["lot_status"] == "verified"),
            "unverified_count": n(lambda s: s["lot_status"] != "verified"),
        },
        "conditions": {
            "adopted_in_protocol_draft_0_3": ADOPTED_CONDITIONS,
            "evaluated_provisional": {
                "max_position_pct": request.terms.max_position_pct,
                "max_positions": request.terms.max_positions,
            },
            "terms": terms,
            "strategy_config_for_history": asdict(request.strategy_config),
        },
        "reconstruction_assumptions": list(request.reconstruction_assumptions),
        "viewed_period_overlaps": overlaps,
        "counts": {
            "a_domestic_common_stock": count,
            "b_price_and_lot_known": n(lambda s: s["b_price_and_lot_known"]),
            "c_purchasable_verified_lot": n(
                lambda s: s["c_purchasable"] and s["c_lot_basis"] == "verified_lot"
            ),
            "c_purchasable_assumed_lot_reference_only": n(
                lambda s: (
                    s["c_purchasable"] and s["c_lot_basis"] == "assumed_lot_unverified"
                )
            ),
            "c_not_evaluable_lot_unknown_or_unsupported": n(
                lambda s: s["lot_status"] in ("unknown", "unsupported_lot_size")
            ),
            "d_history_sufficient": n(lambda s: s["d_history_sufficient"]),
            "e_data_issue_affected": n(lambda s: s["e_data_issue_affected"]),
            "f_current_processing_acceptable": n(
                lambda s: s["f_current_processing_acceptable"]
            ),
        },
        "reasons": {
            "c": dict(Counter(s["c_reason"] for s in symbols if s["c_reason"])),
            "d": dict(Counter(s["d_reason"] for s in symbols if s["d_reason"])),
            "f": dict(Counter(s["f_reason"] for s in symbols if s["f_reason"])),
            "f_notes": dict(Counter(s["f_notes"] for s in symbols if s["f_notes"])),
        },
        "e_issue_symbol_counts": dict(issue_symbols),
        "e_issue_event_counts": dict(issue_events),
        "e_issue_combination_symbol_counts": dict(combinations),
        "e_rows_with_multiple_issues": multi_issue_rows,
        "overlaps": {
            "basis": "c_purchasable on each instrument's c_lot_basis",
            "pairwise": pairwise,
            "cdef_pattern_counts": dict(Counter(_flag_pattern(s) for s in symbols)),
        },
    }


def write_census_bundle(
    result: CensusResult,
    output_dir: str | Path,
    *,
    allowed_roots=None,
    protected_roots=(),
) -> Path:
    """Publish ``census_symbols.csv`` and ``census_summary.json`` into a new dir.

    The target and a sibling staging directory must pass the allowlist checks in
    ``feasibility.paths``; the staging directory is renamed only if the target
    still does not exist (a residual race with a concurrent writer is possible).
    """

    location = {"allowed_roots": allowed_roots, "protected_roots": protected_roots}
    target = require_new_output_dir(output_dir, **location)
    staging = create_exclusive_dir(
        target.parent / f"{target.name}.staging-{uuid.uuid4().hex}", **location
    )
    try:
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(result.symbols)
        write_new_file(
            staging / "census_symbols.csv", buffer.getvalue().encode("utf-8")
        )
        summary = json.dumps(
            result.summary, ensure_ascii=False, indent=2, sort_keys=True
        )
        write_new_file(
            staging / "census_summary.json", (summary + "\n").encode("utf-8")
        )
        if os.path.lexists(target):
            raise UnsafeOutputPath("output_already_exists")
        os.rename(staging, target)
    except BaseException:
        for child in staging.glob("*"):
            child.unlink()
        staging.rmdir()
        raise
    return target
