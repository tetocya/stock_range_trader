"""Deterministic Phase 2 test-data factories."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from data import CANONICAL_COLUMNS


def canonical_bars(
    symbol: str = "7203.T",
    *,
    provider: str = "yfinance",
    periods: int = 180,
    end: date = date(2026, 8, 31),
    phase: float = 0.0,
) -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=periods)
    x = np.arange(periods, dtype=float)
    close = 1_000.0 + 70.0 * np.sin(x / 6.0 + phase)
    open_ = close + 2.0 * np.cos(x / 4.0)
    high = np.maximum(open_, close) + 12.0
    low = np.minimum(open_, close) - 12.0
    volume = 200_000.0 + (x % 7) * 1_000.0
    result = pd.DataFrame(
        {
            "date": dates,
            "symbol": symbol,
            "provider": provider,
            "raw_open": open_,
            "raw_high": high,
            "raw_low": low,
            "raw_close": close,
            "raw_volume": volume,
            "turnover_value": close * volume,
            "adjusted_open": open_,
            "adjusted_high": high,
            "adjusted_low": low,
            "adjusted_close": close,
            "adjusted_volume": volume,
            "adjustment_factor": 1.0,
            "dividend": 0.0,
            "stock_split": 0.0,
            "fetched_at": datetime(2026, 9, 1, tzinfo=UTC),
        }
    )
    return result.loc[:, CANONICAL_COLUMNS]


def master_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Date": "2026-08-31",
                "Code": "130A0",
                "CoName": "Alpha",
                "CoNameEn": "Alpha",
                "S17": "10",
                "S17Nm": "情報通信・サービスその他",
                "S33": "5250",
                "S33Nm": "情報・通信業",
                "ScaleCat": "-",
                "Mkt": "0113",
                "MktNm": "グロース",
                "Mrgn": "1",
                "MrgnNm": "信用",
                "ProdCat": "011",
            },
            {
                "Date": "2026-08-31",
                "Code": "72030",
                "CoName": "Toyota",
                "CoNameEn": "Toyota",
                "S17": "6",
                "S17Nm": "自動車・輸送機",
                "S33": "3700",
                "S33Nm": "輸送用機器",
                "ScaleCat": "TOPIX Core30",
                "Mkt": "0111",
                "MktNm": "プライム",
                "Mrgn": "2",
                "MrgnNm": "貸借",
                "ProdCat": "011",
            },
        ]
    )


def jquants_bar_record(
    code: str = "72030", day: str = "2026-08-31"
) -> dict[str, object]:
    return {
        "Date": day,
        "Code": code,
        "O": 100.0,
        "H": 110.0,
        "L": 90.0,
        "C": 105.0,
        "Vo": 1_000.0,
        "Va": 105_000.0,
        "AdjFactor": 1.0,
        "AdjO": 100.0,
        "AdjH": 110.0,
        "AdjL": 90.0,
        "AdjC": 105.0,
        "AdjVo": 1_000.0,
    }
