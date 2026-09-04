"""Phase 2 documentation, CLI, secret, and generated-data safety checks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GIT_ROOT = PROJECT_ROOT.parent


def test_phase2_readme_documents_required_policies() -> None:
    text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for required in (
        "J-Quants API V2 Free",
        "12週間遅延",
        "yfinanceの5年価格",
        "end`は排他的",
        "Providerを連結",
        "JQUANTS_API_KEY",
        "国内普通株Universe",
        "Adj Close / Close",
        "Provider間差異",
        "Survivorship bias",
        "個人の研究・バックテスト用に限定",
        "Raw DataをGitHubへ掲載してはいけません",
        "利益や将来の成績を保証しません",
    ):
        assert required in text


def test_example_environment_file_and_gitignore_are_safe() -> None:
    assert (GIT_ROOT / ".env.example").read_text(encoding="utf-8") == (
        "JQUANTS_API_KEY=\n"
    )
    patterns = set((GIT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
    assert {".env", "*.parquet", ".data_cache/", "data/raw/"}.issubset(patterns)


def test_phase2_clis_expose_help_without_api_key_or_network(monkeypatch) -> None:
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    scripts = (
        "download_universe.py",
        "download_prices.py",
        "run_screening.py",
        "run_batch_backtest.py",
        "compare_providers.py",
    )
    for script in scripts:
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "examples" / script), "--help"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "--api-key" not in completed.stdout


def test_no_generated_market_data_or_environment_file_is_tracked() -> None:
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=GIT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = completed.stdout.splitlines()

    assert ".env" not in tracked
    assert not any(path.endswith(".parquet") for path in tracked)
    assert not any("/.data_cache/" in f"/{path}" for path in tracked)
    assert not any("/data/raw/" in f"/{path}" for path in tracked)


def test_normal_pytest_live_tests_require_explicit_opt_in() -> None:
    source = (PROJECT_ROOT / "tests" / "test_phase2_live.py").read_text(
        encoding="utf-8"
    )
    assert 'os.environ.get("RUN_LIVE_JQUANTS_TESTS") != "1"' in source
    assert 'os.environ.get("RUN_LIVE_YFINANCE_TESTS") != "1"' in source
    assert 'not os.environ.get("JQUANTS_API_KEY")' in source
