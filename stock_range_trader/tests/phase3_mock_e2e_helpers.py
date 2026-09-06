"""Deterministic, network-free inputs for Phase 3 CLI acceptance tests."""

from __future__ import annotations

import socket
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
import yaml

import examples.phase3_common as phase3_common
from data import CANONICAL_COLUMNS
from data.providers import JQuantsV2Provider, YFinanceProvider
from reports import (
    CANDIDATE_FREQUENCY_COLUMNS,
    EQUITY_COLUMNS,
    EXCLUSIONS_COLUMNS,
    EXECUTABLE_METRICS_COLUMNS,
    EXECUTABLE_SELECTED_PARAMETERS_COLUMNS,
    EXECUTABLE_SUMMARY_COLUMNS,
    EXECUTABLE_VALIDATION_COLUMNS,
    FOLDS_COLUMNS,
    OBSERVATION_BOUNDS_COLUMNS,
    ORDER_COLUMNS,
    SIGNAL_FOLD_SUMMARY_COLUMNS,
    SIGNAL_OBSERVATION_COLUMNS,
    SIGNAL_SELECTED_PARAMETERS_COLUMNS,
    SIGNAL_SUMMARY_COLUMNS,
    SIGNAL_VALIDATION_COLUMNS,
    TRADE_COLUMNS,
    UNIVERSE_COVERAGE_COLUMNS,
    VALIDATION_COHORT_COLUMNS,
)
from universe import UNIVERSE_COLUMNS
from walkforward import AnalysisMode, SourceSnapshot, SourceState

PROJECT_ROOT = Path(__file__).parents[1]
START = date(2024, 1, 1)
END_EXCLUSIVE = date(2024, 5, 1)
FIXED_UTC = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
CANDIDATE_IDS = ("loose", "balanced", "selective")


@dataclass(frozen=True, slots=True)
class MockPhase3Inputs:
    """Paths and immutable facts for one artificial Phase 3 experiment."""

    mode: AnalysisMode
    provider: str
    input_path: Path
    universe_path: Path
    phase3_config_path: Path
    strategy_config_path: Path
    candidate_ids: tuple[str, ...]


def write_mock_phase3_inputs(
    root: Path,
    mode: AnalysisMode,
    *,
    no_eligible_signal_candidate: bool = False,
) -> MockPhase3Inputs:
    """Write three-symbol Canonical, universe, and compact two-fold configs."""

    provider = "yfinance" if mode is AnalysisMode.SIGNAL_VALIDATION else "jquants"
    suffix = "no_eligible" if no_eligible_signal_candidate else mode.value
    input_path = root / f"mock_{provider}_{suffix}.parquet"
    universe_path = root / f"mock_universe_{suffix}.csv"
    phase3_config_path = root / f"mock_phase3_{suffix}.yaml"
    strategy_config_path = root / f"mock_strategy_{suffix}.yaml"

    _canonical_bars(provider).to_parquet(input_path, index=False)
    _universe().to_csv(universe_path, index=False)
    _write_phase3_config(
        phase3_config_path,
        no_eligible_signal_candidate=no_eligible_signal_candidate,
    )
    _write_strategy_config(strategy_config_path)
    return MockPhase3Inputs(
        mode=mode,
        provider=provider,
        input_path=input_path,
        universe_path=universe_path,
        phase3_config_path=phase3_config_path,
        strategy_config_path=strategy_config_path,
        candidate_ids=CANDIDATE_IDS,
    )


def confirmed_arguments(inputs: MockPhase3Inputs, output_dir: Path) -> list[str]:
    """Return an explicitly confirmed CLI argument vector."""

    return [
        "--input",
        str(inputs.input_path),
        "--universe",
        str(inputs.universe_path),
        "--config",
        str(inputs.phase3_config_path),
        "--strategy-config",
        str(inputs.strategy_config_path),
        "--start",
        START.isoformat(),
        "--end",
        END_EXCLUSIVE.isoformat(),
        "--output-dir",
        str(output_dir),
        "--confirm-test-evaluation",
    ]


def single_bundle(output_dir: Path) -> Path:
    """Return the sole completed experiment directory."""

    bundles = tuple(
        item
        for item in output_dir.iterdir()
        if item.is_dir() and item.name.startswith("wf3-")
    )
    if len(bundles) != 1:
        raise AssertionError(f"expected one completed bundle, found {len(bundles)}")
    return bundles[0]


