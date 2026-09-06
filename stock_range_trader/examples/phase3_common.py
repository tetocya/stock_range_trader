"""Shared local-only preflight and orchestration for Phase 3 CLI entry points."""

# Ruff cannot place project imports above the direct-script path bootstrap.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    Phase3Config,
    StrategyConfig,
    load_phase3_config,
    load_strategy_config,
)
from data import (
    CANONICAL_COLUMNS,
    normalize_canonical_frame,
    provider_price_basis,
    require_single_provider,
    validate_canonical_bars,
)
from reports import (
    ExecutableWalkForwardReportBuilder,
    SignalWalkForwardReportBuilder,
    WalkForwardReportWriter,
)
from universe import UNIVERSE_COLUMNS
from walkforward import (
    AnalysisMode,
    ExecutableCandidateCatalog,
    ExecutableCandidateSelector,
    ExecutableOutcomeEvaluator,
    ExecutableWalkForwardAggregator,
    ExecutableWalkForwardRunner,
    ExperimentIdentity,
    ExperimentIdentityBuilder,
    FormalOOSAssessment,
    ProviderCapability,
    ProviderCapabilityRegistry,
    PurgePolicy,
    SignalCandidateCatalog,
    SignalCandidateSelector,
    SignalOutcomeEvaluator,
    SignalWalkForwardAggregator,
    SignalWalkForwardRunner,
    SourceSnapshot,
    SourceStateResolver,
    UniverseAssessment,
    WalkForwardRunMetadata,
    assess_formal_oos,
    assess_universe,
    build_config_artifact_fingerprint,
    build_input_artifact_fingerprint,
    filter_prices_to_universe,
    generate_fold_schedule,
    utc_now,
)

DEFAULT_PHASE3_CONFIG = PROJECT_ROOT / "config" / "phase3.yaml"
DEFAULT_STRATEGY_CONFIG = PROJECT_ROOT / "config" / "strategy.yaml"


@dataclass(frozen=True, slots=True)
class Phase3Preflight:
    """Validated inputs and identity prepared without any outcome evaluation."""

    mode: AnalysisMode
    phase3_config: Phase3Config
    strategy_config: StrategyConfig
    bars: pd.DataFrame
    catalog: object
    selection_policy: object
    capability: ProviderCapability
    schedule: object
    source: SourceSnapshot
    universe: UniverseAssessment
    input_artifact: object
    identity: ExperimentIdentity
    formal_oos: FormalOOSAssessment
    phase3_config_artifact: object
    strategy_config_artifact: object
    requested_start: date
    requested_end_exclusive: date
    output_dir: Path
    parent_experiment_id: str | None
    change_reason: str | None


def build_parser(mode: AnalysisMode) -> argparse.ArgumentParser:
    label = "Signal" if mode is AnalysisMode.SIGNAL_VALIDATION else "Executable"
    parser = argparse.ArgumentParser(
        description=f"Run local Phase 3 {label} walk-forward validation."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_PHASE3_CONFIG)
    parser.add_argument("--strategy-config", type=Path, default=DEFAULT_STRATEGY_CONFIG)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--parent-experiment-id")
    parser.add_argument("--change-reason")
    parser.add_argument("--require-formal-oos", action="store_true")
    confirmation = parser.add_mutually_exclusive_group(required=True)
    confirmation.add_argument("--preflight-only", action="store_true")
    confirmation.add_argument("--confirm-test-evaluation", action="store_true")
    return parser


