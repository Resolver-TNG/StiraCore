"""Successful page polls stay out of the access log; everything else is still logged (#369)."""

from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.observability import access_log
from stiracore.observability.access_log import (
    ENV,
    PollingAccessFilter,
    install_polling_filter,
    is_polled,
)


def _record(request: str, code: str = "200") -> logging.LogRecord:
    # What werkzeug logs: '"%s" %s %s' with (request line, status, size).
    return logging.LogRecord(
        "werkzeug", logging.INFO, __file__, 1, '"%s" %s %s', (request, code, "-"), None
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/turns?limit=1",
        "/api/turns/abc123/events?after_seq=4",
        "/api/turns/abc123/events?after_seq=17",
        "/api/hitl/pending",
        "/api/hitl/history?limit=50",
        "/api/gateway/status",
        "/api/scheduler",
        "/api/state",
        "/api/state/history?limit=500",
    ],
)
def test_successful_poll_is_dropped(path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert is_polled(path)
    assert PollingAccessFilter().filter(_record(f"GET {path} HTTP/1.1")) is False
    assert PollingAccessFilter().filter(_record(f"GET {path} HTTP/1.1", "304")) is False


@pytest.mark.parametrize(
    ("request_line", "code"),
    [
        ("GET /api/hitl/pending HTTP/1.1", "500"),  # a failing poll is the line to see
        ("GET /api/hitl/pending HTTP/1.1", "403"),
        ("POST /api/hitl/pending HTTP/1.1", "200"),
        ("GET /api/chat/history HTTP/1.1", "200"),  # not a poll
        ("POST /api/chat HTTP/1.1", "200"),
        ("GET /chat HTTP/1.1", "200"),
        ("GET /api/turns/abc/events/../../secret HTTP/1.1", "200"),
        # the same routes read for another purpose (not the polling query) stay in the log
        ("GET /api/turns?limit=200 HTTP/1.1", "200"),
        ("GET /api/turns?limit=100 HTTP/1.1", "200"),
        ("GET /api/turns HTTP/1.1", "200"),
        ("GET /api/turns/abc/events?after_seq=0 HTTP/1.1", "200"),
        ("GET /api/turns/abc/events HTTP/1.1", "200"),
        ("GET /api/hitl/history?limit=500 HTTP/1.1", "200"),
        ("GET /api/hitl/pending?client=x HTTP/1.1", "200"),
        ("GET /api/state/history?limit=1 HTTP/1.1", "200"),
        ("GET /api/gateway/status?verbose=1 HTTP/1.1", "200"),
    ],
)
def test_everything_else_is_logged(request_line, code, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert PollingAccessFilter().filter(_record(request_line, code)) is True


def test_env_restores_the_poll_lines(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    assert PollingAccessFilter().filter(_record("GET /api/hitl/pending HTTP/1.1")) is True


def test_other_records_pass(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    plain = logging.LogRecord("werkzeug", logging.INFO, __file__, 1, "WARNING: x", None, None)
    assert PollingAccessFilter().filter(plain) is True


def test_install_is_idempotent():
    first = install_polling_filter()
    try:
        assert install_polling_filter() is first
        count = sum(
            isinstance(f, PollingAccessFilter) for f in logging.getLogger("werkzeug").filters
        )
        assert count == 1
    finally:
        logging.getLogger("werkzeug").removeFilter(first)


def test_real_werkzeug_access_log(monkeypatch):
    """Through the real server: polls of a fresh server vanish, a 404 poll and a page stay."""
    from werkzeug.serving import make_server

    monkeypatch.delenv(ENV, raising=False)
    app = Flask(__name__)

    @app.get("/api/hitl/pending")
    def pending():
        return {"requests": []}

    @app.get("/api/gateway/status")
    def status():
        return "boom", 500

    @app.get("/chat")
    def chat():
        return "ok"

    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    capture = Capture()
    logger = logging.getLogger("werkzeug")
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    installed = install_polling_filter()
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for path in ("/api/hitl/pending", "/chat", "/api/gateway/status"):
            try:
                urllib.request.urlopen(base + path, timeout=5).read()
            except urllib.error.HTTPError:
                pass
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
        logger.removeHandler(capture)
        logger.removeFilter(installed)
    text = "\n".join(lines)
    assert "/api/hitl/pending" not in text
    assert "GET /chat" in text
    assert "GET /api/gateway/status" in text and "500" in text


def test_stira_installs_the_filter():
    from dashboard import stira_server

    assert stira_server.install_polling_filter is access_log.install_polling_filter
