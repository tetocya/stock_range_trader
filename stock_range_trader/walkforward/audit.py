"""Immutable audit records captured from one Executable Test backtest."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from datetime import date, datetime

import numpy as np
import pandas as pd

from backtest import BacktestResult, BacktestWindow
from backtest.engine import EQUITY_CURVE_COLUMNS, ORDER_LOG_COLUMNS, TRADE_LOG_COLUMNS
from backtest.trade import OrderReason, OrderSide, OrderStatus


class ExecutableAuditError(ValueError):
    """Raised when a BacktestResult cannot be frozen without information loss."""


@dataclass(frozen=True, slots=True)
class ExecutableTestTradeRecord:
    """One completed trade from an independent symbol-fold Test account."""

    fold_id: str
    provider: str
    candidate_id: str
    symbol: str
    sequence: int
    entry_signal_date: date
    entry_date: date
    entry_price: float
    shares: int
    split_adjustment_ratio: float
    split_adjusted_entry_price: float
    exit_shares: int
    exit_signal_date: date
    exit_date: date
    exit_price: float
    exit_reason: str
    gross_profit: float
    commission: float
    slippage_cost: float
    net_profit: float
    return_pct: float
    holding_days: int

    def __post_init__(self) -> None:
        _validate_prefix(self)
        for name in (
            "entry_signal_date",
            "entry_date",
            "exit_signal_date",
            "exit_date",
        ):
            _require_date(name, getattr(self, name))
        if not self.entry_signal_date < self.entry_date:
            raise ExecutableAuditError("entry execution must follow its signal")
        if not self.entry_date < self.exit_date:
            raise ExecutableAuditError("trade exit must follow entry execution")
        if not self.exit_signal_date < self.exit_date:
            raise ExecutableAuditError("exit execution must follow its signal")
        for name in ("entry_price", "split_adjusted_entry_price", "exit_price"):
            _require_positive_finite(name, getattr(self, name))
        _require_positive_finite("split_adjustment_ratio", self.split_adjustment_ratio)
        for name in ("shares", "exit_shares", "holding_days"):
            _require_positive_int(name, getattr(self, name))
        if self.shares != self.exit_shares:
            raise ExecutableAuditError("completed trade share counts must match")
        for name in (
            "gross_profit",
            "commission",
            "slippage_cost",
            "net_profit",
            "return_pct",
        ):
            _require_finite(name, getattr(self, name))
        if self.commission < 0.0 or self.slippage_cost < 0.0:
            raise ExecutableAuditError("trade costs must be non-negative")
        _require_non_empty_string("exit_reason", self.exit_reason)


@dataclass(frozen=True, slots=True)
class ExecutableTestOrderRecord:
    """One terminal order result from an independent symbol-fold Test account."""

    fold_id: str
    provider: str
    candidate_id: str
    symbol: str
    sequence: int
    signal_date: date
    scheduled_execution_date: date | None
    side: str
    requested_shares: int | None
    filled_shares: int
    status: str
    reason: str
    raw_open_price: float | None
    execution_price: float | None
    commission: float | None
    slippage_cost: float | None

    def __post_init__(self) -> None:
        _validate_prefix(self)
        _require_date("signal_date", self.signal_date)
        if self.scheduled_execution_date is not None:
            _require_date("scheduled_execution_date", self.scheduled_execution_date)
            if self.scheduled_execution_date <= self.signal_date:
                raise ExecutableAuditError("scheduled execution must follow its signal")
        if self.side not in {item.value for item in OrderSide}:
            raise ExecutableAuditError("unknown order side")
        if self.status not in {item.value for item in OrderStatus}:
            raise ExecutableAuditError("unknown order status")
        if self.reason not in {item.value for item in OrderReason}:
            raise ExecutableAuditError("unknown order reason")
        _require_optional_non_negative_int("requested_shares", self.requested_shares)
        _require_non_negative_int("filled_shares", self.filled_shares)
        for name in (
            "raw_open_price",
            "execution_price",
            "commission",
            "slippage_cost",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_finite(name, value)
        if self.raw_open_price is not None and self.raw_open_price <= 0.0:
            raise ExecutableAuditError("raw_open_price must be positive")
        if self.execution_price is not None and self.execution_price <= 0.0:
            raise ExecutableAuditError("execution_price must be positive")
        if self.commission is not None and self.commission < 0.0:
            raise ExecutableAuditError("commission must be non-negative")
        if self.slippage_cost is not None and self.slippage_cost < 0.0:
            raise ExecutableAuditError("slippage_cost must be non-negative")
        if self.status == OrderStatus.FILLED.value:
            if self.reason != OrderReason.NONE.value:
                raise ExecutableAuditError("filled orders require reason=none")
            if self.requested_shares != self.filled_shares or self.filled_shares <= 0:
                raise ExecutableAuditError("filled order share counts are invalid")
            if any(
                value is None
                for value in (
                    self.scheduled_execution_date,
                    self.raw_open_price,
                    self.execution_price,
                    self.commission,
                    self.slippage_cost,
                )
            ):
                raise ExecutableAuditError("filled orders require complete fill fields")
        else:
            if self.reason == OrderReason.NONE.value or self.filled_shares != 0:
                raise ExecutableAuditError("non-filled order status is inconsistent")
            if any(
                value is not None
                for value in (
                    self.execution_price,
                    self.commission,
                    self.slippage_cost,
                )
            ):
                raise ExecutableAuditError(
                    "non-filled orders cannot contain fill values"
                )


@dataclass(frozen=True, slots=True)
class ExecutableTestEquityRecord:
    """One marked-to-market row from an independent symbol-fold Test account."""

    fold_id: str
    provider: str
    candidate_id: str
    symbol: str
    sequence: int
    date: date
    cash: float
    position_value: float
    total_equity: float
    drawdown: float

    def __post_init__(self) -> None:
        _validate_prefix(self)
        _require_date("date", self.date)
        for name in ("cash", "position_value", "total_equity", "drawdown"):
            _require_finite(name, getattr(self, name))
        if self.cash < 0.0 or self.position_value < 0.0 or self.total_equity <= 0.0:
            raise ExecutableAuditError("equity values violate the long-only contract")
        if not -1.0 <= self.drawdown <= 0.0:
            raise ExecutableAuditError("drawdown must be in [-1, 0]")


def freeze_executable_test_audit(
    result: BacktestResult,
    *,
    fold_id: str,
    provider: str,
    candidate_id: str,
    window: BacktestWindow,
) -> tuple[
    tuple[ExecutableTestTradeRecord, ...],
    tuple[ExecutableTestOrderRecord, ...],
    tuple[ExecutableTestEquityRecord, ...],
]:
    """Freeze all public logs from the already-completed Test backtest."""

    if not isinstance(result, BacktestResult):
        raise TypeError("result must be BacktestResult")
    if not isinstance(window, BacktestWindow):
        raise TypeError("window must be BacktestWindow")
    prefix = {
        "fold_id": fold_id,
        "provider": provider,
        "candidate_id": candidate_id,
        "symbol": result.symbol,
    }
    _require_exact_columns("trade_log", result.trade_log, TRADE_LOG_COLUMNS)
    _require_exact_columns("order_log", result.order_log, ORDER_LOG_COLUMNS)
    _require_exact_columns("equity_curve", result.equity_curve, EQUITY_CURVE_COLUMNS)

    trades = tuple(
        ExecutableTestTradeRecord(
            **prefix,
            sequence=sequence,
            entry_signal_date=_date_value(row["entry_signal_date"]),
            entry_date=_date_value(row["entry_date"]),
            entry_price=_float_value(row["entry_price"]),
            shares=_int_value(row["shares"]),
            split_adjustment_ratio=_float_value(row["split_adjustment_ratio"]),
            split_adjusted_entry_price=_float_value(row["split_adjusted_entry_price"]),
            exit_shares=_int_value(row["exit_shares"]),
            exit_signal_date=_date_value(row["exit_signal_date"]),
            exit_date=_date_value(row["exit_date"]),
            exit_price=_float_value(row["exit_price"]),
            exit_reason=_string_value(row["exit_reason"]),
            gross_profit=_float_value(row["gross_profit"]),
            commission=_float_value(row["commission"]),
            slippage_cost=_float_value(row["slippage_cost"]),
            net_profit=_float_value(row["net_profit"]),
            return_pct=_float_value(row["return_pct"]),
            holding_days=_int_value(row["holding_days"]),
        )
        for sequence, (_, row) in enumerate(result.trade_log.iterrows())
    )
    orders = tuple(
        ExecutableTestOrderRecord(
            **prefix,
            sequence=sequence,
            signal_date=_date_value(row["signal_date"]),
            scheduled_execution_date=_optional_date_value(
                row["scheduled_execution_date"]
            ),
            side=_string_value(row["side"]),
            requested_shares=_optional_int_value(row["requested_shares"]),
            filled_shares=_int_value(row["filled_shares"]),
            status=_string_value(row["status"]),
            reason=_string_value(row["reason"]),
            raw_open_price=_optional_float_value(row["raw_open_price"]),
            execution_price=_optional_float_value(row["execution_price"]),
            commission=_optional_float_value(row["commission"]),
            slippage_cost=_optional_float_value(row["slippage_cost"]),
        )
        for sequence, (_, row) in enumerate(result.order_log.iterrows())
    )
    equities = tuple(
        ExecutableTestEquityRecord(
            **prefix,
            sequence=sequence,
            date=_date_value(row["date"]),
            cash=_float_value(row["cash"]),
            position_value=_float_value(row["position_value"]),
            total_equity=_float_value(row["total_equity"]),
            drawdown=_float_value(row["drawdown"]),
        )
        for sequence, (_, row) in enumerate(result.equity_curve.iterrows())
    )
    if not equities:
        raise ExecutableAuditError("admitted Test symbols require an equity curve")
    if any(
        not window.contains(value)
        for record in trades
        for value in (
            record.entry_signal_date,
            record.entry_date,
            record.exit_signal_date,
            record.exit_date,
        )
    ):
        raise ExecutableAuditError("trade record date is outside the Test window")
    if any(
        not window.contains(value)
        for record in orders
        for value in (record.signal_date, record.scheduled_execution_date)
        if value is not None
    ):
        raise ExecutableAuditError("order record date is outside the Test window")
    if any(not window.contains(record.date) for record in equities):
        raise ExecutableAuditError("equity record date is outside the Test window")
    return trades, orders, equities


def audit_record_columns(record_type: type) -> tuple[str, ...]:
    """Return the immutable dataclass field order used by report schemas."""

    return tuple(field.name for field in fields(record_type))


def _validate_prefix(
    record: ExecutableTestTradeRecord
    | ExecutableTestOrderRecord
    | ExecutableTestEquityRecord,
) -> None:
    for name in ("fold_id", "provider", "candidate_id", "symbol"):
        _require_non_empty_string(name, getattr(record, name))
    _require_non_negative_int("sequence", record.sequence)


def _require_exact_columns(name: str, frame: object, expected: tuple[str, ...]) -> None:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{name} must be a pandas DataFrame")
    if tuple(frame.columns) != expected:
        raise ExecutableAuditError(f"{name} columns do not match the public schema")


def _date_value(value: object) -> date:
    result = _optional_date_value(value)
    if result is None:
        raise ExecutableAuditError("required audit date is missing")
    return result


def _optional_date_value(value: object) -> date | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise ExecutableAuditError("audit date must be date-like")


def _string_value(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ExecutableAuditError("audit string value is invalid")
    return value


def _float_value(value: object) -> float:
    result = _optional_float_value(value)
    if result is None:
        raise ExecutableAuditError("required audit number is missing")
    return result


def _optional_float_value(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise ExecutableAuditError("audit number must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ExecutableAuditError("audit number must be finite")
    return result


def _int_value(value: object) -> int:
    result = _optional_int_value(value)
    if result is None:
        raise ExecutableAuditError("required audit integer is missing")
    return result


def _optional_int_value(value: object) -> int | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ExecutableAuditError("audit integer must be integral")
    return int(value)


def _require_non_empty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ExecutableAuditError(f"{name} must be a non-empty string")


def _require_date(name: str, value: object) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ExecutableAuditError(f"{name} must be a date")


def _require_finite(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float, np.number))
        or not math.isfinite(float(value))
    ):
        raise ExecutableAuditError(f"{name} must be finite")


def _require_positive_finite(name: str, value: object) -> None:
    _require_finite(name, value)
    if float(value) <= 0.0:
        raise ExecutableAuditError(f"{name} must be positive")


def _require_non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExecutableAuditError(f"{name} must be a non-negative integer")


def _require_positive_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutableAuditError(f"{name} must be a positive integer")


def _require_optional_non_negative_int(name: str, value: object) -> None:
    if value is not None:
        _require_non_negative_int(name, value)
