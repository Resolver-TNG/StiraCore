"""ADR 0015 Addendum rev 6, slice S1b (#765): the chat server begins G once at its start,
and the settings page's Diagnostics section shows the guard state in a read-only row.

No browser is started here: the start function, the guard's runner and its clock are scripted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask

from stiracore.diagnostics.write_errors import WriteFailureBoard
from stiracore.observability.diagnostics_api import create_diagnostics_blueprint
from stiracore.tools import page_check as pc
from stiracore.tools import page_check_guard as pg

ROOT = Path(__file__).resolve().parent.parent
GOOD = {
    "status": "ok",
    "reason": None,
    "loaded": True,
    "network": {"blocked": 7, "proxy_refused": 9},
    "blocked": {"outside_workspace": 2},
}
UNREADABLE = {
    "guard": "unverified",
    "guard_code": "guard_unreadable",
    "axis": "axis_unreadable",
    "measured_age_s": None,
}
ROW_KEYS = {"guard", "guard_code", "axis", "measured_age_s"}


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _scripted_guard(clock):
    files = SimpleNamespace(st_dev=1, st_ino=2, st_mtime_ns=3)
    return pg.Guard(
        clock=clock,
        platform="darwin",
        runner=lambda browser, port: dict(GOOD),
        identity=pg.BrowserIdentity(stat=lambda path: files, version=lambda path: "Chromium 1.0"),
        find_browser=lambda: "/x/chrome",
    )


@pytest.fixture
def stira_server():
    from dashboard import stira_server

    return stira_server


# --- the start-up call ------------------------------------------------------------------


def test_server_start_begins_g_once(stira_server, monkeypatch):
    status = SimpleNamespace(available=True, path="/x/chrome")
    monkeypatch.setattr(stira_server.tool_registry, "PAGE_CHECK_STATUS", status)
    started = []
    assert stira_server._start_page_guard(lambda path: started.append(path) or True) == "started"
    assert started == ["/x/chrome"]
    # A measurement that already exists or runs is not started again.
    assert stira_server._start_page_guard(lambda path: False) == "not_started"
    # Without an injected function it is the tool module's own starter, with the same path.
    monkeypatch.setattr(
        stira_server.page_check_tool,
        "start_guard_measurement",
        lambda path: started.append(("module", path)) or True,
    )
    assert stira_server._start_page_guard() == "started"
    assert started[-1] == ("module", "/x/chrome")


def test_serve_begins_g_once_after_the_listener_exists(stira_server, monkeypatch):
    import werkzeug.serving

    order = []
    server = SimpleNamespace(
        log_startup=lambda: order.append("log_startup"),
        serve_forever=lambda: order.append("serve_forever"),
        server_close=lambda: order.append("server_close"),
    )
    sock = SimpleNamespace(fileno=lambda: 7, close=lambda: order.append("sock_close"))
    monkeypatch.setattr(stira_server, "listen_socket", lambda host, port: sock)
    monkeypatch.setattr(
        werkzeug.serving, "make_server", lambda *a, **k: order.append("make_server") or server
    )
    monkeypatch.setattr(stira_server, "_start_page_guard", lambda: order.append("guard"))
    stira_server.serve(object(), "127.0.0.1", 0)
    assert order.count("guard") == 1
    assert order.index("make_server") < order.index("guard") < order.index("serve_forever")

    # A port that cannot be bound starts no measurement.
    def refused(host, port):
        raise stira_server.PortUnavailableError("in use")

    order.clear()
    monkeypatch.setattr(stira_server, "listen_socket", refused)
    with pytest.raises(stira_server.PortUnavailableError):
        stira_server.serve(object(), "127.0.0.1", 0)
    assert order == []


def test_server_start_without_a_browser_starts_nothing_and_never_fails(
    stira_server, monkeypatch, capsys
):
    def never(path):
        raise AssertionError("no measurement is started without a browser")

    for status in (
        SimpleNamespace(available=False, path=None, code="page_check_no_browser"),
        SimpleNamespace(available=False, path="/x/chrome", code="page_check_root"),
        SimpleNamespace(available=True, path=None),
    ):
        monkeypatch.setattr(stira_server.tool_registry, "PAGE_CHECK_STATUS", status)
        assert stira_server._start_page_guard(never) == "no_browser"
    assert capsys.readouterr().out == ""

    # A starter that breaks, or a status that cannot be read: one line with the type name.
    def boom(path):
        raise RuntimeError("secret /Users/someone/chrome")

    monkeypatch.setattr(
        stira_server.tool_registry,
        "PAGE_CHECK_STATUS",
        SimpleNamespace(available=True, path="/x/chrome"),
    )
    assert stira_server._start_page_guard(boom) == "failed"
    monkeypatch.setattr(stira_server.tool_registry, "PAGE_CHECK_STATUS", None)
    assert stira_server._start_page_guard(never) == "failed"
    out = capsys.readouterr().out
    assert "RuntimeError" in out and "AttributeError" in out
    assert "secret" not in out and "/x/chrome" not in out and "someone" not in out


def test_server_stop_waits_for_a_running_g_and_starts_none(stira_server, monkeypatch, capsys):
    import threading

    release, entered = threading.Event(), threading.Event()

    def slow(browser, port):
        entered.set()
        release.wait(5)
        return dict(GOOD)

    files = SimpleNamespace(st_dev=1, st_ino=2, st_mtime_ns=3)
    guard = pg.Guard(
        platform="darwin",
        runner=slow,
        identity=pg.BrowserIdentity(stat=lambda path: files, version=lambda path: "Chromium 1.0"),
        find_browser=lambda: "/x/chrome",
    )
    monkeypatch.setattr(pg, "_GUARD", guard)
    # Nothing runs: the wait returns at once and starts no measurement.
    assert stira_server._finish_page_guard() is True
    assert guard.state().code == "not_run" and not entered.is_set()
    assert guard.start() is True and entered.wait(5)
    assert guard.wait(0.05) is False  # bounded: still running after the wait
    release.set()
    assert stira_server._finish_page_guard() is True
    assert guard.state().code == "guard_verified"

    # A wait that breaks never fails the stop: the type name only.
    def boom():
        raise RuntimeError("secret")

    assert stira_server._finish_page_guard(boom) is False
    monkeypatch.setattr(pg, "_GUARD", None)
    assert stira_server._finish_page_guard() is False
    out = capsys.readouterr().out
    assert "RuntimeError" in out and "secret" not in out


# --- the row's values -------------------------------------------------------------------


def test_guard_row_age_uses_the_injected_clock(monkeypatch):
    clock = _Clock()
    guard = _scripted_guard(clock)
    monkeypatch.setattr(pg, "_GUARD", guard)
    assert guard.age_s() is None
    assert pc.guard_row() == {**pc.guard_report(), "measured_age_s": None}
    assert guard.measure().verified
    assert guard.age_s() == 0
    clock.now += 42.9
    row = pc.guard_row()
    assert set(row) == ROW_KEYS
    assert (row["guard"], row["guard_code"], row["measured_age_s"]) == (
        "verified",
        "guard_verified",
        42,
    )
    assert row["axis"] in pg.AXIS_CODES
    # A clock that went back, or one that breaks, is "not measured", never a negative age.
    clock.now -= 500
    assert guard.age_s() is None and pc.guard_row()["measured_age_s"] is None
    monkeypatch.setattr(pg, "_GUARD", None)
    assert pc.guard_row() == UNREADABLE | {"axis": pc.axis.current().code}


def _client(page_guard):
    report = {"version": 1, "ok": True, "roots": [], "checks": [], "notes": []}
    app = Flask(__name__)
    app.register_blueprint(
        create_diagnostics_blueprint(
            WriteFailureBoard(),
            doctor_report=lambda: report,
            run_doctor=lambda: report,
            page_guard=page_guard,
        )
    )
    return app.test_client(), report


def test_settings_row_returns_codes_and_has_no_writer(monkeypatch):
    clock = _Clock()
    guard = _scripted_guard(clock)
    monkeypatch.setattr(pg, "_GUARD", guard)
    client, report = _client(pc.guard_row)
    before = client.get("/api/diagnostics/doctor").get_json()["page_guard"]
    assert (before["guard"], before["guard_code"], before["measured_age_s"]) == (
        "unmeasured",
        "not_run",
        None,
    )
    guard.measure()
    clock.now += 7
    # Read at the request, not kept in the stored report.
    row = client.get("/api/diagnostics/doctor").get_json()["page_guard"]
    assert set(row) == ROW_KEYS and "page_guard" not in report
    assert (row["guard"], row["guard_code"], row["measured_age_s"]) == (
        "verified",
        "guard_verified",
        7,
    )
    # A request cannot set it: a posted value is ignored, and the POST measures nothing.
    forged = {"page_guard": {"guard": "verified", "guard_code": "guard_verified", "axis": "pass"}}
    monkeypatch.setattr(pg, "_GUARD", _scripted_guard(clock))
    posted = client.post("/api/diagnostics/doctor", json=forged).get_json()["page_guard"]
    assert (posted["guard"], posted["guard_code"]) == ("unmeasured", "not_run")
    for method in ("put", "patch", "delete"):
        assert getattr(client, method)("/api/diagnostics/doctor", json=forged).status_code == 405
    assert client.get("/api/diagnostics/page-guard").status_code == 404
    # Without a reader the report is what it was.
    plain, _ = _client(None)
    assert "page_guard" not in plain.get("/api/diagnostics/doctor").get_json()


@pytest.mark.parametrize(
    "row",
    [
        {"guard": "verified", "guard_code": "/etc/hosts", "axis": "pass"},
        {"guard": "verified", "guard_code": "guard_verified", "axis": "http://127.0.0.1:9/x"},
        {"guard": "Verified now", "guard_code": "guard_verified", "axis": "pass"},
        {"guard": "verified", "guard_code": "guard_verified"},
        {"guard": 1, "guard_code": None, "axis": []},
        None,
        "verified",
    ],
)
def test_settings_row_fails_closed_on_anything_but_codes(row):
    client, _ = _client(lambda: row)
    shown = client.get("/api/diagnostics/doctor").get_json()["page_guard"]
    assert shown == UNREADABLE
    assert "/etc/hosts" not in json.dumps(shown) and "127.0.0.1" not in json.dumps(shown)


def test_settings_row_shows_no_age_it_cannot_read():
    codes = {"guard": "verified", "guard_code": "guard_verified", "axis": "pass"}
    for age in (-1, True, 1.5, "12", None):
        client, _ = _client(lambda age=age: {**codes, "measured_age_s": age, "path": "/x/chrome"})
        assert client.get("/api/diagnostics/doctor").get_json()["page_guard"] == {
            **codes,
            "measured_age_s": None,
        }

    def broken():
        raise RuntimeError("secret")

    client, _ = _client(broken)
    assert client.get("/api/diagnostics/doctor").get_json()["page_guard"] == UNREADABLE


# --- the page ---------------------------------------------------------------------------


def test_settings_row_is_worded_in_both_catalogs_and_adds_no_field():
    script = (ROOT / "dashboard/static/js/settings_diagnostics.js").read_text(encoding="utf-8")
    keys = sorted(set(re.findall(r'"(diagnostics\.tab_page_guard[a-z_]*)"', script)))
    assert keys == [
        "diagnostics.tab_page_guard",
        "diagnostics.tab_page_guard_age",
        "diagnostics.tab_page_guard_no_age",
        "diagnostics.tab_page_guard_value",
    ]
    for lang in ("en", "ja"):
        catalog = json.loads(
            (ROOT / f"dashboard/static/i18n/catalog.{lang}.json").read_text(encoding="utf-8")
        )
        assert [key for key in keys if not catalog.get(key)] == [], lang
    # The script still reads one URL and writes nothing but the existing re-run.
    assert set(re.findall(r'"(/api/[^"]*)"', script)) == {"/api/diagnostics/doctor"}
    html = (ROOT / "dashboard/templates/settings.html").read_text(encoding="utf-8")
    section = html[html.index('id="sec-diagnostics"') :]
    section = section[: section.index("</section>")]
    assert "<input" not in section and "<select" not in section and "<textarea" not in section
    assert section.count("<button") == 1


def test_the_chat_server_serves_the_row_and_building_it_starts_no_measurement(
    stira_server, tmp_path, monkeypatch
):
    from stiracore.agent import nxp_agent
    from stiracore.observability.config_api import ConfigStore

    def never(browser, port):
        raise AssertionError("building the app starts no browser")

    guard = pg.Guard(platform="darwin", runner=never, find_browser=lambda: "/x/chrome")
    monkeypatch.setattr(pg, "_GUARD", guard)
    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
    client = stira_server.create_app(config_store=store).test_client()
    row = client.get("/api/diagnostics/doctor").get_json()["page_guard"]
    assert set(row) == ROW_KEYS
    assert (row["guard"], row["guard_code"], row["measured_age_s"]) == (
        "unmeasured",
        "not_run",
        None,
    )
    assert row["axis"] in pg.AXIS_CODES
    assert guard.state().code == "not_run"
