"""#471 review: the breaker pairs and details on the real settings page, in jsdom.

Saves a customised breaker set and details through the real hub API, renders the real
template and drives ``settings_hub.js`` (``tests/a11y/settings_breaker_dom.cjs``): a moved
exit keeps the critical exit under the warning exit, the sycophancy threshold is shown as
an error instead of rounded, switching the alert off asks the typed confirmation, and
"reset to defaults" fills all six sliders and the details.
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
SCRIPT = ROOT / "tests" / "a11y" / "settings_breaker_dom.cjs"


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


def test_breaker_pairs_details_and_reset_on_the_page(tmp_path):
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    client = app.test_client()
    version = client.get("/api/config/hub").get_json()["version"]
    saved = client.post(
        "/api/config/hub",
        json={
            "changes": {
                "breaker": {"stability_warning": 0.7, "stability_warning_exit": 0.8},
                "breaker_details": {"bc_window_size": 30, "sycophancy_threshold": 0.8},
            },
            "base_version": version,
        },
    )
    assert saved.status_code == 200, saved.get_json()
    hub = client.get("/api/config/hub").get_json()
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
    assert proc.returncode == 0, f"settings breaker page (exit {proc.returncode}):\n{output}"
