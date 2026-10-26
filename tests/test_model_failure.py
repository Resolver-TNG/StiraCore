"""Model-call failures by kind against real HTTP endpoints (llama.cpp client, no Bedrock)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from stiracore.providers.failure import (
    MESSAGES,
    REASONS,
    describe_model_failure,
    endpoint_host,
    model_banner,
)

SECRET = "sk-THIS-MUST-NOT-LEAK"


class _Handler(BaseHTTPRequestHandler):
    status = 500

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.server.mode == "sleep":
            time.sleep(3)
        body = json.dumps({"error": {"message": f"echo {SECRET}"}}).encode()
        self.send_response(self.server.mode)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def endpoint():
    servers = []

    def start(mode):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.mode = mode
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _fail(base_url, timeout=10.0):
    from strands.models.llamacpp import LlamaCppModel

    model = LlamaCppModel(base_url=base_url, timeout=timeout, model_id="x")
    agent = SimpleNamespace(provider="llamacpp", model=model, model_spec=None)

    async def run():
        async for _ in model.stream([{"role": "user", "content": [{"text": SECRET}]}]):
            pass

    with pytest.raises(Exception) as caught:
        asyncio.run(run())
    return describe_model_failure(caught.value, agent)


@pytest.mark.parametrize(
    ("mode", "reason", "status"),
    [
        (401, "auth_failed", 401),
        (403, "auth_failed", 403),
        (404, "model_not_found", 404),
        (429, "throttled", 429),
        (500, "server_error", 500),
        # llama.cpp client: 503 (model loading / busy) is raised as throttling by Strands.
        (503, "throttled", 503),
    ],
)
def test_http_status_kinds(endpoint, mode, reason, status):
    detail = _fail(endpoint(mode))
    assert detail["code"] == "model_failure"
    assert detail["reason"] == reason
    assert detail.get("http_status") in (status, None)
    assert detail["provider"] == "llamacpp"
    assert detail["host"].startswith("127.0.0.1:")
    assert SECRET not in json.dumps(detail)


def test_connection_refused():
    port = _free_port()
    detail = _fail(f"http://127.0.0.1:{port}")
    assert detail["reason"] == "refused"
    assert detail["host"] == f"127.0.0.1:{port}"
    assert MESSAGES["refused"][0] in detail["error"]


def test_timeout(endpoint):
    detail = _fail(endpoint("sleep"), timeout=0.5)
    assert detail["reason"] == "timeout"


def test_dns_failure():
    detail = _fail("http://stira-no-such-host.invalid:8080")
    assert detail["reason"] == "dns_failed"
    assert detail["host"] == "stira-no-such-host.invalid:8080"


def test_missing_credential_and_unknown():
    from stiracore.providers.credentials import MissingCredentialError

    exc = MissingCredentialError.__new__(MissingCredentialError)
    Exception.__init__(exc, "x")
    assert describe_model_failure(exc)["reason"] == "missing_credential"
    assert describe_model_failure(ValueError(SECRET)) is None


def test_host_has_no_path_or_user_info():
    assert endpoint_host("openai", "https://user:pw@api.example.com/v1?key=1") == (
        "api.example.com"
    )
    assert endpoint_host("llamacpp", None) == "localhost:8080"
    assert endpoint_host("bedrock", None) is None


def test_every_reason_has_both_languages_in_catalogs():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "dashboard/static/i18n"
    for lang in ("ja", "en"):
        catalog = json.loads((root / f"catalog.{lang}.json").read_text())
        for reason in REASONS:
            text = catalog[f"chat.model_failure_{reason}"]
            assert text and "http" not in text
    assert set(MESSAGES) == set(REASONS)


def test_banner_names_the_real_provider():
    llama = SimpleNamespace(
        provider="llamacpp",
        model_id="global.anthropic.claude-sonnet-5",
        model_spec=SimpleNamespace(options={"base_url": "http://127.0.0.1:9999"}),
    )
    line = model_banner(llama, "global.anthropic.claude-sonnet-5", "bedrock")
    assert line.startswith("llamacpp (127.0.0.1:9999")
    assert "anthropic" not in line
    bedrock = SimpleNamespace(
        provider="bedrock", model_id="m1", region_name="ap-northeast-1", model_spec=None
    )
    assert model_banner(bedrock, "x", "bedrock") == "m1 (bedrock, ap-northeast-1)"


def _sse(body):
    events = []
    for block in body.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_chat_turn_reports_the_kind_and_the_turn_record_keeps_it(tmp_path):
    """The real server on a model that cannot connect: SSE error, turn.completed, turn view."""
    from dashboard import stira_server
    from tests.provider_fakes import FakeModel

    error = ConnectionRefusedError(61, f"Connection refused {SECRET}")
    model = FakeModel([[{"raise": error}]])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
        patch.setattr(stira_server, "model_kwargs", lambda *a, **k: {"model": model})
        app = stira_server.create_app()
        client = app.test_client()
        body = client.post("/api/chat", json={"message": "hi"}).get_data(as_text=True)
        turns = client.get("/api/turns").json["turns"]
        events = client.get(f"/api/turns/{turns[-1]['turn_id']}/events").json["events"]
    errors = [data for event, data in _sse(body) if event == "error"]
    assert len(errors) == 1
    assert errors[0]["code"] == "model_failure"
    assert errors[0]["reason"] == "refused"
    assert SECRET not in body
    completed = [
        d["data"] for e, d in _sse(body) if e == "ui_event" and d["type"] == "turn.completed"
    ]
    assert completed and completed[-1]["failure"] == "refused"
    assert completed[-1]["stop"] == "error" and completed[-1]["error"] is True
    ends = [e for e in events if e.get("type") == "turn.completed"]
    assert ends and ends[-1]["data"]["failure"] == "refused"
    # The observability page's line names the kind too (no text from the exception).
    from stiracore.observability.turns_view import page_event

    assert "refused" in page_event(1, ends[-1])["message"]
    assert SECRET not in json.dumps(events)
