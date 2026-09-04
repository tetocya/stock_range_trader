"""Offline yfinance provider tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from data.providers import YFinanceProvider, yfinance_to_canonical

FIELDS = [
    "Open",
    "High",
    "Low",
    "Close",
    "Adj Close",
    "Volume",
    "Dividends",
    "Stock Splits",
]


def _single_frame(*, factor: float = 0.5) -> pd.DataFrame:
    index = pd.DatetimeIndex(["2026-08-28", "2026-08-31"], name="Date")
    return pd.DataFrame(
        {
            "Open": [100.0, 102.0],
            "High": [110.0, 112.0],
            "Low": [90.0, 92.0],
            "Close": [105.0, 107.0],
            "Adj Close": [105.0 * factor, 107.0 * factor],
            "Volume": [1_000.0, 2_000.0],
            "Dividends": [1.0, 0.0],
            "Stock Splits": [0.0, 2.0],
        },
        index=index,
    )


def _multi_frame(tickers: list[str], *, field_first: bool = False) -> pd.DataFrame:
    singles = {ticker: _single_frame(factor=1.0) for ticker in tickers}
    frame = pd.concat(singles, axis=1)
    if field_first:
        frame = frame.swaplevel(0, 1, axis=1).sort_index(axis=1)
    return frame


def test_download_arguments_are_explicit_and_end_is_exclusive() -> None:
    calls: list[dict[str, object]] = []

    def download(**kwargs):
        calls.append(kwargs)
        return _multi_frame(["7203.T"])

    provider = YFinanceProvider(download_func=download, max_retries=1)
    bars = provider.get_daily_bars(["7203.T"], date(2026, 8, 1), date(2026, 9, 1))

    assert len(bars) == 2
    assert set(bars["provider"]) == {"yfinance"}
    assert calls == [
        {
            "tickers": ["7203.T"],
            "start": "2026-08-01",
            "end": "2026-09-01",
            "interval": "1d",
            "auto_adjust": False,
            "actions": True,
            "keepna": True,
            "progress": False,
            "threads": False,
            "timeout": 20.0,
            "group_by": "ticker",
            "multi_level_index": True,
        }
    ]


def test_multiindex_both_orientations_are_normalized() -> None:
    for field_first in (False, True):
        provider = YFinanceProvider(
            download_func=lambda field_first=field_first, **kwargs: _multi_frame(
                ["7203.T", "130A.T"], field_first=field_first
            ),
            max_retries=1,
        )
        bars = provider.get_daily_bars(
            ["7203.T", "130A.T"], date(2026, 8, 1), date(2026, 9, 1)
        )

        assert set(bars["symbol"]) == {"7203.T", "130A.T"}
        assert len(bars) == 4


def test_batch_splitting_is_deterministic() -> None:
    batches: list[list[str]] = []

    def download(**kwargs):
        batch = kwargs["tickers"]
        batches.append(batch)
        return _multi_frame(batch)

    provider = YFinanceProvider(
        batch_size=2,
        download_func=download,
        max_retries=1,
    )
    provider.get_daily_bars(
        ["1301.T", "7203.T", "8306.T"],
        date(2026, 8, 1),
        date(2026, 9, 1),
    )

    assert batches == [["1301.T", "7203.T"], ["8306.T"]]


def test_missing_ticker_and_empty_response_are_recorded() -> None:
    responses = [_multi_frame(["7203.T"]), pd.DataFrame()]
    provider = YFinanceProvider(
        batch_size=2,
        download_func=lambda **kwargs: responses.pop(0),
        max_retries=1,
    )
    bars = provider.get_daily_bars(
        ["7203.T", "9984.T", "8306.T"],
        date(2026, 8, 1),
        date(2026, 9, 1),
    )

    assert set(bars["symbol"]) == {"7203.T"}
    issues = {(issue.symbol, issue.status) for issue in provider.issues}
    assert ("9984.T", "empty_response") in issues
    assert ("8306.T", "empty_response") in issues


def test_adjustment_policy_preserves_raw_volume_and_actions() -> None:
    result = yfinance_to_canonical(
        _single_frame(factor=0.5),
        ticker="7203.T",
        fetched_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert result.loc[0, "adjusted_open"] == 50.0
    assert result.loc[0, "adjusted_close"] == 52.5
    assert result.loc[0, "adjusted_volume"] == 1_000.0
    assert result.loc[0, "turnover_value"] == 105_000.0
    assert result.loc[0, "dividend"] == 1.0
    assert result.loc[1, "stock_split"] == 2.0


def test_all_nan_ticker_is_reported_instead_of_returned() -> None:
    response = _multi_frame(["7203.T", "9984.T"])
    response.loc[:, "9984.T"] = np.nan
    provider = YFinanceProvider(download_func=lambda **kwargs: response, max_retries=1)

    bars = provider.get_daily_bars(
        ["7203.T", "9984.T"], date(2026, 8, 1), date(2026, 9, 1)
    )

    assert set(bars["symbol"]) == {"7203.T"}
    assert [(issue.symbol, issue.status) for issue in provider.issues] == [
        ("9984.T", "empty_response")
    ]


def test_partial_missing_price_row_is_reported_as_invalid() -> None:
    response = _multi_frame(["7203.T"])
    response.loc[response.index[-1], ("7203.T", "Close")] = np.nan
    provider = YFinanceProvider(download_func=lambda **kwargs: response, max_retries=1)

    bars = provider.get_daily_bars(["7203.T"], date(2026, 8, 1), date(2026, 9, 1))

    assert bars.empty
    assert provider.issues[0].status == "invalid_ohlcv"
    assert "finite" in provider.issues[0].message


def test_missing_corporate_action_columns_are_not_silently_fabricated() -> None:
    response = _multi_frame(["7203.T"]).drop(
        columns=[("7203.T", "Dividends"), ("7203.T", "Stock Splits")]
    )
    provider = YFinanceProvider(download_func=lambda **kwargs: response, max_retries=1)

    bars = provider.get_daily_bars(["7203.T"], date(2026, 8, 1), date(2026, 9, 1))

    assert bars.empty
    assert provider.issues[0].status == "invalid_ohlcv"
    assert "Dividends" in provider.issues[0].message
