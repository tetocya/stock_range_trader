"""Phase 2 configuration and canonical-schema tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from phase2_helpers import canonical_bars

from config import DataSourcesConfig, load_data_sources_config
from data import (
    CanonicalDataError,
    assess_symbol_data,
    canonical_to_phase1,
    provider_price_basis,
    require_single_provider,
    validate_canonical_bars,
)

PROJECT_ROOT = Path(__file__).parents[1]


def test_data_sources_config_loads_required_free_plan_limits() -> None:
    config = load_data_sources_config(PROJECT_ROOT / "config" / "data_sources.yaml")

    assert config.jquants.plan == "free"
    assert config.jquants.rate_limit_per_minute == 5
    assert config.jquants.min_request_interval_seconds == 13
    assert config.yfinance.batch_size == 50
    assert config.yfinance.threads is False


def test_data_sources_config_rejects_unknown_keys() -> None:
    values = {
        "cache_root": ".data_cache",
        "schema_version": "2.0",
        "jquants": {},
        "yfinance": {},
        "screening": {},
        "comparison": {},
        "typo": True,
    }

    with pytest.raises(ValueError, match="Unknown data source configuration"):
        DataSourcesConfig.from_mapping(values)


def test_canonical_validation_and_phase1_adapter_use_adjusted_prices() -> None:
    bars = canonical_bars(periods=3)
    bars.loc[:, "adjusted_close"] *= 0.5
    bars.loc[:, "adjusted_open"] *= 0.5
    bars.loc[:, "adjusted_high"] *= 0.5
    bars.loc[:, "adjusted_low"] *= 0.5
    bars.loc[:, "adjustment_factor"] = 0.5

    validate_canonical_bars(
        bars,
        expected_provider="yfinance",
        requested_symbols={"7203.T"},
    )
    adapted = canonical_to_phase1(bars, symbol="7203.T")

    assert adapted["close"].equals(bars["adjusted_close"])
    assert adapted["turnover_value"].equals(bars["turnover_value"])


def test_provider_price_basis_does_not_claim_yahoo_is_historically_unadjusted() -> None:
    basis = provider_price_basis("yfinance")

    assert "yahoo_reported" in basis
    assert "historical_split_basis_unverified" in basis


def test_canonical_rejects_future_data_and_provider_mixing() -> None:
    bars = canonical_bars(periods=3)
    mixed = pd.concat(
        [bars, canonical_bars("72030", provider="jquants", periods=3)],
        ignore_index=True,
    )

    with pytest.raises(CanonicalDataError, match="provider mixing"):
        canonical_to_phase1(mixed)
    with pytest.raises(CanonicalDataError, match="after as_of_date"):
        canonical_to_phase1(
            bars,
            symbol="7203.T",
            as_of_date=bars["date"].iloc[-2].date(),
        )
    with pytest.raises(CanonicalDataError, match="exactly one provider"):
        require_single_provider(mixed)


def test_assess_symbol_data_reports_history_and_missing_sessions() -> None:
    bars = canonical_bars(periods=3)

    short = assess_symbol_data(
        bars,
        "7203.T",
        expected_provider="yfinance",
        minimum_observations=4,
    )
    missing = assess_symbol_data(
        bars,
        "7203.T",
        expected_provider="yfinance",
        minimum_observations=1,
        trading_dates=[day.date() for day in pd.bdate_range("2026-08-24", periods=8)],
        maximum_missing_session_ratio=0.1,
    )

    assert short.status == "insufficient_history"
    assert missing.status == "excessive_missing_days"


def test_canonical_rejects_unrequested_ticker_and_invalid_factor() -> None:
    bars = canonical_bars(periods=3)
    with pytest.raises(CanonicalDataError, match="unrequested"):
        validate_canonical_bars(bars, requested_symbols={"130A.T"})

    bars.loc[0, "adjustment_factor"] = 0.0
    with pytest.raises(CanonicalDataError, match="adjustment_factor"):
        validate_canonical_bars(bars)


def test_phase1_adapter_as_of_allows_exact_reference_date() -> None:
    bars = canonical_bars(periods=3)
    adapted = canonical_to_phase1(bars, symbol="7203.T", as_of_date=date(2026, 8, 31))
    assert adapted["date"].max().date() == date(2026, 8, 31)


def test_canonical_flags_non_trading_dates_and_price_unit_discontinuity() -> None:
    bars = canonical_bars(periods=3)
    off_calendar = assess_symbol_data(
        bars,
        "7203.T",
        expected_provider="yfinance",
        minimum_observations=1,
        trading_dates=[date(2026, 8, 27), date(2026, 8, 28)],
    )
    assert off_calendar.status == "invalid_ohlcv"
    assert "outside the supplied trading calendar" in off_calendar.message

    bars.loc[
        2, ["adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close"]
    ] *= 100.0
    with pytest.raises(CanonicalDataError, match="price-unit change"):
        validate_canonical_bars(bars)
