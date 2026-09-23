"""Offline tests for the date-scoped feasibility acquisition ledger."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from feasibility.acquisition import (
    CALENDAR,
    DAILY,
    MASTER,
    AcquisitionError,
    AcquisitionLimits,
    AcquisitionPlan,
    AcquisitionRunner,
    AcquisitionStopped,
    AcquisitionStore,
    DateQuery,
    TransportResponse,
    load_completed_rows,
    summarize,
)
from feasibility.paths import UnsafeOutputPath

PACKAGE = Path(__file__).resolve().parents[1] / "feasibility"


def master_row(code: str, day: str) -> dict:
    return {
        "Date": day,
        "Code": code,
        "CoName": f"Artificial {code}",
        "Mkt": "0112",
        "MktNm": "Standard",
        "S17": "1",
        "S17Nm": "S",
        "S33": "0050",
        "S33Nm": "S",
        "ProdCat": "011",
    }


def daily_row(code: str, day: str, close: float | None = 100.0) -> dict:
    price = None if close is None else close
    volume = 0 if close is None else 1000
    return {
        "Date": day,
        "Code": code,
        "O": price,
        "H": price,
        "L": price,
        "C": price,
        "Vo": volume,
        "Va": None if close is None else close * volume,
        "AdjFactor": 1,
        "AdjO": price,
        "AdjH": price,
        "AdjL": price,
        "AdjC": price,
        "AdjVo": volume,
    }


def body(rows: list[dict], key: str | None = None) -> bytes:
    payload = {"data": rows}
    if key is not None:
        payload["pagination_key"] = key
    return json.dumps(payload).encode()


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 24, tzinfo=UTC)
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


class FakeTransport:
    kind = "offline_fixture"

    def __init__(self, responses: dict) -> None:
        # key: (endpoint, date-or-from, pagination_key) -> list of responses/exceptions
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[tuple[str, dict]] = []

    def fetch(self, endpoint, params):
        self.calls.append((endpoint, dict(params)))
        key = (
            endpoint,
            params.get("date") or params.get("from"),
            params.get("pagination_key"),
        )
        item = self.responses[key].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(payload: bytes) -> TransportResponse:
    return TransportResponse(200, payload)


def limits(**overrides) -> AcquisitionLimits:
    values = dict(
        max_requests=20,
        max_seconds=1200,
        max_bytes=1_000_000,
        max_pages_per_query=5,
        max_attempts_per_page=3,
        min_interval_seconds=13,
    )
    values.update(overrides)
    return AcquisitionLimits(**values)


def plan(**overrides) -> AcquisitionPlan:
    return AcquisitionPlan(
        label="artificial-census-plan",
        queries=(
            DateQuery(CALENDAR, start="2025-03-03", end="2025-03-04"),
            DateQuery(MASTER, market_date="2025-03-04"),
            DateQuery(DAILY, market_date="2025-03-03"),
            DateQuery(DAILY, market_date="2025-03-04"),
        ),
        limits=limits(**overrides),
    )


def healthy_responses() -> dict:
    return {
        (CALENDAR, "2025-03-03", None): [
            ok(
                body(
                    [
                        {"Date": "2025-03-03", "HolDiv": "1"},
                        {"Date": "2025-03-04", "HolDiv": "1"},
                    ]
                )
            )
        ],
        (MASTER, "2025-03-04", None): [
            ok(
                body(
                    [
                        master_row("10010", "2025-03-04"),
                        master_row("10050", "2025-03-04"),
                    ]
                )
            )
        ],
        (DAILY, "2025-03-03", None): [
            ok(body([daily_row("10010", "2025-03-03")], key="page-2")),
        ],
        (DAILY, "2025-03-03", "page-2"): [
            ok(body([daily_row("10050", "2025-03-03", close=None)])),
        ],
        (DAILY, "2025-03-04", None): [
            ok(
                body(
                    [daily_row("10010", "2025-03-04"), daily_row("10050", "2025-03-04")]
                )
            )
        ],
    }


def run(store, transport, clock) -> object:
    return AcquisitionRunner(store, transport, clock=clock, sleep=clock.sleep).run()


def test_paginated_acquisition_records_market_date_and_receipt_separately(tmp_path):
    clock = FakeClock()
    store = AcquisitionStore.create(tmp_path / "acq", plan())
    summary = run(store, FakeTransport(healthy_responses()), clock)

    assert (summary.status, summary.requests, summary.pages, summary.rows) == (
        "completed",
        5,
        5,
        8,
    )
    pages = [r for r in store.verify() if r["type"] == "page"]
    daily = [p for p in pages if p["endpoint"] == DAILY]
    assert daily[0]["market_params"] == {"date": "2025-03-03"}
    assert daily[0]["received_at"].startswith("2026-09-24")
    assert daily[1]["null_price_rows"] == 1
    assert all(len(p["body_sha256"]) == 64 for p in pages)
    assert all(wait >= 13 for wait in clock.sleeps)

    rows = load_completed_rows(store)
    null_rows = [r for r in rows.daily if r["C"] is None]
    assert [r["Code"] for r in null_rows] == ["10050"]  # kept, not dropped
    assert rows.acquired_daily_dates == ("2025-03-03", "2025-03-04")
    assert rows.provenance["value_claim"] == (
        "provider_values_at_retrieval_not_original_snapshot"
    )


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"max_requests": 4}, "request_budget_exhausted"),
        ({"max_bytes": 600}, "storage_budget_exceeded"),
        ({"max_seconds": 30}, "time_budget_exhausted"),
        ({"max_pages_per_query": 1}, "page_limit_exceeded"),
    ],
)
def test_budgets_stop_terminally_and_cannot_be_resumed(tmp_path, override, reason):
    clock = FakeClock()
    store = AcquisitionStore.create(tmp_path / "acq", plan(**override))
    with pytest.raises(AcquisitionStopped) as stopped:
        run(store, FakeTransport(healthy_responses()), clock)
    assert stopped.value.reason == reason and stopped.value.terminal
    resumed = AcquisitionStore.open(tmp_path / "acq", plan(**override))
    with pytest.raises(AcquisitionStopped, match="terminal_stop_recorded"):
        run(resumed, FakeTransport(healthy_responses()), clock)


def test_transient_failures_stop_resumably_and_resume_keeps_budget(tmp_path):
    clock = FakeClock()
    responses = healthy_responses()
    responses[(MASTER, "2025-03-04", None)] = [
        TransportResponse(503, b"{}"),
        ConnectionError("secret-looking message must not be stored"),
        TransportResponse(429, b"{}"),
    ]
    store = AcquisitionStore.create(tmp_path / "acq", plan())
    with pytest.raises(AcquisitionStopped) as stopped:
        run(store, FakeTransport(responses), clock)
    assert stopped.value.reason == "transient_failures_exhausted"
    assert not stopped.value.terminal

    transport = FakeTransport(healthy_responses())
    resumed = AcquisitionStore.open(tmp_path / "acq", plan())
    summary = run(resumed, transport, clock)
    assert summary.status == "completed"
    assert summary.requests == 1 + 3 + 4  # calendar, 3 failed master, 4 remaining
    assert all(
        call[0] != CALENDAR for call in transport.calls
    )  # completed query reused
    ledger = (tmp_path / "acq" / "ledger.jsonl").read_text()
    assert "secret-looking" not in ledger
    assert "ConnectionError" in ledger


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda r: r.__setitem__(
                (DAILY, "2025-03-03", "page-2"),
                [ok(body([daily_row("10050", "2025-03-03")], key="page-2"))],
            ),
            "pagination_key_loop",
        ),
        (
            lambda r: r.__setitem__(
                (DAILY, "2025-03-03", "page-2"),
                [ok(body([daily_row("10010", "2025-03-03")]))],
            ),
            "duplicate_row",
        ),
        (
            lambda r: r.__setitem__(
                (DAILY, "2025-03-04", None),
                [ok(body([daily_row("10010", "2025-03-05")]))],
            ),
            "row_market_date_mismatch",
        ),
        (
            lambda r: r.__setitem__(
                (MASTER, "2025-03-04", None), [ok(body([{"Date": "2025-03-04"}]))]
            ),
            "unsupported_response_schema",
        ),
        (
            lambda r: r.__setitem__(
                (DAILY, "2025-03-03", "page-2"), [ok(body([], key="page-3"))]
            ),
            "empty_page_with_continuation",
        ),
        (
            lambda r: r.__setitem__(
                (MASTER, "2025-03-04", None), [TransportResponse(403, b"{}")]
            ),
            "not_authorized_or_outside_plan",
        ),
    ],
)
def test_contract_violations_stop_safely(tmp_path, mutate, reason):
    responses = healthy_responses()
    mutate(responses)
    store = AcquisitionStore.create(tmp_path / "acq", plan())
    with pytest.raises(AcquisitionStopped) as stopped:
        run(store, FakeTransport(responses), FakeClock())
    assert stopped.value.reason == reason and stopped.value.terminal
    assert store.verify()[-1]["type"] == "stop"


def test_resume_rejects_changed_plan_body_or_ledger(tmp_path):
    clock = FakeClock()
    store = AcquisitionStore.create(tmp_path / "acq", plan())
    run(store, FakeTransport(healthy_responses()), clock)

    with pytest.raises(AcquisitionError, match="plan_mismatch_on_resume"):
        AcquisitionStore.open(tmp_path / "acq", plan(max_requests=21))

    page = next(
        r for r in store.verify() if r["type"] == "page" and r["endpoint"] == MASTER
    )
    response = tmp_path / "acq" / "responses" / f"{page['body_sha256']}.json"
    original = response.read_bytes()
    tampered = original.replace(b"Artificial", b"Changed!!!")
    assert tampered != original
    response.write_bytes(tampered)
    with pytest.raises(AcquisitionError, match="response_file_changed"):
        AcquisitionStore.open(tmp_path / "acq", plan())
    response.write_bytes(original)

    ledger = tmp_path / "acq" / "ledger.jsonl"
    ledger.write_text(ledger.read_text().replace('"row_count":2', '"row_count":3', 1))
    with pytest.raises(AcquisitionError, match="ledger_chain_broken"):
        AcquisitionStore.open(tmp_path / "acq", plan())


def test_network_transport_is_refused_before_any_ledger_write(tmp_path):
    class NetworkTransport:
        kind = "https"

        def fetch(self, endpoint, params):  # pragma: no cover - must not be called
            raise AssertionError("network must not be reached")

    store = AcquisitionStore.create(tmp_path / "acq", plan())
    with pytest.raises(AcquisitionStopped, match="network_transport_not_enabled"):
        run(store, NetworkTransport(), FakeClock())
    assert (tmp_path / "acq" / "ledger.jsonl").read_text() == ""
    assert summarize(store).requests == 0


def test_api_key_never_reaches_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("JQUANTS_API_KEY", "sk-artificial-secret-value")
    store = AcquisitionStore.create(tmp_path / "acq", plan())
    run(store, FakeTransport(healthy_responses()), FakeClock())
    for path in (tmp_path / "acq").rglob("*"):
        if path.is_file():
            assert b"sk-artificial-secret-value" not in path.read_bytes()


def test_plan_rejects_code_scoped_or_malformed_queries():
    with pytest.raises(AcquisitionError, match="unsupported_endpoint"):
        DateQuery("/equities/bars/minute", market_date="2025-03-04")
    with pytest.raises(AcquisitionError, match="market_date_must_be_iso_date"):
        DateQuery(DAILY, market_date="20250304")
    with pytest.raises(AcquisitionError, match="calendar_range_reversed"):
        DateQuery(CALENDAR, start="2025-03-05", end="2025-03-04")
    with pytest.raises(AcquisitionError, match="duplicate_plan_query"):
        AcquisitionPlan(
            "dup", (DateQuery(DAILY, market_date="2025-03-04"),) * 2, limits()
        )
    assert "code" not in DateQuery(DAILY, market_date="2025-03-04").params()


def test_store_refuses_protected_or_existing_output(tmp_path):
    june = tmp_path / "repo" / "stock_range_trader" / ".delayed_replay" / "june_trial"
    with pytest.raises(UnsafeOutputPath, match="protected_trial_area"):
        AcquisitionStore.create(june / "acq", plan())
    with pytest.raises(UnsafeOutputPath, match="forbidden_root"):
        AcquisitionStore.create(
            tmp_path / "june_worktree" / "out",
            plan(),
            forbidden_roots=[tmp_path / "june_worktree"],
        )
    AcquisitionStore.create(tmp_path / "acq", plan())
    with pytest.raises(UnsafeOutputPath, match="already_exists"):
        AcquisitionStore.create(tmp_path / "acq", plan())


def test_feasibility_package_has_no_network_client_or_order_code():
    forbidden = {
        "aiohttp",
        "httpx",
        "requests",
        "socket",
        "urllib",
        "http",
        "websocket",
    }
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            assert not set(names) & forbidden, f"{path.name}: {names}"
            if isinstance(node, ast.FunctionDef):
                assert node.name not in {"place_order", "send_order", "submit_order"}
