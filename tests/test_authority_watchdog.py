"""W2 (#296, ADR 0011 §12): the model-free watchdog, the authority routes, the host
command ``stira authority unlatch`` and the server wiring.

No model is called; one app build without a listener (its watchdog is stopped).
"""

from __future__ import annotations

import ast
import errno
import json
import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from stiracore import authority
from stiracore.authority import AuthorityService, private_file
from stiracore.authority import cli as authority_cli
from stiracore.authority import watchdog as watchdog_mod
from stiracore.authority.latches import LatchStore
from stiracore.authority.lifecycle_events import LifecycleLog
from stiracore.authority.outbox import LifecycleRecorder
from stiracore.authority.watchdog import RECORD_FILE, Watchdog, heartbeat_stale

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def _no_process_service():
    yield
    authority.uninstall()


class Clock:
    def __init__(self, now=10_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make(tmp_path, *, heartbeat=None, clock=None, interval=15.0):
    service = AuthorityService(tmp_path)
    recorder = LifecycleRecorder(tmp_path, on_failure=service.report_unhealthy)
    service.recorder = recorder
    dog = Watchdog(
        service,
        recorder,
        data_dir=tmp_path,
        heartbeat_status=heartbeat,
        interval=interval,
        clock=clock or Clock(),
    )
    return service, recorder, dog


def outbox_codes(recorder):
    return [
        (i["event"]["subject_kind"], i["event"]["to"], i["event"]["reason_code"])
        for i in recorder.outbox()[::-1]
    ]


# --- the import graph (model-free) -----------------------------------------------------

FORBIDDEN = (
    "strands",
    "boto3",
    "botocore",
    "flask",
    "dashboard",
    "stiracore.providers",
    "stiracore.agent",
    "stiracore.core",
    "stiracore.scheduler",
    "stiracore.control",
    "stiracore.heartbeat",
    "stiracore.yolo",
    "stiracore.memory_autonomy",
    "stiracore.memory_rating",
)


def _module_path(src: Path, name: str) -> Path | None:
    base = src.joinpath(*name.split("."))
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    return None


def _imports(src: Path, name: str) -> set[str]:
    """Every import of ``name`` — module level **and** inside functions (a lazy import
    in a pass would load model code too)."""
    path = _module_path(src, name)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                module = ".".join(parts + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            found.add(module)
            found |= {
                f"{module}.{alias.name}"
                for alias in node.names
                if _module_path(src, f"{module}.{alias.name}")
            }
    return found


def _closure(src: Path, start: str) -> set[str]:
    seen: set[str] = set()
    todo = [start]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for module in _imports(src, name):
            parts = module.split(".")
            if parts[0] != "stiracore":
                seen.add(module)
                continue
            for end in range(2, len(parts) + 1):
                candidate = ".".join(parts[:end])
                if _module_path(src, candidate) and candidate not in seen:
                    todo.append(candidate)
    return seen


def _forbidden(modules: set[str]) -> set[str]:
    return {m for m in modules if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)}


def test_the_watchdog_import_graph_reaches_no_model_code():
    """M6 (fix round 1): the guarantee is that the watchdog (and the authority package
    it lives in) never imports or calls model code itself. Every parent ``__init__`` is
    walked except the root ``stiracore/__init__.py`` — the package facade re-exports the
    agent and is run by Python before any submodule; in the server it is loaded already.
    It is the one exclusion (asserted here), and documented."""
    modules = _closure(SRC, "stiracore.authority.watchdog")
    assert "stiracore.authority" in modules  # the parent package's __init__ is walked
    assert "stiracore" not in modules  # the root facade: the documented exclusion
    assert "stiracore.authority.lifecycle_events" in modules  # the walk works
    assert "stiracore.authority.service" in modules  # through the package
    assert _forbidden(modules) == set(), _forbidden(modules)


def test_the_import_graph_walk_catches_an_added_model_import(tmp_path):
    copy = tmp_path / "src"
    shutil.copytree(SRC / "stiracore", copy / "stiracore")
    target = copy / "stiracore" / "authority" / "private_file.py"
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\n\ndef _late():\n    from stiracore.agent import nxp_agent  # noqa\n",
        encoding="utf-8",
    )
    assert "stiracore.agent.nxp_agent" in _forbidden(_closure(copy, "stiracore.authority.watchdog"))


