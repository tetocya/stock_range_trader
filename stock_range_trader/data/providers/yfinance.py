"""Five-year daily-price provider backed by yfinance."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from data.canonical import (
    CANONICAL_COLUMNS,
    CanonicalDataError,
    empty_canonical_frame,
    validate_canonical_bars,
)

from .base import DownloadIssue, PriceDataProvider

YFINANCE_REQUIRED_COLUMNS: tuple[str, ...] = (
    "Open",
    "High",
    "Low",
    "Close",
    "Adj Close",
    "Volume",
)
YFINANCE_ACTION_COLUMNS: tuple[str, ...] = ("Dividends", "Stock Splits")
YFINANCE_ADJUSTMENT_MODE = "adj_close_ratio_for_ohlc_provider_reported_volume"


class YFinanceProvider(PriceDataProvider):
    """Download Yahoo Finance daily batches with ``auto_adjust=False``."""

    name = "yfinance"
    adjustment_mode = YFINANCE_ADJUSTMENT_MODE

    def __init__(
        self,
        *,
        batch_size: int = 50,
        threads: bool = False,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        download_func: Callable[..., pd.DataFrame | None] | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size <= 0
        ):
            raise ValueError("batch_size must be a positive integer")
        if threads is not False:
            raise ValueError(
                "threads must be false for deterministic Phase 2 downloads"
            )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries <= 0
        ):
            raise ValueError("max_retries must be a positive integer")
        if download_func is None:
            import yfinance as yf

            download_func = yf.download
            library_version = yf.__version__
        else:
            library_version = "injected"
        self.batch_size = batch_size
        self.threads = threads
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._download = download_func
        self._sleep = sleep_func
        self.library_version = library_version
        self._issues: list[DownloadIssue] = []

    @property
    def issues(self) -> tuple[DownloadIssue, ...]:
        return tuple(self._issues)

    def get_daily_bars(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Return canonical bars; Yahoo's ``end`` remains explicitly exclusive."""

        requested = _normalize_symbols(symbols)
        if start >= end:
            raise ValueError("start must be before exclusive end")
        self._issues = []
        frames: list[pd.DataFrame] = []
        fetched_at = datetime.now(UTC)
        for offset in range(0, len(requested), self.batch_size):
            batch = requested[offset : offset + self.batch_size]
            raw = self._download_with_retry(batch, start, end)
            if raw is None:
                continue
            returned = _returned_tickers(raw, batch)
            unexpected = sorted(returned - set(batch))
            for ticker in unexpected:
                self._issues.append(
                    DownloadIssue(
                        ticker,
                        "provider_mismatch",
                        "yfinance returned an unrequested ticker",
                    )
                )
            for ticker in batch:
                ticker_frame = extract_yfinance_ticker(raw, ticker, batch)
                if (
                    ticker_frame is None
                    or ticker_frame.empty
                    or _has_no_price_observations(ticker_frame)
                ):
                    self._issues.append(
                        DownloadIssue(
                            ticker, "empty_response", "no daily rows returned"
                        )
                    )
                    continue
                try:
                    converted = yfinance_to_canonical(
                        ticker_frame,
                        ticker=ticker,
                        fetched_at=fetched_at,
                    )
                    validate_canonical_bars(
                        converted,
                        expected_provider=self.name,
                        requested_symbols={ticker},
                        start=start,
                        end=end,
                    )
                except (CanonicalDataError, TypeError, ValueError) as error:
                    self._issues.append(
                        DownloadIssue(ticker, "invalid_ohlcv", str(error))
                    )
                    continue
                frames.append(converted)
        if not frames:
            return empty_canonical_frame()
        result = pd.concat(frames, ignore_index=True)
        return result.loc[:, CANONICAL_COLUMNS].sort_values(
            ["symbol", "date"], kind="stable", ignore_index=True
        )

    def _download_with_retry(
        self, batch: list[str], start: date, end: date
    ) -> pd.DataFrame | None:
        last_error: Exception | None = None
        saw_empty_response = False
        for attempt in range(self.max_retries):
            try:
                result = self._download(
                    tickers=batch,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    interval="1d",
                    auto_adjust=False,
                    actions=True,
                    keepna=True,
                    progress=False,
                    threads=self.threads,
                    timeout=self.timeout_seconds,
                    group_by="ticker",
                    multi_level_index=True,
                )
                if result is not None and not result.empty:
                    return result
                saw_empty_response = True
            except Exception as error:
                last_error = error
            if attempt + 1 < self.max_retries:
                self._sleep(float(2**attempt))
        status = (
            "empty_response"
            if saw_empty_response and last_error is None
            else "download_failed"
        )
        message = "empty response after retries"
        if last_error is not None:
            message = f"download failed after retries: {last_error}"
        for ticker in batch:
            self._issues.append(DownloadIssue(ticker, status, message))
        return None


