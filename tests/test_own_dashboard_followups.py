"""#499: follow-ups of the own-dashboard guard (#495 / PR #498), one test group per item.

1. the observer dashboard's port (``dashboard/app.py``, same config files) is guarded too;
2. start-up does not wait for this host name's lookup (looked up on first need, kept);
3. no in-process call of the server reaches its own port (start, a chat turn, requests
   served, stop): nothing is refused there, and a refusal is logged (never silent);
4. ``http_request`` registered without its module loaded: the redirect check is not
   installed, and that is logged once;
5. (in ``tests/test_breaker_write_paths.py``) every write route of the server is classified
   and none of them writes ``stira_config_overrides``.

Real sockets on loopback, no external network, no model call. No new setting.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import subprocess
import sys
import threading

import pytest
import requests

from stiracore.yolo import own_dashboard
from stiracore.yolo.own_dashboard import (
    OBSERVER_PORT,
    REASON_CODE,
    OwnDashboardRefused,
    Target,
    is_own_dashboard,
    url_refusal,
)
from tests.test_yolo_own_dashboard import _local_non_loopback_address, _serve

PUBLIC = "93.184.216.34"  # a literal public address: no network needed to read it


@pytest.fixture
def two_dashboards():
    """Two loopback servers: the chat server dashboard and an observer on another port."""
    stira, observer = _serve(), _serve()
    try:
        yield stira.server_address[1], observer.server_address[1]
    finally:
        own_dashboard.disarm()
        for server in (stira, observer):
            server.shutdown()
            server.server_close()


# --- 1. the observer's port ---------------------------------------------------------------


def test_the_observer_port_is_refused_like_the_dashboard(two_dashboards):
    port, observer = two_dashboards
    assert requests.get(f"http://127.0.0.1:{observer}/", timeout=5).ok  # control: reachable
    target = own_dashboard.arm("127.0.0.1", port, also_ports=(observer,))
    assert target.ports == {port, observer}
    for host in ("127.0.0.1", "localhost", "[::ffff:127.0.0.1]"):
        assert url_refusal(f"http://{host}:{observer}/api/config/hub", target), host
    with pytest.raises(requests.ConnectionError, match="own dashboard"):
        requests.get(f"http://127.0.0.1:{observer}/api/csrf", timeout=5)
    with socket.socket() as sock, pytest.raises(OwnDashboardRefused):
        sock.connect(("127.0.0.1", observer))


def test_without_the_observer_port_it_stays_reachable(two_dashboards):
    """The mutation check: arming without ``also_ports`` leaves the observer open."""
    port, observer = two_dashboards
    target = own_dashboard.arm("127.0.0.1", port)
    assert url_refusal(f"http://127.0.0.1:{observer}/", target) is None
    assert requests.get(f"http://127.0.0.1:{observer}/", timeout=5).ok


def test_the_observer_port_covers_every_local_address_and_nothing_else():
    """Every address of this host at the observer's port is refused; other hosts and other
    ports are not touched (the observer may listen on another interface)."""
    target = Target(6100, frozenset(), also_ports=frozenset({6101}))
    assert is_own_dashboard("127.0.0.1", 6101, target)
    assert is_own_dashboard("::1", 6101, target)
    assert not is_own_dashboard(PUBLIC, 6101, target)
    assert not is_own_dashboard("127.0.0.1", 6102, target)
    address = _local_non_loopback_address()
    if address is not None:
        assert is_own_dashboard(address, 6101, target)
        assert not is_own_dashboard(address, 6100, target)  # the main port: listen address only


def test_the_server_arms_the_observer_port_and_the_observer_listens_there(monkeypatch):
    from dashboard import app as observer_app
    from dashboard import stira_server

    seen = []
    monkeypatch.setattr(
        stira_server, "serve", lambda application, host, port: seen.append(own_dashboard.armed())
    )
    monkeypatch.setattr(stira_server, "_install_log_masking", lambda *a, **k: [])
    monkeypatch.setattr(stira_server.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(stira_server, "_yolo_config_section", lambda path: None)
    for name in ("STIRA_YOLO_ALLOW", "STIRA_ALLOW_REMOTE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STIRA_HOST", "127.0.0.1")
    try:
        stira_server.main(app_factory=lambda lifecycle: object())
    finally:
        own_dashboard.disarm()
    (target,) = seen
    assert OBSERVER_PORT in target.ports and stira_server.PORT in target.ports
    assert observer_app.OBSERVER_PORT == OBSERVER_PORT  # the port app.py's __main__ uses


# --- 2. start-up cost: this host name is looked up on first need ------------------------


@pytest.fixture
def counted_host_name(monkeypatch):
    calls = []

    def gethostname():
        calls.append(1)
        return "localhost"  # resolves without the network

    monkeypatch.setattr(own_dashboard.socket, "gethostname", gethostname)
    return calls


def test_arming_does_not_look_up_the_host_name(counted_host_name, two_dashboards):
    port, _observer = two_dashboards
    target = own_dashboard.arm("127.0.0.1", port)
    assert counted_host_name == []
    # Loopback and the listen address are decided without it.
    with socket.socket() as sock, pytest.raises(OwnDashboardRefused):
        sock.connect(("127.0.0.1", port))
    assert url_refusal(f"http://localhost:{port}/", target)
    assert counted_host_name == []
    # Another host at the dashboard's port needs the lookup: once, then it is kept.
    assert not is_own_dashboard(PUBLIC, port, target)
    assert not is_own_dashboard(PUBLIC, port, target)
    assert counted_host_name == [1]
    # Other ports never need it.
    assert not is_own_dashboard(PUBLIC, port + 1, target)
    assert counted_host_name == [1]


def test_an_address_of_the_host_name_is_still_refused(monkeypatch):
    """Lazy, not dropped: an address that only the host name resolves to is the dashboard."""
    monkeypatch.setattr(own_dashboard.socket, "gethostname", lambda: "stira-host.example")
    own_address = ipaddress.ip_address("192.0.2.77")
    monkeypatch.setattr(
        own_dashboard,
        "_resolve",
        lambda host: {own_address} if host == "stira-host.example" else set(),
    )
    target = own_dashboard.target_for("127.0.0.1", 6200)
    assert own_address not in target.addresses
    assert is_own_dashboard("192.0.2.77", 6200, target)
    assert not is_own_dashboard("192.0.2.78", 6200, target)


def test_a_failing_host_name_lookup_refuses_nothing_extra(monkeypatch):
    def broken():
        raise OSError("no name")

    monkeypatch.setattr(own_dashboard.socket, "gethostname", broken)
    target = own_dashboard.target_for("127.0.0.1", 6201)
    assert is_own_dashboard("127.0.0.1", 6201, target)
    assert not is_own_dashboard(PUBLIC, 6201, target)


# --- 3. in-process self-calls ---------------------------------------------------------------


def test_a_refusal_is_logged_without_the_address(two_dashboards, caplog):
    port, _observer = two_dashboards
    own_dashboard.arm("127.0.0.1", port)
    with (
        caplog.at_level(logging.WARNING, logger=own_dashboard.__name__),
        socket.socket() as sock,
        pytest.raises(OwnDashboardRefused),
    ):
        sock.connect(("127.0.0.1", port))
    records = [r.getMessage() for r in caplog.records if r.name == own_dashboard.__name__]
    assert len(records) == 1 and records[0].startswith(REASON_CODE)
    assert "127.0.0.1" not in records[0] and str(port) not in records[0]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_no_in_process_call_reaches_the_servers_own_port(tmp_path, monkeypatch, caplog):
    """Armed **before** the app is built (on the port it will listen on), then the start,
    a chat turn, requests served to another process and the **stop** (run here, not in a
    teardown): nothing inside the server connects to its own port, so the always-on guard
    refuses nothing, and nothing is logged. Checked after the stop has finished."""
    from werkzeug.serving import make_server

    from dashboard import stira_server
    from tests.test_new_session import make_stira

    refusals = []
    real = own_dashboard._refuses

    def counted(host, port, target):
        refused = real(host, port, target)
        if refused:
            refusals.append((host, port))
        return refused

    monkeypatch.setattr(own_dashboard, "_refuses", counted)
    port = _free_port()
    caplog.set_level(logging.WARNING, logger=own_dashboard.__name__)
    own_dashboard.arm("127.0.0.1", port, also_ports=(OBSERVER_PORT,))
    monkeypatch.setenv("STIRA_SCHEDULER", "1")  # its thread starts and stops too
    generator = make_stira(tmp_path, monkeypatch)
    server = thread = None
    try:
        stira = next(generator)  # create_app runs while armed
        server = make_server("127.0.0.1", port, stira["app"], threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stira["chat"]("hello, how are you today?")
        client_code = (
            "import sys, urllib.request\n"
            "base = sys.argv[1]\n"
            "for path in ('/api/gateway/status', '/api/config', '/chat', '/api/csrf'):\n"
            "    with urllib.request.urlopen(base + path, timeout=10) as r:\n"
            "        assert r.status == 200, (path, r.status)\n"
            "print('ok')\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", client_code, f"http://127.0.0.1:{port}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={"PATH": "", "NO_PROXY": "*"},
        )
        assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-800:]
        # The stop, still armed: the server's own shutdown and the app's bounded stops.
        server.shutdown()
        thread.join(5)
        assert not thread.is_alive()
        stopped = stira_server.shutdown_app(stira["app"])
        server.server_close()
        server = None
        assert refusals == [], refusals
        assert not [r for r in caplog.records if REASON_CODE in r.getMessage()]
        assert "scheduler" in stopped, stopped
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        own_dashboard.disarm()
        next(generator, None)


# --- 4. the redirect check when the module is not loaded --------------------------------


def test_a_missing_http_request_module_is_logged_once(monkeypatch, caplog):
    from stiracore.tools import own_dashboard_gate

    monkeypatch.setattr(own_dashboard_gate, "_MISSING_LOGGED", False)
    tools = {"http_request": object()}
    with caplog.at_level(logging.WARNING, logger=own_dashboard_gate.__name__):
        assert own_dashboard_gate.install_registered_redirect_check(tools, {}) is False
        assert own_dashboard_gate.install_registered_redirect_check(tools, {}) is False
    lines = [
        r.getMessage() for r in caplog.records if "redirect check not installed" in r.getMessage()
    ]
    assert len(lines) == 1, lines


def test_no_http_request_tool_logs_nothing(monkeypatch, caplog):
    from stiracore.tools import own_dashboard_gate

    monkeypatch.setattr(own_dashboard_gate, "_MISSING_LOGGED", False)
    with caplog.at_level(logging.WARNING, logger=own_dashboard_gate.__name__):
        assert own_dashboard_gate.install_registered_redirect_check({}, {}) is False
    assert not caplog.records


def test_the_loaded_module_gets_the_redirect_check(monkeypatch):
    upstream = pytest.importorskip("strands_tools.http_request")
    from stiracore.tools import own_dashboard_gate, tool_registry

    assert "http_request" in tool_registry.WRITE_TOOLS
    modules = {"strands_tools.http_request": upstream}
    assert own_dashboard_gate.install_registered_redirect_check(tool_registry.WRITE_TOOLS, modules)
    assert getattr(upstream._SafeRedirectSession, "_stira_own_dashboard_check", False)


def test_the_registry_uses_the_logged_install():
    """The registry goes through the logged helper (not a silent ``sys.modules`` check)."""
    import inspect

    from stiracore.tools import tool_registry

    source = inspect.getsource(tool_registry)
    assert "install_registered_redirect_check(WRITE_TOOLS, sys.modules)" in source
    assert '"strands_tools.http_request" in sys.modules' not in source


def test_the_observer_port_on_the_main_port_still_covers_every_local_address(monkeypatch):
    """Review of PR #563: the chat server on loopback and the observer on another interface can share
    one port number. The observer's 'every local address' must not be dropped by the
    de-duplication of ports (the host name here resolves to nothing)."""
    address = _local_non_loopback_address()
    if address is None:
        pytest.skip("no non-loopback local IPv4 address here")
    monkeypatch.setattr(own_dashboard, "_resolve", lambda host: set())
    target = own_dashboard.target_for("127.0.0.1", 6300, also_ports=(6300,))
    assert target.ports == {6300}
    assert is_own_dashboard(address, 6300, target)
    assert not is_own_dashboard(PUBLIC, 6300, target)
    plain = own_dashboard.target_for("127.0.0.1", 6300)  # no observer: listen address only
    assert not is_own_dashboard(address, 6300, plain)
