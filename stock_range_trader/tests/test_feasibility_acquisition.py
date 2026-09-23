"""Offline tests for the date-scoped feasibility acquisition ledger (temp dirs only)."""

from __future__ import annotations

import ast
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from feasibility import paths as paths_module
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
    FixtureFailure,
    OfflineFixtureTransport,
    TransportResponse,
    fixture_key,
    load_completed_rows,
    run_offline_fixture_acquisition,
    summarize,
)
from feasibility.paths import (
    UnsafeOutputPath,
    create_exclusive_dir,
    require_existing_store_dir,
    require_new_output_dir,
)

PACKAGE = Path(__file__).resolve().parents[1] / "feasibility"
CAL_PARAMS = {"from": "2025-03-03", "to": "2025-03-04"}


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


def ok(payload: bytes) -> TransportResponse:
    return TransportResponse(200, payload)


class FakeClock:
    def __init__(self, overshoot: float = 0) -> None:
        self.now = datetime(2026, 9, 24, tzinfo=UTC)
        self.sleeps: list[float] = []
        self.overshoot = overshoot

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds + self.overshoot)


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


def keyed(endpoint: str, day: str | None = None, page: str | None = None) -> tuple:
    params = dict(CAL_PARAMS) if endpoint == CALENDAR else {"date": day}
    if page is not None:
        params["pagination_key"] = page
    return fixture_key(endpoint, params)


def healthy() -> dict:
    return {
        keyed(CALENDAR): [
            ok(
                body(
                    [
                        {"Date": "2025-03-03", "HolDiv": "1"},
                        {"Date": "2025-03-04", "HolDiv": "1"},
                    ]
                )
            )
        ],
        keyed(MASTER, "2025-03-04"): [
            ok(
                body(
                    [
                        master_row("10010", "2025-03-04"),
                        master_row("10050", "2025-03-04"),
                    ]
                )
            )
        ],
        keyed(DAILY, "2025-03-03"): [
            ok(body([daily_row("10010", "2025-03-03")], key="page-2"))
        ],
        keyed(DAILY, "2025-03-03", "page-2"): [
            ok(body([daily_row("10050", "2025-03-03", close=None)]))
        ],
        keyed(DAILY, "2025-03-04"): [
            ok(
                body(
                    [daily_row("10010", "2025-03-04"), daily_row("10050", "2025-03-04")]
                )
            )
        ],
    }


def fixture(responses: dict | None = None) -> OfflineFixtureTransport:
    return OfflineFixtureTransport(healthy() if responses is None else responses)


def run(store, transport, clock) -> object:
    return AcquisitionRunner(store, transport, clock=clock, sleep=clock.sleep).run()


# --------------------------------------------------------------- happy path


def test_paginated_acquisition_records_market_date_and_receipt_separately(tmp_path):
    clock = FakeClock()
    summary = run_offline_fixture_acquisition(
        tmp_path / "acq", plan(), fixture(), clock=clock, sleep=clock.sleep
    )
    assert (summary.status, summary.requests, summary.pages, summary.rows) == (
        "completed",
        5,
        5,
        8,
    )
    store = AcquisitionStore.open(tmp_path / "acq", plan())
    pages = [r for r in store.verify() if r["type"] == "page"]
    daily = [p for p in pages if p["endpoint"] == DAILY]
    assert daily[0]["market_params"] == {"date": "2025-03-03"}
    assert daily[0]["received_at"].startswith("2026-09-24")
    assert daily[1]["null_price_rows"] == 1
    assert all(wait >= 13 for wait in clock.sleeps)
    rows = load_completed_rows(store)
    assert [r["Code"] for r in rows.daily if r["C"] is None] == ["10050"]
    assert rows.acquired_daily_dates == ("2025-03-03", "2025-03-04")


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
    with pytest.raises(AcquisitionStopped) as stopped:
        run_offline_fixture_acquisition(
            tmp_path / "acq",
            plan(**override),
            fixture(),
            clock=clock,
            sleep=clock.sleep,
        )
    assert stopped.value.reason == reason and stopped.value.terminal
    with pytest.raises(AcquisitionStopped, match="terminal_stop_recorded"):
        run_offline_fixture_acquisition(
            tmp_path / "acq",
            plan(**override),
            fixture(),
            clock=clock,
            sleep=clock.sleep,
            resume=True,
        )


