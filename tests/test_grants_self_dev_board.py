"""#791 (B6e): a self-development grant is marked on the Stage board and in the settings
panel — a boolean flag in the board's allow-list (only for such a grant), a badge in words
with a tooltip, drawn with ``textContent``; the words say what the grant really writes.

Offline: no model, no network.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from stiracore import self_protect
from stiracore.observability import activity

ROOT = Path(__file__).resolve().parents[1]
MARKER = "b6eMarker-3f9a71c2d4e8b605"
WORDS = (
    "grants.self_development_label",
    "grants.self_development_desc",
    "grants.self_development_badge_title",
    "grants.confirm_self_development",
    "stage.grant_self_dev_title",
)


def _view(**over):
    view = {
        "short": "abababab",
        "state": "active",
        "goal_state": "active",
        "headline": {"subject": "grant", "state": "active"},
        "issued_wall": 10.0,
    }
    view.update(over)
    return view


def test_the_board_carries_the_flag_only_for_a_self_development_grant():
    plain = activity.grant_view(_view())
    assert "self_development" not in plain
    flagged = activity.grant_view(_view(self_development=True))
    assert flagged == {**plain, "self_development": True}


@pytest.mark.parametrize("odd", [False, None, 1, "true", MARKER, [True], {"on": True}])
def test_anything_but_true_is_no_flag(odd):
    out = activity.grant_view(_view(self_development=odd))
    assert "self_development" not in out and MARKER not in json.dumps(out)


def test_the_pages_draw_the_badge_from_the_catalog_only():
    board = (ROOT / "dashboard/stage/js/activity.js").read_text(encoding="utf-8")
    assert board.count("grant.self_development === true") == 1
    assert "t('stage.grant_self_dev')" in board and "t('stage.grant_self_dev_title')" in board
    panel = (ROOT / "dashboard/static/js/settings_grants.js").read_text(encoding="utf-8")
    assert panel.count("grant.self_development === true") == 1
    assert 'tr("grants.self_development_badge")' in panel
    assert 'tr("grants.self_development_badge_title")' in panel
    for source in (board, panel):
        code = re.sub(r"//.*$", "", source, flags=re.MULTILINE)
        assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML", code)


@pytest.mark.parametrize("lang", ["en", "ja"])
def test_the_words_say_what_the_grant_really_writes(lang):
    catalog = json.loads(
        (ROOT / f"dashboard/static/i18n/catalog.{lang}.json").read_text(encoding="utf-8")
    )
    assert self_protect.CHECKOUT_WRITABLE == (("docs",),)  # the words below name this list
    for key in WORDS:
        text = catalog[key]
        assert "docs/" in text, key
        assert "docs/eval" in text or key == "grants.self_development_label", key
        # the old, too wide wording
        assert "edit Stira's own code" not in text and "rewrite Stira's own code" not in text
        assert "自身のコードを編集" not in text and "自身のコードを書き換え" not in text
    for key in ("grants.self_development_badge", "stage.grant_self_dev"):
        assert catalog[key].strip(), key
    label = catalog["grants.self_development_label"]
    assert label in catalog["grants.code_grant_subtree_protected"]
    assert label in catalog["grants.code_grant_self_development_off"]
    desc = catalog["grants.self_development_desc"]
    assert "docs/proposer-edit-boundary.md" in desc
    assert ("new test" in desc) if lang == "en" else ("新しいテスト" in desc)


def test_the_board_script_under_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not found on PATH")
    # (only this slice's case: the file's other cases are CI's ``node --test``)
    proc = subprocess.run(
        [
            node,
            "--test",
            "--test-name-pattern=#791",
            str(ROOT / "tests" / "stage_board_grant_test.cjs"),
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert re.search(r"^(ℹ|#) pass 1$", proc.stdout, flags=re.MULTILINE), proc.stdout[-1500:]
