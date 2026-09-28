"""Bounded HTTP transport for *artificial loopback* feasibility trials.

This is deliberately not a live J-Quants transport.  It accepts only a
numeric loopback address, never reads credentials, and cannot be used as a
permission token for :mod:`http_contract`'s closed live-acquisition gate.
The caller owns the partial-file sink and the durable audit transitions.

``transfer_bytes`` measures compressed HTTP *entity-body* bytes (not headers
or chunk framing). ``decompressed_bytes`` measures decoded body bytes;
``persisted_bytes`` measures bytes accepted by the caller's sink.
"""

from __future__ import annotations

import hashlib
import http.client
import math
import socket
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from io import BufferedIOBase
from urllib.parse import urlencode, urlsplit

from .http_contract import (
    MAX_WAIT_SECONDS,
    HttpAcquisitionPlan,
    PageRequest,
    validate_page_request,
)


class HttpTransportError(RuntimeError):
    """Fail-closed local attempt with the evidence known at the failure point.

    A known HTTP status is not silently reclassified as a network ``unknown``.
    A caller must preserve a partial sink and decide which journal evidence can
    be written; this exception is not a successful response or permission.
    """

    def __init__(
        self,
        reason: str,
        *,
        status_code: int | None = None,
        observed_retry_after_seconds: int | None = None,
        transfer_bytes: int = 0,
        decompressed_bytes: int = 0,
        persisted_bytes: int = 0,
        elapsed_seconds: float = 0.0,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.observed_retry_after_seconds = observed_retry_after_seconds
        self.transfer_bytes = transfer_bytes
        self.decompressed_bytes = decompressed_bytes
        self.persisted_bytes = persisted_bytes
        self.elapsed_seconds = elapsed_seconds


@dataclass(frozen=True, slots=True)
class HttpObservation:
    status_code: int
    status_class: str
    observed_retry_after_seconds: int | None
    retry_after_issue: str | None
    transfer_bytes: int
    decompressed_bytes: int
    persisted_bytes: int
    body_sha256: str
    elapsed_seconds: float


def _positive_seconds(value: object, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise HttpTransportError(f"{name}_must_be_positive_finite")
    return float(value)


def _allowance(value: object, maximum: int, name: str) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        raise HttpTransportError(f"{name}_outside_plan_page_cap")
    return value


def _retry_after(value: str | None, now: datetime) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    if not value or value.strip() != value:
        return None, "malformed_retry_after"
    if value.isascii() and value.isdecimal():
        # int() has a configurable digit limit; an adversarial server value
        # must remain a classified 429, never an uncaught parser exception.
        significant = value.lstrip("0") or "0"
        if len(significant) > len(str(MAX_WAIT_SECONDS)):
            return None, "retry_after_out_of_range"
        seconds = int(significant)
    else:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None, "malformed_retry_after"
        if parsed.tzinfo is None:
            return None, "malformed_retry_after"
        seconds = math.ceil(max(0.0, (parsed - now).total_seconds()))
    if seconds > MAX_WAIT_SECONDS:
        return None, "retry_after_out_of_range"
    return seconds, None


@dataclass(frozen=True, slots=True)
class LocalhostHttpTransport:
    """One-request-at-a-time artificial HTTP client with no retry or redirect.

    Only ``http://127.0.0.1:<port>`` and ``http://[::1]:<port>`` are accepted.
    An actual HTTPS/J-Quants transport is a separate, unimplemented boundary.
    """

    base_url: str

    def __post_init__(self) -> None:
        if type(self.base_url) is not str:
            raise HttpTransportError("loopback_url_required")
        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port
        except ValueError:
            raise HttpTransportError("loopback_url_required") from None
        if (
            parsed.scheme != "http"
            or parsed.hostname not in ("127.0.0.1", "::1")
            or port is None
            or not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise HttpTransportError("loopback_url_required")

    def fetch(
        self,
        plan: HttpAcquisitionPlan,
        page: PageRequest,
        *,
        sink: BufferedIOBase,
        allowed_transfer_bytes: int,
        allowed_decoded_bytes: int,
        allowed_saved_bytes: int,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        total_timeout_seconds: float,
        now: Callable[[], datetime] | None = None,
    ) -> HttpObservation:
        """Stream a fixed GET into ``sink`` under all three per-attempt caps.

        The caller must obtain exact allowances from fresh journal/inventory
        state, hold the current fenced account slot, and recheck both ledgers at
        send time. This method intentionally cannot satisfy the closed live
        Gate, perform reservation, or save/commit the body by itself.
        """

        if type(self) is not LocalhostHttpTransport:
            raise HttpTransportError("concrete_localhost_transport_required")
        # A frozen dataclass can still be altered with object.__setattr__; do
        # not trust constructor-time validation alone at the send boundary.
        LocalhostHttpTransport.__post_init__(self)
        if type(plan) is not HttpAcquisitionPlan or type(page) is not PageRequest:
            raise HttpTransportError("fixed_plan_and_page_required")
        plan.verify_fixed_scope()
        validate_page_request(plan, page)
        if not callable(getattr(sink, "write", None)):
            raise HttpTransportError("binary_sink_required")
        caps = (
            _allowance(
                allowed_transfer_bytes,
                plan.limits.max_page_transfer_bytes,
                "allowed_transfer_bytes",
            ),
            _allowance(
                allowed_decoded_bytes,
                plan.limits.max_page_decoded_bytes,
                "allowed_decoded_bytes",
            ),
            _allowance(
                allowed_saved_bytes,
                plan.limits.max_page_saved_bytes,
                "allowed_saved_bytes",
            ),
        )
        connect_timeout = _positive_seconds(connect_timeout_seconds, "connect_timeout")
        read_timeout = _positive_seconds(read_timeout_seconds, "read_timeout")
        total_timeout = _positive_seconds(total_timeout_seconds, "total_timeout")
        if total_timeout > plan.retry.timeout_seconds:
            raise HttpTransportError("total_timeout_exceeds_plan_rule")
        clock = now if now is not None else lambda: datetime.now(UTC)
        if not callable(clock):
            raise HttpTransportError("utc_clock_required")

        parsed = urlsplit(self.base_url)
        params = page.query.params()
        if page.pagination_key is not None:
            params["pagination_key"] = page.pagination_key
        target = f"{page.query.endpoint}?{urlencode(params)}"
        started = time.monotonic()
        deadline = started + total_timeout
        status: int | None = None
        retry_after: int | None = None
        retry_issue: str | None = None
        transferred = decoded_total = persisted = 0
        digest = hashlib.sha256()
        connection: http.client.HTTPConnection | None = None
        response: http.client.HTTPResponse | None = None
        watchdog: threading.Timer | None = None
        deadline_fired = threading.Event()

        def elapsed() -> float:
            return time.monotonic() - started

        def failure(reason: str) -> HttpTransportError:
            return HttpTransportError(
                reason,
                status_code=status,
                observed_retry_after_seconds=retry_after,
                transfer_bytes=transferred,
                decompressed_bytes=decoded_total,
                persisted_bytes=persisted,
                elapsed_seconds=elapsed(),
            )

        def remaining() -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise failure("total_timeout")
            return left

        def expired() -> bool:
            return deadline_fired.is_set() or time.monotonic() >= deadline

        def write_decoded(chunk: bytes) -> None:
            nonlocal decoded_total, persisted
            decoded_total += len(chunk)
            if decoded_total > caps[1]:
                raise failure("decoded_budget_exceeded")
            if persisted + len(chunk) > caps[2]:
                raise failure("saved_budget_exceeded")
            if not chunk:
                return
            try:
                written = sink.write(chunk)
            except (OSError, TypeError, ValueError):
                raise failure("sink_write_failed") from None
            if type(written) is int and 0 <= written <= len(chunk):
                persisted += written
                digest.update(chunk[:written])
            if written != len(chunk):
                raise failure("sink_partial_write")

        try:
            connection = http.client.HTTPConnection(
                parsed.hostname, parsed.port, timeout=min(connect_timeout, remaining())
            )
            connection.connect()
            active_socket = connection.sock

            # http.client parses headers and chunk framing with readline().
            # A peer can drip bytes fast enough to reset the socket timeout
            # indefinitely inside that single call. A separate monotonic
            # deadline must interrupt the underlying socket itself.
            def interrupt_at_deadline() -> None:
                deadline_fired.set()
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            watchdog = threading.Timer(remaining(), interrupt_at_deadline)
            watchdog.daemon = True
            watchdog.start()
            active_socket.settimeout(min(read_timeout, remaining()))
            # Explicitly forbid implicit credentials, redirects and connection
            # reuse. http.client performs no retry or redirect on its own.
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "close",
                },
            )
            active_socket.settimeout(min(read_timeout, remaining()))
            response = connection.getresponse()
            status = response.status
            if not 100 <= status <= 599:
                raise failure("malformed_response")
            observed_at = clock()
            if type(observed_at) is not datetime or observed_at.tzinfo is None:
                raise failure("utc_clock_required")
            values = response.getheaders()
            retry_values = [
                value for key, value in values if key.lower() == "retry-after"
            ]
            if len(retry_values) > 1:
                retry_issue = "malformed_retry_after"
            else:
                retry_after, retry_issue = _retry_after(
                    retry_values[0] if retry_values else None,
                    observed_at.astimezone(UTC),
                )
            if 300 <= status < 400:
                raise failure("redirect_forbidden")
            encodings = [
                value.lower()
                for key, value in values
                if key.lower() == "content-encoding"
            ]
            if len(encodings) > 1 or (
                encodings and encodings[0] not in ("identity", "gzip", "deflate")
            ):
                raise failure("unsupported_content_encoding")
            encoding = encodings[0] if encodings else "identity"
            decoder = (
                zlib.decompressobj(16 + zlib.MAX_WBITS)
                if encoding == "gzip"
                else zlib.decompressobj()
                if encoding == "deflate"
                else None
            )
            lengths = [
                value for key, value in values if key.lower() == "content-length"
            ]
            transfer_encodings = [
                value.lower()
                for key, value in values
                if key.lower() == "transfer-encoding"
            ]
            if len(transfer_encodings) > 1 or (
                transfer_encodings and transfer_encodings[0] != "chunked"
            ):
                raise failure("unsupported_transfer_encoding")
            if len(lengths) > 1 or (
                lengths and (not lengths[0].isascii() or not lengths[0].isdecimal())
            ):
                raise failure("invalid_content_length")
            if lengths:
                significant_length = lengths[0].lstrip("0") or "0"
                # Avoid Python's maximum-decimal-digit parser exception. An
                # extreme length is not useful to a bounded attempt anyway.
                if len(significant_length) > 18:
                    raise failure("invalid_content_length")
                declared_length = int(significant_length)
            else:
                declared_length = None
            if response.chunked and declared_length is not None:
                raise failure("ambiguous_body_framing")
            if response.fp is None:
                raise failure("body_stream_missing")
            while True:
                # Content-Length is the HTTP framing boundary. Never wait for
                # EOF on a keep-alive connection after these bytes arrive, and
                # never consume bytes beyond it as part of this response.
                if declared_length is not None and transferred == declared_length:
                    break
                active_socket.settimeout(min(read_timeout, remaining()))
                # Read from the underlying stream so close-delimited responses
                # still use actual EOF. For length-delimited responses the
                # requested read is capped by the remaining declared bytes.
                stream = response if response.chunked else response.fp
                read_size = min(65_536, caps[0] - transferred + 1)
                if declared_length is not None:
                    read_size = min(read_size, declared_length - transferred)
                raw = stream.read1(read_size)
                if raw == b"":
                    break
                transferred += len(raw)
                if transferred > caps[0]:
                    raise failure("transfer_budget_exceeded")
                if decoder is None:
                    write_decoded(raw)
                else:
                    pending = raw
                    while pending:
                        output = decoder.decompress(
                            pending,
                            min(caps[1] - decoded_total, caps[2] - persisted) + 1,
                        )
                        write_decoded(output)
                        next_pending = decoder.unconsumed_tail
                        if next_pending == pending and not output:
                            raise failure("malformed_compressed_body")
                        pending = next_pending
                remaining()  # a trickling stream never extends the total deadline
            if declared_length is not None and transferred != declared_length:
                raise failure("content_length_mismatch")
            if decoder is not None:
                output = decoder.flush(
                    min(caps[1] - decoded_total, caps[2] - persisted) + 1
                )
                write_decoded(output)
                if not decoder.eof or decoder.unused_data:
                    raise failure("malformed_compressed_body")
            remaining()
            observation = HttpObservation(
                status_code=status,
                status_class=f"{status // 100}xx",
                observed_retry_after_seconds=retry_after,
                retry_after_issue=retry_issue,
                transfer_bytes=transferred,
                decompressed_bytes=decoded_total,
                persisted_bytes=persisted,
                body_sha256=digest.hexdigest(),
                elapsed_seconds=elapsed(),
            )
            if expired():
                raise failure("total_timeout")
            return observation
        except HttpTransportError as exc:
            if deadline_fired.is_set() and exc.reason != "total_timeout":
                raise failure("total_timeout") from None
            raise
        except TimeoutError:
            raise failure("total_timeout" if expired() else "network_timeout") from None
        except (ConnectionResetError, BrokenPipeError):
            raise failure(
                "total_timeout" if expired() else "connection_reset"
            ) from None
        except (http.client.HTTPException, EOFError):
            raise failure(
                "total_timeout" if expired() else "malformed_response"
            ) from None
        except zlib.error:
            raise failure("malformed_compressed_body") from None
        except OSError:
            raise failure("total_timeout" if expired() else "network_error") from None
        finally:
            if watchdog is not None:
                watchdog.cancel()
                watchdog.join()
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
