"""#791 (ADR 0029 B6b), the dashboard side: the issue form's separate "self-development"
tick is rendered only on a server whose ``autonomy.grant_self_development`` is on, it is
unticked, and only a ticked box sends ``self_development: true``; the refusal codes have
words in both catalogs; the confirmation names the loosening.

Offline: no model, no network. The page script runs under node when it is installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")

import flask

from stiracore.grants import routes, schema
from tests.ui_pages import render_pages

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "dashboard" / "static" / "js" / "settings_grants.js"
REFUSALS = (
    "grant_subtree_protected",
    "grant_subtree_running_checkout",
    "grant_self_development_off",
)
KEYS = (
    "grants.self_development_label",
    "grants.self_development_desc",
    "grants.self_development_badge",
    "grants.self_development_badge_title",
    "grants.confirm_self_development",
    *(f"grants.code_{code}" for code in REFUSALS),
)
BOX = 'id="grants-self-development"'


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the page check is unavailable: {reason}")
    pytest.skip(reason)


@pytest.mark.parametrize(
    ("setting", "shown"),
    [(True, "on"), (False, "off"), (1, "off"), ("true", "off"), (None, "off")],
)
def test_the_page_is_told_only_when_the_setting_is_exactly_true(setting, shown):
    app = flask.Flask(__name__)
    service = SimpleNamespace(settings=SimpleNamespace(grant_self_development=setting))
    routes.install(app, service)
    with app.test_request_context("/"):
        out = flask.render_template_string("{{ 'on' if grants_self_dev_ui else 'off' }}")
    assert out == shown


def test_a_service_without_settings_shows_no_box():
    app = flask.Flask(__name__)
    routes.install(app, SimpleNamespace())
    with app.test_request_context("/"):
        assert flask.render_template_string("{{ grants_self_dev_ui is defined }}") == "False"


def test_the_box_is_rendered_only_with_the_setting_and_is_not_ticked(tmp_path):
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    for name in ("settings", "settings_grants", "settings_grants_tick"):
        assert BOX not in pages[name], name
    page = pages["settings_grants_self_dev"]
    tag = re.search(r"<input[^>]*" + re.escape(BOX) + r"[^>]*>", page).group(0)
    assert 'type="checkbox"' in tag and "checked" not in tag
    assert 'aria-describedby="grants-self-development-desc"' in tag
    assert '<label class="choice switch-row" for="grants-self-development">' in page
    for key in ("grants.self_development_label", "grants.self_development_desc"):
        assert f'data-i18n="{key}"' in page
    assert 'id="grants-self-development-desc"' in page


def test_the_words_are_in_both_catalogs_and_the_codes_are_the_servers():
    source = PANEL.read_text(encoding="utf-8")
    body = re.search(r"const REFUSAL_CODES = \[([^\]]*)\]", source).group(1)
    assert tuple(re.findall(r'"([a-z_]+)"', body)) == REFUSALS
    assert set(REFUSALS) <= set(schema.CODES)
    # every issue refusal about Stira's own code that the server knows has its words here
    own = {c for c in schema.CODES if c.startswith(("grant_subtree_", "grant_self_development"))}
    assert own - {"grant_subtree_invalid", "grant_subtree_outside"} == set(REFUSALS)
    assert source.count("self_development: true") == 1
    assert source.count('"loosen:self_development"') == 1
    for lang in ("en", "ja"):
        catalog = json.loads(
            (ROOT / f"dashboard/static/i18n/catalog.{lang}.json").read_text(encoding="utf-8")
        )
        for key in KEYS:
            assert catalog.get(key, "").strip(), (lang, key)
        # the tick says what stays read-only; a refusal names the setting
        for code in REFUSALS:
            assert "autonomy.grant_self_development" in catalog[f"grants.code_{code}"]


def test_the_panel_with_the_box_is_axe_clean_and_an_unticked_box_sends_nothing(tmp_path):
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    page = tmp_path / "settings_grants_self_dev.html"
    page.write_text(pages["settings_grants_self_dev"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "grants_axe.cjs"), str(page)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"grants panel a11y (exit {proc.returncode}):\n{output}"


def test_the_page_script_under_node():
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    proc = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "settings_grants_self_dev_test.cjs")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
