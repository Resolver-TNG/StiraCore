"""The timer examples of docs/memory-maintenance.md (#317) run as documented."""

from __future__ import annotations

import configparser
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from stiracore.config import AgentConfig
from stiracore.config.data_dir_lock import DataDirOwnerLock
from stiracore.tools.episodic_formatter import record_episode
from stiracore.tools.memory_store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "memory-maintenance"
WRAPPER = EXAMPLES / "stira-distill.sh"
BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(BASH is None or os.name == "nt", reason="POSIX shell example")


def _data_dir(tmp_path: Path, mode: str = "rule") -> Path:
    data = (tmp_path / "data").resolve()  # MemoryStore refuses symlinked parents (/tmp on macOS)
    AgentConfig(distiller_mode=mode, path=data / "agent-config.json").save()
    store = MemoryStore(data / "memory", "stira")
    record_episode(store, "thanks", "you are welcome", key="chat-1")
    record_episode(
        store, "## Decision\n- keep rule\n```\nx = 1\n```\nDecided.", "Noted.", key="chat-2"
    )
    return data


def _wrapper(data: Path, *args: str, **extra: str) -> tuple[int, str, list[dict]]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(data),
        "STIRA_ROOT": str(ROOT),
        "STIRA_DATA_DIR": str(data),
        "STIRA_PYTHON": sys.executable,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
        **extra,
    }
    done = subprocess.run(
        [BASH, str(WRAPPER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    log = Path(extra.get("STIRA_DISTILL_LOG", data / "distill-runs.jsonl"))
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return done.returncode, done.stderr, [json.loads(line) for line in lines]


@needs_bash
def test_wrapper_runs_the_rule_distiller_and_logs_one_line(tmp_path):
    data = _data_dir(tmp_path)

    code, stderr, log = _wrapper(data)

    assert code == 0, stderr
    assert "stira-distill:" not in stderr  # quiet on success
    assert [entry["phase"] for entry in log] == ["run"]
    result = log[0]["result"]
    assert result["mode"] == "rule" and result["harvest"]["errors"] == 0
    assert result["staging_pending"] == 1
    assert result["forgetting"]["applied"] == 0  # preview only unless asked
    if os.name != "nt":  # umask 077
        assert (data / "distill-runs.jsonl").stat().st_mode & 0o077 == 0
    assert not list(data.glob(".stira-distill.*"))  # the stderr scratch file is removed


@needs_bash
def test_wrapper_refuses_model_calls_by_default(tmp_path):
    data = _data_dir(tmp_path, mode="llm")

    code, stderr, log = _wrapper(data)

    assert code == 4
    assert "stira-distill: limit exit 4" in stderr
    assert log[-1]["phase"] == "limit"
    assert log[-1]["result"]["estimate"]["model_calls"] == 2
    assert not (data / "memory" / "agents" / "stira" / "memory" / "semantic" / "staging").exists()


@needs_bash
def test_wrapper_does_not_run_while_a_server_owns_the_directory(tmp_path):
    data = _data_dir(tmp_path)
    owner = DataDirOwnerLock(data)
    owner.acquire()
    try:
        code, stderr, log = _wrapper(data)
    finally:
        owner.release()

    assert code == 3
    assert "stira-distill: dry-run exit 3" in stderr
    assert log[-1]["result"]["refused"]["code"] == "E_DATA_DIR_OWNED"


@needs_bash
def test_wrapper_fails_loudly_without_a_data_directory(tmp_path):
    code, stderr, _log = _wrapper(tmp_path / "missing")

    assert code == 2
    assert "does not exist" in stderr


def test_launchd_example_is_a_valid_plist():
    plist = plistlib.loads((EXAMPLES / "com.example.stira-distill.plist").read_bytes())

    assert plist["ProgramArguments"][-1].endswith("examples/memory-maintenance/stira-distill.sh")
    assert plist["StartCalendarInterval"] == {"Hour": 4, "Minute": 30}
    assert plist["RunAtLoad"] is False
    assert "STIRA_ROOT" in plist["EnvironmentVariables"]


def test_systemd_examples_are_a_oneshot_service_and_a_timer():
    def load(name: str) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str  # systemd keys are case-sensitive
        parser.read(EXAMPLES / name, encoding="utf-8")
        return parser

    service, timer = load("stira-distill.service"), load("stira-distill.timer")
    assert service["Service"]["Type"] == "oneshot"
    assert service["Service"]["ExecStart"].endswith("examples/memory-maintenance/stira-distill.sh")
    assert timer["Timer"]["OnCalendar"] == "*-*-* 04:30:00"
    assert timer["Timer"]["Persistent"] == "true"
    assert timer["Install"]["WantedBy"] == "timers.target"


def test_cron_example_calls_the_wrapper():
    lines = [
        line
        for line in (EXAMPLES / "crontab.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(lines) == 1
    fields = lines[0].split()
    assert fields[:5] == ["30", "4", "*", "*", "*"]
    assert fields[-1].endswith('examples/memory-maintenance/stira-distill.sh"')
    assert 'STIRA_ROOT="' in lines[0] and "umask 077" in lines[0]


def _staging(data: Path) -> Path:
    return data / "memory" / "agents" / "stira" / "memory" / "semantic" / "staging"


@needs_bash
@pytest.mark.parametrize(
    "name",
    ["STIRA_DISTILL_MAX_CALLS", "STIRA_DISTILL_ERR_LINES", "STIRA_DISTILL_OWNER_LOCK"],
)
@pytest.mark.parametrize("value", ["a[$(touch {marker})0]", "1/0", "-1", "1 ", "0x10"])
def test_wrapper_never_evaluates_numeric_settings(tmp_path, name, value):
    data = _data_dir(tmp_path)
    marker = tmp_path / "evaluated"

    code, stderr, log = _wrapper(data, **{name: value.format(marker=marker)})

    assert code == 2, stderr
    assert name in stderr
    assert not marker.exists()  # no command substitution ran
    assert log == []  # refused before the dry run
    assert not _staging(data).exists()


@needs_bash
def test_wrapper_gives_the_preview_the_same_arguments(tmp_path):
    data = _data_dir(tmp_path)  # rule in the default config
    llm = data / "llm.json"
    AgentConfig(distiller_mode="llm", path=llm).save()

    code, stderr, log = _wrapper(data, "--config", str(llm))

    assert code == 4, stderr
    assert log[-1]["phase"] == "limit"
    assert log[-1]["result"]["mode"] == "llm"
    assert log[-1]["result"]["estimate"]["model_calls"] == 2
    assert not _staging(data).exists()


@needs_bash
@pytest.mark.parametrize("kind", ["symlink", "device", "directory"])
def test_wrapper_refuses_unsafe_log_paths(tmp_path, kind):
    data = _data_dir(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("keep\n", encoding="utf-8")
    log = data / "distill-runs.jsonl"
    if kind == "symlink":
        log.symlink_to(target)
        extra = {}
    elif kind == "device":
        extra = {"STIRA_DISTILL_LOG": "/dev/null"}
    else:
        extra = {"STIRA_DISTILL_ERR_LOG": str(tmp_path)}

    done = subprocess.run(
        [BASH, str(WRAPPER)],
        env={
            "PATH": "/usr/bin:/bin",
            "STIRA_ROOT": str(ROOT),
            "STIRA_DATA_DIR": str(data),
            "STIRA_PYTHON": sys.executable,
            "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
            **extra,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 5, done.stderr
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert not _staging(data).exists()  # nothing ran


@needs_bash
@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root ignores file modes")
def test_wrapper_fails_when_the_log_cannot_be_written(tmp_path):
    data = _data_dir(tmp_path)
    log = data / "distill-runs.jsonl"
    log.write_text("", encoding="utf-8")
    log.chmod(0o400)
    try:
        code, stderr, _log = _wrapper(data)
    finally:
        log.chmod(0o600)

    assert code == 6, stderr  # the run itself succeeded (exit 0)
    assert "attempt itself exited 0" in stderr


@needs_bash
def test_wrapper_keeps_only_the_masked_tail_of_stderr(tmp_path):
    data = _data_dir(tmp_path)
    fake = tmp_path / "python"
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    fake.write_text(
        "#!/bin/bash\n"
        f'if [[ "$1" == -c ]]; then exec "{sys.executable}" "$@"; fi\n'
        'for i in $(seq 1 40); do echo "noise $i" >&2; done\n'
        f'echo "AWS_SECRET_ACCESS_KEY={secret}" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)

    code, _stderr, log = _wrapper(data, STIRA_PYTHON=str(fake), STIRA_DISTILL_ERR_LINES="5")

    assert code == 1 and log[-1]["phase"] == "dry-run"
    kept = (data / "distill-runs.err.log").read_text(encoding="utf-8")
    assert secret not in kept and "AWS_SECRET_ACCESS_KEY=***" in kept
    assert "noise 37" in kept and "noise 36" not in kept  # last 5 lines only


@needs_bash
def test_cron_line_runs_from_a_path_with_spaces(tmp_path):
    data = _data_dir(tmp_path)
    checkout = tmp_path / "my checkout"
    checkout.mkdir()
    (checkout / "examples").symlink_to(ROOT / "examples")
    line = next(
        line
        for line in (EXAMPLES / "crontab.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )
    command = line.split(None, 5)[5].replace("/home/YOU/StiraCore", str(checkout))

    done = subprocess.run(
        ["/bin/sh", "-c", command],
        env={
            "PATH": "/usr/bin:/bin",
            "SHELL": "/bin/sh",
            "STIRA_DATA_DIR": str(data),
            "STIRA_PYTHON": sys.executable,
            "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    entry = json.loads((data / "distill-runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["phase"] == "run" and entry["exit"] == 0
