"""Deterministic same-date cross-sectional Range Score screening."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from data import assess_symbol_data, canonical_to_phase1, require_single_provider
from data.providers import DownloadIssue
from universe import UNIVERSE_COLUMNS

from .range_detector import RangeDetector
from .range_score import RangeScorer

RANKING_COLUMNS: tuple[str, ...] = (
    "rank",
    "as_of_date",
    "symbol",
    "company_name",
    "market_segment",
    "sector33",
    "provider",
    "range_score",
    "trend_score",
    "mean_reversion_score",
    "stability_score",
    "liquidity_score",
    "adx",
    "atr_pct",
    "median_trading_value",
    "available_start",
    "available_end",
    "observation_count",
)
EXCLUSION_COLUMNS: tuple[str, ...] = (
    "as_of_date",
    "symbol",
    "provider_symbol",
    "company_name",
    "provider",
    "status",
    "reason",
)


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    """Ranked candidates and an exhaustive exclusion ledger."""

    as_of_date: date
    provider: str
    ranking: pd.DataFrame
    exclusions: pd.DataFrame

    def save(self, output_dir: str | Path) -> tuple[Path, Path]:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        ranking_path = directory / f"range_ranking_{self.as_of_date.isoformat()}.csv"
        exclusions_path = (
            directory / f"screening_exclusions_{self.as_of_date.isoformat()}.csv"
        )
        self.ranking.to_csv(ranking_path, index=False)
        self.exclusions.to_csv(exclusions_path, index=False)
        return ranking_path, exclusions_path


@dataclass(frozen=True, slots=True)
class BatchScreener:
    """Apply the unchanged Phase 1.1 detector/scorer independently per issue."""

    detector: RangeDetector
    scorer: RangeScorer
    minimum_observations: int = 120
    maximum_missing_session_ratio: float = 0.10

    def __post_init__(self) -> None:
        if self.minimum_observations <= 0:
            raise ValueError("minimum_observations must be positive")
        if not 0.0 <= self.maximum_missing_session_ratio < 1.0:
            raise ValueError("maximum_missing_session_ratio must be in [0, 1)")

    def run(
        self,
        universe: pd.DataFrame,
        bars: pd.DataFrame,
        *,
        as_of_date: date,
        top_n: int = 30,
        trading_dates: Iterable[date] | None = None,
        provider_issues: Sequence[DownloadIssue] = (),
        provider: str | None = None,
    ) -> ScreeningResult:
        if top_n <= 0:
            raise ValueError("top_n must be positive")
        missing = sorted(set(UNIVERSE_COLUMNS) - set(universe.columns))
        if missing:
            raise ValueError("universe missing columns: " + ", ".join(missing))
        snapshot_dates = set(pd.to_datetime(universe["as_of_date"]).dt.date)
        if snapshot_dates != {as_of_date}:
            raise ValueError(
                "universe snapshot date must exactly match screening as_of_date"
            )
        if bars.empty:
            if provider not in {"jquants", "yfinance"}:
                raise ValueError(
                    "provider must be supplied when all downloaded bars are empty"
                )
        else:
            detected_provider = require_single_provider(bars)
            if provider is not None and provider != detected_provider:
                raise ValueError("explicit provider does not match canonical bars")
            provider = detected_provider
        if not bars.empty and (bars["date"].dt.date > as_of_date).any():
            raise ValueError("bars after as_of_date are forbidden")
        assert provider is not None

        issue_map = {issue.symbol: issue for issue in provider_issues}
        ranking_records: list[dict[str, object]] = []
        exclusion_records: list[dict[str, object]] = []
        included = universe.loc[universe["universe_included"].astype(bool)].sort_values(
            "jquants_code", kind="stable"
        )
        for _, security in included.iterrows():
            code = str(security["jquants_code"])
            provider_symbol = (
                str(security["yfinance_ticker"]) if provider == "yfinance" else code
            )
            issue = issue_map.get(provider_symbol)
            symbol_bars = bars.loc[bars["symbol"].astype(str) == provider_symbol].copy()
            if issue is not None and symbol_bars.empty:
                exclusion_records.append(
                    _exclusion_record(
                        security,
                        as_of_date,
                        provider,
                        provider_symbol,
                        issue.status,
                        issue.message,
                    )
                )
                continue
            status = assess_symbol_data(
                symbol_bars,
                provider_symbol,
                expected_provider=provider,
                minimum_observations=self.minimum_observations,
                trading_dates=trading_dates,
                maximum_missing_session_ratio=self.maximum_missing_session_ratio,
            )
            if status.status != "ok":
                exclusion_records.append(
                    _exclusion_record(
                        security,
                        as_of_date,
                        provider,
                        provider_symbol,
                        status.status,
                        status.message,
                    )
                )
                continue
            if as_of_date not in set(symbol_bars["date"].dt.date):
                exclusion_records.append(
                    _exclusion_record(
                        security,
                        as_of_date,
                        provider,
                        provider_symbol,
                        "missing_as_of_date",
                        "no observation on the common screening date",
                    )
                )
                continue
            try:
                phase1 = canonical_to_phase1(
                    symbol_bars, symbol=provider_symbol, as_of_date=as_of_date
                )
                scored = self.scorer.transform(self.detector.transform(phase1))
            except (TypeError, ValueError) as error:
                exclusion_records.append(
                    _exclusion_record(
                        security,
                        as_of_date,
                        provider,
                        provider_symbol,
                        "invalid_ohlcv",
                        str(error),
                    )
                )
                continue
            latest = scored.loc[scored["date"].dt.date == as_of_date].iloc[-1]
            score_columns = [
                "range_score",
                "trend_score",
                "mean_reversion_score",
                "stability_score",
                "liquidity_score",
                "adx",
                "atr_pct",
                "median_trading_value",
            ]
            if latest[score_columns].isna().any():
                exclusion_records.append(
                    _exclusion_record(
                        security,
                        as_of_date,
                        provider,
                        provider_symbol,
                        "insufficient_history",
                        "indicator warm-up is incomplete at as_of_date",
                    )
                )
                continue
            ranking_records.append(
                {
                    "as_of_date": pd.Timestamp(as_of_date),
                    "symbol": code,
                    "company_name": security["company_name"],
                    "market_segment": security["market_segment_name"],
                    "sector33": security["sector33_name"],
                    "provider": provider,
                    **{column: float(latest[column]) for column in score_columns},
                    "available_start": phase1["date"].iloc[0],
                    "available_end": phase1["date"].iloc[-1],
                    "observation_count": len(phase1),
                }
            )

        ranking = pd.DataFrame.from_records(ranking_records)
        if ranking.empty:
            ranking = pd.DataFrame(columns=RANKING_COLUMNS)
        else:
            ranking = ranking.sort_values(
                ["range_score", "symbol"],
                ascending=[False, True],
                kind="stable",
                ignore_index=True,
            ).head(top_n)
            ranking.insert(0, "rank", range(1, len(ranking) + 1))
            ranking = ranking.loc[:, RANKING_COLUMNS]
        exclusions = pd.DataFrame.from_records(
            exclusion_records, columns=EXCLUSION_COLUMNS
        ).sort_values("symbol", kind="stable", ignore_index=True)
        return ScreeningResult(as_of_date, provider, ranking, exclusions)


def _exclusion_record(
    security: pd.Series,
    as_of_date: date,
    provider: str,
    provider_symbol: str,
    status: str,
    reason: str,
) -> dict[str, object]:
    return {
        "as_of_date": pd.Timestamp(as_of_date),
        "symbol": str(security["jquants_code"]),
        "provider_symbol": provider_symbol,
        "company_name": security["company_name"],
        "provider": provider,
        "status": status,
        "reason": reason,
    }
