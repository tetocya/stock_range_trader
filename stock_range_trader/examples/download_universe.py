"""Download and classify a point-in-time J-Quants universe."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from phase2_common import DEFAULT_DATA_CONFIG, load_phase2_config

from data.cache import CacheManager, CacheRequest
from data.providers import JQuantsV2Provider
from reports import Phase2RunMetadata, write_phase2_csv, write_run_manifest
from universe import build_japanese_equity_universe, unresolved_symbols


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download a Japanese equity universe.")
    parser.add_argument("--provider", choices=["jquants"], required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_DATA_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--refresh", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_phase2_config(args.config)
    request = CacheRequest(
        provider="jquants",
        dataset="universe",
        universe_as_of_date=args.as_of,
    )
    cache = CacheManager(config.cache_root, config.schema_version)
    hit = None if args.refresh else cache.load(request)
    if hit is None:
        settings = config.jquants
        provider = JQuantsV2Provider(
            min_request_interval_seconds=settings.min_request_interval_seconds,
            max_retries=settings.max_retries,
            timeout_seconds=settings.timeout_seconds,
        )
        master = provider.get_universe(args.as_of)
        hit = cache.store(
            request,
            master,
            endpoint="/v2/equities/master",
            library_version=provider.library_version,
        )
    universe = build_japanese_equity_universe(hit.data, as_of_date=args.as_of)
    output_dir: Path = args.output_dir
    snapshot = output_dir / f"universe_{args.as_of.isoformat()}.csv"
    actual_start = (
        date.fromisoformat(hit.manifest.actual_start)
        if hit.manifest.actual_start
        else None
    )
    actual_end = (
        date.fromisoformat(hit.manifest.actual_end) if hit.manifest.actual_end else None
    )
    metadata = Phase2RunMetadata(
        provider="jquants",
        requested_start=args.as_of,
        requested_end=args.as_of + timedelta(days=1),
        actual_start=actual_start,
        actual_end=actual_end,
        adjustment_mode="not_applicable_universe_master",
        universe_as_of_date=args.as_of,
        analysis_design="point_in_time_universe_snapshot",
    )
    write_phase2_csv(universe, snapshot, metadata)
    write_phase2_csv(universe, output_dir / "universe_latest.csv", metadata)
    write_phase2_csv(
        unresolved_symbols(universe), output_dir / "unresolved_symbols.csv", metadata
    )
    write_run_manifest(metadata, output_dir / "universe_run_manifest.json")
    print(snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
