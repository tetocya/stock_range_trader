"""June-only preparation and bounded acquisition. No clearing/registration entry."""

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from data.price_policy import provider_price_basis
from delayed_replay.daily_evidence import DAILY_FIELDS, numeric
from delayed_replay.input_artifacts import InputArtifactStore, InputPacket
from delayed_replay.limited_trial.models import implementation_hash
from delayed_replay.live_probe import ProbeError
from delayed_replay.market_view import MarketView
from delayed_replay.reference_evidence import LotEvidence, ReferenceReview
from delayed_replay.selected_trial.acquisition import (
    CALENDAR,
    DAILY,
    MASTER,
    AcquisitionStopped,
    Receipt,
    Transport,
    query,
)
from delayed_replay.selected_trial.contract import SelectedTrialPlan
from delayed_replay.selected_trial.pipeline import daily, read, sessions
from delayed_replay.selected_trial.pipeline import save as save_new
from delayed_replay.selected_trial.service import SelectedInputs
from delayed_replay.selected_trial.workflow import write_once
from delayed_replay.serialization import (
    JsonObject,
    digest,
    parse_time,
    require_hash,
    require_text,
    time_text,
)
from delayed_replay.snapshot import PriceObservation, PriceSnapshot
from delayed_replay.validation import ReplayContractError

SETTINGS_HASH = "69cd67379ea44b39c1084a7c19042e3277180a5815b8d055c0040f111c538b38"
SCOPE = dict(
    symbol="46890",
    start="2026-06-01",
    end="2026-07-01",
    model_id="daily_open_proxy_v1",
    mode="research_only",
    account="independent_200000_no_may_state_import",
    registration_status="draft_not_registered",
    actual_trade_at=None,
)
NOT_BEFORE = "2026-09-24T09:00:00.000000+00:00"


def queries():
    return [
        query(CALENDAR, **{"from": "2026-05-29", "to": "2026-06-30"}),
        query(MASTER, code="46890", date="2026-06-01"),
        query(DAILY, code="46890", **{"from": "2026-01-01", "to": "2026-05-31"}),
        query(DAILY, code="46890", **{"from": "2026-06-01", "to": "2026-06-30"}),
    ]


def june_implementation_hash():
    cli = Path(__file__).parents[1] / "examples/june_proxy_trial.py"
    return digest(
        dict(
            core=implementation_hash(), cli=hashlib.sha256(cli.read_bytes()).hexdigest()
        )
    )


def load_json(path):
    if Path(path).is_symlink():
        raise ReplayContractError("june_symlink_forbidden")
    return JsonObject.from_value(json.loads(Path(path).read_text())).to_dict()


def save(root, value):
    """Immutable and idempotent; never replace a conflicting existing artifact."""
    sha = digest(value)
    try:
        return save_new(root, value)
    except FileExistsError:
        if read(Path(root), sha) != value:
            raise ReplayContractError("june_artifact_conflict") from None
        return sha


def existing_receipt(root, plan):
    path = Path(root) / "acquisition.sqlite"
    if not path.is_file() or path.is_symlink():
        raise ReplayContractError("june_receipt_missing_no_reset")
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT value FROM events WHERE seq=0").fetchone()
        if row is None or json.loads(row[0])["data"] != plan.payload.to_dict():
            raise ValueError
    except (sqlite3.Error, ValueError, KeyError):
        raise ReplayContractError("june_receipt_invalid_no_reset") from None
    return Receipt(path, plan)


def parent_inputs(may_root):
    """Validate saved May evidence read-only, never run its service or authorization."""
    may_root = Path(may_root)
    plan = SelectedTrialPlan(
        JsonObject.from_value(load_json(may_root / "trial_plan.json"))
    )
    p = plan.payload.to_dict()
    settings = {k: p[k] for k in ("strategy", "terms", "rules")}
    if p["scope"]["symbol"] != "46890" or digest(settings) != SETTINGS_HASH:
        raise ReplayContractError("june_parent_settings_or_symbol")
    bundle = SelectedInputs.load(plan, may_root / "inputs", may_root)
    rows = bundle.rows.to_dict()
    if len(rows) < 78 or any(
        v["row"]["session"] >= SCOPE["start"] for v in rows.values()
    ):
        raise ReplayContractError("june_parent_history_scope")
    binding = read(may_root, p["source_identity"].split(":")[1])
    calendar = next(
        c["data"] for c in binding["stage_b"].values() if c["query"]["path"] == CALENDAR
    )
    return plan, bundle, calendar


