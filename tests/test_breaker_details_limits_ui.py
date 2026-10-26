"""#471 review M-1 on the real settings page, in jsdom (``settings_breaker_limits_dom.cjs``).

The details inputs carry the server's upper limits; a value saved above a limit before it
existed is shown as it is with a note (never an error), so an unrelated change still saves;
a typed huge value is rounded into the limit; a server refusal shows on its field.
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
SCRIPT = ROOT / "tests" / "a11y" / "settings_breaker_limits_dom.cjs"


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


def test_detail_limits_on_the_page(tmp_path):
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    store.agent_path.write_text(
        json.dumps({"stira_config_overrides": {"bc_window_size": 10**9}}), encoding="utf-8"
    )
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    hub = app.test_client().get("/api/config/hub").get_json()
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
    assert proc.returncode == 0, f"settings breaker limits page (exit {proc.returncode}):\n{output}"
