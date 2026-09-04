"""Backtest price-lane and corporate-action policy helpers."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from .validation import validate_ohlcv

SIGNAL_PRICE_MODE = "provider_adjusted_ohlc"
EXECUTION_PRICE_MODE = "provider_reported_ohlcv_basis_not_assumed_unadjusted"
DIVIDEND_POLICY = "excluded_from_strategy_and_benchmarks"
CORPORATE_ACTION_MODE = (
    "split_detected_means_executable_unsupported;"
    "share_adjustment_disabled;no_cash_dividends"
)
THEORETICAL_BENCHMARK_MODE = (
    "provider_reported_close_price_return_split_free_intervals_no_dividends"
)
EXECUTABLE_BENCHMARK_MODE = (
    "provider_reported_open_fill_close_valuation_split_free_intervals_no_dividends"
)

_PROVIDER_PRICE_BASES: Mapping[str, str] = {
    "yfinance": (
        "yahoo_reported_ohlcv_auto_adjust_false;historical_split_basis_unverified"
    ),
    "jquants": ("jquants_reported_ohlcv_with_separate_official_adjusted_ohlcv"),
}
_UNKNOWN_PROVIDER_PRICE_BASIS = "provider_reported_ohlcv_basis_unspecified"

SIGNAL_COLUMNS: tuple[str, ...] = (
    "signal_open",
    "signal_high",
    "signal_low",
    "signal_close",
    "signal_volume",
)
EXECUTION_COLUMNS: tuple[str, ...] = (
    "execution_open",
    "execution_high",
    "execution_low",
    "execution_close",
    "execution_volume",
)
POLICY_COLUMNS: tuple[str, ...] = (
    "split_ratio",
    "dividend",
    "corporate_action_supported",
)


class UnsupportedCorporateActionError(RuntimeError):
    """Raised instead of publishing economically inconsistent results."""


def has_dual_price_lanes(frame: pd.DataFrame) -> bool:
    """Return whether all explicit signal/execution columns are present."""

    available = set(frame.columns)
    required = set((*SIGNAL_COLUMNS, *EXECUTION_COLUMNS, *POLICY_COLUMNS))
    present = required & available
    if present and present != required:
        missing = sorted(required - available)
        raise ValueError("incomplete dual-price contract: " + ", ".join(missing))
    return present == required


def validate_backtest_price_contract(frame: pd.DataFrame) -> None:
    """Validate explicit lanes and reject unsupported corporate actions."""

    if not has_dual_price_lanes(frame):
        return
    for legacy, signal in zip(
        ("open", "high", "low", "close", "volume"), SIGNAL_COLUMNS, strict=True
    ):
        if not np.allclose(
            frame[legacy].to_numpy(dtype=float),
            frame[signal].to_numpy(dtype=float),
            rtol=0.0,
            atol=0.0,
            equal_nan=True,
        ):
            raise ValueError(f"legacy {legacy} must equal explicit {signal}")
    execution = pd.DataFrame(
        {
            "date": frame["date"],
            "open": frame["execution_open"],
            "high": frame["execution_high"],
            "low": frame["execution_low"],
            "close": frame["execution_close"],
            "volume": frame["execution_volume"],
        }
    )
    validate_ohlcv(execution)
    split_ratios = pd.to_numeric(frame["split_ratio"], errors="coerce")
    dividends = pd.to_numeric(frame["dividend"], errors="coerce")
    if not np.isfinite(split_ratios).all() or (split_ratios <= 0.0).any():
        raise ValueError("split_ratio must contain finite positive values")
    if not np.isfinite(dividends).all():
        raise ValueError("dividend must contain finite values")
    supported = frame["corporate_action_supported"]
    if (
        supported.isna().any()
        or not supported.map(lambda value: isinstance(value, (bool, np.bool_))).all()
    ):
        raise ValueError("corporate_action_supported must contain booleans")
    if not np.allclose(split_ratios, 1.0, rtol=0.0, atol=0.0):
        raise UnsupportedCorporateActionError(
            "split share adjustment is disabled until the provider price basis "
            "is verified"
        )
    if not supported.astype(bool).all():
        raise UnsupportedCorporateActionError(
            "corporate action detected but the provider price basis is not "
            "verified; executable results are unsupported"
        )


def provider_price_basis(provider: str) -> str:
    """Describe what is known about one provider's reported OHLCV basis."""

    return _PROVIDER_PRICE_BASES.get(
        provider.strip().lower(), _UNKNOWN_PROVIDER_PRICE_BASIS
    )


def price_policy_manifest_fields(provider: str | None = None) -> Mapping[str, str]:
    """Return the stable policy vocabulary used by output manifests."""

    return {
        "provider_price_basis": provider_price_basis(provider or ""),
        "signal_price_mode": SIGNAL_PRICE_MODE,
        "execution_price_mode": EXECUTION_PRICE_MODE,
        "dividend_policy": DIVIDEND_POLICY,
        "corporate_action_mode": CORPORATE_ACTION_MODE,
        "theoretical_benchmark_mode": THEORETICAL_BENCHMARK_MODE,
        "executable_benchmark_mode": EXECUTABLE_BENCHMARK_MODE,
    }
