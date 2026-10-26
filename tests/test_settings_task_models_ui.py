"""Settings page "Model per task" (docs/task-models.md, review r588 #4) in jsdom: a running
server's rows plus the code of a saved setting that would refuse the next start."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.agent import task_models as tm
from stiracore.config import GatewayConfig
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.providers.spec import ModelSpec
from tests.ui_pages import render_pages

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "a11y" / "settings_task_models_dom.cjs"


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


def test_task_models_rows_in_the_browser(tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    for name in [n for n in os.environ if n.upper().startswith(tm.ENV_PREFIX)]:
        monkeypatch.delenv(name)

    def hub_state(name, runtime=None):
        store = ConfigStore(
            tmp_path / name / "g.json", tmp_path / name / "a.json", tmp_path / name / "t.json"
        )
        GatewayConfig(path=store.gateway_path, task_models={"tool_summary": "no-such"}).save()
        app = Flask(name)
        app.register_blueprint(
            create_config_blueprint(
                store, lambda: "normal", runtime_settings_provider=runtime and (lambda: runtime)
            )
        )
        state = app.test_client().get("/api/config/hub").get_json()
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        return state, path

    main = ModelSpec("bedrock", "global.anthropic.claude-sonnet-5", {})
    runtime = {
        "tools": None,
        "breaker": None,
        "classifier_choice": "auto",
        "error": None,
        "task_models": tm.inherit_router(main).describe(),
    }
    hub, running = hub_state("running", runtime)
    rows = {row["task"]: row for row in hub["cost"]["task_models"]["next_start"]}
    assert rows["tool_summary"]["error"] == "unknown_model"
    _standalone, standalone = hub_state("standalone")
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(SCRIPT), str(page), str(running), str(standalone)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"settings task models (exit {proc.returncode}):\n{output}"
