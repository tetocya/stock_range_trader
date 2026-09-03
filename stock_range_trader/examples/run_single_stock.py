"""Command-line entry point for a single-stock Phase 1 backtest."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from config import load_strategy_config
from data import load_ohlcv_csv
from metrics import calculate_backtest_metrics
from reports import format_console_report, generate_all_plots


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a range/mean-reversion backtest for one stock CSV."
    )
    parser.add_argument("--data", required=True, type=Path, help="OHLCV CSV path")
    parser.add_argument("--symbol", required=True, help="Stock symbol, e.g. 7203")
    parser.add_argument("--config", required=True, type=Path, help="Strategy YAML path")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs"),
        help="Output directory (default: outputs)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_strategy_config(args.config)
    market_data = load_ohlcv_csv(args.data)
    detected = config.create_detector().transform(market_data)
    scored = config.create_scorer().transform(detected)
    result = config.create_engine().run(args.symbol, scored)
    metrics = calculate_backtest_metrics(
        result,
        annual_trading_days=config.annual_trading_days,
        risk_free_rate=config.risk_free_rate,
    )

    output_dir: Path = args.output_dir
    result.save_trade_log(output_dir / "trade_log.csv")
    result.save_order_log(output_dir / "order_log.csv")
    result.save_equity_curve(output_dir / "equity_curve.csv")
    plot_paths = generate_all_plots(result, output_dir)

    print(
        format_console_report(
            metrics,
            symbol=result.symbol,
            start_date=market_data["date"].iloc[0],
            end_date=market_data["date"].iloc[-1],
            order_log=result.order_log,
        )
    )
    print("\nOutput files:")
    print(f"  {output_dir / 'trade_log.csv'}")
    print(f"  {output_dir / 'order_log.csv'}")
    print(f"  {output_dir / 'equity_curve.csv'}")
    for path in plot_paths.values():
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
