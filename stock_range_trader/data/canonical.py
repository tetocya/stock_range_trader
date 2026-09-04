"""Canonical Phase 2 daily-bar schema and validation."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from data.validation import DataValidationError, validate_ohlcv

from .price_policy import EXECUTION_COLUMNS, POLICY_COLUMNS, SIGNAL_COLUMNS

CANONICAL_SCHEMA_VERSION = "2.0"
MAX_ADJUSTED_CLOSE_UNIT_RATIO = 50.0
CANONICAL_COLUMNS: tuple[str, ...] = (
    "date",
    "symbol",
    "provider",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "raw_volume",
    "turnover_value",
    "adjusted_open",
    "adjusted_high",
    "adjusted_low",
    "adjusted_close",
    "adjusted_volume",
    "adjustment_factor",
    "dividend",
    "stock_split",
    "fetched_at",
)
REQUIRED_FINITE_COLUMNS: tuple[str, ...] = (
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "raw_volume",
    "turnover_value",
    "adjusted_open",
    "adjusted_high",
    "adjusted_low",
    "adjusted_close",
    "adjusted_volume",
    "adjustment_factor",
    "dividend",
    "stock_split",
)
SYMBOL_STATUSES: frozenset[str] = frozenset(
    {
        "ok",
        "insufficient_history",
        "download_failed",
        "empty_response",
        "unresolved_symbol",
        "invalid_ohlcv",
        "excessive_missing_days",
        "provider_mismatch",
    }
)


class CanonicalDataError(DataValidationError):
    """Raised when provider data violates the canonical contract."""


@dataclass(frozen=True, slots=True)
class SymbolDataStatus:
    """Validation outcome for one symbol."""

    symbol: str
    status: str
    message: str = ""

    def __post_init__(self) -> None:
        if self.status not in SYMBOL_STATUSES:
            raise ValueError(f"Unknown symbol status: {self.status}")


def empty_canonical_frame() -> pd.DataFrame:
    """Return an empty frame with the stable canonical column order."""

    return pd.DataFrame(columns=CANONICAL_COLUMNS)


def normalize_canonical_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Order canonical columns and rows without hiding missing columns."""

    _require_columns(frame, CANONICAL_COLUMNS)
    result = frame.loc[:, CANONICAL_COLUMNS].copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    result["fetched_at"] = pd.to_datetime(
        result["fetched_at"], errors="coerce", utc=True
    )
    result["symbol"] = result["symbol"].astype("string")
    result["provider"] = result["provider"].astype("string")
    return result.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)


