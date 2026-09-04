"""Download provider-isolated canonical daily prices."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from phase2_common import (
    DEFAULT_DATA_CONFIG,
    create_price_provider,
    load_phase2_config,
    load_universe_csv,
    subtract_years,
)

from data import CANONICAL_COLUMNS
from data.cache import CacheManager, CacheRequest
from reports import Phase2RunMetadata, write_phase2_csv, write_run_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download canonical daily prices.")
    parser.add_argument("--provider", choices=["jquants", "yfinance"], required=True)
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument(
        "--universe", type=Path, default=Path("outputs/universe_latest.csv")
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_DATA_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--symbols",
        help="comma-separated provider symbols to download (for example 72030,99840)",
    )
    parser.add_argument("--limit", type=int, help="download only the first N symbols")
    parser.add_argument(
        "--allow-long-run",
        action="store_true",
        help="explicitly allow an unfiltered J-Quants universe download",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.years <= 0:
        raise ValueError("years must be positive")
    exclusive_end = args.end or (date.today() + timedelta(days=1))
    start = subtract_years(exclusive_end, args.years)
    universe = load_universe_csv(args.universe)
    included = universe.loc[universe["universe_included"]]
    column = "yfinance_ticker" if args.provider == "yfinance" else "jquants_code"
    symbols = sorted(set(included[column].dropna().astype(str)))
    if not symbols:
        raise ValueError("universe contains no included symbols for this provider")
    if args.symbols:
        requested_symbols = {
            value.strip().upper() for value in args.symbols.split(",") if value.strip()
        }
        unknown = sorted(requested_symbols - set(symbols))
        if unknown:
            raise ValueError(
                "symbols are not in the supplied universe: " + ", ".join(unknown)
            )
        symbols = [symbol for symbol in symbols if symbol in requested_symbols]
    if args.limit is not None:
        if args.limit <= 0:
            raise ValueError("limit must be positive")
        symbols = symbols[: args.limit]
    if not symbols:
        raise ValueError("symbol selection is empty")
    config = load_phase2_config(args.config)
    adjustment_mode = (
        "adj_close_ratio_for_ohlc_raw_volume"
        if args.provider == "yfinance"
        else "jquants_official_adjusted_ohlcv"
    )
    request = CacheRequest(
        provider=args.provider,
        dataset="daily",
        symbols=tuple(symbols),
        requested_start=start,
        requested_end=exclusive_end,
        adjustment_mode=adjustment_mode,
        universe_as_of_date=pd.Timestamp(universe["as_of_date"].iloc[0]).date(),
    )
    cache = CacheManager(config.cache_root, config.schema_version)
    hit = (
        None
        if args.refresh
        else cache.load(request, required_columns=CANONICAL_COLUMNS)
    )
    issues: list[dict[str, str]] = []
    if hit is None:
        if args.provider == "jquants":
            request_count, minimum_seconds = estimate_jquants_run(
                len(symbols), config.jquants.min_request_interval_seconds
            )
            print(f"J-Quants target symbols: {len(symbols)}")
            print(f"Estimated API requests (minimum): {request_count}")
            print(
                "Minimum rate-limit time at "
                f"{config.jquants.min_request_interval_seconds:g}s/request: "
                f"{format_duration(minimum_seconds)}"
            )
            print("Pagination and retry attempts can increase this estimate.")
            if not (args.symbols or args.limit is not None or args.allow_long_run):
                raise RuntimeError(
                    "unfiltered J-Quants download requires --allow-long-run, "
                    "--symbols, or --limit"
                )
        provider = create_price_provider(args.provider, config)
        bars = provider.get_daily_bars(symbols, start, exclusive_end)
        issues = [
            {
                "symbol": issue.symbol,
                "status": issue.status,
                "message": issue.message,
            }
            for issue in provider.issues
        ]
        counts = (
            pd.Series([issue["status"] for issue in issues]).value_counts().to_dict()
        )
        hit = cache.store(
            request,
            bars,
            endpoint=(
                "yfinance.download"
                if args.provider == "yfinance"
                else "/v2/equities/bars/daily"
            ),
            library_version=provider.library_version,
            status_counts={str(key): int(value) for key, value in counts.items()},
            issues=issues,
            notes=["end is exclusive", "provider series are never concatenated"],
        )
    else:
        issues = hit.manifest.issues
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    price_path = output_dir / f"{args.provider}_prices.parquet"
    hit.data.to_parquet(price_path, index=False)
    issues_frame = pd.DataFrame.from_records(
        issues, columns=["symbol", "status", "message"]
    )
    actual_start = (
        date.fromisoformat(hit.manifest.actual_start)
        if hit.manifest.actual_start
        else None
    )
    actual_end = (
        date.fromisoformat(hit.manifest.actual_end) if hit.manifest.actual_end else None
    )
    metadata = Phase2RunMetadata(
        provider=args.provider,
        requested_start=start,
        requested_end=exclusive_end,
        actual_start=actual_start,
        actual_end=actual_end,
        adjustment_mode=adjustment_mode,
        universe_as_of_date=pd.Timestamp(universe["as_of_date"].iloc[0]).date(),
    )
    write_phase2_csv(
        issues_frame,
        output_dir / f"{args.provider}_download_issues.csv",
        metadata,
    )
    write_run_manifest(metadata, output_dir / f"{args.provider}_prices_manifest.json")
    print(price_path)
    return 0


def estimate_jquants_run(
    symbol_count: int, interval_seconds: float = 13.0
) -> tuple[int, float]:
    """Return the minimum symbol-page request count and conservative duration."""

    if symbol_count <= 0 or interval_seconds <= 0.0:
        raise ValueError("symbol_count and interval_seconds must be positive")
    minimum_requests = symbol_count
    return minimum_requests, minimum_requests * float(interval_seconds)


def format_duration(seconds: float) -> str:
    """Format a duration without hiding the exact number of seconds."""

    hours, remainder = divmod(int(seconds), 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {remaining_seconds}s ({seconds:g}s)"


if __name__ == "__main__":
    raise SystemExit(main())
