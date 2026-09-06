"""Offline tests for STEP 11 live-provider date boundaries."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from phase3_live_helpers import jquants_live_window, yfinance_live_window


def test_yfinance_window_uses_twelve_completed_months_and_half_open_fold() -> None:
    window = yfinance_live_window(date(2026, 9, 6))
    fold = window.fold

    assert window.requested_start == date(2025, 9, 1)
    assert window.requested_end_exclusive == date(2026, 9, 1)
    assert (fold.train_start, fold.train_end) == (
        date(2025, 9, 1),
        date(2026, 3, 1),
    )
    assert (fold.validation_start, fold.validation_end) == (
        date(2026, 3, 1),
        date(2026, 6, 1),
    )
    assert (fold.test_start, fold.test_end) == (
        date(2026, 6, 1),
        date(2026, 9, 1),
    )
    assert fold.embargo_sessions == 20
    assert fold.contains_test_date(date(2026, 8, 31))
    assert not fold.contains_test_date(window.requested_end_exclusive)
    assert window.requested_end_exclusive < date(2026, 9, 6)


def test_jquants_window_respects_thirteen_week_safety_boundary() -> None:
    today_jst = date(2026, 9, 6)
    safety_boundary = today_jst - timedelta(weeks=13)
    window = jquants_live_window(today_jst)
    fold = window.fold

    assert safety_boundary == date(2026, 6, 7)
    assert window.requested_start == date(2025, 6, 1)
    assert window.requested_end_exclusive == date(2026, 6, 1)
    assert window.requested_end_exclusive <= safety_boundary
    assert (fold.train_start, fold.train_end) == (
        date(2025, 6, 1),
        date(2025, 12, 1),
    )
    assert (fold.validation_start, fold.validation_end) == (
        date(2025, 12, 1),
        date(2026, 3, 1),
    )
    assert (fold.test_start, fold.test_end) == (
        date(2026, 3, 1),
        date(2026, 6, 1),
    )
    assert fold.embargo_sessions == 0
    assert fold.contains_test_date(date(2026, 5, 31))
    assert not fold.contains_test_date(window.requested_end_exclusive)


@pytest.mark.parametrize("builder", (yfinance_live_window, jquants_live_window))
def test_live_window_rejects_datetime_boundaries(builder) -> None:
    with pytest.raises(TypeError, match="must be a date"):
        builder(datetime(2026, 9, 6))
