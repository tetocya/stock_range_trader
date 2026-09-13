"""Append-only acquisition receipt: one deadline and attempt budget across restart."""

import fcntl
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import requests

from data.providers.jquants_v2 import _configure_official_client_transport
from delayed_replay.live_probe import BASE, deadline, retry_after
from delayed_replay.serialization import JsonObject, digest, parse_time, time_text
from delayed_replay.validation import ReplayContractError

from .contract import SYMBOLS

MASTER, DAILY, CALENDAR = (
    "/equities/master",
    "/equities/bars/daily",
    "/markets/calendar",
)


def query(path, **params):
    return dict(path=path, params=params)


def stage_a():
    return [query(CALENDAR, **{"from": "2026-04-30", "to": "2026-04-30"})] + [
        query(path, code=s, date="2026-04-30")
        for s in SYMBOLS
        for path in (MASTER, DAILY)
    ]


def stage_b(symbol):
    if symbol not in SYMBOLS:
        raise ReplayContractError("selected_symbol_out_of_scope")
    return [
        query(CALENDAR, **{"from": "2026-01-01", "to": "2026-05-31"}),
        query(MASTER, code=symbol, date="2026-05-01"),
        query(DAILY, code=symbol, **{"from": "2026-01-01", "to": "2026-04-29"}),
        query(DAILY, code=symbol, **{"from": "2026-05-01", "to": "2026-05-31"}),
    ]


class AcquisitionStopped(ReplayContractError):
    pass


class Receipt:
    """Exclusive process lease plus transactional, hash-chained durable events."""

    def __init__(self, path, plan, *, now=None):
        self.now = now or (lambda: datetime.now(UTC))
        self.plan = plan
        self.lease = open(str(path) + ".lock", "a+b")
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lease.close()
            raise AcquisitionStopped("acquisition_already_running") from None
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, value TEXT NOT NULL, sha TEXT NOT NULL)"
        )
        self.db.commit()
        try:
            events = self.events()
            if not events:
                self.append("plan", plan.payload.to_dict())
            elif events[0]["data"] != plan.payload.to_dict():
                raise AcquisitionStopped("acquisition_plan_changed")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.db.close()
        self.lease.close()

    def events(self):
        result, previous = [], "0" * 64
        for seq, raw, sha in self.db.execute(
            "SELECT seq,value,sha FROM events ORDER BY seq"
        ):
            v = JsonObject.from_value(json.loads(raw))
            data = v.to_dict()
            if seq != len(result) or sha != v.sha256 or data["parent"] != previous:
                raise AcquisitionStopped("acquisition_receipt_corrupt")
            previous = sha
            result.append(data)
        return result

    def append(self, kind, data):
        previous = self.db.execute(
            "SELECT sha FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        payload = JsonObject.from_value(
            dict(
                kind=kind,
                at=time_text(self.now()),
                parent="0" * 64 if previous is None else previous[0],
                data=data,
            )
        )
        with self.db:
            self.db.execute(
                "INSERT INTO events VALUES (?,?,?)",
                (len(self.events()), payload.encoded, payload.sha256),
            )
        return payload.sha256

    def remaining(self):
        events = self.events()
        now = self.now()
        if any(now < parse_time(e["at"]) for e in events):
            raise AcquisitionStopped("acquisition_clock_regressed")
        starts = [e for e in events if e["kind"] == "attempt"]
        if not starts:
            return 1200.0
        left = (
            parse_time(starts[0]["at"]) + timedelta(seconds=1200) - now
        ).total_seconds()
        if left <= 0:
            raise AcquisitionStopped("acquisition_deadline_exhausted")
        return left

    def statistics(self):
        events = self.events()
        attempts = [e for e in events if e["kind"] == "attempt"]
        return dict(
            attempts=len(attempts),
            max_attempts=20,
            max_seconds=1200,
            started_at=attempts[0]["at"] if attempts else None,
            deadline=time_text(parse_time(attempts[0]["at"]) + timedelta(seconds=1200))
            if attempts
            else None,
            responses=[e["data"]["status"] for e in events if e["kind"] == "response"],
            acquisition_hash=self.plan.sha256,
            selection=self.selection(),
        )

    def selection(self):
        selected = [e["data"] for e in self.events() if e["kind"] == "selection"]
        if len(selected) > 1:
            raise AcquisitionStopped("multiple_selections")
        return selected[0] if selected else None

    def freeze_selection(self, selection):
        old = self.selection()
        if old is not None:
            if old != selection:
                raise AcquisitionStopped("representative_reselection_forbidden")
            return
        self.remaining()
        self.append("selection", selection)


