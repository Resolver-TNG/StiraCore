"""ADR 0015 phase 1 (#449): the verification browser tool ``page_check``.

The pure parts (discovery, target checks, the refusing proxy, launch flags, result shaping)
run everywhere. The real-browser tests need Chrome / Chromium / Edge; they are skipped when
none is found, unless ``STIRA_REQUIRE_BROWSER=1`` (then a missing browser fails).
"""

from __future__ import annotations

import json
import os

import pytest

from stiracore.diagnostics import doctor
from stiracore.tools import page_check as pc

# --- discovery and doctor --------------------------------------------------------------


def _no(_path):
    return False


def test_windows_is_hidden_with_a_reason():
    status = pc.browser_status(platform="win32", env={}, euid=1000)
    assert (status.available, status.code) == (False, "page_check_platform")


def test_root_is_hidden_with_a_reason():
    status = pc.browser_status(platform="linux", env={}, euid=0, which=lambda n: "/usr/bin/x")
    assert (status.available, status.code) == (False, "page_check_root")


def test_no_browser_is_hidden_with_a_reason():
    status = pc.browser_status(
        platform="linux", env={}, euid=1000, which=lambda n: None, executable=_no
    )
    assert (status.available, status.code) == (False, "page_check_no_browser")


def test_explicit_path_wins_and_a_bad_one_is_not_silently_replaced():
    good = pc.browser_status(
        platform="linux",
        env={pc.BROWSER_ENV: "/opt/chrome"},
        euid=1000,
        which=lambda n: "/usr/bin/chromium",
        executable=lambda p: p == "/opt/chrome",
    )
    assert (good.code, good.path) == ("page_check_ready", "/opt/chrome")
    bad = pc.browser_status(
        platform="linux",
        env={pc.BROWSER_ENV: "/nope"},
        euid=1000,
        which=lambda n: "/usr/bin/chromium",
        executable=_no,
    )
    assert bad.code == "page_check_no_browser" and pc.BROWSER_ENV in bad.detail


def test_path_lookup_and_mac_bundle():
    linux = pc.browser_status(
        platform="linux",
        env={},
        euid=1000,
        which=lambda n: "/usr/bin/chromium" if n == "chromium" else None,
        executable=_no,
    )
    assert (linux.code, linux.path) == ("page_check_ready", "/usr/bin/chromium")
    mac = pc.browser_status(
        platform="darwin",
        env={},
        euid=501,
        home="/Users/x",
        which=lambda n: None,
        executable=lambda p: p == "/Users/x/Applications/Chromium.app/Contents/MacOS/Chromium",
    )
    assert mac.available and mac.path.endswith("Chromium")


@pytest.mark.parametrize(
    "code", ["page_check_ready", "page_check_no_browser", "page_check_platform", "page_check_root"]
)
def test_doctor_reports_the_reason_as_an_info_note(tmp_path, code):
    status = pc.BrowserStatus(code, path="/x/chrome" if code == "page_check_ready" else None)
    report = doctor.run_doctor([], workspace=tmp_path, write_test=False, browser=status)
    notes = [n for n in report["notes"] if n["code"].startswith("page_check")]
    assert [(n["code"], n["level"]) for n in notes] == [(code, "info")]
    assert report["ok"] is True
    assert "page_check" in doctor.render_text(report)


def test_doctor_without_injection_runs_discovery(tmp_path):
    report = doctor.run_doctor([], workspace=tmp_path, write_test=False)
    codes = [n["code"] for n in report["notes"] if n["code"].startswith("page_check")]
    assert codes == [pc.browser_status().code]


BROWSER = pc.browser_status()
REQUIRE_BROWSER = os.environ.get("STIRA_REQUIRE_BROWSER", "").strip() == "1"
needs_browser = pytest.mark.skipif(
    not BROWSER.available and not REQUIRE_BROWSER,
    reason=f"no browser for page_check ({BROWSER.code}); set STIRA_REQUIRE_BROWSER=1 to fail",
)


# --- the refusing proxy ------------------------------------------------------------------


