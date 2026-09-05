"""Deterministic fixtures shared by the Phase 3 adversarial test suite."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from config import FoldScheduleConfig, load_strategy_config
from data import CANONICAL_COLUMNS
from universe import UNIVERSE_COLUMNS
from walkforward import (
    ExecutableCandidateCatalog,
    ExecutableCandidateDefinition,
    FoldSchedule,
    SignalCandidateCatalog,
    SignalCandidateDefinition,
    SignalOutcomeEvaluator,
    WalkForwardFold,
)

PROJECT_ROOT = Path(__file__).parents[1]


def adversarial_dates() -> pd.DatetimeIndex:
    """Return fixed sessions with deliberate calendar gaps."""

    dates = pd.bdate_range("2024-01-02", periods=52)
    return dates.delete([6, 27, 41])


def canonical_bars(
    symbol: str = "7203.T",
    *,
    provider: str = "jquants",
    dates: pd.DatetimeIndex | None = None,
    signal_close: np.ndarray | None = None,
    execution_close: np.ndarray | None = None,
) -> pd.DataFrame:
    """Build valid Canonical bars whose signal and execution lanes differ."""

    sessions = adversarial_dates() if dates is None else dates
    size = len(sessions)
    adjusted_close = (
        100.0 + 3.0 * np.sin(np.arange(size, dtype=float))
        if signal_close is None
        else np.asarray(signal_close, dtype=float)
    )
    raw_close = (
        50.0 + np.cos(np.arange(size, dtype=float))
        if execution_close is None
        else np.asarray(execution_close, dtype=float)
    )
    adjusted_open = adjusted_close.copy()
    raw_open = raw_close.copy()
    volume = np.full(size, 100_000.0)
    return pd.DataFrame(
        {
            "date": sessions,
            "symbol": symbol,
            "provider": provider,
            "raw_open": raw_open,
            "raw_high": raw_close + 1.0,
            "raw_low": raw_close - 1.0,
            "raw_close": raw_close,
            "raw_volume": volume,
            "turnover_value": raw_close * volume,
            "adjusted_open": adjusted_open,
            "adjusted_high": adjusted_close + 1.0,
            "adjusted_low": adjusted_close - 1.0,
            "adjusted_close": adjusted_close,
            "adjusted_volume": volume,
            "adjustment_factor": 1.0,
            "dividend": 0.0,
            "stock_split": 0.0,
            "fetched_at": datetime(2024, 4, 1, tzinfo=UTC),
        },
        columns=CANONICAL_COLUMNS,
    )


def strategy_config():
    """Return a fast deterministic StrategyConfig for adversarial cases."""

    return replace(
        load_strategy_config(PROJECT_ROOT / "config" / "strategy.yaml"),
        sma_period=2,
        atr_period=2,
        adx_period=2,
        range_window=2,
        slope_lookback=2,
        crossing_target=1,
        stability_window=2,
        liquidity_window=2,
        normalized_slope_limit=1.0,
        adx_score_limit=100.0,
        stability_cv_limit=10.0,
        median_trading_value_target=1.0,
        initial_capital=10_000.0,
        max_position_pct=1.0,
        lot_size=1,
        slippage_pct=0.0,
        commission_rate=0.0,
        max_drawdown_stop=0.9,
    )


def adversarial_fold(*, fold_id: str = "fold_0001") -> WalkForwardFold:
    dates = adversarial_dates()
    return WalkForwardFold(
        fold_id=fold_id,
        train_start=dates[0].date(),
        train_end=dates[8].date(),
        validation_start=dates[8].date(),
        validation_end=dates[18].date(),
        test_start=dates[23].date(),
        test_end=dates[35].date(),
        embargo_sessions=3,
    )


def later_fold() -> WalkForwardFold:
    dates = adversarial_dates()
    return WalkForwardFold(
        fold_id="fold_0002",
        train_start=dates[25].date(),
        train_end=dates[35].date(),
        validation_start=dates[35].date(),
        validation_end=dates[40].date(),
        test_start=dates[42].date(),
        test_end=(dates[-1] + pd.Timedelta(days=1)).date(),
        embargo_sessions=3,
    )


def fold_schedule(*folds: WalkForwardFold) -> FoldSchedule:
    return FoldSchedule(
        config=FoldScheduleConfig(
            train_months=1,
            validation_months=1,
            test_months=1,
            step_months=1,
            forward_sessions=3,
            embargo_sessions=3,
            minimum_folds=len(folds),
            purge_rule="label_end_date_lt_test_start",
        ),
        configured_start=min(fold.train_start for fold in folds),
        configured_end=max(fold.test_end for fold in folds),
        folds=tuple(folds),
    )


def signal_catalog() -> SignalCandidateCatalog:
    return SignalCandidateCatalog(
        candidates=(
            SignalCandidateDefinition("signal_a", 0.5, 60.0, 30.0),
            SignalCandidateDefinition("signal_b", 1.0, 70.0, 25.0),
            SignalCandidateDefinition("signal_c", 1.5, 80.0, 20.0),
        )
    )


def executable_catalog() -> ExecutableCandidateCatalog:
    return ExecutableCandidateCatalog(
        candidates=(
            ExecutableCandidateDefinition("exec_a", 0.5, 0.5, 60.0, 30.0),
            ExecutableCandidateDefinition("exec_b", 1.0, 1.0, 70.0, 25.0),
            ExecutableCandidateDefinition("exec_c", 1.5, 1.5, 80.0, 20.0),
        )
    )


def install_signal_pipeline(
    monkeypatch,
    signal_dates: set[date],
    *,
    sma: float = 105.0,
) -> None:
    """Install a causal deterministic feature pipeline for boundary tests."""

    def prepare(self, frame, candidate):
        result = frame.copy()
        result["sma"] = sma
        result["atr"] = 5.0
        result["adx"] = 10.0
        result["range_score"] = 80.0
        result["buy_threshold"] = sma - candidate.buy_atr_multiplier * 5.0
        result["entry_condition"] = result["date"].dt.date.isin(signal_dates)
        return result

    monkeypatch.setattr(SignalOutcomeEvaluator, "_prepare_candidate", prepare)


def mutate_ohlc_lane(
    bars: pd.DataFrame,
    mask: pd.Series,
    *,
    lane: str,
    multiplier: float,
) -> pd.DataFrame:
    """Mutate one valid OHLC lane without implicitly rewriting the other."""

    changed = bars.copy()
    for field in ("open", "high", "low", "close"):
        changed.loc[mask, f"{lane}_{field}"] *= multiplier
    if lane == "raw":
        changed.loc[mask, "raw_volume"] *= multiplier
        changed.loc[mask, "turnover_value"] = (
            changed.loc[mask, "raw_close"] * changed.loc[mask, "raw_volume"]
        )
    else:
        changed.loc[mask, "adjusted_volume"] *= multiplier
    return changed


def result_projection(result) -> tuple:
    """Project the causal result fields used by cross-mutation comparisons."""

    return (
        result.provider,
        result.provider_price_basis,
        result.fold_id,
        result.input_symbol_count,
        result.admitted_symbol_count,
        getattr(result, "observations", None),
        getattr(result, "symbol_outcomes", None),
        result.symbol_exclusions,
        result.scores,
    )


def universe_frame(
    *,
    as_of: str = "2024-01-01",
    symbols: tuple[str, ...] = ("7203.T",),
) -> pd.DataFrame:
    """Build a provider-neutral artificial universe snapshot."""

    rows = []
    for index, symbol in enumerate(symbols):
        code = symbol.removesuffix(".T") + "0"
        rows.append(
            {
                "as_of_date": as_of,
                "jquants_code": code,
                "company_name": f"Artificial {index}",
                "market_segment_code": "0111",
                "market_segment_name": "Prime",
                "sector17_code": "6",
                "sector17_name": "Manufacturing",
                "sector33_code": "3700",
                "sector33_name": "Transport Equipment",
                "product_category": "011",
                "yfinance_ticker": symbol,
                "universe_included": True,
                "exclusion_reason": "",
            }
        )
    return pd.DataFrame(rows, columns=UNIVERSE_COLUMNS)
