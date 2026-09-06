"""Unit tests for calendar folds and observed-session purging."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

import pytest

from config import FoldScheduleConfig
from walkforward import (
    FoldSchedule,
    FoldValidationError,
    InsufficientFoldsError,
    PurgePolicy,
    WalkForwardFold,
    generate_fold_schedule,
    resolve_fold_observation_bounds,
)


def _config(
    *,
    train_months: int = 2,
    validation_months: int = 1,
    test_months: int = 1,
    step_months: int = 1,
    forward_sessions: int = 2,
    embargo_sessions: int = 2,
    minimum_folds: int = 1,
) -> FoldScheduleConfig:
    return FoldScheduleConfig(
        train_months=train_months,
        validation_months=validation_months,
        test_months=test_months,
        step_months=step_months,
        forward_sessions=forward_sessions,
        embargo_sessions=embargo_sessions,
        minimum_folds=minimum_folds,
        purge_rule="label_end_date_lt_test_start",
    )


def _fold() -> WalkForwardFold:
    return WalkForwardFold(
        fold_id="fold_0001",
        train_start=date(2023, 1, 1),
        train_end=date(2024, 1, 1),
        validation_start=date(2024, 1, 1),
        validation_end=date(2024, 4, 1),
        test_start=date(2024, 4, 1),
        test_end=date(2024, 7, 1),
        embargo_sessions=2,
    )


def test_fold_intervals_are_half_open_calendar_dates() -> None:
    fold = _fold()

    assert fold.contains_train_date(date(2023, 1, 1))
    assert fold.contains_train_date(date(2023, 12, 31))
    assert not fold.contains_train_date(date(2024, 1, 1))
    assert fold.contains_validation_date(date(2024, 1, 1))
    assert not fold.contains_validation_date(date(2024, 4, 1))
    assert fold.contains_test_date(date(2024, 4, 1))
    assert not fold.contains_test_date(date(2024, 7, 1))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("train_end", date(2023, 1, 1), "Train"),
        ("validation_end", date(2024, 1, 1), "Validation"),
        ("test_end", date(2024, 4, 1), "Test"),
    ),
)
def test_each_fold_partition_must_have_positive_calendar_duration(
    field: str, value: date, message: str
) -> None:
    with pytest.raises(FoldValidationError, match=message):
        replace(_fold(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("validation_start", date(2023, 12, 31), "Train"),
        ("test_start", date(2024, 3, 31), "Validation"),
    ),
)
def test_fold_partitions_must_be_in_chronological_order(
    field: str, value: date, message: str
) -> None:
    with pytest.raises(FoldValidationError, match=message):
        replace(_fold(), **{field: value})


def test_generator_preserves_month_end_across_year_and_leap_day() -> None:
    schedule = generate_fold_schedule(
        _config(train_months=1, minimum_folds=1),
        configured_start=date(2023, 12, 31),
        configured_end=date(2024, 4, 1),
    )

    assert len(schedule.folds) == 1
    fold = schedule.folds[0]
    assert fold.train_start == date(2023, 12, 31)
    assert fold.train_end == date(2024, 1, 31)
    assert fold.validation_end == date(2024, 2, 29)
    assert fold.test_end == date(2024, 3, 31)


def test_generator_does_not_round_weekend_boundaries_to_observations() -> None:
    schedule = generate_fold_schedule(
        _config(),
        configured_start=date(2024, 1, 6),  # Saturday
        configured_end=date(2024, 6, 6),
    )

    first = schedule.folds[0]
    assert first.train_start == date(2024, 1, 6)
    assert first.train_end == date(2024, 3, 6)
    assert first.validation_start == date(2024, 3, 6)
    assert first.test_start == date(2024, 4, 6)  # Saturday remains configured


def test_schedule_rejects_overlapping_test_intervals() -> None:
    first = WalkForwardFold(
        fold_id="fold_0001",
        train_start=date(2023, 1, 1),
        train_end=date(2023, 7, 1),
        validation_start=date(2023, 7, 1),
        validation_end=date(2024, 1, 1),
        test_start=date(2024, 1, 1),
        test_end=date(2024, 2, 1),
        embargo_sessions=2,
    )
    second = WalkForwardFold(
        fold_id="fold_0002",
        train_start=date(2023, 1, 15),
        train_end=date(2023, 7, 15),
        validation_start=date(2023, 7, 15),
        validation_end=date(2024, 1, 15),
        test_start=date(2024, 1, 15),
        test_end=date(2024, 2, 15),
        embargo_sessions=2,
    )

    with pytest.raises(FoldValidationError, match="Test intervals overlap"):
        FoldSchedule(
            config=_config(minimum_folds=2),
            configured_start=date(2023, 1, 1),
            configured_end=date(2024, 3, 1),
            folds=(first, second),
        )


def test_schedule_rejects_duplicate_fold_ids() -> None:
    schedule = generate_fold_schedule(
        _config(minimum_folds=2),
        date(2023, 1, 1),
        date(2023, 7, 1),
    )
    duplicated = replace(schedule.folds[1], fold_id=schedule.folds[0].fold_id)

    with pytest.raises(FoldValidationError, match="fold_id values must be unique"):
        FoldSchedule(
            config=schedule.config,
            configured_start=schedule.configured_start,
            configured_end=schedule.configured_end,
            folds=(schedule.folds[0], duplicated),
        )


def test_generated_fold_ids_are_unique_and_deterministic() -> None:
    first = generate_fold_schedule(
        _config(minimum_folds=2), date(2023, 1, 1), date(2023, 7, 1)
    )
    second = generate_fold_schedule(
        _config(minimum_folds=2), date(2023, 1, 1), date(2023, 7, 1)
    )

    assert first == second
    assert tuple(fold.fold_id for fold in first.folds) == (
        "fold_0001",
        "fold_0002",
        "fold_0003",
    )


def test_schedule_rejects_folds_out_of_chronological_order() -> None:
    generated = generate_fold_schedule(
        _config(minimum_folds=2), date(2023, 1, 1), date(2023, 7, 1)
    )

    with pytest.raises(FoldValidationError, match="chronological order"):
        FoldSchedule(
            config=generated.config,
            configured_start=generated.configured_start,
            configured_end=generated.configured_end,
            folds=tuple(reversed(generated.folds)),
        )


def test_step_months_must_cover_test_months() -> None:
    with pytest.raises(ValueError, match="step_months"):
        _config(test_months=2, step_months=1)


def test_generator_rejects_minimum_folds_shortfall() -> None:
    with pytest.raises(InsufficientFoldsError, match="minimum_folds requires 2"):
        generate_fold_schedule(
            _config(minimum_folds=2),
            date(2023, 1, 1),
            date(2023, 5, 1),
        )


def test_configured_and_actual_observation_boundaries_remain_separate() -> None:
    fold = _fold()
    sessions = (
        date(2023, 1, 3),
        date(2023, 12, 29),
        date(2024, 1, 4),
        date(2024, 3, 29),
        date(2024, 4, 2),
        date(2024, 6, 28),
    )

    bounds = resolve_fold_observation_bounds(fold, "7203.T", reversed(sessions))

    assert bounds.fold.validation_start == date(2024, 1, 1)
    assert bounds.fold.validation_end == date(2024, 4, 1)
    assert bounds.validation_first_observation_date == date(2024, 1, 4)
    assert bounds.validation_last_observation_date == date(2024, 3, 29)
    assert bounds.validation_observation_count == 2
    assert bounds.test_first_observation_date == date(2024, 4, 2)
    assert bounds.test_last_observation_date == date(2024, 6, 28)


def test_label_dates_follow_each_symbols_actual_sessions() -> None:
    fold = _fold()
    policy = PurgePolicy(forward_sessions=2)
    feature = [date(2024, 3, 25)]

    first = policy.assess_validation(
        fold,
        "A",
        feature,
        [date(2024, 3, 25), date(2024, 3, 26), date(2024, 3, 27)],
    )[0]
    second = policy.assess_validation(
        fold,
        "B",
        feature,
        [date(2024, 3, 25), date(2024, 3, 28), date(2024, 3, 29)],
    )[0]

    assert first.label_start_date == date(2024, 3, 26)
    assert first.label_end_date == date(2024, 3, 27)
    assert second.label_start_date == date(2024, 3, 28)
    assert second.label_end_date == date(2024, 3, 29)
    assert first.retained and second.retained


def test_validation_label_cannot_enter_test_interval() -> None:
    fold = _fold()
    policy = PurgePolicy(forward_sessions=2)
    observations = policy.assess_validation(
        fold,
        "7203.T",
        [date(2024, 3, 27), date(2024, 3, 28)],
        [
            date(2024, 3, 27),
            date(2024, 3, 28),
            date(2024, 4, 1),
            date(2024, 4, 2),
        ],
    )

    assert observations[0].label_end_date == fold.test_start
    assert observations[0].purge_reason == policy.VALIDATION_OVERLAP_REASON
    assert observations[1].label_end_date == date(2024, 4, 2)
    assert observations[1].purge_reason == policy.VALIDATION_OVERLAP_REASON
    assert not any(observation.retained for observation in observations)


def test_validation_rejects_embargo_shorter_than_forward_horizon() -> None:
    with pytest.raises(FoldValidationError, match="embargo_sessions.*must"):
        PurgePolicy(forward_sessions=3).assess_validation(
            _fold(),
            "7203.T",
            [date(2024, 3, 27)],
            [],
        )


def test_test_rejects_embargo_shorter_than_forward_horizon() -> None:
    with pytest.raises(FoldValidationError, match="embargo_sessions.*must"):
        PurgePolicy(forward_sessions=3).assess_test(
            _fold(),
            "7203.T",
            [date(2024, 6, 27)],
            [],
        )


def test_embargo_equal_to_forward_horizon_is_allowed() -> None:
    fold = _fold()
    policy = PurgePolicy(forward_sessions=fold.embargo_sessions)

    validation = policy.assess_validation(
        fold,
        "7203.T",
        [date(2024, 3, 25)],
        [date(2024, 3, 25), date(2024, 3, 26), date(2024, 3, 27)],
    )[0]
    test = policy.assess_test(
        fold,
        "7203.T",
        [date(2024, 4, 1)],
        [date(2024, 4, 1), date(2024, 4, 2), date(2024, 4, 3)],
    )[0]

    assert validation.retained
    assert test.retained


def test_policy_from_config_accepts_fold_generated_from_same_config() -> None:
    config = _config(forward_sessions=2, embargo_sessions=2)
    schedule = generate_fold_schedule(
        config,
        configured_start=date(2023, 1, 1),
        configured_end=date(2023, 5, 1),
    )
    policy = PurgePolicy.from_schedule_config(config)

    observation = policy.assess_validation(
        schedule.folds[0],
        "7203.T",
        [date(2023, 3, 1)],
        [date(2023, 3, 1), date(2023, 3, 2), date(2023, 3, 3)],
    )[0]

    assert observation.retained
    assert observation.label_end_date == date(2023, 3, 3)


def test_test_observation_without_full_horizon_is_right_censored() -> None:
    fold = replace(_fold(), embargo_sessions=3)
    policy = PurgePolicy(forward_sessions=3)
    observation = policy.assess_test(
        fold,
        "7203.T",
        [date(2024, 6, 27)],
        [
            date(2024, 6, 27),
            date(2024, 6, 28),
            date(2024, 7, 1),
            date(2024, 7, 2),
        ],
    )[0]

    assert observation.label_start_date == date(2024, 6, 28)
    assert observation.label_end_date is None
    assert observation.purge_reason == policy.RIGHT_CENSORED_REASON
    assert not observation.retained


def test_input_order_does_not_change_observation_labels() -> None:
    fold = _fold()
    policy = PurgePolicy(forward_sessions=2)
    sessions = [
        date(2024, 2, 1),
        date(2024, 2, 2),
        date(2024, 2, 5),
        date(2024, 2, 6),
    ]
    features = [date(2024, 2, 2), date(2024, 2, 1)]

    chronological = policy.assess_validation(fold, "A", features, sessions)
    reversed_input = policy.assess_validation(
        fold, "A", reversed(features), reversed(sessions)
    )

    assert chronological == reversed_input
    assert [item.feature_date for item in chronological] == sorted(features)


def test_future_data_only_appends_folds_and_cannot_change_past_labels() -> None:
    config = _config(minimum_folds=1)
    short = generate_fold_schedule(
        config,
        date(2023, 1, 31),
        date(2023, 6, 1),
    )
    long = generate_fold_schedule(
        config,
        date(2023, 1, 31),
        date(2023, 9, 1),
    )

    assert short.folds == long.folds[: len(short.folds)]

    fold = replace(_fold(), embargo_sessions=3)
    policy = PurgePolicy(forward_sessions=3)
    base_sessions = [date(2024, 6, 27), date(2024, 6, 28)]
    base = policy.assess_test(fold, "A", [date(2024, 6, 27)], base_sessions)
    appended = policy.assess_test(
        fold,
        "A",
        [date(2024, 6, 27)],
        base_sessions + [date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3)],
    )

    assert base == appended


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("train_start", "2023-01-01"),
        ("train_start", float("nan")),
        ("test_end", datetime(2024, 7, 1)),
        ("embargo_sessions", True),
        ("embargo_sessions", -1),
    ),
)
def test_fold_rejects_invalid_types_bool_and_negative_values(
    field: str, value: object
) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(_fold(), **{field: value})


@pytest.mark.parametrize("value", (True, -1, 1.5, float("nan")))
def test_purge_policy_rejects_invalid_forward_sessions(value: object) -> None:
    with pytest.raises(ValueError, match="forward_sessions"):
        PurgePolicy(forward_sessions=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_date", ("2024-01-01", float("nan"), None))
def test_observation_dates_reject_invalid_values(bad_date: object) -> None:
    with pytest.raises(TypeError, match="datetime.date"):
        resolve_fold_observation_bounds(_fold(), "A", [bad_date])


def test_feature_date_must_be_an_actual_symbol_session() -> None:
    with pytest.raises(ValueError, match="not an observed session"):
        PurgePolicy(forward_sessions=1).assess_validation(
            _fold(),
            "A",
            [date(2024, 2, 2)],
            [date(2024, 2, 1), date(2024, 2, 5)],
        )