# --- heartbeat staleness ---------------------------------------------------------------


def test_heartbeat_staleness_rule():
    ok = {"enabled": True, "running": True, "interval_sec": 30.0, "last_tick_at": 1000.0}
    assert heartbeat_stale(ok, 1000.0 + 120.0, 0.0) is False
    assert heartbeat_stale(ok, 1000.0 + 121.0, 0.0) is True  # max(3 x 30, 120) = 120
    slow = {**ok, "interval_sec": 100.0}
    assert heartbeat_stale(slow, 1000.0 + 299.0, 0.0) is False  # 3 x 100 = 300
    assert heartbeat_stale(slow, 1000.0 + 301.0, 0.0) is True
    assert heartbeat_stale({**ok, "running": False}, 1000.0, 0.0) is True
    assert heartbeat_stale({**ok, "degraded": True}, 1000.0, 0.0) is True
    assert heartbeat_stale({"enabled": False, "running": False}, 1e9, 0.0) is False
    never = {**ok, "last_tick_at": None}
    assert heartbeat_stale(never, 500.0, 400.0) is False  # measured from the watchdog start
    assert heartbeat_stale(never, 600.0, 400.0) is True
    assert heartbeat_stale("garbage", 1.0, 0.0) is False


def test_a_stale_heartbeat_is_one_event_and_its_return_resolves_it(tmp_path):
    clock = Clock(10_000.0)
    status = {"enabled": True, "running": True, "interval_sec": 30.0, "last_tick_at": 10_000.0}
    service, recorder, dog = make(tmp_path, heartbeat=lambda: dict(status), clock=clock)
    dog.start(thread=False)
    assert outbox_codes(recorder) == []
    clock.now += 200.0  # no tick for 200 s > 120 s
    dog.run_pass()
    dog.run_pass()  # still stale: no second event
    assert outbox_codes(recorder) == [("heartbeat", "stale", "heartbeat_stale")]
    status["last_tick_at"] = clock.now
    dog.run_pass()
    assert outbox_codes(recorder) == [
        ("heartbeat", "stale", "heartbeat_stale"),
        ("heartbeat", "healthy", "heartbeat_resolved"),
    ]
    assert service.unhealthy_reason is None
    assert dog.status()["heartbeat_stale"] is False


def test_an_unreadable_heartbeat_status_is_stale(tmp_path):
    def broken():
        raise RuntimeError("boom")

    _service, recorder, dog = make(tmp_path, heartbeat=broken)
    dog.start(thread=False)
    assert outbox_codes(recorder) == [("heartbeat", "stale", "heartbeat_stale")]


# --- the last-pass record and the gap ---------------------------------------------------


def test_a_gap_since_the_last_pass_is_recorded_as_gateway_down(tmp_path):
    clock = Clock(10_000.0)
    _s, _r, dog = make(tmp_path, clock=clock)
    dog.start(thread=False)
    assert json.loads((tmp_path / RECORD_FILE).read_text())["last_pass_wall"] == 10_000.0
    clock.now = 10_000.0 + 25.0  # within 2 x 15 s: a quick restart, no gap
    _s2, recorder2, dog2 = make(tmp_path, clock=clock)
    dog2.start(thread=False)
    assert outbox_codes(recorder2) == []
    clock.now = 10_025.0 + 600.0  # down for ten minutes
    _s3, recorder3, dog3 = make(tmp_path, clock=clock)
    dog3.start(thread=False)
    (item,) = recorder3.outbox()
    assert (item["event"]["subject_kind"], item["event"]["reason_code"]) == (
        "gateway",
        "gateway_down",
    )
    assert item["event"]["gap_from_wall"] == 10_025.0 and item["event"]["ts_wall"] == clock.now


