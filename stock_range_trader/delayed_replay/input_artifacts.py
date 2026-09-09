"""Content-addressed immutable input files, published BEFORE DB acceptance."""

import os
import tempfile
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from .execution import ExecutionOpenEvidence
from .market_view import MarketView, OpenSnapshot
from .replay_policy import audit_value, fingerprint
from .serialization import JsonObject, parse_json, parse_time, require_hash
from .snapshot import PriceObservation, PriceSnapshot
from .validation import ReplayContractError


@dataclass(frozen=True, slots=True)
class InputPacket:
    market: MarketView

    def __post_init__(self):
        if not isinstance(self.market, MarketView) or not (
            self.market.snapshots or self.market.open_snapshots
        ):
            raise ReplayContractError("nonempty_typed_input_packet_required")
        for s in self.market.snapshots + self.market.open_snapshots:
            replace(s)

    @property
    def payload(self):
        return JsonObject.from_value(
            dict(schema="input-packet-7a-1", market=audit_value(self.market))
        )

    @classmethod
    def from_payload(cls, payload):
        data = payload.to_dict()
        if (
            set(data) != {"schema", "market"}
            or data["schema"] != "input-packet-7a-1"
            or set(data["market"]) != {"snapshots", "open_snapshots"}
        ):
            raise ReplayContractError("input_packet_schema")
        prices, opens = [], []
        for data in payload.to_dict()["market"]["snapshots"]:
            data = dict(data)
            bars = []
            for row in data["observations"]:
                row = dict(row)
                row["session_date"] = date.fromisoformat(row["session_date"])
                row["market_available_at"] = parse_time(row["market_available_at"])
                for key in ("raw_ohlcv", "adjusted_ohlcv"):
                    row[key] = tuple(
                        float(v) if type(v) is str else v for v in row[key]
                    )
                for key in ("adjustment_factor", "stock_split", "dividend"):
                    if type(row[key]) is str:
                        row[key] = float(row[key])
                bars.append(PriceObservation(**row))
            data["observations"] = tuple(bars)
            for key in ("first_observed_at", "fetched_at", "provider_published_at"):
                if data[key] is not None:
                    data[key] = parse_time(data[key])
            prices.append(PriceSnapshot(**data))
        for data in payload.to_dict()["market"]["open_snapshots"]:
            data = dict(data)
            data["evidence"] = tuple(
                ExecutionOpenEvidence(**r) for r in data["evidence"]
            )
            for key in ("first_observed_at", "fetched_at", "provider_published_at"):
                if data[key] is not None:
                    data[key] = parse_time(data[key])
            opens.append(OpenSnapshot(**data))
        packet = cls(MarketView(tuple(prices), tuple(opens)))
        if packet.payload != payload:
            raise ReplayContractError("noncanonical_input_packet")
        return packet

    def rows(self):
        from .daily_evidence import numeric

        result = {}
        for snapshot in self.market.snapshots:
            for bar in snapshot.observations:
                row = audit_value(bar)
                for key in ("raw_ohlcv", "adjusted_ohlcv"):
                    row[key] = [numeric(v) for v in row[key]]
                for key in ("adjustment_factor", "stock_split", "dividend"):
                    row[key] = numeric(row[key])
                result[f"price|{bar.symbol}|{bar.session_date}"] = fingerprint(row)
        for snapshot in self.market.open_snapshots:
            for row in snapshot.evidence:
                data = audit_value(row)
                data.pop("snapshot_hash")  # Value identity, not acquisition provenance.
                result[f"open|{row.instrument_id}|{row.session}"] = fingerprint(data)
        return result


class InputArtifactStore:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, sha):
        require_hash(sha)
        return self.root / (sha + ".json")

    def load(self, sha):
        path = self.path(sha)
        try:
            if path.is_symlink():
                raise ReplayContractError("input_symlink_not_allowed")
            body = path.read_text(encoding="utf-8")
            payload = JsonObject.from_value(parse_json(body))
            if payload.sha256 != sha:
                raise ReplayContractError("input_file_hash_mismatch")
            return InputPacket.from_payload(payload)
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
            raise ReplayContractError("input_file_missing_or_corrupt") from error

    def publish(self, packet, *, fault=None):
        if not isinstance(packet, InputPacket):
            raise ReplayContractError("typed_input_packet_required")
        payload = packet.payload
        sha = payload.sha256
        self.root.mkdir(parents=True, exist_ok=True)
        if fault:
            fault("before_file_write")
        fd, name = tempfile.mkstemp(prefix=".input-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload.encoded.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            if fault:
                fault("before_file_publish")
            try:
                os.link(name, self.path(sha))
            except FileExistsError:
                if self.load(sha).payload != payload:
                    raise ReplayContractError("input_file_collision") from None
            # Persist directory entry before a DB can reference it (POSIX).
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if fault:
                fault("after_file_publish")
        finally:
            Path(name).unlink(missing_ok=True)
        self.load(sha)
        return sha
