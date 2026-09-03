"""End-to-end tests using the bundled synthetic OHLCV sample."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pandas as pd

from config import load_strategy_config
from data import load_ohlcv_csv
from metrics import calculate_backtest_metrics
from reports import format_console_report, generate_all_plots

PROJECT_ROOT = Path(__file__).parents[1]
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample.csv"
CONFIG_PATH = PROJECT_ROOT / "config" / "strategy.yaml"


def test_sample_csv_runs_through_every_phase_one_component(tmp_path: Path) -> None:
    config = load_strategy_config(CONFIG_PATH)
    market_data = load_ohlcv_csv(SAMPLE_PATH)
    detected = config.create_detector().transform(market_data)
    scored = config.create_scorer().transform(detected)
    result = config.create_engine().run("7203", scored)
    metrics = calculate_backtest_metrics(
        result, config.annual_trading_days, config.risk_free_rate
    )

    result.save_trade_log(tmp_path / "trade_log.csv")
    result.save_equity_curve(tmp_path / "equity_curve.csv")
    plots = generate_all_plots(result, tmp_path)
    report = format_console_report(
        metrics,
        result.symbol,
        market_data["date"].iloc[0],
        market_data["date"].iloc[-1],
    )

    assert len(market_data) == 320
    assert not result.trade_log.empty
    assert len(result.equity_curve) == len(market_data)
    assert metrics.number_of_trades == len(result.trade_log)
    assert "BACKTEST RESULT" in report
    assert "Symbol: 7203" in report
    assert (tmp_path / "trade_log.csv").stat().st_size > 0
    assert (tmp_path / "equity_curve.csv").stat().st_size > 0
    assert set(plots) == {"price", "equity", "drawdown"}
    for path in plots.values():
        assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    positions = {date: index for index, date in enumerate(market_data["date"])}
    assert all(
        positions[fill.execution_date] == positions[fill.signal_date] + 1
        for fill in result.fills
    )


def test_documented_cli_command_creates_all_outputs(tmp_path: Path) -> None:
    output_dir = tmp_path / "cli-output"
    command = [
        sys.executable,
        str(PROJECT_ROOT / "examples" / "run_single_stock.py"),
        "--data",
        str(SAMPLE_PATH),
        "--symbol",
        "7203",
        "--config",
        str(CONFIG_PATH),
        "--output-dir",
        str(output_dir),
    ]

    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "BACKTEST RESULT" in completed.stdout
    for filename in (
        "trade_log.csv",
        "equity_curve.csv",
        "price_chart.png",
        "equity_curve.png",
        "drawdown.png",
    ):
        assert (output_dir / filename).stat().st_size > 0

    trades = pd.read_csv(output_dir / "trade_log.csv")
    equity = pd.read_csv(output_dir / "equity_curve.csv")
    assert not trades.empty
    assert len(equity) == 320

