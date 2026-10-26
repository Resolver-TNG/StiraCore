"""#495: Stira's URL tools never reach the dashboard they run under — with or without YOLO.

Measured before the fix: with ``http_request`` set to Always (no ``STIRA_YOLO_ALLOW``) the
agent's tool read ``/api/csrf`` and could post to the settings API of its own dashboard
(the guard of ADR 0012 §8b was armed only with the allowance).

Real sockets on loopback, no external network. A local HTTP server stands in for the
dashboard and counts the requests it gets. Each check is pinned on its own (the others
switched off), so removing any one of them fails a test:

* the tool's URL check before anything is sent (``url_refusal`` in the tool gate);
* the redirect hop check (``install_redirect_check``);
* the connect hook (``own_dashboard``), for a second DNS answer (rebinding);
* the server arms the guard at every start (``stira_server.main``), not only with YOLO.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import pytest
import requests

from stiracore.yolo import own_dashboard
from stiracore.yolo.own_dashboard import REASON_CODE, Target, url_refusal
from tests.test_yolo_own_dashboard import _local_non_loopback_address, _serve

pytest.importorskip("strands_tools.http_request")

from stiracore.tools import own_dashboard_gate, tool_registry

PUBLIC = "93.184.216.34"  # a literal public address: resolves without any network


class _Counted:
    """A loopback server that counts what reaches it (the 'dashboard')."""

    def __init__(self, redirect_to=None):
        self.server = _serve(redirect_to=redirect_to)
        self.port = self.server.server_address[1]
        self.hits = 0
        handler = self.server.RequestHandlerClass
        original = handler.do_GET
        outer = self

        def counted(handler_self):
            outer.hits += 1
            original(handler_self)

        handler.do_GET = counted
        handler.do_POST = counted

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def dashboard():
    server = _Counted()
    try:
        yield server
    finally:
        own_dashboard.disarm()
        server.close()


@pytest.fixture
def armed(dashboard):
    own_dashboard.arm("127.0.0.1", dashboard.port)
    return dashboard


@pytest.fixture
def hook_off(monkeypatch):
    """The connect hook answers 'not refused': only the tool-side checks are left."""
    monkeypatch.setattr(own_dashboard, "_refuses", lambda host, port, target: False)


def _tool():
    return tool_registry.WRITE_TOOLS["http_request"]


def _run(url, method="GET", **extra):
    tool_use = {"toolUseId": "t-1", "name": "http_request", "input": {"method": method, "url": url}}
    tool_use["input"].update(extra)

    async def collect():
        return [event async for event in _tool().stream(tool_use, {})]

    events = asyncio.run(collect())
    result = events[-1].tool_result
    text = " ".join(block.get("text", "") for block in result.get("content") or ())
    return result.get("status"), text


def _host_forms():
    """Every way to write the dashboard's host (127.0.0.1 listener), measured."""
    return [
        "127.0.0.1",
        "localhost",
        "LOCALHOST",
        "localhost.",
        "127.1",
        "127.0.1",
        "0x7f.1",
        "0x7f000001",
        "2130706433",
        "127.0.0.2",
        "0.0.0.0",
        "0",
        "[::1]",
        "[::ffff:127.0.0.1]",
        "[::ffff:7f00:1]",
        "[::]",
        "[0:0:0:0:0:0:0:1]",
        "rebind.example",  # a DNS name that answers loopback (patched resolver)
        socket.gethostname(),  # this host's own name
    ]


@pytest.fixture
def rebind_dns(monkeypatch):
    real = socket.getaddrinfo

    def answer(host, *args, **kwargs):
        if host in ("rebind.example", b"rebind.example"):
            return real("127.0.0.1", *args, **kwargs)
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", answer)


# --- 1. the URL check (pure) ------------------------------------------------------------


@pytest.mark.parametrize("host", _host_forms())
def test_every_form_of_the_dashboard_host_is_refused_by_the_url_check(rebind_dns, host):
    target = own_dashboard.target_for("127.0.0.1", 5797)
    for url in (f"http://{host}:5797/api/csrf", f"https://{host}:5797/", f"HTTP://{host}:05797"):
        reason = url_refusal(url, target)
        assert reason is not None and reason.startswith(REASON_CODE), (url, reason)


def test_a_leading_zero_form_follows_the_system_resolver():
    """``0177.0.0.1`` is octal 127.0.0.1 for glibc but decimal 177.0.0.1 on macOS
    (measured): the check resolves like the HTTP client does, so it agrees with it."""
    target = own_dashboard.target_for("127.0.0.1", 5797)
    resolved = {info[4][0] for info in socket.getaddrinfo("0177.0.0.1", None)}
    local = any(ipaddress.ip_address(a).is_loopback for a in resolved)
    assert (url_refusal("http://0177.0.0.1:5797/", target) is not None) == local


def test_the_url_check_reads_userinfo_and_default_ports_like_the_client():
    target = own_dashboard.target_for("127.0.0.1", 80)
    assert url_refusal("http://example.org@127.0.0.1/api/csrf", target)
    assert url_refusal("http://127.0.0.1/api/csrf", target)  # default port 80
    assert url_refusal("https://127.0.0.1/", target) is None  # 443 is another port
    assert url_refusal("http://[::1/", target)  # unparsable on the dashboard's port: refused


