"""Rank a point-in-time Japanese common-stock universe by Range Score."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from phase2_common import (
    DEFAULT_DATA_CONFIG,
    DEFAULT_STRATEGY_CONFIG,
    actual_date_range,
    load_canonical_parquet,
    load_phase2_config,
    load_universe_csv,
)

from config import load_strategy_config
from data import require_single_provider
from data.providers import DownloadIssue
from reports import Phase2RunMetadata, write_phase2_csv, write_run_manifest
from screening import BatchScreener


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run same-date Range Score screening.")
    parser.add_argument("--provider", choices=["jquants", "yfinance"], required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--top", type=int)
    parser.add_argument(
        "--universe", type=Path, default=Path("outputs/universe_latest.csv")
    )
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--issues", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_DATA_CONFIG)
    parser.add_argument("--strategy-config", type=Path, default=DEFAULT_STRATEGY_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_config = load_phase2_config(args.config)
    strategy_config = load_strategy_config(args.strategy_config)
    price_path = args.prices or Path(f"outputs/{args.provider}_prices.parquet")
    bars = load_canonical_parquet(price_path)
    if not bars.empty and require_single_provider(bars) != args.provider:
        raise ValueError("--provider does not match the canonical price file")
    bars = bars.loc[bars["date"].dt.date <= args.as_of].copy()
    universe = load_universe_csv(args.universe)
    issue_path = args.issues or Path(f"outputs/{args.provider}_download_issues.csv")
    provider_issues: list[DownloadIssue] = []
    if issue_path.is_file():
        issue_frame = pd.read_csv(issue_path, dtype=str)
        issue_columns = ["symbol", "status", "message"]
        if not set(issue_columns).issubset(issue_frame.columns):
            raise ValueError("download issue file has an invalid schema")
        provider_issues = [
            DownloadIssue(row.symbol, row.status, row.message)
            for row in issue_frame.loc[:, issue_columns].itertuples(index=False)
        ]
    screener = BatchScreener(
        strategy_config.create_detector(),
        strategy_config.create_scorer(),
        minimum_observations=data_config.screening.minimum_observations,
        maximum_missing_session_ratio=(
            data_config.screening.maximum_missing_session_ratio
        ),
    )
    result = screener.run(
        universe,
        bars,
        as_of_date=args.as_of,
        top_n=args.top or data_config.screening.top_n,
        provider_issues=provider_issues,
        provider=args.provider,
    )
    actual_start, actual_end = actual_date_range(bars)
    metadata = Phase2RunMetadata(
        provider=args.provider,
        requested_start=actual_start,
        requested_end=args.as_of + timedelta(days=1),
        actual_start=actual_start,
        actual_end=actual_end,
        adjustment_mode=(
            "adj_close_ratio_for_ohlc_raw_volume"
            if args.provider == "yfinance"
            else "jquants_official_adjusted_ohlcv"
        ),
        universe_as_of_date=pd.Timestamp(universe["as_of_date"].iloc[0]).date(),
    )
    output_dir: Path = args.output_dir
    ranking_path = output_dir / f"range_ranking_{args.as_of.isoformat()}.csv"
    exclusion_path = output_dir / f"screening_exclusions_{args.as_of.isoformat()}.csv"
    write_phase2_csv(result.ranking, ranking_path, metadata)
    write_phase2_csv(result.exclusions, exclusion_path, metadata)
    write_run_manifest(metadata, output_dir / "screening_run_manifest.json")
    print(ranking_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
