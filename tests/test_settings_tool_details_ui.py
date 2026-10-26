"""#281 / #282: the hub's tool list folds the effective stack; saves confirm in the shared dialog.

Renders the real settings template and a real ``/api/config/hub`` state (breaker WARNING,
fresh install) and drives ``settings_hub.js`` in jsdom (``tests/a11y/settings_tools_dom.cjs``).
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
from tests.ui_pages import render_pages

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "a11y" / "settings_tools_dom.cjs"


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


def test_tool_details_and_shared_confirm_dialog(tmp_path):
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "warning"))
    hub = app.test_client().get("/api/config/hub").get_json()
    assert hub["breaker_level"] == "warning"
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    state = tmp_path / "hub.json"
    state.write_text(json.dumps(hub), encoding="utf-8")
    proc = subprocess.run(
        [node, str(SCRIPT), str(page), str(state)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"settings tool list / dialog (exit {proc.returncode}):\n{output}"
