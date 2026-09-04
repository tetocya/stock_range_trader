"""Provider reconciliation and causal Range Score evaluation tests."""

from __future__ import annotations

import pandas as pd
from phase2_helpers import canonical_bars

from config import load_strategy_config
from data import compare_providers
from screening import evaluate_range_score_history


def test_provider_comparison_warns_without_mutating_either_series() -> None:
    official = canonical_bars("72030", provider="jquants", periods=5)
    yahoo = canonical_bars("7203.T", provider="yfinance", periods=5)
    yahoo.loc[2, "raw_close"] *= 1.20
    official_before = official.copy(deep=True)
    yahoo_before = yahoo.copy(deep=True)

    result = compare_providers(
        official,
        yahoo,
        {"72030": "7203.T"},
        price_relative_tolerance=0.01,
    )

    assert result.summary.loc[0, "status"] == "warning"
    assert "raw_close" in result.summary.loc[0, "warning_fields"]
    assert result.summary.loc[0, "comparison_policy"].endswith("no automatic overwrite")
    pd.testing.assert_frame_equal(official, official_before)
    pd.testing.assert_frame_equal(yahoo, yahoo_before)


def test_provider_comparison_records_no_overlap() -> None:
    official = canonical_bars("72030", provider="jquants", periods=2)
    yahoo = canonical_bars(
        "7203.T",
        provider="yfinance",
        periods=2,
        end=pd.Timestamp("2025-01-31").date(),
    )

    result = compare_providers(official, yahoo, {"72030": "7203.T"})

    assert result.details.empty
    assert result.summary.loc[0, "warning_fields"] == "no_overlap"


def test_month_end_score_evaluation_is_causal_and_discloses_bias() -> None:
    config = load_strategy_config("config/strategy.yaml")
    bars = canonical_bars(periods=260)
    before = bars.copy(deep=True)

    result = evaluate_range_score_history(
        bars,
        config.create_detector(),
        config.create_scorer(),
        forward_sessions=20,
    )

    assert not result.observations.empty
    assert set(result.summary["score_bin"].astype(str)) == {
        "0-40",
        "40-60",
        "60-80",
        "80-100",
    }
    assert result.observations["universe_bias"].str.contains("survivorship bias").all()
    pd.testing.assert_frame_equal(bars, before)


def test_future_price_change_does_not_change_earlier_range_score() -> None:
    config = load_strategy_config("config/strategy.yaml")
    bars = canonical_bars(periods=220)
    phase1 = bars.rename(
        columns={
            "adjusted_open": "open",
            "adjusted_high": "high",
            "adjusted_low": "low",
            "adjusted_close": "close",
            "adjusted_volume": "volume",
        }
    )[["date", "open", "high", "low", "close", "volume", "turnover_value"]]
    cutoff = 180

    original = config.create_scorer().transform(
        config.create_detector().transform(phase1)
    )
    changed = phase1.copy()
    changed.loc[cutoff + 1 :, ["open", "high", "low", "close"]] *= 3.0
    rescored = config.create_scorer().transform(
        config.create_detector().transform(changed)
    )

    pd.testing.assert_series_equal(
        original.loc[:cutoff, "range_score"],
        rescored.loc[:cutoff, "range_score"],
    )
