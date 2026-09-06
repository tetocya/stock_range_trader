"""Unit tests for deterministic Validation-only Phase 3 selection."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from config import load_phase3_config
from walkforward import AnalysisMode
from walkforward.candidates import (
    ExecutableCandidateCatalog,
    SignalCandidateCatalog,
)
from walkforward.selection import (
    CandidateSelection,
    ExecutableCandidateSelector,
    ExecutableValidationScore,
    SelectionInputError,
    SelectionStatus,
    SignalCandidateSelector,
    SignalValidationScore,
)

PROJECT_ROOT = Path(__file__).parents[1]


def _phase3_config():
    return load_phase3_config(PROJECT_ROOT / "config" / "phase3.yaml")


def _signal_catalog() -> SignalCandidateCatalog:
    return SignalCandidateCatalog.from_config(_phase3_config().signal_candidate_catalog)


def _executable_catalog() -> ExecutableCandidateCatalog:
    return ExecutableCandidateCatalog.from_config(
        _phase3_config().executable_candidate_catalog
    )


def _signal_score(
    candidate_id: str,
    *,
    observation_count: int = 30,
    hit_rate: float = 0.60,
    forward_return: float = 0.02,
    mae: float = 0.03,
) -> SignalValidationScore:
    return SignalValidationScore(
        candidate_id=candidate_id,
        observation_count=observation_count,
        mean_reversion_target_hit_rate=hit_rate,
        median_forward_return=forward_return,
        median_mae_magnitude=mae,
    )


def _executable_score(
    candidate_id: str,
    *,
    admitted: int = 10,
    traded: int = 3,
    trades: int = 10,
    finite_sharpe: int = 3,
    sharpe: float | None = 1.0,
    median_drawdown: float | None = 0.10,
    worst_drawdown: float | None = 0.20,
    net_return: float | None = 0.05,
) -> ExecutableValidationScore:
    return ExecutableValidationScore(
        candidate_id=candidate_id,
        admitted_symbol_count=admitted,
        traded_symbol_count=traded,
        total_trade_count=trades,
        finite_sharpe_count=finite_sharpe,
        median_symbol_sharpe_ratio=sharpe,
        median_symbol_maximum_drawdown_magnitude=median_drawdown,
        worst_symbol_maximum_drawdown_magnitude=worst_drawdown,
        median_symbol_net_return=net_return,
    )


def _assessment(selection: CandidateSelection, candidate_id: str):
    return next(
        item for item in selection.assessments if item.candidate_id == candidate_id
    )


def test_signal_selector_uses_primary_metric_first() -> None:
    catalog = _signal_catalog()
    selection = SignalCandidateSelector(_phase3_config().signal_selection).select(
        catalog,
        (
            _signal_score("baseline", hit_rate=0.60),
            _signal_score("conservative", hit_rate=0.80, forward_return=-0.5),
            _signal_score("moderate", hit_rate=0.70),
        ),
    )

    assert selection.analysis_mode is AnalysisMode.SIGNAL_VALIDATION
    assert selection.status is SelectionStatus.SELECTED
    assert selection.selected_candidate_id == "conservative"
    assert selection.ranked_candidate_ids == (
        "conservative",
        "moderate",
        "baseline",
    )


def test_signal_selector_uses_forward_return_then_mae_tie_breakers() -> None:
    catalog = _signal_catalog()
    selector = SignalCandidateSelector(_phase3_config().signal_selection)

    forward = selector.select(
        catalog,
        (
            _signal_score("baseline", forward_return=0.01),
            _signal_score("conservative", forward_return=0.03, mae=0.50),
            _signal_score("moderate", forward_return=0.02),
        ),
    )
    mae = selector.select(
        catalog,
        (
            _signal_score("baseline", mae=0.03),
            _signal_score("conservative", mae=0.01),
            _signal_score("moderate", mae=0.02),
        ),
    )

    assert forward.selected_candidate_id == "conservative"
    assert mae.selected_candidate_id == "conservative"


def test_signal_complete_tie_uses_candidate_id_and_ignores_input_order() -> None:
    catalog = _signal_catalog()
    selector = SignalCandidateSelector(_phase3_config().signal_selection)
    scores = tuple(
        _signal_score(candidate_id) for candidate_id in catalog.candidate_ids
    )

    forward = selector.select(catalog, scores)
    backward = selector.select(catalog, tuple(reversed(scores)))

    assert forward == backward
    assert forward.selected_candidate_id == "baseline"
    assert forward.ranked_candidate_ids == tuple(sorted(catalog.candidate_ids))
    assert tuple(item.candidate_id for item in forward.assessments) == tuple(
        sorted(catalog.candidate_ids)
    )


def test_signal_minimum_observation_boundary_is_inclusive() -> None:
    config = _phase3_config()
    catalog = _signal_catalog()
    policy = config.signal_selection
    selection = SignalCandidateSelector(policy).select(
        catalog,
        (
            _signal_score(
                "baseline", observation_count=policy.minimum_observation_count - 1
            ),
            _signal_score(
                "conservative", observation_count=policy.minimum_observation_count
            ),
            _signal_score(
                "moderate", observation_count=policy.minimum_observation_count - 1
            ),
        ),
    )

    assert selection.selected_candidate_id == "conservative"
    assert _assessment(selection, "conservative").eligible
    assert _assessment(selection, "baseline").rejection_reasons == (
        "insufficient_observation_count",
    )


def test_signal_no_observations_returns_no_eligible_without_fallback() -> None:
    catalog = _signal_catalog()
    empty_scores = tuple(
        SignalValidationScore(candidate_id, 0, None, None, None)
        for candidate_id in reversed(catalog.candidate_ids)
    )

    selection = SignalCandidateSelector(_phase3_config().signal_selection).select(
        catalog, empty_scores
    )

    assert selection.status is SelectionStatus.NO_ELIGIBLE_CANDIDATE
    assert selection.selected_candidate_id is None
    assert selection.ranked_candidate_ids == ()
    assert _assessment(selection, "baseline").rejection_reasons == (
        "insufficient_observation_count",
        "invalid_primary_metric",
        "invalid_median_forward_return",
        "invalid_median_mae_magnitude",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("mean_reversion_target_hit_rate", None),
        ("mean_reversion_target_hit_rate", float("nan")),
        ("mean_reversion_target_hit_rate", float("inf")),
        ("mean_reversion_target_hit_rate", 1.01),
        ("median_forward_return", float("-inf")),
        ("median_mae_magnitude", -0.01),
    ),
)
def test_signal_score_rejects_missing_nonfinite_and_invalid_metrics(
    field: str, value: object
) -> None:
    values = {
        "candidate_id": "baseline",
        "observation_count": 1,
        "mean_reversion_target_hit_rate": 0.5,
        "median_forward_return": 0.01,
        "median_mae_magnitude": 0.02,
    }
    values[field] = value

    with pytest.raises(SelectionInputError):
        SignalValidationScore(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    (("observation_count", True), ("median_forward_return", True)),
)
def test_signal_score_rejects_bool_as_numeric(field: str, value: object) -> None:
    values = {
        "candidate_id": "baseline",
        "observation_count": 1,
        "mean_reversion_target_hit_rate": 0.5,
        "median_forward_return": 0.01,
        "median_mae_magnitude": 0.02,
    }
    values[field] = value

    with pytest.raises(SelectionInputError):
        SignalValidationScore(**values)  # type: ignore[arg-type]


def test_executable_score_uses_fixed_admitted_denominator() -> None:
    assert _executable_score("baseline", admitted=8, traded=3).trading_symbol_ratio == (
        3 / 8
    )
    assert (
        ExecutableValidationScore(
            "empty", 0, 0, 0, 0, None, None, None, None
        ).trading_symbol_ratio
        == 0.0
    )


@pytest.mark.parametrize(
    ("policy_changes", "reason"),
    (
        (
            {"minimum_traded_symbol_count": 4},
            "insufficient_traded_symbol_count",
        ),
        (
            {"minimum_trading_symbol_ratio": 0.5},
            "insufficient_trading_symbol_ratio",
        ),
        (
            {"minimum_total_trade_count": 11},
            "insufficient_total_trade_count",
        ),
        (
            {"minimum_finite_sharpe_count": 4},
            "insufficient_finite_sharpe_count",
        ),
    ),
)
def test_executable_sample_sufficiency_conditions_are_independent(
    policy_changes: dict[str, object],
    reason: str,
) -> None:
    config = _phase3_config()
    policy_values = {
        "minimum_traded_symbol_count": 1,
        "minimum_trading_symbol_ratio": 0.0,
        "minimum_total_trade_count": 1,
        "minimum_finite_sharpe_count": 1,
    }
    policy_values.update(policy_changes)
    policy = replace(config.executable_selection, **policy_values)
    catalog = _executable_catalog()
    scores = tuple(
        _executable_score(candidate_id) for candidate_id in catalog.candidate_ids
    )

    selection = ExecutableCandidateSelector(policy).select(catalog, scores)

    assert selection.status is SelectionStatus.NO_ELIGIBLE_CANDIDATE
    assert _assessment(selection, "baseline").rejection_reasons == (reason,)


def test_executable_thresholds_are_inclusive() -> None:
    config = _phase3_config()
    catalog = _executable_catalog()
    policy = replace(
        config.executable_selection,
        minimum_trading_symbol_ratio=0.30,
    )
    scores = tuple(
        _executable_score(
            candidate_id,
            admitted=10,
            traded=policy.minimum_traded_symbol_count,
            trades=policy.minimum_total_trade_count,
            finite_sharpe=policy.minimum_finite_sharpe_count,
            worst_drawdown=policy.maximum_drawdown_limit,
        )
        for candidate_id in catalog.candidate_ids
    )

    selection = ExecutableCandidateSelector(policy).select(catalog, scores)

    assert selection.status is SelectionStatus.SELECTED
    assert all(item.eligible for item in selection.assessments)


def test_executable_drawdown_limit_uses_worst_symbol_value() -> None:
    catalog = _executable_catalog()
    policy = _phase3_config().executable_selection
    scores = (
        _executable_score(
            "baseline", median_drawdown=0.01, worst_drawdown=0.301, sharpe=100.0
        ),
        _executable_score(
            "conservative", median_drawdown=0.20, worst_drawdown=0.30, sharpe=1.0
        ),
        _executable_score(
            "moderate", median_drawdown=0.10, worst_drawdown=0.30, sharpe=0.5
        ),
    )

    selection = ExecutableCandidateSelector(policy).select(catalog, scores)

    assert selection.selected_candidate_id == "conservative"
    assert _assessment(selection, "baseline").rejection_reasons == (
        "maximum_drawdown_limit_exceeded",
    )


def test_executable_selector_applies_all_tie_breakers_in_order() -> None:
    catalog = _executable_catalog()
    selector = ExecutableCandidateSelector(_phase3_config().executable_selection)

    primary = selector.select(
        catalog,
        (
            _executable_score("baseline", sharpe=1.0, median_drawdown=0.01),
            _executable_score("conservative", sharpe=2.0, median_drawdown=0.20),
            _executable_score("moderate", sharpe=1.5),
        ),
    )
    drawdown = selector.select(
        catalog,
        (
            _executable_score("baseline", median_drawdown=0.10),
            _executable_score("conservative", median_drawdown=0.05),
            _executable_score("moderate", median_drawdown=0.08),
        ),
    )
    net_return = selector.select(
        catalog,
        (
            _executable_score("baseline", net_return=0.01),
            _executable_score("conservative", net_return=0.03),
            _executable_score("moderate", net_return=0.02),
        ),
    )

    assert primary.selected_candidate_id == "conservative"
    assert drawdown.selected_candidate_id == "conservative"
    assert net_return.selected_candidate_id == "conservative"


def test_executable_complete_tie_uses_id_and_ignores_input_order() -> None:
    original = _executable_catalog()
    catalog = ExecutableCandidateCatalog(tuple(reversed(original.candidates)))
    selector = ExecutableCandidateSelector(_phase3_config().executable_selection)
    scores = tuple(
        _executable_score(candidate_id) for candidate_id in catalog.candidate_ids
    )

    forward = selector.select(catalog, scores)
    backward = selector.select(catalog, tuple(reversed(scores)))

    assert forward == backward
    assert forward.selected_candidate_id == "baseline"
    assert forward.ranked_candidate_ids == tuple(sorted(catalog.candidate_ids))


def test_executable_selector_rejects_candidate_dependent_denominator() -> None:
    catalog = _executable_catalog()
    scores = (
        _executable_score("baseline", admitted=10),
        _executable_score("conservative", admitted=11),
        _executable_score("moderate", admitted=10),
    )

    with pytest.raises(SelectionInputError, match="admitted_symbol_count"):
        ExecutableCandidateSelector(_phase3_config().executable_selection).select(
            catalog, scores
        )


def test_zero_admitted_symbols_is_ineligible_without_division_error() -> None:
    catalog = _executable_catalog()
    scores = tuple(
        ExecutableValidationScore(candidate_id, 0, 0, 0, 0, None, None, None, None)
        for candidate_id in catalog.candidate_ids
    )

    selection = ExecutableCandidateSelector(
        _phase3_config().executable_selection
    ).select(catalog, scores)

    assert selection.status is SelectionStatus.NO_ELIGIBLE_CANDIDATE
    assert selection.selected_candidate_id is None
    assert all(not assessment.eligible for assessment in selection.assessments)
    assert _assessment(selection, "baseline").rejection_reasons == (
        "insufficient_traded_symbol_count",
        "insufficient_trading_symbol_ratio",
        "insufficient_total_trade_count",
        "insufficient_finite_sharpe_count",
        "invalid_primary_metric",
        "invalid_median_drawdown_magnitude",
        "invalid_median_net_return",
    )


def test_executable_rejection_reasons_keep_machine_readable_order() -> None:
    catalog = _executable_catalog()
    scores = tuple(
        _executable_score(
            candidate_id,
            traded=0,
            trades=0,
            finite_sharpe=0,
            sharpe=None,
            worst_drawdown=0.31,
        )
        for candidate_id in catalog.candidate_ids
    )

    selection = ExecutableCandidateSelector(
        _phase3_config().executable_selection
    ).select(catalog, scores)

    assert _assessment(selection, "baseline").rejection_reasons == (
        "insufficient_traded_symbol_count",
        "insufficient_trading_symbol_ratio",
        "insufficient_total_trade_count",
        "insufficient_finite_sharpe_count",
        "maximum_drawdown_limit_exceeded",
        "invalid_primary_metric",
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"admitted_symbol_count": True}, "admitted_symbol_count"),
        ({"traded_symbol_count": 11}, "traded_symbol_count"),
        ({"finite_sharpe_count": 11}, "finite_sharpe_count"),
        ({"total_trade_count": 2}, "total_trade_count"),
        ({"median_symbol_sharpe_ratio": float("nan")}, "sharpe"),
        ({"median_symbol_maximum_drawdown_magnitude": -0.01}, "drawdown"),
        ({"worst_symbol_maximum_drawdown_magnitude": -0.01}, "drawdown"),
        ({"median_symbol_net_return": float("inf")}, "net_return"),
    ),
)
def test_executable_score_rejects_invalid_counts_and_metrics(
    changes: dict[str, object], message: str
) -> None:
    values = {
        "candidate_id": "baseline",
        "admitted_symbol_count": 10,
        "traded_symbol_count": 3,
        "total_trade_count": 10,
        "finite_sharpe_count": 3,
        "median_symbol_sharpe_ratio": 1.0,
        "median_symbol_maximum_drawdown_magnitude": 0.10,
        "worst_symbol_maximum_drawdown_magnitude": 0.20,
        "median_symbol_net_return": 0.05,
    }
    values.update(changes)

    with pytest.raises(SelectionInputError, match=message):
        ExecutableValidationScore(**values)  # type: ignore[arg-type]


def test_finite_sharpe_count_and_median_must_be_consistent() -> None:
    with pytest.raises(SelectionInputError, match="must be None"):
        _executable_score("baseline", finite_sharpe=0, sharpe=0.0)
    with pytest.raises(SelectionInputError, match="finite number"):
        _executable_score("baseline", finite_sharpe=1, sharpe=None)


def test_worst_drawdown_cannot_be_below_median_drawdown() -> None:
    with pytest.raises(SelectionInputError, match="worst drawdown"):
        _executable_score("baseline", median_drawdown=0.20, worst_drawdown=0.10)


def test_score_ids_must_match_catalog_exactly() -> None:
    catalog = _signal_catalog()
    selector = SignalCandidateSelector(_phase3_config().signal_selection)
    complete = tuple(
        _signal_score(candidate_id) for candidate_id in catalog.candidate_ids
    )

    with pytest.raises(SelectionInputError, match="must be unique"):
        selector.select(catalog, (complete[0], complete[0], complete[2]))
    with pytest.raises(SelectionInputError, match="missing scores"):
        selector.select(catalog, complete[:-1])
    with pytest.raises(SelectionInputError, match="undefined scores"):
        selector.select(
            catalog,
            (complete[0], complete[1], _signal_score("undefined")),
        )


def test_selectors_reject_other_mode_catalog_score_and_policy() -> None:
    phase3 = _phase3_config()
    signal_selector = SignalCandidateSelector(phase3.signal_selection)
    executable_selector = ExecutableCandidateSelector(phase3.executable_selection)
    signal_catalog = _signal_catalog()
    executable_catalog = _executable_catalog()

    with pytest.raises(SelectionInputError, match="SignalCandidateCatalog"):
        signal_selector.select(executable_catalog, ())  # type: ignore[arg-type]
    with pytest.raises(SelectionInputError, match="ExecutableCandidateCatalog"):
        executable_selector.select(signal_catalog, ())  # type: ignore[arg-type]
    with pytest.raises(SelectionInputError, match="SignalValidationScore"):
        signal_selector.select(
            signal_catalog,
            tuple(
                _executable_score(candidate_id)
                for candidate_id in signal_catalog.candidate_ids
            ),  # type: ignore[arg-type]
        )
    with pytest.raises(SelectionInputError, match="ExecutableValidationScore"):
        executable_selector.select(
            executable_catalog,
            tuple(
                _signal_score(candidate_id)
                for candidate_id in executable_catalog.candidate_ids
            ),  # type: ignore[arg-type]
        )
    with pytest.raises(SelectionInputError, match="SignalSelectionPolicy"):
        SignalCandidateSelector(phase3.executable_selection)  # type: ignore[arg-type]
    with pytest.raises(SelectionInputError, match="ExecutableSelectionPolicy"):
        ExecutableCandidateSelector(phase3.signal_selection)  # type: ignore[arg-type]
