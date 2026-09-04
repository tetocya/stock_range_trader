"""Compare, but never splice, overlapping J-Quants and Yahoo bars."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from phase2_common import (
    DEFAULT_DATA_CONFIG,
    actual_date_range,
    load_canonical_parquet,
    load_phase2_config,
)

from data import compare_providers
from reports import Phase2RunMetadata, write_phase2_csv, write_run_manifest
from universe import jquants_to_yfinance

JAPANESE_ISSUE_STEM = re.compile(r"^[0-9]{3}[0-9A-Z]$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare provider overlap.")
    parser.add_argument(
        "--symbols", required=True, help="Comma-separated J-Quants codes"
    )
    parser.add_argument(
        "--jquants-prices", type=Path, default=Path("outputs/jquants_prices.parquet")
    )
    parser.add_argument(
        "--yfinance-prices", type=Path, default=Path("outputs/yfinance_prices.parquet")
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_DATA_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    codes = sorted(
        {
            _normalize_jquants_code(value)
            for value in args.symbols.split(",")
            if value.strip()
        }
    )
    if not codes:
        raise ValueError("at least one symbol is required")
    mapping = {code: jquants_to_yfinance(code) for code in codes}
    official = load_canonical_parquet(args.jquants_prices)
    yahoo = load_canonical_parquet(args.yfinance_prices)
    config = load_phase2_config(args.config).comparison
    result = compare_providers(
        official,
        yahoo,
        mapping,
        price_relative_tolerance=config.price_relative_tolerance,
        volume_relative_tolerance=config.volume_relative_tolerance,
    )
    official_start, official_end = actual_date_range(official)
    yahoo_start, yahoo_end = actual_date_range(yahoo)
    starts = [value for value in (official_start, yahoo_start) if value is not None]
    ends = [value for value in (official_end, yahoo_end) if value is not None]
    actual_start = max(starts) if starts else None
    actual_end = min(ends) if ends else None
    if (
        actual_start is not None
        and actual_end is not None
        and actual_start > actual_end
    ):
        actual_start = None
        actual_end = None
    as_of = actual_end or max(ends)
    metadata = Phase2RunMetadata(
        provider="jquants_vs_yfinance",
        requested_start=actual_start,
        requested_end=(actual_end + timedelta(days=1) if actual_end else None),
        actual_start=actual_start,
        actual_end=actual_end,
        adjustment_mode="provider_native_no_replacement",
        universe_as_of_date=as_of,
        analysis_design="overlap_reconciliation_only",
    )
    output_dir: Path = args.output_dir
    write_phase2_csv(
        result.summary, output_dir / "provider_comparison_summary.csv", metadata
    )
    write_phase2_csv(
        result.details, output_dir / "provider_comparison_details.csv", metadata
    )
    write_run_manifest(metadata, output_dir / "provider_comparison_manifest.json")
    print(output_dir / "provider_comparison_summary.csv")
    return 0


def _normalize_jquants_code(value: str) -> str:
    normalized = value.strip().upper()
    if JAPANESE_ISSUE_STEM.fullmatch(normalized):
        normalized = f"{normalized}0"
    jquants_to_yfinance(normalized)
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())
