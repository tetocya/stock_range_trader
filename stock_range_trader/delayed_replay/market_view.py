"""Pinned synthetic archive and detached phase-specific publication views."""

from dataclasses import dataclass, replace
from datetime import date, datetime

import pandas as pd

from data.canonical import CANONICAL_COLUMNS

from .clock import ReplayClock
from .execution import ExecutionOpenEvidence
from .replay_policy import fingerprint
from .serialization import parse_time
from .snapshot import PriceSnapshot
from .validation import ReplayContractError, timestamp


class WaitingForInput(ReplayContractError):
    """No phase effect may be committed until required pinned input is visible."""


@dataclass(frozen=True, slots=True)
class OpenSnapshot:
    evidence: tuple[ExecutionOpenEvidence, ...]
    first_observed_at: datetime
    fetched_at: datetime
    version: str
    provider_published_at: datetime | None
    publication_time_unknown_reason: str | None

    def __post_init__(self):
        for name in ("first_observed_at", "fetched_at"):
            timestamp(getattr(self, name), name)
        if self.first_observed_at > self.fetched_at:
            raise ReplayContractError("invalid_open_acquisition_order")
        if not isinstance(self.version, str) or not self.version:
            raise ReplayContractError("open_version_required")
        if self.provider_published_at is None:
            if not self.publication_time_unknown_reason:
                raise ReplayContractError("publication_unknown_reason_required")
        else:
            timestamp(self.provider_published_at, "provider_published_at")
            if (
                self.provider_published_at > self.first_observed_at
                or self.publication_time_unknown_reason is not None
            ):
                raise ReplayContractError("invalid_open_publication_time")
        if type(self.evidence) is not tuple or any(
            not isinstance(item, ExecutionOpenEvidence) for item in self.evidence
        ):
            raise ReplayContractError("immutable_open_evidence_required")
        if any(
            parse_time(item.market_available_at) > self.first_observed_at
            for item in self.evidence
        ):
            raise ReplayContractError("open_observed_before_available")
        ordered = tuple(
            sorted(self.evidence, key=lambda item: (item.session, item.instrument_id))
        )
        if len({(item.session, item.instrument_id) for item in ordered}) != len(
            ordered
        ):
            raise ReplayContractError("duplicate_open_evidence")
        object.__setattr__(self, "evidence", ordered)

    @property
    def sha256(self):
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class MarketView:
    snapshots: tuple[PriceSnapshot, ...]
    open_snapshots: tuple[OpenSnapshot, ...]

    def __post_init__(self):
        for name, cls in (
            ("snapshots", PriceSnapshot),
            ("open_snapshots", OpenSnapshot),
        ):
            values = getattr(self, name)
            if type(values) is not tuple or any(
                not isinstance(item, cls) for item in values
            ):
                raise ReplayContractError("invalid_pinned_archive")
        # Overlap is a run-input contract error, never automatic latest-version selection.
        keys = [
            (bar.symbol, bar.session_date)
            for s in self.snapshots
            for bar in s.observations
        ]
        opens = [
            (e.instrument_id, e.session)
            for s in self.open_snapshots
            for e in s.evidence
        ]
        if len(keys) != len(set(keys)) or len(opens) != len(set(opens)):
            raise ReplayContractError("overlapping_pinned_snapshots")

    @property
    def identity(self):
        return {
            "prices": sorted(s.payload_sha256 for s in self.snapshots),
            "opens": sorted(s.sha256 for s in self.open_snapshots),
        }

    @property
    def references(self):
        return tuple(
            sorted(
                set(
                    self.identity["prices"]
                    + self.identity["opens"]
                    + [e.snapshot_hash for s in self.open_snapshots for e in s.evidence]
                )
            )
        )

    def observations(self, clock: ReplayClock, start: date, end: date, symbols):
        result = []
        for snapshot in self.snapshots:
            replace(snapshot)  # Recheck content hash at the consumption boundary.
            for bar in snapshot.observations:
                if not (bar.symbol in symbols and start <= bar.session_date < end):
                    continue
                if bar.market_available_at > clock.market_decision_at:
                    continue
                if snapshot.fetched_at > clock.replayed_at:
                    raise WaitingForInput("price_snapshot_not_yet_fetched")
                result.append((bar, snapshot))
        return tuple(
            sorted(result, key=lambda item: (item[0].symbol, item[0].session_date))
        )

    def history(self, clock, start, end, symbols):
        """Includes a current bar only at/after its explicit Close publication."""
        rows = []
        for bar, snapshot in self.observations(clock, start, end, symbols):
            row = dict(
                date=pd.Timestamp(bar.session_date),
                symbol=bar.symbol,
                provider=snapshot.provider,
                fetched_at=pd.Timestamp(snapshot.fetched_at),
                adjustment_factor=bar.adjustment_factor,
                stock_split=bar.stock_split,
                dividend=bar.dividend,
                turnover_value=bar.raw_ohlcv[3] * bar.raw_ohlcv[4],
            )
            for prefix, values in (
                ("raw", bar.raw_ohlcv),
                ("adjusted", bar.adjusted_ohlcv),
            ):
                row.update(
                    {
                        f"{prefix}_{key}": value
                        for key, value in zip(
                            ("open", "high", "low", "close", "volume"),
                            values,
                            strict=True,
                        )
                    }
                )
            rows.append(row)
        frame = pd.DataFrame(rows, columns=CANONICAL_COLUMNS)
        frame["date"] = pd.to_datetime(frame["date"])
        frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], utc=True)
        return frame

    def opens(self, clock, session, symbols):
        result = {}
        for snapshot in self.open_snapshots:
            for evidence in snapshot.evidence:
                if evidence.session != session or evidence.instrument_id not in symbols:
                    continue
                if (
                    snapshot.fetched_at > clock.replayed_at
                    or parse_time(evidence.market_available_at)
                    > clock.market_decision_at
                ):
                    raise WaitingForInput("open_snapshot_not_available")
                result[evidence.instrument_id] = evidence
        if set(result) != set(symbols):
            raise WaitingForInput("missing_explicit_open_evidence")
        return tuple(result[key] for key in sorted(result))
