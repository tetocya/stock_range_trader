"""Artificial transport and data fixtures. Real ClientV2 adapter, zero HTTP."""

import json
import time
from dataclasses import replace
from datetime import date, timedelta

import jquantsapi
import pytest
import requests
from delayed_replay_e2e_helpers import network_guard as network_guard

from delayed_replay.input_artifacts import InputArtifactStore
from delayed_replay.live_probe import (
    ENDPOINTS,
    BoundedTransport,
    ProbeConfig,
    ProbeError,
    calendar_check,
    deadline,
    retry_after,
    run_probe,
    validate_live_window,
)
from delayed_replay.validation import ReplayContractError

pytestmark = pytest.mark.usefixtures("network_guard")
START = date(2024, 6, 3)
CONFIG = ProbeConfig(START, START + timedelta(days=2), START)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.payload = payload
        self.closed = False

    def iter_content(self, size):
        yield json.dumps(self.payload).encode()

    def close(self):
        self.closed = True


def transport(monkeypatch, responses, config=CONFIG):
    clock = Clock()
    client = jquantsapi.ClientV2(api_key="artificial-noncredential")
    result = BoundedTransport(config, client, clock=clock, sleep=clock.sleep)
    calls = []
    iterator = iter(responses)

    def get(url, **kwargs):
        calls.append((clock(), url, kwargs))
        response = next(iterator)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(result.session, "get", get)
    return result, calls


def rows():
    return [
        dict(
            Date=(START + timedelta(days=i)).isoformat(),
            Code="72030",
            O=100.0,
            H=110.0,
            L=90.0,
            C=101.0,
            Vo=1000.0,
            Va=101000.0,
            AdjO=100.0,
            AdjH=110.0,
            AdjL=90.0,
            AdjC=101.0,
            AdjVo=1000.0,
            AdjFactor=1.0,
        )
        for i in range(2)
    ]


def successful_responses():
    return [
        Response(
            dict(
                data=[
                    dict(
                        Date=START.isoformat(),
                        Code="72030",
                        CoName="ARTIFICIAL COMPANY",
                    )
                ]
            )
        ),
        Response(dict(data=rows())),
        Response(dict(data=[dict(Date=r["Date"], HolDiv="1") for r in rows()])),
    ]


@pytest.mark.parametrize(
    "changes",
    [
        dict(symbol="99990"),
        dict(max_attempts=21),
        dict(max_attempts=True),
        dict(max_seconds=1201),
        dict(max_seconds=-1),
        dict(interval_seconds=float("nan")),
        dict(interval_seconds=12),
        dict(start=True),
        dict(end=START),
        dict(end=START + timedelta(days=32)),
    ],
)
def test_fixed_scope_and_budgets(changes):
    with pytest.raises(ProbeError):
        replace(CONFIG, **changes)


def test_preflight_and_missing_key_never_construct_transport(monkeypatch, tmp_path):
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)

    def forbidden(*a, **k):
        raise AssertionError("client construction forbidden")

    monkeypatch.setattr(jquantsapi, "ClientV2", forbidden)
    baseline = run_probe(CONFIG, tmp_path / "preflight")
    assert baseline["attempts"] == 0 and baseline["end_reason"] == "preflight_only"
    result = run_probe(CONFIG, tmp_path / "missing", live=True, reviewed=True)
    assert result["attempts"] == 0
    assert result["layers"]["communication"] == dict(
        status="blocked", reason="api_key_missing"
    )
    assert not list(tmp_path.iterdir())


def test_actual_sdk_adapter_redirects_and_timeout(monkeypatch):
    t, calls = transport(monkeypatch, [Response(dict(data=[]))])
    retry = t.session.get_adapter("https://api.jquants.com").max_retries
    assert all(
        getattr(retry, k) == 0
        for k in ("total", "connect", "read", "redirect", "status", "other")
    )
    t.fetch(ENDPOINTS[0])
    assert calls[0][2]["allow_redirects"] is False
    assert calls[0][2]["timeout"] == 30
    assert calls[0][2]["params"]["code"] == "72030"


@pytest.mark.parametrize("status", [401, 403, 302, 400])
def test_nonretryable_status_redacted(monkeypatch, tmp_path, status):
    t, calls = transport(monkeypatch, [Response(status=status)])
    result = run_probe(
        CONFIG, tmp_path / "probe", live=True, reviewed=True, transport=t
    )
    assert len(calls) == result["attempts"] == 1
    assert not result["acquisition_complete"]
    assert not list((tmp_path / "probe").iterdir())
    assert "artificial-noncredential" not in json.dumps(result)


