"""Serializable cache request and manifest models."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True, slots=True)
class CacheRequest:
    """Fields that uniquely identify one provider request."""

    provider: str
    dataset: str
    symbols: tuple[str, ...] = ()
    requested_start: date | None = None
    requested_end: date | None = None
    adjustment_mode: str = ""
    universe_as_of_date: date | None = None

    def __post_init__(self) -> None:
        if not self.provider or not self.dataset:
            raise ValueError("cache provider and dataset must not be empty")
        if (
            self.requested_start is not None
            and self.requested_end is not None
            and self.requested_start >= self.requested_end
        ):
            raise ValueError("cache requested_start must be before requested_end")
        if tuple(sorted(set(self.symbols))) != self.symbols:
            raise ValueError("cache symbols must be unique and sorted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "dataset": self.dataset,
            "symbols": list(self.symbols),
            "requested_start": _date_text(self.requested_start),
            "requested_end": _date_text(self.requested_end),
            "adjustment_mode": self.adjustment_mode,
            "universe_as_of_date": _date_text(self.universe_as_of_date),
        }

    @property
    def key(self) -> str:
        payload = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DataManifest:
    """Audit metadata for one immutable Parquet cache object."""

    provider: str
    endpoint: str
    dataset: str
    symbols: list[str]
    requested_start: str | None
    requested_end: str | None
    actual_start: str | None
    actual_end: str | None
    fetched_at_utc: str
    schema_version: str
    adjustment_mode: str
    library_version: str
    row_count: int
    content_hash: str
    request_key: str
    data_file: str
    columns: list[str]
    provider_price_basis: str = ""
    universe_as_of_date: str | None = None
    status_counts: dict[str, int] = field(default_factory=dict)
    issues: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: object) -> DataManifest:
        if not isinstance(values, dict):
            raise ValueError("cache manifest must be a JSON object")
        try:
            return cls(**values)
        except TypeError as error:
            raise ValueError(f"invalid cache manifest: {error}") from error


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
