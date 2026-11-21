"""#746: the chat page says when context management was asked for but refused.

``tests/chat_context_refused_test.cjs`` (CI: ``node --test tests/*_test.cjs``) is run here
too, where only pytest is at hand.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "chat_context_refused_test.cjs"


def test_chat_context_refused_node_tests():
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
