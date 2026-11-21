"""#680: the Stage's "■ Stop the turn" (``tests/stage_stop_turn_test.cjs``).

CI runs ``node --test tests/*_test.cjs``; this runs the same script where only pytest is
at hand (its jsdom tests skip without jsdom, as the script's own convention).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "stage_stop_turn_test.cjs"


def test_stage_stop_turn_node_tests():
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
