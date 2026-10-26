"""#322: ``heartbeat.lock`` and ``scheduler.lock`` use ``server.lock``'s no-follow lock.

Before, both were opened with ``os.open(path, O_RDWR | O_CREAT)``: a symlink named
``heartbeat.lock`` in the data directory was followed and its target truncated and
overwritten with the PID note. Now all three owner locks share
:class:`stiracore.owner_lock.OwnerLockFile`: no symlink is followed, the descriptor
must be a regular, singly linked file of this user, and nothing is written to a refused
file (the note is only truncated once the kernel lock is held).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from stiracore import owner_lock
from stiracore.config.data_dir_lock import DataDirLockError, DataDirOwnerLock
from stiracore.heartbeat.daemon import HeartbeatLockError, HeartbeatOwnerLock
from stiracore.owner_lock import OwnerLockFile, UnsafeLockFileError
from stiracore.scheduler import scheduler as scheduler_module
from stiracore.scheduler.scheduler import Scheduler, SchedulerLockError
from tests.test_heartbeat_daemon import stira  # noqa: F401 - the chat server app fixture

ROOT = Path(__file__).resolve().parents[1]
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks/FIFOs/owners")
KEEP = "keep me\n"

# name -> (file name, factory(directory), contention error)
LOCKS = {
    "server.lock": ("server.lock", DataDirOwnerLock, DataDirLockError),
    "heartbeat.lock": ("heartbeat.lock", HeartbeatOwnerLock, HeartbeatLockError),
    "scheduler.lock": (
        "scheduler.lock",
        lambda d: HeartbeatOwnerLock(d, scheduler_module.LOCK_FILENAME),
        HeartbeatLockError,
    ),
}
each_lock = pytest.mark.parametrize("name", list(LOCKS))


def _victim(tmp_path: Path) -> Path:
    target = tmp_path / "victim.txt"
    target.write_text(KEEP, encoding="utf-8")
    return target


def _data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    return data


# --- one implementation --------------------------------------------------------------


@each_lock
def test_the_three_locks_share_one_implementation(tmp_path, name):
    filename, make, _contended = LOCKS[name]
    lock = make(tmp_path)
    assert isinstance(lock, OwnerLockFile)
    assert lock.path == tmp_path / filename
    # Opening, checking, locking and the note are not overridden anywhere.
    for method in ("acquire", "release"):
        assert getattr(type(lock), method) is getattr(OwnerLockFile, method)


def test_the_scheduler_takes_scheduler_lock_with_the_shared_class(tmp_path):
    scheduler = Scheduler(tmp_path)
    assert isinstance(scheduler._owner, OwnerLockFile)
    assert scheduler._owner.path == tmp_path / "scheduler.lock"


# --- the lock file itself --------------------------------------------------------------


@POSIX_ONLY
@each_lock
def test_a_symlink_is_refused_and_its_target_is_untouched(tmp_path, name):
    filename, make, _contended = LOCKS[name]
    target, data = _victim(tmp_path), _data(tmp_path)
    (data / filename).symlink_to(target)
    with pytest.raises(UnsafeLockFileError) as refused:
        make(data).acquire()
    assert refused.value.reason == "a symbolic link"
    assert target.read_text(encoding="utf-8") == KEEP
    assert (data / filename).is_symlink()


@POSIX_ONLY
@each_lock
def test_a_dangling_symlink_is_refused_and_nothing_is_created(tmp_path, name):
    filename, make, _contended = LOCKS[name]
    data = _data(tmp_path)
    (data / filename).symlink_to(tmp_path / "would-be-created")
    with pytest.raises(UnsafeLockFileError):
        make(data).acquire()
    assert not (tmp_path / "would-be-created").exists()


@pytest.mark.skipif(not hasattr(os, "link"), reason="no hard links")
@each_lock
def test_a_hard_link_is_refused_and_untouched(tmp_path, name):
    filename, make, _contended = LOCKS[name]
    target, data = _victim(tmp_path), _data(tmp_path)
    os.link(target, data / filename)
    with pytest.raises(UnsafeLockFileError) as refused:
        make(data).acquire()
    assert refused.value.reason == "2 hard links"
    assert target.read_text(encoding="utf-8") == KEEP


_FIFO_CHILD = """
import sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from tests.test_owner_lock_files import LOCKS
from stiracore.owner_lock import UnsafeLockFileError
try:
    LOCKS[sys.argv[3]][1](sys.argv[4]).acquire()