def history_values(bundle):
    return {
        v["row"]["session"]: dict(
            Code=v["row"]["symbol"],
            values=[v["row"][k] for k in ("open", "high", "low", "close", "volume")]
            + v["adjusted"]
            + [v["row"]["adjustment_factor"]],
            ExRT=v["ex_right"],
        )
        for v in bundle.rows.to_dict().values()
    }


def compare_history(bundle, capture):
    """Exact semantic comparison, not whole-response hashes containing retrieval times."""
    expected, actual = history_values(bundle), {}
    if capture["query"] != queries()[2]:
        raise ReplayContractError("june_history_query")
    for r in capture["data"]:
        d = r.get("Date")
        if (
            type(d) is not str
            or d not in expected
            or d in actual
            or r.get("Code") != "46890"
        ):
            raise ReplayContractError("june_history_dates_or_code")
        actual[d] = dict(
            Code=r["Code"],
            values=[numeric(r[k]) for k in DAILY_FIELDS],
            ExRT=r.get("ExRT"),
        )
    if actual != expected:
        raise ReplayContractError("june_history_revision_no_overwrite")
    return dict(
        status="matched",
        count=len(actual),
        fields_hash=digest(expected),
        overwrite=False,
    )


def split_sessions(calendar):
    days = sessions(calendar, "2026-06-01", "2026-07-01")
    if len(days) < 2:
        raise ReplayContractError("june_two_nonempty_parts_required")
    n = len(days) // 2
    return days[:n], days[n:]


def make_lot_review(may_root, parent, reference, now):
    """Explicit new retrospective period review; never mutate the May subject."""
    require_text(reference)
    old = parent.payload.to_dict()["references"]["lot"]
    source = read(Path(may_root), old["review_hash"])
    subject = dict(
        instrument="46890", start=SCOPE["start"], end=SCOPE["end"], lot_size=100
    )
    return dict(
        schema="june-lot-review-v1",
        **subject,
        subject_hash=digest(subject),
        parent_review_hash=old["review_hash"],
        documents=source["documents"],
        source=source["source"],
        review_reference=reference,
        reviewed_at=time_text(now),
        status="retrospective_document_review_not_execution_approval",
        limitation="issuer_charter_and_published_page_not_contemporaneous_or_signed_evidence",
    )


def require_lot(review, may_root, parent):
    old = parent.payload.to_dict()["references"]["lot"]
    if (
        set(review)
        != {
            "schema",
            "instrument",
            "start",
            "end",
            "lot_size",
            "subject_hash",
            "parent_review_hash",
            "documents",
            "source",
            "review_reference",
            "reviewed_at",
            "status",
            "limitation",
        }
        or review["schema"] != "june-lot-review-v1"
        or review["parent_review_hash"] != old["review_hash"]
        or review["start"] != SCOPE["start"]
        or review["end"] != SCOPE["end"]
        or review["status"] != "retrospective_document_review_not_execution_approval"
    ):
        raise ReplayContractError("june_lot_review_scope")
    require_text(review["review_reference"])
    parse_time(review["reviewed_at"])
    prior = read(Path(may_root), old["review_hash"])
    if review["documents"] != prior["documents"] or review["source"] != prior["source"]:
        raise ReplayContractError("june_lot_document_binding")
    for d in review["documents"]:
        require_hash(d["sha256"])
        path = Path(may_root) / (d["sha256"] + ".pdf")
        if (
            path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != d["sha256"]
        ):
            raise ReplayContractError("june_lot_document_corrupt")
    lot = LotEvidence(
        review["instrument"],
        date.fromisoformat(review["start"]),
        date.fromisoformat(review["end"]),
        review["lot_size"],
        ReferenceReview(review["source"], review["subject_hash"], digest(review)),
    )
    for d in (date(2026, 6, 1), date(2026, 6, 30)):
        lot.require("46890", d)


