"""Phase 2 content-addressed cache tests."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest
from phase2_helpers import canonical_bars

from data import CANONICAL_COLUMNS
from data.cache import CacheCorruptionError, CacheManager, CacheRequest


def _request() -> CacheRequest:
    return CacheRequest(
        provider="yfinance",
        dataset="daily",
        symbols=("7203.T",),
        requested_start=date(2026, 1, 1),
        requested_end=date(2026, 9, 1),
        adjustment_mode="adj_close_ratio_for_ohlc_raw_volume",
        universe_as_of_date=date(2026, 8, 31),
    )


def test_cache_round_trip_and_manifest_audit_fields(tmp_path) -> None:
    manager = CacheManager(tmp_path)
    request = _request()
    bars = canonical_bars(periods=3)

    stored = manager.store(
        request,
        bars,
        endpoint="yfinance.download",
        library_version="test",
        status_counts={"ok": 1},
        issues=[
            {
                "symbol": "9984.T",
                "status": "empty_response",
                "message": "mock empty",
            }
        ],
        notes=["end is exclusive"],
    )
    loaded = manager.load(request, required_columns=CANONICAL_COLUMNS)

    assert loaded is not None
    pd.testing.assert_frame_equal(loaded.data, stored.data)
    assert stored.manifest.actual_start == "2026-08-27"
    assert stored.manifest.actual_end == "2026-08-31"
    assert stored.manifest.row_count == 3
    assert stored.manifest.content_hash
    assert stored.manifest.status_counts == {"ok": 1}
    assert stored.manifest.issues == [
        {
            "symbol": "9984.T",
            "status": "empty_response",
            "message": "mock empty",
        }
    ]
    assert stored.manifest.notes == ["end is exclusive"]


def test_identical_request_reuses_cache_without_fetching(tmp_path) -> None:
    manager = CacheManager(tmp_path)
    request = _request()
    bars = canonical_bars(periods=3)
    calls = 0

    def fetcher() -> pd.DataFrame:
        nonlocal calls
        calls += 1
        return bars

    first = manager.get_or_fetch(
        request,
        fetcher,
        endpoint="yfinance.download",
        library_version="test",
    )
    second = manager.get_or_fetch(
        request,
        fetcher,
        endpoint="yfinance.download",
        library_version="test",
    )

    assert calls == 1
    assert first.manifest.content_hash == second.manifest.content_hash


def test_cache_detects_content_corruption(tmp_path) -> None:
    manager = CacheManager(tmp_path)
    request = _request()
    stored = manager.store(
        request,
        canonical_bars(periods=3),
        endpoint="yfinance.download",
        library_version="test",
    )
    data_path = tmp_path / "yfinance" / "daily" / stored.manifest.data_file
    data_path.write_bytes(data_path.read_bytes() + b"corrupt")

    with pytest.raises(CacheCorruptionError, match="hash mismatch"):
        manager.load(request)


def test_cache_detects_schema_and_manifest_corruption(tmp_path) -> None:
    manager = CacheManager(tmp_path)
    request = _request()
    manager.store(
        request,
        canonical_bars(periods=3),
        endpoint="yfinance.download",
        library_version="test",
    )

    with pytest.raises(CacheCorruptionError, match="schema mismatch"):
        CacheManager(tmp_path, schema_version="3.0").load(request)

    manifest_path = tmp_path / "manifests" / f"{request.key}.json"
    values = json.loads(manifest_path.read_text(encoding="utf-8"))
    values["symbols"] = ["9984.T"]
    manifest_path.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(CacheCorruptionError, match="symbols mismatch"):
        manager.load(request)


def test_failed_refresh_leaves_completed_cache_readable(tmp_path, monkeypatch) -> None:
    manager = CacheManager(tmp_path)
    request = _request()
    original = canonical_bars(periods=3)
    manager.store(
        request,
        original,
        endpoint="yfinance.download",
        library_version="test",
    )

    def fail_to_parquet(*args, **kwargs) -> None:
        del args, kwargs
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", fail_to_parquet)
    with pytest.raises(OSError, match="interrupted"):
        manager.store(
            request,
            canonical_bars(periods=4),
            endpoint="yfinance.download",
            library_version="test",
        )
    monkeypatch.undo()

    loaded = manager.load(request)
    assert loaded is not None
    pd.testing.assert_frame_equal(loaded.data, original)
