"""ADR 0015 Addendum rev 4 (#449): ``page_check`` phase 3 — one named localhost origin.

Pure parts (target checks, the proxy's forwarding rule, the logs, the port list in
``tool-config.json``, the person-only route) run everywhere; the canary contract test and
the real-browser checks need Chrome / Chromium / Edge and are skipped without one unless
``STIRA_REQUIRE_BROWSER=1``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from flask import Flask

from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.tools import page_check as pc
from stiracore.tools import tool_config as tc
from stiracore.tools.tool_config import ToolConfig

BROWSER = pc.browser_status()
REQUIRE_BROWSER = os.environ.get("STIRA_REQUIRE_BROWSER", "").strip() == "1"
needs_browser = pytest.mark.skipif(
    not BROWSER.available and not REQUIRE_BROWSER,
    reason=f"no browser for page_check ({BROWSER.code}); set STIRA_REQUIRE_BROWSER=1 to fail",
)


def _policy(ws):
    from stiracore.identity_paths import WritePathPolicy

    return WritePathPolicy(base_dir=ws, write_roots=[ws])


@pytest.fixture
def ws(tmp_path):
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    return root


# --- the target: one named origin, a port a person listed ------------------------------


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("https://localhost:8080/", "scheme_refused"),
        ("ws://localhost:8080/", "scheme_refused"),
        ("http://127.0.0.1:8080/", "host_refused"),  # another origin, also loopback
        ("http://[::1]:8080/", "host_refused"),
        ("http://foo.localhost:8080/", "host_refused"),
        ("http://localhost.:8080/", "host_refused"),
        ("http://example.com:8080/", "host_refused"),
        ("http://192.168.0.2:8080/", "host_refused"),
        ("http://user@localhost:8080/", "host_refused"),
        ("http://localhost/", "port_refused"),  # a port must be named
        ("http://localhost:80/", "port_refused"),
        ("http://localhost:1023/", "port_refused"),
        ("http://localhost:08080/", "port_refused"),  # one spelling only
        ("http://localhost:99999/", "invalid_target"),
        ("http://localhost:8080/a b", "invalid_target"),
        ("http://localhost:8080/\x00", "invalid_target"),
        ("http://localhost:5555/", "own_dashboard"),
        ("http://localhost:9090/", "port_not_allowed"),  # not listed
    ],
)
def test_local_targets_are_refused_before_launch(ws, monkeypatch, target, code):
    monkeypatch.setattr(pc, "_Browser", None)  # a launch would fail loudly
    monkeypatch.setattr(pc, "own_dashboard_ports", lambda env=None: frozenset({5555}))
    result = pc.run_page_check(
        target, _policy(ws), browser="/nonexistent", local_ports=[8080, 5555]
    )
    assert (result["status"], result["reason"]) == ("refused", code), target


def test_an_allowed_local_target_keeps_its_path_query_and_fragment():
    assert pc.check_local_target("HTTP://LOCALHOST:8080", [8080], ()) == (
        8080,
        "http://localhost:8080/",
    )
    assert pc.check_local_target("http://localhost:8080/a/b.html?x=1#y", [8080], ())[1] == (
        "http://localhost:8080/a/b.html?x=1#y"
    )


def test_no_port_is_allowed_by_default_and_the_list_is_never_an_argument(ws, monkeypatch):
    monkeypatch.setattr(pc, "_Browser", None)
    result = pc.run_page_check("http://localhost:8080/", _policy(ws), browser="/x")
    assert result["reason"] == "port_not_allowed"
    seen = {}

    def fake_run(target, policy, **kwargs):
        seen.update(kwargs)
        return {"status": "refused", "reason": "x"}

    import asyncio

    monkeypatch.setattr(pc, "run_page_check", fake_run)
    tool = pc.WorkspacePageCheck(pc.page_check, _policy(ws))
    use = {
        "toolUseId": "u",
        "input": {
            "target": "http://localhost:8080/",
            "local_ports": [8080],
            "ports": [8080],
            "page_check_ports": [8080],
            "allowed_ports": [8080],
        },
    }

    async def go():
        return [e async for e in tool.stream(use, {})]

    pc.set_local_ports_provider(None)
    asyncio.run(go())
    assert seen["local_ports"] == ()  # nothing from the arguments
    pc.set_local_ports_provider(lambda: [8080, 80, "9000", True, 70000])
    try:
        asyncio.run(go())
        assert seen["local_ports"] == (8080,)
        pc.set_local_ports_provider(lambda: 1 / 0)
        asyncio.run(go())
        assert seen["local_ports"] == ()  # unreadable: none (fail closed)
    finally:
        pc.set_local_ports_provider(None)
    properties = pc.page_check.tool_spec["inputSchema"]["json"]["properties"]
    assert set(properties) == {"target", "actions", "wait_ms"}


def test_the_own_dashboard_port_comes_from_the_guard_and_the_environment(monkeypatch):
    from stiracore.yolo import own_dashboard

    monkeypatch.setattr(own_dashboard, "armed", lambda: own_dashboard.Target(6111, frozenset()))
    assert pc.own_dashboard_ports({"STIRA_PORT": "6222"}) == {6111, 6222}
    monkeypatch.setattr(own_dashboard, "armed", lambda: None)
    assert pc.own_dashboard_ports({}) == frozenset()


# --- the proxy's rule: forward the one origin, refuse everything else ------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b"GET http://localhost:8080/ HTTP/1.1", "/"),
        (b"GET http://localhost:8080 HTTP/1.1", "/"),
        (b"POST http://localhost:8080/api?x=1 HTTP/1.1", "/api?x=1"),
        (b"GET http://LOCALHOST:8080/a HTTP/1.1", "/a"),
        (b"CONNECT localhost:8080 HTTP/1.1", "CONNECT"),
        (b"CONNECT localhost:8081 HTTP/1.1", None),
        (b"CONNECT 127.0.0.1:8080 HTTP/1.1", None),
        (b"GET http://127.0.0.1:8080/ HTTP/1.1", None),
        (b"GET http://[::1]:8080/ HTTP/1.1", None),
        (b"GET http://localhost:8081/ HTTP/1.1", None),
        (b"GET http://localhost/ HTTP/1.1", None),
        (b"GET http://foo.localhost:8080/ HTTP/1.1", None),
        (b"GET http://u@localhost:8080/ HTTP/1.1", None),
        (b"GET https://localhost:8080/ HTTP/1.1", None),
        (b"GET ws://localhost:8080/ HTTP/1.1", None),
        (b"GET /relative HTTP/1.1", None),
        (b"GET http://localhost:8080/ HTTP/2", None),
        (b"get http://localhost:8080/ HTTP/1.1", None),
        (b"GET http://localhost:8080/ x HTTP/1.1", None),
        (b"GET http://localhost:8080/\xff HTTP/1.1", None),
        (b"", None),
    ],
)
def test_forward_target(line, expected):
    assert pc.forward_target(line, 8080) == expected


def test_nothing_is_forwarded_without_a_port():
    assert pc.forward_target(b"GET http://localhost:8080/ HTTP/1.1", None) is None


class _Canary:
    def __init__(self, host="127.0.0.1"):
        self.sock = socket.socket()
        self.sock.bind((host, 0))
        self.sock.listen(32)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.hits += 1
            try:
                conn.settimeout(1)
                conn.recv(4096)
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nAccess-Control-Allow-Origin: *\r\n"
                    b"Content-Length: 6\r\nConnection: close\r\n\r\ncanary"
                )
            except OSError:
                pass
            conn.close()

    def close(self):
        self._stop.set()
        self.sock.close()


class _UdpCanary:
    def __init__(self, host):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.packets = 0
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.sock.recvfrom(2048)
                self.packets += 1
            except OSError:
                continue

    def close(self):
        self._stop.set()
        self.sock.close()


def _lan_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            address = s.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


class _Origin:
    """The person's server on 127.0.0.1: a few routes, every request recorded."""

    def __init__(self, pages=None, redirects=None):
        self.requests = []
        self.upgrades = 0
        origin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, code, body=b"", ctype="text/html", headers=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for key, value in headers:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                origin.requests.append((self.command, self.path, dict(self.headers)))
                path = self.path.split("?")[0]
                if redirects and path in redirects:
                    return self._send(302, headers=[("Location", redirects[path])])
                if path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
                    key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                    accept = base64.b64encode(hashlib.sha1(key.encode()).digest()).decode()
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.end_headers()
                    self.wfile.write(b"\x81\x05hello")
                    self.wfile.flush()
                    origin.upgrades += 1
                    time.sleep(0.5)
                    return None
                if path == "/same":
                    return self._send(200, b"same-ok", "text/plain")
                if pages and path in pages:
                    return self._send(200, pages[path].encode())
                return self._send(404, b"no")

            do_POST = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _through(proxy, raw: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=3) as conn:
        conn.sendall(raw)
        data = b""
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
    return data


