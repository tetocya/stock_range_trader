"""Loopback-only artificial tests; no API key or external network is used."""

from __future__ import annotations

import gzip
import hashlib
import io
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from feasibility.http_contract import (
    HTTP_OUTPUT_ROOT,
    HttpAcquisitionPlan,
    HttpLimits,
    PageRequest,
    RetryRules,
)
from feasibility.http_transport import (
    HttpTransportError,
    LocalhostHttpTransport,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)


def _plan() -> HttpAcquisitionPlan:
    return HttpAcquisitionPlan(
        artifact_id="artificial-http-transport",
        kind="calendar_discovery",
        reference_date="2025-03-04",
        calendar_start="2025-03-03",
        calendar_end="2025-03-04",
        master_date=None,
        daily_dates=(),
        calendar_source_sha256=None,
        calendar_source_reference=None,
        output_dir=str(HTTP_OUTPUT_ROOT / "artificial-http-transport"),
        not_before=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        limits=HttpLimits(
            max_attempts=3,
            max_pages_total=3,
            max_pages_per_query=3,
            max_elapsed_seconds=300,
            max_transfer_bytes=10_000,
            max_decoded_bytes=20_000,
            max_saved_bytes=20_000,
            max_page_transfer_bytes=10_000,
            max_page_decoded_bytes=20_000,
            max_page_saved_bytes=20_000,
        ),
        retry=RetryRules(
            max_attempts_per_page=2,
            min_interval_seconds=13,
            min_wait_after_429_seconds=120,
            min_wait_after_5xx_seconds=13,
            min_wait_after_network_error_seconds=13,
            timeout_seconds=5,
        ),
        account_ref="artificial-account",
    )


@contextmanager
def _server(reply):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            reply(self)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _respond(handler, body: bytes, *, status: int = 200, headers=()):
    handler.send_response(status)
    for key, value in headers:
        handler.send_header(key, value)
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


def _fetch(url, *, plan=None, sink=None, **changes):
    fixed = plan or _plan()
    options = dict(
        sink=sink if sink is not None else io.BytesIO(),
        allowed_transfer_bytes=10_000,
        allowed_decoded_bytes=20_000,
        allowed_saved_bytes=20_000,
        connect_timeout_seconds=1,
        read_timeout_seconds=1,
        total_timeout_seconds=2,
        now=lambda: NOW,
    )
    options.update(changes)
    return LocalhostHttpTransport(url).fetch(
        fixed, PageRequest(fixed.queries[0], 0), **options
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://api.jquants.com:443",
        "http://localhost:8080",
        "http://127.0.0.2:8080",
        "http://example.com:8080",
        "http://127.0.0.1:8080/path",
        "http://user:secret@127.0.0.1:8080",
        "http://127.0.0.1:8080?x=1",
        "file:///tmp/artifact",
    ],
)
def test_nonliteral_loopback_or_credential_url_is_rejected_before_socket(url):
    with pytest.raises(HttpTransportError, match="loopback_url_required"):
        LocalhostHttpTransport(url)


def test_changed_base_url_is_rechecked_before_socket():
    transport = LocalhostHttpTransport("http://127.0.0.1:1234")
    object.__setattr__(transport, "base_url", "http://example.com:443")
    fixed = _plan()
    with pytest.raises(HttpTransportError, match="loopback_url_required"):
        transport.fetch(
            fixed,
            PageRequest(fixed.queries[0], 0),
            sink=io.BytesIO(),
            allowed_transfer_bytes=100,
            allowed_decoded_bytes=100,
            allowed_saved_bytes=100,
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            total_timeout_seconds=1,
        )


def test_small_200_streams_to_sink_and_has_no_auth_header():
    body = b'{"data":[]}'
    seen = []

    def reply(handler):
        seen.append((handler.path, handler.headers.get("Authorization")))
        _respond(handler, body)

    with _server(reply) as url:
        sink = io.BytesIO()
        result = _fetch(url, sink=sink)
    assert result.status_code == 200
    assert result.status_class == "2xx"
    assert (
        result.transfer_bytes
        == result.decompressed_bytes
        == result.persisted_bytes
        == len(body)
    )
    assert result.body_sha256 == hashlib.sha256(body).hexdigest()
    assert sink.getvalue() == body
    assert seen == [("/markets/calendar?from=2025-03-03&to=2025-03-04", None)]


