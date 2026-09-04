"""Metadata-complete, atomic Phase 2 report writers."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import pandas as pd


@dataclass(frozen=True, slots=True)
class Phase2RunMetadata:
    """Provenance fields attached to every derived Phase 2 output."""

    provider: str
    requested_start: date | None
    requested_end: date | None
    actual_start: date | None
    actual_end: date | None
    adjustment_mode: str
    universe_as_of_date: date
    analysis_design: str = "exploratory_in_sample"

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.adjustment_mode.strip():
            raise ValueError("provider and adjustment_mode must not be empty")
        if (
            self.requested_start is not None
            and self.requested_end is not None
            and self.requested_start >= self.requested_end
        ):
            raise ValueError("requested_start must be before exclusive requested_end")
        if (
            self.actual_start is not None
            and self.actual_end is not None
            and self.actual_start > self.actual_end
        ):
            raise ValueError("actual_start must not be after actual_end")

    def to_dict(self) -> dict[str, str | None]:
        return {
            key: value.isoformat() if isinstance(value, date) else value
            for key, value in asdict(self).items()
        }


def annotate_phase2_output(
    frame: pd.DataFrame, metadata: Phase2RunMetadata
) -> pd.DataFrame:
    """Return a copy with stable provider/range/bias provenance columns."""

    result = frame.copy()
    for column, value in metadata.to_dict().items():
        if column in result:
            observed = set(result[column].dropna().astype(str))
            if observed and observed != {str(value)}:
                raise ValueError(f"output {column} conflicts with Phase 2 run metadata")
        else:
            result[column] = value
    return result


def write_phase2_csv(
    frame: pd.DataFrame, path: str | Path, metadata: Phase2RunMetadata
) -> Path:
    """Atomically write one annotated derived CSV."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    annotated = annotate_phase2_output(frame, metadata)
    handle, raw_temp = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(handle)
    temp = Path(raw_temp)
    try:
        annotated.to_csv(temp, index=False)
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    return output


def write_run_manifest(metadata: Phase2RunMetadata, path: str | Path) -> Path:
    """Atomically write human-auditable run provenance JSON."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, raw_temp = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(handle)
    temp = Path(raw_temp)
    try:
        temp.write_text(
            json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    return output
