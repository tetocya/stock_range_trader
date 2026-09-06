"""Causal daily-bar backtest orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

import pandas as pd
from pandas.api.types import is_datetime64_any_dtype

from data import validate_backtest_price_contract
from data.validation import validate_ohlcv, validate_required_columns
from risk import RiskManager
from strategy import PositionContext, Signal, SignalAction, Strategy

from .execution import ExecutionModel, MarketBar
from .portfolio import Portfolio
from .trade import (
    Fill,
    Order,
    OrderReason,
    OrderResult,
    OrderSide,
    OrderStatus,
)
from .window import BacktestWindow, BacktestWindowError

PathLike: TypeAlias = str | Path

TRADE_LOG_COLUMNS: tuple[str, ...] = (
    "symbol",
    "entry_signal_date",
    "entry_date",
    "entry_price",
    "shares",
    "split_adjustment_ratio",
    "split_adjusted_entry_price",
    "exit_shares",
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
ORDER_LOG_COLUMNS: tuple[str, ...] = (
    "symbol",
    "signal_date",
    "scheduled_execution_date",
    "side",
    "requested_shares",
    "filled_shares",
    "status",
    "reason",
    "raw_open_price",
    "execution_price",
    "commission",
    "slippage_cost",
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
    order_log: pd.DataFrame
    fills: tuple[Fill, ...]
    portfolio: Portfolio
    unexecuted_signal: Signal | None
    benchmark_lot_size: int
    benchmark_buy_price_multiplier: float
    benchmark_commission_rate: float

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

    def save_order_log(self, path: PathLike) -> None:
        """Write every terminal order result to CSV without the index."""

        _write_csv(self.order_log, path)


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

    def run(
        self,
        symbol: str,
        frame: pd.DataFrame,
        *,
        window: BacktestWindow | None = None,
    ) -> BacktestResult:
        """Run a single-symbol daily backtest.

        For each row, an earlier pending signal is processed at the current
        open before the current close can be observed.  Only after marking the
        portfolio at that close does the strategy generate the next pending
        signal.  This sequence makes same-day-open execution impossible.
        """

        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must not be empty")
        if window is None:
            validate_ohlcv(frame)
            validate_backtest_price_contract(frame)
            prepared = self.strategy.prepare(frame)
        else:
            if not isinstance(window, BacktestWindow):
                raise TypeError("window must be BacktestWindow or None")
            _validate_window_frame_structure(frame)
            causal_history = frame.loc[
                frame["date"].dt.date < window.trading_end
            ].copy()
            if causal_history.empty:
                raise BacktestWindowError(
                    "no observations exist before the backtest window end"
                )
            validate_ohlcv(causal_history)
            validate_backtest_price_contract(causal_history)
            trading_data = causal_history.loc[
                causal_history["date"].dt.date >= window.trading_start
            ].copy()
            if trading_data.empty:
                raise BacktestWindowError(
                    "no observations exist inside the backtest trading window"
                )
            prepared = self.strategy.prepare(trading_data)
        portfolio = Portfolio(self.initial_capital)
        self.risk_manager.reset()

        pending_signal: Signal | None = None
        fills: list[Fill] = []
        order_results: list[OrderResult] = []
        equity_records: list[dict[str, object]] = []
        signal_records: list[dict[str, object]] = []

        for position, (_, row) in enumerate(prepared.iterrows()):
            market_bar = MarketBar.from_series(row)
            current_date = market_bar.date

            # This block runs before any current-day close-based decision.
            if pending_signal is not None:
                fill, order_result = self._execute_pending_signal(
                    symbol,
                    pending_signal,
                    market_bar,
                    portfolio,
                )
                pending_signal = None
                order_results.append(order_result)
                if fill is not None:
                    signal_entry_price = None
                    if fill.side is OrderSide.BUY:
                        signal_entry_price = self.execution_model.execution_price(
                            OrderSide.BUY, float(market_bar.signal_open)
                        )
                    portfolio.apply_fill(
                        fill,
                        signal_entry_price=signal_entry_price,
                    )
                    fills.append(fill)

            # A position bought at this morning's open has one completed
            # holding session when this close becomes observable.
            portfolio.increment_holding_days()
            close_price = market_bar.close
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
                if position == len(prepared) - 1:
                    order_results.append(
                        self._unfilled_order_result(
                            symbol=symbol,
                            signal=signal,
                            status=OrderStatus.CANCELED,
                            reason=OrderReason.NO_NEXT_BAR,
                        )
                    )
                    pending_signal = signal
                elif (
                    signal.action is SignalAction.BUY
                    and not self.risk_manager.allows_new_position(
                        portfolio.position_count
                    )
                ):
                    order_results.append(
                        self._unfilled_order_result(
                            symbol=symbol,
                            signal=signal,
                            scheduled_execution_date=pd.Timestamp(
                                prepared.iloc[position + 1]["date"]
                            ),
                            status=OrderStatus.REJECTED,
                            reason=OrderReason.RISK_LIMIT,
                        )
                    )
                else:
                    pending_signal = signal

        trade_log = pd.DataFrame(
            [trade.to_record() for trade in portfolio.closed_trades],
            columns=TRADE_LOG_COLUMNS,
        )
        equity_curve = pd.DataFrame(equity_records, columns=EQUITY_CURVE_COLUMNS)
        signal_log = pd.DataFrame(signal_records, columns=SIGNAL_LOG_COLUMNS)
        order_log = pd.DataFrame(
            [result.to_record() for result in order_results],
            columns=ORDER_LOG_COLUMNS,
        )
        return BacktestResult(
            symbol=symbol.strip(),
            initial_capital=float(self.initial_capital),
            prepared_data=prepared,
            trade_log=trade_log,
            equity_curve=equity_curve,
            signal_log=signal_log,
            order_log=order_log,
            fills=tuple(fills),
            portfolio=portfolio,
            unexecuted_signal=pending_signal,
            benchmark_lot_size=self.risk_manager.lot_size,
            benchmark_buy_price_multiplier=self.execution_model.execution_price(
                OrderSide.BUY, 1.0
            ),
            benchmark_commission_rate=(
                self.execution_model.position_sizing_commission_rate
            ),
        )

    def _execute_pending_signal(
        self,
        symbol: str,
        signal: Signal,
        market_bar: MarketBar,
        portfolio: Portfolio,
    ) -> tuple[Fill | None, OrderResult]:
        """Size and attempt one signal at its scheduled market bar."""

        if signal.action is SignalAction.BUY:
            if portfolio.position is not None:
                raise RuntimeError("BUY signal reached execution with an open position")
            estimated_price = self.execution_model.execution_price(
                OrderSide.BUY, market_bar.open
            )
            portfolio_value = portfolio.total_equity(market_bar.open)
            shares = self.risk_manager.calculate_position_size(
                portfolio_value=portfolio_value,
                execution_price=estimated_price,
                available_cash=portfolio.cash,
                commission_rate=(self.execution_model.position_sizing_commission_rate),
                current_positions=portfolio.position_count,
            )
            if shares == 0:
                return None, self._unfilled_order_result(
                    symbol=symbol,
                    signal=signal,
                    scheduled_execution_date=market_bar.date,
                    requested_shares=0,
                    status=OrderStatus.REJECTED,
                    reason=OrderReason.INSUFFICIENT_CAPITAL_FOR_LOT,
                    raw_open_price=market_bar.open,
                )
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
        fill = self.execution_model.execute(order, market_bar)
        if fill is None:
            return None, self._unfilled_order_result(
                symbol=symbol,
                signal=signal,
                scheduled_execution_date=market_bar.date,
                requested_shares=shares,
                status=OrderStatus.CANCELED,
                reason=OrderReason.NON_TRADABLE_BAR,
                raw_open_price=market_bar.open,
            )
        return fill, OrderResult.from_fill(fill)

    @staticmethod
    def _unfilled_order_result(
        *,
        symbol: str,
        signal: Signal,
        status: OrderStatus,
        reason: OrderReason,
        scheduled_execution_date: pd.Timestamp | None = None,
        requested_shares: int | None = None,
        raw_open_price: float | None = None,
    ) -> OrderResult:
        side = OrderSide.BUY if signal.action is SignalAction.BUY else OrderSide.SELL
        return OrderResult(
            symbol=symbol.strip(),
            signal_date=signal.signal_date,
            scheduled_execution_date=scheduled_execution_date,
            side=side,
            requested_shares=requested_shares,
            filled_shares=0,
            status=status,
            reason=reason,
            raw_open_price=raw_open_price,
        )

    @staticmethod
    def _position_context(portfolio: Portfolio) -> PositionContext:
        if portfolio.position is None:
            return PositionContext()
        return PositionContext(
            has_position=True,
            entry_price=portfolio.position.signal_entry_price,
            holding_days=portfolio.position.holding_days,
        )


def _write_csv(frame: pd.DataFrame, path: PathLike) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination, index=False)


def _validate_window_frame_structure(frame: pd.DataFrame) -> None:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("OHLCV data must be provided as a pandas DataFrame")
    validate_required_columns(frame.columns)
    if not is_datetime64_any_dtype(frame["date"].dtype):
        raise BacktestWindowError("date must have a pandas datetime dtype")
    if frame["date"].isna().any():
        raise BacktestWindowError("date must not contain missing values")
