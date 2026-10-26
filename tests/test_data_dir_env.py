"""#272: ``STIRA_DATA_DIR`` moves the server's data directory, and only when it is set.

- Unset (or empty): the data directory stays ``<repository>/.local/stira`` and the
  configuration files stay where they were read before (nothing moves, no migration).
- Set: an absolute path, symlinks resolved; the server's data *and* its configuration
  files are in it, whatever the working directory; the floor follows it (#243/#271).
- Refused before anything is created: a relative path, a filesystem root, the home
  directory (or an ancestor), an existing non-directory. The message names the
  variable, never its value.
- ``stira_ctl.sh`` and ``stira doctor`` look at the same directory.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from stiracore.config.data_dir import DataDirSettingError, explicit_data_dir

ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = os.pathsep.join([str(ROOT / "src"), str(ROOT)])


# --- the setting itself ------------------------------------------------------------


@pytest.mark.parametrize("environ", [{}, {"STIRA_DATA_DIR": ""}, {"STIRA_DATA_DIR": "  "}])
def test_unset_or_empty_keeps_the_default(environ):
    assert explicit_data_dir(environ) is None


def test_an_absolute_directory_is_resolved_through_symlinks(tmp_path):
    real = tmp_path / "real-data"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert explicit_data_dir({"STIRA_DATA_DIR": str(link)}) == real.resolve()
    # A directory that does not exist yet is fine (the server creates it).
    missing = tmp_path / "new" / "data"
    assert explicit_data_dir({"STIRA_DATA_DIR": str(missing)}) == missing.resolve()
    assert not missing.exists()


@pytest.mark.parametrize(
    "value, phrase",
    [
        ("relative/data", "must be an absolute path"),
        ("~/data", "must be an absolute path"),
        ("/", "filesystem root, the home directory"),
        (str(Path.home()), "filesystem root, the home directory"),
        (str(Path.home().parent), "filesystem root, the home directory"),
    ],
)
def test_unusable_values_are_refused_without_the_value(value, phrase):
    with pytest.raises(DataDirSettingError) as caught:
        explicit_data_dir({"STIRA_DATA_DIR": value})
    assert phrase in str(caught.value)
    assert value not in str(caught.value)


def test_a_file_is_not_a_data_directory(tmp_path):
    target = tmp_path / "secret-name.json"
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(DataDirSettingError, match="not a directory") as caught:
        explicit_data_dir({"STIRA_DATA_DIR": str(target)})
    assert "secret-name" not in str(caught.value)


# --- stira_server reads it at start -------------------------------------------------

_PROBE = (
    "import json\n"
    "from dashboard import stira_server as p\n"
    "print(json.dumps({'data_dir': str(p.DATA_DIR), 'from_env': p.DATA_DIR_FROM_ENV,"
    " 'model_id': p.GATEWAY_CONFIG.model_id,"
    " 'gateway_path': str(p.GATEWAY_CONFIG.path)}))\n"
)


def _clean_env(**extra):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("STIRA_") and key != "PYTHONPATH"
    }
    env["PYTHONPATH"] = PYTHONPATH
    env.update(extra)
    return env


def _probe(cwd, **extra):
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=_clean_env(**extra),
        cwd=cwd,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_server_uses_the_explicit_directory_and_its_gateway_config(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "gateway-config.json").write_text(
        json.dumps({"model_id": "from-the-data-dir"}), encoding="utf-8"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    seen = _probe(elsewhere, STIRA_DATA_DIR=str(data))
    assert seen["from_env"] is True
    assert Path(seen["data_dir"]) == data.resolve()
    assert seen["model_id"] == "from-the-data-dir"
    assert Path(seen["gateway_path"]).parent == data.resolve()
    # Importing reads only: nothing is created in the working directory.
    assert list(elsewhere.iterdir()) == []


def test_without_the_variable_the_default_location_is_unchanged(tmp_path):
    seen = _probe(tmp_path)
    assert seen["from_env"] is False
    assert Path(seen["data_dir"]) == ROOT / ".local" / "stira"
    # The gateway config is still the working-directory default (not moved).
    assert seen["gateway_path"] == os.path.join(".local", "stira", "gateway-config.json")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("value", ["relative/data-dir", "/"])
def test_the_entry_point_refuses_an_unusable_value_before_creating_anything(tmp_path, value):
    result = subprocess.run(
        [sys.executable, str(ROOT / "dashboard" / "stira_server.py")],
        capture_output=True,
        text=True,
        env=_clean_env(STIRA_DATA_DIR=value, STIRA_PORT=str(_free_port())),
        cwd=tmp_path,
        timeout=120,
        check=False,
    )
    assert result.returncode == 2, (result.stdout[-1000:], result.stderr[-2000:])
    line = next(line for line in result.stderr.splitlines() if line.startswith("Stira: "))
    assert "STIRA_DATA_DIR" in line
    assert value not in line.replace("STIRA_DATA_DIR", "")
    assert list(tmp_path.iterdir()) == []


class _CapturingAgent:
    captured: ClassVar[dict] = {}

    def __init__(self, *, tools=(), on_breaker=None, state=None, config=None, **kwargs):
        from stiracore.core import StiraEngine

        self.engine = StiraEngine(state=state, config=config)
        self.tools = tuple(tools)
        self._stimulus_classifier = None
        self.prompt_cache = SimpleNamespace(enabled=False, ttl=None)
        self.context_management = SimpleNamespace(enabled=False)
        self.last_prompt_cache = self.last_context_management = None
        _CapturingAgent.captured = dict(kwargs)


def _explicit_app(monkeypatch, data_dir, cwd, soul):
    """What the server builds when STIRA_DATA_DIR named ``data_dir`` (resolved)."""
    from dashboard import stira_server

    monkeypatch.chdir(cwd)
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "DATA_DIR_FROM_ENV", True)
    monkeypatch.setattr(stira_server, "SOUL_PATH", soul)
    monkeypatch.setattr(stira_server, "NXPAgent", _CapturingAgent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    return stira_server.create_app()  # no config_store: the server's own defaults


def test_with_the_variable_settings_and_data_share_the_directory(tmp_path, monkeypatch, capsys):
    data = (tmp_path / "data").resolve()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    app = _explicit_app(monkeypatch, data, elsewhere, tmp_path / "SOUL.md")
    out = capsys.readouterr().out
    assert "configuration files are not in the data directory" not in out
    store = app.extensions["config_store"]
    for path in (store.gateway_path, store.agent_path, store.tool_path, store.history_path):
        assert path.parent == data, path
    status = app.test_client().get("/api/gateway/status").json
    assert status["config_location"] == {"matches_data_dir": True, "data_dir": "STIRA_DATA_DIR"}
    assert str(tmp_path) not in json.dumps(status["config_location"])
    # The tool rules were recorded next to the data, not under the working directory.
    assert (data / "tool-config.json").exists()
    assert not (elsewhere / ".local").exists()


def test_the_floor_follows_the_directory_and_its_symlinked_spelling(tmp_path, monkeypatch):
    """#243/#271: the policy files in the moved directory stay unwritable, also through
    the symlink the variable named and also when the data sits inside a write root."""
    from stiracore.config.data_dir import explicit_data_dir as resolve

    work = tmp_path / "work"
    (work / "stira-data").mkdir(parents=True)
    link = tmp_path / "data-link"
    link.symlink_to(work / "stira-data", target_is_directory=True)
    data = resolve({"STIRA_DATA_DIR": str(link)})
    assert data == (work / "stira-data").resolve()
    (data).mkdir(exist_ok=True)
    (data / "agent-config.json").write_text(
        json.dumps({"workspace_dir": str(work), "write_roots": ["@workspace"]}),
        encoding="utf-8",
    )
    _explicit_app(monkeypatch, data, tmp_path, tmp_path / "SOUL.md")
    policy = _CapturingAgent.captured["write_policy"]
    # Inside the write root, outside the data directory: allowed.
    assert policy.check(str(work / "notes.md")).allowed
    for name in ("gateway-config.json", "agent-config.json", "tool-config.json", "x.md"):
        for base in (data, link):
            decision = policy.check(str(base / name))
            assert not decision.allowed, (base, name)
            assert decision.code == "floor", (base, name, decision.code)


# --- stira_ctl.sh and stira doctor look at the same directory ------------------------

_STUB_SERVER = """
import os, signal, sys, time
from pathlib import Path
sys.path[:0] = {paths!r}
from stiracore.config.data_dir import explicit_data_dir
from stiracore.config.data_dir_lock import DataDirOwnerLock
data = explicit_data_dir(os.environ) or Path(__file__).resolve().parent.parent / ".local" / "stira"
data.mkdir(parents=True, exist_ok=True)
DataDirOwnerLock(data).acquire()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
time.sleep(120)
"""


@pytest.fixture
def ctl(tmp_path):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "dashboard").mkdir()
    shutil.copy(ROOT / "scripts" / "stira_ctl.sh", root / "scripts" / "stira_ctl.sh")
    (root / "dashboard" / "stira_server.py").write_text(
        _STUB_SERVER.format(paths=[str(ROOT / "src"), str(ROOT)]), encoding="utf-8"
    )
    pids: list[int] = []

    def run(*args, data_dir):
        env = {
            **_clean_env(STIRA_PORT=str(_free_port()), STIRA_PYTHON=sys.executable),
            "STIRA_DATA_DIR": str(data_dir),
        }
        return subprocess.run(
            [shutil.which("bash"), str(root / "scripts" / "stira_ctl.sh"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            cwd=root,
            check=False,
        )

    yield root, run, pids
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


@pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="bash script")
def test_stira_ctl_follows_the_data_directory(ctl, tmp_path):
    root, run, pids = ctl
    data = tmp_path / "data"
    started = time.monotonic()
    first = run("start", data_dir=data)
    assert first.returncode == 0, first.stderr
    assert (data / "stira_server.pid").exists()
    pid = int((data / "stira_server.pid").read_text().strip())
    pids.append(pid)
    assert (data / "server.lock").read_text().strip() == str(pid)
    # It saw the server take the lock (no 30 s wait on another directory's lock).
    assert time.monotonic() - started < 25
    assert not (root / ".local" / "stira").exists()
    status = run("status", data_dir=data)
    assert f"pid {pid} owns the data directory" in status.stdout
    stop = run("stop", data_dir=data)
    assert stop.returncode == 0, stop.stderr
    assert f"STOPPED (killed: {pid})" in stop.stdout


@pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="bash script")
def test_stira_ctl_refuses_a_relative_data_directory(ctl):
    root, run, _pids = ctl
    result = run("start", data_dir="relative/data")
    assert result.returncode == 2
    assert "STIRA_DATA_DIR must be an absolute path" in result.stderr
    assert not (root / "relative").exists()
    assert not (root / ".local").exists()


def test_stira_doctor_reads_the_config_in_the_data_directory(tmp_path, monkeypatch, capsys):
    from stiracore.diagnostics.__main__ import main

    data = tmp_path / "data"
    (data / "workspace").mkdir(parents=True)
    work = tmp_path / "chosen-workspace"
    work.mkdir()
    (data / "agent-config.json").write_text(
        json.dumps({"workspace_dir": str(work)}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STIRA_DATA_DIR", str(data))
    assert main(["doctor", "--json", "--no-write-test"]) in (0, 1)
    report = json.loads(capsys.readouterr().out)
    # The workspace of the agent-config.json inside STIRA_DATA_DIR (shown with ~ under home).
    assert report["workspace"].endswith("chosen-workspace"), report["workspace"]

    monkeypatch.setenv("STIRA_DATA_DIR", "relative")
    assert main(["doctor", "--json", "--no-write-test"]) == 2
    assert "STIRA_DATA_DIR must be an absolute path" in capsys.readouterr().err


# --- review round (#407): parents that are files, creation failures, one validator ------

CANARY = "canary-7f3c"


def test_a_path_below_a_file_is_refused(tmp_path):
    parent = tmp_path / f"{CANARY}-file"
    parent.write_text("x", encoding="utf-8")
    with pytest.raises(DataDirSettingError, match="not a directory") as caught:
        explicit_data_dir({"STIRA_DATA_DIR": str(parent / "data" / "deeper")})
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize("value", ["/ok\n/injected", "/tab\there"])
def test_control_characters_are_refused(value):
    with pytest.raises(DataDirSettingError, match="control character"):
        explicit_data_dir({"STIRA_DATA_DIR": value})


@pytest.mark.parametrize(
    "value, absolute",
    [
        ("C:stira-data", False),  # relative to the drive's current directory
        ("\\stira-data", False),  # relative to the current drive
        ("stira-data", False),
        ("C:\\stira-data", True),
        ("\\\\server\\share\\stira-data", True),
    ],
)
def test_windows_spellings_that_depend_on_the_working_directory_are_not_absolute(value, absolute):
    from pathlib import PureWindowsPath

    from stiracore.config.data_dir import is_fully_qualified

    assert is_fully_qualified(value, PureWindowsPath) is absolute


def _run_entry(tmp_path, value):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    result = subprocess.run(
        [sys.executable, str(ROOT / "dashboard" / "stira_server.py")],
        capture_output=True,
        text=True,
        env=_clean_env(STIRA_DATA_DIR=value, STIRA_PORT=str(_free_port())),
        cwd=cwd,
        timeout=120,
        check=False,
    )
    return result, cwd


def test_the_entry_point_refuses_a_path_below_a_file_without_printing_it(tmp_path):
    parent = tmp_path / f"{CANARY}-file"
    parent.write_text("x", encoding="utf-8")
    result, cwd = _run_entry(tmp_path, str(parent / "data"))
    assert result.returncode == 2, (result.stdout[-1000:], result.stderr[-2000:])
    assert any(line.startswith("Stira: STIRA_DATA_DIR") for line in result.stderr.splitlines())
    assert CANARY not in result.stdout + result.stderr
    assert list(cwd.iterdir()) == []


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
def test_the_entry_point_turns_a_failed_creation_into_exit_2_without_the_path(tmp_path):
    locked = tmp_path / f"{CANARY}-readonly"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        result, _cwd = _run_entry(tmp_path, str(locked / "data"))
    finally:
        locked.chmod(0o700)
    assert result.returncode == 2, (result.stdout[-1000:], result.stderr[-2000:])
    line = next(line for line in result.stderr.splitlines() if line.startswith("Stira: "))
    assert "STIRA_DATA_DIR cannot be created" in line
    assert CANARY not in result.stdout + result.stderr
    assert not (locked / "data").exists()


def _validator(value):
    env = _clean_env()
    if value is not None:
        env["STIRA_DATA_DIR"] = value
    return subprocess.run(
        [sys.executable, "-m", "stiracore.config.data_dir_check"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def _cases(tmp_path):
    parent = tmp_path / f"{CANARY}-file"
    parent.write_text("x", encoding="utf-8")
    good = tmp_path / "good"
    return {
        "unset": (None, ""),
        "blank": ("   ", ""),
        "good": (str(good), str(good.resolve())),
        "relative": ("relative/data", None),
        "drive-relative": ("C:stira-data", None),
        "root": ("/", None),
        "home": (str(Path.home()), None),
        "below-a-file": (str(parent / "data"), None),
    }


def test_the_validator_command_matches_the_server_check(tmp_path):
    for name, (value, expected) in _cases(tmp_path).items():
        result = _validator(value)
        environ = {} if value is None else {"STIRA_DATA_DIR": value}
        if expected is None:
            with pytest.raises(DataDirSettingError):
                explicit_data_dir(environ)
            assert result.returncode == 2, name
            assert "Stira: STIRA_DATA_DIR" in result.stderr, name
            assert "data_dir=" not in result.stdout, name
            assert CANARY not in result.stdout + result.stderr, name
        else:
            assert result.returncode == 0, (name, result.stderr[-1000:])
            assert result.stdout.splitlines()[-1] == f"data_dir={expected}", name
            assert (explicit_data_dir(environ) or "") == (Path(expected) if expected else ""), name


@pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="bash script")
def test_stira_ctl_sh_accepts_and_refuses_exactly_what_the_server_does(ctl, tmp_path):
    root, run, pids = ctl
    for name, (value, expected) in _cases(tmp_path).items():
        if value is None or expected is not None:
            continue
        result = run("status", data_dir=value)
        assert result.returncode == 2, (name, result.stdout, result.stderr)
        assert "Stira: STIRA_DATA_DIR" in result.stderr, name
        assert CANARY not in result.stdout + result.stderr, name
        assert not (root / ".local").exists(), name
    # Blank = unset, as in the server: the default directory gets the PID file.
    blank = run("start", data_dir="   ")
    assert blank.returncode == 0, blank.stderr
    pid_file = root / ".local" / "stira" / "stira_server.pid"
    assert pid_file.exists()
    pids.append(int(pid_file.read_text().strip()))
    assert run("stop", data_dir="   ").returncode == 0


def test_stira_ctl_ps1_uses_the_shared_validator():
    text = (ROOT / "scripts" / "stira_ctl.ps1").read_text(encoding="utf-8")
    assert "stiracore.config.data_dir_check" in text
    assert "IsPathRooted" not in text


def test_moving_the_whole_directory_keeps_the_rules_unlocked(tmp_path):
    """What docs/configuration.md tells an operator who moves an existing directory."""
    from stiracore.observability.config_api import ConfigStore
    from stiracore.tools.tool_config import ToolConfig, latch_path

    old = tmp_path / "old"
    old.mkdir()
    ConfigStore(old / "g.json", old / "a.json", old / "tool-config.json").load_tools()
    assert (old / "tool-config.json.head").exists()
    new = tmp_path / "new"
    shutil.move(str(old), str(new))
    ToolConfig.load(new / "tool-config.json")
    assert not latch_path(new / "tool-config.json").exists()
    # Moving the rules without their head is what locks them.
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copy(new / "tool-config.json", alone / "tool-config.json")
    ToolConfig.load(alone / "tool-config.json")
    assert latch_path(alone / "tool-config.json").exists()


# --- review round 2 (#407): OSError during the checks, control characters after resolve --


@pytest.mark.parametrize("failing", ["is_dir", "exists"])
def test_an_os_error_while_checking_becomes_exit_2_without_the_path(
    tmp_path, monkeypatch, capsys, failing
):
    from stiracore.config import data_dir_check

    target = tmp_path / f"{CANARY}-dir"
    target.mkdir()
    original = getattr(Path, failing)

    def denied(self, *args, **kwargs):
        if CANARY in str(self):
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, failing, denied)
    monkeypatch.setenv("STIRA_DATA_DIR", str(target))
    assert data_dir_check.main() == 2
    out, err = capsys.readouterr()
    assert err.strip() == "Stira: STIRA_DATA_DIR cannot be checked"
    assert CANARY not in out + err


def test_an_os_error_in_the_broad_root_check_is_refused_too(tmp_path, monkeypatch):
    from stiracore.config import data_dir

    def boom(path):
        raise PermissionError(13, "Permission denied", f"/{CANARY}")

    monkeypatch.setattr(data_dir, "broad_root_problem", boom)
    with pytest.raises(DataDirSettingError, match="cannot be checked") as caught:
        explicit_data_dir({"STIRA_DATA_DIR": str(tmp_path / "data")})
    assert CANARY not in str(caught.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file names")
def test_a_symlink_resolving_to_a_name_with_a_newline_is_refused(tmp_path):
    real = tmp_path / "real\ndata_dir=elsewhere"
    real.mkdir()
    link = tmp_path / "plain-link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(DataDirSettingError, match="control character"):
        explicit_data_dir({"STIRA_DATA_DIR": str(link)})
    # The wrappers' validator command refuses it as well (no second data_dir= line).
    result = _validator(str(link))
    assert result.returncode == 2
    assert "data_dir=" not in result.stdout
    assert "Stira: STIRA_DATA_DIR contains a control character" in result.stderr
