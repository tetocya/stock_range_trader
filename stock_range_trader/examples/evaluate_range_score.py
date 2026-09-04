"""Evaluate fixed Range Score bins on causal month-end observations."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from phase2_common import (
    DEFAULT_STRATEGY_CONFIG,
    actual_date_range,
    load_canonical_parquet,
)

from config import load_strategy_config
from data import require_single_provider
from data.providers import JQUANTS_ADJUSTMENT_MODE, YFINANCE_ADJUSTMENT_MODE
from reports import Phase2RunMetadata, write_phase2_csv, write_run_manifest
from screening import (
    RANGE_SCORE_DIVIDEND_POLICY,
    RANGE_SCORE_FORWARD_RETURN_MODE,
    SCORE_LABELS,
    evaluate_range_score_history,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate fixed Range Score bins without look-ahead."
    )
    parser.add_argument("--input", type=Path, required=True, help="canonical Parquet")
    parser.add_argument("--provider", choices=["jquants", "yfinance"], required=True)
    parser.add_argument("--forward-sessions", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--strategy-config", type=Path, default=DEFAULT_STRATEGY_CONFIG)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bars = load_canonical_parquet(args.input)
    if bars.empty:
        raise ValueError("canonical price file is empty")
    if require_single_provider(bars) != args.provider:
        raise ValueError("--provider does not match the canonical price file")
    config = load_strategy_config(args.strategy_config)
    result = evaluate_range_score_history(
        bars,
        config.create_detector(),
        config.create_scorer(),
        forward_sessions=args.forward_sessions,
    )
    actual_start, actual_end = actual_date_range(bars)
    if actual_start is None or actual_end is None:
        raise ValueError("canonical price file has no date range")
    metadata = Phase2RunMetadata(
        provider=args.provider,
        requested_start=actual_start,
        requested_end=None,
        actual_start=actual_start,
        actual_end=actual_end,
        adjustment_mode=(
            YFINANCE_ADJUSTMENT_MODE
            if args.provider == "yfinance"
            else JQUANTS_ADJUSTMENT_MODE
        ),
        universe_as_of_date=None,
        analysis_design="causal_month_end_fixed_bins_exploratory",
        dividend_policy=RANGE_SCORE_DIVIDEND_POLICY,
    )
    output_dir: Path = args.output_dir
    observation_path = output_dir / "range_score_observations.csv"
    summary_path = output_dir / "range_score_bin_summary.csv"
    exclusion_path = output_dir / "range_score_exclusions.csv"
    manifest_path = output_dir / "range_score_evaluation_manifest.json"
    write_phase2_csv(result.observations, observation_path, metadata)
    write_phase2_csv(result.summary, summary_path, metadata)
    write_phase2_csv(result.exclusions, exclusion_path, metadata)
    exclusion_records = result.exclusions.loc[
        :, ["symbol", "status", "reason"]
    ].to_dict(orient="records")
    write_run_manifest(
        metadata,
        manifest_path,
        {
            "input_file": str(args.input),
            "forward_sessions": args.forward_sessions,
            "score_bins": list(SCORE_LABELS),
            "evaluation_frequency": "last supplied trading session of each month",
            "universe_policy": (
                "supplied symbols; point-in-time universe date is unknown and "
                "survivorship bias may remain"
            ),
            "score_information_set": "evaluation date and earlier rows only",
            "forward_return_mode": RANGE_SCORE_FORWARD_RETURN_MODE,
            "exclusions_file": exclusion_path.name,
            "excluded_symbol_count": int(result.exclusions["symbol"].nunique()),
            "exclusion_count": len(result.exclusions),
            "exclusions": exclusion_records,
            "profit_factor_policy": "not_applicable_no_trading_rule",
            "confidence_interval": "normal 95% interval for mean forward return",
            "overlap_warning": (
                "monthly observations with overlapping forward windows are dependent; "
                "observation_count is not an independent sample size"
            ),
            "optimization": "none; bins and strategy parameters are fixed",
        },
    )
    print(observation_path)
    print(summary_path)
    print(exclusion_path)
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
