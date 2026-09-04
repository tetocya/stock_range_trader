"""Calendar fold generation and observed-session forward-label purging."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import ClassVar

from config.phase3 import FoldScheduleConfig


class FoldValidationError(ValueError):
    """Raised when configured fold boundaries are internally inconsistent."""


class InsufficientFoldsError(FoldValidationError):
    """Raised instead of silently reducing the configured minimum fold count."""


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """One immutable set of half-open calendar-date fold boundaries."""

    fold_id: str
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    embargo_sessions: int

    def __post_init__(self) -> None:
        if not isinstance(self.fold_id, str) or not self.fold_id.strip():
            raise FoldValidationError("fold_id must be a non-empty string")
        for name in (
            "train_start",
            "train_end",
            "validation_start",
            "validation_end",
            "test_start",
            "test_end",
        ):
            _require_date(name, getattr(self, name))
        _require_non_negative_int("embargo_sessions", self.embargo_sessions)
        if self.train_start >= self.train_end:
            raise FoldValidationError("Train must have positive calendar duration")
        if self.validation_start >= self.validation_end:
            raise FoldValidationError("Validation must have positive calendar duration")
        if self.test_start >= self.test_end:
            raise FoldValidationError("Test must have positive calendar duration")
        if self.train_end > self.validation_start:
            raise FoldValidationError("Train must end before Validation starts")
        if self.validation_end > self.test_start:
            raise FoldValidationError("Validation must end before Test starts")

    def contains_train_date(self, value: date) -> bool:
        """Return membership in the half-open Train interval."""

        _require_date("value", value)
        return self.train_start <= value < self.train_end

    def contains_validation_date(self, value: date) -> bool:
        """Return membership in the half-open Validation interval."""

        _require_date("value", value)
        return self.validation_start <= value < self.validation_end

    def contains_test_date(self, value: date) -> bool:
        """Return membership in the half-open Test interval."""

        _require_date("value", value)
        return self.test_start <= value < self.test_end


@dataclass(frozen=True, slots=True)
class FoldSchedule:
    """A validated deterministic sequence of calendar-date folds."""

    config: FoldScheduleConfig
    configured_start: date
    configured_end: date
    folds: tuple[WalkForwardFold, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.config, FoldScheduleConfig):
            raise TypeError("config must be FoldScheduleConfig")
        _require_date("configured_start", self.configured_start)
        _require_date("configured_end", self.configured_end)
        if self.configured_start >= self.configured_end:
            raise FoldValidationError("configured_start must be before configured_end")
        if not isinstance(self.folds, tuple) or any(
            not isinstance(fold, WalkForwardFold) for fold in self.folds
        ):
            raise TypeError("folds must be a tuple of WalkForwardFold values")
        if len(self.folds) < self.config.minimum_folds:
            raise InsufficientFoldsError(
                f"generated {len(self.folds)} folds; minimum_folds requires "
                f"{self.config.minimum_folds}"
            )

        fold_ids = [fold.fold_id for fold in self.folds]
        if len(fold_ids) != len(set(fold_ids)):
            raise FoldValidationError("fold_id values must be unique")
        chronological = tuple(
            sorted(
                self.folds,
                key=lambda fold: (
                    fold.train_start,
                    fold.validation_start,
                    fold.test_start,
                    fold.fold_id,
                ),
            )
        )
        if self.folds != chronological:
            raise FoldValidationError("folds must be in chronological order")

        previous: WalkForwardFold | None = None
        for fold in self.folds:
            if fold.train_start < self.configured_start:
                raise FoldValidationError(
                    f"{fold.fold_id} starts before configured_start"
                )
            if fold.test_end > self.configured_end:
                raise FoldValidationError(f"{fold.fold_id} ends after configured_end")
            if fold.embargo_sessions != self.config.embargo_sessions:
                raise FoldValidationError(
                    f"{fold.fold_id} embargo_sessions does not match its schedule"
                )
            if previous is not None and previous.test_end > fold.test_start:
                raise FoldValidationError(
                    f"Test intervals overlap: {previous.fold_id} and {fold.fold_id}"
                )
            previous = fold


@dataclass(frozen=True, slots=True)
class FoldObservationBounds:
    """Configured fold plus actual first/last observations for one symbol."""

    fold: WalkForwardFold
    symbol: str
    train_first_observation_date: date | None
    train_last_observation_date: date | None
    train_observation_count: int
    validation_first_observation_date: date | None
    validation_last_observation_date: date | None
    validation_observation_count: int
    test_first_observation_date: date | None
    test_last_observation_date: date | None
    test_observation_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.fold, WalkForwardFold):
            raise TypeError("fold must be WalkForwardFold")
        _require_symbol(self.symbol)
        _validate_actual_bounds(
            "Train",
            self.train_first_observation_date,
            self.train_last_observation_date,
            self.train_observation_count,
            self.fold.train_start,
            self.fold.train_end,
        )
        _validate_actual_bounds(
            "Validation",
            self.validation_first_observation_date,
            self.validation_last_observation_date,
            self.validation_observation_count,
            self.fold.validation_start,
            self.fold.validation_end,
        )
        _validate_actual_bounds(
            "Test",
            self.test_first_observation_date,
            self.test_last_observation_date,
            self.test_observation_count,
            self.fold.test_start,
            self.fold.test_end,
        )


@dataclass(frozen=True, slots=True)
class ForwardObservation:
    """One feature date and its label dates on a symbol's observed sessions."""

    fold_id: str
    partition: str
    symbol: str
    feature_date: date
    label_start_date: date | None
    label_end_date: date | None
    purge_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.fold_id, str) or not self.fold_id.strip():
            raise ValueError("fold_id must be a non-empty string")
        if self.partition not in {"validation", "test"}:
            raise ValueError("partition must be 'validation' or 'test'")
        _require_symbol(self.symbol)
        _require_date("feature_date", self.feature_date)
        for name in ("label_start_date", "label_end_date"):
            value = getattr(self, name)
            if value is not None:
                _require_date(name, value)
        if self.label_start_date is not None and (
            self.label_start_date <= self.feature_date
        ):
            raise ValueError("label_start_date must be after feature_date")
        if self.label_end_date is not None:
            if self.label_start_date is None:
                raise ValueError("label_end_date requires label_start_date")
            if self.label_end_date < self.label_start_date:
                raise ValueError("label_end_date must not precede label_start_date")
        if self.purge_reason is not None and (
            not isinstance(self.purge_reason, str) or not self.purge_reason.strip()
        ):
            raise ValueError("purge_reason must be None or a non-empty string")
        if self.purge_reason is None and self.label_end_date is None:
            raise ValueError("a retained observation requires label_end_date")

    @property
    def retained(self) -> bool:
        """Return whether this observation survives purge/censoring."""

        return self.purge_reason is None


