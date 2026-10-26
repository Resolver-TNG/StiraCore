"""#472: the settings page's heartbeat section, driven in jsdom with real hub states.

Renders the real settings template and three real ``/api/config/hub`` states: a running chat
server with ``STIRA_HEARTBEAT`` set (a marker value that must reach neither the hub state nor
the page; the switch is pinned and shows the running value), no chat server, and a hand-edited
floor below the page's limit; runs ``tests/a11y/settings_heartbeat_dom.cjs``.
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

from stiracore.config import GatewayConfig
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from tests.ui_pages import render_pages

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "a11y" / "settings_heartbeat_dom.cjs"
MARKER = "x-heartbeat-marker-7702"  # not 1/true/yes/on: the switch is off, the value secret


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


def test_heartbeat_section_in_the_browser(tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    monkeypatch.delenv("STIRA_HEARTBEAT", raising=False)

    def hub_state(name, runtime=None, live=None, **gateway):
        store = ConfigStore(
            tmp_path / name / "g.json", tmp_path / name / "a.json", tmp_path / name / "t.json"
        )
        if gateway:
            GatewayConfig(path=store.gateway_path, **gateway).save()
        app = Flask(name)
        app.register_blueprint(
            create_config_blueprint(
                store,
                lambda: "normal",
                runtime_settings_provider=runtime and (lambda: runtime),
                heartbeat_status_provider=live and (lambda: live),
            )
        )
        state = app.test_client().get("/api/config/hub").get_json()
        text = json.dumps(state)
        assert MARKER not in text, name
        path = tmp_path / f"{name}.json"
        path.write_text(text, encoding="utf-8")
        return state, path

    runtime = {
        "tools": None,
        "breaker": None,
        "classifier_choice": "auto",
        "error": None,
        "heartbeat": {
            "enabled": False,
            "interval_floor_sec": 300.0,
            "interval_ceil_sec": 10800.0,
            "error": None,
        },
    }
    # A running daemon's status (the shapes HeartbeatDaemon.status returns).
    live = {
        "enabled": True,
        "state": "running",
        "running": True,
        "stopping": False,
        "last_tick_at": 1_760_000_000.0,
        "next_tick_at": 1_760_000_900.0,
        "interval_sec": 900.0,
        "last_level": "normal",
        "last_priority": None,
        "last_duration_ms": 0.4,
        "tick_count": 2,
        "consecutive_failures": 0,
        "degraded": False,
        "clock_resyncs": 0,
        "last_error": None,
    }
    monkeypatch.setenv("STIRA_HEARTBEAT", MARKER)
    pinned, pinned_path = hub_state("pinned", runtime, live, hb_enabled=True)
    assert pinned["surface"]["fields"]["gateway.hb_enabled"]["pinned_by"] == "STIRA_HEARTBEAT"
    assert pinned["apply"]["heartbeat"]["status"] == "applied"
    monkeypatch.delenv("STIRA_HEARTBEAT")
    _standalone, standalone_path = hub_state("standalone")
    _kept, kept_path = hub_state("kept", hb_interval_floor_sec=30.0)
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    proc = subprocess.run(
        [
            node,
            str(SCRIPT),
            str(page),
            str(pinned_path),
            str(standalone_path),
            str(kept_path),
            MARKER,
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"settings heartbeat section (exit {proc.returncode}):\n{output}"
    assert "ok: heartbeat section" in output, output
