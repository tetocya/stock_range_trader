"""Allowlist-based output-location guard shared by the feasibility tools.

Primary rule: every output must lie strictly under an explicitly allowed root.
The allowed roots are this checkout's git-ignored ``outputs/feasibility`` and
the system temporary directory; callers may only narrow them. On top of that,
paths are refused when they are relative, contain ``..``, pass through a
symlink or alias below a trusted base, sit inside another git checkout (for
example the June limited-trial worktree) or inside any directory tree that
holds trial evidence (``.delayed_replay``). Names are never the only protection.

Trusted bases and aliases: each root is recorded with its fully resolved
location and the spellings accepted for that same location. The temporary base
accepts exactly two spellings, the one ``tempfile.gettempdir()`` reports and its
resolved form, so macOS ``/var/folders/...`` and ``/private/var/folders/...``
both work. The alias is trusted only at the base itself: every component below
the base must be a real directory, and a path reaching the resolved location
through any other symlink is refused. The project output root is trusted only
when it is not itself reached through a symlink. All location checks run on
the resolved path, so a trusted alias cannot lead into a protected tree.

Residual limit: checks run before and immediately after the exclusive directory
creation, and files are created with ``O_EXCL | O_NOFOLLOW``. A concurrent
process that can rewrite an ancestor directory (or the temporary base alias)
between those steps is not fully excluded; that would need
directory-descriptor-relative I/O and is out of scope. This is a guard against
mistakes, not file-system isolation.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
CHECKOUT_ROOT = PROJECT_DIR.parent
PROJECT_OUTPUT_ROOT = PROJECT_DIR / "outputs" / "feasibility"
TRIAL_EVIDENCE_MARKER = ".delayed_replay"
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class UnsafeOutputPath(ValueError):
    """Raised instead of writing outside the allowed feasibility output roots."""


@dataclass(frozen=True)
class _Root:
    canonical: Path
    spellings: tuple[Path, ...]


def _normal(path: str | Path) -> Path:
    raw = Path(path)
    if not raw.is_absolute():
        raise UnsafeOutputPath("output_path_must_be_absolute")
    if ".." in raw.parts:
        raise UnsafeOutputPath("parent_reference_not_allowed")
    return Path(os.path.normpath(raw))


def _default_roots() -> tuple[_Root, ...]:
    roots = []
    project = Path(os.path.normpath(PROJECT_OUTPUT_ROOT))
    if project.resolve() == project:
        roots.append(_Root(project, (project,)))
    temp = Path(os.path.normpath(os.path.abspath(tempfile.gettempdir())))
    resolved = temp.resolve()
    roots.append(_Root(resolved, tuple(dict.fromkeys((temp, resolved)))))
    return tuple(roots)


def default_allowed_roots() -> tuple[Path, ...]:
    return tuple(root.canonical for root in _default_roots())


def _map_into(path: str | Path, roots: Iterable[_Root]) -> tuple[Path, _Root]:
    """Return the resolved location of ``path`` spelled under a trusted root."""

    normal = _normal(path)
    for root in roots:
        for spelling in root.spellings:
            if normal == spelling or spelling in normal.parents:
                candidate = root.canonical.joinpath(normal.relative_to(spelling))
                if candidate.resolve() != candidate:
                    raise UnsafeOutputPath("path_contains_symlink_or_alias")
                return candidate, root
    if normal.resolve() != normal:
        raise UnsafeOutputPath("path_contains_symlink_or_alias")
    raise UnsafeOutputPath("output_outside_allowed_roots")


def _allowed(roots: Iterable[str | Path] | None) -> tuple[_Root, ...]:
    defaults = _default_roots()
    if roots is None:
        return defaults
    narrowed = []
    for root in roots:
        try:
            canonical, _ = _map_into(root, defaults)
        except UnsafeOutputPath:
            raise UnsafeOutputPath("allowed_root_must_narrow_default_roots") from None
        spellings = (Path(os.path.normpath(root)), canonical)
        narrowed.append(_Root(canonical, tuple(dict.fromkeys(spellings))))
    return tuple(narrowed)


def _target(path: str | Path, allowed_roots: Iterable[str | Path] | None) -> Path:
    target, root = _map_into(path, _allowed(allowed_roots))
    if target == root.canonical:
        raise UnsafeOutputPath("output_outside_allowed_roots")
    return target


def _check_location(target: Path, protected_roots: Iterable[str | Path]) -> None:
    for protected in protected_roots:
        root = Path(protected).resolve()
        if target == root or root in target.parents:
            raise UnsafeOutputPath("output_inside_protected_root")
    nearest_checkout = None
    for ancestor in (target, *target.parents):
        if ancestor.name == TRIAL_EVIDENCE_MARKER or (
            ancestor != target and (ancestor / TRIAL_EVIDENCE_MARKER).exists()
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
    """Validate a not-yet-existing output directory; return its resolved path."""

    target = _target(path, allowed_roots)
    if not SAFE_NAME.fullmatch(target.name):
        raise UnsafeOutputPath("output_name_not_allowed")
    _check_location(target, protected_roots)
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

    target = _target(path, allowed_roots)
    _check_location(target, protected_roots)
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
        if target.is_symlink() or target.resolve() != target:
            raise UnsafeOutputPath("output_path_changed_after_check")
        _check_location(target, protected_roots)
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
