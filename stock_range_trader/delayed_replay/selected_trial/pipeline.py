"""Scope-first selection and evidence-preserving input construction. No trading."""

import hashlib
from datetime import date, timedelta
from pathlib import Path

from data.price_policy import provider_price_basis
from delayed_replay.daily_evidence import (
    JQUANTS_RESPONSE_SCHEMA,
    adapt_daily_response,
    response_payload,
)
from delayed_replay.input_artifacts import InputArtifactStore, InputPacket
from delayed_replay.live_probe import save_capture
from delayed_replay.market_view import MarketView
from delayed_replay.reference_evidence import LotEvidence, ReferenceReview
from delayed_replay.serialization import JsonObject, digest, parse_time
from delayed_replay.sizing import size_buy
from delayed_replay.snapshot import PriceObservation, PriceSnapshot

from .acquisition import AcquisitionStopped, stage_a, stage_b
from .contract import SYMBOLS, SelectedTrialPlan


def save(root, value):
    payload = JsonObject.from_value(value)
    save_capture(Path(root), payload)
    return payload.sha256


def read(root, sha):
    from delayed_replay.limited_trial.inputs import capture

    return capture(Path(root), sha)


def lot_review(root, sha, symbol):
    r = read(root, sha)
    if r["schema"] != "selected-lot-review-v1" or r["instrument"] != symbol:
        raise AcquisitionStopped("lot_review_scope")
    if not r["review_reference"] or not r["documents"]:
        raise AcquisitionStopped("lot_evidence_missing")
    for doc in r["documents"]:
        path = Path(root) / (doc["sha256"] + ".pdf")
        if (
            path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]
        ):
            raise AcquisitionStopped("lot_document_corrupt")
    lot = LotEvidence(
        symbol,
        date.fromisoformat(r["start"]),
        date.fromisoformat(r["end"]),
        r["lot_size"],
        ReferenceReview(r["source"], r["subject_hash"], sha),
    )
    for day in (date(2026, 4, 30), date(2026, 5, 31)):
        lot.require(symbol, day)
    return dict(
        instrument=symbol,
        start=r["start"],
        end=r["end"],
        lot_size=100,
        source=r["source"],
        subject_hash=lot.subject_hash,
        review_hash=sha,
    )


def sessions(rows, start, end):
    expected = {
        (date.fromisoformat(start) + timedelta(days=i)).isoformat()
        for i in range((date.fromisoformat(end) - date.fromisoformat(start)).days)
    }
    if (
        len(rows) != len(expected)
        or {r.get("Date") for r in rows} != expected
        or any(r.get("HolDiv") not in ("0", "1", "2", "3") for r in rows)
    ):
        raise AcquisitionStopped("calendar_incomplete_or_invalid")
    return tuple(sorted(r["Date"] for r in rows if r["HolDiv"] in ("1", "2")))


def daily(capture, symbol, allowed_dates):
    result = {}
    wall = parse_time(capture["fetched_at"])
    for row in capture["data"]:
        day = row.get("Date")
        if day not in allowed_dates or day in result:
            raise AcquisitionStopped("daily_scope_duplicate_or_calendar")
        item = adapt_daily_response(
            row,
            provider="jquants",
            basis=provider_price_basis("jquants"),
            response_schema=JQUANTS_RESPONSE_SCHEMA,
            symbol=symbol,
            session=date.fromisoformat(day),
            first_observed_at=wall,
            fetched_at=wall,
            snapshot_hash=response_payload(row).sha256,
        )
        if item.to_dict()["quality"] not in ("reported", "no_trade_reported"):
            raise AcquisitionStopped("daily_" + item.to_dict()["quality"])
        result[day] = item.to_dict()
    if set(result) != set(allowed_dates):
        raise AcquisitionStopped("daily_missing_sessions")
    return result


