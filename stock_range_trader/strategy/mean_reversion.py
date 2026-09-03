"""Long-only ATR-based mean-reversion strategy."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .base import ExitReason, PositionContext, Signal, SignalAction, Strategy


@dataclass(frozen=True, slots=True)
class MeanReversionStrategy(Strategy):
    """Generate close-time signals around an SMA center and ATR envelope."""

    buy_atr_multiplier: float = 1.5
    sell_atr_multiplier: float = 1.5
    range_score_threshold: float = 70.0
    range_exit_threshold: float = 50.0
    adx_entry_max: float = 25.0
    adx_exit_min: float = 30.0
    stop_loss_pct: float = 0.05
    max_holding_days: int = 40
    range_breakdown_days: int = 3

    def __post_init__(self) -> None:
        for name in ("buy_atr_multiplier", "sell_atr_multiplier"):
            value = getattr(self, name)
            if isinstance(value, bool) or not np.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
        for name in ("range_score_threshold", "range_exit_threshold"):
            value = getattr(self, name)
            if not np.isfinite(value) or not 0.0 <= value <= 100.0:
                raise ValueError(f"{name} must be between 0 and 100")
        for name in ("adx_entry_max", "adx_exit_min"):
            value = getattr(self, name)
            if not np.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not np.isfinite(self.stop_loss_pct) or not 0.0 < self.stop_loss_pct < 1.0:
            raise ValueError("stop_loss_pct must be greater than 0 and less than 1")
        for name in ("max_holding_days", "range_breakdown_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def prepare(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Add thresholds and causal entry/exit conditions to scored bars."""

        if not isinstance(frame, pd.DataFrame):
            raise TypeError("strategy input must be a pandas DataFrame")
        required = {"date", "close", "sma", "atr", "adx", "range_score"}
        missing = sorted(required.difference(frame.columns))
        if missing:
            raise ValueError("Missing strategy features: " + ", ".join(missing))

        result = frame.copy()
        result["buy_threshold"] = (
            result["sma"] - self.buy_atr_multiplier * result["atr"]
        )
        result["sell_threshold"] = (
            result["sma"] + self.sell_atr_multiplier * result["atr"]
        )

        complete_entry_data = (
            result[["close", "buy_threshold", "range_score", "adx"]].notna().all(axis=1)
        )
        result["entry_condition"] = (
            complete_entry_data
            & (result["close"] <= result["buy_threshold"])
            & (result["range_score"] >= self.range_score_threshold)
            & (result["adx"] <= self.adx_entry_max)
        )
        result["mean_reversion_exit_condition"] = result[
            ["close", "sell_threshold"]
        ].notna().all(axis=1) & (result["close"] >= result["sell_threshold"])

        complete_range_data = result[["range_score", "adx"]].notna().all(axis=1)
        result["range_breakdown_condition"] = complete_range_data & (
            (result["range_score"] < self.range_exit_threshold)
            | (result["adx"] > self.adx_exit_min)
        )
        result["range_breakdown_streak"] = _consecutive_true_count(
            result["range_breakdown_condition"]
        )
        result["range_breakdown_exit_condition"] = (
            result["range_breakdown_streak"] >= self.range_breakdown_days
        )
        return result

    def generate_signal(self, row: pd.Series, position: PositionContext) -> Signal:
        """Generate one signal after the row's daily close is known.

        EXIT precedence is stop loss, range breakdown, maximum holding period,
        then mean reversion.  Execution timing is deliberately absent here; a
        later execution model must consume this signal on the next bar.
        """

        if not isinstance(row, pd.Series):
            raise TypeError("row must be a pandas Series")
        if "date" not in row:
            raise ValueError("strategy row must contain date")
        signal_date = pd.Timestamp(row["date"])
        if pd.isna(signal_date):
            raise ValueError("strategy row date must be valid")

        if not position.has_position:
            action = (
                SignalAction.BUY
                if _condition_is_true(row.get("entry_condition", False))
                else SignalAction.HOLD
            )
            return Signal(action=action, signal_date=signal_date)

        close = row.get("close", np.nan)
        if np.isfinite(close) and close <= position.entry_price * (
            1.0 - self.stop_loss_pct
        ):
            return Signal(SignalAction.SELL, signal_date, ExitReason.STOP_LOSS)
        if _condition_is_true(row.get("range_breakdown_exit_condition", False)):
            return Signal(SignalAction.SELL, signal_date, ExitReason.RANGE_BREAKDOWN)
        if position.holding_days >= self.max_holding_days:
            return Signal(SignalAction.SELL, signal_date, ExitReason.MAX_HOLDING_PERIOD)
        if _condition_is_true(row.get("mean_reversion_exit_condition", False)):
            return Signal(SignalAction.SELL, signal_date, ExitReason.MEAN_REVERSION)
        return Signal(SignalAction.HOLD, signal_date)


def _consecutive_true_count(condition: pd.Series) -> pd.Series:
    """Count consecutive true observations using only present and past rows."""

    count = 0
    values: list[int] = []
    for is_true in condition.fillna(False).astype(bool).to_numpy():
        count = count + 1 if is_true else 0
        values.append(count)
    return pd.Series(values, index=condition.index, dtype=int)


def _condition_is_true(value: object) -> bool:
    """Interpret missing prepared conditions as false."""

    return False if pd.isna(value) else bool(value)