def test_an_unusable_watchdog_record_is_unhealthy_and_never_overwritten(tmp_path):
    path = tmp_path / RECORD_FILE
    path.write_text('{"v": 1, "last_pass_wall": 5, "hash": "x"}')
    os.chmod(path, 0o600)
    before = path.read_bytes()
    service, _r, dog = make(tmp_path)
    dog.start(thread=False)
    assert service.unhealthy_reason == "watchdog_record_failed"
    assert path.read_bytes() == before
    assert "unhealthy" in LatchStore(tmp_path).latched()  # persisted by the pass (a)


def test_a_failed_record_write_is_sticky(tmp_path, monkeypatch):
    clock = Clock(10_000.0)
    service, _r, dog = make(tmp_path, clock=clock)
    dog.start(thread=False)
    real = private_file.replace

    def failing(directory, name, body):
        if name == RECORD_FILE:
            raise OSError(errno.EIO, "io")
        return real(directory, name, body)

    monkeypatch.setattr(private_file, "replace", failing)
    clock.now += 15.0
    assert dog.run_pass() is False
    assert service.unhealthy_reason == "watchdog_record_failed"
    monkeypatch.setattr(private_file, "replace", real)
    before = (tmp_path / RECORD_FILE).read_bytes()
    clock.now += 15.0
    dog.run_pass()
    assert (tmp_path / RECORD_FILE).read_bytes() == before  # not written again
    assert dog.status()["record_healthy"] is False


def test_a_failing_pass_latches_unhealthy_and_the_next_pass_runs(tmp_path, monkeypatch):
    service, _r, dog = make(tmp_path)
    calls = {"n": 0}
    real = service.rescue_overdue

    def failing_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real()

    monkeypatch.setattr(service, "rescue_overdue", failing_once)
    assert dog.run_pass() is False
    assert service.unhealthy_reason == "watchdog_failed"
    dog.run_pass()  # goes on
    assert calls["n"] == 2
    assert "unhealthy" in LatchStore(tmp_path).latched()


def test_an_unhealthy_service_is_persisted_as_the_unhealthy_latch(tmp_path):
    service, recorder, dog = make(tmp_path)
    service.report_unhealthy("epoch_settle_failed")
    dog.run_pass()
    assert "unhealthy" in LatchStore(tmp_path).latched()
    assert ("latch", "set", "epoch_settle_failed") in outbox_codes(recorder)
    restarted = AuthorityService(tmp_path)
    assert restarted.unhealthy_reason == "latched_unhealthy"


def test_the_watchdog_repairs_the_outbox_for_a_latch_without_a_record(tmp_path):
    LatchStore(tmp_path).set("kill", incarnation="9" * 32, epoch=12)  # no lifecycle record
    service, recorder, dog = make(tmp_path)
    dog.start(thread=False)
    assert service.killed
    (item,) = recorder.outbox()
    assert item["synthesized"] and item["event"]["reason_code"] == "reason_not_recorded"


def test_the_watchdog_writes_queued_events(tmp_path):
    service, recorder, dog = make(tmp_path)
    service.breaker_transition("normal", "warning")
    assert service.flush(timeout=30)
    assert recorder.pending_count() == 1
    dog.run_pass()
    assert recorder.pending_count() == 0
    assert [e["reason_code"] for e in LifecycleLog(tmp_path).events()] == ["breaker_warning"]


