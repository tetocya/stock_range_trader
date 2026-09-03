"""Repository-level acceptance checks for local and CI quality gates."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def test_gitignore_excludes_required_python_and_report_artifacts() -> None:
    patterns = set(
        (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    )

    assert {
        ".venv/",
        "__pycache__/",
        "*.py[cod]",
        ".pytest_cache/",
        ".ruff_cache/",
        "*.egg-info/",
        "build/",
        "dist/",
        "outputs/",
        ".DS_Store",
    }.issubset(patterns)


def test_github_actions_covers_supported_pythons_and_quality_gates() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    for version in ('"3.11"', '"3.12"', '"3.13"'):
        assert version in workflow
    for command in (
        'python -m pip install -e ".[dev]"',
        "python -m pytest -q",
        "ruff check .",
        "ruff format --check .",
    ):
        assert command in workflow