def expected_schemas(mode: AnalysisMode) -> dict[str, tuple[str, ...]]:
    """Return the public fixed CSV schema for a report lane."""

    common = {
        "walk_forward_folds.csv": FOLDS_COLUMNS,
        "fold_observation_bounds.csv": OBSERVATION_BOUNDS_COLUMNS,
        "validation_cohort.csv": VALIDATION_COHORT_COLUMNS,
        "universe_coverage.csv": UNIVERSE_COVERAGE_COLUMNS,
        "candidate_selection_frequency.csv": CANDIDATE_FREQUENCY_COLUMNS,
        "walk_forward_exclusions.csv": EXCLUSIONS_COLUMNS,
    }
    if mode is AnalysisMode.SIGNAL_VALIDATION:
        return {
            **common,
            "candidate_validation_results.csv": SIGNAL_VALIDATION_COLUMNS,
            "selected_parameters.csv": SIGNAL_SELECTED_PARAMETERS_COLUMNS,
            "walk_forward_summary.csv": SIGNAL_SUMMARY_COLUMNS,
            "oos_signal_observations.csv": SIGNAL_OBSERVATION_COLUMNS,
            "oos_signal_fold_summary.csv": SIGNAL_FOLD_SUMMARY_COLUMNS,
        }
    return {
        **common,
        "candidate_validation_results.csv": EXECUTABLE_VALIDATION_COLUMNS,
        "selected_parameters.csv": EXECUTABLE_SELECTED_PARAMETERS_COLUMNS,
        "walk_forward_summary.csv": EXECUTABLE_SUMMARY_COLUMNS,
        "oos_executable_metrics.csv": EXECUTABLE_METRICS_COLUMNS,
        "oos_trade_log.csv": TRADE_COLUMNS,
        "oos_order_log.csv": ORDER_COLUMNS,
        "oos_equity_curve.csv": EQUITY_COLUMNS,
    }


def install_deterministic_offline_runtime(monkeypatch: Any) -> list[str]:
    """Fix provenance/time and fail immediately on any external communication."""

    source = SourceSnapshot(
        source_state=SourceState.CLEAN,
        git_root="/artificial/source",
        git_commit_sha="a" * 40,
        git_branch="main",
        worktree_dirty=False,
        source_tree_sha256="b" * 64,
        reproducibility_status="reproducible",
    )
    monkeypatch.setattr(
        phase3_common.SourceStateResolver,
        "resolve",
        lambda self, path: source,
    )
    monkeypatch.setattr(phase3_common, "utc_now", lambda: FIXED_UTC)

    calls: list[str] = []

    def blocker(name: str):
        def forbidden(*args: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"external communication attempted through {name}")

        return forbidden

    monkeypatch.setattr(socket, "create_connection", blocker("socket"))
    monkeypatch.setattr(urllib.request, "urlopen", blocker("urllib"))
    monkeypatch.setattr(requests.sessions.Session, "request", blocker("requests"))
    monkeypatch.setattr(
        YFinanceProvider,
        "get_daily_bars",
        blocker("yfinance_daily_bars"),
    )
    monkeypatch.setattr(
        JQuantsV2Provider,
        "get_daily_bars",
        blocker("jquants_daily_bars"),
    )
    monkeypatch.setattr(
        JQuantsV2Provider,
        "get_universe",
        blocker("jquants_universe"),
    )
    monkeypatch.setattr(
        JQuantsV2Provider,
        "get_trading_calendar",
        blocker("jquants_calendar"),
    )
    return calls


def _canonical_bars(provider: str) -> pd.DataFrame:
    dates = pd.bdate_range(START, END_EXCLUSIVE - pd.Timedelta(days=1))
    mappings = (
        (("1301.T", "7203.T", "9984.T"))
        if provider == "yfinance"
        else (("13010", "72030", "99840"))
    )
    frames: list[pd.DataFrame] = []
    for phase, symbol in enumerate(mappings):
        position = np.arange(len(dates), dtype=float)
        angle = 2.0 * np.pi * position / 8.0 + phase * 0.7
        adjusted_close = 100.0 + 10.0 * np.sin(angle)
        raw_close = (
            adjusted_close.copy()
            if provider == "jquants"
            else 50.0 + 5.0 * np.sin(angle)
        )
        volume = np.full(len(dates), 100_000.0)
        frames.append(
            pd.DataFrame(
                {
                    "date": dates,
                    "symbol": symbol,
                    "provider": provider,
                    "raw_open": raw_close,
                    "raw_high": raw_close + 1.0,
                    "raw_low": raw_close - 1.0,
                    "raw_close": raw_close,
                    "raw_volume": volume,
                    "turnover_value": raw_close * volume,
                    "adjusted_open": adjusted_close,
                    "adjusted_high": adjusted_close + 1.0,
                    "adjusted_low": adjusted_close - 1.0,
                    "adjusted_close": adjusted_close,
                    "adjusted_volume": volume,
                    "adjustment_factor": 1.0,
                    "dividend": 0.0,
                    "stock_split": 0.0,
                    "fetched_at": datetime(2024, 5, 1, tzinfo=UTC),
                },
                columns=CANONICAL_COLUMNS,
            )
        )
    return pd.concat(frames, ignore_index=True)