@dataclass(frozen=True, slots=True)
class PurgePolicy:
    """Build forward labels from actual per-symbol observed sessions only."""

    LABEL_END_RULE: ClassVar[str] = "label_end_date_lt_test_start"
    VALIDATION_OVERLAP_REASON: ClassVar[str] = (
        "validation_label_end_not_before_test_start"
    )
    INSUFFICIENT_LABEL_REASON: ClassVar[str] = (
        "insufficient_forward_sessions_within_fold"
    )
    RIGHT_CENSORED_REASON: ClassVar[str] = "right_censored_at_test_end"

    forward_sessions: int
    purge_rule: str = LABEL_END_RULE

    def __post_init__(self) -> None:
        _require_positive_int("forward_sessions", self.forward_sessions)
        if self.purge_rule != self.LABEL_END_RULE:
            raise ValueError("purge_rule must retain only label_end_date < test_start")

    @classmethod
    def from_schedule_config(cls, config: FoldScheduleConfig) -> PurgePolicy:
        """Create a policy from the immutable STEP 2 schedule contract."""

        if not isinstance(config, FoldScheduleConfig):
            raise TypeError("config must be FoldScheduleConfig")
        return cls(
            forward_sessions=config.forward_sessions,
            purge_rule=config.purge_rule,
        )

    def assess_validation(
        self,
        fold: WalkForwardFold,
        symbol: str,
        feature_dates: object,
        observed_session_dates: object,
    ) -> tuple[ForwardObservation, ...]:
        """Purge Validation labels that do not end before Test starts."""

        return self._assess(
            fold=fold,
            partition="validation",
            symbol=symbol,
            feature_dates=feature_dates,
            observed_session_dates=observed_session_dates,
        )

    def assess_test(
        self,
        fold: WalkForwardFold,
        symbol: str,
        feature_dates: object,
        observed_session_dates: object,
    ) -> tuple[ForwardObservation, ...]:
        """Right-censor Test labels lacking a full horizon before Test end."""

        return self._assess(
            fold=fold,
            partition="test",
            symbol=symbol,
            feature_dates=feature_dates,
            observed_session_dates=observed_session_dates,
        )

    def _assess(
        self,
        *,
        fold: WalkForwardFold,
        partition: str,
        symbol: str,
        feature_dates: object,
        observed_session_dates: object,
    ) -> tuple[ForwardObservation, ...]:
        if not isinstance(fold, WalkForwardFold):
            raise TypeError("fold must be WalkForwardFold")
        _require_symbol(symbol)
        sessions = _normalize_unique_dates(
            "observed_session_dates", observed_session_dates
        )
        # Nothing on or after configured Test end can affect this fold. This
        # preserves past-fold output when genuinely future data is appended.
        sessions = tuple(value for value in sessions if value < fold.test_end)
        features = _normalize_unique_dates("feature_dates", feature_dates)
        if partition == "validation":
            contains_feature = fold.contains_validation_date
        else:
            contains_feature = fold.contains_test_date

        session_positions = {value: index for index, value in enumerate(sessions)}
        observations: list[ForwardObservation] = []
        for feature_date in features:
            if not contains_feature(feature_date):
                raise ValueError(
                    f"feature_date {feature_date.isoformat()} is outside the "
                    f"configured {partition} interval"
                )
            if feature_date not in session_positions:
                raise ValueError(
                    f"feature_date {feature_date.isoformat()} is not an observed "
                    f"session for {symbol}"
                )
            position = session_positions[feature_date]
            label_start_position = position + 1
            label_end_position = position + self.forward_sessions
            label_start = (
                sessions[label_start_position]
                if label_start_position < len(sessions)
                else None
            )
            label_end = (
                sessions[label_end_position]
                if label_end_position < len(sessions)
                else None
            )

            if partition == "validation":
                if label_end is None:
                    purge_reason = self.INSUFFICIENT_LABEL_REASON
                elif label_end < fold.test_start:
                    purge_reason = None
                else:
                    purge_reason = self.VALIDATION_OVERLAP_REASON
            else:
                purge_reason = (
                    None if label_end is not None else self.RIGHT_CENSORED_REASON
                )

            observations.append(
                ForwardObservation(
                    fold_id=fold.fold_id,
                    partition=partition,
                    symbol=symbol,
                    feature_date=feature_date,
                    label_start_date=label_start,
                    label_end_date=label_end,
                    purge_reason=purge_reason,
                )
            )
        return tuple(observations)