@dataclass(frozen=True)
class JunePlan:
    payload: JsonObject

    def __post_init__(self):
        p = self.payload.to_dict()
        fixed = dict(
            schema="june-acquisition-plan-v1",
            scope=SCOPE,
            queries=queries(),
            settings_hash=SETTINGS_HASH,
            max_attempts=20,
            max_seconds=1200,
            interval_seconds=13,
            not_before=NOT_BEFORE,
            history_comparison="all_fields_exact_normalized_no_overwrite",
            split_rule="ordered_calendar_floor_half_no_prices",
            execution_permission="not_granted",
            acquisition_permission="not_granted",
        )
        if set(p) != set(fixed) | {
            "settings",
            "parent",
            "lot_review_hash",
            "implementation_hash",
        }:
            raise ReplayContractError("june_plan_fields")
        for k, v in fixed.items():
            if JsonObject.from_value({"value": p[k]}) != JsonObject.from_value(
                {"value": v}
            ):
                raise ReplayContractError("june_plan_scope")
        if digest(p["settings"]) != SETTINGS_HASH:
            raise ReplayContractError("june_settings_changed")
        for k in ("lot_review_hash", "implementation_hash"):
            require_hash(p[k])
        parent = p["parent"]
        if set(parent) != {
            "plan_hash",
            "model_hash",
            "history_hash",
            "packets",
            "provenance",
        } or parent["provenance"] not in ("saved_jquants", "artificial_fixture"):
            raise ReplayContractError("june_parent_contract")
        for k in ("plan_hash", "model_hash", "history_hash"):
            require_hash(parent[k])
        if (
            type(parent["packets"]) is not list
            or len(parent["packets"]) != 4
            or len(set(parent["packets"])) != 4
        ):
            raise ReplayContractError("june_parent_packets")
        for h in parent["packets"]:
            require_hash(h)

    @property
    def sha256(self):
        return self.payload.sha256


def prepare(root, may_root, review_reference, *, now=None):
    root, may_root = Path(root), Path(may_root)
    parent, bundle, _ = parent_inputs(may_root)
    if (
        root.resolve() == may_root.resolve()
        or may_root.resolve() in root.resolve().parents
        or root.exists()
    ):
        raise ReplayContractError("june_new_output_required_no_reset")
    review = make_lot_review(
        may_root, parent, review_reference, now or datetime.now(UTC)
    )
    require_lot(review, may_root, parent)
    p = parent.payload.to_dict()
    plan = JunePlan(
        JsonObject.from_value(
            dict(
                schema="june-acquisition-plan-v1",
                scope=SCOPE,
                queries=queries(),
                settings={k: p[k] for k in ("strategy", "terms", "rules")},
                settings_hash=SETTINGS_HASH,
                parent=dict(
                    plan_hash=parent.sha256,
                    model_hash=p["model_hash"],
                    history_hash=digest(history_values(bundle)),
                    packets=p["history_packets"] + p["packets"],
                    provenance=p["provenance"],
                ),
                lot_review_hash=digest(review),
                implementation_hash=june_implementation_hash(),
                max_attempts=20,
                max_seconds=1200,
                interval_seconds=13,
                not_before=NOT_BEFORE,
                history_comparison="all_fields_exact_normalized_no_overwrite",
                split_rule="ordered_calendar_floor_half_no_prices",
                execution_permission="not_granted",
                acquisition_permission="not_granted",
            )
        )
    )
    root.mkdir(parents=True, exist_ok=False)
    save(root, review)
    write_once(root / "plan.json", plan.payload.to_dict())
    write_once(
        root / "acquisition_authorization.template.json",
        dict(
            schema="june-acquisition-authorization-v1",
            plan_hash=plan.sha256,
            status="not_approved",
            approval_reference=None,
            permission="acquire_only",
            execution_permission=False,
        ),
    )
    Receipt(root / "acquisition.sqlite", plan).close()
    return plan


