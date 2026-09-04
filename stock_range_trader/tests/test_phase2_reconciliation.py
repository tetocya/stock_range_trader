"""Provider reconciliation and causal Range Score evaluation tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
    assert result.exclusions.empty
    pd.testing.assert_frame_equal(bars, before)


def test_range_score_excludes_split_symbol_and_continues_normal_symbol() -> None:
    config = load_strategy_config("config/strategy.yaml")
    normal = canonical_bars("7203.T", periods=260)
    split = canonical_bars("7011.T", periods=260, phase=0.5)
    split.loc[100, "stock_split"] = 10.0

    result = evaluate_range_score_history(
        pd.concat([split, normal], ignore_index=True),
        config.create_detector(),
        config.create_scorer(),
        forward_sessions=20,
    )

    assert not result.observations.empty
    assert set(result.observations["symbol"]) == {"7203.T"}
    assert result.exclusions.to_dict(orient="records") == [
        {
            "provider": "yfinance",
            "symbol": "7011.T",
            "status": "unsupported",
            "reason": "yfinance Stock Splits != 0 in the evaluation interval",
        }
    ]


def test_range_score_forward_returns_use_adjusted_signal_close() -> None:
    config = load_strategy_config("config/strategy.yaml")
    bars = canonical_bars(periods=260)
    changed_execution = bars.copy()
    raw_columns = ["raw_open", "raw_high", "raw_low", "raw_close"]
    changed_execution.loc[:, raw_columns] *= 5.0
    changed_execution.loc[:, "turnover_value"] *= 5.0

    original = evaluate_range_score_history(
        bars,
        config.create_detector(),
        config.create_scorer(),
        forward_sessions=20,
    )
    changed = evaluate_range_score_history(
        changed_execution,
        config.create_detector(),
        config.create_scorer(),
        forward_sessions=20,
    )

    pd.testing.assert_series_equal(
        original.observations["forward_return"],
        changed.observations["forward_return"],
    )


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


def test_range_score_evaluation_cli_writes_complete_metrics_and_manifest(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    input_path = tmp_path / "prices.parquet"
    output_dir = tmp_path / "evaluation"
    normal = canonical_bars(periods=260)
    split = canonical_bars("7011.T", periods=260, phase=0.5)
    split.loc[100, "stock_split"] = 10.0
    pd.concat([normal, split], ignore_index=True).to_parquet(input_path, index=False)

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "examples" / "evaluate_range_score.py"),
            "--input",
            str(input_path),
            "--provider",
            "yfinance",
            "--forward-sessions",
            "20",
            "--output-dir",
            str(output_dir),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    observations = pd.read_csv(output_dir / "range_score_observations.csv")
    summary = pd.read_csv(output_dir / "range_score_bin_summary.csv")
    exclusions = pd.read_csv(output_dir / "range_score_exclusions.csv")
    manifest = json.loads(
        (output_dir / "range_score_evaluation_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert not observations.empty
    assert set(observations["symbol"]) == {"7203.T"}
    assert exclusions.loc[0, "symbol"] == "7011.T"
    assert exclusions.loc[0, "status"] == "unsupported"
    assert "Stock Splits != 0" in exclusions.loc[0, "reason"]
    assert {
        "mean_reversion_target_hit",
        "maximum_adverse_excursion",
        "maximum_favorable_excursion",
        "maximum_drawdown",
    }.issubset(observations.columns)
    assert {
        "symbol_count",
        "observation_count",
        "mean_forward_return",
        "median_forward_return",
        "mean_reversion_target_hit_rate",
        "win_rate",
        "profit_factor",
        "forward_return_standard_error",
        "forward_return_ci95_lower",
        "forward_return_ci95_upper",
    }.issubset(summary.columns)
    assert manifest["forward_sessions"] == 20
    assert "overlapping" in manifest["overlap_warning"]
    assert manifest["profit_factor_policy"] == "not_applicable_no_trading_rule"
    assert manifest["excluded_symbol_count"] == 1
    assert manifest["exclusion_count"] == 1
    assert manifest["exclusions_file"] == "range_score_exclusions.csv"
    assert manifest["exclusions"] == [
        {
            "symbol": "7011.T",
            "status": "unsupported",
            "reason": "yfinance Stock Splits != 0 in the evaluation interval",
        }
    ]
    assert manifest["forward_return_mode"] == (
        "provider_adjusted_signal_close;cash_dividends_not_added;"
        "provider_adjustment_may_include_distributions"
    )
    assert manifest["execution_price_mode"] == (
        "provider_reported_ohlcv_executable_only_if_price_basis_verified;"
        "yfinance_unsupported"
    )
    assert manifest["provider_price_basis"] == (
        "yahoo_reported_ohlcv_auto_adjust_false;historical_split_basis_unverified"
    )
