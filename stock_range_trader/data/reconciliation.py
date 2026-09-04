"""Non-mutating reconciliation of overlapping provider observations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from data.canonical import require_single_provider

COMPARISON_FIELDS: tuple[str, ...] = (
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "raw_volume",
    "adjusted_close",
    "turnover_value",
)


@dataclass(frozen=True, slots=True)
class ProviderComparisonResult:
    """Summary warnings and immutable date-level differences."""

    summary: pd.DataFrame
    details: pd.DataFrame

    def save(self, output_dir: str | Path) -> tuple[Path, Path]:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        summary_path = directory / "provider_comparison_summary.csv"
        details_path = directory / "provider_comparison_details.csv"
        self.summary.to_csv(summary_path, index=False)
        self.details.to_csv(details_path, index=False)
        return summary_path, details_path


def compare_providers(
    jquants_bars: pd.DataFrame,
    yfinance_bars: pd.DataFrame,
    symbol_mapping: Mapping[str, str],
    *,
    price_relative_tolerance: float = 0.01,
    volume_relative_tolerance: float = 0.10,
) -> ProviderComparisonResult:
    """Compare overlap and warn; never replace either provider's values."""

    if require_single_provider(jquants_bars) != "jquants":
        raise ValueError("jquants_bars must contain only jquants data")
    if require_single_provider(yfinance_bars) != "yfinance":
        raise ValueError("yfinance_bars must contain only yfinance data")
    for name, tolerance in (
        ("price_relative_tolerance", price_relative_tolerance),
        ("volume_relative_tolerance", volume_relative_tolerance),
    ):
        if tolerance < 0:
            raise ValueError(f"{name} must be non-negative")

    summaries: list[dict[str, object]] = []
    detail_frames: list[pd.DataFrame] = []
    for jquants_code, yahoo_ticker in sorted(symbol_mapping.items()):
        official = jquants_bars.loc[
            jquants_bars["symbol"].astype(str) == jquants_code,
            ["date", *COMPARISON_FIELDS],
        ].copy()
        yahoo = yfinance_bars.loc[
            yfinance_bars["symbol"].astype(str) == yahoo_ticker,
            ["date", *COMPARISON_FIELDS],
        ].copy()
        merged = official.merge(
            yahoo,
            on="date",
            how="inner",
            suffixes=("_jquants", "_yfinance"),
            validate="one_to_one",
        ).sort_values("date", kind="stable", ignore_index=True)
        warning_fields: list[str] = []
        if not merged.empty:
            for field in COMPARISON_FIELDS:
                relative = _relative_difference(
                    merged[f"{field}_jquants"], merged[f"{field}_yfinance"]
                )
                merged[f"{field}_relative_difference"] = relative
                tolerance = (
                    volume_relative_tolerance
                    if field in {"raw_volume", "turnover_value"}
                    else price_relative_tolerance
                )
                merged[f"{field}_warning"] = relative > tolerance
                if merged[f"{field}_warning"].any():
                    warning_fields.append(field)
            merged.insert(0, "yfinance_ticker", yahoo_ticker)
            merged.insert(0, "jquants_code", jquants_code)
            merged["warning_fields"] = ",".join(warning_fields)
            detail_frames.append(merged)
        summaries.append(
            {
                "jquants_code": jquants_code,
                "yfinance_ticker": yahoo_ticker,
                "overlap_start": (
                    merged["date"].iloc[0] if not merged.empty else pd.NaT
                ),
                "overlap_end": (
                    merged["date"].iloc[-1] if not merged.empty else pd.NaT
                ),
                "overlap_observations": len(merged),
                "status": "warning" if warning_fields or merged.empty else "ok",
                "warning_fields": (
                    ",".join(warning_fields) if not merged.empty else "no_overlap"
                ),
                **{
                    f"max_{field}_relative_difference": (
                        float(merged[f"{field}_relative_difference"].max())
                        if not merged.empty
                        else np.nan
                    )
                    for field in COMPARISON_FIELDS
                },
                "comparison_policy": "J-Quants reference; no automatic overwrite",
            }
        )
    summary = pd.DataFrame.from_records(summaries)
    details = (
        pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    )
    return ProviderComparisonResult(summary, details)


def _relative_difference(reference: pd.Series, candidate: pd.Series) -> pd.Series:
    reference_values = pd.to_numeric(reference, errors="coerce").astype(float)
    candidate_values = pd.to_numeric(candidate, errors="coerce").astype(float)
    denominator = reference_values.abs().replace(0.0, np.nan)
    difference = (candidate_values - reference_values).abs() / denominator
    both_zero = reference_values.eq(0.0) & candidate_values.eq(0.0)
    return difference.where(~both_zero, 0.0)
