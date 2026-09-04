"""Shared CLI helpers for Phase 2 examples."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from config import DataSourcesConfig, load_data_sources_config
from data import CANONICAL_COLUMNS
from data.providers import JQuantsV2Provider, YFinanceProvider

PROJECT_ROOT = Path(__file__).parents[1]
DEFAULT_DATA_CONFIG = PROJECT_ROOT / "config" / "data_sources.yaml"
DEFAULT_STRATEGY_CONFIG = PROJECT_ROOT / "config" / "strategy.yaml"


def create_price_provider(name: str, config: DataSourcesConfig):
    if name == "jquants":
        values = config.jquants
        return JQuantsV2Provider(
            min_request_interval_seconds=values.min_request_interval_seconds,
            max_retries=values.max_retries,
            timeout_seconds=values.timeout_seconds,
        )
    if name == "yfinance":
        values = config.yfinance
        return YFinanceProvider(
            batch_size=values.batch_size,
            threads=values.threads,
            timeout_seconds=values.timeout_seconds,
            max_retries=values.max_retries,
        )
    raise ValueError(f"unsupported provider: {name}")


def load_phase2_config(path: Path) -> DataSourcesConfig:
    return load_data_sources_config(path)


def load_universe_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"universe snapshot not found: {path}")
    frame = pd.read_csv(
        path,
        dtype={
            "jquants_code": "string",
            "market_segment_code": "string",
            "sector17_code": "string",
            "sector33_code": "string",
            "product_category": "string",
            "yfinance_ticker": "string",
        },
        parse_dates=["as_of_date"],
    )
    if frame["universe_included"].dtype != bool:
        frame["universe_included"] = (
            frame["universe_included"].astype(str).str.lower() == "true"
        )
    return frame


def load_canonical_parquet(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"canonical price file not found: {path}")
    frame = pd.read_parquet(path)
    missing = sorted(set(CANONICAL_COLUMNS) - set(frame.columns))
    if missing:
        raise ValueError("canonical price file missing columns: " + ", ".join(missing))
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], errors="raise", utc=True)
    return frame.loc[:, CANONICAL_COLUMNS]


def actual_date_range(frame: pd.DataFrame) -> tuple[date | None, date | None]:
    if frame.empty:
        return None, None
    dates = pd.to_datetime(frame["date"], errors="raise")
    return dates.min().date(), dates.max().date()


def subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)