@pytest.mark.parametrize(
    "first",
    [
        Response(status=429, headers={"Retry-After": "40"}),
        Response(status=503),
        requests.ConnectionError("SECRET URL AND HEADER"),
    ],
)
def test_every_retry_is_budgeted_and_spaced(monkeypatch, first):
    t, calls = transport(monkeypatch, [first, Response(dict(data=[]))])
    assert t.fetch(ENDPOINTS[0]) == []
    assert t.attempts == 2
    assert calls[1][0] - calls[0][0] >= (
        40 if getattr(first, "status_code", None) == 429 else 13
    )
    assert "SECRET" not in json.dumps(t.audit)


def test_wait_budget_is_not_shortened(monkeypatch):
    t, calls = transport(
        monkeypatch, [Response(status=429, headers={"Retry-After": "1200"})]
    )
    with pytest.raises(ProbeError, match="wait_exceeds_budget"):
        t.fetch(ENDPOINTS[0])
    assert len(calls) == 1 and t.clock() == 0


def test_pagination_shares_global_budget(monkeypatch):
    t, calls = transport(
        monkeypatch,
        [Response(dict(data=[], pagination_key="next"))],
        replace(CONFIG, max_attempts=1),
    )
    with pytest.raises(ProbeError, match="attempt_budget_exhausted"):
        t.fetch(ENDPOINTS[0])
    assert len(calls) == 1


def test_partial_fetch_never_saved_complete(monkeypatch, tmp_path):
    responses = successful_responses()[:1] + [
        Response(dict(data=rows()[:1], pagination_key="next")),
        Response(status=403),
    ]
    t, calls = transport(monkeypatch, responses)
    report = run_probe(
        CONFIG, tmp_path / "probe", live=True, reviewed=True, transport=t
    )
    assert report["attempts"] == 3 and not report["acquisition_complete"]
    files = list((tmp_path / "probe").iterdir())
    assert len(files) == 1  # Completed master only; no partial daily capture.
    assert json.loads(files[0].read_text())["schema"] == "stage7b-master-capture-1"
    assert calls[-1][2]["params"]["pagination_key"] == "next"


def test_offline_real_components_and_immutable_reload(monkeypatch, tmp_path):
    t, _ = transport(monkeypatch, successful_responses())
    report = run_probe(
        CONFIG, tmp_path / "probe", live=True, reviewed=True, transport=t
    )
    assert report["end_reason"] == "data_probe_finished_execution_unsupported"
    assert report["layers"]["persistence"]["status"] == "verified"
    assert report["layers"]["public_view"]["status"] == "verified"
    assert report["layers"]["calendar"]["status"] == "verified"
    assert report["layers"]["execution"]["status"] == "unsupported"
    assert report["layers"]["lot"]["status"] == "blocked"
    assert report["layers"]["input_extension"]["status"] == "unsupported"
    assert not report["live_attempted"]
    store = InputArtifactStore(tmp_path / "probe" / "inputs")
    sha = report["snapshot_hashes"][0]
    assert store.load(sha)
    store.path(sha).write_text(
        "{}"
    )  # Deliberately corrupt only an artificial tmp fixture.
    with pytest.raises(ReplayContractError):
        store.load(sha)


@pytest.mark.parametrize("change", ["null", "duplicate", "symbol", "action"])
def test_bad_or_unsupported_prices_not_promoted(monkeypatch, tmp_path, change):
    responses = successful_responses()
    data = responses[1].payload["data"]
    if change == "null":
        data[0]["O"] = None
    elif change == "duplicate":
        data.append(data[0].copy())
    elif change == "symbol":
        data[0]["Code"] = "OTHER"
    else:
        data[0]["ExRT"] = "1"
    t, _ = transport(monkeypatch, responses)
    result = run_probe(
        CONFIG, tmp_path / "probe", live=True, reviewed=True, transport=t
    )
    assert result["end_reason"] != "data_probe_finished_execution_unsupported"
    assert result["snapshot_hashes"] == []


def test_calendar_denied_does_not_prevent_independent_persistence(
    monkeypatch, tmp_path
):
    responses = successful_responses()
    responses[-1] = Response(status=403)
    t, _ = transport(monkeypatch, responses)
    report = run_probe(
        CONFIG, tmp_path / "probe", live=True, reviewed=True, transport=t
    )
    assert report["layers"]["calendar"] == dict(status="blocked", reason="http_403")
    assert report["layers"]["persistence"]["status"] == "verified"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "not-a-date"])
