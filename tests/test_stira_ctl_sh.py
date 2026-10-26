"""Smoke tests for scripts/stira_ctl.sh (#160)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "stira_ctl.sh"

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="bash script is POSIX only"),
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available"),
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(*args: str, port: int | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if port is not None:
        env["STIRA_PORT"] = str(port)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        cwd=ROOT,
        check=False,
    )


def test_syntax_ok() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr


def test_unknown_subcommand_prints_usage() -> None:
    result = _run("bogus", port=_free_port())
    assert result.returncode != 0
    assert "usage" in (result.stdout + result.stderr).lower()


@pytest.mark.parametrize("port", ["5555;id", "$(id)", "-1", "0", "70000", "abc"])
@pytest.mark.parametrize("action", ["status", "start"])
def test_a_port_that_is_not_a_number_is_refused(action: str, port: str) -> None:
    env = dict(os.environ, STIRA_PORT=port)
    result = subprocess.run(
        ["bash", str(SCRIPT), action],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 2
    assert "STIRA_PORT must be a port number" in result.stderr


def test_status_when_down_does_not_fail() -> None:
    result = _run("status", port=_free_port())
    assert result.returncode == 0, result.stderr
    assert "stira:" in result.stdout


# --- data-directory owner (#285): start/status/stop agree with the server's refusal ---

_STUB_SERVER = """
import signal, sys, time
from pathlib import Path
sys.path[:0] = {paths!r}
from stiracore.config.data_dir_lock import DataDirLockError, DataDirOwnerLock
lock = DataDirOwnerLock(Path(__file__).resolve().parent.parent / ".local" / "stira")
try:
    lock.acquire()
except DataDirLockError as error:
    print(f"Stira: {{error}}", file=sys.stderr)
    raise SystemExit(3)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
