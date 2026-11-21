"""The /schedules page's poll (#369), run from pytest: ``tests/schedules_page_test.cjs``.

CI runs the file with ``node --test tests/*_test.cjs``; this runs the same file where only
pytest is at hand. The second test pins the server side of what the page leaves out of its
"did anything change" comparison: the scheduler's status carries the time of its last tick.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "schedules_page_test.cjs"


def test_schedules_page_node_tests():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not found on PATH")
    proc = subprocess.run(
        [node, "--test", str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-6000:]


def test_the_page_leaves_out_exactly_the_status_field_the_scheduler_sends():
    page = (ROOT / "dashboard" / "static" / "js" / "schedules_page.js").read_text(encoding="utf-8")
    scheduler = (ROOT / "src" / "stiracore" / "scheduler" / "scheduler.py").read_text(
        encoding="utf-8"
    )
    assert '"last_tick_at": self._last_tick_at' in scheduler
    assert "last_tick_at: undefined" in page