def test_the_thread_runs_without_yolo_wakes_on_a_post_and_stops_cleanly(tmp_path):
    # The interval (600 s) is far beyond the wait (30 s): a write within the wait can only
    # come from the wake-up on the post, also on a loaded machine.
    service, recorder, dog = make(tmp_path, interval=600.0)
    dog.start()
    try:
        assert dog.status()["running"] is True
        thread = dog._thread  # this test's own thread (not any thread by its name)
        assert thread is not None and thread.is_alive()
        service.kill_switch()  # posts: the thread wakes and writes it (not after 600 s)
        deadline = time.monotonic() + 30.0
        while not (recorder.pending_count() == 0 and recorder.outbox()):
            assert time.monotonic() < deadline, "the watchdog did not wake on the post"
            time.sleep(0.01)
        assert recorder.outbox() and recorder.pending_count() == 0
    finally:
        assert dog.stop(30) == "stopped"
    assert dog.status()["running"] is False
    assert not thread.is_alive()


def test_stop_is_not_clean_when_events_cannot_be_written(tmp_path, monkeypatch):
    service, _recorder, dog = make(tmp_path)
    dog.start(thread=False)

    def failing(*_a, **_k):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(private_file, "append_line", failing)
    service.kill_switch()
    assert dog.stop(2) == "unhealthy"


def test_a_watchdog_that_cannot_start_latches_unhealthy(tmp_path, monkeypatch):
    service, _r, dog = make(tmp_path)
    real_start = threading.Thread.start

    def start(self):
        if self.name == "authority-watchdog":
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    dog.start()
    assert service.unhealthy_reason == "watchdog_unavailable"


# --- the routes -------------------------------------------------------------------------


@pytest.fixture
def app_client(tmp_path):
    from flask import Flask

    from stiracore.authority.routes import create_authority_blueprint
    from stiracore.observability.security import install_security

    service, recorder, dog = make(tmp_path)
    dog.start(thread=False)
    app = Flask(__name__)
    install_security(app)
    app.register_blueprint(
        create_authority_blueprint(lambda: service, lambda: recorder, lambda: dog)
    )
    client = app.test_client()
    token = app.config["STIRA_CSRF_TOKEN"]
    return SimpleNamespace(client=client, token=token, service=service, recorder=recorder)


def _post(ns, path, body=None, *, token=True, headers=None):
    head = {"X-CSRF-Token": ns.token} if token else {"X-Test-No-Csrf": "1"}
    head.update(headers or {})
    return ns.client.post(path, json=body or {}, headers=head)


def test_status_is_read_only_codes_and_numbers(app_client):
    data = app_client.client.get("/api/authority/status").get_json()
    assert data["healthy"] is True and data["killed"] is False
    assert data["latches"] == [] and data["outbox_unread"] == 0
    assert data["watchdog"]["passes"] == 1
    assert "incarnation" not in json.dumps(data)


def test_the_kill_route_needs_csrf_and_same_origin(app_client):
    assert _post(app_client, "/api/authority/kill", token=False).status_code == 403
    cross = _post(app_client, "/api/authority/kill", headers={"Origin": "http://evil.example"})
    assert cross.status_code == 403
    assert app_client.service.killed is False


def test_one_press_kills_persists_and_records(app_client, tmp_path):
    response = _post(app_client, "/api/authority/kill")
    assert response.status_code == 200
    data = response.get_json()
    assert data["killed"] is True and data["latch"] == "set" and data["recorded"] == "clean"
    assert data["settlement"] == "settled"
    assert "kill" in LatchStore(tmp_path).latched()
    status = app_client.client.get("/api/authority/status").get_json()
    assert status["latches"] == ["kill"] and status["outbox_unread"] == 1


def test_the_kill_route_works_while_unhealthy(app_client):
    app_client.service.report_unhealthy("epoch_store_write_failed")
    data = _post(app_client, "/api/authority/kill").get_json()
    assert data["killed"] is True and data["healthy"] is False


