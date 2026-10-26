"""#450: the tool profiles on the settings page, driven in jsdom (ja and en).

Renders the real settings template and a real ``/api/config/hub`` state (the reported
install: nine tools Always, nothing Ask) and runs ``tests/a11y/settings_profiles_dom.cjs``:
the cards, the preview (three rule groups, the exec variable, the YOLO line), axe with the
preview open, and "Put in draft" -> Save -> the shared confirmation -> ``profile:dev``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from tests.test_tool_profiles import _zero_ask
from tests.ui_pages import render_pages

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "a11y" / "settings_profiles_dom.cjs"


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_tool_profiles_preview_axe_and_confirmed_save(tmp_path, lang):
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "tool-config.json")
    _zero_ask(store)
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    hub = app.test_client().get("/api/config/hub").get_json()
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    state = tmp_path / "hub.json"
    state.write_text(json.dumps(hub), encoding="utf-8")
    proc = subprocess.run(
        [node, str(SCRIPT), str(page), str(state), lang],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"tool profiles ({lang}, exit {proc.returncode}):\n{output}"
