"""Explicit synthetic replay choices; no operational parameter defaults."""

import math
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from enum import Enum

from .account_policy import D
from .serialization import JsonObject, time_text
from .validation import ReplayContractError, calendar_date


def audit_value(value):
    """Lossless deterministic JSON representation (audit JSON forbids floats)."""
    if is_dataclass(value):
        return audit_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat()
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        return time_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReplayContractError("non_finite_audit_value")
        return repr(value)
    if isinstance(value, dict):
        return {key: audit_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [audit_value(item) for item in value]
    return value


def fingerprint(value):
    return JsonObject.from_value({"value": audit_value(value)}).sha256


@dataclass(frozen=True, slots=True)
class ReplayPolicy:
    purpose: str
    run_start: date
    run_end: date
    lookback_months: int
    warmup_months: int
    minimum_warmup_sessions: int
    selection_mode: str
    no_candidate_mode: str
    proceeds_mode: str
    missing_input_mode: str
    corporate_action_mode: str
    maximum_drawdown: str

    def __post_init__(self):
        for name in ("run_start", "run_end"):
            calendar_date(getattr(self, name), name)
        if self.run_start.day != 1 or self.run_start >= self.run_end:
            raise ReplayContractError("run_requires_positive_month_boundary_start")
        for name in ("lookback_months", "warmup_months", "minimum_warmup_sessions"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ReplayContractError(f"{name}_must_be_positive_integer")
        supported = {
            "purpose": "synthetic_test",
            "selection_mode": "independent_symbol_validation",
            "no_candidate_mode": "disable_entries_keep_positions_and_pending",
            "proceeds_mode": "release_after_sale_open_at_complete_close",
            "missing_input_mode": "wait_without_expiry",
            "corporate_action_mode": "stop_preserve_positions",
        }
        for name, expected in supported.items():
            if getattr(self, name) != expected:
                raise ReplayContractError(f"unsupported_{name}")
        if not 0 < D(self.maximum_drawdown) < 1:
            raise ReplayContractError("invalid_maximum_drawdown")

    @property
    def sha256(self):
        return fingerprint(self)
