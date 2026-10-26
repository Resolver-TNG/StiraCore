"""The run-wide check that tests leave the checkout as they found it (#514)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import tree_guard


def test_new_and_missing_files_are_listed_and_litter_is_not(tmp_path):
    (tmp_path / "kept.txt").write_text("x", "utf-8")
    (tmp_path / "gone.txt").write_text("x", "utf-8")
    before = tree_guard.tree_files(tmp_path)
    (tmp_path / "gone.txt").unlink()
    (tmp_path / ".local" / "stira" / "checkpoints").mkdir(parents=True)
    (tmp_path / ".local" / "stira" / "checkpoints" / "a.json").write_text("{}", "utf-8")
    for litter in ("__pycache__/m.pyc", "node_modules/p/i.js", ".coverage.host", "a.py.swp"):
        (tmp_path / litter).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / litter).write_text("x", "utf-8")
    (tmp_path / "kept.txt").write_text("changed by an editor", "utf-8")
    after = tree_guard.tree_files(tmp_path)
    assert tree_guard.tree_changes(before, after) == [
        "+ .local/stira/checkpoints/a.json",
        "- gone.txt",
    ]


def test_a_data_dir_owned_by_a_running_server_is_skipped(tmp_path):
    live, stale, mine = (tmp_path / ".local" / name for name in ("live", "stale", "mine"))
    for directory in (live, stale, mine):
        directory.mkdir(parents=True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (live / "server.lock").write_text(str(child.pid), "ascii")
        (mine / "server.lock").write_text(str(os.getpid()), "ascii")
        (stale / "server.lock").write_text(str(2**22 + 12345), "ascii")  # above pid_max
        owned = tree_guard.owned_data_dirs(tmp_path)
    finally:
        child.kill()
        child.wait()
    # This process's own lock (a test that started a server here) is not a reason to skip.
    assert live in owned and mine not in owned
    if os.name != "nt":  # Windows cannot probe a PID without ending it: any note counts
        assert stale not in owned
    files = tree_guard.tree_files(tmp_path, tuple(owned))
    assert not any(f.startswith(".local/live/") for f in files)
    assert ".local/mine/server.lock" in files


def test_no_local_dir_means_nothing_to_skip(tmp_path):
    assert tree_guard.owned_data_dirs(tmp_path) == []


# --- the hooks themselves, through a real pytest run (exit status) --------------------

ROOT = Path(__file__).resolve().parents[1]
_INNER = """
import os
from pathlib import Path

def test_inner():
    report = os.environ.get("STIRA_CONTRACT_REPORT")
    if report:
        Path(report).write_text("{}", "utf-8")
    if os.environ.get("INNER_LEAK"):
        Path(".local", "stira").mkdir(parents=True)
        if os.environ["INNER_LEAK"] == "file":
            Path(".local", "stira", "t.json").write_text("{}", "utf-8")
"""


def _inner_run(tmp_path, *, report=None, leak=None, existing_report=False, empty_dir=None):
    (tmp_path / "conftest.py").write_text((ROOT / "conftest.py").read_text("utf-8"), "utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", "utf-8")
    (tmp_path / "test_inner.py").write_text(_INNER, "utf-8")
    if existing_report:
        (tmp_path / "report.json").write_text("{}", "utf-8")
    if empty_dir:
        (tmp_path / empty_dir).mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k not in ("STIRA_CONTRACT_REPORT",)}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    if report:
        env["STIRA_CONTRACT_REPORT"] = report
    if leak:
        env["INNER_LEAK"] = leak
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_inner.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


@pytest.mark.parametrize("existing", [False, True], ids=["new-report", "existing-report"])
def test_the_contract_report_is_never_a_change(tmp_path, existing):
    code, out = _inner_run(tmp_path, report="report.json", existing_report=existing)
    assert code == 0, out
    assert "changed the checkout" not in out


@pytest.mark.parametrize("leak", ["file", "empty-dir"])
def test_a_leak_fails_the_run_with_its_path(tmp_path, leak):
    code, out = _inner_run(tmp_path, report="report.json", leak=leak)
    assert code == 1, out
    expected = "+ .local/stira/t.json" if leak == "file" else "+ .local/stira/"
    assert expected in out, out


def test_a_report_in_an_existing_empty_directory_is_not_a_change(tmp_path):
    """The report's parent was an empty directory at the start: filling it is no change."""
    code, out = _inner_run(tmp_path, report="reports/r.json", empty_dir="reports")
    assert code == 0, out
    assert "changed the checkout" not in out


def test_an_empty_data_dir_still_fails_next_to_an_excluded_report(tmp_path):
    code, out = _inner_run(tmp_path, report="reports/r.json", empty_dir="reports", leak="empty-dir")
    assert code == 1, out
    assert "+ .local/stira/" in out and "reports" not in out.split("checkout")[-1], out
