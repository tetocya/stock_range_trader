"""Stage 7B bounded acquisition probe. Never a strategy or execution runner."""

import json
import math
import os
import signal
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from importlib.metadata import version
from pathlib import Path

import requests

from data.canonical import validate_canonical_bars
from data.price_policy import provider_price_basis
from data.providers.jquants_v2 import (
    _configure_official_client_transport,
    jquants_daily_to_canonical,
)

from .clock import ReplayClock
from .daily_evidence import (
    JQUANTS_RESPONSE_SCHEMA,
    adapt_daily_response,
    response_payload,
)
from .input_artifacts import InputArtifactStore, InputPacket
from .market_view import MarketView
from .reference_evidence import lot_from_master
from .serialization import JsonObject
from .snapshot import PriceObservation, PriceSnapshot

STAGE7A_SHA = "575c733517955a7dc9ca9b5a617ac4e43d6e2cc6"
BASE = "https://api.jquants.com/v2"
ENDPOINTS = ("/equities/master", "/equities/bars/daily", "/markets/calendar")


class ProbeError(Exception):
    """Only fixed, safe reason codes cross the probe boundary."""

    def __init__(self, reason, status="failed"):
        self.reason = reason
        self.status = status
        super().__init__(reason)


@dataclass(frozen=True)
class ProbeConfig:
    start: date
    end: date
    master_date: date
    symbol: str = "72030"
    max_attempts: int = 20
    max_seconds: int = 1200
    interval_seconds: float = 13.0

    def __post_init__(self):
        if any(type(v) is not date for v in (self.start, self.end, self.master_date)):
            raise ProbeError("invalid_dates")
        if not 1 <= (self.end - self.start).days <= 31:
            raise ProbeError("one_month_limit")
        if not self.start <= self.master_date < self.end or self.symbol != "72030":
            raise ProbeError("target_scope_mismatch")
        for value, limit in ((self.max_attempts, 20), (self.max_seconds, 1200)):
            if type(value) is not int or not 1 <= value <= limit:
                raise ProbeError("invalid_budget")
        if (
            type(self.interval_seconds) not in (int, float)
            or not math.isfinite(self.interval_seconds)
            or not 13 <= self.interval_seconds <= 1200
        ):
            raise ProbeError("invalid_interval")

    def queries(self):
        period = dict(
            **{"from": self.start.isoformat()},
            to=(self.end - timedelta(days=1)).isoformat(),
        )
        return {
            ENDPOINTS[0]: dict(code=self.symbol, date=self.master_date.isoformat()),
            ENDPOINTS[1]: dict(code=self.symbol, **period),
            ENDPOINTS[2]: period,
        }

    def preflight(self):
        return dict(
            stage7a_sha=STAGE7A_SHA,
            symbol=self.symbol,
            start=self.start.isoformat(),
            end_exclusive=self.end.isoformat(),
            queries=self.queries(),
            max_http_attempts=self.max_attempts,
            max_elapsed_seconds=self.max_seconds,
            min_interval_seconds=self.interval_seconds,
            destination=".delayed_replay/stage7b/<unique-run>/",
            client_version=version("jquants-api-client"),
            executable="unsupported",
            registration="draft_not_registered",
        )


