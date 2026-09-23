"""Allowlist-based output-location guard shared by the feasibility tools.

Primary rule: every output must lie strictly under an explicitly allowed root.
The allowed roots are this checkout's git-ignored ``outputs/feasibility`` and
the system temporary directory; callers may only narrow them. On top of that,
paths are refused when they are relative, contain ``..``, pass through a
symlink or alias, sit inside another git checkout (for example the June
limited-trial worktree) or inside any directory tree that holds trial evidence
(``.delayed_replay``). Names are never the only protection.

Residual limit: checks run before and immediately after the exclusive directory
creation, and files are created with ``O_EXCL | O_NOFOLLOW``. A concurrent
process that can rewrite an ancestor directory between those steps is not fully
excluded; that would need directory-descriptor-relative I/O and is out of scope.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
CHECKOUT_ROOT = PROJECT_DIR.parent
PROJECT_OUTPUT_ROOT = PROJECT_DIR / "outputs" / "feasibility"
TRIAL_EVIDENCE_MARKER = ".delayed_replay"
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class UnsafeOutputPath(ValueError):
    """Raised instead of writing outside the allowed feasibility output roots."""


def default_allowed_roots() -> tuple[Path, ...]:
    return (PROJECT_OUTPUT_ROOT.resolve(), Path(tempfile.gettempdir()).resolve())


def _canonical(path: str | Path) -> Path:
    raw = Path(path)
    if not raw.is_absolute():
        raise UnsafeOutputPath("output_path_must_be_absolute")
    if ".." in raw.parts:
        raise UnsafeOutputPath("parent_reference_not_allowed")
    normal = Path(os.path.normpath(raw))
    if normal.resolve() != normal:
        raise UnsafeOutputPath("path_contains_symlink_or_alias")
    return normal


def _allowed(roots: Iterable[str | Path] | None) -> tuple[Path, ...]:
    defaults = default_allowed_roots()
    if roots is None:
        return defaults
    narrowed = tuple(Path(root).resolve() for root in roots)
    for root in narrowed:
        if not any(root == d or d in root.parents for d in defaults):
            raise UnsafeOutputPath("allowed_root_must_narrow_default_roots")
    return narrowed


def _check_location(
    path: Path,
    allowed_roots: Iterable[str | Path] | None,
    protected_roots: Iterable[str | Path],
) -> None:
    if not any(root in path.parents for root in _allowed(allowed_roots)):
        raise UnsafeOutputPath("output_outside_allowed_roots")
    for protected in protected_roots:
        root = Path(protected).resolve()
        if path == root or root in path.parents:
            raise UnsafeOutputPath("output_inside_protected_root")
    nearest_checkout = None
    for ancestor in (path, *path.parents):
        if ancestor.name == TRIAL_EVIDENCE_MARKER or (
            ancestor != path and (ancestor / TRIAL_EVIDENCE_MARKER).exists()
        ):
            raise UnsafeOutputPath("output_inside_trial_evidence_tree")
        if nearest_checkout is None and (ancestor / ".git").exists():
            nearest_checkout = ancestor
    if nearest_checkout is not None and nearest_checkout != CHECKOUT_ROOT:
        raise UnsafeOutputPath("output_inside_other_git_checkout")


def require_new_output_dir(
    path: str | Path,
    *,
    allowed_roots: Iterable[str | Path] | None = None,
    protected_roots: Iterable[str | Path] = (),
) -> Path:
    """Validate a not-yet-existing output directory without creating it."""

    target = _canonical(path)
    if not SAFE_NAME.fullmatch(target.name):
        raise UnsafeOutputPath("output_name_not_allowed")
    _check_location(target, allowed_roots, protected_roots)
    if os.path.lexists(target):
        raise UnsafeOutputPath("output_already_exists")
    return target


def require_existing_store_dir(
    path: str | Path,
    *,
    allowed_roots: Iterable[str | Path] | None = None,
    protected_roots: Iterable[str | Path] = (),
) -> Path:
    """Apply the same location rules to an existing store opened for resume."""

    target = _canonical(path)
    _check_location(target, allowed_roots, protected_roots)
    if target.is_symlink() or not target.is_dir():
        raise UnsafeOutputPath("store_must_be_a_real_directory")
    return target


def create_exclusive_dir(
    path: str | Path,
    *,
    allowed_roots: Iterable[str | Path] | None = None,
    protected_roots: Iterable[str | Path] = (),
) -> Path:
    """Create the directory exclusively and re-verify it right after creation."""

    target = require_new_output_dir(
        path, allowed_roots=allowed_roots, protected_roots=protected_roots
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    os.mkdir(target)
    try:
        if target.is_symlink() or _canonical(target) != target:
            raise UnsafeOutputPath("output_path_changed_after_check")
        _check_location(target, allowed_roots, protected_roots)
    except BaseException:
        if target.is_dir() and not target.is_symlink() and not any(target.iterdir()):
            target.rmdir()
        raise
    return target


def write_new_file(path: Path, data: bytes) -> None:
    """Create a file that must not exist, refusing symlinks at the final component."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
