"""Immutable daily observations and pinned snapshots with causal visibility.

No provider calls, automatic latest-version selection, price restoration or
execution authorization. Timestamps are supplied evidence, not proof that the
provider delivered these same values contemporaneously with the market.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from data.price_policy import provider_price_basis

from .clock import ReplayClock
from .validation import (
    ReplayContractError,
    calendar_date,
    number,
    text,
    timestamp,
)


@dataclass(frozen=True, slots=True)
class PriceObservation:
    """One full daily bar; availability is explicit, never inferred from date.

    Both lanes use (open, high, low, close, volume). A daily bar becomes visible
    only after its supplied market_available_at (normally after session close).
    This type deliberately offers no next-open execution interface in Stage 1.
    """

    symbol: str
    session_date: date
    market_available_at: datetime
    raw_ohlcv: tuple[float, float, float, float, float]
    adjusted_ohlcv: tuple[float, float, float, float, float]
    adjustment_factor: float
    stock_split: float
    dividend: float

    def __post_init__(self) -> None:
        text(self.symbol, "symbol")
        calendar_date(self.session_date, "session_date")
        timestamp(self.market_available_at, "market_available_at")
        if (
            self.market_available_at.astimezone(ZoneInfo("Asia/Tokyo")).date()
            < self.session_date
        ):
            raise ReplayContractError(
                "bar cannot be available before its market session"
            )
        for name in ("raw_ohlcv", "adjusted_ohlcv"):
            values = getattr(self, name)
            if type(values) is not tuple or len(values) != 5:
                raise ReplayContractError(f"{name} must be an immutable OHLCV tuple")
            for value in values:
                number(value, name, minimum=0, maximum=1e30)
            opening, high, low, close, _ = values
            if (
                min(opening, high, low, close) <= 0
                or not low <= min(opening, close) <= max(opening, close) <= high
            ):
                raise ReplayContractError(f"{name} contains invalid OHLC")
        number(self.adjustment_factor, "adjustment_factor", minimum=0, maximum=1e30)
        if self.adjustment_factor == 0:
            raise ReplayContractError("adjustment_factor must be positive")
        number(self.stock_split, "stock_split", minimum=0, maximum=1e30)
        number(self.dividend, "dividend", minimum=0, maximum=1e30)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError("unsupported snapshot value")


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    provider: str
    provider_price_basis: str
    source_artifact_sha256: str
    data_version: str
    first_observed_at: datetime
    fetched_at: datetime
    provider_published_at: datetime | None
    publication_time_unknown_reason: str | None
    price_basis_evidence_id: str | None
    observations: tuple[PriceObservation, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        if self.provider != "jquants":
            raise ReplayContractError("only jquants snapshots are supported")
        if self.provider_price_basis != provider_price_basis(self.provider):
            raise ReplayContractError("provider_price_basis mismatch")
        text(self.data_version, "data_version")
        for name in ("source_artifact_sha256", "payload_sha256"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ReplayContractError(f"{name} must be a lowercase SHA-256 digest")
        timestamp(self.first_observed_at, "first_observed_at")
        timestamp(self.fetched_at, "fetched_at")
        if self.first_observed_at > self.fetched_at:
            raise ReplayContractError("first_observed_at cannot exceed fetched_at")
        if self.provider_published_at is None:
            text(
                self.publication_time_unknown_reason, "publication_time_unknown_reason"
            )
        else:
            timestamp(self.provider_published_at, "provider_published_at")
            if self.provider_published_at > self.first_observed_at:
                raise ReplayContractError("publication cannot follow first observation")
            if self.publication_time_unknown_reason is not None:
                raise ReplayContractError(
                    "known publication time cannot have an unknown reason"
                )
        if self.price_basis_evidence_id is not None:
            text(self.price_basis_evidence_id, "price_basis_evidence_id")
        if type(self.observations) is not tuple or not self.observations:
            raise ReplayContractError(
                "observations must be a non-empty immutable tuple"
            )
        if any(not isinstance(bar, PriceObservation) for bar in self.observations):
            raise ReplayContractError("observations must contain PriceObservation")
        keys = [(bar.symbol, bar.session_date) for bar in self.observations]
        if len(set(keys)) != len(keys):
            raise ReplayContractError("duplicate symbol/session in snapshot")
        if any(
            bar.market_available_at > self.first_observed_at
            for bar in self.observations
        ):
            raise ReplayContractError("observation precedes market availability")
        object.__setattr__(
            self,
            "observations",
            tuple(
                sorted(
                    self.observations, key=lambda bar: (bar.symbol, bar.session_date)
                )
            ),
        )
        if self.payload_sha256 != self.compute_digest():
            raise ReplayContractError("snapshot payload SHA-256 mismatch")

    def compute_digest(self) -> str:
        payload = asdict(self)
        payload.pop("payload_sha256")
        encoded = json.dumps(
            payload,
            default=_json_default,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    @classmethod
    def create(cls, **values: object) -> "PriceSnapshot":
        """Build the digest, then run the same validation as direct construction."""
        if "payload_sha256" in values:
            raise ReplayContractError(
                "create computes payload_sha256; do not supply it"
            )
        # Build JSON independently; no partially validated instance escapes.
        payload = dict(values)
        bars = payload.get("observations")
        if type(bars) is not tuple or any(
            not isinstance(bar, PriceObservation) for bar in bars
        ):
            raise ReplayContractError(
                "observations must be an immutable PriceObservation tuple"
            )
        payload["observations"] = [
            asdict(bar)
            for bar in sorted(bars, key=lambda bar: (bar.symbol, bar.session_date))
        ]
        encoded = json.dumps(
            payload,
            default=_json_default,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        return cls(**values, payload_sha256=digest)


@dataclass(frozen=True, slots=True)
class ReplayDataView:
    """Caller pins exact snapshots; revisions are never selected automatically.

    Only history() results, not this archive object, may be handed to selection
    or strategy code. Stage 1 does not implement that runner integration.
    """

    snapshots: tuple[PriceSnapshot, ...]

    def __post_init__(self) -> None:
        if type(self.snapshots) is not tuple or any(
            not isinstance(snapshot, PriceSnapshot) for snapshot in self.snapshots
        ):
            raise ReplayContractError(
                "snapshots must be an immutable PriceSnapshot tuple"
            )

    def history(
        self, clock: ReplayClock, *, start: date
    ) -> tuple[PriceObservation, ...]:
        """Return [start, decision) history; no interpolation or future metadata.

        Available means both first observed and fetched by replay wall time.
        Market date AND market availability must strictly precede the decision.
        The date check prevents exposing any current-session OHLCV.
        """
        if not isinstance(clock, ReplayClock):
            raise ReplayContractError("clock must be ReplayClock")
        calendar_date(start, "start")
        decision_date = clock.market_decision_at.astimezone(
            ZoneInfo("Asia/Tokyo")
        ).date()
        if start > decision_date:
            raise ReplayContractError("start cannot follow the market decision date")
        visible: dict[tuple[str, date], PriceObservation] = {}
        for snapshot in self.snapshots:
            if snapshot.fetched_at > clock.replayed_at:
                continue
            # Validate integrity again at the consumption boundary.
            replace(snapshot)
            for bar in snapshot.observations:
                if not (
                    start <= bar.session_date < decision_date
                    and bar.market_available_at < clock.market_decision_at
                ):
                    continue
                key = (bar.symbol, bar.session_date)
                if key in visible:
                    raise ReplayContractError(
                        "overlapping visible snapshots; pin one version explicitly"
                    )
                visible[key] = bar
        return tuple(visible[key] for key in sorted(visible))
