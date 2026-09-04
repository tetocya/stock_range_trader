"""Atomic, content-verified local Parquet cache."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from ..price_policy import provider_price_basis
from .manifest import CacheRequest, DataManifest


class CacheCorruptionError(RuntimeError):
    """Raised when cached data and its manifest no longer agree."""


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A verified cache hit or newly stored object."""

    data: pd.DataFrame
    manifest: DataManifest


@dataclass(slots=True)
class CacheManager:
    """Persist immutable Parquet objects behind atomic JSON manifests."""

    root: Path
    schema_version: str = "2.0"

    def __init__(self, root: str | Path, schema_version: str = "2.0") -> None:
        self.root = Path(root).expanduser()
        self.schema_version = schema_version

    def load(
        self,
        request: CacheRequest,
        *,
        required_columns: Collection[str] = (),
    ) -> CacheEntry | None:
        """Return a verified hit, ``None`` on miss, and raise on corruption."""

        manifest_path = self._manifest_path(request)
        if not manifest_path.exists():
            return None
        try:
            values = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = DataManifest.from_dict(values)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise CacheCorruptionError(
                f"invalid cache manifest: {manifest_path}"
            ) from error
        self._validate_manifest(request, manifest)
        data_path = self._dataset_dir(request) / manifest.data_file
        if not data_path.is_file():
            raise CacheCorruptionError(f"cached Parquet file is missing: {data_path}")
        if _sha256_file(data_path) != manifest.content_hash:
            raise CacheCorruptionError(f"cached Parquet hash mismatch: {data_path}")
        try:
            frame = pd.read_parquet(data_path)
        except Exception as error:
            raise CacheCorruptionError(
                f"cannot read cached Parquet: {data_path}"
            ) from error
        if len(frame) != manifest.row_count or list(frame.columns) != manifest.columns:
            raise CacheCorruptionError(
                "cached row count or columns do not match manifest"
            )
        missing = sorted(set(required_columns) - set(frame.columns))
        if missing:
            raise CacheCorruptionError(
                "cached data is missing required columns: " + ", ".join(missing)
            )
        return CacheEntry(frame, manifest)

    def store(
        self,
        request: CacheRequest,
        frame: pd.DataFrame,
        *,
        endpoint: str,
        library_version: str,
        actual_start: str | None = None,
        actual_end: str | None = None,
        fetched_at_utc: str | None = None,
        status_counts: dict[str, int] | None = None,
        issues: list[dict[str, str]] | None = None,
        notes: list[str] | None = None,
    ) -> CacheEntry:
        """Atomically publish Parquet first and its pointer manifest last."""

        if not isinstance(frame, pd.DataFrame):
            raise TypeError("cache data must be a pandas DataFrame")
        dataset_dir = self._dataset_dir(request)
        manifest_dir = self.root / "manifests"
        dataset_dir.mkdir(parents=True, exist_ok=True)
        manifest_dir.mkdir(parents=True, exist_ok=True)
        temp_data = _temporary_path(dataset_dir, ".parquet")
        temp_manifest = _temporary_path(manifest_dir, ".json")
        try:
            frame.to_parquet(temp_data, index=False)
            content_hash = _sha256_file(temp_data)
            data_file = f"{request.key}-{content_hash[:16]}.parquet"
            final_data = dataset_dir / data_file
            os.replace(temp_data, final_data)
            actual_start, actual_end = _actual_dates(
                frame, actual_start=actual_start, actual_end=actual_end
            )
            manifest = DataManifest(
                provider=request.provider,
                endpoint=endpoint,
                dataset=request.dataset,
                symbols=list(request.symbols),
                requested_start=_iso(request.requested_start),
                requested_end=_iso(request.requested_end),
                actual_start=actual_start,
                actual_end=actual_end,
                fetched_at_utc=fetched_at_utc or datetime.now(UTC).isoformat(),
                schema_version=self.schema_version,
                adjustment_mode=request.adjustment_mode,
                library_version=library_version,
                row_count=len(frame),
                content_hash=content_hash,
                request_key=request.key,
                data_file=data_file,
                columns=list(frame.columns),
                provider_price_basis=provider_price_basis(request.provider),
                universe_as_of_date=_iso(request.universe_as_of_date),
                status_counts=dict(status_counts or {}),
                issues=[dict(issue) for issue in issues or []],
                notes=list(notes or []),
            )
            temp_manifest.write_text(
                json.dumps(
                    manifest.to_dict(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temp_manifest, self._manifest_path(request))
            return CacheEntry(frame.copy(), manifest)
        finally:
            temp_data.unlink(missing_ok=True)
            temp_manifest.unlink(missing_ok=True)

    def get_or_fetch(
        self,
        request: CacheRequest,
        fetcher: Callable[[], pd.DataFrame],
        *,
        endpoint: str,
        library_version: str,
        refresh: bool = False,
        required_columns: Collection[str] = (),
        status_counts: dict[str, int] | None = None,
        issues: list[dict[str, str]] | None = None,
        notes: list[str] | None = None,
    ) -> CacheEntry:
        """Reuse an identical request unless refresh is explicitly requested."""

        if not refresh:
            hit = self.load(request, required_columns=required_columns)
            if hit is not None:
                return hit
        frame = fetcher()
        return self.store(
            request,
            frame,
            endpoint=endpoint,
            library_version=library_version,
            status_counts=status_counts,
            issues=issues,
            notes=notes,
        )

    def _dataset_dir(self, request: CacheRequest) -> Path:
        return self.root / request.provider / request.dataset

    def _manifest_path(self, request: CacheRequest) -> Path:
        return self.root / "manifests" / f"{request.key}.json"

    def _validate_manifest(self, request: CacheRequest, manifest: DataManifest) -> None:
        if manifest.request_key != request.key:
            raise CacheCorruptionError("cache manifest request key mismatch")
        if manifest.schema_version != self.schema_version:
            raise CacheCorruptionError(
                f"cache schema mismatch: {manifest.schema_version} != {self.schema_version}"
            )
        if manifest.provider_price_basis != provider_price_basis(request.provider):
            raise CacheCorruptionError("cache manifest provider_price_basis mismatch")
        expected = request.to_dict()
        for key in (
            "provider",
            "dataset",
            "symbols",
            "requested_start",
            "requested_end",
            "adjustment_mode",
            "universe_as_of_date",
        ):
            if getattr(manifest, key) != expected[key]:
                raise CacheCorruptionError(f"cache manifest {key} mismatch")


def _temporary_path(directory: Path, suffix: str) -> Path:
    handle, raw_path = tempfile.mkstemp(
        prefix=".partial-", suffix=suffix, dir=directory
    )
    os.close(handle)
    return Path(raw_path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso(value: object) -> str | None:
    return value.isoformat() if value is not None else None  # type: ignore[union-attr]


def _actual_dates(
    frame: pd.DataFrame,
    *,
    actual_start: str | None,
    actual_end: str | None,
) -> tuple[str | None, str | None]:
    if actual_start is not None or actual_end is not None or frame.empty:
        return actual_start, actual_end
    for column in ("date", "as_of_date", "Date"):
        if column in frame:
            dates = pd.to_datetime(frame[column], errors="coerce").dropna()
            if not dates.empty:
                return dates.min().date().isoformat(), dates.max().date().isoformat()
    return None, None