def test_other_ports_and_other_hosts_are_not_touched():
    target = own_dashboard.target_for("127.0.0.1", 5797)
    assert url_refusal("http://127.0.0.1:11434/api/tags", target) is None  # local model
    assert url_refusal("http://localhost:8080/", target) is None
    assert url_refusal(f"http://{PUBLIC}:5797/", target) is None  # another host, same number
    assert url_refusal("https://example.org/", target) is None
    assert url_refusal("ftp://127.0.0.1/", target) is None  # not HTTP: no default port
    assert url_refusal("ftp://127.0.0.1:5797/", target)  # the dashboard's port, any scheme
    assert url_refusal("http://127.0.0.1:5797/", None) is None  # nothing armed


def test_an_unresolvable_host_on_the_dashboard_port_is_refused():
    target = own_dashboard.target_for("127.0.0.1", 5797)
    assert url_refusal("http://stira-no-such-host.invalid:5797/", target)
    assert url_refusal("http://stira-no-such-host.invalid:5798/", target) is None


def test_a_wildcard_listener_covers_every_local_address():
    address = _local_non_loopback_address()
    if address is None:
        pytest.skip("no non-loopback local IPv4 address here")
    narrow = Target(5797, frozenset())
    wide = Target(5797, frozenset(), any_interface=True)
    assert not own_dashboard.is_own_dashboard(address, 5797, narrow)
    assert own_dashboard.is_own_dashboard(address, 5797, wide)
    assert not own_dashboard.is_own_dashboard(PUBLIC, 5797, wide)  # not this host's
    assert not own_dashboard.is_own_dashboard(address, 5798, wide)  # another port


# --- 2. through the tool: the URL check alone (connect hook off) ------------------------


def test_http_request_to_its_own_dashboard_is_refused_with_the_reason_code(armed):
    status, text = _run(f"http://127.0.0.1:{armed.port}/api/csrf")
    assert status == "error"
    assert REASON_CODE in text and "was not run" in text
    assert armed.hits == 0


@pytest.mark.parametrize("host", _host_forms())
def test_the_tool_refuses_every_host_form_before_sending(armed, hook_off, rebind_dns, host):
    status, text = _run(f"http://{host}:{armed.port}/api/csrf")
    assert status == "error" and REASON_CODE in text and "was not run" in text, text
    assert armed.hits == 0


def test_a_post_with_the_token_is_refused_as_well(armed, hook_off, monkeypatch):
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")  # the worst operator setting
    status, text = _run(
        f"http://localhost:{armed.port}/api/config/tools",
        method="POST",
        headers={"X-CSRF-Token": "token", "Content-Type": "application/json"},
        body='{"name": "file_write", "policy": "always"}',
    )
    assert status == "error" and REASON_CODE in text
    assert armed.hits == 0


def test_other_loopback_ports_stay_reachable(armed):
    other = _Counted()
    try:
        status, text = _run(f"http://127.0.0.1:{other.port}/")
        assert status == "success" and "Status Code: 200" in text, text
        assert other.hits == 1 and armed.hits == 0
    finally:
        other.close()


def test_nothing_armed_reaches_the_server(dashboard):
    """Control: the stand-in dashboard is reachable when no server armed the guard."""
    status, text = _run(f"http://127.0.0.1:{dashboard.port}/")
    assert status == "success" and "Status Code: 200" in text
    assert dashboard.hits == 1


# --- 3. a redirect to the dashboard (connect hook off) ------------------------------------


def test_a_redirect_to_the_dashboard_is_refused_hop_by_hop(armed, hook_off):
    hop = _Counted(redirect_to=f"http://localhost:{armed.port}/api/csrf")
    try:
        status, text = _run(f"http://127.0.0.1:{hop.port}/")
        assert status == "error" and REASON_CODE in text, text
        assert hop.hits == 1 and armed.hits == 0
    finally:
        hop.close()


def test_the_redirect_check_is_installed_on_the_upstream_session():
    import strands_tools.http_request as upstream

    assert getattr(upstream._SafeRedirectSession, "_stira_own_dashboard_check", False)
    assert own_dashboard_gate.install_redirect_check(upstream)  # idempotent


# --- 4. the connect hook alone (tool-side checks off): a second DNS answer --------------


def test_a_rebinding_answer_is_refused_where_the_connection_is_made(armed, monkeypatch):
    """The URL check sees a public address, the client's own lookup gets loopback."""
    real = socket.getaddrinfo
    answers = iter([PUBLIC])

    def rebinding(host, *args, **kwargs):
        if host in ("rebind.example", b"rebind.example"):
            return real(next(answers, "127.0.0.1"), *args, **kwargs)
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    status, text = _run(f"http://rebind.example:{armed.port}/api/csrf")
    assert status == "error" and REASON_CODE in text, text
    assert "was not run" not in text  # it got past the URL check: the hook refused it
    assert armed.hits == 0


