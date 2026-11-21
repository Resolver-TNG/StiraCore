"""ADR 0015 Addendum rev 6, slice S1 (#765): G, the start-time guard measurement.

The state machine is tested with a scripted runner and the real loopback canary; one test
runs the real browser (skipped without one unless ``STIRA_REQUIRE_BROWSER=1``).
"""

from __future__ import annotations

import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from stiracore.diagnostics import doctor
from stiracore.tools import page_check as pc
from stiracore.tools import page_check_guard as pg

BROWSER = pc.browser_status()
REQUIRE_BROWSER = os.environ.get("STIRA_REQUIRE_BROWSER", "").strip() == "1"
needs_browser = pytest.mark.skipif(
    not BROWSER.available and not REQUIRE_BROWSER,
    reason=f"no browser for page_check ({BROWSER.code}); set STIRA_REQUIRE_BROWSER=1 to fail",
)

GOOD = {
    "status": "ok",
    "reason": None,
    "loaded": True,
    "network": {"blocked": 7, "proxy_refused": 9},
    "blocked": {"outside_workspace": 2},
}


class _Files:
    """A scripted browser file: ``stat`` and ``--version`` without a process."""

    def __init__(self):
        self.mtime = 1
        self.version = "Chromium 1.0"
        self.version_reads = 0

    def stat(self, path):
        return SimpleNamespace(st_dev=1, st_ino=2, st_mtime_ns=self.mtime)

    def read_version(self, path):
        self.version_reads += 1
        return self.version

    def identity(self):
        return pg.BrowserIdentity(stat=self.stat, version=self.read_version)


def _guard(runner, *, platform="darwin", canary=None, files=None, browser="/x/chrome"):
    return pg.Guard(
        platform=platform,
        runner=runner,
        canary=canary,
        identity=(files or _Files()).identity(),
        find_browser=lambda: browser,
    )


def _silent(browser, port):
    return dict(GOOD)


def test_guard_verified_when_canary_silent():
    calls = []

    def runner(browser, port):
        calls.append((browser, port))
        return dict(GOOD)

    guard = _guard(runner)
    assert (guard.state().state, guard.state().code) == ("unmeasured", "not_run")
    state = guard.measure()
    assert (state.state, state.code, state.verified) == ("verified", "guard_verified", True)
    assert state.elapsed_ms is not None
    assert len(calls) == 1 and calls[0][0] == "/x/chrome" and calls[0][1] > 0


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"network": {"blocked": 7, "proxy_refused": 0}}, "proxy_silent"),
        ({"network": {"blocked": 0, "proxy_refused": 9}}, "proxy_silent"),
        ({"blocked": {"outside_workspace": 0}}, "file_read_uncounted"),
        ({"blocked": {}}, "file_read_uncounted"),
        ({"status": "error", "reason": "browser_exited"}, "guard_run_failed"),
        ({"reason": "time_limit", "status": "truncated"}, "guard_run_failed"),
        ({"loaded": False}, "guard_run_failed"),
        ({"network": {"blocked": True, "proxy_refused": True}}, "proxy_silent"),
    ],
)
def test_guard_unverified_when_a_count_is_missing(change, code):
    state = _guard(lambda browser, port: {**GOOD, **change}).measure()
    assert (state.state, state.code) == ("unverified", code)


def test_guard_unverified_when_the_runner_breaks_or_returns_nonsense():
    def boom(browser, port):
        raise RuntimeError("secret /path")

    assert _guard(boom).measure().code == "guard_run_failed"
    assert _guard(lambda b, p: None).measure().code == "guard_run_failed"


@pytest.mark.parametrize("channel", ["tcp", "udp"])
def test_guard_unverified_when_canary_hears(channel):
    def leaking(browser, port):
        if channel == "tcp":
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as packet:
                packet.sendto(b"stun", ("127.0.0.1", port))
        time.sleep(0.3)
        return dict(GOOD)  # the browser's own report looks clean: the canary decides

    state = _guard(leaking).measure()
    assert (state.state, state.code) == ("unverified", "canary_heard")


