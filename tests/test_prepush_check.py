"""scripts/prepush_check.sh plans the cheap checks from the changed files (dry run, no tool is run)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepush_check.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash needed")


def run(files: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PREPUSH_FILES": files}
    r = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return r


def plan(files: str) -> str:
    return run(files).stdout


def test_docs_only_changes_need_no_check():
    assert "nothing to check" in plan("README.md\ndocs/stage.md")


def test_python_changes_plan_lint_and_format_or_say_ruff_is_missing():
    r = run("src/stiracore/x.py")
    assert (
        "check ." in r.stdout and "format --check ." in r.stdout
    ) or "ruff not found" in r.stderr


def test_stage_changes_plan_the_stage_node_tests_and_an_asset_rebuild_check():
    out = plan("dashboard/stage/js/gadget_scene.js\ndashboard/stage/tools/stack_beacon.py")
    assert "node --test tests/stage_gadgets_test.cjs" in out
    assert "build_gadgets.py" in out and "git diff --exit-code" in out


def test_a_changed_node_test_is_planned_by_name_and_a_deleted_one_is_not():
    out = plan("tests/stage_gadgets_test.cjs\ntests/does_not_exist_test.cjs")
    assert "node --test tests/stage_gadgets_test.cjs" in out
    assert "does_not_exist" not in out