def test_invalid_retry_after_is_not_ignored(value):
    with pytest.raises(ProbeError, match="invalid_retry_after"):
        retry_after(value)


def test_calendar_missing_date_not_inferred():
    with pytest.raises(ProbeError, match="partial_calendar"):
        calendar_check([dict(Date=START.isoformat(), HolDiv="1")], CONFIG, [START])


def test_elapsed_budget_before_request(monkeypatch):
    t, calls = transport(monkeypatch, [])
    t.clock.now = 1200
    with pytest.raises(ProbeError, match="elapsed_budget_exhausted"):
        t.fetch(ENDPOINTS[0])
    assert not calls


def test_review_gate(monkeypatch, tmp_path):
    t, calls = transport(monkeypatch, [])
    result = run_probe(CONFIG, tmp_path / "probe", live=True, transport=t)
    assert result["end_reason"] == "current_free_window_and_terms_review_required"
    assert not calls


def test_deadline_interrupts_blocking_work():
    started = time.monotonic()
    with pytest.raises(ProbeError, match="elapsed_budget_exhausted"), deadline(0.02):
        time.sleep(1)
    assert time.monotonic() - started < 0.8


def test_conservative_live_window():
    today = date(2024, 10, 1)
    validate_live_window(CONFIG, today)
    with pytest.raises(ProbeError, match="outside_conservative_free_window"):
        validate_live_window(CONFIG, START)
    with pytest.raises(ProbeError, match="outside_conservative_free_window"):
        validate_live_window(CONFIG, date(2030, 1, 1))


def test_successful_pagination_no_extra_request(monkeypatch):
    t, calls = transport(
        monkeypatch,
        [
            Response(dict(data=[{"a": 1}], pagination_key="next")),
            Response(dict(data=[{"a": 2}])),
        ],
    )
    assert t.fetch(ENDPOINTS[0]) == [{"a": 1}, {"a": 2}]
    assert len(calls) == 2 and calls[1][0] >= 13


def test_repeated_pagination_key_rejected(monkeypatch):
    t, calls = transport(
        monkeypatch,
        [
            Response(dict(data=[], pagination_key="next")),
            Response(dict(data=[], pagination_key="next")),
        ],
    )
    with pytest.raises(ProbeError, match="invalid_pagination"):
        t.fetch(ENDPOINTS[0])
    assert len(calls) == 2


def test_request_scope_is_not_expandable(monkeypatch):
    t, calls = transport(monkeypatch, [])
    with pytest.raises(ProbeError, match="request_out_of_scope"):
        t.page(ENDPOINTS[0], {"date": CONFIG.master_date.isoformat()})
    assert not calls


def test_master_mismatch_stops_before_daily(monkeypatch, tmp_path):
    t, calls = transport(
        monkeypatch,
        [
            Response(
                dict(
                    data=[
                        dict(Code="OTHER", Date=START.isoformat(), CoName="ARTIFICIAL")
                    ]
                )
            )
        ],
    )
    result = run_probe(
        CONFIG, tmp_path / "probe", live=True, reviewed=True, transport=t
    )
    assert len(calls) == 1
    assert result["layers"]["instrument"]["status"] == "failed"
    assert not result["acquisition_complete"]


def test_network_exhaustion_is_blocked_not_success(monkeypatch, tmp_path):
    t, calls = transport(monkeypatch, [requests.Timeout("SECRET") for _ in range(3)])
    result = run_probe(
        CONFIG, tmp_path / "probe", live=True, reviewed=True, transport=t
    )
    assert len(calls) == 3 and result["end_reason"] == "retry_exhausted"
    assert result["layers"]["communication"]["status"] == "blocked"
    assert "SECRET" not in json.dumps(result)


def test_data_values_never_leak_into_redacted_report(monkeypatch, tmp_path):
    t, _ = transport(monkeypatch, successful_responses())
    result = run_probe(
        CONFIG, tmp_path / "probe", live=True, reviewed=True, transport=t
    )
    encoded = json.dumps(result)
    for forbidden in (
        "ARTIFICIAL COMPANY",
        "artificial-noncredential",
        str(tmp_path),
        '"raw_open"',
        '"values"',
    ):
        assert forbidden not in encoded
