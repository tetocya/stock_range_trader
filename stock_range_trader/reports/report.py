"""Human-readable console report generation."""

from __future__ import annotations

import math

import pandas as pd

from metrics import PerformanceMetrics


def format_console_report(
    metrics: PerformanceMetrics,
    symbol: str,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> str:
    """Return a stable text summary for one backtest."""

    lines = [
        "=" * 40,
        "BACKTEST RESULT",
        "=" * 40,
        f"Symbol: {symbol}",
        f"Period: {start_date:%Y-%m-%d} - {end_date:%Y-%m-%d}",
        "",
        f"Initial Capital: {_yen(metrics.initial_capital)}",
        f"Final Equity: {_yen(metrics.final_equity)}",
        f"Total Return: {_percentage(metrics.total_return)}",
        f"Buy & Hold: {_percentage(metrics.buy_and_hold_return)}",
        f"Strategy vs Buy & Hold: {_percentage(metrics.strategy_vs_buy_and_hold)}",
        f"CAGR: {_percentage(metrics.cagr)}",
        f"Maximum Drawdown: {_percentage(metrics.maximum_drawdown)}",
        f"Sharpe Ratio: {_number(metrics.sharpe_ratio)}",
        f"Sortino Ratio: {_number(metrics.sortino_ratio)}",
        f"Number of Trades: {metrics.number_of_trades}",
        f"Win Rate: {_percentage(metrics.win_rate)}",
        f"Profit Factor: {_number(metrics.profit_factor)}",
        f"Average Profit: {_yen(metrics.average_profit_per_trade)}",
        f"Average Holding: {_days(metrics.average_holding_period)}",
        f"Exposure: {_percentage(metrics.exposure)}",
        "=" * 40,
    ]
    return "\n".join(lines)


def _yen(value: float) -> str:
    if not math.isfinite(value):
        return "N/A"
    return f"¥{value:,.0f}"


def _percentage(value: float) -> str:
    if not math.isfinite(value):
        return "N/A"
    return f"{value:.1%}"


def _number(value: float) -> str:
    if math.isnan(value):
        return "N/A"
    if math.isinf(value):
        return "∞" if value > 0 else "-∞"
    return f"{value:.2f}"


def _days(value: float) -> str:
    if not math.isfinite(value):
        return "N/A"
    return f"{value:.1f} days"