def test_the_proxy_forwards_the_one_origin_and_refuses_the_rest():
    origin, other = _Origin(), _Canary()
    try:
        with pc.RefusingProxy(forward_port=origin.port) as proxy:
            answer = _through(
                proxy,
                f"GET http://localhost:{origin.port}/same?q=1 HTTP/1.1\r\n"
                f"Host: evil.test\r\nProxy-Connection: keep-alive\r\n"
                f"Proxy-Authorization: x\r\nX-Kept: 1\r\n\r\n".encode(),
            )
            assert answer.startswith(b"HTTP/1.0 200") and answer.endswith(b"same-ok")
            method, path, headers = origin.requests[-1]
            assert (method, path) == ("GET", "/same?q=1")
            assert headers["Host"] == f"localhost:{origin.port}"  # never the page's
            assert headers["Connection"] == "close" and headers["X-Kept"] == "1"
            assert "Proxy-Connection" not in headers and "Proxy-Authorization" not in headers
            for raw in (
                f"GET http://127.0.0.1:{origin.port}/same HTTP/1.1\r\n\r\n",
                f"GET http://localhost:{other.port}/ HTTP/1.1\r\n\r\n",
                f"CONNECT localhost:{other.port} HTTP/1.1\r\n\r\n",
                f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\n\r\n",
                f"GET https://localhost:{origin.port}/ HTTP/1.1\r\n\r\n",
            ):
                assert _through(proxy, raw.encode()).startswith(b"HTTP/1.1 403"), raw
            time.sleep(0.2)
            assert other.hits == 0 and len(origin.requests) == 1
            assert proxy.forwarded == 1 and proxy.attempts == 5
            assert f"localhost:{other.port}" in proxy.hosts
    finally:
        origin.close()
        other.close()