class _Canary:
    """A loopback TCP listener that counts connections (a "leak" would reach it)."""

    def __init__(self, host="127.0.0.1"):
        import socket
        import threading

        self.sock = socket.socket()
        self.sock.bind((host, 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.hits += 1
            conn.close()

    def close(self):
        self._stop.set()
        self._t.join(1)
        self.sock.close()


def _ask(proxy, raw: bytes) -> bytes:
    import socket

    with socket.create_connection(("127.0.0.1", proxy.port), timeout=3) as s:
        s.sendall(raw)
        data = b""
        while True:
            chunk = s.recv(1024)
            if not chunk:
                return data
            data += chunk


@pytest.mark.parametrize(
    ("raw", "host"),
    [
        (b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n", "example.com"),
        (b"GET http://Example.org/a?secret=1 HTTP/1.1\r\nHost: example.org\r\n\r\n", "example.org"),
        (b"GET http://127.0.0.1:%d/ HTTP/1.1\r\n\r\n", "127.0.0.1:{port}"),
        (
            b"GET ws://localhost:9/sock HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n",
            "localhost:9",
        ),
        (b"CONNECT [::1]:8443 HTTP/1.1\r\n\r\n", "::1:8443"),
    ],
)
def test_proxy_refuses_and_records_only_the_host(raw, host):
    canary = _Canary()
    try:
        with pc.RefusingProxy() as proxy:
            if b"%d" in raw:
                raw = raw % canary.port
            assert _ask(proxy, raw).startswith(b"HTTP/1.1 403")
            assert proxy.attempts == 1
            assert proxy.hosts == [host.format(port=canary.port)]
            assert all("secret" not in h and "/" not in h for h in proxy.hosts)
        assert canary.hits == 0  # never forwarded, also not to loopback
    finally:
        canary.close()


def test_proxy_counts_garbage_and_caps_the_host_list():
    with pc.RefusingProxy() as proxy:
        assert _ask(proxy, b"\x16\x03\x01 not http\r\n").startswith(b"HTTP/1.1 403")
        for i in range(pc.MAX_HOSTS + 3):
            _ask(proxy, f"CONNECT h{i}.test:443 HTTP/1.1\r\n\r\n".encode())
        assert proxy.attempts == pc.MAX_HOSTS + 4
        assert len(proxy.hosts) == pc.MAX_HOSTS and proxy.hosts_total == pc.MAX_HOSTS + 3
    assert not proxy.alive


# --- target checks (before any browser starts) -----------------------------------------


def _policy(ws, **extra):
    from stiracore.identity_paths import WritePathPolicy

    return WritePathPolicy(base_dir=ws, write_roots=[ws], **extra)


@pytest.fixture
def ws(tmp_path):
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    (root / "ok.html").write_text("<title>t</title>", encoding="utf-8")
    (root / "note.txt").write_text("x", encoding="utf-8")
    (tmp_path / "outside.html").write_text("<p>secret</p>", encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("javascript:alert(1)", "scheme_refused"),
        ("data:text/html,<p>x</p>", "scheme_refused"),
        ("http://localhost:8080/", "port_not_allowed"),  # phase 3: no port listed
        ("https://example.com/", "scheme_refused"),
        ("blob:null/1", "scheme_refused"),
        ("view-source:file:///etc/hosts", "scheme_refused"),
        ("chrome://settings", "scheme_refused"),
        ("file://example.com/ok.html", "scheme_refused"),
        ("file:///etc/hosts", "outside_workspace"),
        ("../outside.html", "outside_workspace"),
        ("note.txt", "not_page"),
        ("missing.html", "not_found"),
        ("", "invalid_target"),
        ("a\0b", "invalid_target"),
        (None, "invalid_target"),
    ],
)
def test_targets_refused_before_launch(ws, target, code, monkeypatch):
    monkeypatch.setattr(pc, "_Browser", None)  # a launch would fail loudly
    result = pc.run_page_check(target, _policy(ws), browser="/nonexistent")
    assert (result["status"], result["reason"]) == ("refused", code)


def test_symlink_out_of_the_workspace_is_refused(ws):
    (ws / "link.html").symlink_to(ws.parent / "outside.html")
    with pytest.raises(pc.PageCheckRefused) as refused:
        pc.check_target("link.html", _policy(ws))
    assert refused.value.code == "outside_workspace"


def test_read_floor_is_honoured(ws):
    (ws / "data").mkdir()
    (ws / "data" / "p.html").write_text("<p>x</p>", encoding="utf-8")
    with pytest.raises(pc.PageCheckRefused) as refused:
        pc.check_target("data/p.html", _policy(ws, floor_tree=ws / "data"))
    assert refused.value.code == "floor"


def test_file_url_and_absolute_path_inside_are_accepted(ws):
    path, url = pc.check_target((ws / "ok.html").as_uri(), _policy(ws))
    assert path == ws / "ok.html" and url.startswith("file://")
    assert pc.check_target(str(ws / "ok.html"), _policy(ws))[0] == path


def test_launch_flags_keep_the_guard():
    flags = pc.launch_flags("/tmp/p", 4242)
    assert pc._flags_safe(flags, 4242)
    for needed in (
        "--proxy-server=http://127.0.0.1:4242",
        "--proxy-bypass-list=<-loopback>",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--user-data-dir=/tmp/p",
        "--remote-debugging-pipe",
    ):
        assert needed in flags
        assert not pc._flags_safe([f for f in flags if f != needed], 4242) or needed in (
            "--user-data-dir=/tmp/p",
            "--remote-debugging-pipe",
        )
    assert not any(f.startswith(("--no-sandbox", "--remote-debugging-port")) for f in flags)
    assert not pc._flags_safe([*flags, "--no-sandbox"], 4242)


def test_an_unverified_guard_never_launches(ws, monkeypatch):
    monkeypatch.setattr(pc, "launch_flags", lambda profile, port: ["--no-sandbox"])
    monkeypatch.setattr(pc, "_Browser", None)
    result = pc.run_page_check("ok.html", _policy(ws), browser="/nonexistent")
    assert (result["status"], result["reason"]) == ("error", "browser_guard_unverified")


# --- real browser ----------------------------------------------------------------------


def _lan_address():
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent for a UDP connect
            address = s.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


class _UdpCanary:
    def __init__(self, host):
        import socket
        import threading

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.packets = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.sock.recvfrom(2048)
            except OSError:
                continue
            self.packets += 1

    def close(self):
        self._stop.set()
        self._t.join(1)
        self.sock.close()


@pytest.fixture
def launched(monkeypatch):
    """Every browser this test starts, to check it is gone afterwards."""
    started = []

    class Recording(pc._Browser):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)

    monkeypatch.setattr(pc, "_Browser", Recording)
    return started


def _gone(started):
    for browser in started:
        assert browser.proc.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(browser.proc.pid, 0)


def _own_profiles(monkeypatch):
    """The browser profiles *this* test creates (#515).

    Not a glob of the temporary directory: a page check in another process (a parallel
    test run) has its own ``stira-page-check-*`` there for a moment.
    """
    made = []
    real = pc.tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        path = real(*args, **kwargs)
        if os.path.basename(path).startswith("stira-page-check-"):
            made.append(path)
        return path

    monkeypatch.setattr(pc.tempfile, "mkdtemp", mkdtemp)
    return made


@needs_browser
def test_clean_page_ok_with_a_screenshot_and_nothing_left(ws, launched, monkeypatch):
    (ws / "clean.html").write_text("<title>Clean</title><h1>Score 0</h1>", encoding="utf-8")
    profiles = _own_profiles(monkeypatch)
    result = pc.run_page_check("clean.html", _policy(ws), wait_ms=100)
    # The whole result in the message: a rare failure under load says which part (#515).
    assert result["status"] == "ok" and result["reason"] is None and result["loaded"], result
    assert result["title"] == "Clean", result
    assert result["page_errors"]["count"] == result["console_errors"]["count"] == 0, result
    assert result["network"]["blocked"] == 0 and result["network"]["hosts"] == [], result
    shot = ws / result["screenshot"]
    assert shot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert shot.is_relative_to(ws / ".stira-verify")
    assert result["open_yourself"]["path"] == "clean.html"
    assert len(launched) == 1
    _gone(launched)
    assert len(profiles) == 1 and not os.path.exists(profiles[0]), _left_in(profiles)


def _left_in(profiles):
    from pathlib import Path

    return {p: sorted(str(f.relative_to(p)) for f in Path(p).rglob("*"))[:20] for p in profiles}


@needs_browser
def test_a_slow_browser_start_is_not_a_time_limit(ws, launched, monkeypatch, tmp_path):
    """#515: the browser's start counts against the run's limit, not the 5 s of one call.

    The start is delayed by a wrapper (no CPU load): before the fix the first call gave up
    after 5 s and a clean page came back ``truncated`` / ``time_limit`` (measured: a 4.5 s
    delay was enough), although the run's own limit (30 s) was far away.
    """
    import time

    if os.name == "nt":
        pytest.skip("a POSIX shell wrapper")
    slow = tmp_path / "slow-browser"
    slow.write_text(f'#!/bin/sh\nsleep 5.5\nexec "{BROWSER.path}" "$@"\n', encoding="utf-8")
    slow.chmod(0o755)
    (ws / "clean.html").write_text("<title>Clean</title>", encoding="utf-8")
    profiles = _own_profiles(monkeypatch)
    started = time.monotonic()
    result = pc.run_page_check(
        "clean.html", _policy(ws), browser=str(slow), wait_ms=0, screenshot=False
    )
    assert (result["status"], result["reason"], result["title"]) == ("ok", None, "Clean"), result
    assert time.monotonic() - started >= 5.5
    _gone(launched)
    assert len(profiles) == 1 and not os.path.exists(profiles[0]), _left_in(profiles)


def test_a_browser_that_never_answers_still_stops_at_the_time_limit(ws, launched, tmp_path):
    """The longer first wait is bounded by the run's limit (the measured interval: the
    whole call, a browser that never starts answering)."""
    import time

    if os.name == "nt":
        pytest.skip("a POSIX shell wrapper")
    mute = tmp_path / "mute-browser"
    mute.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    mute.chmod(0o755)
    started = time.monotonic()
    result = pc.run_page_check("ok.html", _policy(ws), browser=str(mute), time_limit=2)
    elapsed = time.monotonic() - started
    assert (result["status"], result["reason"]) == ("truncated", "time_limit"), result
    assert 2 <= elapsed < 10, elapsed
    _gone(launched)


@needs_browser
def test_errors_missing_files_and_outside_reads_are_counted(ws, launched):
    (ws / "bad.html").write_text(
        "<title>Bad</title><script src='nope.js'></script>"
        f"<img src='{(ws.parent / 'outside.html').as_uri()}'>"
        f"<iframe src='{(ws.parent / 'outside.html').as_uri()}'></iframe>"
        "<script>console.error('first', 1); console.error('second');"
        "setTimeout(() => { throw new TypeError('late') }, 10);"
        "undefinedFunction();</script>",
        encoding="utf-8",
    )
    result = pc.run_page_check("bad.html", _policy(ws), wait_ms=300)
    assert result["page_errors"]["count"] == 2
    assert any("ReferenceError" in e for e in result["page_errors"]["first"])
    assert result["console_errors"] == {"count": 2, "first": ["first 1", "second"]}
    assert result["missing_files"] == {"count": 1, "first": ["nope.js"]}
    assert result["blocked"]["outside_workspace"] == 2
    _gone(launched)


@needs_browser
@pytest.mark.parametrize("guarded", [True, False], ids=["proxy", "no-proxy-proves-the-canary"])
def test_no_channel_reaches_the_network(ws, launched, monkeypatch, guarded):
    """ADR 0015 spike as a regression test: fetch, img, iframe, prefetch, beacon, WebSocket
    and WebRTC towards a LAN canary and a loopback canary on another port."""
    lan = _lan_address()
    if lan is None:
        pytest.skip("no non-loopback address on this host")
    lan_tcp, loop_tcp, udp = _Canary(lan), _Canary("127.0.0.1"), _UdpCanary(lan)
    if not guarded:  # the baseline: without the proxy the canaries do see the page
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
        urls = [f"http://{lan}:{lan_tcp.port}", f"http://127.0.0.1:{loop_tcp.port}"]
        js = "".join(
            f"fetch('{u}/f').catch(()=>{{}});"
            f"navigator.sendBeacon('{u}/b', 'x');"
            f"try {{ new WebSocket('{u.replace('http', 'ws')}/w') }} catch (e) {{}}"
            for u in urls
        )
        tags = "".join(
            f"<img src='{u}/i.png'><iframe src='{u}/frame'></iframe>"
            f"<link rel='prefetch' href='{u}/p'>"
            for u in urls
        )
        rtc = (
            "const pc = new RTCPeerConnection({iceServers: [{urls: "
            f"'stun:{lan}:{udp.port}'}}]}});"
            "pc.createDataChannel('x');"
            "pc.createOffer().then(o => pc.setLocalDescription(o));"
        )
        (ws / "leak.html").write_text(
            f"<title>Leak</title>{tags}<script>{js}{rtc}</script>", encoding="utf-8"
        )
        result = pc.run_page_check("leak.html", _policy(ws), wait_ms=2500, screenshot=False)
        leaked = lan_tcp.hits + loop_tcp.hits + udp.packets
        if guarded:
            assert (lan_tcp.hits, loop_tcp.hits, udp.packets) == (0, 0, 0)
            assert result["network"]["blocked"] >= 8
            hosts = set(result["network"]["hosts"])
            assert {f"{lan}:{lan_tcp.port}", f"127.0.0.1:{loop_tcp.port}"} <= hosts
            assert result["blocked"]["navigations"] >= 2  # the iframes
        else:
            assert leaked > 0
    finally:
        lan_tcp.close()
        loop_tcp.close()
        udp.close()
    _gone(launched)


@needs_browser
def test_navigation_away_is_refused_and_counted(ws, launched):
    (ws / "nav.html").write_text(
        "<script>location = 'https://example.com/next?token=abc'</script>", encoding="utf-8"
    )
    result = pc.run_page_check("nav.html", _policy(ws), wait_ms=500, screenshot=False)
    assert result["blocked"]["navigations"] >= 1
    assert result["network"]["hosts"] == ["example.com"]
    assert "token" not in str(result)


@needs_browser
def test_reload_loop_stops_at_the_page_limit(ws, launched):
    (ws / "loop.html").write_text("<script>setTimeout(() => location.reload(), 30)</script>")
    result = pc.run_page_check("loop.html", _policy(ws), wait_ms=1500, screenshot=False)
    assert (result["status"], result["reason"]) == ("truncated", "page_limit")


@needs_browser
def test_a_busy_page_stops_at_the_time_limit(ws, launched):
    import time

    (ws / "busy.html").write_text("<script>setTimeout(() => { while (true) {} }, 50)</script>")
    started = time.monotonic()
    result = pc.run_page_check("busy.html", _policy(ws), wait_ms=200, time_limit=4)
    assert (result["status"], result["reason"]) == ("truncated", "time_limit")
    assert time.monotonic() - started < 12
    _gone(launched)


@needs_browser
def test_popups_downloads_and_file_choosers_are_blocked(ws, launched):
    (ws / "pop.html").write_text(
        "<a id=d download='x.txt' href='data:text/plain,hello'>d</a>"
        "<input type=file id=f>"
        "<script>document.getElementById('d').click();"
        "document.getElementById('f').click();"
        "window.open('ok.html');</script>",
        encoding="utf-8",
    )
    result = pc.run_page_check("pop.html", _policy(ws), wait_ms=800, screenshot=False)
    assert result["blocked"]["downloads"] == 1
    assert not list(ws.glob("x.txt")) and not list(ws.parent.glob("x.txt"))


# --- event handling without a browser (popups, interception) ---------------------------


class _FakeBrowser:
    def __init__(self):
        import queue

        self.sent = []
        self.inbox = queue.Queue()

    def send(self, method, params=None, session=None):
        self.sent.append((method, params, session))
        return len(self.sent)


def _fake_run(ws):
    import time

    run = pc._Run(_FakeBrowser(), _policy(ws), time.monotonic() + 5)
    run.main_target, run.main_session, run.known_pages = "MAIN", "S", {"MAIN"}
    return run


def test_a_new_page_target_is_closed_and_counted(ws):
    run = _fake_run(ws)
    run.on_event(
        {
            "method": "Target.targetCreated",
            "params": {"targetInfo": {"targetId": "P", "type": "page"}},
        }
    )
    run.on_event(
        {
            "method": "Target.targetCreated",
            "params": {"targetInfo": {"targetId": "P", "type": "page"}},
        }
    )
    assert run.blocked["popups"] == 1
    assert run.b.sent == [("Target.closeTarget", {"targetId": "P"}, None)]


@pytest.mark.parametrize(
    ("url", "kind", "verdict", "counter"),
    [
        ("{ws}/ok.html", "Script", "Fetch.continueRequest", None),
        ("{out}/outside.html", "Image", "Fetch.failRequest", "outside_workspace"),
        ("data:text/plain,x", "Image", "Fetch.continueRequest", None),
        ("https://cdn.test/x?q=1", "Script", "Fetch.continueRequest", None),
        ("https://cdn.test/", "Document", "Fetch.failRequest", "navigations"),
        ("chrome-extension://abc/x.js", "Script", "Fetch.failRequest", "other"),
        ("filesystem:file:///x", "Script", "Fetch.failRequest", "other"),
    ],
)
def test_interception_rules(ws, url, kind, verdict, counter):
    run = _fake_run(ws)
    url = url.format(ws=ws.as_uri(), out=ws.parent.as_uri())
    params = {"requestId": "r", "request": {"url": url}, "resourceType": kind, "frameId": "F"}
    run.on_event({"method": "Fetch.requestPaused", "params": params, "sessionId": "S"})
    assert run.b.sent[-1][0] == verdict
    expected = {k: 0 for k in run.blocked}
    if counter:
        expected[counter] = 1
    assert run.blocked == expected
    if url.startswith("https"):
        assert run.page_hosts == {"cdn.test": None}


# --- M1: every session's guard is confirmed before it runs (fail closed) -----------------


class _ScriptedBrowser:
    """A fake CDP pipe. Each command is answered per ``how[(method, session)]`` or
    ``how[method]``: ``"ok"`` (default), ``"error"``, ``"silent"`` (never) or a delay in
    seconds. ``log`` keeps ``("send" | "reply", method, session)`` in the order they happen.
    ``events[method]``: events pushed once, right after that command is answered."""

    def __init__(self, how=None, results=None, events=None):
        import queue
        import threading

        self.inbox = queue.Queue()
        self.log = []
        self.how = how or {}
        self.results = results or {}
        self.events = events or {}
        self.closed = False
        self._n = 0
        self._lock = threading.Lock()

    def send(self, method, params=None, session=None):
        import threading

        with self._lock:
            self._n += 1
            n = self._n
            self.log.append(("send", method, session))
        how = self.how.get((method, session), self.how.get(method, "ok"))
        if how == "silent":
            return n
        if isinstance(how, (int, float)):
            threading.Timer(how, self._answer, (n, method, session, "ok")).start()
        else:
            self._answer(n, method, session, how)
        return n

    def _answer(self, n, method, session, how):
        with self._lock:
            self.log.append(("reply", method, session))
        if how == "error":
            self.inbox.put({"id": n, "error": {"code": -32601, "message": "not found"}})
        else:
            self.inbox.put({"id": n, "result": self.results.get(method, {})})
        for event in self.events.pop(method, ()):  # once
            self.inbox.put(event)

    def close(self):
        self.closed = True

    def index(self, kind, method, session):
        return self.log.index((kind, method, session))

    def sent(self, method, session=None):
        return ("send", method, session) in self.log


def _attached(session, kind, target="T1"):
    return {
        "method": "Target.attachedToTarget",
        "sessionId": "S",
        "params": {
            "sessionId": session,
            "targetInfo": {"targetId": target, "type": kind},
            "waitingForDebugger": True,
        },
    }


def _scripted_run(ws, browser):
    import time

    run = pc._Run(browser, _policy(ws), time.monotonic() + 10)
    run.main_target, run.main_session, run.known_pages = "MAIN", "S", {"MAIN"}
    return run


def test_a_child_runs_only_after_its_fetch_guard_is_confirmed(ws):
    import time

    fake = _ScriptedBrowser({("Fetch.enable", "C"): 0.3})
    run = _scripted_run(ws, fake)
    fake.inbox.put(_attached("C", "iframe"))
    run.pump(time.monotonic() + 1.5)
    resumed = fake.index("send", "Runtime.runIfWaitingForDebugger", "C")
    assert fake.index("reply", "Fetch.enable", "C") < resumed
    assert fake.index("reply", "Target.setAutoAttach", "C") < resumed


@pytest.mark.parametrize(
    ("how", "step"),
    [
        ({("Fetch.enable", "C"): "error"}, "Fetch.enable"),
        ({("Fetch.enable", "C"): "silent"}, "Fetch.enable"),
        ({("Target.setAutoAttach", "C"): "error"}, "Target.setAutoAttach"),
    ],
    ids=["fetch-error", "fetch-no-answer", "auto-attach-error"],
)
def test_a_child_whose_guard_fails_is_never_resumed(ws, monkeypatch, how, step):
    import time

    monkeypatch.setattr(pc, "ENABLE_TIMEOUT_S", 0.3)
    fake = _ScriptedBrowser(how)
    run = _scripted_run(ws, fake)
    fake.inbox.put(_attached("C", "iframe"))
    with pytest.raises(pc._GuardFailed) as failed:
        run.pump(time.monotonic() + 2)
    assert str(failed.value) == step
    assert not fake.sent("Runtime.runIfWaitingForDebugger", "C")


def test_a_dedicated_worker_needs_only_the_auto_attach(ws):
    """Measured: a worker has no Fetch domain; its requests pause in the owning frame."""
    import time

    fake = _ScriptedBrowser({("Fetch.enable", "W"): "error"})
    run = _scripted_run(ws, fake)
    fake.inbox.put(_attached("W", "worker", "TW"))
    run.pump(time.monotonic() + 0.5)
    assert not fake.sent("Fetch.enable", "W")
    resumed = fake.index("send", "Runtime.runIfWaitingForDebugger", "W")
    assert fake.index("reply", "Target.setAutoAttach", "W") < resumed


def test_children_attaching_during_a_guard_wait_queue_up(ws):
    import time

    nested = _attached("D", "iframe", "T2")
    fake = _ScriptedBrowser({("Fetch.enable", "C"): 0.2}, events={"Network.enable": [nested]})
    run = _scripted_run(ws, fake)
    fake.inbox.put(_attached("C", "iframe"))
    run.pump(time.monotonic() + 1.5)
    for child in ("C", "D"):
        resumed = fake.index("send", "Runtime.runIfWaitingForDebugger", child)
        assert fake.index("reply", "Fetch.enable", child) < resumed


def _fake_launch(monkeypatch, how, events=None):
    made = []

    def factory(_executable, _flags):
        fake = _ScriptedBrowser(
            how,
            results={
                "Target.createTarget": {"targetId": "MAIN"},
                "Target.getTargets": {"targetInfos": [{"targetId": "MAIN", "type": "page"}]},
                "Target.attachToTarget": {"sessionId": "S"},
            },
            events=events,
        )
        made.append(fake)
        return fake

    monkeypatch.setattr(pc, "_Browser", factory)
    return made


def test_main_page_is_not_opened_when_its_guard_fails(ws, monkeypatch):
    made = _fake_launch(monkeypatch, {("Fetch.enable", "S"): "error"})
    result = pc.run_page_check("ok.html", _policy(ws), browser="/fake", screenshot=False)
    assert (result["status"], result["reason"]) == ("error", "browser_guard_unverified")
    assert result["guard_failed"] == "Fetch.enable"
    (fake,) = made
    assert not fake.sent("Page.navigate", "S") and fake.closed


def test_a_frame_whose_guard_fails_ends_the_run(ws, monkeypatch):
    made = _fake_launch(
        monkeypatch,
        {("Fetch.enable", "C"): "error"},
        events={"Page.navigate": [_attached("C", "iframe")]},
    )
    result = pc.run_page_check("ok.html", _policy(ws), browser="/fake", screenshot=False)
    assert (result["status"], result["reason"]) == ("error", "browser_guard_unverified")
    (fake,) = made
    assert not fake.sent("Runtime.runIfWaitingForDebugger", "C") and fake.closed


def test_a_malformed_screenshot_is_a_browser_error_not_an_exception(ws, monkeypatch):
    _fake_launch(
        monkeypatch,
        {},
        events={"Page.navigate": [{"method": "Page.loadEventFired", "sessionId": "S"}]},
    )
    monkeypatch.setattr(
        pc._Run,
        "call",
        _with_result(pc._Run.call, "Page.captureScreenshot", {"data": "abc"}),
    )
    result = pc.run_page_check("ok.html", _policy(ws), browser="/fake", wait_ms=0)
    assert (result["status"], result["reason"]) == ("error", "browser_error")


def _with_result(call, method, result):
    def patched(self, name, *args, **kwargs):
        reply = call(self, name, *args, **kwargs)
        return result if name == method else reply

    return patched


@needs_browser
def test_frames_and_workers_still_run_under_the_guard(ws, launched):
    (ws / "sub.html").write_text("<script>fetch('https://sub.test/')</script>", encoding="utf-8")
    (ws / "kids.html").write_text(
        "<title>K</title><iframe src='sub.html'></iframe>"
        "<iframe src='https://frame.test/'></iframe><script>"
        "const src = \"fetch('https://worker.test/').catch(() => {})\";"
        "new Worker(URL.createObjectURL(new Blob([src])));</script>",
        encoding="utf-8",
    )
    result = pc.run_page_check("kids.html", _policy(ws), wait_ms=1500, screenshot=False)
    assert result["status"] == "ok", result
    assert {"sub.test", "frame.test", "worker.test"} <= set(result["network"]["hosts"])
    _gone(launched)


# --- registration, rules, YOLO, logs ----------------------------------------------------


def _subprocess_registry(env_extra):
    import json
    import subprocess
    import sys

    code = (
        "import json; from stiracore.tools import tool_registry as r, tool_config as c;"
        "from pathlib import Path;"
        "d = c.ToolConfig.first_run_defaults(Path('/nonexistent/x.json'));"
        "print(json.dumps({'registered': 'page_check' in r.WRITE_TOOLS,"
        " 'rule': d.policy_of('page_check') if 'page_check' in r.WRITE_TOOLS else None,"
        " 'status': r.PAGE_CHECK_STATUS.code}))"
    )
    env = {**os.environ, **env_extra}
    out = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_not_registered_without_a_browser():
    seen = _subprocess_registry({pc.BROWSER_ENV: "/nonexistent/chrome"})
    assert seen == {"registered": False, "rule": None, "status": "page_check_no_browser"}


@needs_browser
def test_registered_as_ask_on_a_new_install():
    seen = _subprocess_registry({})
    assert seen == {"registered": True, "rule": "ask", "status": "page_check_ready"}


def test_never_answered_by_yolo():
    from stiracore.yolo import tiers

    assert tiers.tool_class("page_check") == tiers.EXEC
    assert "page_check" in tiers.PERSON_TOOLS and "page_check" not in tiers.INTERNAL_TOOLS


@needs_browser
def test_offered_only_with_the_workspace_policy(ws):
    from stiracore.tools import tool_registry

    def names(**kw):
        return [t.tool_name for t in tool_registry.get_tools(**kw)]

    assert "page_check" not in names()
    tools = {t.tool_name: t for t in tool_registry.get_tools(write_policy=_policy(ws))}
    assert isinstance(tools["page_check"], pc.WorkspacePageCheck)
    assert "page_check" not in names(breaker_level="warning", write_policy=_policy(ws))


@needs_browser
def test_the_tool_runs_through_the_registry_wrapper(ws):
    import asyncio
    import json

    from stiracore.tools import tool_registry

    (ws / "t.html").write_text("<title>T</title><script>nope()</script>", encoding="utf-8")
    tool = {t.tool_name: t for t in tool_registry.get_tools(write_policy=_policy(ws))}["page_check"]

    async def run():
        use = {"toolUseId": "u1", "name": "page_check", "input": {"target": "t.html", "wait_ms": 0}}
        return [event async for event in tool.stream(use, {})]

    (event,) = asyncio.run(run())
    result = event.tool_result
    assert result["toolUseId"] == "u1" and result["status"] == "success"
    data = json.loads(result["content"][0]["text"])
    assert data["page_errors"] == {"count": 1} and "title" not in data
    assert '"title": "T"' in result["content"][1]["text"]  # page content, framed (phase 2)


def test_the_log_keeps_counts_and_the_relative_target_only():
    import json

    from stiracore.tools.file_tools import payload_for_log

    result = {
        "status": "ok",
        "reason": None,
        "title": "SECRET-TITLE",
        "page_errors": {"count": 2, "first": ["TypeError: SECRET-MSG"]},
        "console_errors": {"count": 0, "first": []},
        "missing_files": {"count": 1, "first": ["secret-name.js"]},
        "network": {"blocked": 3, "hosts": ["secret.example"], "hosts_total": 1},
        "blocked": {"outside_workspace": 1},
        "screenshot": ".stira-verify/r/1.png",
        "open_yourself": {"path": "games/hockey.html", "url": "file:///Users/someone/w/x.html"},
    }
    value = {"status": "success", "content": [{"text": json.dumps(result)}]}
    logged = json.dumps(payload_for_log("page_check", "output", value))
    for secret in ("SECRET", "secret", "stira-verify", "file://", "/Users", "someone"):
        assert secret not in logged
    kept = json.loads(json.loads(logged)["content"][0]["text"])
    assert kept["page_errors"] == 2 and kept["network"]["blocked"] == 3
    assert kept["screenshot"] is True
    assert kept["target"] == "@workspace/games/hockey.html"


def test_the_input_log_keeps_the_workspace_relative_target_only(ws, monkeypatch):
    from stiracore.tools.file_tools import payload_for_log

    monkeypatch.setattr(pc, "_LOG_WORKSPACES", set())
    pc.WorkspacePageCheck(pc.page_check, _policy(ws))  # the registry's wrapper remembers it
    (ws / "games").mkdir()
    home = os.path.expanduser("~")
    cases = {
        "games/hockey.html": "@workspace/games/hockey.html",
        "./games/../games/hockey.html": "@workspace/games/hockey.html",
        str(ws / "games" / "hockey.html"): "@workspace/games/hockey.html",
        (ws / "games" / "hockey.html").as_uri(): "@workspace/games/hockey.html",
        "../outside.html": "refused:outside_workspace",
        str(ws.parent / "outside.html"): "refused:outside_workspace",
        "~/Desktop/private.html": "refused:outside_workspace",
        "file:///etc/hosts": "refused:outside_workspace",
        "javascript:alert(1)": "refused:scheme_refused",
        "https://example.com/?token=***": "refused:scheme_refused",
        "": "refused:invalid_target",
    }
    for target, expected in cases.items():
        logged = payload_for_log(
            "page_check", "input", {"target": target, "wait_ms": 500, "note": "SECRET"}
        )
        assert logged == {"target": expected, "wait_ms": 500}, target
        assert home not in str(logged) and str(ws) not in str(logged)
    assert payload_for_log("page_check", "input", ["x"]) == {"omitted": "page_check input"}
    assert payload_for_log("page_check", "input", {"target": None}) == {
        "target": "refused:invalid_target"
    }


def test_the_input_log_without_a_known_workspace_keeps_no_absolute_path(monkeypatch):
    monkeypatch.setattr(pc, "_LOG_WORKSPACES", set())
    assert pc.target_for_log("/Users/someone/w/x.html") == "refused:outside_workspace"
    assert pc.target_for_log("x" * 5000) == "refused:invalid_target"
    assert len(pc.target_for_log("a/" * 1000 + "x.html")) == pc.LOG_PATH_CHARS


def test_only_the_last_runs_are_kept(ws):
    for i in range(pc.KEEP_RUNS + 2):
        (ws / pc.SHOT_DIR / f"2026-run-{i:02d}").mkdir(parents=True)
    path, problem = pc._save_screenshot(_policy(ws), b"\x89PNG\r\n\x1a\n")
    assert problem is None and (ws / path).exists()
    runs = sorted(p.name for p in (ws / pc.SHOT_DIR).iterdir())
    assert len(runs) == pc.KEEP_RUNS and str(path).split("/")[1] in runs


def test_a_refused_screenshot_write_is_reported(tmp_path):
    from stiracore.identity_paths import WritePathPolicy

    ws = (tmp_path / "w").resolve()
    ws.mkdir()
    other = (tmp_path / "other").resolve()
    other.mkdir()
    policy = WritePathPolicy(base_dir=ws, write_roots=[other])
    assert pc._save_screenshot(policy, b"x") == (None, "screenshot_refused")
    assert not (ws / pc.SHOT_DIR).exists()


@needs_browser
def test_long_and_many_messages_say_output_truncated(ws):
    (ws / "many.html").write_text(
        "<script>for (let i = 0; i < 5; i++) console.error('e' + i);"
        "console.error('x'.repeat(5000));</script>",
        encoding="utf-8",
    )
    result = pc.run_page_check("many.html", _policy(ws), wait_ms=100, screenshot=False)
    assert (result["status"], result["reason"]) == ("truncated", "output_truncated")
    assert result["console_errors"]["count"] == 6 and len(result["console_errors"]["first"]) == 3
    assert len(json.dumps(result)) < 4000


def test_short_cuts_long_messages():
    assert len(pc._short("y" * 1000)) == pc.SUMMARY_CHARS
    assert pc._short("a\n  b") == "a b"
