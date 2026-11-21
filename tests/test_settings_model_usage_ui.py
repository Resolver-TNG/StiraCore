"""#476 / #195: the settings page's model fields and its usage card, driven in jsdom.

Renders the real settings template and real API answers (``/api/config/providers`` with and
without ``AWS_REGION``, the usage summary of real ``turn.completed`` data) and runs
``tests/a11y/settings_model_usage_dom.cjs``: the region / organization-variable fields, the
save body (``base_version``), coded refusals in the page's language (no server sentence, no
exception text), and a usage card where a count nobody reported reads "unknown", never 0.
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
from stiracore.observability.turns_view import usage_summary
from tests.ui_pages import render_pages

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "a11y" / "settings_model_usage_dom.cjs"
MARKER = "sk-test-marker-476-0123456789"
PINS = ("STIRA_PROVIDER", "STIRA_MODEL", "STIRA_BASE_URL", "STIRA_API_KEY_ENV", "AWS_REGION")


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


def _run(mode: str, tmp_path, fixture: dict) -> None:
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    data = tmp_path / "fixture.json"
    data.write_text(json.dumps(fixture), encoding="utf-8")
    proc = subprocess.run(
        [node, str(SCRIPT), mode, str(page), str(data), MARKER],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"settings {mode} (exit {proc.returncode}):\n{output}"


def test_model_fields_in_the_browser(tmp_path, monkeypatch):
    for name in PINS:
        monkeypatch.delenv(name, raising=False)

    def providers(name):
        store = ConfigStore(
            tmp_path / name / "g.json", tmp_path / name / "a.json", tmp_path / name / "t.json"
        )
        app = Flask(name)
        app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
        return app.test_client().get("/api/config/providers").get_json()

    plain = providers("plain")
    assert plain["region_pinned_by"] is None and plain["current"]["provider"] == "bedrock"
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    pinned = providers("pinned")
    assert pinned["region_pinned_by"] == "AWS_REGION"
    assert "eu-central-1" not in json.dumps(pinned)
    _run("model", tmp_path, {"providers": plain, "pinned": pinned})


def test_usage_card_in_the_browser(tmp_path):
    usage = usage_summary(
        [
            {"model": "model-a", "usage": {"input_tokens": 1200, "output_tokens": 340}},
            {"model": "model-a", "usage": {"input_tokens": 300, "output_tokens": 60}},
            {"model": "local-model", "usage": {}},
            {"usage": {"input_tokens": 5, "output_tokens": 0}},
        ]
    )
    local = next(row for row in usage["by_model"] if row["model"] == "local-model")
    assert local["input_tokens"] is None and usage["total"]["turns_without_usage"] == 1
    _run("usage", tmp_path, {"usage": {"source": "turns.db", **usage}})
