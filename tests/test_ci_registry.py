"""CI must run every Node test file. A hand-written file list in ci.yml let 33 ``tests/*_test.cjs`` go unrun
(2026-11-22 audit); one of them had a stale assertion that nobody saw. The workflow now uses a glob; this keeps it so.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def node_test_commands() -> list[str]:
    return re.findall(r"^\s*run:\s*(node --test .*)$", CI, flags=re.MULTILINE)


def test_ci_has_a_node_test_step():
    assert node_test_commands(), "ci.yml lost its `node --test` step"


def test_every_node_test_file_runs_in_ci():
    commands = node_test_commands()
    files = sorted(p.name for p in (ROOT / "tests").glob("*_test.cjs"))
    assert files
    if any("tests/*_test.cjs" in c for c in commands):
        return  # the glob covers every file, including future ones
    listed = {Path(m).name for c in commands for m in re.findall(r"tests/[\w./-]+\.cjs", c)}
    missing = [f for f in files if f not in listed]
    assert not missing, f"not run by CI (add them, or use the glob `tests/*_test.cjs`): {missing}"


def test_helper_files_are_not_mistaken_for_tests():
    helpers = {
        "chat_dom_mock.cjs",
        "dialog_polyfill.cjs",
        "observability_cost_dom.cjs",
        "stage_png.cjs",
        "ui_shim.cjs",
    }
    existing = {p.name for p in (ROOT / "tests").glob("*.cjs") if not p.name.endswith("_test.cjs")}
    assert existing <= helpers | {"stage_png.cjs"}, (
        f"a new helper or an oddly named test: {sorted(existing - helpers)}"
    )