def validate_canonical_bars(
    frame: pd.DataFrame,
    *,
    expected_provider: str | None = None,
    requested_symbols: Collection[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> None:
    """Validate canonical bars without repairing or dropping observations."""

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("canonical bars must be a pandas DataFrame")
    _require_columns(frame, CANONICAL_COLUMNS)
    if frame.empty:
        raise CanonicalDataError("canonical bars must contain at least one row")
    if not pd.api.types.is_datetime64_any_dtype(frame["date"].dtype):
        raise CanonicalDataError("canonical date must have a pandas datetime dtype")
    if frame["date"].isna().any():
        raise CanonicalDataError("canonical date contains invalid values")
    if frame["fetched_at"].isna().any():
        raise CanonicalDataError("fetched_at contains invalid values")

    symbols = frame["symbol"].astype(str)
    providers = frame["provider"].astype(str)
    if symbols.str.strip().eq("").any():
        raise CanonicalDataError("symbol must not be empty")
    if providers.str.strip().eq("").any():
        raise CanonicalDataError("provider must not be empty")
    if expected_provider is not None and set(providers) != {expected_provider}:
        raise CanonicalDataError(
            f"provider mismatch: expected only {expected_provider!r}"
        )
    if requested_symbols is not None:
        unexpected = sorted(set(symbols) - set(requested_symbols))
        if unexpected:
            raise CanonicalDataError(
                "provider returned unrequested symbols: " + ", ".join(unexpected)
            )

    if start is not None and (frame["date"].dt.date < start).any():
        raise CanonicalDataError("provider returned data before requested start")
    if end is not None and (frame["date"].dt.date >= end).any():
        raise CanonicalDataError("provider returned data at or after exclusive end")

    numeric = frame.loc[:, REQUIRED_FINITE_COLUMNS]
    try:
        finite = np.isfinite(numeric.to_numpy(dtype=float))
    except (TypeError, ValueError) as error:
        raise CanonicalDataError("canonical numeric columns must be numeric") from error
    if not finite.all():
        raise CanonicalDataError("canonical required numeric values must be finite")
    if (frame["adjustment_factor"].astype(float) <= 0.0).any():
        raise CanonicalDataError("adjustment_factor must be greater than zero")
    for column in ("raw_volume", "adjusted_volume", "turnover_value"):
        if (frame[column].astype(float) < 0.0).any():
            raise CanonicalDataError(f"{column} must be non-negative")

    duplicated = frame.duplicated(["symbol", "date"], keep=False)
    if duplicated.any():
        raise CanonicalDataError("duplicate symbol/date observations detected")
    for symbol, group in frame.groupby("symbol", sort=False):
        if not group["date"].is_monotonic_increasing:
            raise CanonicalDataError(f"dates for {symbol} must be ascending")
        _validate_ohlc(group, "raw")
        _validate_ohlc(group, "adjusted")
        ratios = group["adjusted_close"].astype(float).pct_change(fill_method=None) + 1
        if (
            (ratios > MAX_ADJUSTED_CLOSE_UNIT_RATIO)
            | (ratios < 1.0 / MAX_ADJUSTED_CLOSE_UNIT_RATIO)
        ).any():
            raise CanonicalDataError(
                f"abrupt adjusted price-unit change detected for {symbol}"
            )


def assess_symbol_data(
    frame: pd.DataFrame,
    symbol: str,
    *,
    expected_provider: str,
    minimum_observations: int,
    trading_dates: Iterable[date] | None = None,
    maximum_missing_session_ratio: float = 0.10,
) -> SymbolDataStatus:
    """Return an explicit symbol status instead of silently dropping failures."""

    if frame.empty:
        return SymbolDataStatus(symbol, "empty_response", "provider returned no rows")
    try:
        validate_canonical_bars(
            frame,
            expected_provider=expected_provider,
            requested_symbols={symbol},
        )
    except CanonicalDataError as error:
        status = (
            "provider_mismatch"
            if "provider" in str(error) or "unrequested" in str(error)
            else "invalid_ohlcv"
        )
        return SymbolDataStatus(symbol, status, str(error))
    if len(frame) < minimum_observations:
        return SymbolDataStatus(
            symbol,
            "insufficient_history",
            f"{len(frame)} observations; need {minimum_observations}",
        )
    if trading_dates is not None:
        expected = set(trading_dates)
        available = set(frame["date"].dt.date)
        if expected:
            unexpected = sorted(available - expected)
            if unexpected:
                return SymbolDataStatus(
                    symbol,
                    "invalid_ohlcv",
                    "observations found outside the supplied trading calendar: "
                    + ", ".join(day.isoformat() for day in unexpected[:5]),
                )
            missing_ratio = len(expected - available) / len(expected)
            if missing_ratio > maximum_missing_session_ratio:
                return SymbolDataStatus(
                    symbol,
                    "excessive_missing_days",
                    f"missing session ratio {missing_ratio:.3f}",
                )
    return SymbolDataStatus(symbol, "ok")


def canonical_to_phase1(
    frame: pd.DataFrame,
    *,
    symbol: str | None = None,
    as_of_date: date | None = None,
) -> pd.DataFrame:
    """Adapt one provider/symbol to the compatible dual-price engine contract."""

    selected = frame.copy()
    if symbol is not None:
        selected = selected.loc[selected["symbol"].astype(str) == symbol].copy()
    if as_of_date is not None:
        future = selected["date"].dt.date > as_of_date
        if future.any():
            raise CanonicalDataError("data after as_of_date is not allowed")
    if selected.empty:
        raise CanonicalDataError("no canonical bars selected")
    if selected["provider"].nunique() != 1:
        raise CanonicalDataError("provider mixing is not allowed")
    if selected["symbol"].nunique() != 1:
        raise CanonicalDataError("Phase 1 adapter accepts exactly one symbol")
    result = pd.DataFrame(
        {
            "date": selected["date"],
            "open": selected["adjusted_open"],
            "high": selected["adjusted_high"],
            "low": selected["adjusted_low"],
            "close": selected["adjusted_close"],
            "volume": selected["adjusted_volume"],
            "turnover_value": selected["turnover_value"],
            "signal_open": selected["adjusted_open"],
            "signal_high": selected["adjusted_high"],
            "signal_low": selected["adjusted_low"],
            "signal_close": selected["adjusted_close"],
            "signal_volume": selected["adjusted_volume"],
            "execution_open": selected["raw_open"],
            "execution_high": selected["raw_high"],
            "execution_low": selected["raw_low"],
            "execution_close": selected["raw_close"],
            "execution_volume": selected["raw_volume"],
            "dividend": selected["dividend"],
        }
    )
    provider = str(selected["provider"].iloc[0])
    if provider == "yfinance":
        split = pd.to_numeric(selected["stock_split"], errors="coerce")
        result["split_ratio"] = split.where(split > 0.0, 1.0)
        result["corporate_action_supported"] = True
    elif provider == "jquants":
        result["split_ratio"] = 1.0
        result["corporate_action_supported"] = bool(
            selected["adjustment_factor"].astype(float).eq(1.0).all()
        )
    else:
        result["split_ratio"] = 1.0
        result["corporate_action_supported"] = False
    result = result.reset_index(drop=True)
    result = result[
        [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "turnover_value",
            *SIGNAL_COLUMNS,
            *EXECUTION_COLUMNS,
            *POLICY_COLUMNS,
        ]
    ]
    validate_ohlcv(result)
    return result


def require_single_provider(frame: pd.DataFrame) -> str:
    """Return the sole provider name or reject mixed input."""

    _require_columns(frame, ("provider",))
    providers = sorted(set(frame["provider"].dropna().astype(str)))
    if len(providers) != 1:
        raise CanonicalDataError(
            "exactly one provider is required; mixing is forbidden"
        )
    return providers[0]


def _require_columns(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise CanonicalDataError("Missing canonical columns: " + ", ".join(missing))


def _validate_ohlc(frame: pd.DataFrame, prefix: str) -> None:
    open_ = frame[f"{prefix}_open"].astype(float)
    high = frame[f"{prefix}_high"].astype(float)
    low = frame[f"{prefix}_low"].astype(float)
    close = frame[f"{prefix}_close"].astype(float)
    if ((open_ <= 0) | (high <= 0) | (low <= 0) | (close <= 0)).any():
        raise CanonicalDataError(f"{prefix} prices must be positive")
    if (
        (high < low) | (high < open_) | (high < close) | (low > open_) | (low > close)
    ).any():
        raise CanonicalDataError(f"invalid {prefix} OHLC relationship")