def test_transient_failures_stop_resumably_and_resume_keeps_budget(tmp_path):
    clock = FakeClock()
    responses = healthy()
    responses[keyed(MASTER, "2025-03-04")] = [
        TransportResponse(503, b"{}"),
        FixtureFailure("ConnectionError"),
        TransportResponse(429, b"{}"),
    ]
    with pytest.raises(AcquisitionStopped) as stopped:
        run_offline_fixture_acquisition(
            tmp_path / "acq", plan(), fixture(responses), clock=clock, sleep=clock.sleep
        )
    assert stopped.value.reason == "transient_failures_exhausted"
    assert not stopped.value.terminal

    transport = fixture()
    summary = run_offline_fixture_acquisition(
        tmp_path / "acq", plan(), transport, clock=clock, sleep=clock.sleep, resume=True
    )
    assert summary.status == "completed"
    assert summary.requests == 1 + 3 + 4
    assert all(call[0] != CALENDAR for call in transport.calls)
    assert "OSError" in (tmp_path / "acq" / "ledger.jsonl").read_text()


# ------------------------------------------------ finding 1: output protection


def test_output_inside_another_git_checkout_is_refused(tmp_path):
    other = tmp_path / "june_limited_trial_worktree"  # no protected names needed
    (other / "stock_range_trader").mkdir(parents=True)
    (other / ".git").write_text("gitdir: /elsewhere\n")
    with pytest.raises(UnsafeOutputPath, match="other_git_checkout"):
        AcquisitionStore.create(other / "stock_range_trader" / "acq", plan())
    assert not (other / "stock_range_trader" / "acq").exists()


def test_output_in_a_tree_holding_trial_evidence_is_refused(tmp_path):
    project = tmp_path / "project"
    (project / ".delayed_replay").mkdir(parents=True)
    with pytest.raises(UnsafeOutputPath, match="trial_evidence_tree"):
        require_new_output_dir(project / "outputs" / "acq")
    with pytest.raises(UnsafeOutputPath, match="trial_evidence_tree"):
        require_new_output_dir(project / ".delayed_replay" / "acq")


@pytest.mark.parametrize(
    ("make_path", "reason"),
    [
        (lambda tmp: Path("relative") / "acq", "must_be_absolute"),
        (lambda tmp: tmp / "a" / ".." / "acq", "parent_reference"),
        (lambda tmp: Path.home() / "feasibility-not-allowed-probe", "outside_allowed"),
        (lambda tmp: tmp / ".hidden", "name_not_allowed"),
    ],
)
def test_relative_parent_outside_and_hidden_paths_are_refused(
    tmp_path, make_path, reason
):
    with pytest.raises(UnsafeOutputPath, match=reason):
        require_new_output_dir(make_path(tmp_path))


def test_symlink_alias_into_protected_tree_is_refused(tmp_path):
    other = tmp_path / "other_checkout"
    other.mkdir()
    (other / ".git").mkdir()
    alias = tmp_path / "innocent_alias"
    alias.symlink_to(other)
    with pytest.raises(UnsafeOutputPath, match="symlink_or_alias"):
        require_new_output_dir(alias / "acq")


def test_allowed_roots_can_only_narrow_defaults(tmp_path):
    with pytest.raises(UnsafeOutputPath, match="must_narrow_default_roots"):
        require_new_output_dir(tmp_path / "acq", allowed_roots=[Path.home()])
    narrow = tmp_path / "narrow"
    narrow.mkdir()
    with pytest.raises(UnsafeOutputPath, match="outside_allowed_roots"):
        require_new_output_dir(tmp_path / "acq", allowed_roots=[narrow])
    assert require_new_output_dir(narrow / "acq", allowed_roots=[narrow])


def test_resume_applies_the_same_location_checks(tmp_path):
    clock = FakeClock()
    run_offline_fixture_acquisition(
        tmp_path / "acq", plan(), fixture(), clock=clock, sleep=clock.sleep
    )
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "acq")
    with pytest.raises(UnsafeOutputPath, match="symlink_or_alias"):
        AcquisitionStore.open(alias, plan())
    other = tmp_path / "checkout"
    other.mkdir()
    (other / ".git").mkdir()
    os.rename(tmp_path / "acq", other / "acq")
    with pytest.raises(UnsafeOutputPath, match="other_git_checkout"):
        require_existing_store_dir(other / "acq")


