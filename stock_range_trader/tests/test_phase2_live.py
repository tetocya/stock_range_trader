"""Explicitly opted-in, minimal Phase 2 live integration tests."""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from data import (
    UnsupportedCorporateActionError,
    canonical_to_phase1,
    validate_backtest_price_contract,
)
from data.providers import JQuantsV2Provider, YFinanceProvider


@pytest.mark.live_jquants
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_JQUANTS_TESTS") != "1"
    or not os.environ.get("JQUANTS_API_KEY"),
    reason="requires RUN_LIVE_JQUANTS_TESTS=1 and JQUANTS_API_KEY",
)
def test_live_jquants_one_symbol_short_period() -> None:
    end = date.today() - timedelta(weeks=13)
    start = end - timedelta(days=10)
    bars = JQuantsV2Provider().get_daily_bars(["72030"], start, end)
    assert set(bars["provider"]) == {"jquants"}


@pytest.mark.live_yfinance
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_YFINANCE_TESTS") != "1",
    reason="requires RUN_LIVE_YFINANCE_TESTS=1",
)
def test_live_yfinance_one_symbol_short_period() -> None:
    end = date.today()
    start = end - timedelta(days=10)
    bars = YFinanceProvider(batch_size=1).get_daily_bars(["7203.T"], start, end)
    assert set(bars["provider"]) == {"yfinance"}


@pytest.mark.live_yfinance
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_YFINANCE_TESTS") != "1",
    reason="requires RUN_LIVE_YFINANCE_TESTS=1",
)
def test_live_yfinance_mhi_2024_split_is_golden_unsupported() -> None:
    """Lock Yahoo's ex-date row for MHI's 2024-04-01 effective 1:10 split."""

    bars = YFinanceProvider(batch_size=1).get_daily_bars(
        ["7011.T"], date(2024, 3, 25), date(2024, 4, 9)
    )
    split_events = bars.loc[bars["stock_split"].ne(0.0)]

    assert list(split_events["date"].dt.date) == [date(2024, 3, 28)]
    assert list(split_events["stock_split"]) == [10.0]
    adapted = canonical_to_phase1(bars, symbol="7011.T")
    assert adapted["split_ratio"].eq(1.0).all()
    assert not adapted["corporate_action_supported"].any()
    with pytest.raises(UnsupportedCorporateActionError, match="price basis"):
        validate_backtest_price_contract(adapted)
