"""Headless PNG generation for price, equity, and drawdown charts."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

# Headless/restricted environments may not permit writes below the user's home.
# Respect caller-provided cache locations; otherwise use an application-specific
# temporary cache so plotting never depends on home-directory write access.
_CACHE_ROOT = Path(tempfile.gettempdir()) / "stock_range_trader_plot_cache"
_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_CACHE_ROOT / "matplotlib"))
os.environ.setdefault("XDG_CACHE_HOME", str(_CACHE_ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from backtest import Fill, OrderSide
from metrics import executable_buy_and_hold_equity

if TYPE_CHECKING:
    from backtest import BacktestResult

PathLike: TypeAlias = str | Path


def plot_price_chart(
    data: pd.DataFrame, fills: tuple[Fill, ...], path: PathLike
) -> Path:
    """Save close, SMA, ATR thresholds, and BUY/SELL fills as a PNG."""

    _require_columns(
        data,
        {"date", "close", "sma", "buy_threshold", "sell_threshold"},
        "price chart",
    )
    destination = _prepare_path(path)
    figure, axis = plt.subplots(figsize=(12, 6))
    axis.plot(data["date"], data["close"], label="Close", color="black", linewidth=1.2)
    axis.plot(data["date"], data["sma"], label="SMA", color="royalblue")
    axis.plot(
        data["date"],
        data["buy_threshold"],
        label="Buy threshold",
        color="seagreen",
        linestyle="--",
    )
    axis.plot(
        data["date"],
        data["sell_threshold"],
        label="Sell threshold",
        color="firebrick",
        linestyle="--",
    )
    buys = [fill for fill in fills if fill.side is OrderSide.BUY]
    sells = [fill for fill in fills if fill.side is OrderSide.SELL]
    if buys:
        axis.scatter(
            [fill.execution_date for fill in buys],
            [fill.execution_price for fill in buys],
            label="BUY",
            marker="^",
            color="green",
            s=45,
            zorder=3,
        )
    if sells:
        axis.scatter(
            [fill.execution_date for fill in sells],
            [fill.execution_price for fill in sells],
            label="SELL",
            marker="v",
            color="red",
            s=45,
            zorder=3,
        )
    axis.set_title("Price and Mean-Reversion Signals")
    axis.set_xlabel("Date")
    axis.set_ylabel("Price")
    axis.grid(alpha=0.25)
    axis.legend(loc="best")
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(destination, dpi=150)
    plt.close(figure)
    return destination


def plot_equity_curve(
    equity_curve: pd.DataFrame,
    market_data: pd.DataFrame,
    initial_capital: float,
    path: PathLike,
    lot_size: int = 100,
    slippage_pct: float = 0.0,
    commission_rate: float = 0.0,
) -> Path:
    """Save strategy equity against executable buy-and-hold."""

    _require_columns(equity_curve, {"date", "total_equity"}, "equity chart")
    _require_columns(
        market_data, {"date", "open", "close", "volume"}, "buy-and-hold chart"
    )
    destination = _prepare_path(path)
    benchmark = executable_buy_and_hold_equity(
        market_data,
        initial_capital,
        lot_size=lot_size,
        slippage_pct=slippage_pct,
        commission_rate=commission_rate,
    )
    figure, axis = plt.subplots(figsize=(12, 5))
    axis.plot(
        equity_curve["date"],
        equity_curve["total_equity"],
        label="Strategy",
        color="royalblue",
    )
    axis.plot(
        market_data["date"],
        benchmark,
        label="Executable Buy & Hold",
        color="darkorange",
        alpha=0.85,
    )
    axis.set_title("Equity Curve")
    axis.set_xlabel("Date")
    axis.set_ylabel("Equity")
    axis.grid(alpha=0.25)
    axis.legend(loc="best")
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(destination, dpi=150)
    plt.close(figure)
    return destination


def plot_drawdown(equity_curve: pd.DataFrame, path: PathLike) -> Path:
    """Save the daily strategy drawdown as a percentage chart."""

    _require_columns(equity_curve, {"date", "drawdown"}, "drawdown chart")
    destination = _prepare_path(path)
    drawdown_pct = equity_curve["drawdown"].astype(float) * 100.0
    figure, axis = plt.subplots(figsize=(12, 4))
    axis.plot(equity_curve["date"], drawdown_pct, color="firebrick")
    axis.fill_between(
        equity_curve["date"], drawdown_pct, 0.0, color="firebrick", alpha=0.25
    )
    axis.set_title("Drawdown")
    axis.set_xlabel("Date")
    axis.set_ylabel("Drawdown (%)")
    axis.grid(alpha=0.25)
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(destination, dpi=150)
    plt.close(figure)
    return destination


def generate_all_plots(result: BacktestResult, output_dir: PathLike) -> dict[str, Path]:
    """Generate every Phase 1 PNG and return its path by chart name."""

    directory = Path(output_dir)
    return {
        "price": plot_price_chart(
            result.prepared_data, result.fills, directory / "price_chart.png"
        ),
        "equity": plot_equity_curve(
            result.equity_curve,
            result.prepared_data,
            result.initial_capital,
            directory / "equity_curve.png",
            lot_size=result.benchmark_lot_size,
            slippage_pct=result.benchmark_buy_price_multiplier - 1.0,
            commission_rate=result.benchmark_commission_rate,
        ),
        "drawdown": plot_drawdown(result.equity_curve, directory / "drawdown.png"),
    }


def _prepare_path(path: PathLike) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _require_columns(frame: pd.DataFrame, required: set[str], name: str) -> None:
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"Missing {name} columns: " + ", ".join(missing))
