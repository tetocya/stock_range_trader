"""Read-only Stage7B evidence checks and detached raw/adjusted proxy lanes."""

from dataclasses import dataclass
from datetime import date, timedelta

from data.price_policy import provider_price_basis
from delayed_replay.daily_evidence import (
    DailyOpenObservation,
    adapt_daily_response,
    numeric,
    response_payload,
)
from delayed_replay.input_artifacts import InputArtifactStore
from delayed_replay.serialization import (
    JsonObject,
    parse_json,
    parse_time,
    require_hash,
    time_text,
)
from delayed_replay.validation import ReplayContractError


class LimitedEvidenceBlocked(ReplayContractError):
    """Structurally valid but incomplete evidence, distinct from corruption."""


def capture(root, sha):
    require_hash(sha)
    path = root / (sha + ".json")
    if path.is_symlink():
        raise ReplayContractError("limited_capture_symlink")
    value = JsonObject.from_value(parse_json(path.read_text()))
    if value.sha256 != sha:
        raise ReplayContractError("limited_capture_corruption")
    return value.to_dict()


def artificial_response_rows(sessions):
    """Explicit test generator, not a switch that relabels market prices."""
    result = {}
    for i, day in enumerate(sessions):
        price = 100 + ((i * 3) % 5 - 2) * 10
        result[day] = dict(
            Date=day,
            Code="72030",
            O=str(price),
            H=str(price + 10),
            L=str(price - 10),
            C=str(price),
            Vo="100000",
            AdjO=str(price),
            AdjH=str(price + 10),
            AdjL=str(price - 10),
            AdjC=str(price),
            AdjVo="100000",
            AdjFactor="1",
            ExRT=None,
        )
    return result


