"""Readonly June evidence adapters. Never construct Receipt or a trading service."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from data.price_policy import provider_price_basis
from delayed_replay import june_trial as june
from delayed_replay.daily_evidence import numeric
from delayed_replay.input_artifacts import InputPacket
from delayed_replay.selected_trial.acquisition import Receipt
from delayed_replay.selected_trial.contract import SelectedTrialPlan
from delayed_replay.serialization import (
    JsonObject,
    digest,
    parse_time,
    require_hash,
    time_text,
)

from .order_audit import _price_evidence
from .reader import EvidenceFiles, ObservationError, read_database


class PreflightEvidence(EvidenceFiles):
    def __init__(self, *roots):
        super().__init__()
        self.roots = tuple(Path(r).absolute() for r in roots)
        self.members = self._members()
        for p in self.members:
            self.read(p)

    def _members(self):
        return tuple(
            sorted(
                p
                for root in self.roots
                for p in root.rglob("*")
                if p.suffix in (".json", ".pdf") and (p.is_file() or p.is_symlink())
            )
        )

    def verify(self):
        if self._members() != self.members:
            raise ObservationError("evidence_inventory_changed")
        super().verify()

    def artifact(self, root, sha):
        require_hash(sha)
        return self.json(Path(root) / (sha + ".json"), sha).to_dict()


class ReadOnlyReceipt:
    # Only existing pure SELECT/hash/statistics methods. No __init__, append,
    # remaining, lease, transport, budget initialization or recovery method.
    events = Receipt.events
    statistics = Receipt.statistics
    selection = Receipt.selection

    def __init__(self, db, plan):
        self.db, self.plan = db, plan


def receipt_snapshot(root, plan, files):
    with read_database(Path(root) / "acquisition.sqlite", files) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != 0:
            raise ObservationError("unsupported_receipt_schema")
        columns = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='events'"
        ).fetchone()
        if columns is None:
            raise ObservationError("missing_receipt_table")
        receipt = ReadOnlyReceipt(db, plan)
        events = receipt.events()
        if (
            not events
            or events[0]["kind"] != "plan"
            or events[0]["data"] != plan.payload.to_dict()
        ):
            raise ObservationError("receipt_plan_binding")
        last = None
        seen_captures = set()
        kinds = {
            "plan",
            "attempt",
            "response",
            "capture",
            "history_verified",
            "stopped",
        }
        for index, event in enumerate(events):
            if (
                set(event) != {"kind", "at", "parent", "data"}
                or event["kind"] not in kinds
                or (index and event["kind"] == "plan")
            ):
                raise ObservationError("unsupported_receipt_event")
            stamp = parse_time(event["at"])
            if last and stamp < last:
                raise ObservationError("receipt_clock_order")
            last = stamp
            if event["kind"] == "attempt":
                a = event["data"]
                if a["query"] not in june.queries() or a["request_id"] != digest(
                    dict(path=a["query"]["path"], params=a["params"])
                ):
                    raise ObservationError("receipt_request_scope")
                params = dict(a["params"])
                params.pop("pagination_key", None)
                if params != a["query"]["params"]:
                    raise ObservationError("receipt_request_params")
            if event["kind"] == "response":
                status = event["data"]["status"]
                if status is not None and (
                    type(status) is not int or not 100 <= status <= 599
                ):
                    raise ObservationError("receipt_response_status")
            if event["kind"] == "capture":
                item = event["data"]
                query = item["capture"]["query"]
                sha = digest(query)
                if (
                    query not in june.queries()
                    or sha in seen_captures
                    or item["request_id"] != sha
                ):
                    raise ObservationError("receipt_capture_scope_or_duplicate")
                seen_captures.add(sha)
        stats = receipt.statistics()
        attempts = [e for e in events if e["kind"] == "attempt"]
        if stats["attempts"] > 20 or any(
            parse_time(b["at"]) - parse_time(a["at"]) < timedelta(seconds=13)
            for a, b in zip(attempts, attempts[1:], strict=False)
        ):
            raise ObservationError("receipt_attempt_contract")
        stats["last_attempt_at"] = attempts[-1]["at"] if attempts else None
        return SimpleNamespace(
            events=lambda: events,
            statistics=stats,
            head=dict(sequence=len(events) - 1, event_hash=digest(events[-1])),
        )


def history_snapshot(may_root, plan, files):
    """Validate frozen selection's saved packet lineage; do not rerun select()."""
    parent = SelectedTrialPlan(files.json(Path(may_root) / "trial_plan.json"))
    p = parent.payload.to_dict()
    expected = plan.payload.to_dict()["parent"]
    settings = {k: p[k] for k in ("strategy", "terms", "rules")}
    if (
        parent.sha256 != expected["plan_hash"]
        or settings != plan.payload.to_dict()["settings"]
        or digest(settings) != june.SETTINGS_HASH
    ):
        raise ObservationError("parent_settings_or_plan_binding")
    if (
        p["history_packets"] + p["packets"] != expected["packets"]
        or p["model_hash"] != expected["model_hash"]
        or p["provenance"] != expected["provenance"]
    ):
        raise ObservationError("parent_packet_binding")
    binding = files.artifact(
        may_root, p["source_identity"].removeprefix("selected-binding:")
    )
    calendar = next(
        c["data"]
        for c in binding["stage_b"].values()
        if c["query"]["path"] == june.CALENDAR
    )
    allowed = june.sessions(calendar, "2026-01-01", "2026-06-01")
    rows = {}
    cache = {}
    for sha in expected["packets"]:
        packet = InputPacket.from_payload(
            files.json(Path(may_root) / "inputs" / (sha + ".json"), sha)
        )
        if packet.market.open_snapshots:
            raise ObservationError("historical_execution_evidence_forbidden")
        for snapshot in packet.market.snapshots:
            if (
                snapshot.provider != "jquants"
                or snapshot.provider_price_basis != provider_price_basis("jquants")
            ):
                raise ObservationError("historical_price_basis")
            raw_source = files.artifact(may_root, snapshot.source_artifact_sha256)
            for bar in snapshot.observations:
                day = bar.session_date.isoformat()
                key = bar.symbol + "|" + day
                if bar.symbol != "46890" or day not in allowed or key in rows:
                    raise ObservationError("historical_scope_or_duplicate")
                raw = list(map(numeric, bar.raw_ohlcv))
                source = next(
                    r
                    for r in raw_source["data"]
                    if r["Date"] == day and r["Code"] == bar.symbol
                )
                rows[key] = dict(
                    row=dict(
                        symbol=bar.symbol,
                        session=day,
                        **dict(
                            zip(
                                ("open", "high", "low", "close", "volume"),
                                raw,
                                strict=True,
                            )
                        ),
                        adjustment_factor=numeric(bar.adjustment_factor),
                    ),
                    adjusted=list(map(numeric, bar.adjusted_ohlcv)),
                    packet=sha,
                    snapshot_hash=snapshot.payload_sha256,
                    first_observed_at=time_text(snapshot.first_observed_at),
                    fetched_at=time_text(snapshot.fetched_at),
                    ex_right=source.get("ExRT"),
                )
                _, _, missing = _price_evidence(
                    Path(may_root), dict(inputs=rows), bar.symbol, day, files, cache
                )
                if missing:
                    raise ObservationError("historical_price_evidence_missing")
    bundle = SimpleNamespace(rows=JsonObject.from_value(rows))
    if (
        len(rows) < 78
        or {r["row"]["session"] for r in rows.values()} != set(allowed)
        or digest(june.history_values(bundle)) != expected["history_hash"]
    ):
        raise ObservationError("historical_values_or_coverage")
    return parent, bundle, calendar