def select(plan, captures, evidence_root):
    queries = stage_a()
    if set(captures) != {digest(q) for q in queries}:
        raise AcquisitionStopped("selection_requires_all_stage_a")
    cal = captures[digest(queries[0])]
    days = sessions(cal["data"], "2026-04-30", "2026-05-01")
    if days != ("2026-04-30",):
        raise AcquisitionStopped("reference_date_not_observed_session")
    observations = []
    for symbol in SYMBOLS:
        lot_review(evidence_root, plan.payload.to_dict()["lot_reviews"][symbol], symbol)
        master_query, price_query = [
            q for q in queries if q["params"].get("code") == symbol
        ]
        master = captures[digest(master_query)]["data"]
        if (
            len(master) != 1
            or master[0].get("Code") != symbol
            or master[0].get("Date") != "2026-04-30"
        ):
            raise AcquisitionStopped("reference_master_mismatch")
        # Existing canonical master product codes: require an expressly identified ordinary share.
        if master[0].get("ProdCat") != "011":
            raise AcquisitionStopped("ordinary_share_product_unverified")
        bar = daily(captures[digest(price_query)], symbol, days)[days[0]]
        if bar["quality"] != "reported":
            raise AcquisitionStopped("reference_not_traded")
        q = size_buy(bar["values"][3], "200000", "200000", plan.policy)
        observations.append(
            dict(
                symbol=symbol,
                reference_date=days[0],
                reference_field="raw_close",
                reference_price=bar["values"][3],
                shares=q.shares,
                budget=q.frozen_budget,
                reserved_cash=q.reserved_cash,
                price_capture_hash=digest(captures[digest(price_query)]),
            )
        )
    eligible = [r["symbol"] for r in observations if r["shares"] >= 100]
    if not eligible:
        raise AcquisitionStopped("no_affordable_representative")
    return dict(
        schema="selected-representative-v1",
        acquisition_hash=plan.sha256,
        symbol=min(eligible),
        rule="raw_close_affordability_then_code_ascending",
        observations=observations,
        input_hashes=sorted(digest(v) for v in captures.values()),
    )


def acquire(transport, evidence_root):
    plan = transport.receipt.plan
    a = {digest(q): transport.fetch(q) for q in stage_a()}
    selected = select(plan, a, evidence_root)
    transport.receipt.freeze_selection(selected)
    symbol = selected["symbol"]
    qs = stage_b(symbol)
    b = {}
    b[digest(qs[0])] = transport.fetch(qs[0])
    dates = sessions(b[digest(qs[0])]["data"], "2026-01-01", "2026-06-01")
    if a[digest(stage_a()[0])]["data"][0] not in b[digest(qs[0])]["data"]:
        raise AcquisitionStopped("calendar_revision")
    b[digest(qs[1])] = transport.fetch(qs[1])
    master = b[digest(qs[1])]["data"]
    if (
        len(master) != 1
        or master[0].get("Code") != symbol
        or master[0].get("Date") != "2026-05-01"
        or master[0].get("ProdCat") != "011"
    ):
        raise AcquisitionStopped("run_master_mismatch")
    b[digest(qs[2])] = transport.fetch(qs[2])
    hist = daily(b[digest(qs[2])], symbol, tuple(d for d in dates if d < "2026-04-30"))
    if len(hist) + 1 < 78:
        raise AcquisitionStopped("insufficient_history")
    b[digest(qs[3])] = transport.fetch(qs[3])
    daily(b[digest(qs[3])], symbol, tuple(d for d in dates if d >= "2026-05-01"))
    return selected, a, b