def test_guard_unverified_on_dead_canary():
    def dead():
        canary = pg.Canary()
        canary.close()  # it would hear nothing, whatever leaks
        return canary

    ran = []
    state = _guard(lambda b, p: ran.append(1) or dict(GOOD), canary=dead).measure()
    assert (state.state, state.code) == ("unverified", "canary_dead")
    assert ran == []  # no browser is started against a canary that cannot hear

    opened = []

    def dies_during_the_run():
        opened.append(pg.Canary())
        return opened[-1]

    def runner(browser, port):
        opened[-1].close()
        return dict(GOOD)

    state = _guard(runner, canary=dies_during_the_run).measure()
    assert (state.state, state.code) == ("unverified", "canary_dead")

    def no_port():
        raise OSError("no port")

    assert _guard(_silent, canary=no_port).measure().code == "canary_dead"


def test_guard_unmeasured_on_linux(tmp_path):
    def never(browser, port):
        raise AssertionError("no browser is started where the platform is unmeasured")

    guard = _guard(never, platform="linux")
    state = guard.measure()
    assert (state.state, state.code) == ("unmeasured", "platform_unmeasured")
    assert guard.platform_measured() is False
    # No browser: unmeasured too, not an error.
    none = _guard(never, browser=None).measure()
    assert (none.state, none.code) == ("unmeasured", "no_browser")
    # The doctor says so in an info note and stays ok.
    report = doctor.run_doctor(
        [],
        workspace=tmp_path,
        write_test=False,
        platform="linux",
        browser=pc.BrowserStatus("page_check_ready", path="/usr/bin/chromium"),
        page_check_guard={
            "guard": state.state,
            "guard_code": state.code,
            "axis": "platform_unmeasured",
        },
    )
    (note,) = [n for n in report["notes"] if n["code"].startswith("page_check")]
    assert (note["level"], note["guard"], note["guard_code"], note["axis"]) == (
        "info",
        "unmeasured",
        "platform_unmeasured",
        "platform_unmeasured",
    )
    assert report["ok"] is True
    assert "unmeasured (platform_unmeasured)" in doctor.render_text(report)


def test_doctor_note_reads_memory_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "_GUARD", _guard(_silent))
    status = pc.BrowserStatus("page_check_ready", path="/x/chrome")
    report = doctor.run_doctor([], workspace=tmp_path, write_test=False, browser=status)
    (note,) = [n for n in report["notes"] if n["code"].startswith("page_check")]
    # Nothing was measured in this process and the doctor starts no browser.
    assert (note["guard"], note["guard_code"]) == ("unmeasured", "not_run")
    assert note["axis"] in pg.AXIS_CODES
    # A value outside the fixed lists (a path, free text) is never shown.
    bad = doctor.run_doctor(
        [],
        workspace=tmp_path,
        write_test=False,
        browser=status,
        page_check_guard={"guard": "verified", "guard_code": "/etc/hosts", "axis": "pass"},
    )
    (note,) = [n for n in bad["notes"] if n["code"].startswith("page_check")]
    assert (note["guard"], note["guard_code"], note["axis"]) == (
        "unverified",
        "guard_unreadable",
        "axis_unreadable",
    )
    assert "/etc/hosts" not in doctor.render_text(bad)


def test_guard_state_fails_closed(monkeypatch):
    guard = _guard(_silent)
    guard.measure()
    guard._state = pg.GuardState("verified", "http://127.0.0.1:1/x")  # not a fixed code
    assert guard.state() == pg.GuardState("unverified", "guard_unreadable")
    guard._state = None
    assert guard.state() == pg.GuardState("unverified", "guard_unreadable")
    monkeypatch.setattr(pg, "_GUARD", None)
    assert pc.guard_state() == pg.GuardState("unverified", "guard_unreadable")
    assert pc.guard_report() == {
        "guard": "unverified",
        "guard_code": "guard_unreadable",
        "axis": pc.axis.current().code,
    }


