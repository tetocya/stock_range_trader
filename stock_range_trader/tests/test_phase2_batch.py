"""Cross-sectional screening and independent batch-backtest tests."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from phase2_helpers import canonical_bars, master_frame

from backtest import BatchBacktestRunner
from config import load_strategy_config
from data import CanonicalDataError, canonical_to_phase1, empty_canonical_frame
from data.providers import DownloadIssue
from metrics import calculate_backtest_metrics
from screening import BatchScreener
from universe import build_japanese_equity_universe


class PassthroughDetector:
    def transform(self, frame: pd.DataFrame) -> pd.DataFrame:
        return frame.copy()


class TiedScorer:
    def transform(self, frame: pd.DataFrame) -> pd.DataFrame:
        result = frame.copy()
        result["range_score"] = 75.0
        result["trend_score"] = 70.0
        result["mean_reversion_score"] = 80.0
        result["stability_score"] = 75.0
        result["liquidity_score"] = 75.0
        result["adx"] = 20.0
        result["atr_pct"] = 0.02
        result["median_trading_value"] = result["turnover_value"]
        return result


def _universe() -> pd.DataFrame:
    return build_japanese_equity_universe(master_frame(), as_of_date=date(2026, 8, 31))


def test_screening_tie_order_is_deterministic_and_missing_date_is_recorded() -> None:
    complete = canonical_bars("7203.T", periods=3)
    missing_date = canonical_bars("130A.T", periods=3, end=date(2026, 8, 28))
    screener = BatchScreener(
        PassthroughDetector(),
        TiedScorer(),
        minimum_observations=1,
    )

    partial = screener.run(
        _universe(),
        pd.concat([complete, missing_date], ignore_index=True),
        as_of_date=date(2026, 8, 31),
    )

    assert list(partial.ranking["symbol"]) == ["72030"]
    assert partial.exclusions.loc[0, "symbol"] == "130A0"
    assert partial.exclusions.loc[0, "status"] == "missing_as_of_date"

    all_complete = pd.concat(
        [complete, canonical_bars("130A.T", periods=3)], ignore_index=True
    )
    tied = screener.run(_universe(), all_complete, as_of_date=date(2026, 8, 31))
    assert list(tied.ranking["symbol"]) == ["130A0", "72030"]
    assert list(tied.ranking["rank"]) == [1, 2]


def test_screening_rejects_future_and_mixed_provider_data() -> None:
    future = canonical_bars(periods=3, end=date(2026, 9, 1))
    screener = BatchScreener(
        PassthroughDetector(), TiedScorer(), minimum_observations=1
    )

    with pytest.raises(ValueError, match="after as_of_date"):
        screener.run(_universe(), future, as_of_date=date(2026, 8, 31))

    mixed = pd.concat(
        [
            canonical_bars("7203.T", periods=3),
            canonical_bars("72030", provider="jquants", periods=3),
        ],
        ignore_index=True,
    )
    with pytest.raises(CanonicalDataError, match="mixing is forbidden"):
        screener.run(_universe(), mixed, as_of_date=date(2026, 8, 31))


def test_screening_records_every_symbol_when_all_downloads_fail() -> None:
    issues = [
        DownloadIssue("130A.T", "download_failed", "mock failure"),
        DownloadIssue("7203.T", "empty_response", "mock empty"),
    ]
    result = BatchScreener(
        PassthroughDetector(), TiedScorer(), minimum_observations=1
    ).run(
        _universe(),
        empty_canonical_frame(),
        as_of_date=date(2026, 8, 31),
        provider="yfinance",
        provider_issues=issues,
    )

    assert result.ranking.empty
    assert list(result.exclusions["symbol"]) == ["130A0", "72030"]
    assert set(result.exclusions["status"]) == {
        "download_failed",
        "empty_response",
    }


def test_screening_rejects_a_relabelled_universe_snapshot() -> None:
    universe = _universe()
    universe.loc[:, "as_of_date"] = pd.Timestamp("2026-09-01")

    with pytest.raises(ValueError, match="snapshot date"):
        BatchScreener(PassthroughDetector(), TiedScorer(), minimum_observations=1).run(
            universe,
            canonical_bars(periods=3),
            as_of_date=date(2026, 8, 31),
        )


def test_batch_failure_does_not_stop_successful_symbol_and_matches_single_run() -> None:
    config = load_strategy_config("config/strategy.yaml")
    bars = canonical_bars("7203.T", periods=180)
    ranking = pd.DataFrame(
        [
            {
                "rank": 1,
                "symbol": "72030",
                "company_name": "Toyota",
                "provider": "yfinance",
                "range_score": 81.0,
            },
            {
                "rank": 2,
                "symbol": "99840",
                "company_name": "Missing",
                "provider": "yfinance",
                "range_score": 80.0,
            },
        ]
    )

    batch = BatchBacktestRunner(config).run(ranking, bars)
    successful = batch.summary.loc[batch.summary["symbol"] == "72030"].iloc[0]
    failed = batch.summary.loc[batch.summary["symbol"] == "99840"].iloc[0]

    phase1 = canonical_to_phase1(bars, symbol="7203.T")
    scored = config.create_scorer().transform(
        config.create_detector().transform(phase1)
    )
    single = config.create_engine().run("72030", scored)
    metrics = calculate_backtest_metrics(
        single, config.annual_trading_days, config.risk_free_rate
    )

    assert successful["status"] == "ok"
    assert successful["total_return"] == pytest.approx(metrics.total_return)
    assert successful["number_of_trades"] == metrics.number_of_trades
    assert failed["status"] == "failed"
    assert failed["number_of_trades"] == 0
    assert "no canonical bars selected" in failed["error"]


def test_batch_rejects_provider_mixing() -> None:
    config = load_strategy_config("config/strategy.yaml")
    ranking = pd.DataFrame(
        [
            {
                "symbol": "72030",
                "company_name": "Toyota",
                "provider": "yfinance",
                "range_score": 80.0,
            }
        ]
    )
    mixed = pd.concat(
        [
            canonical_bars("7203.T", periods=3),
            canonical_bars("72030", provider="jquants", periods=3),
        ],
        ignore_index=True,
    )

    with pytest.raises(CanonicalDataError, match="mixing is forbidden"):
        BatchBacktestRunner(config).run(ranking, mixed)


def test_batch_records_all_ranked_symbols_when_price_data_is_empty() -> None:
    config = load_strategy_config("config/strategy.yaml")
    ranking = pd.DataFrame(
        [
            {
                "rank": 1,
                "symbol": "72030",
                "company_name": "Toyota",
                "provider": "yfinance",
                "range_score": 80.0,
            }
        ]
    )

    result = BatchBacktestRunner(config).run(ranking, empty_canonical_frame())

    assert result.provider == "yfinance"
    assert result.summary.loc[0, "status"] == "failed"
    assert "no canonical bars selected" in result.summary.loc[0, "error"]


def test_batch_marks_unreproducible_jquants_corporate_action_unsupported() -> None:
    config = load_strategy_config("config/strategy.yaml")
    bars = canonical_bars("72030", provider="jquants", periods=180)
    bars.loc[90, "adjustment_factor"] = 0.5
    ranking = pd.DataFrame(
        [
            {
                "symbol": "72030",
                "company_name": "Toyota",
                "provider": "jquants",
                "range_score": 80.0,
            }
        ]
    )

    result = BatchBacktestRunner(config).run(ranking, bars)

    assert result.summary.loc[0, "status"] == "unsupported"
    assert "provider price basis" in result.summary.loc[0, "error"]


def test_batch_marks_yfinance_split_interval_unsupported() -> None:
    config = load_strategy_config("config/strategy.yaml")
    bars = canonical_bars("7203.T", provider="yfinance", periods=180)
    bars.loc[90, "stock_split"] = 2.0
    ranking = pd.DataFrame(
        [
            {
                "symbol": "72030",
                "company_name": "Toyota",
                "provider": "yfinance",
                "range_score": 80.0,
            }
        ]
    )

    result = BatchBacktestRunner(config).run(ranking, bars)

    assert result.summary.loc[0, "status"] == "unsupported"
    assert "provider price basis" in result.summary.loc[0, "error"]
