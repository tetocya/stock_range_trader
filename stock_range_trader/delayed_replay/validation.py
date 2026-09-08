"""Strict validation shared only by the new delayed replay contracts."""

from datetime import date, datetime
from math import isfinite


class ReplayContractError(ValueError):
    """Invalid or incomplete delayed replay contract."""


def text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReplayContractError(f"{name} must be a non-empty string")


def number(value: object, name: str, *, minimum: float, maximum: float) -> None:
    if (
        type(value) not in (int, float)
        or not isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ReplayContractError(f"{name} must be finite in [{minimum}, {maximum}]")


def positive_int(value: object, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ReplayContractError(f"{name} must be a positive integer")


def calendar_date(value: object, name: str) -> None:
    if type(value) is not date:
        raise ReplayContractError(f"{name} must be a calendar date, not a timestamp")


def timestamp(value: object, name: str) -> None:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ReplayContractError(f"{name} must be a timezone-aware datetime")