def test_a_websocket_tunnel_reaches_only_the_origin():
    origin = _Origin()
    try:
        with pc.RefusingProxy(forward_port=origin.port) as proxy:
            with socket.create_connection(("127.0.0.1", proxy.port), timeout=3) as conn:
                conn.sendall(f"CONNECT localhost:{origin.port} HTTP/1.1\r\n\r\n".encode())
                assert conn.recv(1024).startswith(b"HTTP/1.1 200 Connection Established")
                conn.sendall(
                    b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                    b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    b"Sec-WebSocket-Version: 13\r\n\r\n"
                )
                data = b""
                while b"hello" not in data:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    data += chunk
            assert b"101" in data and b"hello" in data and origin.upgrades == 1
    finally:
        origin.close()


def test_a_dead_origin_is_a_bad_gateway_not_a_fallback():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nobody listens there now
    with pc.RefusingProxy(forward_port=port) as proxy:
        answer = _through(proxy, f"GET http://localhost:{port}/ HTTP/1.1\r\n\r\n".encode())
        assert answer.startswith(b"HTTP/1.1 502") and proxy.forward_failed == 1


def test_a_page_load_counts_and_off_origin_documents_are_aborted(ws):
    sent = []

    class Pipe:
        def send(self, method, params=None, session=None):
            sent.append((method, params))
            return len(sent)

    run = pc._Run(Pipe(), _policy(ws), time.monotonic() + 5, origin_port=8080)
    run.main_target = "F"
    for url, kind in (
        ("http://localhost:8080/", "Document"),
        ("ws://localhost:8080/ws", "WebSocket"),
        ("http://localhost:8080/app.js", "Script"),
        ("http://127.0.0.1:8080/", "Document"),
        ("http://localhost:8081/x", "XHR"),
    ):
        run._paused(
            {"requestId": url, "request": {"url": url}, "resourceType": kind, "frameId": "F"},
            "S",
        )
    verbs = [m for m, _p in sent]
    assert verbs[:3] == ["Fetch.continueRequest"] * 3
    assert sent[3] == (
        "Fetch.failRequest",
        {"requestId": sent[3][1]["requestId"], "errorReason": "Aborted"},
    )
    assert verbs[4] == "Fetch.continueRequest"  # the proxy refuses it
    assert run.page_loads == 1 and run.blocked["navigations"] == 1
    assert run.page_requests == 2 and set(run.page_hosts) == {"127.0.0.1:8080", "localhost:8081"}


