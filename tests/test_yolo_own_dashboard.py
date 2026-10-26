"""ADR 0012 §8b (phase 1): the server process cannot connect to its own dashboard.

Real sockets on loopback, no external network: a local HTTP server stands in for the
dashboard, the guard is armed with its port, and Stira's HTTP tools (and the libraries
under them) are pointed at it — directly, by name, through a redirect.
"""

from __future__ import annotations

import http.server
import ipaddress
import socket
import threading
import urllib.request

import pytest
import requests

from stiracore.yolo import own_dashboard
from stiracore.yolo.own_dashboard import OwnDashboardRefused, Target, is_own_dashboard


class _Handler(http.server.BaseHTTPRequestHandler):
    redirect_to = None

    def do_GET(self):
        if self.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        body = b"dashboard says hello"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(family=socket.AF_INET, host="127.0.0.1", redirect_to=None):
    handler = type("H", (_Handler,), {"redirect_to": redirect_to})

    class Server(http.server.ThreadingHTTPServer):
        address_family = family

    server = Server((host, 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def dashboard():
    server = _serve()
    try:
        yield server
    finally:
        own_dashboard.disarm()
        server.shutdown()
        server.server_close()


@pytest.fixture
def armed(dashboard):
    own_dashboard.arm("127.0.0.1", dashboard.server_address[1])
    return dashboard.server_address[1]


# --- pure decision --------------------------------------------------------------------


def test_decision_covers_loopback_unspecified_and_listen_addresses():
    target = Target(5751, frozenset({ipaddress.ip_address("192.0.2.10")}))
    for address in ("127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1", "0.0.0.0", "::"):
        assert is_own_dashboard(address, 5751, target), address
    assert is_own_dashboard("192.0.2.10", 5751, target)
    assert not is_own_dashboard("127.0.0.1", 5752, target)  # another port
    assert not is_own_dashboard("192.0.2.11", 5751, target)
    assert not is_own_dashboard("not an address", 5751, target)
    assert not is_own_dashboard("127.0.0.1", 5751, None)  # not armed


def test_target_includes_the_bind_host():
    target = own_dashboard.target_for("127.0.0.1", 5751)
    assert target.port == 5751
    assert ipaddress.ip_address("127.0.0.1") in target.addresses


# --- real connections -----------------------------------------------------------------


def test_not_armed_reaches_the_dashboard(dashboard):
    port = dashboard.server_address[1]
    assert requests.get(f"http://127.0.0.1:{port}/", timeout=5).text == "dashboard says hello"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.0.0.2"])
def test_requests_to_own_dashboard_are_refused(armed, host):
    with pytest.raises(requests.ConnectionError, match="own dashboard"):
        requests.get(f"http://{host}:{armed}/api/csrf", timeout=5)


def test_raw_socket_urllib_and_unspecified_address_are_refused(armed):
    with pytest.raises(OwnDashboardRefused):
        socket.create_connection(("127.0.0.1", armed), timeout=5)
    with socket.socket() as sock, pytest.raises(OwnDashboardRefused):
        sock.connect_ex(("localhost", armed))
    with pytest.raises(OSError):  # urllib (feedparser's path for rss)
        urllib.request.urlopen(f"http://127.0.0.1:{armed}/", timeout=5)
    with pytest.raises(requests.ConnectionError):
        requests.get(f"http://0.0.0.0:{armed}/", timeout=5)


def test_ipv6_loopback_is_refused():
    try:
        server = _serve(socket.AF_INET6, "::1")
    except OSError:
        pytest.skip("no IPv6 loopback here")
    try:
        port = server.server_address[1]
        assert requests.get(f"http://[::1]:{port}/", timeout=5).ok  # control
        own_dashboard.arm("::1", port)
        with pytest.raises(requests.ConnectionError, match="own dashboard"):
            requests.get(f"http://[::1]:{port}/", timeout=5)
    finally:
        own_dashboard.disarm()
        server.shutdown()
        server.server_close()


def test_other_ports_stay_reachable_while_armed(armed):
    other = _serve()
    try:
        port = other.server_address[1]
        assert requests.get(f"http://127.0.0.1:{port}/", timeout=5).text == "dashboard says hello"
    finally:
        other.shutdown()
        other.server_close()


def test_web_fetch_and_web_search_cannot_reach_the_dashboard(armed, monkeypatch):
    from stiracore.tools.web_tools import web_fetch, web_search

    result = web_fetch(f"http://127.0.0.1:{armed}/api/csrf")
    assert result["status"] == "error"
    monkeypatch.setenv("STIRA_WEB_SEARCH_ENDPOINT", f"http://localhost:{armed}/search")
    assert web_search("anything")["status"] == "error"
    own_dashboard.disarm()  # control: the same call works without the guard
    assert web_fetch(f"http://127.0.0.1:{armed}/")["status"] == "success"


def test_http_request_tool_is_refused_also_through_a_redirect(armed):
    http_request = pytest.importorskip("strands_tools.http_request")
    redirector = _serve(redirect_to=f"http://localhost:{armed}/api/csrf")
    try:
        hop = redirector.server_address[1]
        for url in (f"http://127.0.0.1:{armed}/api/csrf", f"http://127.0.0.1:{hop}/"):
            result = http_request.http_request(
                {"toolUseId": "t1", "input": {"method": "GET", "url": url}}
            )
            assert result["status"] == "error", url
            assert "dashboard says hello" not in str(result)
        own_dashboard.disarm()
        result = http_request.http_request(
            {"toolUseId": "t2", "input": {"method": "GET", "url": f"http://127.0.0.1:{hop}/"}}
        )
        assert "dashboard says hello" in str(result)  # control: the redirect is followed
    finally:
        redirector.shutdown()
        redirector.server_close()


def test_guarded_integration_fetch_was_already_refusing_loopback(armed):
    from stiracore.integrations.http import DestinationRefused, fetch

    with pytest.raises(DestinationRefused):
        fetch("GET", f"https://127.0.0.1:{armed}/")


# --- review round 1: every host form the socket module accepts -------------------------


@pytest.mark.parametrize(
    "host",
    [
        b"127.0.0.1",
        bytearray(b"127.0.0.1"),
        "127.1",  # short form the resolver accepts
        b"localhost",
        "",  # INADDR_ANY: the local host
    ],
    ids=["bytes", "bytearray", "short", "bytes_name", "empty"],
)
def test_every_host_form_to_the_dashboard_port_is_refused(armed, host):
    for call in ("connect", "connect_ex"):
        with socket.socket() as sock, pytest.raises(OwnDashboardRefused):
            getattr(sock, call)((host, armed))


def test_unknown_host_type_or_encoding_to_the_dashboard_port_is_refused(armed):
    from stiracore.yolo.own_dashboard import _refuses
    from stiracore.yolo.own_dashboard import armed as current

    target = current()
    assert _refuses(object(), armed, target)
    assert _refuses(b"\xff\xfe", armed, target)
    assert _refuses(12345, armed, target)
    # Forms the socket module itself refuses today: refused here too, should that change.
    assert _refuses(memoryview(b"127.0.0.1"), armed, target)
    assert _refuses(ipaddress.ip_address("127.0.0.1"), armed, target)
    assert not _refuses(object(), armed + 1, target)  # another port: never touched


def test_ipv6_and_mapped_forms_are_refused(armed):
    from stiracore.yolo.own_dashboard import _refuses
    from stiracore.yolo.own_dashboard import armed as current

    target = current()
    for host in ("::1", b"::1", "::ffff:127.0.0.1", ipaddress.ip_address("::1"), "::"):
        assert _refuses(host, armed, target), host


def test_normalize_host():
    from stiracore.yolo.own_dashboard import normalize_host

    assert normalize_host(b"127.0.0.1") == ("address", ipaddress.ip_address("127.0.0.1"))
    assert normalize_host(b"::1") == ("address", ipaddress.ip_address("::1"))
    # Only built-in immutable types are evaluated; anything else is unknown (None).
    assert normalize_host(memoryview(b"::1")) is None
    assert normalize_host(bytearray(b"127.0.0.1")) is None
    assert normalize_host(ipaddress.ip_address("127.0.0.1")) is None
    assert normalize_host("::ffff:127.0.0.1") == ("address", ipaddress.ip_address("127.0.0.1"))
    assert normalize_host("example.com") == ("name", "example.com")
    assert normalize_host(b"\xff") is None
    assert normalize_host(3.5) is None


def test_asyncio_connection_is_refused(armed):
    import asyncio

    async def connect():
        await asyncio.open_connection("127.0.0.1", armed)

    with pytest.raises(OSError):
        asyncio.run(connect())


# --- legitimate loopback use stays untouched (local model, embeddings, MCP over HTTP) ----


class _JsonHandler(http.server.BaseHTTPRequestHandler):
    redirect_to = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        reply = b'{"jsonrpc": "2.0", "id": 1, "result": {"echo": ' + body + b"}}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    do_GET = _Handler.do_GET

    def log_message(self, *args):
        pass


def test_other_loopback_services_work_in_every_form_while_armed(armed):
    """Stand-ins for Ollama / llama-server / an embeddings server / an MCP HTTP server:
    every form of loopback address and name, every client library, on their own ports."""
    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler) for _ in range(2)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for server in servers:
            port = server.server_address[1]
            assert port != armed
            for host in ("127.0.0.1", "localhost"):
                reply = requests.post(
                    f"http://{host}:{port}/api/embeddings", json={"x": 1}, timeout=5
                )
                assert reply.json()["result"]["echo"] == {"x": 1}
            with urllib.request.urlopen(f"http://localhost:{port}/", timeout=5) as response:
                assert response.read() == b"dashboard says hello"
            for host in (b"127.0.0.1", "localhost", bytearray(b"127.0.0.1"), ""):
                with socket.socket() as sock:
                    sock.settimeout(5)
                    assert sock.connect_ex((host, port)) == 0, host
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


# --- review round 2: ports read like the socket reads them; no wildcard listener ------


class _IndexPort:
    """A numpy-style integer: not an ``int``, accepted by ``socket`` through ``__index__``."""

    def __init__(self, value):
        self.value = value

    def __index__(self):
        return self.value


@pytest.mark.parametrize("call", ["connect", "connect_ex"])
def test_an_integer_compatible_port_to_the_dashboard_is_refused(armed, call):
    with socket.socket() as sock, pytest.raises(OwnDashboardRefused):
        getattr(sock, call)(("127.0.0.1", _IndexPort(armed)))


@pytest.mark.parametrize("call", ["connect", "connect_ex"])
def test_an_integer_compatible_port_to_another_local_port_is_refused_too(armed, call):
    """The guard does not evaluate a port object (round 3): to a local host it cannot
    tell which port it is, so it refuses — also when it would have been another port."""
    other = _serve()
    try:
        port = other.server_address[1]
        with socket.socket() as sock, pytest.raises(OwnDashboardRefused):
            getattr(sock, call)(("127.0.0.1", _IndexPort(port)))
        with socket.socket() as sock:  # the same port as a plain int connects
            sock.settimeout(5)
            assert sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        other.shutdown()
        other.server_close()


class _ChangingPort:
    """``__index__`` answers the dashboard's port first, another port afterwards."""

    def __init__(self, first, later):
        self.first, self.later, self.calls = first, later, 0

    def __index__(self):
        self.calls += 1
        return self.first if self.calls == 1 else self.later


@pytest.mark.parametrize("call", ["connect", "connect_ex"])
def test_a_port_that_changes_per_call_cannot_reach_the_dashboard(armed, call):
    """TOCTOU (round 3): the socket converts first (the dashboard's port), the hook would
    see another value. The hook never calls ``__index__``: refused, and only the socket's
    own conversion ran."""
    port = _ChangingPort(armed, armed + 1 if armed < 65535 else armed - 1)
    with socket.socket() as sock, pytest.raises(OwnDashboardRefused):
        sock.settimeout(5)
        getattr(sock, call)(("127.0.0.1", port))
    assert port.calls == 1


class _LyingStr(str):
    def __str__(self):
        return "192.0.2.10"

    def __eq__(self, other):
        return False

    __hash__ = str.__hash__


class _LyingTuple(tuple):
    def __getitem__(self, index):
        return ("192.0.2.10", 1)[index]

    def __len__(self):
        return 2


@pytest.mark.parametrize("call", ["connect", "connect_ex"])
def test_host_and_tuple_objects_that_could_answer_differently_are_not_trusted(armed, call):
    """Same principle for the host side: a ``str`` subclass and a tuple subclass whose
    Python methods lie are judged by what the socket read (the C-level values)."""
    for address in (
        (_LyingStr("127.0.0.1"), armed),
        (_LyingStr("127.0.0.1"), _ChangingPort(armed, 1)),
        _LyingTuple(("127.0.0.1", armed)),
        (bytearray(b"127.0.0.1"), _IndexPort(armed)),
    ):
        with socket.socket() as sock, pytest.raises(OwnDashboardRefused):
            sock.settimeout(5)
            getattr(sock, call)(address)


def test_other_hosts_are_not_guarded_whatever_the_port_type(armed):
    """A literal address that is not this host's passes with any port object (the guard
    only holds the own dashboard): the connection attempt goes out and fails on its own."""
    for port in (_IndexPort(armed), _ChangingPort(armed, 9), 9):
        with socket.socket() as sock:
            sock.settimeout(0.3)
            try:
                result = sock.connect_ex(("192.0.2.1", port))  # TEST-NET-1: unroutable
            except OwnDashboardRefused:
                raise AssertionError("another host was refused") from None
            except OSError:
                result = "os-error"
            assert result != 0  # nothing listens there; not refused by the guard


def test_port_forms_bool_float_and_unreadable(armed):
    from stiracore.yolo.own_dashboard import _refuses, normalize_port
    from stiracore.yolo.own_dashboard import armed as current

    assert normalize_port(5751) == 5751
    for unknown in (True, _IndexPort(7), 5.0, "80"):
        assert normalize_port(unknown) is None, unknown  # never evaluated
    # A bool (or any non-int port) to a local host is unknown: refused, also when its
    # value would be another port.
    assert _refuses("127.0.0.1", True, Target(1, frozenset()))
    assert _refuses("127.0.0.1", True, current())
    assert not _refuses("192.0.2.1", True, current())  # another host: not guarded
    # A float port never connects anywhere: the socket itself refuses it before the hook.
    with socket.socket() as sock, pytest.raises(TypeError):
        sock.connect(("127.0.0.1", float(armed)))

    class Broken:
        def __index__(self):
            raise RuntimeError("no")

    assert _refuses("127.0.0.1", Broken(), current())  # unreadable: refused, not allowed
    assert _refuses("127.0.0.1", 5.0, current())


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", "", " 0.0.0.0 "])
def test_a_wildcard_listener_cannot_be_armed(host):
    from stiracore.yolo.own_dashboard import WildcardBindRefused, is_wildcard

    assert is_wildcard(host)
    with pytest.raises(WildcardBindRefused):
        own_dashboard.arm(host, 5751)
    assert own_dashboard.armed() is None
    for specific in ("127.0.0.1", "::1", "192.0.2.10", "localhost"):
        assert not is_wildcard(specific)


def _local_non_loopback_address():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
        except OSError:
            return None
        address = probe.getsockname()[0]
    return None if ipaddress.ip_address(address).is_loopback else address


def test_a_listener_on_an_address_the_host_name_does_not_resolve_to_is_refused(monkeypatch):
    """The dashboard listens on one local, non-loopback address that the host name does
    not resolve to (another NIC): arming with that bind address covers it on its own."""
    address = _local_non_loopback_address()
    if address is None:
        pytest.skip("no non-loopback local IPv4 address here")
    try:
        server = _serve(host=address)
    except OSError:
        pytest.skip("cannot listen on the local address")
    try:
        port = server.server_address[1]
        assert requests.get(f"http://{address}:{port}/", timeout=5).ok  # control
        monkeypatch.setattr(
            own_dashboard.socket, "gethostname", lambda: "stira-no-such-name.invalid"
        )
        target = own_dashboard.arm(address, port)
        assert ipaddress.ip_address(address) in target.addresses
        with pytest.raises(requests.ConnectionError, match="own dashboard"):
            requests.get(f"http://{address}:{port}/api/csrf", timeout=5)
        with socket.socket() as sock, pytest.raises(OwnDashboardRefused):
            sock.connect((address.encode(), _IndexPort(port)))
    finally:
        own_dashboard.disarm()
        server.shutdown()
        server.server_close()


def test_a_socket_subclass_cannot_hide_its_family(armed):
    class Disguised(socket.socket):
        @property
        def family(self):
            return socket.AF_UNIX

    with Disguised() as sock, pytest.raises(OwnDashboardRefused):
        sock.connect(("127.0.0.1", armed))