def load_context(root, may_root):
    root = Path(root)
    plan = JunePlan(JsonObject.from_value(load_json(root / "plan.json")))
    parent, bundle, calendar = parent_inputs(may_root)
    p, old = plan.payload.to_dict(), parent.payload.to_dict()
    expected = dict(
        plan_hash=parent.sha256,
        model_hash=old["model_hash"],
        history_hash=digest(history_values(bundle)),
        packets=old["history_packets"] + old["packets"],
        provenance=old["provenance"],
    )
    if p["parent"] != expected or p["settings"] != {
        k: old[k] for k in ("strategy", "terms", "rules")
    }:
        raise ReplayContractError("june_parent_binding_changed")
    if p["implementation_hash"] != june_implementation_hash():
        raise ReplayContractError("june_implementation_changed")
    require_lot(read(root, p["lot_review_hash"]), may_root, parent)
    if not (root / "acquisition.sqlite").is_file():
        raise ReplayContractError("june_receipt_missing_no_reset")
    return plan, bundle, calendar


class JuneTransport(Transport):
    def allowed(self, q):
        if type(self.receipt.plan) is not JunePlan or q not in queries():
            raise AcquisitionStopped("june_request_outside_plan")
        if q == queries()[3] and not any(
            e["kind"] == "history_verified" for e in self.receipt.events()
        ):
            raise AcquisitionStopped("june_history_first")


def acquire_inputs(transport, bundle, old_calendar):
    q = queries()
    calendar = transport.fetch(q[0])
    sessions(calendar["data"], "2026-05-29", "2026-07-01")
    overlap = [r for r in old_calendar if r["Date"] >= "2026-05-29"]
    if sorted(overlap, key=lambda r: r["Date"]) != sorted(
        (r for r in calendar["data"] if r["Date"] < SCOPE["start"]),
        key=lambda r: r["Date"],
    ):
        raise ReplayContractError("june_calendar_revision")
    split_sessions([r for r in calendar["data"] if r["Date"] >= SCOPE["start"]])
    master = transport.fetch(q[1])
    rows = master["data"]
    if (
        len(rows) != 1
        or rows[0].get("Code") != "46890"
        or rows[0].get("Date") != SCOPE["start"]
        or rows[0].get("ProdCat") != "011"
    ):
        raise ReplayContractError("june_master_mismatch")
    history = transport.fetch(q[2])
    result = compare_history(bundle, history)
    transport.receipt.append(
        "history_verified", dict(**result, capture_hash=digest(history))
    )
    run = transport.fetch(q[3])
    parts = split_sessions([r for r in calendar["data"] if r["Date"] >= SCOPE["start"]])
    daily(run, "46890", parts[0] + parts[1])
    return [calendar, master, history, run]


def require_acquisition_permission(plan, authorization, now, reviewed_free_window):
    p = plan.payload.to_dict()
    if p["parent"]["provenance"] != "saved_jquants":
        raise ReplayContractError("june_artificial_live_forbidden")
    if (
        set(authorization)
        != {
            "schema",
            "plan_hash",
            "status",
            "approval_reference",
            "permission",
            "execution_permission",
        }
        or authorization["schema"] != "june-acquisition-authorization-v1"
        or authorization["plan_hash"] != plan.sha256
        or authorization["status"] != "approved_for_acquisition"
        or authorization["permission"] != "acquire_only"
        or authorization["execution_permission"] is not False
    ):
        raise ReplayContractError("june_separate_acquisition_approval_required")
    require_text(authorization["approval_reference"])
    if reviewed_free_window is not True:
        raise ReplayContractError("june_current_free_terms_review_required")
    if (
        now < parse_time(NOT_BEFORE)
        or date(2026, 7, 1) > (now - timedelta(weeks=12)).date()
        or date(2026, 1, 1) < (now - timedelta(days=730)).date()
    ):
        raise ReplayContractError("june_outside_acquisition_window")