def generate_fold_schedule(
    config: FoldScheduleConfig,
    configured_start: date,
    configured_end: date,
) -> FoldSchedule:
    """Generate deterministic folds from unmodified calendar-date boundaries."""

    if not isinstance(config, FoldScheduleConfig):
        raise TypeError("config must be FoldScheduleConfig")
    _require_date("configured_start", configured_start)
    _require_date("configured_end", configured_end)
    if configured_start >= configured_end:
        raise FoldValidationError("configured_start must be before configured_end")

    folds: list[WalkForwardFold] = []
    offset_months = 0
    while True:
        train_start = _add_calendar_months(configured_start, offset_months)
        train_end = _add_calendar_months(
            configured_start, offset_months + config.train_months
        )
        validation_start = train_end
        validation_end = _add_calendar_months(
            configured_start,
            offset_months + config.train_months + config.validation_months,
        )
        test_start = validation_end
        test_end = _add_calendar_months(
            configured_start,
            offset_months
            + config.train_months
            + config.validation_months
            + config.test_months,
        )
        if test_end > configured_end:
            break
        folds.append(
            WalkForwardFold(
                fold_id=f"fold_{len(folds) + 1:04d}",
                train_start=train_start,
                train_end=train_end,
                validation_start=validation_start,
                validation_end=validation_end,
                test_start=test_start,
                test_end=test_end,
                embargo_sessions=config.embargo_sessions,
            )
        )
        offset_months += config.step_months

    return FoldSchedule(
        config=config,
        configured_start=configured_start,
        configured_end=configured_end,
        folds=tuple(folds),
    )


