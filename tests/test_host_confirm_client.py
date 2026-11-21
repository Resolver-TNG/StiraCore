"""#656: the shared fetch wrapper's contract for a 428 ``host_confirmation_required``.

Runs ``tests/host_confirm_test.cjs`` (``dashboard/static/js/api.js`` in a vm context, a
scripted server, no network): a scheduler create answered 428 either shows the confirm
dialog or, on a page without it, settles at once with a readable 409.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "host_confirm_test.cjs"


def test_the_fetch_wrapper_settles_a_428_with_the_dialog_or_a_readable_refusal():
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
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"host_confirm_test.cjs (exit {proc.returncode}):\n{output[-6000:]}"
    )