# --- logs: @localhost:<port>/<path>, never the query, the fragment or user info --------


def test_the_logs_keep_the_origin_and_path_only():
    from stiracore.tools.file_tools import payload_for_log

    for target, expected in {
        "http://localhost:8080/app/index.html?token=***#frag": "@localhost:8080/app/index.html",
        "http://localhost:8080": "@localhost:8080/",
        "http://u:pw@localhost:8080/": "refused:host_refused",
        "http://127.0.0.1:8080/?token=***": "refused:host_refused",
        "https://localhost:8080/?token=***": "refused:scheme_refused",
        "http://localhost:80/": "refused:port_refused",
    }.items():
        logged = payload_for_log("page_check", "input", {"target": target})
        assert logged == {"target": expected}, target
    result = {
        "status": "ok",
        "reason": None,
        "network": {
            "blocked": 1,
            "hosts": ["x"],
            "hosts_total": 1,
            "proxy_refused": 2,
            "forwarded": 7,
        },
        "open_yourself": {"url": "http://localhost:8080/a?token=***#f"},
    }
    kept = pc.for_log(result)
    assert kept["target"] == "@localhost:8080/a" and kept["network"]["forwarded"] == 7
    assert "token" not in json.dumps(kept) and "#f" not in json.dumps(kept)


# --- the port list in tool-config.json -------------------------------------------------


def _file(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_ports_are_optional_in_the_file_and_part_of_the_checked_rules(tmp_path):
    path = tmp_path / "tool-config.json"
    ToolConfig(mode="custom", enabled_tools={"file_read"}, path=path).save()
    assert tc.PAGE_CHECK_PORTS not in _file(path)  # an older file stays the same
    before = ToolConfig.load(path)
    assert before.integrity_error is None and before.page_check_ports == []
    ToolConfig(**before.as_dict(), page_check_ports=[9000, 8080, 8080], path=path).save()
    assert _file(path)[tc.PAGE_CHECK_PORTS] == [8080, 9000]
    loaded = ToolConfig.load(path)
    assert loaded.integrity_error is None and loaded.page_check_ports == [8080, 9000]
    assert tc.PAGE_CHECK_PORTS not in loaded.as_dict()  # not a hub / API field
    head = json.loads(tc.head_path(path).read_text(encoding="utf-8"))
    assert head["rules"][tc.PAGE_CHECK_PORTS] == [8080, 9000]


def test_every_other_save_keeps_the_recorded_ports(tmp_path):
    path = tmp_path / "tool-config.json"
    ToolConfig(mode="custom", enabled_tools=set(), page_check_ports=[8080], path=path).save()
    # A tool-rule save of another path builds its config without the ports.
    ToolConfig(mode="custom", enabled_tools={"file_read"}, path=path).save()
    loaded = ToolConfig.load(path)
    assert loaded.page_check_ports == [8080] and loaded.enabled_tools == {"file_read"}


def test_a_hand_edit_adding_a_port_locks_the_rules(tmp_path):
    path = tmp_path / "tool-config.json"
    ToolConfig(mode="custom", enabled_tools=set(), path=path).save()
    data = _file(path)
    data[tc.PAGE_CHECK_PORTS] = [8080]
    path.write_text(json.dumps(data), encoding="utf-8")
    locked = ToolConfig.load(path)
    assert locked.integrity_error == "edited" and not locked.page_check_ports


@pytest.mark.parametrize(
    "bad", [[80], [8080.0], ["8080"], [True], [70000], list(range(2000, 2009)), 8080]
)
def test_bad_ports_in_the_file_lock(tmp_path, bad):
    path = tmp_path / "tool-config.json"
    ToolConfig(mode="custom", enabled_tools=set(), path=path).save()
    data = _file(path)
    data[tc.PAGE_CHECK_PORTS] = bad
    path.write_text(json.dumps(data), encoding="utf-8")
    assert ToolConfig.load(path).integrity_error == "malformed"
    with pytest.raises(ValueError):
        ToolConfig(page_check_ports=bad)


@pytest.mark.parametrize("broken", ["head", "file"])
def test_a_recovery_save_drops_the_ports(tmp_path, broken):
    path = tmp_path / "tool-config.json"
    ToolConfig(mode="custom", enabled_tools=set(), page_check_ports=[8080], path=path).save()
    if broken == "head":
        tc.head_path(path).write_text("{", encoding="utf-8")  # the record is unreadable
    else:  # a hand edit: the head (with its ports) is still readable, but not trusted
        data = _file(path)
        data["enabled_tools"] = ["file_read"]
        path.write_text(json.dumps(data), encoding="utf-8")
    assert ToolConfig.load(path).integrity_error is not None
    ToolConfig(mode="custom", enabled_tools=set(), path=path).save(recover=True)
    recovered = ToolConfig.load(path)
    assert recovered.integrity_error is None and recovered.page_check_ports == []


def test_adding_a_port_is_loosening_and_removing_is_not():
    rules = ToolConfig(mode="custom", enabled_tools=set()).as_dict()
    with_port = {**rules, tc.PAGE_CHECK_PORTS: [8080]}
    assert tc.rules_loosening(rules, with_port) == [tc.PAGE_CHECK_PORTS]
    assert tc.rules_loosening(with_port, {**rules, tc.PAGE_CHECK_PORTS: [8080, 9000]}) == [
        tc.PAGE_CHECK_PORTS
    ]
    assert tc.rules_loosening(with_port, rules) == []
    assert tc.rules_loosening(with_port, with_port) == []
    assert tc.rules_loosening(rules, {**rules, tc.PAGE_CHECK_PORTS: ["x"]}) == ["snapshot"]


def test_an_interrupted_save_that_adds_a_port_stays_locked(tmp_path):
    path = tmp_path / "tool-config.json"
    ToolConfig(mode="custom", enabled_tools=set(), path=path).save()
    data = _file(path)
    data["generation"] += 1
    data[tc.PAGE_CHECK_PORTS] = [8080]  # the file of a next generation, head not written
    path.write_text(json.dumps(data), encoding="utf-8")
    assert ToolConfig.load(path).integrity_error == "unconfirmed_generation"


# --- the person-only route -------------------------------------------------------------


def _store(tmp_path, ports=None):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    ToolConfig(
        mode="custom", enabled_tools={"file_read"}, page_check_ports=ports, path=store.tool_path
    ).save()
    return store


def _client(store, level="normal"):
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: level))
    return app.test_client()