@dataclass(frozen=True)
class SavedProxyInputs:
    rows: JsonObject
    sessions: tuple[str, ...]
    inventory: JsonObject

    @classmethod
    def load(cls, plan, packet_root, evidence_root):
        p = plan.payload.to_dict()
        start, end, symbol = (
            p["scope"]["start"],
            p["scope"]["end"],
            p["scope"]["symbol"],
        )
        captures = {k: capture(evidence_root, h) for k, h in p["captures"].items()}
        calendar = captures["calendar"]
        if calendar.get("schema") != "stage7b-calendar-capture-1":
            raise ReplayContractError("limited_calendar_schema")
        dates = {}
        for r in calendar["data"]:
            if (
                set(r) != {"Date", "HolDiv"}
                or r["Date"] in dates
                or r["HolDiv"] not in ("0", "1", "2", "3")
            ):
                raise ReplayContractError("limited_calendar_invalid")
            dates[r["Date"]] = r["HolDiv"]
        expected = {
            (date.fromisoformat(start) + timedelta(days=i)).isoformat()
            for i in range((date.fromisoformat(end) - date.fromisoformat(start)).days)
        }
        if set(dates) != expected:
            raise LimitedEvidenceBlocked("limited_calendar_incomplete")
        sessions = tuple(sorted(d for d, code in dates.items() if code in ("1", "2")))
        master = captures["master"]
        if (
            master.get("schema") != "stage7b-master-capture-1"
            or len(master["data"]) != 1
            or (
                master["data"][0]["Code"] != symbol
                or not start <= master["data"][0]["Date"] < end
            )
        ):
            raise ReplayContractError("limited_master_mismatch")
        daily = captures["daily_capture"]
        if daily.get("schema") != "stage7b-daily-capture-1":
            raise ReplayContractError("limited_daily_capture_schema")
        raw_source = captures["daily_source"]
        artificial = raw_source.get("fixture_generator") == "limited-artificial-v1"
        if artificial != (p["provenance"] == "artificial_fixture"):
            raise ReplayContractError("limited_provenance_mismatch")
        generated = (
            artificial_response_rows(raw_source["fixture_sessions"])
            if artificial
            else None
        )
        source = raw_source["data"]
        source_by_day = {}
        for r in source:
            if (
                r["Date"] in source_by_day
                or r["Code"] != symbol
                or r["Date"] not in sessions
            ):
                raise ReplayContractError("limited_source_scope_or_duplicate")
            source_by_day[r["Date"]] = r
            if generated is not None and generated.get(r["Date"]) != r:
                raise ReplayContractError("limited_artificial_generation_mismatch")
        reported = {}
        for r in daily["data"]:
            value = JsonObject.from_value(r)
            observation = DailyOpenObservation(value, value.sha256)
            if (
                r["session"] in reported
                or r["symbol"] != symbol
                or r["session"] not in source_by_day
            ):
                raise ReplayContractError("limited_daily_scope_or_duplicate")
            computed = adapt_daily_response(
                source_by_day[r["session"]],
                provider=r["provider"],
                basis=r["provider_price_basis"],
                response_schema=r["response_schema"],
                symbol=symbol,
                session=date.fromisoformat(r["session"]),
                first_observed_at=parse_time(r["first_observed_at"]),
                fetched_at=parse_time(r["fetched_at"]),
                snapshot_hash=response_payload(source_by_day[r["session"]]).sha256,
            )
            if computed != observation:
                raise ReplayContractError("limited_daily_source_lineage")
            reported[r["session"]] = r
        if set(reported) != set(source_by_day):
            raise ReplayContractError("limited_daily_capture_coverage")
        store = InputArtifactStore(packet_root)
        rows, inventory, snapshots = {}, [], []
        for sha in p["history_packets"] + p["packets"]:
            packet = store.load(sha)
            if packet.market.open_snapshots:
                raise ReplayContractError("limited_old_execution_evidence_forbidden")
            count = 0
            for snapshot in packet.market.snapshots:
                snapshots.append(
                    dict(
                        packet_hash=sha,
                        snapshot_hash=snapshot.payload_sha256,
                        provider=snapshot.provider,
                        provider_price_basis=snapshot.provider_price_basis,
                        source_hash=snapshot.source_artifact_sha256,
                        first_observed_at=time_text(snapshot.first_observed_at),
                        fetched_at=time_text(snapshot.fetched_at),
                        count=len(snapshot.observations),
                    )
                )
                if (
                    snapshot.provider != "jquants"
                    or snapshot.provider_price_basis != provider_price_basis("jquants")
                ):
                    raise ReplayContractError("limited_provider_price_basis")
                for b in snapshot.observations:
                    day = b.session_date.isoformat()
                    if (
                        b.symbol != symbol
                        or (sha in p["history_packets"] and day >= start)
                        or (sha in p["packets"] and not start <= day < end)
                    ):
                        raise ReplayContractError("limited_packet_scope")
                    key = symbol + "|" + day
                    if key in rows:
                        raise ReplayContractError("limited_duplicate_observation")
                    raw, adjusted = (
                        [numeric(v) for v in b.raw_ohlcv],
                        [numeric(v) for v in b.adjusted_ohlcv],
                    )
                    if generated is not None:
                        expected_row = generated.get(day)
                        if (
                            expected_row is None
                            or raw
                            != [expected_row[k] for k in ("O", "H", "L", "C", "Vo")]
                            or adjusted != raw
                        ):
                            raise ReplayContractError(
                                "limited_artificial_packet_mismatch"
                            )
                    if sha in p["packets"]:
                        r = reported.get(day)
                        if (
                            r is None
                            or r["values"]
                            != raw + adjusted + [numeric(b.adjustment_factor)]
                            or (
                                snapshot.source_artifact_sha256
                                != p["captures"]["daily_source"]
                                or time_text(snapshot.first_observed_at)
                                != r["first_observed_at"]
                                or time_text(snapshot.fetched_at) != r["fetched_at"]
                            )
                        ):
                            raise ReplayContractError("limited_packet_capture_lineage")
                    row = dict(
                        symbol=symbol,
                        session=day,
                        **dict(
                            zip(
                                ("open", "high", "low", "close", "volume"),
                                raw,
                                strict=True,
                            )
                        ),
                        adjustment_factor=numeric(b.adjustment_factor),
                        halt=p["references"]["halt"]["observations"].get(
                            day, "unknown"
                        ),
                        volume_unit="execution_shares",
                        actual_trade_at=None,
                    )
                    rows[key] = dict(
                        row=row,
                        adjusted=adjusted,
                        packet=sha,
                        snapshot_hash=snapshot.payload_sha256,
                        first_observed_at=time_text(snapshot.first_observed_at),
                        fetched_at=time_text(snapshot.fetched_at),
                        acquired_at=time_text(snapshot.fetched_at),
                        stock_split=numeric(b.stock_split),
                        dividend=numeric(b.dividend),
                        ex_right=reported.get(day, {}).get("ex_right"),
                    )
                    count += 1
            inventory.append(
                dict(
                    packet_hash=sha,
                    count=count,
                    role="history" if sha in p["history_packets"] else "run",
                )
            )
        run_dates = {
            v["row"]["session"] for v in rows.values() if v["packet"] in p["packets"]
        }
        if run_dates != set(sessions) or set(reported) != run_dates:
            raise ReplayContractError("limited_session_price_coverage")
        parts = [
            [v["row"]["session"] for v in rows.values() if v["packet"] == sha]
            for sha in p["packets"]
        ]
        if any(len(x) != 9 for x in parts) or max(parts[0]) >= min(parts[1]):
            raise ReplayContractError("limited_ordered_input_parts")
        return cls(
            JsonObject.from_value(rows),
            sessions,
            JsonObject.from_value(
                dict(
                    packets=inventory,
                    snapshots=snapshots,
                    rows=len(run_dates),
                    actual_start=min(run_dates),
                    actual_end=max(run_dates),
                    capture_hashes=p["captures"],
                    calendar_days=len(dates),
                    session_count=len(sessions),
                )
            ),
        )