def live_acquire(
    root, may_root, authorization_path, *, resume=False, reviewed_free_window=False
):
    """Not invoked by prepare/inspect/build; a future separate permission is mandatory."""
    root = Path(root)
    plan, bundle, calendar = load_context(root, may_root)
    require_acquisition_permission(
        plan, load_json(authorization_path), datetime.now(UTC), reviewed_free_window
    )
    key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not key:
        raise ReplayContractError("june_api_key_missing")
    receipt = existing_receipt(root, plan)
    try:
        events = receipt.events()
        if any(e["kind"] == "stopped" for e in events):
            raise AcquisitionStopped("june_previously_stopped")
        if not resume and any(e["kind"] == "attempt" for e in events):
            raise AcquisitionStopped("june_use_resume_no_reset")
        receipt.remaining()
        import jquantsapi

        captures = acquire_inputs(
            JuneTransport(receipt, client=jquantsapi.ClientV2(api_key=key)),
            bundle,
            calendar,
        )
        write_once(
            root / "acquisition_result.json",
            dict(plan_hash=plan.sha256, captures=captures),
        )
        write_once(root / "communication.json", receipt.statistics())
        return dict(
            status="acquired_not_cleared",
            plan_hash=plan.sha256,
            attempts=receipt.statistics()["attempts"],
        )
    except (ValueError, OSError, TypeError, KeyError, ProbeError) as exc:
        if isinstance(exc, AcquisitionStopped) and str(exc) in (
            "june_use_resume_no_reset",
            "june_previously_stopped",
        ):
            raise
        receipt.append(
            "stopped", dict(reason="june_acquisition_contract_transport_or_budget_stop")
        )
        write_once(root / "communication.json", receipt.statistics())
        raise AcquisitionStopped("june_acquisition_stopped") from None
    finally:
        receipt.close()


def receipt_captures(receipt):
    """Bind derived captures to the successful durable response pages, not filenames alone."""
    events = receipt.events()
    responses = {digest(e): e["data"] for e in events if e["kind"] == "response"}
    attempts = {digest(e): e["data"] for e in events if e["kind"] == "attempt"}
    captures = {}
    for e in events:
        if e["kind"] != "capture":
            continue
        c, rows = e["data"]["capture"], []
        if c["query"] not in queries():
            raise ReplayContractError("june_receipt_query")
        key = None
        if not c["pages"] or len(c["pages"]) != len(set(c["pages"])):
            raise ReplayContractError("june_receipt_pages")
        for page_number, h in enumerate(c["pages"]):
            r = responses[h]
            params = dict(c["query"]["params"])
            if key is not None:
                params["pagination_key"] = key
            request_id = digest(dict(path=c["query"]["path"], params=params))
            a = attempts[r["attempt_id"]]
            if (
                r["status"] != 200
                or r["request_id"] != request_id
                or a != dict(request_id=request_id, query=c["query"], params=params)
            ):
                raise ReplayContractError("june_receipt_response")
            page = json.loads(r["raw"], parse_float=str)
            rows.extend(page["data"])
            key = page.get("pagination_key")
            if page_number < len(c["pages"]) - 1 and (type(key) is not str or not key):
                raise ReplayContractError("june_receipt_pagination")
        if key or rows != c["data"] or digest(c["query"]) in captures:
            raise ReplayContractError("june_receipt_capture_mismatch")
        captures[digest(c["query"])] = c
    if set(captures) != {digest(q) for q in queries()}:
        raise ReplayContractError("june_acquisition_incomplete")
    return [captures[digest(q)] for q in queries()]


def inspect(root, may_root):
    plan, bundle, _ = load_context(root, may_root)
    receipt = existing_receipt(root, plan)
    try:
        return dict(
            status="prepared_only",
            plan_hash=plan.sha256,
            implementation_hash=plan.payload.to_dict()["implementation_hash"],
            settings_hash=SETTINGS_HASH,
            history_observations=len(bundle.rows.to_dict()),
            communication=receipt.statistics(),
            acquisition_permission="not_granted_by_preparation",
            clearing="not_executed",
            formal_oos=False,
        )
    finally:
        receipt.close()


