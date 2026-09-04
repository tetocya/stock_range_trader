"""Network-free Phase 2 pipeline smoke test."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
from phase2_helpers import canonical_bars, master_frame

from backtest import BatchBacktestRunner
from config import load_strategy_config
from data import CANONICAL_COLUMNS
from data.cache import CacheManager, CacheRequest
from data.providers import YFinanceProvider
from reports import (
    Phase2RunMetadata,
    annotate_phase2_output,
    write_phase2_csv,
    write_run_manifest,
)
from screening import BatchScreener
from universe import build_japanese_equity_universe


def _mock_yfinance_response() -> pd.DataFrame:
    frames: dict[str, pd.DataFrame] = {}
    for ticker, phase in (("130A.T", 0.5), ("7203.T", 0.0)):
        canonical = canonical_bars(ticker, periods=180, phase=phase)
        frames[ticker] = pd.DataFrame(
            {
                "Open": canonical["raw_open"].to_numpy(),
                "High": canonical["raw_high"].to_numpy(),
                "Low": canonical["raw_low"].to_numpy(),
                "Close": canonical["raw_close"].to_numpy(),
                "Adj Close": canonical["adjusted_close"].to_numpy(),
                "Volume": canonical["raw_volume"].to_numpy(),
                "Dividends": 0.0,
                "Stock Splits": 0.0,
            },
            index=canonical["date"],
        )
    return pd.concat(frames, axis=1)


def test_mock_pipeline_download_cache_screen_backtest_and_report(tmp_path) -> None:
    as_of = date(2026, 8, 31)
    universe = build_japanese_equity_universe(master_frame(), as_of_date=as_of)
    symbols = tuple(
        sorted(universe.loc[universe["universe_included"], "yfinance_ticker"].tolist())
    )
    calls = 0

    def download(**kwargs):
        nonlocal calls
        calls += 1
        return _mock_yfinance_response()

    provider = YFinanceProvider(download_func=download, max_retries=1)
    cache = CacheManager(tmp_path / "cache")
    request = CacheRequest(
        provider="yfinance",
        dataset="daily",
        symbols=symbols,
        requested_start=date(2025, 12, 23),
        requested_end=date(2026, 9, 1),
        adjustment_mode=provider.adjustment_mode,
        universe_as_of_date=as_of,
    )
    cached = cache.get_or_fetch(
        request,
        lambda: provider.get_daily_bars(symbols, date(2025, 12, 23), date(2026, 9, 1)),
        endpoint="yfinance.download",
        library_version=provider.library_version,
        required_columns=CANONICAL_COLUMNS,
    )
    second = cache.get_or_fetch(
        request,
        lambda: provider.get_daily_bars(symbols, date(2025, 12, 23), date(2026, 9, 1)),
        endpoint="yfinance.download",
        library_version=provider.library_version,
        required_columns=CANONICAL_COLUMNS,
    )

    config = load_strategy_config("config/strategy.yaml")
    screened = BatchScreener(
        config.create_detector(),
        config.create_scorer(),
        minimum_observations=120,
    ).run(universe, second.data, as_of_date=as_of, top_n=2)
    batch = BatchBacktestRunner(config).run(screened.ranking, second.data)

    metadata = Phase2RunMetadata(
        provider="yfinance",
        requested_start=date(2025, 12, 23),
        requested_end=date(2026, 9, 1),
        actual_start=cached.manifest.actual_start
        and date.fromisoformat(cached.manifest.actual_start),
        actual_end=cached.manifest.actual_end
        and date.fromisoformat(cached.manifest.actual_end),
        adjustment_mode=provider.adjustment_mode,
        universe_as_of_date=as_of,
        analysis_design="mock_exploratory_in_sample",
    )
    ranking_path = write_phase2_csv(
        screened.ranking, tmp_path / "range_ranking_2026-08-31.csv", metadata
    )
    summary_path = write_phase2_csv(
        batch.summary, tmp_path / "batch_backtest_summary.csv", metadata
    )
    manifest_path = write_run_manifest(metadata, tmp_path / "run_manifest.json")

    assert calls == 1
    assert len(screened.ranking) == 2
    assert screened.exclusions.empty
    assert set(batch.summary["status"]) == {"unsupported"}
    assert batch.summary["error"].str.contains("always unsupported").all()
    assert ranking_path.is_file()
    assert summary_path.is_file()
    assert manifest_path.is_file()
    assert set(pd.read_csv(summary_path)["provider"]) == {"yfinance"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["provider_price_basis"] == (
        "yahoo_reported_ohlcv_auto_adjust_false;historical_split_basis_unverified"
    )

    conflicting = screened.ranking.copy()
    conflicting.loc[:, "provider"] = "jquants"
    try:
        annotate_phase2_output(conflicting, metadata)
    except ValueError as error:
        assert "conflicts" in str(error)
    else:
        raise AssertionError("conflicting provider metadata must be rejected")
