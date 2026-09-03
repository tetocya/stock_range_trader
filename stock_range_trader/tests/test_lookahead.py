"""Dedicated regression tests against look-ahead bias."""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import BacktestEngine, MarketOnNextOpen, OrderSide
from risk import RiskManager
from screening import RangeDetector, RangeScorer
from strategy import MeanReversionStrategy

PROJECT_ROOT = Path(__file__).parents[1]
CAUSAL_MODULES = ("indicators", "screening", "strategy", "backtest", "metrics")


def _market_data(size: int = 120) -> pd.DataFrame:
    close = 100.0 + 5.0 * np.sin(np.arange(size) * 2.0 * np.pi / 10.0)
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=size, freq="B"),
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(size, 200_000),
        }
    )


def _run_full_pipeline(frame: pd.DataFrame):
    detector = RangeDetector(
        sma_period=5,
        atr_period=5,
        adx_period=5,
        range_window=10,
        slope_lookback=5,
    )
    scorer = RangeScorer(
        normalized_slope_limit=0.01,
        adx_score_limit=50.0,
        crossing_target=2,
        stability_window=5,
        liquidity_window=10,
        average_volume_target=100_000.0,
        range_threshold=50.0,
        adx_max=50.0,
    )
    strategy = MeanReversionStrategy(
        buy_atr_multiplier=0.5,
        sell_atr_multiplier=0.5,
        range_score_threshold=50.0,
        range_exit_threshold=20.0,
        adx_entry_max=50.0,
        adx_exit_min=60.0,
        stop_loss_pct=0.20,
        max_holding_days=20,
        range_breakdown_days=3,
    )
    engine = BacktestEngine(
        strategy=strategy,
        execution_model=MarketOnNextOpen(slippage_pct=0.001),
        risk_manager=RiskManager(max_drawdown_stop=0.50),
    )
    scored = scorer.transform(detector.transform(frame))
    return engine.run("7203", scored)


def test_1_future_prices_do_not_change_days_1_through_79() -> None:
    """Changing day 80 onward must not alter any result through day 79."""

    original_data = _market_data()
    changed_data = original_data.copy()
    cutoff_position = 79  # zero-based: row 79 is the 80th trading day
    changed_data.loc[cutoff_position:, ["open", "high", "low", "close"]] += 10_000.0

    original = _run_full_pipeline(original_data)
    changed = _run_full_pipeline(changed_data)
    cutoff_date = original_data.iloc[cutoff_position]["date"]

    pd.testing.assert_frame_equal(
        original.prepared_data.iloc[:cutoff_position],
        changed.prepared_data.iloc[:cutoff_position],
    )

    original_signals = original.signal_log.loc[
        original.signal_log["signal_date"] < cutoff_date
    ].reset_index(drop=True)
    changed_signals = changed.signal_log.loc[
        changed.signal_log["signal_date"] < cutoff_date
    ].reset_index(drop=True)
    assert not original_signals.empty
    pd.testing.assert_frame_equal(original_signals, changed_signals)

    original_fills = tuple(
        fill for fill in original.fills if fill.execution_date < cutoff_date
    )
    changed_fills = tuple(
        fill for fill in changed.fills if fill.execution_date < cutoff_date
    )
    assert original_fills
    assert original_fills == changed_fills

    original_trades = original.trade_log.loc[
        original.trade_log["exit_date"] < cutoff_date
    ].reset_index(drop=True)
    changed_trades = changed.trade_log.loc[
        changed.trade_log["exit_date"] < cutoff_date
    ].reset_index(drop=True)
    assert not original_trades.empty
    pd.testing.assert_frame_equal(original_trades, changed_trades)


def test_2_close_signal_never_executes_at_the_same_day_open() -> None:
    """A close-time BUY must use the next available row's open, not today's."""

    opens = np.array([10.0, 900.0])
    closes = np.array([84.0, 900.0])
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-06", periods=2, freq="B"),
            "open": opens,
            "high": np.maximum(opens, closes) + 1.0,
            "low": np.minimum(opens, closes) - 1.0,
            "close": closes,
            "volume": [100_000, 100_000],
            "sma": [100.0, 100.0],
            "atr": [10.0, 10.0],
            "adx": [20.0, 20.0],
            "range_score": [80.0, 80.0],
        }
    )
    engine = BacktestEngine(
        strategy=MeanReversionStrategy(),
        execution_model=MarketOnNextOpen(slippage_pct=0.0),
        risk_manager=RiskManager(),
    )

    result = engine.run("7203", frame)

    buy_fill = result.fills[0]
    assert buy_fill.side is OrderSide.BUY
    assert buy_fill.signal_date == frame.loc[0, "date"]
    assert buy_fill.execution_date == frame.loc[1, "date"]
    assert buy_fill.execution_date > buy_fill.signal_date
    assert buy_fill.raw_open_price == frame.loc[1, "open"]
    assert buy_fill.raw_open_price != frame.loc[0, "open"]


def test_3_rolling_code_has_no_centering_backfill_or_future_shift() -> None:
    """Reject common source-level mechanisms that can introduce future data."""

    violations: list[str] = []
    for directory in CAUSAL_MODULES:
        for path in (PROJECT_ROOT / directory).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(
                    node.func, ast.Attribute
                ):
                    continue

                if node.func.attr == "rolling":
                    for keyword in node.keywords:
                        if (
                            keyword.arg == "center"
                            and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is True
                        ):
                            violations.append(
                                f"{path.relative_to(PROJECT_ROOT)}: center=True"
                            )

                if node.func.attr == "shift":
                    period_node = node.args[0] if node.args else None
                    for keyword in node.keywords:
                        if keyword.arg == "periods":
                            period_node = keyword.value
                    period = _literal_number(period_node)
                    if period is not None and period < 0:
                        violations.append(
                            f"{path.relative_to(PROJECT_ROOT)}: shift({period})"
                        )

                if node.func.attr in {"backfill", "bfill"}:
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)}: {node.func.attr}()"
                    )

    assert not violations, "Potential future-data operations found: " + ", ".join(
        violations
    )


def _literal_number(node: ast.expr | None) -> float | None:
    """Return a numeric AST literal, including a unary negative literal."""

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        return -float(node.operand.value)
    return None
