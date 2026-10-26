"""#470: the settings page's cost section, driven in jsdom with a real hub state.

Renders the real settings template and three real ``/api/config/hub`` states (with
``STIRA_PROMPT_CACHE`` and ``STIRA_CONTEXT_MANAGEMENT`` set, so two switches are pinned through
the settings surface, #469): with a running agent, without one, and with a saved cap above the
hub's sanity bound; runs ``tests/a11y/settings_cost_dom.cjs``. The context variable holds a
marker value that must not reach the hub state or the page (names only).
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

from stiracore.agent.context_management import ContextManagementSettings
from stiracore.config import GatewayConfig
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from tests.ui_pages import render_pages

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "a11y" / "settings_cost_dom.cjs"
MARKER = "x-context-marker-4821"  # not 1/true/yes/on: the switch is off, the value secret


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


def test_cost_section_in_the_browser(tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    for name in ("STIRA_PROMPT_CACHE_TTL", "STIRA_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STIRA_PROMPT_CACHE", "1")
    monkeypatch.setenv("STIRA_CONTEXT_MANAGEMENT", MARKER)

    def hub_state(name, runtime=None, **gateway):
        store = ConfigStore(
            tmp_path / name / "g.json", tmp_path / name / "a.json", tmp_path / name / "t.json"
        )
        if gateway:
            GatewayConfig(path=store.gateway_path, **gateway).save()
        app = Flask(name)
        app.register_blueprint(
            create_config_blueprint(
                store, lambda: "normal", runtime_settings_provider=runtime and (lambda: runtime)
            )
        )
        state = app.test_client().get("/api/config/hub").get_json()
        text = json.dumps(state)
        assert MARKER not in text, name
        path = tmp_path / f"{name}.json"
        path.write_text(text, encoding="utf-8")
        return state, path

    # The running agent uses the env value (cache on), the saved value is off.
    runtime = {
        "tools": None,
        "breaker": None,
        "classifier_choice": "auto",
        "error": None,
        "max_output_tokens": 64_000,
        "prompt_cache": {"enabled": True, "ttl": "5m"},
        "context_management": ContextManagementSettings().as_dict(),
    }
    hub, state = hub_state("running", runtime)
    # The fixture's model documents a limit above the automatic cap (the warning case).
    assert hub["cost"]["model"]["model_max_output"] > hub["cost"]["model"]["auto_max_output_tokens"]
    fields = hub["surface"]["fields"]
    assert fields["gateway.prompt_cache"]["pinned_by"] == "STIRA_PROMPT_CACHE"
    assert fields["gateway.context_management"]["pinned_by"] == "STIRA_CONTEXT_MANAGEMENT"
    assert fields["gateway.prompt_cache_ttl"]["pinned_by"] is None
    assert hub["apply"]["cache"]["running"]["enabled"] is True
    _no_runtime, no_runtime = hub_state("standalone")
    _big, big_cap = hub_state(
        "bigcap", dict(runtime, max_output_tokens=2_000_000), max_output_tokens=2_000_000
    )
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(SCRIPT), str(page), str(state), str(no_runtime), str(big_cap), MARKER],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"settings cost section (exit {proc.returncode}):\n{output}"
