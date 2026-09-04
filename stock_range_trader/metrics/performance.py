"""Strategy and buy-and-hold performance calculations."""

from __future__ import annotations

import math
import warnings
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype, is_numeric_dtype

from data.price_policy import has_dual_price_lanes, validate_backtest_price_contract

if TYPE_CHECKING:
    from backtest.engine import BacktestResult


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """All Phase 1 performance metrics, expressed as decimal returns."""

    initial_capital: float
    final_equity: float
    total_return: float
    cagr: float
    number_of_trades: int
    win_rate: float
    average_profit_per_trade: float
    average_winning_trade: float
    average_losing_trade: float
    profit_factor: float
    maximum_drawdown: float
    sharpe_ratio: float
    sortino_ratio: float
    average_holding_period: float
    exposure: float
    theoretical_buy_and_hold_return: float
    executable_buy_and_hold_return: float
    strategy_vs_executable_buy_and_hold: float

    def to_dict(self) -> dict[str, float | int]:
        """Return a plain mapping suitable for reports or serialization."""

        return asdict(self)

    @property
    def buy_and_hold_return(self) -> float:
        """Deprecated alias for the theoretical close-to-close return."""

        warnings.warn(
            "buy_and_hold_return is deprecated; use theoretical_buy_and_hold_return",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.theoretical_buy_and_hold_return

    @property
    def strategy_vs_buy_and_hold(self) -> float:
        """Deprecated alias for comparison with the theoretical benchmark."""

        warnings.warn(
            "strategy_vs_buy_and_hold is deprecated; use "
            "strategy_vs_executable_buy_and_hold",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.total_return - self.theoretical_buy_and_hold_return


def calculate_backtest_metrics(
    result: BacktestResult,
    annual_trading_days: int = 252,
    risk_free_rate: float = 0.0,
) -> PerformanceMetrics:
    """Calculate metrics directly from a BacktestResult."""

    return calculate_performance_metrics(
        equity_curve=result.equity_curve,
        trade_log=result.trade_log,
        market_data=result.prepared_data,
        initial_capital=result.initial_capital,
        annual_trading_days=annual_trading_days,
        risk_free_rate=risk_free_rate,
        lot_size=result.benchmark_lot_size,
        slippage_pct=result.benchmark_buy_price_multiplier - 1.0,
        commission_rate=result.benchmark_commission_rate,
    )


def calculate_performance_metrics(
    equity_curve: pd.DataFrame,
    trade_log: pd.DataFrame,
    market_data: pd.DataFrame,
    initial_capital: float,
    annual_trading_days: int = 252,
    risk_free_rate: float = 0.0,
    lot_size: int = 100,
    slippage_pct: float = 0.0,
    commission_rate: float = 0.0,
) -> PerformanceMetrics:
    """Calculate strategy, trade, risk, exposure, and benchmark metrics.

    CAGR uses the number of return intervals divided by
    ``annual_trading_days``.  Sharpe uses sample standard deviation of daily
    excess returns.  Sortino uses the root mean square of negative daily excess
    returns as downside deviation.
    """

    _validate_parameters(
        initial_capital,
        annual_trading_days,
        risk_free_rate,
        lot_size,
        slippage_pct,
        commission_rate,
    )
    _validate_equity_curve(equity_curve)
    _validate_market_data(market_data, equity_curve)
    _validate_trade_log(trade_log)

    equity = equity_curve["total_equity"].astype(float)
    final_equity = float(equity.iloc[-1])
    total_return = final_equity / initial_capital - 1.0
    intervals = len(equity) - 1
    if intervals > 0:
        years = intervals / annual_trading_days
        cagr = (final_equity / initial_capital) ** (1.0 / years) - 1.0
    else:
        cagr = np.nan

    daily_returns = equity.pct_change(fill_method=None).iloc[1:]
    daily_risk_free = (1.0 + risk_free_rate) ** (1.0 / annual_trading_days) - 1.0
    excess_returns = daily_returns - daily_risk_free
    sharpe_ratio = _annualized_sharpe(excess_returns, annual_trading_days)
    sortino_ratio = _annualized_sortino(excess_returns, annual_trading_days)

    running_peak = equity.cummax()
    drawdown = equity / running_peak - 1.0
    maximum_drawdown = float(drawdown.min())
    exposure = float((equity_curve["position_value"] > 0.0).mean())

    net_profits = trade_log["net_profit"].astype(float)
    holding_days = trade_log["holding_days"].astype(float)
    number_of_trades = len(net_profits)
    winning = net_profits[net_profits > 0.0]
    losing = net_profits[net_profits < 0.0]
    win_rate = float(len(winning) / number_of_trades) if number_of_trades else 0.0
    average_profit = float(net_profits.mean()) if number_of_trades else np.nan
    average_winner = float(winning.mean()) if not winning.empty else np.nan
    average_loser = float(losing.mean()) if not losing.empty else np.nan
    average_holding = float(holding_days.mean()) if number_of_trades else np.nan
    profit_factor = _profit_factor(winning, losing)

    _, benchmark_close, _ = _benchmark_price_inputs(market_data)
    theoretical_buy_and_hold_return = float(
        benchmark_close.iloc[-1] / benchmark_close.iloc[0] - 1.0
    )
    executable_equity = executable_buy_and_hold_equity(
        market_data=market_data,
        initial_capital=initial_capital,
        lot_size=lot_size,
        slippage_pct=slippage_pct,
        commission_rate=commission_rate,
    )
    executable_buy_and_hold_return = float(
        executable_equity.iloc[-1] / initial_capital - 1.0
    )

    return PerformanceMetrics(
        initial_capital=float(initial_capital),
        final_equity=final_equity,
        total_return=total_return,
        cagr=float(cagr),
        number_of_trades=number_of_trades,
        win_rate=win_rate,
        average_profit_per_trade=average_profit,
        average_winning_trade=average_winner,
        average_losing_trade=average_loser,
        profit_factor=profit_factor,
        maximum_drawdown=maximum_drawdown,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        average_holding_period=average_holding,
        exposure=exposure,
        theoretical_buy_and_hold_return=theoretical_buy_and_hold_return,
        executable_buy_and_hold_return=executable_buy_and_hold_return,
        strategy_vs_executable_buy_and_hold=(
            total_return - executable_buy_and_hold_return
        ),
    )


def executable_buy_and_hold_equity(
    market_data: pd.DataFrame,
    initial_capital: float,
    lot_size: int = 100,
    slippage_pct: float = 0.0,
    commission_rate: float = 0.0,
) -> pd.Series:
    """Return executable buy-and-hold equity marked at each daily close.

    The benchmark buys whole lots at the first open with adverse BUY
    slippage and commission, retains residual cash, and never force-sells.
    A non-tradable first bar or unaffordable lot remains 100% cash.
    """

    _validate_parameters(
        initial_capital,
        annual_trading_days=1,
        risk_free_rate=0.0,
        lot_size=lot_size,
        slippage_pct=slippage_pct,
        commission_rate=commission_rate,
    )
    _validate_benchmark_market_data(market_data)

    benchmark_open, benchmark_close, benchmark_volume = _benchmark_price_inputs(
        market_data
    )
    first_open = float(benchmark_open.iloc[0])
    first_volume = float(benchmark_volume.iloc[0])
    execution_price = first_open * (1.0 + slippage_pct)
    cash_per_share = execution_price * (1.0 + commission_rate)
    shares = 0
    if first_volume > 0.0:
        shares = math.floor(initial_capital / cash_per_share / lot_size) * lot_size
    commission = execution_price * shares * commission_rate
    residual_cash = initial_capital - execution_price * shares - commission
    equity = residual_cash + shares * benchmark_close
    return pd.Series(
        equity.to_numpy(), index=market_data.index, name="executable_buy_and_hold"
    )


def _annualized_sharpe(returns: pd.Series, annual_trading_days: int) -> float:
    if len(returns) < 2:
        return np.nan
    standard_deviation = float(returns.std(ddof=1))
    if np.isclose(standard_deviation, 0.0):
        return np.nan
    return float(returns.mean() / standard_deviation * np.sqrt(annual_trading_days))


def _annualized_sortino(returns: pd.Series, annual_trading_days: int) -> float:
    if returns.empty:
        return np.nan
    downside = np.minimum(returns.to_numpy(dtype=float), 0.0)
    downside_deviation = float(np.sqrt(np.mean(np.square(downside))))
    mean_return = float(returns.mean())
    if np.isclose(downside_deviation, 0.0):
        return np.inf if mean_return > 0.0 else np.nan
    return float(mean_return / downside_deviation * np.sqrt(annual_trading_days))


def _profit_factor(winning: pd.Series, losing: pd.Series) -> float:
    gross_profit = float(winning.sum())
    gross_loss = abs(float(losing.sum()))
    if np.isclose(gross_loss, 0.0):
        return np.inf if gross_profit > 0.0 else np.nan
    return gross_profit / gross_loss


def _validate_parameters(
    initial_capital: float,
    annual_trading_days: int,
    risk_free_rate: float,
    lot_size: int,
    slippage_pct: float,
    commission_rate: float,
) -> None:
    if (
        isinstance(initial_capital, bool)
        or not np.isfinite(initial_capital)
        or initial_capital <= 0.0
    ):
        raise ValueError("initial_capital must be finite and greater than zero")
    if (
        isinstance(annual_trading_days, bool)
        or not isinstance(annual_trading_days, int)
        or annual_trading_days <= 0
    ):
        raise ValueError("annual_trading_days must be a positive integer")
    if (
        isinstance(risk_free_rate, bool)
        or not np.isfinite(risk_free_rate)
        or risk_free_rate <= -1.0
    ):
        raise ValueError("risk_free_rate must be finite and greater than -1")
    if isinstance(lot_size, bool) or not isinstance(lot_size, int) or lot_size <= 0:
        raise ValueError("lot_size must be a positive integer")
    if (
        isinstance(slippage_pct, bool)
        or not np.isfinite(slippage_pct)
        or not 0.0 <= slippage_pct < 1.0
    ):
        raise ValueError("slippage_pct must be finite and in [0, 1)")
    if (
        isinstance(commission_rate, bool)
        or not np.isfinite(commission_rate)
        or not 0.0 <= commission_rate < 1.0
    ):
        raise ValueError("commission_rate must be finite and in [0, 1)")


def _validate_equity_curve(frame: pd.DataFrame) -> None:
    _require_dataframe_columns(
        frame, {"date", "position_value", "total_equity"}, "equity curve"
    )
    if frame.empty:
        raise ValueError("equity curve must contain at least one row")
    _validate_dates(frame["date"], "equity curve")
    _validate_finite_numeric(frame["total_equity"], "total_equity", positive=True)
    _validate_finite_numeric(
        frame["position_value"], "position_value", non_negative=True
    )


def _validate_market_data(frame: pd.DataFrame, equity_curve: pd.DataFrame) -> None:
    _validate_benchmark_market_data(frame)
    if (
        not frame["date"]
        .reset_index(drop=True)
        .equals(equity_curve["date"].reset_index(drop=True))
    ):
        raise ValueError("market data and equity curve dates must match exactly")


def _validate_benchmark_market_data(frame: pd.DataFrame) -> None:
    _require_dataframe_columns(
        frame, {"date", "open", "close", "volume"}, "market data"
    )
    if frame.empty:
        raise ValueError("market data must contain at least one row")
    _validate_dates(frame["date"], "market data")
    _validate_finite_numeric(frame["open"], "open", positive=True)
    _validate_finite_numeric(frame["close"], "close", positive=True)
    _validate_finite_numeric(frame["volume"], "volume", non_negative=True)
    validate_backtest_price_contract(frame)


def _benchmark_price_inputs(
    frame: pd.DataFrame,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Select the provider-reported execution lane when available."""

    if has_dual_price_lanes(frame):
        return (
            frame["execution_open"].astype(float),
            frame["execution_close"].astype(float),
            frame["execution_volume"].astype(float),
        )
    return (
        frame["open"].astype(float),
        frame["close"].astype(float),
        frame["volume"].astype(float),
    )


def _validate_trade_log(frame: pd.DataFrame) -> None:
    _require_dataframe_columns(frame, {"net_profit", "holding_days"}, "trade log")
    if frame.empty:
        return
    _validate_finite_numeric(frame["net_profit"], "net_profit")
    _validate_finite_numeric(frame["holding_days"], "holding_days", positive=True)


def _require_dataframe_columns(
    frame: pd.DataFrame, required: set[str], name: str
) -> None:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{name} must be a pandas DataFrame")
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"Missing {name} columns: " + ", ".join(missing))


def _validate_dates(values: pd.Series, name: str) -> None:
    if not is_datetime64_any_dtype(values.dtype):
        raise ValueError(f"{name} date must have a pandas datetime dtype")
    if values.isna().any() or values.duplicated().any():
        raise ValueError(f"{name} dates must be present and unique")
    if not values.is_monotonic_increasing:
        raise ValueError(f"{name} dates must be in ascending order")


def _validate_finite_numeric(
    values: pd.Series,
    name: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> None:
    if not is_numeric_dtype(values.dtype):
        raise ValueError(f"{name} must be numeric")
    numeric = values.to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise ValueError(f"{name} must contain only finite values")
    if positive and (numeric <= 0.0).any():
        raise ValueError(f"{name} must be greater than zero")
    if non_negative and (numeric < 0.0).any():
        raise ValueError(f"{name} must be non-negative")