def test_directory_swapped_during_creation_is_detected(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def swapped_mkdir(path, *args, **kwargs):
        os.symlink(elsewhere, path)

    monkeypatch.setattr(paths_module.os, "mkdir", swapped_mkdir)
    with pytest.raises(UnsafeOutputPath, match="changed_after_check"):
        create_exclusive_dir(tmp_path / "acq")


def test_existing_output_or_store_is_never_reused_by_create(tmp_path):
    AcquisitionStore.create(tmp_path / "acq", plan())
    with pytest.raises(UnsafeOutputPath, match="already_exists"):
        AcquisitionStore.create(tmp_path / "acq", plan())


# ------------------------------------------------ finding 2: offline transport only


class SpoofedTransport:
    kind = "offline_fixture"

    def fetch(self, endpoint, params):  # pragma: no cover - must never be called
        raise AssertionError("spoofed transport reached")


class SubclassedFixture(OfflineFixtureTransport):
    __slots__ = ()


def test_spoofed_transport_is_refused_before_store_creation(tmp_path):
    for impostor in (SpoofedTransport(), SubclassedFixture({})):
        with pytest.raises(AcquisitionStopped, match="only_offline_fixture"):
            run_offline_fixture_acquisition(
                tmp_path / "acq",
                plan(),
                impostor,
                clock=FakeClock(),
                sleep=lambda s: None,
            )
        assert not (tmp_path / "acq").exists()
    store = AcquisitionStore.create(tmp_path / "acq", plan())
    with pytest.raises(AcquisitionStopped, match="only_offline_fixture"):
        AcquisitionRunner(store, SpoofedTransport(), clock=FakeClock(), sleep=print)
    assert (tmp_path / "acq" / "ledger.jsonl").read_text() == ""


def test_fixture_cannot_carry_callables_or_instance_overrides():
    with pytest.raises(AcquisitionError, match="response_values"):
        OfflineFixtureTransport({keyed(MASTER, "2025-03-04"): [lambda: None]})
    transport = fixture()
    with pytest.raises(AttributeError):
        transport.fetch = lambda *a: None  # type: ignore[method-assign]


# ------------------------------------------------ finding 3: time budget


def test_overrunning_sleep_stops_before_the_next_fetch(tmp_path):
    clock = FakeClock(overshoot=100)
    transport = fixture()
    with pytest.raises(AcquisitionStopped, match="time_budget_exhausted"):
        run_offline_fixture_acquisition(
            tmp_path / "acq",
            plan(max_seconds=60),
            transport,
            clock=clock,
            sleep=clock.sleep,
        )
    assert len(transport.calls) == 1


# ------------------------------------------------ finding 4: typed schema checks


@pytest.mark.parametrize(
    ("key", "rows", "reason"),
    [
        (keyed(MASTER, "2025-03-04"), [dict(master_row("10010", "2025-03-04"), Code=[])], "Code"),
        (keyed(MASTER, "2025-03-04"), [dict(master_row("7203", "2025-03-04"))], "Code"),
        (keyed(MASTER, "2025-03-04"), [dict(master_row("10010", "2025-03-04"), Mkt=112)], "Mkt"),
        (keyed(CALENDAR), [{"Date": "2025-03-3", "HolDiv": "1"}], "Date"),
        (keyed(CALENDAR), [{"Date": "2025-03-03", "HolDiv": "9"}], "HolDiv"),
        (keyed(CALENDAR), [{"Date": ["2025-03-03"], "HolDiv": "1"}], "Date"),
        (keyed(DAILY, "2025-03-04"), [dict(daily_row("10010", "2025-03-04"), O="100")], "O"),
        (keyed(DAILY, "2025-03-04"), [dict(daily_row("10010", "2025-03-04"), C=-1)], "C"),
        (keyed(DAILY, "2025-03-04"), [dict(daily_row("10010", "2025-03-04"), Vo=True)], "Vo"),
        (keyed(DAILY, "2025-03-04"), [dict(daily_row("10010", "2025-03-04"), AdjFactor=0)], "AdjFactor"),
        (keyed(DAILY, "2025-03-04"), [dict(daily_row("10010", "2025-03-04"), ExRT=1)], "ExRT"),
    ],
)  # fmt: skip
def test_malformed_values_become_recorded_safe_stops(tmp_path, key, rows, reason):
    responses = healthy()
    responses[key] = [ok(body(rows))]
    with pytest.raises(AcquisitionStopped) as stopped:
        run_offline_fixture_acquisition(
            tmp_path / "acq",
            plan(),
            fixture(responses),
            clock=FakeClock(),
            sleep=lambda s: None,
        )
    assert stopped.value.reason == f"invalid_response_value:{reason}"
    last = AcquisitionStore.open(tmp_path / "acq", plan()).verify()[-1]
    assert (last["type"], last["reason"], last["terminal"]) == (
        "stop",
        stopped.value.reason,
        True,
    )


@pytest.mark.parametrize(
    ("key", "payload", "reason"),
    [
        (keyed(DAILY, "2025-03-03", "page-2"), body([daily_row("10050", "2025-03-03")], key="page-2"), "pagination_key_loop"),
        (keyed(DAILY, "2025-03-03", "page-2"), body([daily_row("10010", "2025-03-03")]), "duplicate_row"),
        (keyed(DAILY, "2025-03-04"), body([daily_row("10010", "2025-03-05")]), "row_market_date_mismatch"),
        (keyed(MASTER, "2025-03-04"), body([{"Date": "2025-03-04"}]), "unsupported_response_schema"),
        (keyed(DAILY, "2025-03-03", "page-2"), body([], key="page-3"), "empty_page_with_continuation"),
        (keyed(MASTER, "2025-03-04"), b"not json", "invalid_json_response"),
    ],
)  # fmt: skip
def test_contract_violations_stop_safely(tmp_path, key, payload, reason):
    responses = healthy()
    responses[key] = [ok(payload)]
    with pytest.raises(AcquisitionStopped) as stopped:
        run_offline_fixture_acquisition(
            tmp_path / "acq",
            plan(),
            fixture(responses),
            clock=FakeClock(),
            sleep=lambda s: None,
        )
    assert stopped.value.reason == reason and stopped.value.terminal


def test_http_403_is_terminal(tmp_path):
    responses = healthy()
    responses[keyed(MASTER, "2025-03-04")] = [TransportResponse(403, b"{}")]
    with pytest.raises(AcquisitionStopped, match="not_authorized_or_outside_plan"):
        run_offline_fixture_acquisition(
            tmp_path / "acq",
            plan(),
            fixture(responses),
            clock=FakeClock(),
            sleep=lambda s: None,
        )


# ------------------------------------------------ finding 5: completion and crashes


def complete(tmp_path) -> Path:
    clock = FakeClock()
    run_offline_fixture_acquisition(
        tmp_path / "acq", plan(), fixture(), clock=clock, sleep=clock.sleep
    )
    return tmp_path / "acq"


def test_rerunning_a_completed_plan_adds_no_completion_event(tmp_path):
    root = complete(tmp_path)
    before = AcquisitionStore.open(root, plan()).verify()
    summary = run_offline_fixture_acquisition(
        root, plan(), fixture({}), clock=FakeClock(), sleep=lambda s: None, resume=True
    )
    after = AcquisitionStore.open(root, plan()).verify()
    assert summary.status == "completed"
    assert after == before
    assert sum(r["type"] == "completed" for r in after) == 1


def crash_on(store: AcquisitionStore, record_type: str, nth: int = 1) -> None:
    original = store.append
    seen = {"n": 0}

    def append(record):
        if record["type"] == record_type:
            seen["n"] += 1
            if seen["n"] == nth:
                raise RuntimeError("simulated crash")
        return original(record)

    store.append = append  # type: ignore[method-assign]


def test_crash_between_body_save_and_page_record_is_recovered(tmp_path):
    store = AcquisitionStore.create(tmp_path / "acq", plan())
    crash_on(store, "page", nth=2)  # master body saved, page record lost
    with pytest.raises(RuntimeError, match="simulated crash"):
        run(store, fixture(), FakeClock())
    reopened = AcquisitionStore.open(tmp_path / "acq", plan())
    assert len(reopened.orphans()) == 1
    orphan_size = next(iter(reopened.orphans().values()))

    summary = run(reopened, fixture(), FakeClock())
    records = reopened.verify()
    orphan_records = [r for r in records if r["type"] == "orphan_detected"]
    assert [r["bytes"] for r in orphan_records] == [orphan_size]
    master_pages = [
        r for r in records if r["type"] == "page" and r["endpoint"] == MASTER
    ]
    assert len(master_pages) == 1  # identical body reused, not duplicated
    assert summary.status == "completed" and summary.orphan_bodies == 0


def test_orphan_bytes_count_against_the_storage_budget(tmp_path):
    store = AcquisitionStore.create(tmp_path / "acq", plan(max_bytes=700))
    crash_on(store, "page", nth=2)
    with pytest.raises(RuntimeError):
        run(store, fixture(), FakeClock())
    orphan = tmp_path / "acq" / "responses"
    # A different, larger orphan (e.g. an abandoned page) must still consume budget.
    extra = b'{"data": []' + b" " * 400 + b"}"
    import hashlib

    (orphan / f"{hashlib.sha256(extra).hexdigest()}.json").write_bytes(extra)
    reopened = AcquisitionStore.open(tmp_path / "acq", plan(max_bytes=700))
    with pytest.raises(AcquisitionStopped, match="storage_budget_exceeded"):
        run(reopened, fixture(), FakeClock())


def test_crash_after_final_page_completes_without_refetch(tmp_path):
    store = AcquisitionStore.create(tmp_path / "acq", plan())
    crash_on(store, "query_complete", nth=1)  # calendar page recorded, completion lost
    with pytest.raises(RuntimeError):
        run(store, fixture(), FakeClock())
    transport = fixture()
    run(AcquisitionStore.open(tmp_path / "acq", plan()), transport, FakeClock())
    assert all(call[0] != CALENDAR for call in transport.calls)


def test_tampered_store_contents_are_refused_on_resume(tmp_path):
    root = complete(tmp_path)
    with pytest.raises(AcquisitionError, match="plan_mismatch_on_resume"):
        AcquisitionStore.open(root, plan(max_requests=21))

    page = next(
        r
        for r in AcquisitionStore.open(root, plan()).verify()
        if r["type"] == "page" and r["endpoint"] == MASTER
    )
    response = root / "responses" / f"{page['body_sha256']}.json"
    original = response.read_bytes()
    response.write_bytes(original.replace(b"Artificial", b"Changed!!!"))
    with pytest.raises(AcquisitionError, match="response_file_changed"):
        AcquisitionStore.open(root, plan())
    response.write_bytes(original)

    (root / "responses" / "notes.txt").write_text("stray")
    with pytest.raises(AcquisitionError, match="invalid_file"):
        AcquisitionStore.open(root, plan())
    (root / "responses" / "notes.txt").unlink()

    ledger = root / "ledger.jsonl"
    text = ledger.read_text()
    ledger.write_text(text.replace('"row_count":2', '"row_count":3', 1))
    with pytest.raises(AcquisitionError, match="ledger_chain_broken"):
        AcquisitionStore.open(root, plan())
    ledger.write_text(text)
    assert AcquisitionStore.open(root, plan())  # normal resume path intact


def test_saved_bodies_are_never_overwritten_or_followed(tmp_path):
    import hashlib

    store = AcquisitionStore.create(tmp_path / "acq", plan())
    payload = b'{"data": []}'
    target = (
        tmp_path / "acq" / "responses" / f"{hashlib.sha256(payload).hexdigest()}.json"
    )
    target.write_bytes(b"different content")
    with pytest.raises(AcquisitionError, match="response_file_conflict"):
        store.save_body(payload)
    assert target.read_bytes() == b"different content"

    root = complete(tmp_path / "second")
    page = next(
        r for r in AcquisitionStore.open(root, plan()).verify() if r["type"] == "page"
    )
    body_path = root / "responses" / f"{page['body_sha256']}.json"
    copy = tmp_path / "copy.json"
    copy.write_bytes(body_path.read_bytes())
    body_path.unlink()
    body_path.symlink_to(copy)
    with pytest.raises(AcquisitionError, match="invalid_file"):
        AcquisitionStore.open(root, plan())


# ------------------------------------------------ secrets and structure


def test_api_key_never_reaches_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("JQUANTS_API_KEY", "sk-artificial-secret-value")
    root = complete(tmp_path)
    for path in root.rglob("*"):
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


def test_summary_is_available_for_an_empty_store(tmp_path):
    store = AcquisitionStore.create(tmp_path / "acq", plan())
    assert summarize(store).requests == 0


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