def test_guard_drops_on_browser_guard_unverified_call(tmp_path, monkeypatch):
    from stiracore.identity_paths import WritePathPolicy

    guard = _guard(_silent)
    monkeypatch.setattr(pg, "_GUARD", guard)
    assert pc.measure_guard().state == "verified"
    ws = (tmp_path / "ws").resolve()
    ws.mkdir()
    (ws / "ok.html").write_text("<title>t</title>", encoding="utf-8")
    policy = WritePathPolicy(base_dir=ws, write_roots=[ws])
    # Another failure does not drop the state.
    assert pc.run_page_check("missing.html", policy, browser="/nonexistent")["status"] == "refused"
    pc.note_call_result("browser_error")
    assert pc.guard_state().state == "verified"
    # The per-call launch guard fails (C9): the state drops ...
    monkeypatch.setattr(pc, "launch_flags", lambda profile, port: ["--no-sandbox"])
    monkeypatch.setattr(pc, "_Browser", None)
    result = pc.run_page_check("ok.html", policy, browser="/nonexistent")
    assert (result["status"], result["reason"]) == ("error", "browser_guard_unverified")
    assert (pc.guard_state().state, pc.guard_state().code) == (
        "unverified",
        "browser_guard_unverified",
    )
    # ... and stays dropped until the next start: a new measurement does not lift it.
    assert pc.measure_guard().state == "unverified"
    assert guard.start() is False
    assert pc.guard_report()["guard"] == "unverified"


def test_guard_rerun_on_browser_version_change():
    files = _Files()
    runs = []
    guard = _guard(lambda b, p: runs.append(b) or dict(GOOD), files=files)
    assert guard.same_browser("/x/chrome") is False  # nothing measured yet: no run either
    assert runs == []
    assert guard.measure().state == "verified"
    # Same file: a stat and a comparison; no process, no new run.
    reads = files.version_reads
    assert guard.same_browser("/x/chrome") is True and guard.same_browser(None) is True
    assert (len(runs), files.version_reads) == (1, reads)
    # The browser was updated (the file changed, another --version).
    files.mtime, files.version = 2, "Chromium 2.0"
    assert guard.same_browser("/x/chrome") is False
    assert guard.state().state != "verified"  # unmeasured until the new run ends
    assert guard.ensure(5.0).state == "verified"
    assert len(runs) == 2
    assert guard.same_browser("/x/chrome") is True
    # Another path is another browser.
    assert guard.same_browser("/y/chrome") is False
    guard.ensure(5.0)
    assert runs[-1] == "/y/chrome" and len(runs) == 3


def test_guard_runs_once_in_the_background_and_the_reader_waits_a_bounded_time():
    release = threading.Event()
    threads = []

    def slow(browser, port):
        threads.append(threading.current_thread())
        release.wait(5)
        return dict(GOOD)

    guard = _guard(slow)
    assert guard.start() is True and guard.start() is False
    waiting = guard.ensure(0.05)  # the wait ran out: unmeasured, never an error
    assert (waiting.state, waiting.code) == ("unmeasured", "guard_pending")
    release.set()
    assert guard.ensure(5.0).state == "verified"
    assert len(threads) == 1 and threads[0] is not threading.current_thread()
    assert guard.start() is False  # once per process


def test_canary_counts_a_direct_probe_and_resets():
    canary = pg.Canary()
    try:
        assert canary.hits == 0 and canary.probe() is True and canary.hits == 2
        canary.reset()
        assert canary.hits == 0
    finally:
        canary.close()
    assert canary.probe() is False


def test_fixed_page_names_every_channel_and_only_the_loopback_canary():
    page = pg._G_PAGE
    for channel in (
        "fetch('http://'",
        '<img src="http://127.0.0.1:__PORT__',
        "<iframe",
        'rel="prefetch"',
        "sendBeacon",
        "WebSocket",
        "RTCPeerConnection",
        "stun:",
        "fetch('file:///etc/hosts')",
    ):
        assert channel in page, channel
    assert "__PORT__" in page and "localhost" not in page


@needs_browser
def test_guard_verified_with_a_real_browser(capsys):
    """G itself on this host (the platform check is lifted so that a Linux CI browser is
    measured too; the module's own state is not touched)."""
    started = time.monotonic()
    state = pg.Guard(platform="darwin").measure(BROWSER.path)
    wall = time.monotonic() - started
    with capsys.disabled():
        print(f"\n[G] {state.state} ({state.code}) wall={wall:.2f}s")
    assert (state.state, state.code) == ("verified", "guard_verified"), state
    assert wall < pg.GUARD_WAIT_S
