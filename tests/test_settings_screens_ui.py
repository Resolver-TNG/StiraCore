"""#477 / #809: the settings page's "Screens" section in jsdom, and the links between pages.

N1: the real settings template with four real ``/api/config/hub`` states (defaults: the Stage
on; the Stage saved off; the
switch saved on with ``STIRA_STAGE`` set to a marker that must reach neither the hub state nor
the page; ``ui.home: "stage"`` saved with the Stage off) runs
``tests/a11y/settings_screens_dom.cjs``.

N2: Settings → Goal grants and the approvals page link to the Stage only when the server
registered it; the Stage links to Settings → Screens; the Stage's grant panel links to the
approvals page.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.config import GatewayConfig
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from tests import ui_pages
from tests.test_stage_page import _app
from tests.ui_pages import render_pages

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "a11y" / "settings_screens_dom.cjs"
MARKER = "x-stage-marker-8091"  # not 1/true/yes/on: the switch is off, the value secret
NAV_ON = [("chat", "/chat", "Chat"), ("stage", "/stage/", "Stage")]
NAV_OFF = [("chat", "/chat", "Chat")]


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


def test_screens_section_in_the_browser(tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    monkeypatch.delenv("STIRA_STAGE", raising=False)

    def hub_state(name, **gateway) -> Path:
        store = ConfigStore(
            tmp_path / name / "g.json", tmp_path / name / "a.json", tmp_path / name / "t.json"
        )
        if gateway:
            GatewayConfig(path=store.gateway_path, **gateway).save()
        app = Flask(name)
        app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
        text = json.dumps(app.test_client().get("/api/config/hub").get_json())
        assert MARKER not in text, name
        path = tmp_path / f"{name}.json"
        path.write_text(text, encoding="utf-8")
        return path

    defaults = hub_state("defaults")  # no file: the Stage is on, "/" is the chat
    standalone = hub_state("standalone", stage_enabled=False)
    kept = hub_state("kept", stage_enabled=False, ui={"home": "stage"})
    monkeypatch.setenv("STIRA_STAGE", MARKER)
    pinned = hub_state("pinned", stage_enabled=True)
    monkeypatch.delenv("STIRA_STAGE")
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    states = [str(standalone), str(pinned), str(kept), MARKER, str(defaults)]
    proc = subprocess.run(
        [node, str(SCRIPT), str(page), *states],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"settings screens section (exit {proc.returncode}):\n{output}"
    assert "ok: screens section" in output, output


def test_the_settings_scripts_parse():
    """``node --check``: a syntax error would leave the whole settings page without its script."""
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    for name in ("settings_hub.js", "settings_surface.js"):
        path = ROOT / "dashboard" / "static" / "js" / name
        proc = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True, timeout=60, check=False
        )
        assert proc.returncode == 0, proc.stderr


def test_the_slider_has_the_limits_the_stage_reads():
    """The fps slider's limits and default are the Stage's own (``pacer.js``)."""
    pacer = (ROOT / "dashboard" / "stage" / "js" / "pacer.js").read_text(encoding="utf-8")
    html = (ROOT / "dashboard" / "templates" / "settings.html").read_text(encoding="utf-8")
    hub = (ROOT / "dashboard" / "static" / "js" / "settings_hub.js").read_text(encoding="utf-8")

    def const(name):
        return re.search(rf"export const {name} = '?([\w.]+)'?;", pacer).group(1)

    slider = re.search(r'<input id="screens-fps"[^>]*>', html).group(0)
    assert f'min="{const("MIN_FPS")}"' in slider
    assert f'max="{const("MAX_FPS")}"' in slider
    assert f'value="{const("DEFAULT_FPS")}"' in slider
    assert f'const FPS_KEY = "{const("FPS_KEY")}";' in hub


def _render(template: str, **context) -> str:
    import flask

    from stiracore.observability.i18n import install_i18n

    factory = ui_pages._find_blueprint_factory()
    if factory is None:
        pytest.skip("create_config_blueprint not importable")
    app = flask.Flask(
        __name__,
        template_folder=str(ui_pages.DASHBOARD / "templates"),
        static_folder=str(ui_pages.STATIC),
    )
    install_i18n(app)
    app.register_blueprint(ui_pages._build_blueprint(factory))
    app.add_url_rule("/chat", "chat_page", lambda: "")
    app.context_processor(lambda: {"csrf_token": lambda: "t"})
    with app.test_request_context("/"):
        return flask.render_template(template, **context)


@pytest.mark.parametrize(("nav", "linked"), [(NAV_ON, True), (NAV_OFF, False)])
def test_goal_grants_links_to_the_stage_only_when_it_is_on(nav, linked):
    html = _render("settings.html", grants_ui=True, shell_nav=nav, shell_status={})
    section = html[html.index('id="sec-grants"') : html.index('id="sec-screens"')]
    assert ('id="grants-open-stage" href="/stage/"' in section) is linked
    assert 'data-i18n="grants.open_stage"' in section or not linked
    screens = html[html.index('id="sec-screens"') : html.index('id="sec-board"')]
    assert ('id="screens-open-stage" href="/stage/"' in screens) is linked
    # The section itself is always there: it is where the Stage is turned on.
    assert 'href="#sec-screens"' in html and 'data-surface-field="gateway.stage_enabled"' in screens


@pytest.mark.parametrize("enabled", [True, False])
def test_the_pages_link_to_each_other_on_the_real_server(tmp_path, monkeypatch, enabled):
    client = _app(tmp_path, monkeypatch, enabled=enabled).test_client()
    approvals = client.get("/approvals").get_data(as_text=True)
    assert ('id="approvals-open-stage" href="/stage/"' in approvals) is enabled
    settings = client.get("/settings").get_data(as_text=True)
    assert 'id="sec-screens"' in settings
    assert ('id="screens-open-stage"' in settings) is enabled
    if enabled:
        stage = client.get("/stage/").get_data(as_text=True)
        assert 'id="stage-settings" class="mini" href="/settings#sec-screens"' in stage


def test_the_stage_grant_panel_links_to_the_approvals_page():
    """The link is in the panel unless its cue already is that link; never in "unknown"."""
    source = (ROOT / "dashboard" / "stage" / "js" / "activity.js").read_text(encoding="utf-8")
    block = source[source.index("export function renderGrant") :]
    assert "if (!g.unknown && !(g.cue && g.cue.href === APPROVALS_LINK))" in block
    assert "link(doc, t('stage.grant_cue_approval_link'), APPROVALS_LINK)" in block
