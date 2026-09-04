"""Backtest reporting and visualization."""

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
]