def test_gzip_counts_compressed_and_decoded_entity_bytes_separately():
    body = b"A" * 500
    compressed = gzip.compress(body)
    with _server(
        lambda handler: _respond(
            handler,
            compressed,
            headers=(
                ("Content-Encoding", "gzip"),
                ("Content-Length", str(len(compressed))),
            ),
        )
    ) as url:
        sink = io.BytesIO()
        result = _fetch(url, sink=sink)
    assert (
        result.transfer_bytes,
        result.decompressed_bytes,
        result.persisted_bytes,
    ) == (
        len(compressed),
        len(body),
        len(body),
    )
    assert sink.getvalue() == body


def test_chunked_body_counts_entity_bytes_not_chunk_framing():
    def reply(handler):
        handler.send_response(200)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(b"5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n")

    with _server(reply) as url:
        sink = io.BytesIO()
        result = _fetch(url, sink=sink)
    assert sink.getvalue() == b"helloworld"
    assert result.transfer_bytes == 10


def test_unsupported_transfer_encoding_is_rejected_with_known_status():
    with _server(
        lambda handler: _respond(
            handler, b"body", headers=(("Transfer-Encoding", "gzip"),)
        )
    ) as url:
        with pytest.raises(
            HttpTransportError, match="unsupported_transfer_encoding"
        ) as caught:
            _fetch(url)
    assert caught.value.status_code == 200


@pytest.mark.parametrize("declared", [1, 100])
def test_false_content_length_is_rejected_from_measured_bytes(declared):
    body = b"1234567890"
    with _server(
        lambda handler: _respond(
            handler, body, headers=(("Content-Length", str(declared)),)
        )
    ) as url:
        with pytest.raises(
            HttpTransportError, match="content_length_mismatch"
        ) as caught:
            _fetch(url)
    assert caught.value.status_code == 200
    assert caught.value.transfer_bytes == len(body)


def test_giant_content_length_fails_closed_without_decimal_parser_exception():
    with _server(
        lambda handler: _respond(
            handler, b"body", headers=(("Content-Length", "9" * 5000),)
        )
    ) as url:
        with pytest.raises(
            HttpTransportError, match="invalid_content_length"
        ) as caught:
            _fetch(url)
    assert caught.value.status_code == 200


def test_redirect_is_not_followed_even_to_loopback():
    seen = []

    def reply(handler):
        seen.append(handler.path)
        _respond(handler, b"", status=302, headers=(("Location", "/other"),))

    with _server(reply) as url:
        with pytest.raises(HttpTransportError, match="redirect_forbidden") as caught:
            _fetch(url)
    assert caught.value.status_code == 302
    assert len(seen) == 1


@pytest.mark.parametrize(
    "header,expected,issue",
    [
        ("600", 600, None),
        (format_datetime(NOW + timedelta(seconds=600), usegmt=True), 600, None),
        ("not-a-date", None, "malformed_retry_after"),
        ("86401", None, "retry_after_out_of_range"),
        ("9" * 5000, None, "retry_after_out_of_range"),
        (None, None, None),
    ],
)
def test_429_retains_known_status_and_retry_after_evidence(header, expected, issue):
    headers = (("Retry-After", header),) if header is not None else ()
    with _server(
        lambda handler: _respond(handler, b"slow", status=429, headers=headers)
    ) as url:
        result = _fetch(url)
    assert result.status_code == 429
    assert result.observed_retry_after_seconds == expected
    assert result.retry_after_issue == issue


def test_503_is_a_known_response_not_unknown():
    with _server(lambda handler: _respond(handler, b"busy", status=503)) as url:
        result = _fetch(url)
    assert result.status_code == 503
    assert result.status_class == "5xx"


def test_transfer_cap_cuts_stream_after_first_over_limit_byte():
    with _server(lambda handler: _respond(handler, b"1234567890")) as url:
        with pytest.raises(
            HttpTransportError, match="transfer_budget_exceeded"
        ) as caught:
            _fetch(url, allowed_transfer_bytes=5)
    assert caught.value.transfer_bytes == 6
    assert caught.value.persisted_bytes == 0