def build_inputs(plan, selection, a, b, root, *, artificial=False):
    """Keep actual retrieval dates; never relabel daily values as auction evidence."""
    from delayed_replay.limited_trial.models import SCOPE

    root = Path(root)
    p = plan.payload.to_dict()
    symbol = selection["symbol"]
    if select(plan, a, root) != selection:
        raise AcquisitionStopped("selection_binding_changed")
    qs = stage_b(symbol)
    calendar, master, history, run = [b[digest(q)] for q in qs]
    all_days = sessions(calendar["data"], "2026-01-01", "2026-06-01")
    original_cal = a[digest(stage_a()[0])]["data"][0]
    if original_cal not in calendar["data"]:
        raise AcquisitionStopped("calendar_revision")
    hist_dates = tuple(d for d in all_days if d < "2026-04-30")
    run_dates = tuple(d for d in all_days if d >= "2026-05-01")
    reference = next(
        v
        for v in a.values()
        if v["query"]["path"].endswith("bars/daily")
        and v["query"]["params"].get("code") == symbol
    )
    historical = daily(history, symbol, hist_dates)
    ref = daily(reference, symbol, ("2026-04-30",))
    reported = daily(run, symbol, run_dates)
    if len(historical) + 1 < 78:
        raise AcquisitionStopped("insufficient_history")
    if len(run_dates) != 18:
        raise AcquisitionStopped("unexpected_run_session_count")
    if (
        len(master["data"]) != 1
        or master["data"][0].get("Code") != symbol
        or master["data"][0].get("Date") != "2026-05-01"
    ):
        raise AcquisitionStopped("run_master_mismatch")
    binding = dict(
        acquisition=plan.payload.to_dict(),
        selection=selection,
        stage_a=a,
        stage_b=b,
        origin="generated_selected_v1" if artificial else "saved_jquants",
    )
    binding_sha = save(root, binding)
    # Persist direct source envelopes and derived canonical evidence independently.
    source = dict(data=run["data"])
    if artificial:
        source.update(
            fixture_generator="limited-artificial-v1", fixture_sessions=list(all_days)
        )
    source_sha = save(root, source)
    daily_sha = save(
        root, dict(schema="stage7b-daily-capture-1", data=list(reported.values()))
    )
    master_sha = save(
        root, dict(schema="stage7b-master-capture-1", data=master["data"])
    )
    cal_sha = save(
        root,
        dict(
            schema="stage7b-calendar-capture-1",
            data=[r for r in calendar["data"] if r["Date"] >= "2026-05-01"],
        ),
    )
    store = InputArtifactStore(root / "inputs")

    def packet(values, source_hash, stamp):
        wall = parse_time(stamp)
        obs = tuple(
            PriceObservation(
                symbol,
                date.fromisoformat(d),
                wall,
                tuple(map(float, r["values"][:5])),
                tuple(map(float, r["values"][5:10])),
                float(r["values"][10]),
                0.0,
                0.0,
            )
            for d, r in sorted(values.items())
        )
        snapshot = PriceSnapshot.create(
            provider="jquants",
            provider_price_basis=provider_price_basis("jquants"),
            source_artifact_sha256=source_hash,
            data_version="selected-research-v1",
            first_observed_at=wall,
            fetched_at=wall,
            provider_published_at=None,
            publication_time_unknown_reason="historical_publication_not_observed",
            price_basis_evidence_id="jquants_reported_separate_adjusted",
            observations=obs,
        )
        return store.publish(InputPacket(MarketView((snapshot,), ())))

    histories = [
        packet(historical, save(root, history), history["fetched_at"]),
        packet(ref, save(root, reference), reference["fetched_at"]),
    ]
    parts = [
        packet({d: reported[d] for d in ds}, source_sha, run["fetched_at"])
        for ds in (run_dates[:9], run_dates[9:])
    ]
    result = dict(
        schema="selected-trial-plan-v1",
        scope=dict(
            SCOPE,
            symbol=symbol,
            acquisition_hash=plan.sha256,
            selection_hash=digest(selection),
        ),
        model_hash=p["model_hash"],
        source_identity="selected-binding:" + binding_sha,
        provenance="artificial_fixture" if artificial else "saved_jquants",
        packets=parts,
        history_packets=histories,
        captures=dict(
            master=master_sha,
            daily_source=source_sha,
            daily_capture=daily_sha,
            calendar=cal_sha,
        ),
        references=dict(
            lot=lot_review(root, p["lot_reviews"][symbol], symbol),
            price=dict(
                basis=provider_price_basis("jquants"),
                volume_unit="execution_shares",
                review_scope="provider_contract_and_saved_capture_only",
            ),
            halt=dict(coverage="unknown", observations={}),
            external_price=None,
        ),
        terms=p["terms"],
        rules=p["rules"],
        strategy=p["strategy"],
        candidates=[p["candidate"]],
        candidate_id="baseline",
        candidate_supply="predeclared_single_candidate",
        monthly_reference=dict(
            lookback_months=None,
            warmup_months=None,
            minimum_warmup_sessions=None,
            status="not_selected_for_this_proposal",
        ),
        repetition="continuous_and_split_resume_same_plan",
    )
    from delayed_replay.proxy.policy import ASSUMPTIONS

    result["assumptions"] = list(ASSUMPTIONS)
    return SelectedTrialPlan(JsonObject.from_value(result))
