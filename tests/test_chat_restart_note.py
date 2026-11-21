"""#655: the chat page after a restart, and the approval panel in a short window.

``tests/chat_restart_note_test.cjs`` (CI: ``node --test tests/*_test.cjs``) is run here too,
where only pytest is at hand. The CSS test pins the rules that keep "Approve once" inside
the chat panel: no browser is at hand in the suite, so it reads the stylesheet.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "chat_restart_note_test.cjs"
CSS = ROOT / "dashboard" / "static" / "css"


def test_chat_restart_note_node_tests():
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


def _rule(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"no rule for {selector}"
    return match.group(1)


def test_a_tall_approval_panel_scrolls_inside_the_chat_panel():
    """The chat panel has a fixed height and clips (``overflow: hidden``): a panel that
    cannot shrink pushes its own buttons and the composer out of it."""
    css = (CSS / "chat.css").read_text(encoding="utf-8")
    panel = _rule(css, ".chat-panel")
    assert "overflow: hidden" in panel and "height: calc(100dvh - 112px)" in panel
    fixed = ".chat-panel > :not(.conversation) { flex-shrink: 0; }"
    queue = _rule(css, ".chat-panel > .hitl-panel")
    assert "flex-shrink: 1" in queue and "overflow-y: auto" in queue
    # Same specificity as the rule it overrides, so it has to come later.
    assert css.index(fixed) < css.index(".chat-panel > .hitl-panel")


def test_approve_and_reject_stay_in_view_while_the_chat_queue_scrolls():
    css = (CSS / "approvals.css").read_text(encoding="utf-8")
    selector = ".chat-panel > .approval-queue .approval-actions:not(.approval-allow-actions)"
    actions = _rule(css, selector)
    assert "position: sticky" in actions and "bottom: 0" in actions
    # Opaque: the card's text scrolls under the row, not through it.
    assert "background: var(--surface)" in actions
    # The /approvals page (no fixed-height panel) is not changed.
    assert "position: sticky" not in _rule(css, ".approval-actions")
