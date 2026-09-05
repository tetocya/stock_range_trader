"""Run local Phase 3 Executable walk-forward validation."""

from __future__ import annotations

from collections.abc import Sequence

try:
    from .phase3_common import run_phase3_cli
except ImportError:  # Direct ``python examples/...`` execution.
    from phase3_common import run_phase3_cli

from walkforward import AnalysisMode


def main(argv: Sequence[str] | None = None) -> int:
    return run_phase3_cli(AnalysisMode.EXECUTABLE_VALIDATION, argv)


if __name__ == "__main__":
    raise SystemExit(main())