def resolve_fold_observation_bounds(
    fold: WalkForwardFold,
    symbol: str,
    observed_session_dates: object,
) -> FoldObservationBounds:
    """Resolve actual observations without rounding configured boundaries."""

    if not isinstance(fold, WalkForwardFold):
        raise TypeError("fold must be WalkForwardFold")
    _require_symbol(symbol)
    sessions = _normalize_unique_dates("observed_session_dates", observed_session_dates)
    train = _actual_interval(sessions, fold.train_start, fold.train_end)
    validation = _actual_interval(sessions, fold.validation_start, fold.validation_end)
    test = _actual_interval(sessions, fold.test_start, fold.test_end)
    return FoldObservationBounds(
        fold=fold,
        symbol=symbol,
        train_first_observation_date=train[0],
        train_last_observation_date=train[1],
        train_observation_count=train[2],
        validation_first_observation_date=validation[0],
        validation_last_observation_date=validation[1],
        validation_observation_count=validation[2],
        test_first_observation_date=test[0],
        test_last_observation_date=test[1],
        test_observation_count=test[2],
    )


def _add_calendar_months(value: date, months: int) -> date:
    """Shift an anchored date by calendar months, preserving month-end status."""

    _require_date("value", value)
    _require_non_negative_int("months", months)
    source_month_end = calendar.monthrange(value.year, value.month)[1]
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    target_month_end = calendar.monthrange(year, month)[1]
    day = (
        target_month_end
        if value.day == source_month_end
        else min(value.day, target_month_end)
    )
    return date(year, month, day)


def _normalize_unique_dates(name: str, values: object) -> tuple[date, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be an iterable of date values")
    try:
        normalized = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(f"{name} must be an iterable of date values") from error
    for value in normalized:
        _require_date(name, value)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must not contain duplicate sessions")
    return tuple(sorted(normalized))


def _actual_interval(
    sessions: tuple[date, ...], start: date, end: date
) -> tuple[date | None, date | None, int]:
    actual = tuple(value for value in sessions if start <= value < end)
    if not actual:
        return None, None, 0
    return actual[0], actual[-1], len(actual)


def _validate_actual_bounds(
    name: str,
    first: date | None,
    last: date | None,
    count: int,
    configured_start: date,
    configured_end: date,
) -> None:
    _require_non_negative_int(f"{name} observation count", count)
    if count == 0:
        if first is not None or last is not None:
            raise ValueError(f"empty {name} bounds must use None dates")
        return
    if first is None or last is None:
        raise ValueError(f"non-empty {name} bounds require first and last dates")
    _require_date(f"{name} first observation", first)
    _require_date(f"{name} last observation", last)
    if not configured_start <= first <= last < configured_end:
        raise ValueError(f"actual {name} observations must remain in [start, end)")


def _require_symbol(symbol: object) -> None:
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")


def _require_date(name: str, value: object) -> None:
    if type(value) is not date:
        raise TypeError(f"{name} must be datetime.date without a time component")


def _require_positive_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