def test_outbox_read_and_ack(app_client):
    _post(app_client, "/api/authority/kill")
    items = app_client.client.get("/api/authority/outbox?unread=1").get_json()["items"]
    (item,) = items
    assert app_client.service.incarnation not in json.dumps(items)  # a short reference only
    assert len(item["event"]["instance"]) == 16
    event_id = item["event_ids"][0]
    assert (
        _post(
            app_client, "/api/authority/outbox/ack", {"event_id": event_id}, token=False
        ).status_code
        == 403
    )
    first = _post(app_client, "/api/authority/outbox/ack", {"event_id": event_id})
    again = _post(app_client, "/api/authority/outbox/ack", {"event_id": event_id})
    assert first.get_json() == {"acked": True, "already": False}
    assert again.get_json() == {"acked": True, "already": True}
    assert _post(app_client, "/api/authority/outbox/ack", {"event_id": "f" * 32}).status_code == 404
    assert app_client.client.get("/api/authority/outbox?unread=1").get_json()["items"] == []


def test_no_route_clears_a_latch(app_client):
    _post(app_client, "/api/authority/kill")
    for path in ("/api/authority/unlatch", "/api/authority/clear", "/api/authority/resume"):
        assert _post(app_client, path, {"latch": "kill"}).status_code in (404, 405)
    assert app_client.service.killed


# --- the host command -----------------------------------------------------------------


def _args(command, data_dir, latch=None):
    return SimpleNamespace(authority_command=command, data_dir=data_dir, latch=latch)


def _run(args, *, tty=True, typed="UNLATCH", capsys=None):
    return authority_cli.run(args, is_tty=lambda: tty, read_line=lambda _prompt: typed)


@pytest.fixture
def killed_dir(tmp_path):
    service = AuthorityService(tmp_path)
    service.kill_switch()
    return tmp_path


def test_unlatch_clears_the_kill_latch_and_records_it(killed_dir, capsys):
    assert _run(_args("unlatch", killed_dir, "kill")) == 0
    assert LatchStore(killed_dir).latched() == {}
    events = LifecycleLog(killed_dir).events()
    assert [(e["subject_id"], e["to"], e["reason_code"]) for e in events] == [
        ("kill", "cleared", "human_unlatch")
    ]
    assert AuthorityService(killed_dir).killed is False  # the next start runs normally
    out = capsys.readouterr().out
    assert "cleared kill" in out


def test_unlatch_needs_a_tty_and_the_word(killed_dir):
    assert _run(_args("unlatch", killed_dir, "kill"), tty=False) == 1
    assert _run(_args("unlatch", killed_dir, "kill"), typed="yes") == 1
    assert _run(_args("unlatch", killed_dir, "kill"), typed="unlatch") == 1
    assert "kill" in LatchStore(killed_dir).latched()


@pytest.mark.skipif(os.name != "posix", reason="POSIX data-directory lock")
def test_unlatch_refuses_while_a_server_runs(killed_dir):
    from stiracore.config.data_dir_lock import DataDirOwnerLock

    lock = DataDirOwnerLock(killed_dir)
    lock.acquire()
    try:
        assert _run(_args("unlatch", killed_dir, "kill")) == 2
    finally:
        lock.release()
    assert "kill" in LatchStore(killed_dir).latched()


def test_a_change_while_the_person_confirms_is_resume_stale(killed_dir, capsys):
    def typed(_prompt):
        # Another latch is set between the request and the confirmation.
        LatchStore(killed_dir).set("halted:breaker", incarnation="8" * 32, epoch=99)
        return "UNLATCH"

    code = authority_cli.run(
        _args("unlatch", killed_dir, "kill"), is_tty=lambda: True, read_line=typed
    )
    assert code == 1
    assert "resume_stale" in capsys.readouterr().err
    assert set(LatchStore(killed_dir).latched()) == {"kill", "halted:breaker"}


def test_unlatch_of_a_latch_that_is_not_set_or_unknown(tmp_path):
    assert _run(_args("unlatch", tmp_path, "kill")) == 1
    assert _run(_args("unlatch", tmp_path, "halted:anything")) == 2


def test_unlatch_refuses_an_unusable_store_and_leaves_it(tmp_path):
    path = tmp_path / "authority-latches"
    path.write_text("garbage")
    os.chmod(path, 0o600)
    assert _run(_args("unlatch", tmp_path, "kill")) == 1
    assert path.read_text() == "garbage"


