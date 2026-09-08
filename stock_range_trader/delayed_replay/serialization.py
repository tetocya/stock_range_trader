"""Audit JSON v1: UTF-8, sorted keys, exact integers, no floats or pickle."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from .audit_errors import InvalidEvent


def _validate(value: object) -> None:
    if value is None or type(value) in (str, bool):
        return
    if type(value) is int and -(2**63) <= value < 2**63:
        return
    if type(value) is list:
        for child in value:
            _validate(child)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            _validate(child)
        return
    raise InvalidEvent("unsupported_json_type")


def canonical_json(value: dict) -> str:
    if type(value) is not dict:
        raise InvalidEvent("json_object_required")
    try:
        _validate(value)
        result = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        result.encode("utf-8")
        return result
    except (RecursionError, UnicodeError):
        raise InvalidEvent("invalid_json_encoding") from None


def parse_json(encoded: str) -> dict:
    def pairs(items: list) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidEvent("duplicate_json_key")
            result[key] = value
        return result

    if type(encoded) is not str:
        raise InvalidEvent("json_text_required")
    try:
        value = json.loads(encoded, object_pairs_hook=pairs)
        if canonical_json(value) != encoded:
            raise InvalidEvent("noncanonical_json")
        return value
    except (ValueError, TypeError, RecursionError):
        raise InvalidEvent("invalid_canonical_json") from None


def digest(value: dict) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_hash(value: object) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise InvalidEvent("invalid_sha256")


def require_text(value: object) -> None:
    if type(value) is not str or not value.strip():
        raise InvalidEvent("invalid_text")


def require_fields(value: object, model: type) -> None:
    if type(value) is not dict or set(value) != set(model.__dataclass_fields__):
        raise InvalidEvent("record_fields_mismatch")


def require_sequence(value: object, *, zero: bool = False) -> None:
    if type(value) is not int or not (0 if zero else 1) <= value < 2**63:
        raise InvalidEvent("invalid_sequence")


def time_text(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidEvent("aware_timestamp_required")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if time_text(parsed) != value:
            raise InvalidEvent("noncanonical_timestamp")
        return parsed
    except (ValueError, TypeError):
        raise InvalidEvent("invalid_timestamp") from None


def decimal_text(value: str) -> str:
    """Explicit exact-decimal helper for future typed fields, not float coercion."""
    if type(value) is not str or len(value) > 128:
        raise InvalidEvent("decimal_string_required")
    try:
        parsed = Decimal(value)
        if not parsed.is_finite() or abs(parsed.as_tuple().exponent) > 128:
            raise InvalidEvent("invalid_decimal")
        normalized = format(parsed, "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        return "0" if parsed == 0 else normalized
    except InvalidOperation:
        raise InvalidEvent("invalid_decimal") from None


@dataclass(frozen=True, slots=True)
class JsonObject:
    """Only canonical text is retained; every export returns a detached tree."""

    encoded: str

    def __post_init__(self) -> None:
        parse_json(self.encoded)

    @classmethod
    def from_value(cls, value: dict) -> "JsonObject":
        return cls(canonical_json(value))

    def to_dict(self) -> dict:
        return parse_json(self.encoded)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.encoded.encode("utf-8")).hexdigest()
