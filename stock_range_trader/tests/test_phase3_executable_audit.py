"""STEP 8 immutable Executable Test audit-record contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest
from test_phase3_executable_evaluation import (
    _bars,
    _catalog,
    _evaluator,
    _fold,
    _install_constant_features,
)
from test_phase3_executable_test_evaluation import _cohort, _test_trade_bars

from backtest import BacktestEngine
from backtest.engine import EQUITY_CURVE_COLUMNS, ORDER_LOG_COLUMNS, TRADE_LOG_COLUMNS
from walkforward import (
    ExecutableEvaluationError,
    ExecutableTestEquityRecord,
    ExecutableTestOrderRecord,
    ExecutableTestTradeRecord,
    audit_record_columns,
)


def test_one_engine_run_produces_outcome_and_complete_immutable_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    original = BacktestEngine.run
    calls = 0

    def recording(self, symbol, frame, *, window=None):
        nonlocal calls
        calls += 1
        return original(self, symbol, frame, window=window)

    monkeypatch.setattr(BacktestEngine, "run", recording)
    result = _evaluator().evaluate_test(
        _test_trade_bars(), _fold(), _catalog().candidates[0], _cohort()
    )

    assert calls == 1
    assert len(result.trade_records) == result.symbol_outcomes[0].number_of_trades == 1
    assert len(result.order_records) == 2
    assert len(result.equity_records) > 0
    assert result.equity_records[-1].total_equity == pytest.approx(
        result.symbol_outcomes[0].final_equity
    )
    with pytest.raises(FrozenInstanceError):
        result.equity_records[0].cash = 0.0  # type: ignore[misc]


def test_audit_record_fields_preserve_all_phase1_log_columns() -> None:
    prefix = ("fold_id", "provider", "candidate_id")

    assert audit_record_columns(ExecutableTestTradeRecord) == (
        *prefix,
        "symbol",
        "sequence",
        *TRADE_LOG_COLUMNS[1:],
    )
    assert audit_record_columns(ExecutableTestOrderRecord) == (
        *prefix,
        "symbol",
        "sequence",
        *ORDER_LOG_COLUMNS[1:],
    )
    assert audit_record_columns(ExecutableTestEquityRecord) == (
        *prefix,
        "symbol",
        "sequence",
        *EQUITY_CURVE_COLUMNS,
    )


def test_empty_trade_and_order_logs_are_valid_but_equity_is_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    result = _evaluator().evaluate_test(
        _bars(signal_close=np.full(40, 100.0)),
        _fold(),
        _catalog().candidates[0],
        _cohort(),
    )

    assert result.trade_records == ()
    assert result.order_records == ()
    assert result.equity_records
    assert result.symbol_outcomes[0].number_of_trades == 0


def test_result_rejects_equity_outside_test_observation_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    result = _evaluator().evaluate_test(
        _bars(signal_close=np.full(40, 100.0)),
        _fold(),
        _catalog().candidates[0],
        _cohort(),
    )
    changed = replace(result.equity_records[0], date=_fold().train_start)

    with pytest.raises(ExecutableEvaluationError, match="outside"):
        replace(result, equity_records=(changed, *result.equity_records[1:]))


def test_result_rejects_non_contiguous_audit_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_constant_features(monkeypatch)
    result = _evaluator().evaluate_test(
        _bars(signal_close=np.full(40, 100.0)),
        _fold(),
        _catalog().candidates[0],
        _cohort(),
    )
    changed = replace(result.equity_records[-1], sequence=1_000)

    with pytest.raises(ExecutableEvaluationError, match="sequences"):
        replace(result, equity_records=(*result.equity_records[:-1], changed))