def run_phase3_cli(mode: AnalysisMode, argv: Sequence[str] | None = None) -> int:
    """Run one explicitly selected mode without downloading external data."""

    parser = build_parser(mode)
    args = parser.parse_args(argv)
    if (args.parent_experiment_id is None) != (args.change_reason is None):
        parser.error(
            "--parent-experiment-id and --change-reason must be supplied together"
        )
    if args.start >= args.end:
        parser.error("--start must be before exclusive --end")

    started_at = utc_now()
    preflight = prepare_preflight(mode, args)
    print_preflight(preflight)
    if args.require_formal_oos and not preflight.formal_oos.eligible:
        reasons = ", ".join(preflight.formal_oos.reasons)
        raise ValueError(f"formal OOS eligibility is required: {reasons}")
    if args.preflight_only:
        print("Preflight completed. No Validation or Test evaluation was run.")
        return 0

    writer = WalkForwardReportWriter()
    try:
        if mode is AnalysisMode.SIGNAL_VALIDATION:
            evaluator = SignalOutcomeEvaluator(
                preflight.strategy_config, ProviderCapabilityRegistry()
            )
            runner = SignalWalkForwardRunner(
                evaluator,
                SignalCandidateSelector(preflight.selection_policy),
                PurgePolicy.from_schedule_config(preflight.schedule.config),
            )
            run_result = runner.run(
                preflight.bars, preflight.schedule, preflight.catalog
            )
            aggregate = SignalWalkForwardAggregator().aggregate(run_result)
            builder = SignalWalkForwardReportBuilder()
        else:
            evaluator = ExecutableOutcomeEvaluator(
                preflight.strategy_config, ProviderCapabilityRegistry()
            )
            runner = ExecutableWalkForwardRunner(
                evaluator,
                ExecutableCandidateSelector(preflight.selection_policy),
            )
            run_result = runner.run(
                preflight.bars, preflight.schedule, preflight.catalog
            )
            aggregate = ExecutableWalkForwardAggregator().aggregate(run_result)
            builder = ExecutableWalkForwardReportBuilder()
        completed_at = utc_now()
        metadata = WalkForwardRunMetadata(
            identity=preflight.identity,
            source=preflight.source,
            input_artifact=preflight.input_artifact,
            universe=preflight.universe,
            provider_capability=preflight.capability,
            phase3_config=preflight.phase3_config,
            strategy_config=preflight.strategy_config,
            schedule=preflight.schedule,
            phase3_config_artifact=preflight.phase3_config_artifact,
            strategy_config_artifact=preflight.strategy_config_artifact,
            requested_start=preflight.requested_start,
            requested_end_exclusive=preflight.requested_end_exclusive,
            formal_oos=preflight.formal_oos,
            started_at_utc=started_at,
            completed_at_utc=completed_at,
            parent_experiment_id=preflight.parent_experiment_id,
            change_reason=preflight.change_reason,
        )
        bundle = builder.build(
            run_result,
            aggregate,
            metadata,
            preflight.bars,
            preflight.catalog,
        )
        destination = writer.write(bundle, preflight.output_dir)
    except Exception as error:
        try:
            writer.write_failure_receipt(
                output_dir=preflight.output_dir,
                experiment_id=preflight.identity.experiment_id,
                analysis_mode=mode,
                failed_stage="walk_forward_or_reporting",
                exception=error,
                started_at_utc=started_at,
                failed_at_utc=utc_now(),
                source_state=preflight.source.source_state.value,
                git_commit_sha=preflight.source.git_commit_sha,
            )
        except Exception as receipt_error:
            error.add_note(
                f"failure receipt could not be written: {type(receipt_error).__name__}"
            )
        raise

    print(f"Completed: {aggregate.aggregate_status.value}")
    print(f"Report bundle: {destination}")
    return 0


def prepare_preflight(mode: AnalysisMode, args: argparse.Namespace) -> Phase3Preflight:
    """Perform all preflight work, including collision checks, before a Runner."""

    phase3_config = load_phase3_config(args.config)
    strategy_config = load_strategy_config(args.strategy_config)
    raw_prices = _load_canonical_parquet(args.input)
    requested = raw_prices.loc[
        (raw_prices["date"].dt.date >= args.start)
        & (raw_prices["date"].dt.date < args.end)
    ].copy()
    if requested.empty:
        raise ValueError("no Canonical observations exist in the requested range")
    requested = normalize_canonical_frame(requested)
    provider = require_single_provider(requested)
    validate_canonical_bars(
        requested,
        expected_provider=provider,
        start=args.start,
        end=args.end,
    )
    universe_frame = _load_universe_csv(args.universe)
    registry = ProviderCapabilityRegistry()
    capability = registry.require(
        provider,
        mode,
        require_benchmark=mode is AnalysisMode.EXECUTABLE_VALIDATION,
    )
    expected_basis = provider_price_basis(provider)
    if capability.provider_price_basis != expected_basis:
        raise ValueError("provider_price_basis capability mismatch")

    if mode is AnalysisMode.SIGNAL_VALIDATION:
        schedule_config = phase3_config.signal_fold_schedule
        catalog = SignalCandidateCatalog.from_config(
            phase3_config.signal_candidate_catalog
        )
        selection_policy = phase3_config.signal_selection
    else:
        schedule_config = phase3_config.executable_fold_schedule
        catalog = ExecutableCandidateCatalog.from_config(
            phase3_config.executable_candidate_catalog
        )
        selection_policy = phase3_config.executable_selection
    schedule = generate_fold_schedule(schedule_config, args.start, args.end)
    universe = assess_universe(
        args.universe,
        universe_frame,
        requested,
        provider=provider,
        schedule=schedule,
    )
    bars = filter_prices_to_universe(requested, universe)
    if bars.empty:
        raise ValueError(
            "no price symbols match the provider-specific included universe"
        )
    validate_canonical_bars(
        bars,
        expected_provider=provider,
        requested_symbols=set(universe.included_symbols),
        start=args.start,
        end=args.end,
    )
    input_artifact = build_input_artifact_fingerprint(args.input, bars)
    source = SourceStateResolver().resolve(PROJECT_ROOT)
    identity = ExperimentIdentityBuilder().build(
        phase3_config=phase3_config,
        strategy_config=strategy_config,
        candidate_catalog=catalog,
        selection_policy=selection_policy,
        schedule=schedule,
        provider=provider,
        analysis_mode=mode,
        provider_price_basis=expected_basis,
        input_artifact=input_artifact,
        universe=universe,
        source=source,
    )
    writer = WalkForwardReportWriter()
    writer.ensure_available(args.output_dir, identity.experiment_id)
    formal_oos = assess_formal_oos(
        source=source,
        config=phase3_config,
        input_artifact=input_artifact,
        universe=universe,
        capability_allowed=True,
        output_collision=False,
        parent_experiment_id=args.parent_experiment_id,
    )
    return Phase3Preflight(
        mode=mode,
        phase3_config=phase3_config,
        strategy_config=strategy_config,
        bars=bars,
        catalog=catalog,
        selection_policy=selection_policy,
        capability=capability,
        schedule=schedule,
        source=source,
        universe=universe,
        input_artifact=input_artifact,
        identity=identity,
        formal_oos=formal_oos,
        phase3_config_artifact=build_config_artifact_fingerprint(args.config),
        strategy_config_artifact=build_config_artifact_fingerprint(
            args.strategy_config
        ),
        requested_start=args.start,
        requested_end_exclusive=args.end,
        output_dir=args.output_dir,
        parent_experiment_id=args.parent_experiment_id,
        change_reason=args.change_reason,
    )