def extract_yfinance_ticker(
    frame: pd.DataFrame,
    ticker: str,
    requested_batch: Sequence[str],
) -> pd.DataFrame | None:
    """Extract one ticker from either orientation of a MultiIndex response."""

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("yfinance response must be a pandas DataFrame")
    if isinstance(frame.columns, pd.MultiIndex):
        for level in range(frame.columns.nlevels):
            values = frame.columns.get_level_values(level).astype(str)
            if ticker in set(values):
                return frame.xs(ticker, axis=1, level=level, drop_level=True).copy()
        return None
    if len(requested_batch) != 1:
        return None
    return frame.copy()


def yfinance_to_canonical(
    frame: pd.DataFrame,
    *,
    ticker: str,
    fetched_at: datetime,
) -> pd.DataFrame:
    """Preserve provider-reported fields/actions and derive adjusted OHLC.

    ``Adj Close / Close`` is applied only to OHLC. Volume remains as reported
    because Yahoo does not expose a standalone dividend-free split factor in
    this response; applying the total-return factor to volume would mix
    dividends with split adjustment. Liquidity uses reported Close × Volume.
    """

    required = set(YFINANCE_REQUIRED_COLUMNS + YFINANCE_ACTION_COLUMNS)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError("Missing yfinance columns: " + ", ".join(missing))
    result = pd.DataFrame(index=frame.index)
    result["date"] = _normalize_date_index(frame.index)
    result["symbol"] = ticker
    result["provider"] = "yfinance"
    result["raw_open"] = pd.to_numeric(frame["Open"], errors="coerce")
    result["raw_high"] = pd.to_numeric(frame["High"], errors="coerce")
    result["raw_low"] = pd.to_numeric(frame["Low"], errors="coerce")
    result["raw_close"] = pd.to_numeric(frame["Close"], errors="coerce")
    result["raw_volume"] = pd.to_numeric(frame["Volume"], errors="coerce")
    result["turnover_value"] = result["raw_close"] * result["raw_volume"]
    adj_close = pd.to_numeric(frame["Adj Close"], errors="coerce")
    factor = adj_close / result["raw_close"].replace(0.0, np.nan)
    result["adjustment_factor"] = factor
    for suffix in ("open", "high", "low"):
        result[f"adjusted_{suffix}"] = result[f"raw_{suffix}"] * factor
    result["adjusted_close"] = adj_close
    result["adjusted_volume"] = result["raw_volume"]
    result["dividend"] = pd.to_numeric(frame["Dividends"], errors="coerce")
    result["stock_split"] = pd.to_numeric(frame["Stock Splits"], errors="coerce")
    result["fetched_at"] = fetched_at
    return result.loc[:, CANONICAL_COLUMNS].reset_index(drop=True)


def _has_no_price_observations(frame: pd.DataFrame) -> bool:
    available = [column for column in YFINANCE_REQUIRED_COLUMNS if column in frame]
    return not available or frame.loc[:, available].isna().all(axis=None)


def _normalize_date_index(index: pd.Index) -> pd.DatetimeIndex:
    values = pd.to_datetime(index, errors="coerce")
    if not isinstance(values, pd.DatetimeIndex):
        values = pd.DatetimeIndex(values)
    if values.tz is not None:
        values = values.tz_localize(None)
    return values.normalize()


def _normalize_symbols(symbols: Sequence[str]) -> list[str]:
    result: list[str] = []
    for symbol in symbols:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbols must contain non-empty strings")
        normalized = symbol.strip().upper()
        if normalized not in result:
            result.append(normalized)
    if not result:
        raise ValueError("at least one symbol is required")
    return result


def _returned_tickers(frame: pd.DataFrame, requested: Sequence[str]) -> set[str]:
    if not isinstance(frame.columns, pd.MultiIndex):
        return set(requested) if len(requested) == 1 else set()
    candidates: set[str] = set()
    for level in range(frame.columns.nlevels):
        values = {str(value) for value in frame.columns.get_level_values(level)}
        candidates.update(value for value in values if value.upper().endswith(".T"))
    return candidates
