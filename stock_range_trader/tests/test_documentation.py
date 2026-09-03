"""Acceptance tests for required Phase 1 documentation."""

from __future__ import annotations

from pathlib import Path

README_PATH = Path(__file__).parents[1] / "README.md"


def _readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


def test_readme_contains_all_required_sections() -> None:
    required_headings = (
        "## プロジェクトの目的",
        "## Installation",
        "## 入力データ形式",
        "## 実行方法とEnd-to-endサンプル",
        "## 戦略概要",
        "## Range Scoreの計算規約",
        "## 売買シグナル",
        "## Look-ahead bias対策",
        "## Performance Metrics",
        "## 現在の制限事項",
        "## Roadmap",
    )
    text = _readme()

    assert all(heading in text for heading in required_headings)
    assert "構造を作成した段階" not in text


def test_readme_documents_command_and_all_outputs() -> None:
    text = _readme()

    assert "python examples/run_single_stock.py" in text
    for filename in (
        "trade_log.csv",
        "order_log.csv",
        "equity_curve.csv",
        "price_chart.png",
        "equity_curve.png",
        "drawdown.png",
    ):
        assert filename in text


def test_readme_documents_phase_one_point_one_execution_and_metrics() -> None:
    text = _readme()

    for required_text in (
        "non_tradable_bar",
        "no_next_bar",
        "Theoretical Buy & Hold",
        "Executable Buy & Hold",
        "median_trading_value_target",
        "period=14`ではindex 27",
        "TA-Libは実行時・テスト時依存ではありません",
        "ruff check .",
        "ruff format --check .",
    ):
        assert required_text in text


def test_readme_discloses_biases_corporate_actions_and_no_live_orders() -> None:
    text = _readme()

    for limitation in (
        "Survivorship bias",
        "上場廃止",
        "株式分割",
        "配当",
        "銘柄コード変更",
        "売買停止",
        "実注文機能は存在しません",
    ):
        assert limitation in text