def test_the_connect_hook_alone_refuses_the_dashboard(armed, monkeypatch):
    monkeypatch.setattr(own_dashboard_gate.own_dashboard, "url_refusal", lambda *a, **k: None)
    status, text = _run(f"http://127.0.0.1:{armed.port}/api/csrf")
    assert status == "error" and REASON_CODE in text, text
    assert armed.hits == 0
    with pytest.raises(requests.ConnectionError, match=REASON_CODE):
        requests.get(f"http://localhost:{armed.port}/", timeout=5)


# --- 5. the server arms the guard at every start ----------------------------------------


@pytest.fixture
def entry_point(monkeypatch):
    from dashboard import stira_server

    seen = []

    def serve(application, host, port):
        seen.append((host, port, own_dashboard.armed()))

    monkeypatch.setattr(stira_server, "serve", serve)
    monkeypatch.setattr(stira_server, "_install_log_masking", lambda *a, **k: [])
    monkeypatch.setattr(stira_server.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(stira_server, "_yolo_config_section", lambda path: None)
    for name in ("STIRA_YOLO_ALLOW", "STIRA_ALLOW_REMOTE"):
        monkeypatch.delenv(name, raising=False)
    yield stira_server, seen
    own_dashboard.disarm()


def test_the_server_arms_the_guard_without_yolo(entry_point, monkeypatch):
    stira_server, seen = entry_point
    monkeypatch.setenv("STIRA_HOST", "127.0.0.1")
    stira_server.main(app_factory=lambda lifecycle: object())
    ((_host, port, target),) = seen
    assert target is not None and target.port == stira_server.PORT == port
    assert ipaddress.ip_address("127.0.0.1") in target.addresses
    assert not target.any_interface
    assert own_dashboard.armed() is None  # disarmed once the server is gone


def test_a_wildcard_listener_without_yolo_is_armed_for_every_interface(entry_point, monkeypatch):
    stira_server, seen = entry_point
    monkeypatch.setenv("STIRA_HOST", "0.0.0.0")
    monkeypatch.setenv("STIRA_ALLOW_REMOTE", "1")
    stira_server.main(app_factory=lambda lifecycle: object())
    ((_host, _port, target),) = seen
    assert target is not None and target.any_interface


# --- 6. end to end: a live chat server, http_request Always, no YOLO ------------------------


@pytest.fixture
def live_stira(tmp_path, monkeypatch):
    import json
    import threading

    from werkzeug.serving import make_server

    from stiracore.yolo import runtime
    from tests.test_new_session import make_stira

    runtime._reset_for_tests()  # no allowance at all
    generator = make_stira(tmp_path, monkeypatch)
    stira = next(generator)
    client, token = stira["client"], stira["token"]
    saved = client.post(
        "/api/config/tools",
        json={
            "name": "http_request",
            "enabled": True,
            "policy": "always",
            "confirm": True,
            "confirm_always": ["http_request"],
        },
        headers={"X-CSRF-Token": token},
    )
    assert saved.status_code == 200, saved.get_json()
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    server = make_server("127.0.0.1", 0, stira["app"], threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    own_dashboard.arm("127.0.0.1", server.server_port)  # what main() does at every start

    def call(request):
        stira["model"].script = [
            [{"toolUse": {"name": "http_request", "toolUseId": "self", "input": request}}],
            [{"text": "tried"}],
        ]
        body = stira["chat"]("call your own dashboard")
        turn = json.loads(
            next(b for b in body.split("\n\n") if b.startswith("event: turn\n")).split("data: ", 1)[
                1
            ]
        )["turn_id"]
        events = client.get(f"/api/turns/{turn}/events").get_json()["events"]
        return [
            e["data"].get("output_preview") or ""
            for e in events
            if e["type"] == "tool_call.updated" and e["data"].get("name") == "http_request"
        ]

    try:
        yield stira, server.server_port, call
    finally:
        own_dashboard.disarm()
        server.shutdown()
        thread.join(5)
        next(generator, None)


def test_live_dashboard_refuses_its_own_http_request_always_without_yolo(live_stira):
    import json

    stira, port, call = live_stira
    client = stira["client"]
    before = client.get("/api/tools/config").get_json()
    for request in (
        {"method": "GET", "url": f"http://127.0.0.1:{port}/api/csrf"},
        {
            "method": "POST",
            "url": f"http://localhost:{port}/api/config/tools",
            "headers": {"X-CSRF-Token": stira["token"], "Content-Type": "application/json"},
            "body": json.dumps({"name": "file_write", "policy": "always", "confirm": True}),
        },
    ):
        outputs = call(request)
        # #547: a POST nobody approved is refused before the tool runs (the upstream
        # terminal prompt is Stira's; BYPASS_TOOL_CONSENT is ignored), so it never reaches
        # the URL guard. A POST a person approved meets it: tests/attack/test_a3_own_dashboard.py.
        expected = REASON_CODE if request["method"] == "GET" else "person approved this exact"
        assert outputs and any(expected in output for output in outputs), outputs
        assert all(stira["token"] not in output for output in outputs)
        assert all("Status Code" not in output for output in outputs), outputs
    assert client.get("/api/tools/config").get_json() == before