def test_unlatch_all(tmp_path):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation="7" * 32, epoch=1)
    store.set("unhealthy", incarnation="7" * 32, epoch=2)
    assert _run(_args("unlatch", tmp_path, "all")) == 0
    assert LatchStore(tmp_path).latched() == {}


def test_status_reads_only(killed_dir, capsys):
    before = {p.name: p.read_bytes() for p in killed_dir.iterdir() if p.is_file()}
    assert _run(_args("status", killed_dir)) == 0
    assert "kill" in capsys.readouterr().out
    assert {p.name: p.read_bytes() for p in killed_dir.iterdir() if p.is_file()} == before


def test_the_stira_command_has_the_authority_subcommand(killed_dir, capsys):
    from stiracore.diagnostics.__main__ import main

    assert main(["authority", "status", "--data-dir", str(killed_dir)]) == 0
    assert "kill" in capsys.readouterr().out


# --- the server -----------------------------------------------------------------------


def test_the_server_wires_the_routes_the_watchdog_and_keeps_a_kill_across_a_restart(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.observability.config_api import ConfigStore

    data = tmp_path / "data"
    monkeypatch.setattr(stira_server, "DATA_DIR", data)
    monkeypatch.setenv("STIRA_YOLO_ALLOW", "off")
    model = Mock(side_effect=AssertionError("model reached"))
    for method in ("stream", "structured_output", "converse"):
        getattr(model, method).side_effect = AssertionError(f"model.{method} reached")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=model))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda **_kw: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    store.gateway_path.write_text("{}", encoding="utf-8")

    def build():
        return stira_server.create_app(config_store=store)

    app = build()
    try:
        client = app.test_client()
        token = app.config["STIRA_CSRF_TOKEN"]
        status = client.get("/api/authority/status").get_json()
        assert status["healthy"] is True and status["watchdog"]["running"] is True
        response = client.post("/api/authority/kill", json={}, headers={"X-CSRF-Token": token})
        assert response.get_json()["latch"] == "set"
        result = app.extensions["lifecycle"].shutdown()
        assert result["authority_watchdog"] == "stopped"
        assert result["authority_watchdog_passes"] == "stopped"
        line = stira_server._stopped_line(result)
        assert "authority_watchdog" not in line, line
    finally:
        for name in ("heartbeat", "scheduler"):
            if app.extensions.get(name) is not None:
                app.extensions[name].stop()
    assert "kill" in LatchStore(data).latched()
    app.extensions["data_dir_lock"].release()
    again = build()
    try:
        assert again.extensions["authority"].killed is True
        status = again.test_client().get("/api/authority/status").get_json()
        assert status["killed"] is True and status["latches"] == ["kill"]
        assert status["outbox_unread"] >= 1  # the kill notice is still unread
    finally:
        again.extensions["lifecycle"].shutdown()
        for name in ("heartbeat", "scheduler"):
            if again.extensions.get(name) is not None:
                again.extensions[name].stop()


def test_the_yolo_row_observer_keeps_the_rows_result_and_records_pause_and_end(tmp_path):
    from dashboard.stira_server import _observe_yolo_rows

    service = AuthorityService(tmp_path)
    recorder = LifecycleRecorder(tmp_path)
    service.recorder = recorder
    rows = []

    def record(event_type, **fields):
        rows.append(event_type)
        return "op:" + "a" * 32 + ":transition"

    observed = _observe_yolo_rows(record, service)
    wid = "6" * 32
    assert observed("audit.yolo.paused", window_id=wid, reason_code="breaker_warning") == (
        "op:" + "a" * 32 + ":transition"
    )
    observed("audit.yolo.ended", window_id=wid, end_reason="revoked_stop", end_class="normal")
    observed("audit.yolo.started", window_id=wid)

    def failing(event_type, **fields):
        raise RuntimeError("audit write failed")

    with pytest.raises(RuntimeError):  # YOLO's own failure, unchanged
        _observe_yolo_rows(failing, service)("audit.yolo.ended", window_id=wid)
    recorder.drain()
    assert rows == ["audit.yolo.paused", "audit.yolo.ended", "audit.yolo.started"]
    assert [(e["to"], e["reason_code"]) for e in LifecycleLog(tmp_path).events()] == [
        ("paused", "breaker_warning"),
        ("ended", "revoked_stop"),
    ]