time.sleep(120)
"""


@pytest.fixture
def ctl_root(tmp_path):
    """A copy of the script in its own ROOT with a stub server that only takes the lock."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "dashboard").mkdir()
    shutil.copy(SCRIPT, root / "scripts" / "stira_ctl.sh")
    (root / "dashboard" / "stira_server.py").write_text(
        _STUB_SERVER.format(paths=[str(ROOT / "src"), str(ROOT)]), encoding="utf-8"
    )
    started: list[int] = []

    def run(*args, port, path=None):
        env = {**os.environ, "STIRA_PORT": str(port), "STIRA_PYTHON": sys.executable}
        if path is not None:
            env["PATH"] = path
        result = subprocess.run(
            [shutil.which("bash"), str(root / "scripts" / "stira_ctl.sh"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            cwd=root,
            check=False,
        )
        pid_file = root / ".local" / "stira" / "stira_server.pid"
        if pid_file.exists():
            started.append(int(pid_file.read_text().strip()))
        return result

    yield root, run, started
    for pid in started:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def test_start_status_stop_follow_the_data_directory_owner(ctl_root):
    root, run, _started = ctl_root
    state = root / ".local" / "stira"
    first = run("start", port=_free_port())
    assert first.returncode == 0, first.stderr
    pid = int((state / "stira_server.pid").read_text().strip())
    assert (state / "server.lock").read_text().strip() == str(pid)

    # The PID file is gone (started by hand, or overwritten): the owner still counts.
    (state / "stira_server.pid").unlink()
    second = run("start", port=_free_port())
    assert second.returncode == 1
    assert f"pid {pid} already owns the data directory" in second.stderr
    assert not (state / "stira_server.pid").exists()

    status = run("status", port=_free_port())
    assert status.returncode == 0
    assert f"but pid {pid} owns the data directory" in status.stdout

    stop = run("stop", port=_free_port())
    assert stop.returncode == 0, stop.stderr
    assert f"STOPPED (killed: {pid})" in stop.stdout
    assert "DOWN" in run("status", port=_free_port()).stdout


def test_a_start_the_server_refuses_reports_why_and_keeps_no_pid_file(ctl_root):
    root, run, _started = ctl_root
    state = root / ".local" / "stira"
    holder_code = (
        f"import sys, time; sys.path[:0] = {[str(ROOT / 'src')]!r}\n"
        "from stiracore.config.data_dir_lock import DataDirOwnerLock\n"
        f"DataDirOwnerLock({str(state)!r}).acquire(); print('held', flush=True); time.sleep(60)\n"
    )
    # Not a chat server process of this checkout: the script's pre-check lets it through,
    # the server's own lock refuses.
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        result = run("start", port=_free_port())
        assert result.returncode == 1
        assert "start failed (exit 3)" in result.stderr
        assert f"another server (pid {holder.pid}) owns the data directory" in result.stderr
        assert not (state / "stira_server.pid").exists()
    finally:
        holder.kill()
        holder.wait(10)


_DECOY = "import time; time.sleep(60)"


def _decoy(root):
    """Another process whose command line looks like this checkout's chat server."""
    return subprocess.Popen(
        [sys.executable, "-c", _DECOY, str(root / "dashboard" / "stira_server.py")]
    )


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not available")
def test_a_stale_note_naming_a_look_alike_process_is_not_stopped(ctl_root):
    # The PID in server.lock was reused by a process with the same command line, but it
    # does not hold the lock file: neither status, start nor stop treat it as the owner.
    root, run, _started = ctl_root
    state = root / ".local" / "stira"
    state.mkdir(parents=True)
    decoy = _decoy(root)
    try:
        (state / "server.lock").write_text(f"{decoy.pid}\n", encoding="ascii")
        stop = run("stop", port=_free_port())
        assert stop.returncode == 0 and "already down" in stop.stdout, stop
        assert decoy.poll() is None, "the look-alike process was killed"
        assert "owns the data directory" not in run("status", port=_free_port()).stdout
    finally:
        decoy.kill()
        decoy.wait(10)


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not available")
def test_a_stale_pid_file_naming_a_look_alike_process_is_refused(ctl_root):
    root, run, _started = ctl_root
    state = root / ".local" / "stira"
    first = run("start", port=_free_port())  # a real owner (stub) holds server.lock
    assert first.returncode == 0, first.stderr
    owner = int((state / "stira_server.pid").read_text().strip())
    decoy = _decoy(root)
    try:
        (state / "stira_server.pid").write_text(f"{decoy.pid}\n", encoding="ascii")
        stop = run("stop", port=_free_port())
        assert stop.returncode == 1
        assert f"skipping pid {decoy.pid}: it does not hold" in stop.stderr
        assert decoy.poll() is None, "the look-alike process was killed"
        os.kill(owner, 0)  # the real owner is untouched too
    finally:
        decoy.kill()
        decoy.wait(10)


# Commands stira_ctl.sh runs, without lsof (round 2: "cannot tell" refuses the stop).
_TOOLS = (
    "ps",
    "tr",
    "head",
    "grep",
    "seq",
    "sleep",
    "rm",
    "mkdir",
    "dirname",
    "basename",
    "cat",
    "curl",
    "nohup",
    "tail",
    "env",
    "bash",
)


def _path_without_lsof(tmp_path):
    bin_dir = tmp_path / "bin-no-lsof"
    bin_dir.mkdir()
    for tool in _TOOLS:
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    assert shutil.which("lsof", path=str(bin_dir)) is None
    return str(bin_dir)


def _detached_decoy(root):
    """A look-alike re-parented away from pytest (so a killed one is reaped, not a zombie)."""
    out = subprocess.run(
        [
            shutil.which("bash"),
            "-c",
            f'"$0" -c {_DECOY!r} "$1" >/dev/null 2>&1 & echo $!',
            sys.executable,
            str(root / "dashboard" / "stira_server.py"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill(pid):
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def test_stop_refuses_when_ownership_cannot_be_verified(ctl_root, tmp_path):
    root, run, _started = ctl_root
    state = root / ".local" / "stira"
    state.mkdir(parents=True)
    decoy = _detached_decoy(root)
    try:
        (state / "stira_server.pid").write_text(f"{decoy}\n", encoding="ascii")
        (state / "server.lock").write_text(f"{decoy}\n", encoding="ascii")
        no_lsof = _path_without_lsof(tmp_path)
        stop = run("stop", port=_free_port(), path=no_lsof)
        assert stop.returncode == 1, stop
        assert "cannot verify it owns the data directory" in stop.stderr
        assert "(lsof not found)" in stop.stderr
        assert "stop --force-unverified" in stop.stderr
        assert _alive(decoy), "a process that could not be verified was signalled"

        (state / "server.lock").unlink()  # no lock file (e.g. a server older than #285)
        stop = run("stop", port=_free_port())
        assert stop.returncode == 1 and "does not exist" in stop.stderr, stop
        (state / "server.lock").symlink_to(tmp_path / "elsewhere")
        stop = run("stop", port=_free_port())
        assert stop.returncode == 1 and "is a symbolic link" in stop.stderr, stop
        assert _alive(decoy)
        restart = run("restart", port=_free_port(), path=no_lsof)
        assert restart.returncode != 0 and _alive(decoy)  # restart never forces

        assert run("stop", "--bogus", port=_free_port()).returncode == 2
        # The explicit, separate operation keeps the pre-#285 behaviour.
        forced = run("stop", "--force-unverified", port=_free_port(), path=no_lsof)
        assert forced.returncode == 0, forced
        assert "--force-unverified: stopping pid" in forced.stderr
        assert f"STOPPED (killed: {decoy})" in forced.stdout
        assert not _alive(decoy)
    finally:
        _kill(decoy)
