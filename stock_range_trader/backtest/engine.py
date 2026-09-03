"""Causal daily-bar backtest orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

import pandas as pd

from data.validation import validate_ohlcv
from risk import RiskManager
from strategy import PositionContext, Signal, SignalAction, Strategy

from .execution import ExecutionModel
from .portfolio import Portfolio
from .trade import Fill, Order, OrderSide

PathLike: TypeAlias = str | Path

TRADE_LOG_COLUMNS: tuple[str, ...] = (
    "symbol",
    "entry_signal_date",
    "entry_date",
    "entry_price",
    "shares",
    "exit_signal_date",
    "exit_date",
    "exit_price",
    "exit_reason",
    "gross_profit",
    "commission",
    "slippage_cost",
    "net_profit",
    "return_pct",
    "holding_days",
)
EQUITY_CURVE_COLUMNS: tuple[str, ...] = (
    "date",
    "cash",
    "position_value",
    "total_equity",
    "drawdown",
)
SIGNAL_LOG_COLUMNS: tuple[str, ...] = (
    "signal_date",
    "action",
    "exit_reason",
)


@dataclass(slots=True)
class BacktestResult:
    """Data products and final state from one single-stock backtest."""

    symbol: str
    initial_capital: float
    prepared_data: pd.DataFrame
    trade_log: pd.DataFrame
    equity_curve: pd.DataFrame
    signal_log: pd.DataFrame
    fills: tuple[Fill, ...]
    portfolio: Portfolio
    unexecuted_signal: Signal | None

    @property
    def final_equity(self) -> float:
        """Return the last marked-to-market account value."""

        return float(self.equity_curve.iloc[-1]["total_equity"])

    def save_trade_log(self, path: PathLike) -> None:
        """Write completed trades to CSV without the DataFrame index."""

        _write_csv(self.trade_log, path)

    def save_equity_curve(self, path: PathLike) -> None:
        """Write the daily equity curve to CSV without the DataFrame index."""

        _write_csv(self.equity_curve, path)


@dataclass(slots=True)
class BacktestEngine:
    """Run close-time signals and next-open fills in strict chronological order."""

    strategy: Strategy
    execution_model: ExecutionModel
    risk_manager: RiskManager
    initial_capital: float = 1_000_000.0

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, Strategy):
            raise TypeError("strategy must implement Strategy")
        if not isinstance(self.execution_model, ExecutionModel):
            raise TypeError("execution_model must implement ExecutionModel")
        if not isinstance(self.risk_manager, RiskManager):
            raise TypeError("risk_manager must be a RiskManager")

    def run(self, symbol: str, frame: pd.DataFrame) -> BacktestResult:
        """Run a single-symbol daily backtest.

        For each row, an earlier pending signal is processed at the current
        open before the current close can be observed.  Only after marking the
        portfolio at that close does the strategy generate the next pending
        signal.  This sequence makes same-day-open execution impossible.
        """

        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must not be empty")
        validate_ohlcv(frame)
        prepared = self.strategy.prepare(frame)
        portfolio = Portfolio(self.initial_capital)
        self.risk_manager.reset()

        pending_signal: Signal | None = None
        fills: list[Fill] = []
        equity_records: list[dict[str, object]] = []
        signal_records: list[dict[str, object]] = []

        for _, row in prepared.iterrows():
            current_date = pd.Timestamp(row["date"])

            # This block runs before any current-day close-based decision.
            if pending_signal is not None:
                fill = self._execute_pending_signal(
                    symbol,
                    pending_signal,
                    current_date,
                    float(row["open"]),
                    portfolio,
                )
                pending_signal = None
                if fill is not None:
                    portfolio.apply_fill(fill)
                    fills.append(fill)

            # A position bought at this morning's open has one completed
            # holding session when this close becomes observable.
            portfolio.increment_holding_days()
            close_price = float(row["close"])
            position_value = portfolio.position_value(close_price)
            total_equity = portfolio.cash + position_value
            drawdown = self.risk_manager.update_equity(total_equity)
            equity_records.append(
                {
                    "date": current_date,
                    "cash": portfolio.cash,
                    "position_value": position_value,
                    "total_equity": total_equity,
                    "drawdown": drawdown,
                }
            )

            position_context = self._position_context(portfolio)
            signal = self.strategy.generate_signal(row, position_context)
            if signal.action is not SignalAction.HOLD:
                signal_records.append(
                    {
                        "signal_date": signal.signal_date,
                        "action": signal.action.value,
                        "exit_reason": (
                            signal.exit_reason.value
                            if signal.exit_reason is not None
                            else None
                        ),
                    }
                )
                if signal.action is SignalAction.SELL or self.risk_manager.allows_new_position(
                    portfolio.position_count
                ):
                    pending_signal = signal

        trade_log = pd.DataFrame(
            [trade.to_record() for trade in portfolio.closed_trades],
            columns=TRADE_LOG_COLUMNS,
        )
        equity_curve = pd.DataFrame(equity_records, columns=EQUITY_CURVE_COLUMNS)
        signal_log = pd.DataFrame(signal_records, columns=SIGNAL_LOG_COLUMNS)
        return BacktestResult(
            symbol=symbol.strip(),
            initial_capital=float(self.initial_capital),
            prepared_data=prepared,
            trade_log=trade_log,
            equity_curve=equity_curve,
            signal_log=signal_log,
            fills=tuple(fills),
            portfolio=portfolio,
            unexecuted_signal=pending_signal,
        )

    def _execute_pending_signal(
        self,
        symbol: str,
        signal: Signal,
        execution_date: pd.Timestamp,
        open_price: float,
        portfolio: Portfolio,
    ) -> Fill | None:
        """Size and execute one signal at the current open."""

        if signal.action is SignalAction.BUY:
            if portfolio.position is not None:
                raise RuntimeError("BUY signal reached execution with an open position")
            estimated_price = self.execution_model.execution_price(
                OrderSide.BUY, open_price
            )
            portfolio_value = portfolio.total_equity(open_price)
            shares = self.risk_manager.calculate_position_size(
                portfolio_value=portfolio_value,
                execution_price=estimated_price,
                available_cash=portfolio.cash,
                commission_rate=(
                    self.execution_model.position_sizing_commission_rate
                ),
                current_positions=portfolio.position_count,
            )
            if shares == 0:
                return None
            side = OrderSide.BUY
        elif signal.action is SignalAction.SELL:
            if portfolio.position is None:
                raise RuntimeError("SELL signal reached execution without a position")
            shares = portfolio.position.shares
            side = OrderSide.SELL
        else:
            raise RuntimeError("HOLD signals must never reach execution")

        order = Order(
            symbol=symbol.strip(),
            side=side,
            signal_date=signal.signal_date,
            shares=shares,
            exit_reason=signal.exit_reason,
        )
        return self.execution_model.execute(order, execution_date, open_price)

    @staticmethod
    def _position_context(portfolio: Portfolio) -> PositionContext:
        if portfolio.position is None:
            return PositionContext()
        return PositionContext(
            has_position=True,
            entry_price=portfolio.position.entry_price,
            holding_days=portfolio.position.holding_days,
        )


def _write_csv(frame: pd.DataFrame, path: PathLike) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination, index=False)
