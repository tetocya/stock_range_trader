"""Output-location guard shared by the feasibility tools."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

# Trial ledgers, approvals and saved market data live under these names. The
# feasibility census must never write into, copy from or link to them.
FORBIDDEN_PATH_COMPONENTS = frozenset({".delayed_replay", "june_trial"})


class UnsafeOutputPath(ValueError):
    """Raised instead of writing next to protected trial evidence."""


def require_new_output_dir(
    path: str | Path, *, forbidden_roots: Iterable[str | Path] = ()
) -> Path:
    """Return an absolute, not-yet-existing directory outside protected roots."""

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise UnsafeOutputPath("output_path_is_symlink")
    resolved = candidate.resolve()
    if FORBIDDEN_PATH_COMPONENTS & set(resolved.parts):
        raise UnsafeOutputPath("output_inside_protected_trial_area")
    for root in forbidden_roots:
        protected = Path(root).expanduser().resolve()
        if resolved == protected or protected in resolved.parents:
            raise UnsafeOutputPath("output_inside_forbidden_root")
    if resolved.exists():
        raise UnsafeOutputPath("output_already_exists")
    return resolved
