"""Explicitly opted-in, minimal Phase 2 live integration tests."""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

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
