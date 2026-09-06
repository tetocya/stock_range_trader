"""Backtest reporting and visualization."""

from importlib import import_module

from .phase2_report import (
    Phase2RunMetadata,
    annotate_phase2_output,
    write_phase2_csv,
    write_run_manifest,
)
from .plotting import (
    generate_all_plots,
    plot_drawdown,
    plot_equity_curve,
    plot_price_chart,
)
from .report import format_console_report

_WALK_FORWARD_EXPORTS = frozenset(
    {
        "CANDIDATE_FREQUENCY_COLUMNS",
        "COMMON_FILENAMES",
        "EQUITY_COLUMNS",
        "EXCLUSIONS_COLUMNS",
        "EXECUTABLE_FILENAMES",
        "EXECUTABLE_METRICS_COLUMNS",
        "EXECUTABLE_SELECTED_PARAMETERS_COLUMNS",
        "EXECUTABLE_SUMMARY_COLUMNS",
        "EXECUTABLE_VALIDATION_COLUMNS",
        "FOLDS_COLUMNS",
        "OBSERVATION_BOUNDS_COLUMNS",
        "ORDER_COLUMNS",
        "PROVENANCE_COLUMNS",
        "SIGNAL_FILENAMES",
        "SIGNAL_FOLD_SUMMARY_COLUMNS",
        "SIGNAL_OBSERVATION_COLUMNS",
        "SIGNAL_SELECTED_PARAMETERS_COLUMNS",
        "SIGNAL_SUMMARY_COLUMNS",
        "SIGNAL_VALIDATION_COLUMNS",
        "TRADE_COLUMNS",
        "UNIVERSE_COVERAGE_COLUMNS",
        "VALIDATION_COHORT_COLUMNS",
        "ExecutableWalkForwardReportBuilder",
        "ExperimentAlreadyExistsError",
        "ReportWriteError",
        "SignalWalkForwardReportBuilder",
        "WalkForwardReportBundle",
        "WalkForwardReportTable",
        "WalkForwardReportWriter",
    }
)


def __getattr__(name: str):
    """Load Phase 3 reporting only when a caller requests its public API."""

    if name not in _WALK_FORWARD_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(".walk_forward_report", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


__all__ = [
    "Phase2RunMetadata",
    "annotate_phase2_output",
    "format_console_report",
    "generate_all_plots",
    "plot_drawdown",
    "plot_equity_curve",
    "plot_price_chart",
    "write_phase2_csv",
    "write_run_manifest",
    "CANDIDATE_FREQUENCY_COLUMNS",
    "COMMON_FILENAMES",
    "EQUITY_COLUMNS",
    "EXCLUSIONS_COLUMNS",
    "EXECUTABLE_FILENAMES",
    "EXECUTABLE_METRICS_COLUMNS",
    "EXECUTABLE_SELECTED_PARAMETERS_COLUMNS",
    "EXECUTABLE_SUMMARY_COLUMNS",
    "EXECUTABLE_VALIDATION_COLUMNS",
    "FOLDS_COLUMNS",
    "OBSERVATION_BOUNDS_COLUMNS",
    "ORDER_COLUMNS",
    "PROVENANCE_COLUMNS",
    "SIGNAL_FILENAMES",
    "SIGNAL_FOLD_SUMMARY_COLUMNS",
    "SIGNAL_OBSERVATION_COLUMNS",
    "SIGNAL_SELECTED_PARAMETERS_COLUMNS",
    "SIGNAL_SUMMARY_COLUMNS",
    "SIGNAL_VALIDATION_COLUMNS",
    "TRADE_COLUMNS",
    "UNIVERSE_COVERAGE_COLUMNS",
    "VALIDATION_COHORT_COLUMNS",
    "ExecutableWalkForwardReportBuilder",
    "ExperimentAlreadyExistsError",
    "ReportWriteError",
    "SignalWalkForwardReportBuilder",
    "WalkForwardReportBundle",
    "WalkForwardReportTable",
    "WalkForwardReportWriter",
]