def build(root, may_root):
    """Publish input-only artifacts. Does not instantiate any account/reducer/runner."""
    root = Path(root)
    plan, bundle, old_calendar = load_context(root, may_root)
    receipt = existing_receipt(root, plan)
    try:
        captured = receipt_captures(receipt)
    finally:
        receipt.close()

    # Reuse all validation paths offline without touching the expired HTTP budget.
    class Saved:
        def fetch(self, q):
            return captured[queries().index(q)]

        class receipt:
            @staticmethod
            def append(*_):
                pass

    acquire_inputs(Saved(), bundle, old_calendar)
    calendar, _, history, run = captured
    parts = split_sessions([r for r in calendar["data"] if r["Date"] >= SCOPE["start"]])
    reported = daily(run, "46890", parts[0] + parts[1])
    # Check finite features at each actual session using only its prefix; never Signals.
    import numpy as np
    import pandas as pd

    config = (
        SelectedTrialPlan(
            JsonObject.from_value(load_json(Path(may_root) / "trial_plan.json"))
        )
        .signals()
        .config("baseline")
    )
    values = {d: r["values"] for d, r in history_values(bundle).items()}
    values.update({d: r["values"] for d, r in reported.items()})
    for day in parts[0] + parts[1]:
        frame = pd.DataFrame(
            [
                dict(
                    date=pd.Timestamp(d),
                    **dict(
                        zip(
                            ("open", "high", "low", "close", "volume"),
                            map(float, v[5:10]),
                            strict=True,
                        )
                    ),
                )
                for d, v in sorted(values.items())
                if d <= day
            ]
        )
        frame["turnover_value"] = frame.close * frame.volume
        features = config.create_scorer().transform(
            config.create_detector().transform(frame)
        )
        if not np.isfinite(
            features.iloc[-1][["sma", "atr", "adx", "range_score"]].to_numpy(
                dtype=float
            )
        ).all():
            raise ReplayContractError("june_nonfinite_prefix_features")
    source_hash = save(root, run)
    wall = parse_time(run["fetched_at"])
    store, hashes = InputArtifactStore(root / "inputs"), []
    for days in parts:
        observations = tuple(
            PriceObservation(
                "46890",
                date.fromisoformat(d),
                wall,
                tuple(map(float, reported[d]["values"][:5])),
                tuple(map(float, reported[d]["values"][5:10])),
                float(reported[d]["values"][10]),
                0.0,
                0.0,
            )
            for d in days
        )
        snapshot = PriceSnapshot.create(
            provider="jquants",
            provider_price_basis=provider_price_basis("jquants"),
            source_artifact_sha256=source_hash,
            data_version="june-input-only-v1",
            first_observed_at=wall,
            fetched_at=wall,
            provider_published_at=None,
            publication_time_unknown_reason="historical_publication_not_observed",
            price_basis_evidence_id="jquants_reported_separate_adjusted",
            observations=observations,
        )
        hashes.append(store.publish(InputPacket(MarketView((snapshot,), ()))))
    result = dict(
        schema="june-input-manifest-v1",
        plan_hash=plan.sha256,
        scope=SCOPE,
        settings_hash=SETTINGS_HASH,
        implementation_hash=plan.payload.to_dict()["implementation_hash"],
        history_packets=plan.payload.to_dict()["parent"]["packets"],
        run_packets=hashes,
        parts=[list(p) for p in parts],
        history_comparison=compare_history(bundle, history),
        captures=[save(root, c) for c in captured],
        lot_review_hash=plan.payload.to_dict()["lot_review_hash"],
        provenance=plan.payload.to_dict()["parent"]["provenance"],
        causal_features="finite_session_prefixes_only",
        input_ready=True,
        executable=False,
        reason="input_preparation_only_no_june_clearing_adapter_or_permission",
        clearing="not_executed",
        formal_oos=False,
    )
    write_once(root / "input_manifest.json", result)
    return result