def test_compression_expansion_and_saved_cap_stop_before_persisting():
    compressed = gzip.compress(b"A" * 5000)
    with _server(
        lambda handler: _respond(
            handler, compressed, headers=(("Content-Encoding", "gzip"),)
        )
    ) as url:
        sink = io.BytesIO()
        with pytest.raises(
            HttpTransportError, match="decoded_budget_exceeded"
        ) as caught:
            _fetch(url, sink=sink, allowed_decoded_bytes=20)
        assert sink.getvalue() == b""
        assert caught.value.decompressed_bytes == 21
        with pytest.raises(HttpTransportError, match="saved_budget_exceeded") as caught:
            _fetch(url, allowed_saved_bytes=20)
        assert caught.value.decompressed_bytes == 21
        assert caught.value.persisted_bytes == 0


def test_partial_sink_write_is_not_a_completed_body():
    class PartialSink(io.BytesIO):
        def write(self, data):
            return super().write(data[:2])

    with _server(lambda handler: _respond(handler, b"12345")) as url:
        sink = PartialSink()
        with pytest.raises(HttpTransportError, match="sink_partial_write") as caught:
            _fetch(url, sink=sink)
    assert sink.getvalue() == b"12"
    assert caught.value.persisted_bytes == 2


def test_invalid_gzip_does_not_produce_a_successful_observation():
    with _server(
        lambda handler: _respond(
            handler, b"not-gzip", headers=(("Content-Encoding", "gzip"),)
        )
    ) as url:
        with pytest.raises(
            HttpTransportError, match="malformed_compressed_body"
        ) as caught:
            _fetch(url)
    assert caught.value.status_code == 200


def test_invalid_status_line_is_not_a_known_http_result():
    def reply(handler):
        handler.connection.sendall(b"NOT HTTP\r\n\r\n")

    with _server(reply) as url:
        with pytest.raises(HttpTransportError, match="malformed_response") as caught:
            _fetch(url)
    assert caught.value.status_code is None


def test_incomplete_body_timeout_preserves_observed_header_status():
    def reply(handler):
        handler.send_response(200)
        handler.send_header("Connection", "close")
        handler.end_headers()
        time.sleep(0.2)

    with _server(reply) as url:
        with pytest.raises(HttpTransportError, match="network_timeout") as caught:
            _fetch(url, read_timeout_seconds=0.05, total_timeout_seconds=0.5)
    assert caught.value.status_code == 200  # headers were already observed


def test_total_deadline_is_not_extended_by_trickling_body():
    def reply(handler):
        handler.send_response(200)
        handler.send_header("Connection", "close")
        handler.end_headers()
        for _ in range(8):
            try:
                handler.wfile.write(b"x")
                handler.wfile.flush()
            except BrokenPipeError:
                break
            time.sleep(0.04)

    with _server(reply) as url:
        with pytest.raises(HttpTransportError, match="total_timeout"):
            _fetch(url, read_timeout_seconds=0.2, total_timeout_seconds=0.1)


def test_total_deadline_interrupts_slow_drip_inside_header_readline():
    def reply(handler):
        handler.connection.sendall(b"HTTP/1.1 200 OK\r\n")
        for byte in b"X-Drip: 1234567890\r\n\r\n":
            try:
                handler.connection.sendall(bytes((byte,)))
            except OSError:
                break
            time.sleep(0.03)

    with _server(reply) as url:
        with pytest.raises(HttpTransportError, match="total_timeout") as caught:
            _fetch(url, read_timeout_seconds=0.5, total_timeout_seconds=0.12)
    assert caught.value.elapsed_seconds < 0.35


def test_total_deadline_interrupts_slow_drip_inside_chunk_readline():
    def reply(handler):
        handler.connection.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n"
        )
        for byte in b"1;" + b"x" * 20 + b"\r\na\r\n0\r\n\r\n":
            try:
                handler.connection.sendall(bytes((byte,)))
            except OSError:
                break
            time.sleep(0.03)

    with _server(reply) as url:
        with pytest.raises(HttpTransportError, match="total_timeout") as caught:
            _fetch(url, read_timeout_seconds=0.5, total_timeout_seconds=0.12)
    assert caught.value.status_code == 200
    assert caught.value.elapsed_seconds < 0.35


def test_connection_reset_is_fail_closed():
    def reply(handler):
        handler.connection.shutdown(socket.SHUT_RDWR)
        handler.connection.close()

    with _server(reply) as url:
        with pytest.raises(HttpTransportError) as caught:
            _fetch(url)
    assert caught.value.status_code is None
    assert caught.value.reason == "connection_reset"
