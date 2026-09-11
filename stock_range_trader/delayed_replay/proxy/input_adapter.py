"""Versioned artificial-only lane, immutable files before ledger acceptance."""

import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from delayed_replay.account_policy import D, M
from delayed_replay.serialization import (
    JsonObject,
    digest,
    parse_json,
    parse_time,
    require_hash,
    require_text,
)
from delayed_replay.validation import ReplayContractError


@dataclass(frozen=True)
class SyntheticRecipe:
    fixture_id: str
    sessions: tuple[str, ...]
    symbols: tuple[str, ...]
    base: str
    amplitude: str
    volume: str
    acquired_at: str
    overrides: JsonObject

    def __post_init__(self):
        require_text(self.fixture_id)
        if (
            type(self.sessions) is not tuple
            or not self.sessions
            or tuple(sorted(set(self.sessions))) != self.sessions
        ):
            raise ReplayContractError("proxy_explicit_unique_sessions")
        for day in self.sessions:
            if date.fromisoformat(day).isoformat() != day:
                raise ReplayContractError("proxy_date")
        if (
            type(self.symbols) is not tuple
            or not self.symbols
            or len(set(self.symbols)) != len(self.symbols)
            or any(s not in ("TEST_A", "TEST_B") for s in self.symbols)
        ):
            raise ReplayContractError("proxy_fixture_symbols_only")
        if (
            D(self.amplitude) <= 0
            or D(self.base) <= 4 * D(self.amplitude)
            or D(self.volume) <= 0
        ):
            raise ReplayContractError("proxy_fixture_generation_parameters")
        if parse_time(self.acquired_at).date() <= date.fromisoformat(self.sessions[-1]):
            raise ReplayContractError("proxy_fixture_acquisition")
        if type(self.overrides) is not JsonObject:
            raise ReplayContractError("proxy_fixture_overrides")

    @property
    def origin(self):
        d = asdict(self)
        d.pop("overrides")
        # Acquisition is packet metadata, not the identity of the fixed
        # artificial calendar/generator. Later acquisitions remain a lineage.
        d.pop("acquired_at")
        d["sessions"], d["symbols"] = list(self.sessions), list(self.symbols)
        return dict(generator="proxy-fixture-generator-v1", recipe_identity=digest(d))

    def records(self):
        changes = self.overrides.to_dict()
        result = {}
        for i, day in enumerate(self.sessions):
            for symbol in self.symbols:
                price = D(self.base) + ((i * 3) % 5 - 2) * D(self.amplitude)
                row = dict(
                    symbol=symbol,
                    session=day,
                    open=M(price),
                    high=M(price + D(self.amplitude)),
                    low=M(price - D(self.amplitude)),
                    close=M(price),
                    volume=self.volume,
                    adjustment_factor="1",
                    halt="unknown",
                    volume_unit="execution_shares",
                    actual_trade_at=None,
                )
                key = symbol + "|" + day
                if key in changes:
                    patch = changes[key]
                    if (
                        type(patch) is not dict
                        or set(patch) - set(row)
                        or {"symbol", "session", "actual_trade_at"} & set(patch)
                    ):
                        raise ReplayContractError("proxy_fixture_override_fields")
                    row.update(patch)
                validate_row(row)
                result[key] = row
        if set(changes) - set(result):
            raise ReplayContractError("proxy_fixture_override_scope")
        return result


def validate_row(row):
    if set(row) != {
        "symbol",
        "session",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "adjustment_factor",
        "halt",
        "volume_unit",
        "actual_trade_at",
    }:
        raise ReplayContractError("proxy_observation_schema")
    if row["actual_trade_at"] is not None or row["halt"] not in (
        "unknown",
        "full",
        "temporary",
        "delayed_first_trade",
    ):
        raise ReplayContractError("proxy_observation_halt_or_time")
    if row["volume_unit"] != "execution_shares":
        raise ReplayContractError("proxy_execution_volume_unit_required")
    for k in ("open", "high", "low", "close", "volume", "adjustment_factor"):
        if row[k] is not None and D(row[k]) < 0:
            raise ReplayContractError("proxy_negative_observation")


@dataclass(frozen=True)
class ProxyPacket:
    payload: JsonObject

    def __post_init__(self):
        p = self.payload.to_dict()
        if (
            set(p) != {"schema", "recipe", "origin", "records"}
            or p["schema"] != "proxy-input-lane-v1"
        ):
            raise ReplayContractError("proxy_input_lane_required")
        r = dict(p["recipe"])
        r["sessions"], r["symbols"] = tuple(r["sessions"]), tuple(r["symbols"])
        r["overrides"] = JsonObject.from_value(r["overrides"])
        recipe = SyntheticRecipe(**r)
        expected = recipe.records()
        if (
            p["origin"] != recipe.origin
            or not p["records"]
            or any(expected.get(k) != v for k, v in p["records"].items())
        ):
            raise ReplayContractError("proxy_generator_snapshot_mismatch")

    @classmethod
    def generate(cls, recipe, keys):
        if type(recipe) is not SyntheticRecipe:
            raise ReplayContractError("proxy_generated_recipe_required")
        records = recipe.records()
        r = dict(
            fixture_id=recipe.fixture_id,
            sessions=list(recipe.sessions),
            symbols=list(recipe.symbols),
            base=recipe.base,
            amplitude=recipe.amplitude,
            volume=recipe.volume,
            acquired_at=recipe.acquired_at,
            overrides=recipe.overrides.to_dict(),
        )
        return cls(
            JsonObject.from_value(
                dict(
                    schema="proxy-input-lane-v1",
                    recipe=r,
                    origin=recipe.origin,
                    records={k: records[k] for k in keys},
                )
            )
        )


class ProxyInputStore:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, sha):
        require_hash(sha)
        return self.root / (sha + ".json")

    def load(self, sha):
        path = self.path(sha)
        if path.is_symlink():
            raise ReplayContractError("proxy_input_symlink")
        try:
            p = JsonObject.from_value(parse_json(path.read_text()))
            if p.sha256 != sha:
                raise ReplayContractError("proxy_input_hash_mismatch")
            return ProxyPacket(p)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ReplayContractError("proxy_input_corruption") from exc

    def publish(self, packet, *, fault=None):
        if type(packet) is not ProxyPacket:
            raise ReplayContractError("proxy_not_a_generated_packet")
        ProxyPacket(packet.payload)
        self.root.mkdir(parents=True, exist_ok=True)
        if fault:
            fault("before_file_write")
        fd, name = tempfile.mkstemp(prefix=".proxy-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(packet.payload.encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if fault:
                fault("before_file_publish")
            try:
                os.link(name, self.path(packet.payload.sha256))
            except FileExistsError:
                self.load(packet.payload.sha256)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if fault:
                fault("after_file_publish")
        finally:
            Path(name).unlink(missing_ok=True)
        self.load(packet.payload.sha256)
        return packet.payload.sha256
