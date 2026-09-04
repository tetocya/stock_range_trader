"""Rate-limited J-Quants API V2 provider for the Free plan."""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import pandas as pd

from data.canonical import (
    CANONICAL_COLUMNS,
    CanonicalDataError,
    empty_canonical_frame,
    validate_canonical_bars,
)

from .base import (
    DownloadIssue,
    PriceDataProvider,
    ProviderAuthenticationError,
    ProviderDownloadError,
    UniverseProvider,
)

JQUANTS_API_KEY_ENV = "JQUANTS_API_KEY"
JQUANTS_ADJUSTMENT_MODE = "jquants_official_adjusted_ohlcv"
EQ_MASTER_COLUMNS: tuple[str, ...] = (
    "Date",
    "Code",
    "CoName",
    "CoNameEn",
    "S17",
    "S17Nm",
    "S33",
    "S33Nm",
    "ScaleCat",
    "Mkt",
    "MktNm",
    "Mrgn",
    "MrgnNm",
    "ProdCat",
)


class _SequentialRateLimiter:
    def __init__(
        self,
        interval_seconds: float,
        *,
        clock: Callable[[], float],
        sleep_func: Callable[[float], None],
    ) -> None:
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._sleep = sleep_func
        self._last_request: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last_request is not None:
            remaining = self.interval_seconds - (now - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = self._clock()


class JQuantsV2Provider(PriceDataProvider, UniverseProvider):
    """Use official ``ClientV2`` transport with explicit sequential paging."""

    name = "jquants"
    adjustment_mode = JQUANTS_ADJUSTMENT_MODE

    def __init__(
        self,
        *,
        min_request_interval_seconds: float = 13.0,
        max_retries: int = 3,
        timeout_seconds: float = 30.0,
        client: Any | None = None,
        request_func: Callable[[str, Mapping[str, Any], float], Any] | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        random_func: Callable[[], float] = random.random,
    ) -> None:
        if min_request_interval_seconds < 12.0:
            raise ValueError("J-Quants Free requests must be at least 12 seconds apart")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries <= 0
        ):
            raise ValueError("max_retries must be a positive integer")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if client is None and request_func is None:
            api_key = os.environ.get(JQUANTS_API_KEY_ENV, "").strip()
            if not api_key:
                raise ProviderAuthenticationError(
                    "JQUANTS_API_KEY is required for J-Quants API V2"
                )
            import jquantsapi

            client = jquantsapi.ClientV2(api_key=api_key)
            library_version = jquantsapi.__version__
        else:
            library_version = "injected"
        if request_func is None:
            if client is None or not callable(getattr(client, "_get", None)):
                raise TypeError("client must provide the ClientV2 _get transport")

            def official_request(
                path: str, params: Mapping[str, Any], timeout: float
            ) -> Any:
                del timeout  # ClientV2 currently applies its own transport timeout.
                return client._get(  # noqa: SLF001
                    f"{client.JQUANTS_API_BASE}{path}", params=dict(params)
                )

            request_func = official_request
        self._request = request_func
        self._limiter = _SequentialRateLimiter(
            min_request_interval_seconds,
            clock=clock,
            sleep_func=sleep_func,
        )
        self._sleep = sleep_func
        self._random = random_func
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self.library_version = library_version
        self._issues: list[DownloadIssue] = []

    @property
    def issues(self) -> tuple[DownloadIssue, ...]:
        return tuple(self._issues)

    def get_universe(self, as_of_date: date) -> pd.DataFrame:
        """Fetch a point-in-time listed-issue snapshot with sequential paging."""

        records = self._get_paginated(
            "/equities/master", {"date": as_of_date.isoformat()}
        )
        if not records:
            return pd.DataFrame(columns=EQ_MASTER_COLUMNS)
        frame = pd.DataFrame.from_records(records)
        missing = sorted(set(EQ_MASTER_COLUMNS) - set(frame.columns))
        if missing:
            raise ProviderDownloadError(
                "J-Quants master response missing columns: " + ", ".join(missing)
            )
        if any(not isinstance(code, str) for code in frame["Code"]):
            raise ProviderDownloadError(
                "J-Quants master Code must be returned as a string"
            )
        result = frame.loc[:, EQ_MASTER_COLUMNS].copy()
        result["Date"] = pd.to_datetime(result["Date"], errors="coerce")
        result["Code"] = result["Code"].astype("string")
        return result.sort_values("Code", kind="stable", ignore_index=True)

    def get_daily_bars(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Fetch each symbol sequentially; ``end`` is normalized to exclusive."""

        requested = _normalize_symbols(symbols)
        if start >= end:
            raise ValueError("start must be before exclusive end")
        self._issues = []
        frames: list[pd.DataFrame] = []
        fetched_at = datetime.now(UTC)
        inclusive_end = end - timedelta(days=1)
        for symbol in requested:
            try:
                records = self._get_paginated(
                    "/equities/bars/daily",
                    {
                        "code": symbol,
                        "from": start.isoformat(),
                        "to": inclusive_end.isoformat(),
                    },
                )
            except ProviderDownloadError as error:
                self._issues.append(
                    DownloadIssue(symbol, "download_failed", str(error))
                )
                continue
            if not records:
                self._issues.append(
                    DownloadIssue(symbol, "empty_response", "no daily rows returned")
                )
                continue
            try:
                frame = jquants_daily_to_canonical(records, fetched_at=fetched_at)
                returned = set(frame["symbol"].astype(str))
                if returned != {symbol}:
                    self._issues.append(
                        DownloadIssue(
                            symbol,
                            "provider_mismatch",
                            "J-Quants returned a different issue code",
                        )
                    )
                    continue
                validate_canonical_bars(
                    frame,
                    expected_provider=self.name,
                    requested_symbols={symbol},
                    start=start,
                    end=end,
                )
            except (CanonicalDataError, ProviderDownloadError, ValueError) as error:
                self._issues.append(DownloadIssue(symbol, "invalid_ohlcv", str(error)))
                continue
            frames.append(frame)
        if not frames:
            return empty_canonical_frame()
        return (
            pd.concat(frames, ignore_index=True)
            .loc[:, CANONICAL_COLUMNS]
            .sort_values(["symbol", "date"], kind="stable", ignore_index=True)
        )

    def get_trading_calendar(self, start: date, end: date) -> pd.DataFrame:
        """Return J-Quants calendar rows for a half-open interval.

        The endpoint may be unavailable on the Free plan; that upstream error
        is surfaced rather than replaced with an inferred weekday calendar.
        """

        if start >= end:
            raise ValueError("start must be before exclusive end")
        records = self._get_paginated(
            "/markets/calendar",
            {
                "from": start.isoformat(),
                "to": (end - timedelta(days=1)).isoformat(),
            },
        )
        frame = pd.DataFrame.from_records(records)
        if frame.empty:
            return pd.DataFrame(columns=["Date", "HolDiv"])
        if not {"Date", "HolDiv"}.issubset(frame.columns):
            raise ProviderDownloadError("J-Quants calendar response schema mismatch")
        frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
        return frame[["Date", "HolDiv"]].sort_values(
            "Date", kind="stable", ignore_index=True
        )

    def _get_paginated(
        self, path: str, params: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        query = dict(params)
        records: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        while True:
            response = self._request_page(path, query)
            payload = (
                response.json()
                if callable(getattr(response, "json", None))
                else response
            )
            if not isinstance(payload, dict):
                raise ProviderDownloadError("J-Quants response must be a JSON object")
            page = payload.get("data", [])
            if not isinstance(page, list) or any(
                not isinstance(row, dict) for row in page
            ):
                raise ProviderDownloadError("J-Quants response data must be a list")
            records.extend(page)
            pagination_key = payload.get("pagination_key")
            if not pagination_key:
                return records
            key = str(pagination_key)
            if key in seen_keys:
                raise ProviderDownloadError("J-Quants pagination key repeated")
            seen_keys.add(key)
            query["pagination_key"] = key

    def _request_page(self, path: str, params: Mapping[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._limiter.wait()
            try:
                return self._request(path, params, self.timeout_seconds)
            except Exception as error:
                last_error = error
                status = _status_code(error)
                retryable = status == 429 or status is None or status >= 500
                if not retryable or attempt + 1 >= self.max_retries:
                    break
                retry_after = _retry_after_seconds(error)
                delay = (
                    retry_after
                    if retry_after is not None
                    else float(2**attempt) + self._random()
                )
                self._sleep(max(0.0, delay))
        raise ProviderDownloadError(
            f"J-Quants request failed after {self.max_retries} attempts: {last_error}"
        ) from last_error


def jquants_daily_to_canonical(
    records: Sequence[Mapping[str, Any]], *, fetched_at: datetime
) -> pd.DataFrame:
    """Convert a V2 daily-bar response without hiding incomplete values."""

    frame = pd.DataFrame.from_records(records)
    required = {
        "Date",
        "Code",
        "O",
        "H",
        "L",
        "C",
        "Vo",
        "Va",
        "AdjFactor",
        "AdjO",
        "AdjH",
        "AdjL",
        "AdjC",
        "AdjVo",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ProviderDownloadError(
            "J-Quants daily response missing columns: " + ", ".join(missing)
        )
    if any(not isinstance(code, str) for code in frame["Code"]):
        raise ProviderDownloadError("J-Quants daily Code must be returned as a string")
    result = pd.DataFrame(index=frame.index)
    result["date"] = pd.to_datetime(frame["Date"], errors="coerce")
    result["symbol"] = frame["Code"].astype("string")
    result["provider"] = "jquants"
    mapping = {
        "raw_open": "O",
        "raw_high": "H",
        "raw_low": "L",
        "raw_close": "C",
        "raw_volume": "Vo",
        "turnover_value": "Va",
        "adjusted_open": "AdjO",
        "adjusted_high": "AdjH",
        "adjusted_low": "AdjL",
        "adjusted_close": "AdjC",
        "adjusted_volume": "AdjVo",
        "adjustment_factor": "AdjFactor",
    }
    for target, source in mapping.items():
        result[target] = pd.to_numeric(frame[source], errors="coerce")
    result["dividend"] = 0.0
    # AdjFactor is retained separately. It is not relabeled as Yahoo's
    # split-event ratio because the two fields do not share one convention.
    result["stock_split"] = 0.0
    result["fetched_at"] = fetched_at
    return result.loc[:, CANONICAL_COLUMNS].sort_values(
        ["symbol", "date"], kind="stable", ignore_index=True
    )


def _normalize_symbols(symbols: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in symbols:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("symbols must contain non-empty strings")
        symbol = value.strip().upper()
        if symbol not in result:
            result.append(symbol)
    if not result:
        raise ValueError("at least one symbol is required")
    return result


def _status_code(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _retry_after_seconds(error: Exception) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {})
    value = headers.get("Retry-After") if isinstance(headers, Mapping) else None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