ROUTE = "/api/tools/page-check-ports"


def _history(store):
    path = store.history_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_adding_a_port_needs_the_typed_port_and_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "own_dashboard_ports", lambda env=None: frozenset())
    store = _store(tmp_path)
    client = _client(store)
    assert client.get(ROUTE).get_json()["ports"] == []
    for body in (
        {"ports": [8080]},
        {"ports": [8080], "confirm": True},
        {"ports": [8080], "confirm": True, "confirm_ports": [9090]},
        {"ports": [8080], "confirm": True, "confirm_ports": ["8080"]},
        {"ports": [8080], "confirm": "yes", "confirm_ports": [8080]},
        {"ports": [8080, 9090], "confirm": True, "confirm_ports": [8080]},
    ):
        answer = client.post(ROUTE, json=body)
        assert answer.status_code == 400, body
        assert answer.get_json()["error_code"] == "needs_confirmation"
    assert store.load_tools().page_check_ports == [] and _history(store) == []
    answer = client.post(ROUTE, json={"ports": [8080], "confirm": True, "confirm_ports": [8080]})
    assert answer.status_code == 200 and answer.get_json() == {"ports": [8080], "saved": True}
    assert store.load_tools().page_check_ports == [8080]
    (row,) = _history(store)
    assert row["source"] == "api:page_check_ports"
    assert row["changes"] == [
        {"field": "page_check.localhost_ports", "added": [8080], "removed": []}
    ]
    # Removing narrows: no confirmation.
    assert client.post(ROUTE, json={"ports": []}).get_json() == {"ports": [], "saved": True}
    assert store.load_tools().page_check_ports == [] and len(_history(store)) == 2


