"""One server per data directory and a clean stop on SIGTERM (#285)."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

import pytest

from stiracore.config.data_dir_lock import (
    LOCK_FILENAME,
    DataDirLockError,
    DataDirOwnerLock,
    pid_alive,
    read_owner_pid,
)
from stiracore.observability.chat_api import HITLApprovalBroker

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "stira_owner_harness.py"
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and flock")

_HOLDER = """
import sys, time
sys.path[:0] = {paths!r}
from stiracore.config.data_dir_lock import DataDirOwnerLock
lock = DataDirOwnerLock(sys.argv[1])
lock.acquire()
print("held", flush=True)
time.sleep(60)
"""


def _hold_in_child(data_dir: Path) -> subprocess.Popen:
    code = _HOLDER.format(paths=[str(ROOT / "src"), str(ROOT)])
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(data_dir)], stdout=subprocess.PIPE, text=True
    )
    assert child.stdout.readline().strip() == "held"
    return child


# --- the lock ---------------------------------------------------------------------


def test_a_second_lock_is_refused_even_in_the_same_process(tmp_path):
    first = DataDirOwnerLock(tmp_path)
    first.acquire()
    if sys.platform != "win32":  # a Windows byte-range lock makes the note unreadable
        assert read_owner_pid(tmp_path / LOCK_FILENAME) == os.getpid()
    with pytest.raises(DataDirLockError) as refused:
        DataDirOwnerLock(tmp_path).acquire()
    assert refused.value.same_process and refused.value.owner_pid == os.getpid()
    assert "this process" in str(refused.value)
    first.release()
    again = DataDirOwnerLock(tmp_path)
    again.acquire()  # released: the next owner gets it
    again.release()


def test_a_lock_file_left_by_a_dead_owner_is_reclaimed(tmp_path):
    gone = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        text=True,
        capture_output=True,
        check=True,
    )
    dead_pid = int(gone.stdout)
    (tmp_path / LOCK_FILENAME).write_text(f"{dead_pid}\n", encoding="ascii")
    lock = DataDirOwnerLock(tmp_path)
    lock.acquire()  # the kernel lock is free; the stale note does not block
    if sys.platform != "win32":
        assert not pid_alive(dead_pid)
        assert read_owner_pid(tmp_path / LOCK_FILENAME) == os.getpid()
    lock.release()


@POSIX_ONLY
def test_another_process_is_named_and_a_killed_owner_releases(tmp_path):
    child = _hold_in_child(tmp_path)
    try:
        with pytest.raises(DataDirLockError) as refused:
            DataDirOwnerLock(tmp_path).acquire()
        assert (refused.value.owner_pid, refused.value.owner_alive) == (child.pid, True)
        assert f"pid {child.pid}" in str(refused.value)
    finally:
        child.kill()  # SIGKILL: no cleanup ran, the lock file stays behind
        child.wait(10)
    assert (tmp_path / LOCK_FILENAME).exists()
    lock = DataDirOwnerLock(tmp_path)
    lock.acquire()
    lock.release()


def test_other_os_errors_propagate(tmp_path, monkeypatch):
    import errno

    from stiracore import owner_lock  # the shared implementation (#322)

    def broken(_fd):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(owner_lock, "_try_lock", broken)
    with pytest.raises(OSError) as error:
        DataDirOwnerLock(tmp_path).acquire()
    assert not isinstance(error.value, DataDirLockError)


# --- create_app -------------------------------------------------------------------


@pytest.fixture
def stira(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.observability.config_api import ConfigStore

    data_dir = tmp_path / "data"
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    monkeypatch.delenv("STIRA_HEARTBEAT", raising=False)
    monkeypatch.delenv("STIRA_SCHEDULER", raising=False)

    def make(**kwargs):
        store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
        return stira_server.create_app(config_store=store, **kwargs)

    return make, stira_server, data_dir


def test_a_second_app_is_refused_before_it_opens_the_approval_store(stira, monkeypatch):
    make, stira_server, _data_dir = stira
    first = make()
    assert first.extensions["data_dir_lock"].held
    opened = Mock(side_effect=AssertionError("approval store opened"))
    monkeypatch.setattr(stira_server, "ApprovalStore", opened)
    monkeypatch.setattr(stira_server, "StatePersistence", opened)
    with pytest.raises(DataDirLockError):
        make()
    opened.assert_not_called()


def test_the_test_switch_only_tolerates_this_process(stira):
    make, _stira_server, data_dir = stira
    make()
    second = make(_tests_share_data_dir=True)
    assert second.extensions["data_dir_lock"] is None
    child_dir = data_dir.parent / "other"
    if sys.platform != "win32":
        child = _hold_in_child(child_dir)
        try:
            from dashboard import stira_server

            with pytest.raises(DataDirLockError):
                stira_server._acquire_data_dir_lock(child_dir, allow_same_process=True)
        finally:
            child.kill()
            child.wait(10)


# --- shutdown ---------------------------------------------------------------------


def test_a_closed_broker_never_stores_a_new_request(tmp_path):
    from stiracore.core.approval_store import ApprovalStore

    store = ApprovalStore(tmp_path / "a.db")
    broker = HITLApprovalBroker(store=store)
    broker.close()
    request = Mock(
        tool_name="file_write",
        tool_input={"path": "x"},
        args_hash=None,
        request_id=None,
        reason="ask",
        tool_use_id="u1",
        supersedes=None,
        agent_id="a",
        break_level=None,
        expires_at=None,
    )
    decision = broker.request(request)
    assert decision.action.value == "reject" and decision.reason == "approval closed"
    assert store.pending() == []


def test_close_waits_for_a_save_that_passed_the_closed_check(tmp_path):
    """Measured race (#285 review): close() used to return while a save was in flight.

    Barrier: the save is held after the closed check; close() runs meanwhile. Before
    the fix close() returned at once with 0 pending and the record appeared later.
    Now close() waits, so the store never changes after close() returned.
    """
    import threading

    from stiracore.core.approval_store import ApprovalStore

    store = ApprovalStore(tmp_path / "a.db")
    broker = HITLApprovalBroker(store=store)
    entered, proceed = threading.Event(), threading.Event()
    real_persist = broker._persist

    def held_persist(pending):
        entered.set()
        proceed.wait(5)
        return real_persist(pending)

    broker._persist = held_persist
    decision = {}
    worker = threading.Thread(target=lambda: decision.update(d=broker.request(_request())))
    worker.start()
    assert entered.wait(5)
    closer = threading.Thread(target=lambda: decision.update(closed=broker.close(timeout=5)))
    closer.start()
    closer.join(0.5)
    assert closer.is_alive(), "close() returned while a save was still running"
    proceed.set()
    closer.join(5)
    worker.join(5)
    assert decision["closed"] is True
    stored = store.pending()
    assert [r.tool_name for r in stored] == ["file_write"]  # accepted before close: restorable
    assert decision["d"].reason == "approval closed"
    assert broker.request(_request("u2")).reason == "approval closed"
    assert len(store.pending()) == 1  # nothing after close() returned


def test_close_is_bounded_when_a_save_hangs(tmp_path):
    import threading

    from stiracore.core.approval_store import ApprovalStore

    broker = HITLApprovalBroker(store=ApprovalStore(tmp_path / "a.db"))
    release = threading.Event()
    broker._persist = lambda pending: release.wait(10)
    worker = threading.Thread(target=lambda: broker.request(_request()), daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while not broker._saving and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    assert broker.close(timeout=0.3) is False
    assert time.monotonic() - started < 2
    release.set()
    worker.join(5)


def _request(tool_use_id="u1"):
    return Mock(
        tool_name="file_write",
        tool_input={"path": "x", "id": tool_use_id},
        args_hash=None,
        request_id=None,
        reason="ask",
        tool_use_id=tool_use_id,
        supersedes=None,
        agent_id="a",
        break_level=None,
        expires_at=None,
    )


def test_lifecycle_runs_steps_in_order_and_continues_after_failures():
    from stiracore.observability.lifecycle import ServerLifecycle

    calls = []
    life = ServerLifecycle(step_timeout=1.5)
    life.add("heartbeat", lambda t: calls.append(("hb", t)) or "stopped", order=30)
    life.add("approvals", lambda t: calls.append("close") or "closed", order=10)
    life.add("scheduler", Mock(side_effect=RuntimeError("boom")), order=20)
    life.add("store", lambda t: calls.append("store") or "closed", order=90, abort_only=True)
    assert life.shutdown() == {
        "approvals": "closed",
        "scheduler": "failed (RuntimeError)",
        "heartbeat": "stopped",
    }
    # abort-only steps do not run on a normal stop; a step gets its budget minus a margin
    assert calls == ["close", ("hb", pytest.approx(1.3))]


def test_lifecycle_bounds_a_hanging_step_and_the_whole_run():
    import threading

    from stiracore.observability.lifecycle import ServerLifecycle

    hang = threading.Event()
    life = ServerLifecycle(step_timeout=0.3, total_timeout=0.6)
    life.add("a", lambda t: hang.wait(30) and "stopped", order=1)
    life.add("b", lambda t: hang.wait(30) and "stopped", order=2)
    life.add("c", lambda t: "stopped", order=3)
    started = time.monotonic()
    result = life.shutdown()
    elapsed = time.monotonic() - started
    hang.set()
    assert result == {"a": "timed out", "b": "timed out", "c": "skipped (deadline)"}
    assert elapsed < 0.6 + 0.25, elapsed  # the joins never add up past the total


def test_a_step_whose_thread_cannot_start_is_reported_not_run_inline(monkeypatch):
    import threading

    from stiracore.observability import lifecycle as lifecycle_module
    from stiracore.observability.lifecycle import ServerLifecycle

    real_start = threading.Thread.start
    refused = []

    def start(self):
        if not refused:
            refused.append(self.name)
            raise RuntimeError("can't create new thread at interpreter shutdown")
        return real_start(self)

    monkeypatch.setattr(lifecycle_module.threading.Thread, "start", start)
    ran = []
    life = ServerLifecycle()
    life.add("approvals", lambda t: ran.append("approvals") or "closed", order=10)
    life.add("heartbeat", lambda t: ran.append("heartbeat") or "stopped", order=30)
    assert life.shutdown() == {"approvals": "not run (RuntimeError)", "heartbeat": "stopped"}
    assert ran == ["heartbeat"]  # never run inline (unbounded) on the caller's thread


def test_abort_after_a_normal_shutdown_keeps_the_lock():
    from stiracore.observability.lifecycle import ServerLifecycle

    lock, closed = Mock(), []
    life = ServerLifecycle()
    life.own_lock(lock)
    life.add("heartbeat", lambda _t: "stopped")
    life.add("store", lambda _t: closed.append(1) or "closed", abort_only=True)
    assert life.shutdown() == {"heartbeat": "stopped"}
    assert life.abort() == {"heartbeat": "stopped"}
    assert closed == [] and not lock.release.called  # the store was never closed


def test_concurrent_aborts_release_the_lock_once():
    import threading

    from stiracore.observability.lifecycle import ServerLifecycle

    lock = Mock()
    life = ServerLifecycle()
    life.own_lock(lock)
    gate = threading.Event()
    life.add("heartbeat", lambda _t: gate.wait(5) and "stopped")
    threads = [threading.Thread(target=life.abort) for _ in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(0.1)
    gate.set()
    for thread in threads:
        thread.join(5)
    life.abort()
    assert lock.release.call_count == 1


def test_add_after_shutdown_started_is_refused():
    from stiracore.observability.lifecycle import LifecycleClosedError, ServerLifecycle

    life = ServerLifecycle()
    life.shutdown()
    with pytest.raises(LifecycleClosedError, match="'late' not registered"):
        life.add("late", lambda _t: "stopped")
    assert life.shutdown() == {}


def test_lifecycle_runs_once_for_repeated_and_concurrent_calls():
    import threading

    from stiracore.observability.lifecycle import ServerLifecycle

    gate = threading.Event()
    runs = []

    def slow(_t):
        runs.append(1)
        gate.wait(5)
        return "stopped"

    life = ServerLifecycle()
    life.add("heartbeat", slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(life.shutdown())) for _ in range(3)]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    gate.set()
    for thread in threads:
        thread.join(5)
    assert runs == [1]
    assert results == [{"heartbeat": "stopped"}] * 3
    assert life.shutdown() == {"heartbeat": "stopped"}  # e.g. atexit after the entry point
    assert life.abort() == {"heartbeat": "stopped"}
    assert runs == [1]


def test_abort_releases_the_lock_only_when_every_step_finished(tmp_path):
    from stiracore.observability.lifecycle import ServerLifecycle

    clean, dirty = ServerLifecycle(), ServerLifecycle()
    for life, label in ((clean, "stopped"), (dirty, "stopping")):
        lock = Mock()
        life.own_lock(lock)
        life.add("heartbeat", lambda _t, label=label: label)
        life.add("store", lambda _t: "closed", abort_only=True)
        assert life.abort()["store"] == "closed"
        assert lock.release.called is (label == "stopped")


def test_a_failed_start_stops_what_started_and_releases_the_lock(stira, monkeypatch):
    import threading

    make, stira_server, data_dir = stira
    monkeypatch.setenv("STIRA_HEARTBEAT", "1")
    monkeypatch.setenv("STIRA_SCHEDULER", "1")
    seen = {}

    def interrupted(*_args, **_kwargs):
        # Runs after the heartbeat, scheduler and stores exist: a SIGTERM here.
        seen["threads"] = {t.name for t in threading.enumerate()}
        raise SystemExit(0)

    monkeypatch.setattr(stira_server, "create_scheduler_blueprint", interrupted)
    life = stira_server.ServerLifecycle()
    with pytest.raises(SystemExit):
        make(lifecycle=life)
    assert {"stira-heartbeat", "stira-scheduler"} <= seen["threads"]
    assert life.stopped
    result = life.shutdown()  # what abort() did, not run again
    assert result["approvals"] == "closed"
    assert (result["scheduler"], result["heartbeat"]) == ("stopped", "stopped")
    assert {result[k] for k in ("turns", "approval_store", "ledger")} == {"closed"}
    names = {t.name for t in threading.enumerate()}
    assert "stira-heartbeat" not in names and "stira-scheduler" not in names
    again = DataDirOwnerLock(data_dir)
    again.acquire()  # released by the failed start: another owner gets it
    again.release()


# --- the lock file itself (review: symlink / hard link / owner) ------------------------


@POSIX_ONLY
def test_a_symlinked_lock_file_is_refused_and_its_target_is_untouched(tmp_path):
    from stiracore.config.data_dir_lock import UnsafeLockFileError

    target = tmp_path / "victim.txt"
    target.write_text("keep me\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / LOCK_FILENAME).symlink_to(target)
    with pytest.raises(UnsafeLockFileError) as refused:
        DataDirOwnerLock(data).acquire()
    assert "symbolic link" in str(refused.value)
    assert target.read_text(encoding="utf-8") == "keep me\n"
    dangling = tmp_path / "d2"
    dangling.mkdir()
    (dangling / LOCK_FILENAME).symlink_to(tmp_path / "would-be-created")
    with pytest.raises(UnsafeLockFileError):
        DataDirOwnerLock(dangling).acquire()
    assert not (tmp_path / "would-be-created").exists()


@pytest.mark.skipif(not hasattr(os, "link"), reason="no hard links")
def test_a_hard_linked_lock_file_is_refused_and_untouched(tmp_path):
    from stiracore.config.data_dir_lock import UnsafeLockFileError

    target = tmp_path / "victim.txt"
    target.write_text("keep me\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    os.link(target, data / LOCK_FILENAME)
    with pytest.raises(UnsafeLockFileError) as refused:
        DataDirOwnerLock(data).acquire()
    assert "2 hard links" in str(refused.value)
    assert target.read_text(encoding="utf-8") == "keep me\n"


@POSIX_ONLY
def test_a_fifo_a_directory_and_another_owner_are_refused(tmp_path, monkeypatch):
    from stiracore.config.data_dir_lock import UnsafeLockFileError

    fifo = tmp_path / "fifo"
    fifo.mkdir()
    os.mkfifo(fifo / LOCK_FILENAME)
    with pytest.raises(UnsafeLockFileError, match="not a regular file"):
        DataDirOwnerLock(fifo).acquire()  # returns at once: no wait for a writer
    directory = tmp_path / "dir"
    (directory / LOCK_FILENAME).mkdir(parents=True)
    with pytest.raises(UnsafeLockFileError):
        DataDirOwnerLock(directory).acquire()
    other = tmp_path / "other"
    other.mkdir()
    (other / LOCK_FILENAME).write_text("123\n", encoding="ascii")
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(other / LOCK_FILENAME).st_uid + 1)
    with pytest.raises(UnsafeLockFileError, match="another user"):
        DataDirOwnerLock(other).acquire()
    assert (other / LOCK_FILENAME).read_text(encoding="ascii") == "123\n"


@POSIX_ONLY
def test_an_unsafe_lock_file_stops_the_server_with_exit_code_3(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    (data / LOCK_FILENAME).symlink_to(victim)
    log = tmp_path / "x.log"
    process = _spawn("serve", data, _free_port(), log)
    try:
        assert process.wait(60) == 3, log.read_text("utf-8")
    finally:
        _stop(process)
    assert "is unsafe (a symbolic link); not starting" in log.read_text("utf-8")
    assert victim.read_text(encoding="utf-8") == "keep me\n"


# --- the #285 report, with real processes (scripted model, no Bedrock) -----------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(phase: str, data_dir: Path, port: int, log: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
        "STIRA_PORT": str(port),
        "STIRA_HEARTBEAT": "1",
        "STIRA_SCHEDULER": "1",
        "PYTHONUNBUFFERED": "1",
    }
    env.pop("STIRA_PROMPT_CACHE", None)
    return subprocess.Popen(
        [sys.executable, str(HARNESS), phase, str(data_dir)],
        cwd=str(data_dir.parent),
        env=env,
        stdout=log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def _pending(port: int):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hitl/pending", timeout=2) as r:
            return json.load(r)["requests"]
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
        process.wait(10)


@POSIX_ONLY
def test_second_process_is_refused_and_sigterm_runs_the_bounded_stops(tmp_path):
    data_dir = tmp_path / "data"
    port_a, port_b, port_c = _free_port(), _free_port(), _free_port()
    a_log, b_log, c_log = tmp_path / "a.log", tmp_path / "b.log", tmp_path / "c.log"
    target = data_dir / "workspace" / "owner-check.txt"
    first = _spawn("pause", data_dir, port_a, a_log)
    restarted = None
    try:
        # 1. A: a turn waits on an Ask file_write approval.
        _wait_for(lambda: "HARNESS paused=1" in a_log.read_text("utf-8"), 60, "A to pause")
        live = _wait_for(lambda: _pending(port_a), 30, "A to serve")
        assert [(r["status"], r.get("restored", False)) for r in live] == [("pending", False)]

        # 2. B on the same data directory: refused before it serves, names A.
        second = _spawn("serve", data_dir, port_b, b_log)
        assert second.wait(60) == 3, b_log.read_text("utf-8")
        refusal = b_log.read_text("utf-8")
        assert f"another server (pid {first.pid}) owns the data directory" in refusal
        assert "Serving Flask app" not in refusal and _pending(port_b) is None
        # A's approval was never offered (or replayed) anywhere else.
        assert [(r["id"], r.get("restored", False)) for r in _pending(port_a)] == [
            (live[0]["id"], False)
        ]
        assert not target.exists()

        # 3. SIGTERM (what stira_ctl.sh stop sends): the bounded stops run, quickly,
        #    although a worker is still waiting on the approval.
        started = time.monotonic()
        first.send_signal(signal.SIGTERM)
        assert first.wait(15) == 0
        elapsed = time.monotonic() - started
        out = a_log.read_text("utf-8")
        # #452: the chat turn waiting on the approval is recorded as interrupted first.
        assert (
            "StiraCore: stopped (chat_turn: interrupted, approvals: closed, scheduler: stopped,"
            " heartbeat: stopped)" in out
        ), out
        assert elapsed < 10.0, elapsed  # inside stira_ctl.sh's SIGKILL window

        # 4. The pending approval is still restorable after the restart (#240 kept).
        restarted = _spawn("serve", data_dir, port_c, c_log)
        restored = _wait_for(lambda: _pending(port_c), 60, "the restarted server")
        assert [(r["id"], r["restored"], r["resumable"]) for r in restored] == [
            (live[0]["id"], True, True)
        ]
        assert not target.exists()
        with urllib.request.urlopen(f"http://127.0.0.1:{port_c}/api/turns", timeout=5) as r:
            (turn,) = json.load(r)["turns"]
        assert (turn["stop"], turn["error"]) == ("interrupted", True)  # not "cancelled"
        # Recorded by the SIGTERM stop (another thread than the turn's) on the turn that
        # raised the card, once; not left for the start-up sweep (#482 review M3).
        assert turn["turn_id"] == live[0]["turn_id"]
        url = f"http://127.0.0.1:{port_c}/api/turns/{turn['turn_id']}/events"
        with urllib.request.urlopen(url, timeout=5) as r:
            ends = [e for e in json.load(r)["events"] if e["type"] == "turn.completed"]
        assert [e["data"]["reason"] for e in ends] == ["server_stopped"]
        restarted.send_signal(signal.SIGTERM)
        assert restarted.wait(15) == 0
    finally:
        _stop(first)
        if restarted is not None:
            _stop(restarted)


@POSIX_ONLY
def test_sigterm_during_start_up_runs_the_stops_on_what_exists(tmp_path):
    """SIGTERM while create_app is still starting (heartbeat, scheduler, stores exist)."""
    data_dir = tmp_path / "data"
    log = tmp_path / "slow.log"
    port = _free_port()
    process = _spawn("slow-start", data_dir, port, log)
    try:
        _wait_for(lambda: "HARNESS starting (stalled)" in log.read_text("utf-8"), 60, "stall")
        assert (data_dir / "heartbeat.lock").exists() and (data_dir / "scheduler.lock").exists()
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        assert process.wait(15) == 0, log.read_text("utf-8")
        assert time.monotonic() - started < 10.0
    finally:
        _stop(process)
    out = log.read_text("utf-8")
    line = next(x for x in out.splitlines() if x.startswith("StiraCore: stopped ("))
    for part in (
        "approvals: closed",
        "scheduler: stopped",
        "heartbeat: stopped",
        "turns: closed",
        "approval_store: closed",
        "ledger: closed",
    ):
        assert part in line, line
    assert "Serving Flask app" not in out and _pending(port) is None
    # Everything was released: the next server starts on the same directory at once.
    again = DataDirOwnerLock(data_dir)
    again.acquire()
    again.release()