class BoundedTransport:
    """One retry owner, redirects off, deadline checked before every wait/attempt.

    Official ClientV2 Session is reused, NOT its request/pagination/retry loop.
    Socket timeouts and a POSIX main-thread deadline bound the request, body
    reads and retry waits. Unsupported deadline environments fail closed.
    """

    def __init__(self, config, client, *, clock=time.monotonic, sleep=time.sleep):
        self.config, self.client = config, client
        self.session = _configure_official_client_transport(client)
        if client.JQUANTS_API_BASE != BASE:
            raise ProbeError("client_base_mismatch")
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.last = None
        self.attempts = 0
        self.audit = []

    def remaining(self):
        remaining = self.config.max_seconds - (self.clock() - self.started)
        if remaining <= 0:
            raise ProbeError("elapsed_budget_exhausted", "blocked")
        return remaining

    def wait(self, seconds):
        if not math.isfinite(seconds) or seconds >= self.remaining():
            raise ProbeError("wait_exceeds_budget", "blocked")
        if seconds > 0:
            self.sleep(seconds)
        self.remaining()

    def page(self, path, query):
        with deadline(self.remaining()):
            return self._page(path, query)

    def _page(self, path, query):
        expected = self.config.queries().get(path)
        if (
            expected is None
            or {k: v for k, v in query.items() if k != "pagination_key"} != expected
        ):
            raise ProbeError("request_out_of_scope")
        for retry in range(3):
            if self.attempts >= self.config.max_attempts:
                raise ProbeError("attempt_budget_exhausted", "blocked")
            if self.last is not None:
                self.wait(
                    max(0.0, self.config.interval_seconds - (self.clock() - self.last))
                )
            timeout = min(30.0, self.remaining())
            self.last = self.clock()
            self.attempts += 1
            entry = dict(
                attempt=self.attempts,
                endpoint=path,
                status=None,
                reason="network_error",
            )
            self.audit.append(entry)
            response = None
            delay = float(2**retry)
            try:
                response = self.session.get(
                    BASE + path,
                    params=query,
                    headers=self.client._base_headers(),  # noqa: SLF001
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
                status = response.status_code
                entry["status"] = status
                self.remaining()
                if status == 200:
                    body = bytearray()
                    for chunk in response.iter_content(65536):
                        self.remaining()
                        body.extend(chunk)
                        if len(body) > 4_000_000:
                            raise ProbeError("response_size_limit")
                    try:
                        payload = json.loads(body)
                    except (ValueError, UnicodeError):
                        raise ProbeError("invalid_json") from None
                    entry["reason"] = "http_ok"
                    return payload
                if status in (401, 403):
                    entry["reason"] = f"http_{status}"
                    raise ProbeError(f"http_{status}", "blocked")
                if status != 429 and not 500 <= status < 600:
                    entry["reason"] = "http_nonretryable"
                    raise ProbeError("http_nonretryable")
                entry["reason"] = "http_retryable"
                value = response.headers.get("Retry-After")
                if value is not None:
                    delay = retry_after(value)
            except requests.RequestException:
                entry["reason"] = "network_error"
            finally:
                if response is not None:
                    response.close()
            if retry == 2:
                raise ProbeError("retry_exhausted", "blocked")
            if self.attempts >= self.config.max_attempts:
                raise ProbeError("attempt_budget_exhausted", "blocked")
            self.wait(delay)
        raise AssertionError("unreachable")

    def fetch(self, path):
        query = dict(self.config.queries()[path])
        rows, seen = [], set()
        while True:
            payload = self.page(path, query)
            if (
                type(payload) is not dict
                or type(payload.get("data")) is not list
                or any(type(r) is not dict for r in payload["data"])
            ):
                raise ProbeError("invalid_response_schema")
            rows.extend(payload["data"])
            if len(rows) > 1000:
                raise ProbeError("response_row_limit")
            key = payload.get("pagination_key")
            if key in (None, ""):
                return rows
            if type(key) is not str or len(key) > 4096 or key in seen:
                raise ProbeError("invalid_pagination")
            seen.add(key)
            query["pagination_key"] = key


def retry_after(value):
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                raise ValueError
            delay = (parsed - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            raise ProbeError("invalid_retry_after", "blocked") from None
    if not math.isfinite(delay):
        raise ProbeError("invalid_retry_after", "blocked")
    return max(0.0, delay)


@contextmanager
def deadline(seconds):
    if (
        not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        raise ProbeError("hard_deadline_unavailable", "blocked")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ProbeError("existing_process_timer", "blocked")

    def expire(signum, frame):
        raise ProbeError("elapsed_budget_exhausted", "blocked")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def validate_live_window(config, today):
    # Conservative inner bounds, NOT an entitlement assertion or inferred session.
    if config.end > today - timedelta(weeks=12) or config.start < today - timedelta(
        days=730
    ):
        raise ProbeError("outside_conservative_free_window", "blocked")


def save_capture(root, capture):
    """Exclusive atomic local value capture, no partial file under final hash."""
    target = root / (capture.sha256 + ".json")
    fd, name = tempfile.mkstemp(prefix=".capture-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(capture.encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(name, target)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)
    loaded = JsonObject.from_value(json.loads(target.read_text(encoding="utf-8")))
    if loaded != capture:
        raise ProbeError("capture_hash_mismatch")


def layer(status, reason):
    return dict(status=status, reason=reason)


def calendar_check(rows, config, dates):
    """Date evidence only: never invent session hours or exchange holidays."""
    parsed = {}
    for row in rows:
        day = date.fromisoformat(row["Date"])
        if (
            day in parsed
            or not config.start <= day < config.end
            or row["HolDiv"] not in ("0", "1", "2", "3")
        ):
            raise ProbeError("invalid_calendar")
        parsed[day] = row["HolDiv"]
    if len(parsed) != (config.end - config.start).days:
        raise ProbeError("partial_calendar")
    sessions = {d for d, division in parsed.items() if division in ("1", "2")}
    if sessions != set(dates):
        raise ProbeError("calendar_price_session_mismatch")
    return layer("verified", "explicit_session_dates_only_hours_unverified")


def persist_observations(records, observations, frame, acquired, root):
    """Immutable Stage7A packets and conservative public view; no Fill adapter.

    Unknown historical publication times are not backdated. Full bars become
    visible at the ACTUAL acquisition time only. This cannot drive a historical
    account and is explicitly not a historical daily publication replay.
    """
    if any(o.to_dict()["quality"] != "reported" for o in observations):
        raise ProbeError("price_packet_requires_reported_rows", "unsupported")
    store = InputArtifactStore(root / "inputs")
    rows = list(frame.to_dict("records"))
    if len(rows) < 2:
        raise ProbeError("insufficient_rows_for_split", "blocked")
    packets = []
    hashes = []
    for part in (rows[: len(rows) // 2], rows[len(rows) // 2 :]):
        bars = tuple(
            PriceObservation(
                symbol=r["symbol"],
                session_date=r["date"].date(),
                market_available_at=acquired,
                raw_ohlcv=tuple(
                    float(r["raw_" + k])
                    for k in ("open", "high", "low", "close", "volume")
                ),
                adjusted_ohlcv=tuple(
                    float(r["adjusted_" + k])
                    for k in ("open", "high", "low", "close", "volume")
                ),
                adjustment_factor=float(r["adjustment_factor"]),
                stock_split=0.0,
                dividend=0.0,
            )
            for r in part
        )
        snapshot = PriceSnapshot.create(
            provider="jquants",
            provider_price_basis=provider_price_basis("jquants"),
            source_artifact_sha256=response_payload(dict(data=records)).sha256,
            data_version="stage7b-acquisition-only-1",
            first_observed_at=acquired,
            fetched_at=acquired,
            provider_published_at=None,
            publication_time_unknown_reason="historical_publication_unverified",
            price_basis_evidence_id=None,
            observations=bars,
        )
        packet = InputPacket(MarketView((snapshot,), ()))
        sha = store.publish(packet)
        before = store.path(sha).read_bytes()
        assert store.publish(store.load(sha)) == sha
        assert store.path(sha).read_bytes() == before
        packets.append(store.load(sha))
        hashes.append(sha)
    view = MarketView(tuple(p.market.snapshots[0] for p in packets), ())
    start = frame["date"].min().date()
    end = frame["date"].max().date() + timedelta(days=1)
    prior = acquired - timedelta(microseconds=1)
    assert not view.observations(ReplayClock(prior, acquired), start, end, {"72030"})
    assert len(
        view.observations(ReplayClock(acquired, acquired), start, end, {"72030"})
    ) == len(rows)
    return hashes


def run_probe(config, root, *, live=False, reviewed=False, transport=None):
    """Default is preflight only. Injected transport is artificial-test-only.

    Caller uses a unique ignored directory. No raw partial fetch is saved.
    A successful fetch is not a claim of complete market-session coverage.
    """
    report = dict(
        preflight=config.preflight(),
        executed_at=datetime.now(UTC).isoformat(),
        live_attempted=False,
        attempts=0,
        end_reason="preflight_only",
        rows=0,
        actual_start=None,
        actual_end=None,
        snapshot_hashes=[],
        acquisition_complete=False,
        layers={
            k: layer("not_tested", "not_attempted")
            for k in (
                "communication",
                "data",
                "instrument",
                "lot",
                "calendar",
                "persistence",
                "public_view",
                "input_extension",
                "corporate_actions",
            )
        },
    )
    report["layers"]["execution"] = layer(
        "unsupported", "daily_open_not_auction_evidence_proxy_unapproved"
    )
    if not live:
        return report
    if transport is None and not os.environ.get("JQUANTS_API_KEY", "").strip():
        report["end_reason"] = "api_key_missing"
        report["layers"]["communication"] = layer("blocked", "api_key_missing")
        return report
    if reviewed is not True:
        report["end_reason"] = "current_free_window_and_terms_review_required"
        report["layers"]["communication"] = layer("blocked", report["end_reason"])
        return report
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    stage = "communication"
    try:
        if transport is None:
            import jquantsapi

            validate_live_window(config, datetime.now(UTC).date())
            transport = BoundedTransport(
                config,
                jquantsapi.ClientV2(api_key=os.environ["JQUANTS_API_KEY"].strip()),
            )
            report["live_attempted"] = True
        elif transport.config != config:
            raise ProbeError("transport_config_mismatch")
        master = transport.fetch(ENDPOINTS[0])
        report["layers"]["communication"] = layer(
            "verified", "master_pagination_completed_only"
        )
        stage = "instrument"
        if (
            len(master) != 1
            or not isinstance(master[0].get("CoName"), str)
            or not master[0]["CoName"].strip()
        ):
            raise ProbeError("master_identity_mismatch")
        lot = lot_from_master(
            master[0],
            instrument=config.symbol,
            requested_date=config.master_date,
            effective_end=config.end,
            source="jquants-v2-equities-master",
        )
        report["layers"][stage] = layer(
            "verified", "exact_master_code_and_effective_date"
        )
        master_capture = response_payload(
            dict(schema="stage7b-master-capture-1", data=master)
        )
        save_capture(root, master_capture)
        report["master_capture_hash"] = master_capture.sha256
        assert lot.lot_size is None
        report["layers"]["lot"] = layer(
            "blocked", "master_has_no_verified_lot_evidence"
        )
        stage = "communication"
        records = transport.fetch(ENDPOINTS[1])
        report["acquisition_complete"] = True
        acquired = datetime.now(UTC)
        report["layers"][stage] = layer(
            "verified", "master_and_daily_pagination_completed"
        )
        stage = "data"
        if not records:
            raise ProbeError("empty_daily_response", "blocked")
        observations, dates = [], []
        for row in records:
            day = date.fromisoformat(row["Date"])
            if not config.start <= day < config.end or day in dates:
                raise ProbeError("daily_date_or_duplicate")
            dates.append(day)
            observations.append(
                adapt_daily_response(
                    row,
                    provider="jquants",
                    basis=provider_price_basis("jquants"),
                    response_schema=JQUANTS_RESPONSE_SCHEMA,
                    symbol=config.symbol,
                    session=day,
                    first_observed_at=acquired,
                    fetched_at=acquired,
                    snapshot_hash=response_payload(row).sha256,
                )
            )
        # Local-only value capture; no exception or arbitrary API metadata in report.
        source_capture = response_payload(dict(data=records))
        save_capture(root, source_capture)
        report["daily_source_hash"] = source_capture.sha256
        capture = JsonObject.from_value(
            dict(
                schema="stage7b-daily-capture-1",
                fetched_at=acquired.isoformat(),
                data=[o.to_dict() for o in observations],
            )
        )
        save_capture(root, capture)
        report.update(
            rows=len(records),
            actual_start=min(dates).isoformat(),
            actual_end=max(dates).isoformat(),
            daily_capture_hash=capture.sha256,
        )
        report["quality_counts"] = {
            q: sum(o.to_dict()["quality"] == q for o in observations)
            for q in (
                "reported",
                "missing",
                "invalid",
                "unsupported",
                "no_trade_reported",
            )
        }
        frame = jquants_daily_to_canonical(records, fetched_at=acquired)
        validate_canonical_bars(
            frame,
            expected_provider="jquants",
            requested_symbols={config.symbol},
            start=config.start,
            end=config.end,
        )
        report["layers"][stage] = layer(
            "verified", "canonical_and_daily_observation_contract"
        )
        stage = "calendar"
        try:
            calendar = transport.fetch(ENDPOINTS[2])
            report["layers"][stage] = calendar_check(calendar, config, dates)
            calendar_capture = response_payload(
                dict(schema="stage7b-calendar-capture-1", data=calendar)
            )
            save_capture(root, calendar_capture)
            report["calendar_capture_hash"] = calendar_capture.sha256
        except ProbeError as error:
            report["layers"][stage] = layer(error.status, error.reason)
        stage = "persistence"
        report["snapshot_hashes"] = persist_observations(
            records, observations, frame, acquired, root
        )
        report["layers"][stage] = layer(
            "verified", "stage7a_packet_reload_hash_and_idempotent_publish"
        )
        report["layers"]["public_view"] = layer(
            "verified", "acquisition_time_only_no_backdated_publication"
        )
        report["layers"]["input_extension"] = layer(
            "unsupported", "execution_gate_stopped_before_account_stream"
        )
        report["end_reason"] = "data_probe_finished_execution_unsupported"
    except ProbeError as error:
        report["layers"][stage] = layer(error.status, error.reason)
        report["end_reason"] = error.reason
    except Exception:
        # Never include repr(error), request URLs, response text, or local paths.
        report["layers"][stage] = layer("failed", "contract_validation_failed")
        report["end_reason"] = "contract_validation_failed"
    finally:
        if transport is not None:
            report["attempts"] = transport.attempts
            report["http_attempts"] = transport.audit
            report["elapsed_seconds"] = round(transport.clock() - transport.started, 3)
    return report
