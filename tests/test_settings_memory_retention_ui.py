"""#474: the settings page's "Memory retention limits" section in jsdom, and the chat
server's wiring of the read-only dry run.

The real settings template with a real ``/api/config/hub`` state and a real dry-run answer
(built from a data directory whose episodes hold a canary text) runs
``tests/a11y/settings_retention_dom.cjs``: inputs from the schema, the page's range check, the
change list, the save body, counts only, "apply is command line only".
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

from stiracore.config.models import AgentConfig
from stiracore.control.memory_layout import DataLayout
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.tools.memory_store import MemoryLayer, MemoryStore
from tests.ui_pages import render_pages

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "a11y" / "settings_retention_dom.cjs"
BODY = "Retention-Page-Canary-4471"
DRY_RUN = "/api/config/memory-retention/dry-run"


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


def _episodes(data: Path, count: int = 3) -> None:
    memory = MemoryStore(base_dir=data / "memory", agent_id="stira")
    for index in range(count):
        memory.write(MemoryLayer.EPISODIC, f"canary-key-{index}", f"{BODY} {index}")


def test_retention_section_in_the_browser(tmp_path):
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    assert 'id="sec-memory-retention"' in pages["settings"]

    data = tmp_path / "data"
    _episodes(data)
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    app = Flask(__name__)
    app.register_blueprint(
        create_config_blueprint(
            store, lambda: "normal", retention_layout_provider=lambda: DataLayout.at(data)
        )
    )
    client = app.test_client()
    hub = tmp_path / "hub.json"
    hub.write_text(json.dumps(client.get("/api/config/hub").get_json()), encoding="utf-8")
    # The counts of a set limit; the page's hub state stays the default one (no limit).
    AgentConfig(
        path=store.agent_path,
        memory_retention={"episodic": {"max_entries": 1, "keep_unharvested": False}},
    ).save()
    answer = client.get(DRY_RUN).get_json()
    assert answer["will_remove"]["episodic"] == 2
    dry = tmp_path / "dry.json"
    dry.write_text(json.dumps(answer), encoding="utf-8")
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")

    proc = subprocess.run(
        [node, str(SCRIPT), str(page), str(hub), str(dry), BODY],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"settings retention section (exit {proc.returncode}):\n{output}"
    assert "ok: memory retention section" in output, output


def test_the_chat_server_wires_the_dry_run_to_its_data_directory(tmp_path, monkeypatch):
    """``stira_server`` passes ``retention_layout_provider``: on the real app the dry run counts
    the server's own data directory, returns counts only and removes nothing."""
    from tests.test_stage_page import _app

    client = _app(tmp_path, monkeypatch, enabled=False).test_client()
    from dashboard import stira_server as server

    data = Path(server.DATA_DIR)
    _episodes(data)
    state = client.get("/api/config/hub").get_json()
    assert state["values"]["memory_retention"]["episodic__max_entries"] is None
    before = client.get(DRY_RUN).get_json()
    assert before["available"] is True, before
    assert before["apply"] == "cli"
    assert before["will_remove"]["episodic"] == 0

    saved = client.post(
        "/api/config/hub",
        json={
            "changes": {
                "memory_retention": {
                    "episodic__max_entries": 1,
                    "episodic__keep_unharvested": False,
                }
            },
            "base_version": state["version"],
        },
    )
    assert saved.status_code == 200, saved.get_json()
    response = client.get(DRY_RUN)
    body = response.get_json()
    assert body["available"] is True and body["policy_configured"] is True
    assert body["will_remove"]["episodic"] == 2
    text = response.get_data(as_text=True)
    assert BODY not in text and "canary-key" not in text and str(tmp_path) not in text
    # Saving a limit and counting removed nothing.
    assert len(list((data / "memory").rglob("canary-key-*"))) == 3
