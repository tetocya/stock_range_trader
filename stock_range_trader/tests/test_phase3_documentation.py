"""Offline drift checks for the completed Phase 3 public documentation."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from phase3_mock_e2e_helpers import (
    confirmed_arguments,
    install_deterministic_offline_runtime,
    single_bundle,
    write_mock_phase3_inputs,
)

from examples.run_walk_forward_signal import main as signal_main
from reports import COMMON_FILENAMES, EXECUTABLE_FILENAMES, SIGNAL_FILENAMES
from walkforward import REPORT_SCHEMA_VERSION, AnalysisMode

PROJECT_ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parent
ROOT_README = REPOSITORY_ROOT / "README.md"
PROJECT_README = PROJECT_ROOT / "README.md"
MANIFEST_SPEC = PROJECT_ROOT / "docs" / "phase3_manifest_spec.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_both_readmes_link_to_the_existing_manifest_spec() -> None:
    root = _read(ROOT_README)
    project = _read(PROJECT_README)

    assert "(stock_range_trader/docs/phase3_manifest_spec.md)" in root
    assert "(docs/phase3_manifest_spec.md)" in project
    assert MANIFEST_SPEC.is_file()


def test_project_readme_has_no_stale_step8_completion_claim() -> None:
    text = _read(PROJECT_README)

    assert "STEP 8まで" not in text
    assert "Phase 3（STEP 12完了）" in text


def test_readme_separates_modes_and_forbids_yfinance_execution() -> None:
    text = _read(PROJECT_README)

    assert "Signal ValidationとExecutable Validation" in text
    assert "yfinanceはSignal Validation専用" in text
    assert (
        "Executable Validation、Theoretical Buy & Hold、Executable Buy & Holdは常に"
    ) in text
    assert ("約定可能な利益、Backtest Return、Portfolio Returnではありません") in text


def test_readme_records_current_jquants_free_and_live_status() -> None:
    text = _read(PROJECT_README)

    assert "2年履歴・12週間遅延" in text
    assert "5 requests/min" in text
    assert "J-Quants Live Executable経路は**未検証**" in text


def test_readme_records_fold_purge_selection_and_one_time_test_contract() -> None:
    text = _read(PROJECT_README)

    for contract in (
        "半開区間`[start, end)`",
        "Candidate selectionはValidation結果だけ",
        "`embargo_sessions >= forward_sessions`",
        "`label_end_date < test_start`",
        "別Candidateへfallbackせず",
        "そのCandidateだけを一回評価",
    ):
        assert contract in text


def test_readme_distinguishes_temporal_and_universe_oos() -> None:
    text = _read(PROJECT_README)

    assert "`temporal_oos`" in text
    assert "`point_in_time_universe`" in text
    assert "別の判定" in text
    assert "`survivorship_bias_status=present`" in text


def test_readme_limits_formal_oos_to_machine_verifiable_claims() -> None:
    text = _read(PROJECT_README)

    assert "`formal_oos_eligible=true`" in text
    assert "コードが検証できる" in text
    assert "人が過去にTest結果を見ていないこと" in text
    assert "将来利益" in text


def test_signal_and_executable_cli_examples_have_both_confirmation_paths() -> None:
    blocks = re.findall(r"```bash\n(.*?)```", _read(PROJECT_README), flags=re.DOTALL)

    for script in ("run_walk_forward_signal.py", "run_walk_forward_executable.py"):
        selected = [block for block in blocks if script in block]
        assert len(selected) == 2
        assert all("--start " in block and "--end " in block for block in selected)
        assert any("--preflight-only" in block for block in selected)
        assert any("--confirm-test-evaluation" in block for block in selected)


def test_executable_cli_examples_never_use_yfinance_input() -> None:
    blocks = re.findall(r"```bash\n(.*?)```", _read(PROJECT_README), flags=re.DOTALL)
    executable = [
        block for block in blocks if "run_walk_forward_executable.py" in block
    ]

    assert executable
    assert all("yfinance" not in block.lower() for block in executable)
    assert all("jquants" in block.lower() for block in executable)


def test_all_production_bundle_filenames_are_documented() -> None:
    readme = _read(PROJECT_README)
    specification = _read(MANIFEST_SPEC)

    for filename in COMMON_FILENAMES | SIGNAL_FILENAMES | EXECUTABLE_FILENAMES:
        assert filename in readme
        assert filename in specification


def test_documented_bundle_counts_match_production_constants() -> None:
    readme = _read(PROJECT_README)
    specification = _read(MANIFEST_SPEC)

    assert len(COMMON_FILENAMES) + len(SIGNAL_FILENAMES) + 1 == 12
    assert len(COMMON_FILENAMES) + len(EXECUTABLE_FILENAMES) + 1 == 14
    for text in (readme, specification):
        assert "12ファイル" in text
        assert "14ファイル" in text


def test_manifest_spec_records_the_production_schema_version() -> None:
    assert REPORT_SCHEMA_VERSION == "phase3-report-1.0"
    assert REPORT_SCHEMA_VERSION in _read(MANIFEST_SPEC)


def test_generated_manifest_sections_and_test_policy_are_documented(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = write_mock_phase3_inputs(tmp_path, AnalysisMode.SIGNAL_VALIDATION)
    network_calls = install_deterministic_offline_runtime(monkeypatch)
    output_dir = tmp_path / "documentation_contract"

    assert signal_main(confirmed_arguments(inputs, output_dir)) == 0
    bundle = single_bundle(output_dir)
    manifest = json.loads(
        (bundle / "walk_forward_manifest.json").read_text(encoding="utf-8")
    )
    specification = _read(MANIFEST_SPEC)

    for section in manifest:
        assert f"`{section}`" in specification
    for key, value in manifest["test_policy"].items():
        assert f"`{key}`" in specification
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        assert f"`{rendered}`" in specification
    assert network_calls == []


def test_manifest_spec_forbids_sensitive_or_local_only_output() -> None:
    specification = _read(MANIFEST_SPEC)

    assert (
        "完成Manifestへlocal absolute pathや`git_root`を出力しません" in specification
    )
    assert "API Key、token、環境変数値、local absolute path" in specification
    local_home_prefix = Path("/").joinpath("Users").as_posix() + "/"
    assert local_home_prefix not in specification
    assert "Raw市場データはManifestへ保存しません" in specification


def test_all_repository_relative_markdown_links_resolve() -> None:
    for source in (ROOT_README, PROJECT_README, MANIFEST_SPEC):
        destinations = re.findall(r"\[[^]]+\]\(([^)]+)\)", _read(source))
        for destination in destinations:
            if destination.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = destination.split("#", maxsplit=1)[0]
            assert relative
            target = (source.parent / relative).resolve()
            assert target.exists(), f"{source}: missing Markdown target {destination}"
