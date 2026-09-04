"""Offline J-Quants API V2 provider tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from phase2_helpers import jquants_bar_record, master_frame

from data.providers import (
    JQuantsV2Provider,
    ProviderAuthenticationError,
    ProviderDownloadError,
    jquants_daily_to_canonical,
)


class FakeHttpError(RuntimeError):
    def __init__(self, status: int, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP {status}")
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        self.response = SimpleNamespace(status_code=status, headers=headers)


def test_api_key_is_required_when_no_transport_is_injected(monkeypatch) -> None:
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)

    with pytest.raises(ProviderAuthenticationError, match="JQUANTS_API_KEY"):
        JQuantsV2Provider()


def test_v2_daily_response_maps_to_canonical_fields() -> None:
    fetched_at = datetime(2026, 9, 1, tzinfo=UTC)
    record = jquants_bar_record()
    record["AdjFactor"] = 0.5
    record["AdjC"] = 52.5
    frame = jquants_daily_to_canonical([record], fetched_at=fetched_at)

    assert list(frame["provider"]) == ["jquants"]
    assert frame.loc[0, "symbol"] == "72030"
    assert frame.loc[0, "turnover_value"] == 105_000.0
    assert frame.loc[0, "adjusted_close"] == 52.5
    assert frame.loc[0, "adjustment_factor"] == 0.5
    assert frame.loc[0, "stock_split"] == 0.0
    assert frame.loc[0, "dividend"] == 0.0


def test_pagination_is_sequential_and_rate_limited() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    sleeps: list[float] = []
    clock_value = [0.0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock_value[0] += seconds

    def request(path, params, timeout):
        del timeout
        calls.append((path, dict(params)))
        if "pagination_key" not in params:
            return {"data": [master_frame().iloc[0].to_dict()], "pagination_key": "p2"}
        return {"data": [master_frame().iloc[1].to_dict()]}

    provider = JQuantsV2Provider(
        request_func=request,
        min_request_interval_seconds=13,
        sleep_func=sleep,
        clock=lambda: clock_value[0],
    )
    result = provider.get_universe(date(2026, 8, 31))

    assert list(result["Code"]) == ["130A0", "72030"]
    assert calls[0][0] == "/equities/master"
    assert calls[1][1]["pagination_key"] == "p2"
    assert sleeps == [13.0]


def test_429_prefers_retry_after_before_retrying() -> None:
    attempts = 0
    sleeps: list[float] = []
    clock_value = [0.0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock_value[0] += seconds

    def request(path, params, timeout):
        nonlocal attempts
        del path, params, timeout
        attempts += 1
        if attempts == 1:
            raise FakeHttpError(429, "7")
        return {"data": [jquants_bar_record()]}

    provider = JQuantsV2Provider(
        request_func=request,
        min_request_interval_seconds=12,
        max_retries=2,
        sleep_func=sleep,
        clock=lambda: clock_value[0],
        random_func=lambda: 0.75,
    )
    result = provider.get_daily_bars(["72030"], date(2026, 8, 1), date(2026, 9, 1))

    assert len(result) == 1
    assert attempts == 2
    assert sleeps[0] == 7.0
    assert sum(sleeps) == 12.0


def test_maximum_retry_failure_is_recorded_per_symbol() -> None:
    calls = 0

    def request(path, params, timeout):
        nonlocal calls
        del path, params, timeout
        calls += 1
        raise FakeHttpError(503)

    provider = JQuantsV2Provider(
        request_func=request,
        min_request_interval_seconds=12,
        max_retries=3,
        sleep_func=lambda seconds: None,
        clock=lambda: 0.0,
        random_func=lambda: 0.0,
    )
    result = provider.get_daily_bars(["72030"], date(2026, 8, 1), date(2026, 9, 1))

    assert result.empty
    assert calls == 3
    assert provider.issues[0].status == "download_failed"
    assert "3 attempts" in provider.issues[0].message


def test_empty_daily_response_and_calendar_are_handled() -> None:
    responses = [
        {"data": []},
        {"data": [{"Date": "2026-08-31", "HolDiv": "1"}]},
    ]
    provider = JQuantsV2Provider(
        request_func=lambda path, params, timeout: responses.pop(0),
        min_request_interval_seconds=12,
        sleep_func=lambda seconds: None,
        clock=lambda: 0.0,
    )

    bars = provider.get_daily_bars(["72030"], date(2026, 8, 1), date(2026, 9, 1))
    calendar = provider.get_trading_calendar(date(2026, 8, 31), date(2026, 9, 1))

    assert bars.empty
    assert provider.issues[0].status == "empty_response"
    assert list(calendar.columns) == ["Date", "HolDiv"]


def test_repeated_pagination_key_is_rejected() -> None:
    provider = JQuantsV2Provider(
        request_func=lambda path, params, timeout: {
            "data": [],
            "pagination_key": "same",
        },
        min_request_interval_seconds=12,
        sleep_func=lambda seconds: None,
        clock=lambda: 0.0,
    )

    with pytest.raises(ProviderDownloadError, match="pagination key repeated"):
        provider.get_universe(date(2026, 8, 31))


def test_master_code_dtype_remains_string() -> None:
    provider = JQuantsV2Provider(
        request_func=lambda path, params, timeout: {
            "data": master_frame().to_dict("records")
        },
        min_request_interval_seconds=12,
    )
    result = provider.get_universe(date(2026, 8, 31))

    assert isinstance(result.loc[0, "Code"], str)
    assert pd.api.types.is_string_dtype(result["Code"].dtype)