except UnsafeLockFileError as error:
    print(error.reason, flush=True)
else:
    print("acquired", flush=True)
"""


@POSIX_ONLY
@each_lock
def test_a_fifo_is_refused_without_waiting_for_a_writer(tmp_path, name):
    filename, _make, _contended = LOCKS[name]
    data = _data(tmp_path)
    os.mkfifo(data / filename)
    # In a child with a timeout: a blocking open would hang here, not the test run.
    child = subprocess.run(
        [sys.executable, "-c", _FIFO_CHILD, str(ROOT / "src"), str(ROOT), name, str(data)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert child.stdout.strip() == "not a regular file", child.stderr


@each_lock
def test_a_directory_is_refused(tmp_path, name):
    filename, make, _contended = LOCKS[name]
    data = _data(tmp_path)
    (data / filename).mkdir()
    with pytest.raises(UnsafeLockFileError) as refused:
        make(data).acquire()
    assert refused.value.reason in ("a directory", "not a regular file")


@POSIX_ONLY
@each_lock
def test_a_file_of_another_user_is_refused_and_untouched(tmp_path, name, monkeypatch):
    filename, make, _contended = LOCKS[name]
    data = _data(tmp_path)
    (data / filename).write_text("123\n", encoding="ascii")
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(data / filename).st_uid + 1)
    with pytest.raises(UnsafeLockFileError, match="another user"):
        make(data).acquire()
    assert (data / filename).read_text(encoding="ascii") == "123\n"


@each_lock
def test_an_unsafe_refusal_keeps_the_old_catch_clauses_working(tmp_path, name):
    filename, make, contended = LOCKS[name]
    data = _data(tmp_path)
    (data / filename).mkdir()
    with pytest.raises(UnsafeLockFileError) as refused:
        make(data).acquire()
    # Compatibility: a caller that catches the old contention error still stops there
    # (server.lock: DataDirLockError, exit code 3, #285; heartbeat/scheduler:
    # HeartbeatLockError); the shared UnsafeLockFileError tells the two apart.
    assert isinstance(refused.value, contended)


# --- the note ---------------------------------------------------------------------------


@each_lock
def test_the_note_is_replaced_only_once_the_lock_is_held(tmp_path, name):
    filename, make, contended = LOCKS[name]
    data = _data(tmp_path)
    (data / filename).write_text("999999\nleft by a crashed owner\n", encoding="ascii")
    first = make(data)
    first.acquire()
    try:
        posix = sys.platform != "win32"  # a Windows byte-range lock hides the note
        if posix:
            assert (data / filename).read_text(encoding="ascii") == f"{os.getpid()}\n"
            (data / filename).write_text("note of the owner\n", encoding="ascii")
        with pytest.raises(contended):
            make(data).acquire()  # contended: the owner's note is not truncated
        if posix:
            assert (data / filename).read_text(encoding="ascii") == "note of the owner\n"
    finally:
        first.release()
    again = make(data)
    again.acquire()  # released: the next owner gets it
    again.release()


def test_other_os_errors_propagate_for_every_lock(tmp_path, monkeypatch):
    import errno

    def broken(_fd):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(owner_lock, "_try_lock", broken)
    for name, (_filename, make, contended) in LOCKS.items():
        with pytest.raises(OSError) as error:
            make(tmp_path / name).acquire()
        assert not isinstance(error.value, (contended, UnsafeLockFileError)), name


# --- the owners ---------------------------------------------------------------------------


@POSIX_ONLY
def test_a_symlinked_heartbeat_lock_keeps_the_daemon_stopped(tmp_path):
    from tests.test_heartbeat_daemon import _daemon

    daemon, _clock, _state, _engine = _daemon(tmp_path)
    target = _victim(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "heartbeat.lock").symlink_to(target)
    with pytest.raises(UnsafeLockFileError):
        daemon.start(background=False)
    assert daemon.status()["state"] == "stopped" and not daemon.running
    assert target.read_text(encoding="utf-8") == KEEP


@POSIX_ONLY
def test_a_symlinked_scheduler_lock_keeps_the_scheduler_stopped(tmp_path):
    target = _victim(tmp_path)
    (tmp_path / "scheduler.lock").symlink_to(target)
    scheduler = Scheduler(tmp_path)
    with pytest.raises(UnsafeLockFileError) as refused:
        scheduler.start(background=False)
    assert isinstance(refused.value, SchedulerLockError)  # old callers' catch still holds
    assert refused.value.reason == "a symbolic link"
    assert scheduler.state.value == "stopped" and not scheduler.running
    assert target.read_text(encoding="utf-8") == KEEP
    assert not (tmp_path / scheduler_module.LOG_FILENAME).exists()


@POSIX_ONLY
def test_stira_serves_without_a_heartbeat_when_heartbeat_lock_is_a_symlink(
    stira,  # noqa: F811 - the imported fixture
    tmp_path,
    monkeypatch,
    capsys,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = _victim(tmp_path)
    (data_dir / "heartbeat.lock").symlink_to(target)
    (data_dir / "scheduler.lock").symlink_to(target)
    monkeypatch.setenv("STIRA_SCHEDULER", "1")
    app, _ = stira(env="1")
    assert app.extensions["heartbeat"] is None
    assert not app.extensions["scheduler"].running
    status = app.test_client().get("/api/gateway/status").get_json()
    assert status["heartbeat"] == {
        "enabled": True,
        "running": False,
        "last_error": "UnsafeLockFileError",
    }
    assert app.extensions["data_dir_lock"].held  # the server itself started
    out = capsys.readouterr().out
    assert "heartbeat.lock is unsafe (a symbolic link); not started" in out
    assert "scheduler.lock is unsafe (a symbolic link); not started" in out
    assert target.read_text(encoding="utf-8") == KEEP


# --- hooks that raise (review round 1) ---------------------------------------------------


class _RaisingHooks(OwnerLockFile):
    def __init__(self, path, *, on_acquire=False, on_release=False):
        super().__init__(path)
        self.on_acquire, self.on_release = on_acquire, on_release

    def _acquired(self):
        if self.on_acquire:
            raise RuntimeError("hook failed on acquire")

    def _released(self):
        if self.on_release:
            raise RuntimeError("hook failed on release")


def _opened_fds(monkeypatch) -> list[int]:
    opened: list[int] = []
    real_open = os.open

    def recording(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(owner_lock.os, "open", recording)
    return opened


def _closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


def test_a_raising_acquire_hook_leaks_no_descriptor_and_no_lock(tmp_path, monkeypatch):
    opened = _opened_fds(monkeypatch)
    lock = _RaisingHooks(tmp_path / "x.lock", on_acquire=True)
    with pytest.raises(RuntimeError, match="on acquire"):
        lock.acquire()
    assert not lock.held and len(opened) == 1 and _closed(opened[0])
    again = OwnerLockFile(tmp_path / "x.lock")
    again.acquire()  # the kernel lock was dropped
    again.release()


def test_a_raising_release_hook_still_closes_the_descriptor(tmp_path, monkeypatch):
    opened = _opened_fds(monkeypatch)
    lock = _RaisingHooks(tmp_path / "x.lock", on_release=True)
    lock.acquire()
    with pytest.raises(RuntimeError, match="on release"):
        lock.release()
    assert not lock.held and _closed(opened[0])
    lock.release()  # a second release is a no-op, not a second failure
    again = OwnerLockFile(tmp_path / "x.lock")
    again.acquire()
    again.release()