def print_preflight(preflight: Phase3Preflight) -> None:
    """Print the explicit Test information set before any outcome evaluation."""

    print(f"Experiment ID: {preflight.identity.experiment_id}")
    print(f"Analysis Mode: {preflight.mode.value}")
    print(f"Provider: {preflight.capability.provider}")
    print(f"Provider price basis: {preflight.capability.provider_price_basis}")
    print(
        "Input actual range: "
        f"{preflight.input_artifact.actual_start} to "
        f"{preflight.input_artifact.actual_end}"
    )
    print(
        "Requested range: "
        f"[{preflight.requested_start}, {preflight.requested_end_exclusive})"
    )
    print(f"Fold count: {len(preflight.schedule.folds)}")
    for fold in preflight.schedule.folds:
        print(
            f"  {fold.fold_id}: Validation "
            f"[{fold.validation_start}, {fold.validation_end}); "
            f"Test [{fold.test_start}, {fold.test_end})"
        )
    print("Candidate IDs: " + ", ".join(preflight.catalog.candidate_ids))
    print(f"Universe as-of date: {preflight.universe.universe_as_of_date}")
    false_folds = [
        item.fold_id
        for item in preflight.universe.fold_assessments
        if not item.point_in_time_universe
    ]
    print("Point-in-time false folds: " + (", ".join(false_folds) or "none"))
    print(f"Survivorship bias status: {preflight.universe.survivorship_bias_status}")
    print(
        f"Git state / commit: {preflight.source.source_state.value} / "
        f"{preflight.source.git_commit_sha or 'unavailable'}"
    )
    print(f"Formal OOS eligible: {str(preflight.formal_oos.eligible).lower()}")
    print("Formal OOS reasons: " + (", ".join(preflight.formal_oos.reasons) or "none"))
    print(preflight.formal_oos.claim_scope)
    print("yfinance Executable validation is forbidden.")
    print(f"Output directory: {preflight.output_dir}")
    print("Each fold evaluates zero or one Validation-selected candidate on Test.")
    if preflight.mode is AnalysisMode.SIGNAL_VALIDATION:
        print(
            "This result evaluates adjusted-price signals only. It is not an "
            "executable backtest and contains no simulated trading profit."
        )
    else:
        print(
            "Executable results are independent-capital symbol-fold distributions, "
            "not a shared portfolio."
        )


def _load_canonical_parquet(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"Canonical Parquet not found: {path}")
    frame = pd.read_parquet(path)
    missing = tuple(column for column in CANONICAL_COLUMNS if column not in frame)
    if missing:
        raise ValueError("Canonical Parquet missing columns: " + ", ".join(missing))
    frame = frame.loc[:, CANONICAL_COLUMNS].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], errors="raise", utc=True)
    return frame


def _load_universe_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"universe snapshot not found: {path}")
    frame = pd.read_csv(
        path,
        dtype={
            "jquants_code": "string",
            "market_segment_code": "string",
            "sector17_code": "string",
            "sector33_code": "string",
            "product_category": "string",
            "yfinance_ticker": "string",
        },
    )
    missing = tuple(column for column in UNIVERSE_COLUMNS if column not in frame)
    if missing:
        raise ValueError("universe snapshot missing columns: " + ", ".join(missing))
    frame = frame.loc[:, UNIVERSE_COLUMNS].copy()
    frame["as_of_date"] = pd.to_datetime(frame["as_of_date"], errors="raise")
    included = frame["universe_included"]
    if included.dtype != bool:
        normalized = included.astype("string").str.strip().str.lower()
        if not normalized.isin(["true", "false"]).all():
            raise ValueError("universe_included must contain only true or false")
        frame["universe_included"] = normalized.eq("true")
    return frame