def _universe() -> pd.DataFrame:
    rows = []
    for code, ticker in (
        ("13010", "1301.T"),
        ("72030", "7203.T"),
        ("99840", "9984.T"),
    ):
        rows.append(
            {
                "as_of_date": "2023-12-29",
                "jquants_code": code,
                "company_name": f"Mock {code}",
                "market_segment_code": "0111",
                "market_segment_name": "Prime",
                "sector17_code": "1",
                "sector17_name": "Mock sector",
                "sector33_code": "1",
                "sector33_name": "Mock industry",
                "product_category": "011",
                "yfinance_ticker": ticker,
                "universe_included": True,
                "exclusion_reason": "",
            }
        )
    return pd.DataFrame.from_records(rows, columns=UNIVERSE_COLUMNS)


def _write_phase3_config(
    path: Path,
    *,
    no_eligible_signal_candidate: bool,
) -> None:
    with (PROJECT_ROOT / "config" / "phase3.yaml").open(encoding="utf-8") as source:
        config = yaml.safe_load(source)
    for name in ("signal_fold_schedule", "executable_fold_schedule"):
        config[name].update(
            {
                "train_months": 1,
                "validation_months": 1,
                "test_months": 1,
                "step_months": 1,
                "minimum_folds": 2,
            }
        )
    config["signal_fold_schedule"].update(
        {"forward_sessions": 2, "embargo_sessions": 2}
    )
    config["executable_fold_schedule"].update(
        {"forward_sessions": 0, "embargo_sessions": 0}
    )
    config["signal_selection"]["minimum_observation_count"] = (
        100_000 if no_eligible_signal_candidate else 1
    )
    config["executable_selection"].update(
        {
            "minimum_traded_symbol_count": 1,
            "minimum_trading_symbol_ratio": 0.1,
            "minimum_total_trade_count": 1,
            "minimum_finite_sharpe_count": 1,
        }
    )
    config["signal_candidate_catalog"]["candidates"] = [
        {
            "id": "loose",
            "buy_atr_multiplier": 0.0,
            "range_score_threshold": 0.0,
            "adx_entry_max": 1_000.0,
        },
        {
            "id": "balanced",
            "buy_atr_multiplier": 0.25,
            "range_score_threshold": 0.0,
            "adx_entry_max": 1_000.0,
        },
        {
            "id": "selective",
            "buy_atr_multiplier": 0.75,
            "range_score_threshold": 0.0,
            "adx_entry_max": 1_000.0,
        },
    ]
    config["executable_candidate_catalog"]["candidates"] = [
        {
            "id": "loose",
            "buy_atr_multiplier": 0.0,
            "sell_atr_multiplier": 0.0,
            "range_score_threshold": 0.0,
            "adx_entry_max": 1_000.0,
        },
        {
            "id": "balanced",
            "buy_atr_multiplier": 0.25,
            "sell_atr_multiplier": 0.25,
            "range_score_threshold": 0.0,
            "adx_entry_max": 1_000.0,
        },
        {
            "id": "selective",
            "buy_atr_multiplier": 0.75,
            "sell_atr_multiplier": 0.75,
            "range_score_threshold": 0.0,
            "adx_entry_max": 1_000.0,
        },
    ]
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _write_strategy_config(path: Path) -> None:
    with (PROJECT_ROOT / "config" / "strategy.yaml").open(encoding="utf-8") as source:
        config = yaml.safe_load(source)
    config.update(
        {
            "sma_period": 3,
            "atr_period": 3,
            "adx_period": 3,
            "range_window": 4,
            "slope_lookback": 2,
            "normalized_slope_limit": 1.0,
            "adx_score_limit": 1_000.0,
            "crossing_target": 1,
            "stability_window": 2,
            "stability_cv_limit": 10.0,
            "liquidity_window": 2,
            "median_trading_value_target": 1.0,
            "range_exit_threshold": 0.0,
            "adx_exit_min": 1_000.0,
            "range_breakdown_days": 3,
            "stop_loss_pct": 0.5,
            "max_holding_days": 10,
        }
    )
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