# --- fix round 1 -------------------------------------------------------------------------


def test_a_stale_heartbeat_is_one_logged_event_per_transition(tmp_path):
    clock = Clock(10_000.0)
    status = {"enabled": True, "running": True, "interval_sec": 30.0, "last_tick_at": 10_000.0}
    _service, _recorder, dog = make(tmp_path, heartbeat=lambda: dict(status), clock=clock)
    dog.start(thread=False)
    clock.now += 200.0
    for _ in range(5):
        dog.run_pass()
    reasons = [e["reason_code"] for e in LifecycleLog(tmp_path).events()]
    assert reasons.count("heartbeat_stale") == 1  # in the log, not only the outbox


def test_start_twice_is_refused_and_stop_then_start_runs_again(tmp_path):
    _service, _recorder, dog = make(tmp_path, interval=30.0)
    dog.start()
    try:
        with pytest.raises(RuntimeError, match="watchdog_running"):
            dog.start()
    finally:
        assert dog.stop(30) == "stopped"
    dog.start()
    try:
        assert dog.status()["running"] is True
    finally:
        assert dog.stop(30) == "stopped"


def test_the_defaults_are_the_operator_decisions():
    from stiracore.authority import service as service_mod

    assert service_mod.CANCEL_DEADLINE_SEC == 5.0
    assert watchdog_defaults() == 15.0
    service = AuthorityService(None)
    assert service.cancel_deadline == service_mod.CANCEL_DEADLINE_SEC
    dog = Watchdog(service, LifecycleRecorder(None), data_dir=None)
    assert dog.interval == watchdog_defaults()


def watchdog_defaults():
    from stiracore.authority import watchdog

    return watchdog.INTERVAL_SEC


def test_a_clean_interpreter_imports_the_watchdog_without_model_code():
    """M6: a fresh interpreter (no fixture has imported anything) with only the root
    facade replaced by a bare package: importing the watchdog, the service, the routes'
    siblings and the recorder loads no model, agent, provider, strands or boto code."""
    import subprocess
    import sys

    code = f"""
import sys, types
root = types.ModuleType("stiracore")
root.__path__ = [{str(SRC / "stiracore")!r}]
sys.modules["stiracore"] = root
import stiracore.authority.watchdog, stiracore.authority.outbox, stiracore.authority.service
import stiracore.authority.latches, stiracore.authority.lifecycle_events
bad = sorted(m for m in sys.modules if any(m == f or m.startswith(f + ".") for f in {FORBIDDEN!r}))
print("FORBIDDEN", bad)
"""
    out = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(SRC)},
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "FORBIDDEN []" in out.stdout, out.stdout


def test_unlatch_is_refused_when_the_lifecycle_log_cannot_record_it(killed_dir, capsys):
    from stiracore.authority.lifecycle_events import LOG_FILE as LOG

    path = killed_dir / LOG
    path.write_bytes(b"torn")  # no newline: unusable
    os.chmod(path, 0o600)
    assert _run(_args("unlatch", killed_dir, "kill")) == 1
    assert "kill" in LatchStore(killed_dir).latched()  # nothing cleared without its record


def test_the_running_watchdogs_are_registered_and_stopped_by_stop_all(tmp_path):
    """The suite's teardown stops every running watchdog (an app build starts one)."""
    _service, _recorder, dog = make(tmp_path, interval=600.0)
    dog.start()
    assert dog in watchdog_mod.live()
    assert watchdog_mod.stop_all(30) >= 1
    assert dog not in watchdog_mod.live() and not dog._thread.is_alive()
