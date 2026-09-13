"""Artificial-only tests of owner scope, real adapters, persistent budget and restart."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import requests
from delayed_replay_e2e_helpers import network_guard as network_guard
from test_limited_proxy_trial import SESSIONS

from delayed_replay.limited_trial.models import (
    LimitedProxyTrialPlan,
    ScopedResearchAuthorization,
)
from delayed_replay.limited_trial.service import LimitedTrialService
from delayed_replay.selected_trial.acquisition import (
    AcquisitionStopped,
    Receipt,
    Transport,
    stage_a,
    stage_b,
)
from delayed_replay.selected_trial.contract import (
    SYMBOLS,
    AcquisitionPlan,
    SelectedTrialPlan,
    new_acquisition,
)
from delayed_replay.selected_trial.pipeline import (
    acquire,
    build_inputs,
    read,
    save,
    select,
)
from delayed_replay.selected_trial.service import (
    SelectedInputs,
    SelectedPreflight,
    SelectedService,
    generated_rows,
)
from delayed_replay.serialization import JsonObject, digest, time_text
from delayed_replay.validation import ReplayContractError
from examples.validate_limited_proxy import compare

pytestmark = pytest.mark.usefixtures("network_guard")
PROJECT = Path(__file__).parents[1]


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 14, tzinfo=UTC)

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += timedelta(seconds=seconds)


class Response:
    def __init__(self, data=None, status=200, headers=None, key=None):
        self.status_code, self.headers = status, headers or {}
        self.body = json.dumps(dict(data=data or [], pagination_key=key)).encode()

    def iter_content(self, _):
        yield self.body

    def close(self):
        pass


def review(root, symbol):
    raw = b"ARTIFICIAL DOCUMENT NOT ISSUER EVIDENCE"
    h = hashlib.sha256(raw).hexdigest()
    (root / (h + ".pdf")).write_bytes(raw)
    subject = dict(
        instrument=symbol, start="2026-04-30", end="2026-06-01", lot_size=100
    )
    return save(
        root,
        dict(
            schema="selected-lot-review-v1",
            **subject,
            subject_hash=digest(subject),
            source="artificial-only",
            review_reference="artificial-test-review-not-owner-approval",
            documents=[dict(sha256=h)],
        ),
    )


def plan_for(root):
    root.mkdir(parents=True, exist_ok=True)
    reviews = {s: review(root, s) for s in SYMBOLS}
    old = LimitedProxyTrialPlan(
        JsonObject.from_value(
            json.loads((PROJECT / "config/limited_proxy_trial_plan.json").read_text())
        )
    )
    return new_acquisition(
        old, PROJECT / "config/selected_proxy_strategy.yaml", reviews
    )


def fake_rows(path, params):
    if path.endswith("master"):
        return [dict(Code=params["code"], Date=params["date"], ProdCat="011")]
    if "date" in params:
        days = [params["date"]]
    else:
        start, end = (
            date.fromisoformat(params["from"]),
            date.fromisoformat(params["to"]),
        )
        days = [
            (start + timedelta(days=i)).isoformat()
            for i in range((end - start).days + 1)
        ]

    def open_day(d):
        return (
            d in SESSIONS if d >= "2026-05-01" else date.fromisoformat(d).weekday() < 5
        )

    if path.endswith("calendar"):
        return [dict(Date=d, HolDiv="1" if open_day(d) else "0") for d in days]
    return list(
        generated_rows([d for d in days if open_day(d)], params["code"]).values()
    )


def artifacts(root):
    plan, clock = plan_for(root), Clock()
    receipt = Receipt(root / "acq.sqlite", plan, now=clock.now)
    calls = []

    def request(path, params, timeout):
        calls.append((path, params, timeout, clock.now()))
        return Response(fake_rows(path, params))

    selected, a, b = acquire(
        Transport(receipt, request=request, sleep=clock.sleep), root
    )
    receipt.close()
    trial = build_inputs(plan, selected, a, b, root, artificial=True)
    auth = ScopedResearchAuthorization(
        JsonObject.from_value(
            dict(
                schema="limited-authorization-v1",
                plan_hash=trial.sha256,
                status="artificial_test_authorization",
                approval_reference="generated-fixture-only",
                recorded_at=time_text(clock.now()),
            )
        )
    )
    return plan, trial, auth, clock, selected, a, b, calls


def test_eleven_queries_only_selected_history_and_causal_order(tmp_path):
    _, trial, auth, clock, selection, a, _, calls = artifacts(tmp_path)
    assert selection["symbol"] == "46890"
    assert len(calls) == 11
    assert all(p.get("code", "46890") == "46890" for _, p, _, _ in calls[7:])
    assert all(
        p.get("date") == "2026-04-30"
        for path, p, _, _ in calls[:7]
        if not path.endswith("calendar")
    )
    assert all(
        (b[3] - a[3]).total_seconds() >= 13
        for a, b in zip(calls, calls[1:], strict=False)
    )
    result = SelectedPreflight.evaluate(
        trial, tmp_path / "inputs", tmp_path, auth
    ).to_dict()
    assert result["ready"], result["reasons"]
    assert result["history"]["available_before_start"] >= 78
    assert result["clearing"] == "not_executed"
    assert len(a) == 7
    assert clock.now() >= parse_last(calls)


def parse_last(calls):
    return calls[-1][3]


def test_continuous_split_restart_shared_arithmetic_and_no_formal(tmp_path):
    root = tmp_path / "inputs_root"
    _, trial, auth, clock, *_ = artifacts(root)
    result = compare(
        trial, auth, root, tmp_path / "result", clock.now(), _service=SelectedService
    ).to_dict()
    assert result["logical_state_equal"] and result["prefix_unchanged"]
    assert result["report"]["formal_checkpoint"] == "unsupported"
    assert result["report"]["capability"]["model_approval"] == "unapproved"
    assert result["report"]["fill_count"] > 0


def test_legacy_plan_and_service_reject_selected_scope(tmp_path):
    _, trial, auth, _, *_ = artifacts(tmp_path)
    with pytest.raises(ReplayContractError):
        LimitedProxyTrialPlan(trial.payload)
    with pytest.raises(ReplayContractError, match="plan_type"):
        LimitedTrialService._prepare(trial, auth, tmp_path / "inputs", tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_attempts", True),
        ("max_attempts", 21),
        ("max_seconds", 1201),
        ("history_start", "2025-12-01"),
        ("interval_seconds", 12),
        ("symbols", ["72030"]),
        ("reference_date", "2026-05-01"),
    ],
)
def test_acquisition_scope_changes_rejected(tmp_path, field, value):
    p = plan_for(tmp_path).payload.to_dict()
    p[field] = value
    with pytest.raises(ReplayContractError):
        AcquisitionPlan(JsonObject.from_value(p))


@pytest.mark.parametrize(
    "field",
    ["max_position_pct", "commission_rate", "slippage_pct", "reservation_buffer_pct"],
)
def test_approved_arithmetic_cannot_change(tmp_path, field):
    p = plan_for(tmp_path).payload.to_dict()
    p["terms"][field] = "0.2"
    with pytest.raises(ReplayContractError):
        AcquisitionPlan(JsonObject.from_value(p))


def test_all_strategy_fields_are_frozen(tmp_path):
    p = plan_for(tmp_path).payload.to_dict()
    p["strategy"]["sma_period"] = 19
    with pytest.raises(ReplayContractError, match="strategy_changed"):
        AcquisitionPlan(JsonObject.from_value(p))


def test_stage_b_before_selection_and_other_symbol_rejected(tmp_path):
    plan, clock = plan_for(tmp_path), Clock()
    r = Receipt(tmp_path / "a.sqlite", plan, now=clock.now)
    t = Transport(
        r, request=lambda *_: pytest.fail("network invoked"), sleep=clock.sleep
    )
    with pytest.raises(AcquisitionStopped, match="outside"):
        t.fetch(stage_b("94320")[2])
    r.freeze_selection(dict(symbol="46890"))
    with pytest.raises(AcquisitionStopped, match="outside"):
        t.fetch(stage_b("94320")[2])
    with pytest.raises(AcquisitionStopped, match="reselection"):
        r.freeze_selection(dict(symbol="94320"))
    r.close()


def test_restart_preserves_attempts_absolute_deadline_and_timeout(tmp_path):
    plan, clock = plan_for(tmp_path), Clock()
    path = tmp_path / "a.sqlite"
    r = Receipt(path, plan, now=clock.now)
    r.append("attempt", dict(request_id="x"))  # response lost after send
    started = r.statistics()["started_at"]
    r.close()
    clock.sleep(1195)
    r = Receipt(path, plan, now=clock.now)
    timeouts = []
    t = Transport(
        r,
        request=lambda p, q, timeout: timeouts.append(timeout) or Response(),
        sleep=clock.sleep,
    )
    t.page(stage_a()[0])
    assert timeouts == [5]
    assert r.statistics()["attempts"] == 2 and r.statistics()["started_at"] == started
    r.close()
    clock.sleep(5)
    r = Receipt(path, plan, now=clock.now)
    with pytest.raises(AcquisitionStopped, match="deadline"):
        r.remaining()
    r.close()


def test_twenty_attempt_limit_survives_restart(tmp_path):
    plan, clock = plan_for(tmp_path), Clock()
    path = tmp_path / "a.sqlite"
    r = Receipt(path, plan, now=clock.now)
    for i in range(20):
        r.append("attempt", dict(index=i))
    r.close()
    r = Receipt(path, plan, now=clock.now)
    with pytest.raises(AcquisitionStopped, match="attempt_budget"):
        Transport(
            r, request=lambda *_: pytest.fail("twenty-first request"), sleep=clock.sleep
        ).page(stage_a()[0])
    assert r.statistics()["attempts"] == 20
    r.close()


@pytest.mark.parametrize("first", [429, 503, "network"])
def test_retry_spacing_and_all_actual_attempts_count(tmp_path, first):
    plan, clock = plan_for(tmp_path), Clock()
    r = Receipt(tmp_path / "a.sqlite", plan, now=clock.now)
    calls = []

    def request(*_):
        calls.append(clock.now())
        if len(calls) == 1:
            if first == "network":
                raise requests.ConnectionError("private details must not appear")
            return Response(status=first, headers={"Retry-After": "40"})
        return Response()

    Transport(r, request=request, sleep=clock.sleep).page(stage_a()[0])
    assert len(calls) == r.statistics()["attempts"] == 2
    assert (calls[1] - calls[0]).total_seconds() >= (13 if first == "network" else 40)
    assert "private details" not in str(r.events())
    r.close()


def test_retry_after_persists_across_restart(tmp_path):
    plan, clock = plan_for(tmp_path), Clock()
    path = tmp_path / "a.sqlite"
    r = Receipt(path, plan, now=clock.now)
    r.append("attempt", {})
    r.append(
        "response",
        dict(status=429, retry_at=time_text(clock.now() + timedelta(seconds=90))),
    )
    r.close()
    r = Receipt(path, plan, now=clock.now)
    Transport(r, request=lambda *_: Response(), sleep=clock.sleep).page(stage_a()[0])
    assert r.statistics()["attempts"] == 2
    assert (clock.now() - datetime(2026, 9, 14, tzinfo=UTC)).total_seconds() == 90
    r.close()


def test_real_official_client_adapter_internal_retries_disabled(tmp_path):
    import jquantsapi

    plan = plan_for(tmp_path)
    r = Receipt(tmp_path / "a.sqlite", plan)
    client = jquantsapi.ClientV2(api_key="ARTIFICIAL-NOT-A-KEY")
    Transport(r, client=client)
    session = client._session
    assert session.get_adapter("https://api.jquants.com/").max_retries.total == 0
    r.close()


def test_receipt_lock_and_hash_tamper(tmp_path):
    plan = plan_for(tmp_path)
    path = tmp_path / "a.sqlite"
    r = Receipt(path, plan)
    with pytest.raises(AcquisitionStopped, match="already_running"):
        Receipt(path, plan)
    r.db.execute("UPDATE events SET sha=? WHERE seq=0", ("a" * 64,))
    r.db.commit()
    with pytest.raises(AcquisitionStopped, match="corrupt"):
        r.events()
    r.close()


def test_pagination_counts_and_cached_capture_does_not_refetch(tmp_path):
    plan, clock = plan_for(tmp_path), Clock()
    r = Receipt(tmp_path / "a.sqlite", plan, now=clock.now)
    calls = []

    def request(path, params, timeout):
        calls.append(params)
        return Response(
            [dict(Date="2026-04-30", HolDiv="1")],
            key="next" if len(calls) == 1 else None,
        )

    t = Transport(r, request=request, sleep=clock.sleep)
    expected = t.fetch(stage_a()[0])
    assert t.fetch(stage_a()[0]) == expected
    assert len(calls) == 2 and calls[1]["pagination_key"] == "next"
    r.close()


def test_future_prices_cannot_change_selection_and_missing_reference_stops(tmp_path):
    plan, _, _, _, selection, a, b, _ = artifacts(tmp_path)
    b.clear()
    assert select(plan, a, tmp_path) == selection
    price = next(c for c in a.values() if c["query"]["path"].endswith("bars/daily"))
    price["data"] = []
    with pytest.raises(AcquisitionStopped, match="missing"):
        select(plan, a, tmp_path)


def test_history_tamper_and_real_vs_artificial_approval_rejected(tmp_path):
    _, trial, auth, _, *_ = artifacts(tmp_path)
    changed = trial.payload.to_dict()
    changed["strategy"]["sma_period"] = 2
    with pytest.raises(ReplayContractError, match="settings_changed"):
        SelectedInputs.load(
            SelectedTrialPlan(JsonObject.from_value(changed)),
            tmp_path / "inputs",
            tmp_path,
        )
    a = auth.payload.to_dict()
    a["status"] = "approved_for_limited_trial"
    with pytest.raises(ReplayContractError, match="scope"):
        ScopedResearchAuthorization(JsonObject.from_value(a)).require(trial)


def test_lot_bytes_corruption_stops_selection(tmp_path):
    plan, _, _, _, _, a, _, _ = artifacts(tmp_path)
    review_data = read(tmp_path, plan.payload.to_dict()["lot_reviews"]["46890"])
    (tmp_path / (review_data["documents"][0]["sha256"] + ".pdf")).write_bytes(
        b"tampered"
    )
    with pytest.raises(AcquisitionStopped, match="corrupt"):
        select(plan, a, tmp_path)


def test_affordability_not_return_ranking_and_no_candidate_fallback(tmp_path):
    plan, _, _, _, _, a, _, _ = artifacts(tmp_path)
    for c in a.values():
        if (
            c["query"]["path"].endswith("bars/daily")
            and c["query"]["params"]["code"] == "46890"
        ):
            for r in c["data"]:
                for k in ("O", "H", "L", "C", "AdjO", "AdjH", "AdjL", "AdjC"):
                    r[k] = "3000"
    assert select(plan, a, tmp_path)["symbol"] == "94320"
    for c in a.values():
        if c["query"]["path"].endswith("bars/daily"):
            for r in c["data"]:
                for k in ("O", "H", "L", "C", "AdjO", "AdjH", "AdjL", "AdjC"):
                    r[k] = "3000"
    with pytest.raises(AcquisitionStopped, match="no_affordable"):
        select(plan, a, tmp_path)


@pytest.mark.parametrize(
    "failure", ["history_missing", "corporate_action", "short_history"]
)
def test_history_failure_stops_before_month_fetch_no_reselection(tmp_path, failure):
    plan, clock = plan_for(tmp_path), Clock()
    r = Receipt(tmp_path / "a.sqlite", plan, now=clock.now)
    calls = []

    def request(path, params, timeout):
        calls.append(params)
        rows = fake_rows(path, params)
        if (
            failure == "short_history"
            and path.endswith("calendar")
            and params.get("from") == "2026-01-01"
        ):
            for row in rows:
                if row["Date"] < "2026-02-01":
                    row["HolDiv"] = "0"
        if path.endswith("bars/daily") and params.get("from") == "2026-01-01":
            if failure == "history_missing":
                rows = rows[1:]
            elif failure == "corporate_action":
                rows[0]["AdjFactor"] = "0.5"
            else:
                rows = [row for row in rows if row["Date"] >= "2026-02-01"]
        return Response(rows)

    with pytest.raises(AcquisitionStopped):
        acquire(Transport(r, request=request, sleep=clock.sleep), tmp_path)
    assert len(calls) == 10
    assert r.selection()["symbol"] == "46890"
    assert all(q.get("code", "46890") == "46890" for q in calls[7:])
    r.close()


def test_clock_regression_and_wait_exceeding_deadline_rejected(tmp_path):
    plan, clock = plan_for(tmp_path), Clock()
    r = Receipt(tmp_path / "a.sqlite", plan, now=clock.now)
    r.append("attempt", {})
    clock.sleep(-1)
    with pytest.raises(AcquisitionStopped, match="regressed"):
        r.remaining()
    clock.sleep(1199)
    t = Transport(
        r, request=lambda *_: pytest.fail("request after budget"), sleep=clock.sleep
    )
    with pytest.raises(AcquisitionStopped, match="wait_exceeds"):
        t.wait(3)
    r.close()


def test_real_adapter_passes_remaining_timeout_and_no_redirects(tmp_path, monkeypatch):
    import jquantsapi

    plan, clock = plan_for(tmp_path), Clock()
    r = Receipt(tmp_path / "a.sqlite", plan, now=clock.now)
    r.append("attempt", {})
    clock.sleep(1198)
    client = jquantsapi.ClientV2(api_key="ARTIFICIAL-KEY")
    t = Transport(r, client=client, sleep=clock.sleep)
    seen = []

    def get(url, **kwargs):
        seen.append(kwargs)
        return Response()

    monkeypatch.setattr(client._session, "get", get)
    t.page(stage_a()[0])
    assert seen[0]["timeout"] == 2
    assert seen[0]["allow_redirects"] is False and seen[0]["stream"] is True
    assert (
        client._session.get_adapter("https://api.jquants.com/").max_retries.total == 0
    )
    r.close()


def test_missing_approval_never_creates_account(tmp_path):
    _, trial, _, _, *_ = artifacts(tmp_path)
    path = tmp_path / "forbidden.sqlite"
    with pytest.raises(ReplayContractError, match="approval_required"):
        SelectedService.create(
            path, trial, None, tmp_path / "inputs", tmp_path, "continuous"
        )
    assert not path.exists()


def test_output_plan_and_original_strategy_never_mutated(tmp_path):
    before = {
        p: p.read_bytes()
        for p in (
            PROJECT / "config/limited_proxy_trial_plan.json",
            PROJECT / "config/strategy.yaml",
        )
    }
    artifacts(tmp_path)
    assert all(p.read_bytes() == value for p, value in before.items())


def test_nonfinite_features_block_before_account(tmp_path, monkeypatch):
    import numpy as np

    from screening import RangeScorer

    _, trial, auth, _, *_ = artifacts(tmp_path)
    original = RangeScorer.transform

    def corrupt(self, frame):
        result = original(self, frame)
        result["range_score"] = np.nan
        return result

    monkeypatch.setattr(RangeScorer, "transform", corrupt)
    result = SelectedPreflight.evaluate(
        trial, tmp_path / "inputs", tmp_path, auth
    ).to_dict()
    assert not result["ready"] and "nonfinite_features" in result["reasons"]
    assert result["clearing"] == "not_executed"
