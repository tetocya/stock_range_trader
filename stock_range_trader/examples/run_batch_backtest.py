"""Run independent Phase 1.1 backtests for ranked Phase 2 symbols."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pandas as pd
from phase2_common import (
    DEFAULT_STRATEGY_CONFIG,
    actual_date_range,
    load_canonical_parquet,
)

from backtest import BatchBacktestRunner
from config import load_strategy_config
from data import require_single_provider
from data.providers import JQUANTS_ADJUSTMENT_MODE, YFINANCE_ADJUSTMENT_MODE
from reports import Phase2RunMetadata, write_phase2_csv, write_run_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run ranked independent backtests.")
    parser.add_argument("--provider", choices=["jquants", "yfinance"], required=True)
    parser.add_argument("--ranking", type=Path, required=True)
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_STRATEGY_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ranking = pd.read_csv(
        args.ranking,
        dtype={"symbol": "string", "provider": "string"},
        parse_dates=["as_of_date"],
    )
    if ranking.empty:
        raise ValueError("ranking must contain at least one candidate")
    price_path = args.prices or Path(f"outputs/{args.provider}_prices.parquet")
    bars = load_canonical_parquet(price_path)
    if not bars.empty and require_single_provider(bars) != args.provider:
        raise ValueError("--provider does not match the canonical price file")
    as_of_date = pd.Timestamp(ranking["as_of_date"].iloc[0]).date()
    bars = bars.loc[bars["date"].dt.date <= as_of_date].copy()
    result = BatchBacktestRunner(load_strategy_config(args.config)).run(ranking, bars)
    actual_start, actual_end = actual_date_range(bars)
    metadata = Phase2RunMetadata(
        provider=args.provider,
        requested_start=actual_start,
        requested_end=as_of_date + timedelta(days=1),
        actual_start=actual_start,
        actual_end=actual_end,
        adjustment_mode=(
            YFINANCE_ADJUSTMENT_MODE
            if args.provider == "yfinance"
            else JQUANTS_ADJUSTMENT_MODE
        ),
        universe_as_of_date=as_of_date,
        analysis_design="exploratory_in_sample_not_predictive_validation",
    )
    output_dir: Path = args.output_dir
    write_phase2_csv(
        result.summary, output_dir / "batch_backtest_summary.csv", metadata
    )
    write_phase2_csv(result.trade_log, output_dir / "batch_trade_log.csv", metadata)
    write_phase2_csv(result.order_log, output_dir / "batch_order_log.csv", metadata)
    write_run_manifest(metadata, output_dir / "batch_backtest_run_manifest.json")
    print(output_dir / "batch_backtest_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