@pytest.mark.parametrize(
    ("ports", "code"),
    [
        ([80], "port_refused"),
        ([1023], "port_refused"),
        ([0], "port_refused"),
        ([65536], "invalid_ports"),
        (["8080"], "invalid_ports"),
        ([True], "invalid_ports"),
        (list(range(3000, 3009)), "invalid_ports"),
        ("8080", "invalid_ports"),
        ([6111], "own_dashboard"),
        ([7777], "own_dashboard"),  # the port the request came in on
    ],
)
def test_refused_ports(tmp_path, monkeypatch, ports, code):
    monkeypatch.setattr(pc, "own_dashboard_ports", lambda env=None: frozenset({6111}))
    store = _store(tmp_path)
    answer = _client(store).post(
        ROUTE,
        json={"ports": ports, "confirm": True, "confirm_ports": ports},
        base_url="http://localhost:7777",
    )
    assert (answer.status_code, answer.get_json()["error_code"]) == (400, code)
    assert store.load_tools().page_check_ports == []


def test_no_port_is_added_while_the_breaker_is_active_or_a_tool_call_runs(tmp_path, monkeypatch):
    from stiracore.yolo import runtime as yolo_runtime

    monkeypatch.setattr(pc, "own_dashboard_ports", lambda env=None: frozenset())
    store = _store(tmp_path)
    body = {"ports": [8080], "confirm": True, "confirm_ports": [8080]}
    answer = _client(store, level="warning").post(ROUTE, json=body)
    assert (answer.status_code, answer.get_json()["error_code"]) == (403, "breaker_active")
    assert _history(store) == []  # refused before the audit (not audited, then aborted)
    monkeypatch.setattr(
        yolo_runtime,
        "tool_call_refusal",
        lambda: {
            "error": "running",
            "error_code": "tool_call_running",
            "code": "tool_call_running",
        },
    )
    answer = _client(store).post(ROUTE, json=body)
    assert (answer.status_code, answer.get_json()["error_code"]) == (409, "tool_call_running")
    assert store.load_tools().page_check_ports == []


def test_unknown_fields_and_locked_rules_are_refused(tmp_path):
    store = _store(tmp_path)
    client = _client(store)
    assert client.post(ROUTE, json={"ports": [], "recover": "RECOVER"}).status_code == 400
    assert client.post(ROUTE, data="x").status_code == 415
    tc.head_path(store.tool_path).write_text("{", encoding="utf-8")
    answer = client.post(ROUTE, json={"ports": [8080], "confirm": True, "confirm_ports": [8080]})
    assert answer.status_code == 409 and answer.get_json()["error_code"] == "tool_rules_locked"
    assert client.get(ROUTE).get_json()["locked"] is True


def test_no_other_route_can_add_a_port(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "own_dashboard_ports", lambda env=None: frozenset())
    store = _store(tmp_path)
    client = _client(store)
    # The tool-rule APIs refuse the field outright.
    for route in ("/api/tools/config", "/api/config/tools"):
        answer = client.post(route, json={"page_check_ports": [8080], "confirm": True})
        assert answer.status_code == 400, route
    # The hub: the field is not a hub value; a body carrying it changes nothing.
    state = client.get("/api/config/hub").get_json()
    tools = dict(state["values"]["tools"])
    assert tc.PAGE_CHECK_PORTS not in tools
    client.post(
        "/api/config/hub",
        json={
            "changes": {"tools": {**tools, tc.PAGE_CHECK_PORTS: [8080]}},
            "base_version": state["version"],
            "confirm": True,
        },
    )
    assert store.load_tools().page_check_ports == []
    # A rollback of a ports row is not restorable (it can never add a port back).
    client.post(ROUTE, json={"ports": [8080], "confirm": True, "confirm_ports": [8080]})
    client.post(ROUTE, json={"ports": []})
    removal = _history(store)[-1]
    answer = client.post(f"/api/config/history/{removal['id']}/rollback", json={"confirm": True})
    assert answer.status_code in (400, 404, 409)
    assert store.load_tools().page_check_ports == []


def test_a_tool_rule_save_through_the_hub_keeps_the_ports(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "own_dashboard_ports", lambda env=None: frozenset())
    store = _store(tmp_path, ports=[8080])
    client = _client(store)
    answer = client.post("/api/config/tools", json={"name": "file_read", "policy": "never"})
    assert answer.status_code == 200, answer.get_json()
    loaded = store.load_tools()
    assert loaded.policy_of("file_read") == "never" and loaded.page_check_ports == [8080]