def validate_captures(receipt, history):
    captures = june.receipt_captures(receipt)
    calendar, master, previous, run = captures
    _, bundle, old_calendar = history
    june.sessions(calendar["data"], "2026-05-29", "2026-07-01")
    if sorted(
        (r for r in old_calendar if r["Date"] >= "2026-05-29"), key=lambda r: r["Date"]
    ) != sorted(
        (r for r in calendar["data"] if r["Date"] < "2026-06-01"),
        key=lambda r: r["Date"],
    ):
        raise ObservationError("calendar_history_changed")
    parts = june.split_sessions(
        [r for r in calendar["data"] if r["Date"] >= "2026-06-01"]
    )
    if len(master["data"]) != 1 or any(
        master["data"][0].get(k) != v
        for k, v in dict(Code="46890", Date="2026-06-01", ProdCat="011").items()
    ):
        raise ObservationError("master_scope_mismatch")
    comparison = june.compare_history(bundle, previous)
    reported = june.daily(run, "46890", parts[0] + parts[1])
    return captures, parts, comparison, reported


def finite_prefixes(history, captured):
    import numpy as np
    import pandas as pd

    parent, bundle, _ = history
    _, parts, _, reported = captured
    # Constructing indicator configuration is pure; never call SignalAdapter.decide.
    config = parent.signals().config("baseline")
    values = {d: r["values"] for d, r in june.history_values(bundle).items()}
    values.update({d: r["values"] for d, r in reported.items()})
    for day in parts[0] + parts[1]:
        frame = pd.DataFrame(
            [
                dict(
                    date=pd.Timestamp(d),
                    **dict(
                        zip(
                            ("open", "high", "low", "close", "volume"),
                            map(float, v[5:10]),
                            strict=True,
                        )
                    ),
                )
                for d, v in sorted(values.items())
                if d <= day
            ]
        )
        frame["turnover_value"] = frame.close * frame.volume
        features = config.create_scorer().transform(
            config.create_detector().transform(frame)
        )
        if not np.isfinite(
            features.iloc[-1][["sma", "atr", "adx", "range_score"]].to_numpy(
                dtype=float
            )
        ).all():
            raise ObservationError("nonfinite_saved_prefix_features")
    return dict(
        sessions=len(parts[0]) + len(parts[1]),
        method="indicator_prefix_validation_no_signals",
    )
