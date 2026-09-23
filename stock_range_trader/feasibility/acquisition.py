"""Date-scoped J-Quants V2 acquisition ledger for the feasibility census.

This module contains no network client. ``AcquisitionRunner`` accepts only
transports whose ``kind`` is ``offline_fixture``; enabling a real transport is a
separate change that needs its own owner approval, an up-to-date check of the
official specification, the Free-plan window, the rate limit and a request
estimate. Market dates (what a row describes) and ``received_at`` (when this
tool stored the response) are recorded separately. A response for a past date
is the provider's value at retrieval time, never proof of the original
contemporaneous snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from .paths import require_new_output_dir

PLAN_SCHEMA = "feasibility-acquisition-plan-v1"
LEDGER_SCHEMA = "feasibility-acquisition-ledger-v1"
MASTER = "/equities/master"
DAILY = "/equities/bars/daily"
CALENDAR = "/markets/calendar"
ALLOWED_TRANSPORT_KINDS = frozenset({"offline_fixture"})
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
ROW_KEYS = {MASTER: ("Code", "Date"), DAILY: ("Code", "Date"), CALENDAR: ("Date",)}
REQUIRED_FIELDS = {
    MASTER: frozenset(
        {"Date", "Code", "CoName", "Mkt", "MktNm", "S17", "S17Nm", "S33", "S33Nm"}
        | {"ProdCat"}
    ),
    DAILY: frozenset(
        {"Date", "Code", "O", "H", "L", "C", "Vo", "Va", "AdjFactor"}
        | {"AdjO", "AdjH", "AdjL", "AdjC", "AdjVo"}
    ),
    CALENDAR: frozenset({"Date", "HolDiv"}),
}
PRICE_FIELDS = ("O", "H", "L", "C")


class AcquisitionError(ValueError):
    """Invalid plan, store or recorded evidence."""


class AcquisitionStopped(AcquisitionError):
    """A run stopped safely; ``terminal`` stops may not be resumed."""

    def __init__(self, reason: str, *, terminal: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.terminal = terminal


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso_date(value: object, name: str) -> str:
    if type(value) is not str:
        raise AcquisitionError(f"{name}_must_be_iso_date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise AcquisitionError(f"{name}_must_be_iso_date") from error
    if parsed.isoformat() != value:
        raise AcquisitionError(f"{name}_must_be_iso_date")
    return value


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise AcquisitionError("clock_must_be_timezone_aware")
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class DateQuery:
    """One endpoint call scoped by market date (never by instrument code)."""

    endpoint: str
    market_date: str | None = None
    start: str | None = None
    end: str | None = None

    def __post_init__(self) -> None:
        if self.endpoint in (MASTER, DAILY):
            _iso_date(self.market_date, "market_date")
            if self.start is not None or self.end is not None:
                raise AcquisitionError("date_query_takes_single_market_date")
        elif self.endpoint == CALENDAR:
            if self.market_date is not None:
                raise AcquisitionError("calendar_query_takes_range")
            if _iso_date(self.start, "start") > _iso_date(self.end, "end"):
                raise AcquisitionError("calendar_range_reversed")
        else:
            raise AcquisitionError("unsupported_endpoint")

    def params(self) -> dict[str, str]:
        if self.endpoint == CALENDAR:
            return {"from": str(self.start), "to": str(self.end)}
        return {"date": str(self.market_date)}

    def to_dict(self) -> dict[str, object]:
        return {"endpoint": self.endpoint, "params": self.params()}

    @property
    def query_id(self) -> str:
        return sha256_text(canonical_json(self.to_dict()))


@dataclass(frozen=True)
class AcquisitionLimits:
    max_requests: int
    max_seconds: int
    max_bytes: int
    max_pages_per_query: int
    max_attempts_per_page: int
    min_interval_seconds: int

    def __post_init__(self) -> None:
        for name, value in self.to_dict().items():
            if type(value) is not int or value <= 0:
                raise AcquisitionError(f"{name}_must_be_positive_integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_requests": self.max_requests,
            "max_seconds": self.max_seconds,
            "max_bytes": self.max_bytes,
            "max_pages_per_query": self.max_pages_per_query,
            "max_attempts_per_page": self.max_attempts_per_page,
            "min_interval_seconds": self.min_interval_seconds,
        }


@dataclass(frozen=True)
class AcquisitionPlan:
    label: str
    queries: tuple[DateQuery, ...]
    limits: AcquisitionLimits

    def __post_init__(self) -> None:
        if type(self.label) is not str or not self.label.strip():
            raise AcquisitionError("plan_label_required")
        if type(self.queries) is not tuple or not self.queries:
            raise AcquisitionError("plan_queries_required")
        if not all(isinstance(q, DateQuery) for q in self.queries):
            raise AcquisitionError("plan_queries_must_be_date_queries")
        ids = [q.query_id for q in self.queries]
        if len(ids) != len(set(ids)):
            raise AcquisitionError("duplicate_plan_query")
        if len(self.queries) > self.limits.max_requests:
            raise AcquisitionError("request_budget_below_query_count")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": PLAN_SCHEMA,
            "label": self.label,
            "provider": "jquants",
            "api": "v2",
            "purpose": "historical_feasibility_census",
            "value_claim": "provider_values_at_retrieval_not_original_snapshot",
            "queries": [q.to_dict() for q in self.queries],
            "limits": self.limits.to_dict(),
        }

    @property
    def sha256(self) -> str:
        return sha256_text(canonical_json(self.to_dict()))


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: bytes


class Transport(Protocol):
    kind: str

    def fetch(self, endpoint: str, params: Mapping[str, str]) -> TransportResponse:
        """Return one HTTP-like response. Credentials never pass through here."""


class AcquisitionStore:
    """Write-once plan, append-only hash-chained ledger, content-addressed bodies."""

    def __init__(self, root: Path, plan: AcquisitionPlan) -> None:
        self.root = root
        self.plan = plan
        self._records: list[dict] | None = None

    @classmethod
    def create(
        cls, root: str | Path, plan: AcquisitionPlan, *, forbidden_roots=()
    ) -> AcquisitionStore:
        target = require_new_output_dir(root, forbidden_roots=forbidden_roots)
        target.mkdir(parents=True)
        (target / "responses").mkdir()
        _write_exclusive(target / "plan.json", canonical_json(plan.to_dict()) + "\n")
        (target / "ledger.jsonl").touch(exist_ok=False)
        return cls(target, plan)

    @classmethod
    def open(cls, root: str | Path, plan: AcquisitionPlan) -> AcquisitionStore:
        target = Path(root).expanduser().resolve()
        plan_path = target / "plan.json"
        if not plan_path.is_file():
            raise AcquisitionError("plan_missing_no_implicit_recreation")
        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        if sha256_text(canonical_json(saved)) != plan.sha256:
            raise AcquisitionError("plan_mismatch_on_resume")
        store = cls(target, plan)
        store.records()  # full chain and body verification before any resume
        return store

    def records(self) -> list[dict]:
        """Return the ledger, verified once per open; appends extend the cache."""

        if self._records is None:
            self._records = self.verify()
        return list(self._records)

    def verify(self) -> list[dict]:
        """Re-read and verify the whole ledger; any break in chain or body is fatal."""

        records, previous = [], None
        text = (self.root / "ledger.jsonl").read_text(encoding="utf-8")
        for seq, line in enumerate(text.splitlines()):
            record = json.loads(line)
            body = {k: v for k, v in record.items() if k != "record_hash"}
            if (
                record.get("seq") != seq
                or record.get("prev_hash") != previous
                or record.get("plan_sha256") != self.plan.sha256
                or sha256_text(canonical_json(body)) != record.get("record_hash")
            ):
                raise AcquisitionError("ledger_chain_broken")
            if record["type"] == "page":
                path = self.root / "responses" / f"{record['body_sha256']}.json"
                if not path.is_file() or path.is_symlink():
                    raise AcquisitionError("response_file_missing")
                if (
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    != record["body_sha256"]
                ):
                    raise AcquisitionError("response_file_changed")
            previous = record["record_hash"]
            records.append(record)
        return records

    def append(self, record: dict) -> dict:
        existing = self.records()
        body = {
            **record,
            "schema": LEDGER_SCHEMA,
            "seq": len(existing),
            "prev_hash": existing[-1]["record_hash"] if existing else None,
            "plan_sha256": self.plan.sha256,
        }
        body["record_hash"] = sha256_text(canonical_json(body))
        with (self.root / "ledger.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(body) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._records = [*existing, body]
        return body

    def save_body(self, body: bytes) -> str:
        digest = hashlib.sha256(body).hexdigest()
        path = self.root / "responses" / f"{digest}.json"
        if path.exists():
            if path.read_bytes() != body:
                raise AcquisitionError("response_hash_collision")
            return digest
        with open(path, "xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        return digest

    def load_body(self, digest: str) -> dict:
        return json.loads((self.root / "responses" / f"{digest}.json").read_bytes())


def _write_exclusive(path: Path, text: str) -> None:
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True)
class AcquisitionSummary:
    status: str
    requests: int
    pages: int
    rows: int
    bytes: int
    completed_queries: int


class AcquisitionRunner:
    def __init__(
        self,
        store: AcquisitionStore,
        transport: Transport,
        *,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
    ) -> None:
        self.store, self.transport = store, transport
        self.clock, self.sleep = clock, sleep

    def run(self) -> AcquisitionSummary:
        # Refuse before any ledger write: no real network transport is enabled.
        if getattr(self.transport, "kind", None) not in ALLOWED_TRANSPORT_KINDS:
            raise AcquisitionStopped("network_transport_not_enabled", terminal=True)
        records = self.store.records()
        for record in records:
            if record["type"] == "stop" and record["terminal"]:
                raise AcquisitionStopped(
                    "terminal_stop_recorded:" + record["reason"], terminal=True
                )
        try:
            for query in self.store.plan.queries:
                self._run_query(query)
        except AcquisitionStopped as stop:
            self.store.append(
                {"type": "stop", "reason": stop.reason, "terminal": stop.terminal}
            )
            raise
        self.store.append({"type": "completed"})
        return summarize(self.store)

    def _state(self) -> dict:
        records = self.store.records()
        attempts = [r for r in records if r["type"] == "attempt"]
        pages = [r for r in records if r["type"] == "page"]
        return {
            "records": records,
            "requests": len(attempts),
            "bytes": sum(r["bytes"] for r in pages),
            "started_at": attempts[0]["requested_at"] if attempts else None,
            "last_request_at": attempts[-1]["requested_at"] if attempts else None,
        }

    def _run_query(self, query: DateQuery) -> None:
        qid = query.query_id
        records = self.store.records()
        if any(r["type"] == "query_complete" and r["query_id"] == qid for r in records):
            return
        pages = [r for r in records if r["type"] == "page" and r["query_id"] == qid]
        seen_keys = {r["next_key"] for r in pages if r["next_key"]}
        seen_bodies = {r["body_sha256"] for r in pages}
        seen_rows = set()
        for page in pages:
            for row in self.store.load_body(page["body_sha256"])["data"]:
                seen_rows.add(tuple(row[k] for k in ROW_KEYS[query.endpoint]))
        next_key = pages[-1]["next_key"] if pages else None
        page_index = len(pages)
        while True:
            if page_index >= self.store.plan.limits.max_pages_per_query:
                raise AcquisitionStopped("page_limit_exceeded", terminal=True)
            params = query.params()
            if next_key is not None:
                params["pagination_key"] = next_key
            response = self._fetch_with_retry(query, page_index, params)
            payload = self._validate(query, response.body, seen_rows)
            digest = hashlib.sha256(response.body).hexdigest()
            key = payload.get("pagination_key") or None
            if digest in seen_bodies:
                raise AcquisitionStopped("repeated_page_body", terminal=True)
            if key is not None and (key in seen_keys or key == next_key):
                raise AcquisitionStopped("pagination_key_loop", terminal=True)
            if not payload["data"] and key is not None:
                raise AcquisitionStopped("empty_page_with_continuation", terminal=True)
            self.store.save_body(response.body)
            null_rows = sum(
                1
                for row in payload["data"]
                if query.endpoint == DAILY
                and any(row.get(f) is None for f in PRICE_FIELDS)
            )
            self.store.append(
                {
                    "type": "page",
                    "query_id": qid,
                    "endpoint": query.endpoint,
                    "market_params": query.params(),
                    "page_index": page_index,
                    "received_at": _utc_text(self.clock()),
                    "status": response.status,
                    "bytes": len(response.body),
                    "body_sha256": digest,
                    "row_count": len(payload["data"]),
                    "null_price_rows": null_rows,
                    "next_key": key,
                }
            )
            seen_bodies.add(digest)
            if key is None:
                self.store.append({"type": "query_complete", "query_id": qid})
                return
            seen_keys.add(key)
            next_key, page_index = key, page_index + 1

    def _fetch_with_retry(
        self, query: DateQuery, page_index: int, params: dict[str, str]
    ) -> TransportResponse:
        limits = self.store.plan.limits
        for attempt in range(limits.max_attempts_per_page):
            state = self._state()
            if state["requests"] >= limits.max_requests:
                raise AcquisitionStopped("request_budget_exhausted", terminal=True)
            now = self.clock()
            if state["started_at"] is not None:
                started = datetime.fromisoformat(state["started_at"])
                if (now - started).total_seconds() >= limits.max_seconds:
                    raise AcquisitionStopped("time_budget_exhausted", terminal=True)
            if state["last_request_at"] is not None:
                elapsed = (
                    now - datetime.fromisoformat(state["last_request_at"])
                ).total_seconds()
                wait = limits.min_interval_seconds - elapsed
                if wait > 0:
                    started = datetime.fromisoformat(state["started_at"])
                    if (now - started).total_seconds() + wait >= limits.max_seconds:
                        raise AcquisitionStopped("time_budget_exhausted", terminal=True)
                    self.sleep(wait)
            # Reserve the attempt before sending so a crash still consumes budget.
            self.store.append(
                {
                    "type": "attempt",
                    "query_id": query.query_id,
                    "page_index": page_index,
                    "attempt": attempt,
                    "requested_at": _utc_text(self.clock()),
                }
            )
            try:
                response = self.transport.fetch(query.endpoint, dict(params))
            except Exception as error:  # noqa: BLE001 - class name only, no message
                self.store.append(
                    {"type": "attempt_failed", "error_class": type(error).__name__}
                )
                continue
            if response.status == 200:
                state = self._state()
                if state["bytes"] + len(response.body) > limits.max_bytes:
                    raise AcquisitionStopped("storage_budget_exceeded", terminal=True)
                return response
            self.store.append(
                {"type": "attempt_failed", "error_class": f"http_{response.status}"}
            )
            if response.status in (401, 403):
                raise AcquisitionStopped(
                    "not_authorized_or_outside_plan", terminal=True
                )
            if response.status not in TRANSIENT_STATUSES:
                raise AcquisitionStopped(f"http_{response.status}", terminal=True)
        raise AcquisitionStopped("transient_failures_exhausted", terminal=False)

    def _validate(self, query: DateQuery, body: bytes, seen_rows: set) -> dict:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AcquisitionStopped("invalid_json_response", terminal=True) from error
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise AcquisitionStopped("unsupported_response_schema", terminal=True)
        key = payload.get("pagination_key")
        if key is not None and (type(key) is not str or not key):
            raise AcquisitionStopped("invalid_pagination_key", terminal=True)
        page_rows = set()
        for row in data:
            if not REQUIRED_FIELDS[query.endpoint] <= set(row):
                raise AcquisitionStopped("unsupported_response_schema", terminal=True)
            if query.endpoint == CALENDAR:
                if not str(query.start) <= str(row["Date"]) <= str(query.end):
                    raise AcquisitionStopped(
                        "row_outside_requested_range", terminal=True
                    )
            elif row["Date"] != query.market_date:
                raise AcquisitionStopped("row_market_date_mismatch", terminal=True)
            row_key = tuple(row[k] for k in ROW_KEYS[query.endpoint])
            if row_key in page_rows or row_key in seen_rows:
                raise AcquisitionStopped("duplicate_row", terminal=True)
            page_rows.add(row_key)
        seen_rows |= page_rows
        return payload


def summarize(store: AcquisitionStore) -> AcquisitionSummary:
    records = store.records()
    pages = [r for r in records if r["type"] == "page"]
    done = {r["query_id"] for r in records if r["type"] == "query_complete"}
    status = "completed" if records and records[-1]["type"] == "completed" else "open"
    return AcquisitionSummary(
        status=status,
        requests=sum(1 for r in records if r["type"] == "attempt"),
        pages=len(pages),
        rows=sum(r["row_count"] for r in pages),
        bytes=sum(r["bytes"] for r in pages),
        completed_queries=len(done),
    )


@dataclass(frozen=True)
class AcquiredRows:
    """Rows of completed queries plus retrieval provenance (not market time)."""

    master: tuple[dict, ...]
    daily: tuple[dict, ...]
    calendar: tuple[dict, ...]
    acquired_daily_dates: tuple[str, ...]
    provenance: dict


def load_completed_rows(store: AcquisitionStore) -> AcquiredRows:
    records = store.verify()
    if not records or records[-1]["type"] != "completed":
        raise AcquisitionError("acquisition_incomplete")
    rows: dict[str, list] = {MASTER: [], DAILY: [], CALENDAR: []}
    received = []
    for record in records:
        if record["type"] != "page":
            continue
        received.append(record["received_at"])
        for row in store.load_body(record["body_sha256"])["data"]:
            rows[record["endpoint"]].append(
                {**row, "_received_at": record["received_at"]}
            )
    daily_dates = tuple(
        sorted(q.market_date for q in store.plan.queries if q.endpoint == DAILY)
    )
    return AcquiredRows(
        master=tuple(rows[MASTER]),
        daily=tuple(rows[DAILY]),
        calendar=tuple(rows[CALENDAR]),
        acquired_daily_dates=daily_dates,
        provenance={
            "source": "jquants_v2_date_queries",
            "plan_sha256": store.plan.sha256,
            "ledger_head": records[-1]["record_hash"],
            "received_at_min": min(received) if received else None,
            "received_at_max": max(received) if received else None,
            "value_claim": "provider_values_at_retrieval_not_original_snapshot",
        },
    )