# --- real browser: the canary contract test of the addendum ---------------------------


def _leak_page(lan, lan_tcp, other, listed, udp, port):
    urls = {
        "lan": f"http://{lan}:{lan_tcp.port}",
        "other": f"http://localhost:{other.port}",
        "other127": f"http://127.0.0.1:{other.port}",
        "listed": f"http://localhost:{listed.port}",
    }
    js = [
        (
            "const out = {}; const all = []; const put = (k, v) => { out[k] = v;"
            " document.getElementById('out').textContent = JSON.stringify(out); };"
        )
    ]
    fetches = {
        "same": "/same",
        "p127": f"http://127.0.0.1:{port}/same",
        "r_lan": "/r-lan",
        "r_other": "/r-other",
        "r_listed": "/r-listed",
        **{k: u + "/f" for k, u in urls.items()},
    }
    for key, url in fetches.items():
        js.append(
            f"all.push(fetch('{url}').then(r => r.text()).then(t => put('{key}', 'ok:' + t),"
            f" () => put('{key}', 'err')));"
        )
    sockets = {
        "ws_same": f"ws://localhost:{port}/ws",
        "ws_lan": f"ws://{lan}:{lan_tcp.port}/w",
        "ws_other": f"ws://localhost:{other.port}/w",
        "ws_listed": f"ws://localhost:{listed.port}/w",
    }
    for key, url in sockets.items():
        js.append(
            f"all.push(new Promise(done => {{ const w = new WebSocket('{url}');"
            f" w.onmessage = m => {{ put('{key}', 'msg:' + m.data); done(); }};"
            f" w.onerror = () => {{ put('{key}', 'err'); done(); }};"
            f" setTimeout(() => {{ if (!('{key}' in out)) put('{key}', 'none'); done(); }}, 3000); }}));"
        )
    for url in urls.values():
        js.append(f"navigator.sendBeacon('{url}/b', 'x');")
    js.append(
        "const rtc = new RTCPeerConnection({iceServers: [{urls: "
        f"'stun:{lan}:{udp.port}'}}]}}); rtc.createDataChannel('x');"
        "rtc.createOffer().then(o => rtc.setLocalDescription(o));"
    )
    js.append("Promise.all(all).then(() => setTimeout(() => put('done', 'yes'), 1000));")
    tags = "".join(
        f"<img src='{u}/i.png'><iframe src='{u}/frame'></iframe><link rel=prefetch href='{u}/p'>"
        for u in urls.values()
    )
    return f"<title>Local</title><pre id=out></pre>{tags}<script>{''.join(js)}</script>", urls


@needs_browser
@pytest.mark.parametrize("guarded", [True, False], ids=["proxy", "no-proxy-proves-the-canaries"])
def test_only_the_named_origin_is_reachable(ws, monkeypatch, guarded):
    """The addendum's contract: a LAN canary (TCP and UDP), another loopback port and a
    listed port that is not the target's are never reached; the named origin is (HTTP and
    WebSocket); redirects off the origin are refused."""
    lan = _lan_address()
    if lan is None:
        pytest.skip("no non-loopback address on this host")
    lan_tcp, other, listed, udp = _Canary(lan), _Canary(), _Canary(), _UdpCanary(lan)
    pages, redirects = {}, {}
    origin = _Origin(pages=pages, redirects=redirects)  # filled in once its port is known
    pages["/"], urls = _leak_page(lan, lan_tcp, other, listed, udp, origin.port)
    redirects.update(
        {
            "/r-lan": urls["lan"] + "/r",
            "/r-other": urls["other"] + "/r",
            "/r-listed": urls["listed"] + "/r",
        }
    )
    if not guarded:
        real = pc.launch_flags
        monkeypatch.setattr(
            pc,
            "launch_flags",
            lambda profile, port: [
                f
                for f in real(profile, port)
                if not f.startswith(("--proxy", "--force-webrtc", "--webrtc", "--host-resolver"))
            ],
        )
        monkeypatch.setattr(pc, "_flags_safe", lambda flags, port: True)
    try:
        result = pc.run_page_check(
            f"http://localhost:{origin.port}/",
            _policy(ws),
            wait_ms=200,
            screenshot=False,
            local_ports=[origin.port, listed.port],
            actions=[
                {"wait_for": {"text": '"done":"yes"'}, "timeout_ms": 10_000},
                {"text": "#out"},
            ],
        )
        time.sleep(0.5)
        out = json.loads(result["steps"][1]["text"])
        if guarded:
            assert (lan_tcp.hits, udp.packets, other.hits, listed.hits) == (0, 0, 0, 0), out
            assert out["same"] == "ok:same-ok" and out["ws_same"] == "msg:hello", out
            for key in (
                "lan",
                "other",
                "other127",
                "listed",
                "p127",
                "r_lan",
                "r_other",
                "r_listed",
            ):
                assert out[key] == "err", key
            for key in ("ws_lan", "ws_other", "ws_listed"):
                assert out[key] in ("err", "none"), key
            assert result["network"]["forwarded"] >= 3 and origin.upgrades == 1
        else:
            assert lan_tcp.hits + other.hits + listed.hits + udp.packets > 0
    finally:
        origin.close()
        for canary in (lan_tcp, other, listed, udp):
            canary.close()