class Transport:
    def __init__(self, receipt, *, client=None, request=None, sleep=time.sleep):
        self.receipt, self.sleep = receipt, sleep
        if request is None:
            if client is None or client.JQUANTS_API_BASE != BASE:
                raise AcquisitionStopped("official_client_required")
            session = _configure_official_client_transport(client)

            def request(path, params, timeout):
                return session.get(
                    BASE + path,
                    params=params,
                    headers=client._base_headers(),
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )

        self.request = request

    def allowed(self, q):
        selected = self.receipt.selection()
        allowed = stage_a() + (stage_b(selected["symbol"]) if selected else [])
        if q not in allowed:
            raise AcquisitionStopped("request_outside_approved_stage")

    def wait(self, seconds):
        if seconds > 0:
            if seconds >= self.receipt.remaining():
                raise AcquisitionStopped("retry_wait_exceeds_deadline")
            self.sleep(seconds)
        self.receipt.remaining()

    def page(self, q, key=None):
        self.allowed(q)
        params = dict(q["params"])
        if key is not None:
            params["pagination_key"] = key
        request_id = digest(dict(path=q["path"], params=params))
        for _ in range(3):
            self.receipt.remaining()
            events = self.receipt.events()
            attempts = [e for e in events if e["kind"] == "attempt"]
            if len(attempts) >= 20:
                raise AcquisitionStopped("acquisition_attempt_budget_exhausted")
            not_before = self.receipt.now()
            if attempts:
                not_before = max(
                    not_before, parse_time(attempts[-1]["at"]) + timedelta(seconds=13)
                )
            for e in events:
                if e["kind"] == "response" and e["data"].get("retry_at"):
                    not_before = max(not_before, parse_time(e["data"]["retry_at"]))
            self.wait((not_before - self.receipt.now()).total_seconds())
            attempt_id = self.receipt.append(
                "attempt", dict(request_id=request_id, query=q, params=params)
            )
            # Reserve durably BEFORE invoking Session.get. A lost reply consumes its attempt.
            response, status, raw, retry_at = None, None, None, None
            try:
                with deadline(self.receipt.remaining()):
                    response = self.request(
                        q["path"], params, min(30.0, self.receipt.remaining())
                    )
                    status = response.status_code
                    if status == 200:
                        body = bytearray()
                        for chunk in response.iter_content(65536):
                            body.extend(chunk)
                            self.receipt.remaining()
                            if len(body) > 4_000_000:
                                raise AcquisitionStopped("response_size_limit")
                        raw = bytes(body).decode("utf-8")
                    elif status == 429 or 500 <= status < 600:
                        value = response.headers.get("Retry-After")
                        delay = retry_after(value) if value is not None else 13
                        retry_at = time_text(
                            self.receipt.now() + timedelta(seconds=delay)
                        )
            except requests.RequestException:
                retry_at = time_text(self.receipt.now() + timedelta(seconds=13))
            finally:
                if response is not None:
                    response.close()
            sha = self.receipt.append(
                "response",
                dict(
                    attempt_id=attempt_id,
                    status=status,
                    request_id=request_id,
                    raw=raw,
                    retry_at=retry_at,
                ),
            )
            self.receipt.remaining()
            if status == 200:
                value = json.loads(raw, parse_float=str)
                if type(value) is not dict or type(value.get("data")) is not list:
                    raise AcquisitionStopped("invalid_response_schema")
                return value, sha
            if status is not None and status != 429 and not 500 <= status < 600:
                raise AcquisitionStopped("http_nonretryable_" + str(status))
        raise AcquisitionStopped("retry_exhausted")

    def fetch(self, q):
        self.allowed(q)
        self.receipt.remaining()
        request_id = digest(q)
        saved = [
            e["data"]["capture"]
            for e in self.receipt.events()
            if e["kind"] == "capture" and e["data"]["request_id"] == request_id
        ]
        if saved:
            return saved[0]
        rows, pages, seen, key = [], [], set(), None
        while True:
            value, sha = self.page(q, key)
            rows.extend(value["data"])
            pages.append(sha)
            if len(rows) > 1000 or any(type(r) is not dict for r in rows):
                raise AcquisitionStopped("response_row_limit_or_schema")
            key = value.get("pagination_key")
            if not key:
                break
            if type(key) is not str or key in seen:
                raise AcquisitionStopped("pagination_cycle_or_type")
            seen.add(key)
        capture = dict(
            query=q, data=rows, pages=pages, fetched_at=time_text(self.receipt.now())
        )
        self.receipt.remaining()
        self.receipt.append("capture", dict(request_id=request_id, capture=capture))
        return capture
