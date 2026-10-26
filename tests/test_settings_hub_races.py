"""#601 E7 / E9 / E10: answers that arrive while something else is on the way, in jsdom.

Renders the real settings page and drives ``settings_hub.js``
(``tests/a11y/settings_hub_races_dom.cjs``): a value typed while a save is on the way is
kept (also a scope radio, and on the model-only save path), the model section's "saved" event during a hub save does not put the older state
back, and a rollback retried after its confirmation keeps the save lock until it ends.
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
SCRIPT = ROOT / "tests" / "a11y" / "settings_hub_races_dom.cjs"


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


CASES = [
    "e9_values",
    "e9_model_only_values",
    "e9_scope_save",
    "e9_scope_model_only",
    "e7_model_event",
    "e10_rollback",
]


@pytest.mark.parametrize("case", CASES)
def test_settings_hub_answers_on_the_way(tmp_path, case):
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
    hub = app.test_client().get("/api/config/hub").get_json()
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    state = tmp_path / "hub.json"
    state.write_text(json.dumps(hub), encoding="utf-8")
    proc = subprocess.run(
        [node, str(SCRIPT), str(page), str(state), case],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"settings hub races {case} (exit {proc.returncode}):\n{output}"