@needs_browser
def test_a_main_frame_redirect_off_the_origin_is_refused(ws):
    other = _Canary()
    origin = _Origin(redirects={"/": f"http://localhost:{other.port}/away"})
    try:
        result = pc.run_page_check(
            f"http://localhost:{origin.port}/",
            _policy(ws),
            wait_ms=500,
            screenshot=False,
            local_ports=[origin.port],
        )
        assert other.hits == 0 and result["blocked"]["navigations"] == 1
        assert result["loaded"] is False
    finally:
        origin.close()
        other.close()


@needs_browser
def test_a_local_page_is_checked_like_a_file(ws):
    origin = _Origin(
        pages={
            "/app.html": "<title>App</title><button id=b onclick=\"fetch('/same').then(r => r.text())"
            ".then(t => document.getElementById('o').textContent = t)\">go</button><p id=o></p>"
        }
    )
    try:
        result = pc.run_page_check(
            f"http://localhost:{origin.port}/app.html?x=1",
            _policy(ws),
            wait_ms=100,
            local_ports=[origin.port],
            actions=[
                {"click": "#b"},
                {"wait_for": {"text": "same-ok"}, "timeout_ms": 3000},
                {"expect": {"selector": "#o", "text_equals": "same-ok"}},
            ],
        )
        assert result["status"] == "ok" and result["title"] == "App", result
        assert result["assertions"]["passed"] == 1
        assert result["open_yourself"] == {"url": f"http://localhost:{origin.port}/app.html?x=1"}
        assert (ws / result["screenshot"]).read_bytes()[:4] == b"\x89PNG"
        assert pc.for_log(result)["target"] == f"@localhost:{origin.port}/app.html"
    finally:
        origin.close()


# --- the settings page: rendered, axe-checked ------------------------------------------


def test_axe_settings_page_check_ports(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    from tests.ui_pages import render_pages

    def skip_or_fail(reason):
        if os.environ.get("STIRA_REQUIRE_AXE") == "1":
            pytest.fail(f"STIRA_REQUIRE_AXE=1 but axe check unavailable: {reason}")
        pytest.skip(reason)

    node = shutil.which("node")
    if node is None:
        skip_or_fail("node not found on PATH")
    root = Path(__file__).resolve().parents[1]
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        skip_or_fail(str(exc))
    assert 'id="pcports"' in pages["settings"]
    assert "settings_page_check_ports.js" in pages["settings"]
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(root / "tests" / "a11y" / "page_check_ports_axe.cjs"), str(page)],
        capture_output=True,
        text=True,
        cwd=str(root),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        skip_or_fail(output.strip())
    assert proc.returncode == 0, f"page_check ports a11y (exit {proc.returncode}):\n{output}"


def test_the_server_reads_the_ports_from_the_rules_at_every_call(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
        ),
    )
    store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
    try:
        stira_server.create_app(config_store=store)
        assert pc.allowed_local_ports() == ()
        current = store.load_tools()
        ToolConfig(**current.as_dict(), page_check_ports=[8080], path=store.tool_path).save()
        assert pc.allowed_local_ports() == (8080,)  # the next call, no restart
        tc.head_path(store.tool_path).write_text("{", encoding="utf-8")
        assert pc.allowed_local_ports() == ()  # locked rules: none
    finally:
        pc.set_local_ports_provider(None)
