"""#471 on the #490 settings surface: the breaker set and its details use ``surface.fields``.

- The manifest marks the 13 breaker rows (3 entries, 3 exits, 7 details) ``ui: "yes"`` and the
  settings page carries ``data-surface-field="stira.<name>"`` on exactly those inputs (both ways).
- The hub answer says per field where the value comes from (``source``) and when it applies
  (``applies``), and a saved detail shows as "waits for a restart" (group ``breaker_details``);
  an entry/exit (next turn) never does. The hub-only ``breaker_detail_fields`` map is gone.
- A variable that pins a breaker field (none does today: the manifest is extended for the test)
  disables that input on the real page with the variable's **name** as the reason, never its
  value (``tests/a11y/settings_breaker_surface_dom.cjs``, axe on the breaker section).
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.config import settings_surface as surface
from stiracore.config import stira_delivery
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from tests.ui_pages import render_pages

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "dashboard" / "templates" / "settings.html"
SCRIPT = ROOT / "tests" / "a11y" / "settings_breaker_surface_dom.cjs"
BREAKER_GROUPS = ("breaker", "breaker_details")
NAMES = (*hub.BREAKER_SET, *hub.DETAIL_FIELDS)
PIN_VAR = "STIRA_PROBE_SYCOPHANCY"
PIN_VALUE = "MARKER-0.4242-not-for-the-page"


def _breaker_rows(data: dict | None = None) -> list[dict]:
    rows = (data or surface.load_manifest())["items"]
    return [row for row in rows if row.get("group") in BREAKER_GROUPS]


def _marked_fields(html: str) -> list[str]:
    return re.findall(r'data-surface-field="([^"]+)"', html)


def _store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")


def _client(store: ConfigStore):
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    return app.test_client()


def _save(client, changes: dict, **extra):
    version = client.get("/api/config/hub").get_json()["version"]
    return client.post(
        "/api/config/hub", json={"changes": changes, "base_version": version, **extra}
    )


# --- the manifest and the page agree -----------------------------------------------------


def test_the_thirteen_breaker_rows_are_on_the_page() -> None:
    rows = _breaker_rows()
    assert sorted(row["id"] for row in rows) == sorted(f"stira.{name}" for name in NAMES)
    assert {row["id"]: row["ui"] for row in rows} == {f"stira.{name}": "yes" for name in NAMES}


def test_every_breaker_row_has_one_marked_input_and_back() -> None:
    marked = _marked_fields(TEMPLATE.read_text(encoding="utf-8"))
    assert len(marked) == len(set(marked)), "one input per field"
    on_page = {field for field in marked if field.startswith("stira.")}
    assert on_page == {row["id"] for row in _breaker_rows() if row["ui"] == "yes"}
    # Each input's name is the field it marks (what the hub saves).
    html = TEMPLATE.read_text(encoding="utf-8")
    for name in NAMES:
        assert re.search(rf'name="{name}" data-surface-field="stira\.{name}"', html), name


def test_every_marked_input_names_a_ui_yes_row() -> None:
    by_id = {row["id"]: row for row in surface.load_manifest()["items"]}
    for field in _marked_fields(TEMPLATE.read_text(encoding="utf-8")):
        assert field in by_id, f"{field}: not in the manifest"
        assert by_id[field]["ui"] == "yes", f"{field}: marked on the page but ui is not yes"


# --- the hub answer: source, applies, restart pending ------------------------------------


def test_hub_says_source_and_applies_for_each_breaker_field(tmp_path) -> None:
    client = _client(_store(tmp_path))
    state = client.get("/api/config/hub").get_json()
    assert "breaker_detail_fields" not in state
    fields = state["surface"]["fields"]
    for name in NAMES:
        entry = fields[f"stira.{name}"]
        assert entry["ui"] == "yes"
        assert entry["source"] == "default", name
        assert entry["applies"] == stira_delivery.applies_when(name), name
        assert entry["pinned_by"] is None and entry["env"] == []
    assert {fields[f"stira.{n}"]["applies"] for n in hub.BREAKER_SET} == {"next_turn"}
    assert {fields[f"stira.{n}"]["applies"] for n in hub.DETAIL_FIELDS} == {"restart"}


def test_a_saved_detail_waits_for_a_restart_an_exit_does_not(tmp_path) -> None:
    client = _client(_store(tmp_path))
    response = _save(client, {"breaker": {"stability_warning": 0.7, "stability_warning_exit": 0.8}})
    assert response.status_code == 200, response.get_json()
    after = response.get_json()["state"]["surface"]
    assert after["fields"]["stira.stability_warning_exit"]["source"] == "agent"
    assert after["fields"]["stira.stability_critical_exit"]["source"] == "default"
    assert after["restart_pending"] == [], "entries and exits apply from the next turn"
    response = _save(client, {"breaker_details": {"bc_window_size": 30, "hitl_timeout_sec": 600}})
    assert response.status_code == 200, response.get_json()
    after = response.get_json()["state"]["surface"]
    assert sorted(after["restart_pending"]) == ["stira.bc_window_size", "stira.hitl_timeout_sec"]
    assert after["restart_pending_groups"] == ["breaker_details"], "two details = one change"
    assert after["fields"]["stira.bc_window_size"]["pending_restart"] is True
    assert after["fields"]["stira.bc_window_size"]["source"] == "agent"
    assert after["fields"]["stira.stability_warning_exit"]["pending_restart"] is False


# --- a variable pins a breaker field: the page disables it and names the variable --------


def _with_probe_variable(monkeypatch) -> None:
    """A manifest with one extra server variable that overrides the sycophancy threshold."""
    data = copy.deepcopy(surface._cached())
    data["items"].append(
        {
            "id": f"env.{PIN_VAR}",
            "class": "A",
            "ui": "partial",
            "applies": "restart",
            "overrides": ["stira.sycophancy_threshold"],
            "when": "non_empty",
        }
    )
    assert surface.problems(data) == []
    monkeypatch.setattr(surface, "_cached", lambda: data)
    monkeypatch.setenv(PIN_VAR, PIN_VALUE)


def test_a_pinned_breaker_field_is_named_never_valued(tmp_path, monkeypatch) -> None:
    _with_probe_variable(monkeypatch)
    client = _client(_store(tmp_path))
    state = client.get("/api/config/hub").get_json()
    entry = state["surface"]["fields"]["stira.sycophancy_threshold"]
    assert entry["pinned_by"] == PIN_VAR and entry["source"] == "env"
    assert entry["env"] == [PIN_VAR]
    # Other variables of the machine running the tests may be listed too; this one is, by name.
    probe = [p for p in state["surface"]["pinned"] if p["name"] == PIN_VAR]
    assert probe == [
        {
            "name": PIN_VAR,
            "fields": ["stira.sycophancy_threshold"],
            "applies": "restart",
            "class": "A",
            "issue": None,
        }
    ]
    assert PIN_VALUE not in json.dumps(state)
    assert "MARKER" not in json.dumps(state)


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


def test_the_page_disables_a_pinned_breaker_field(tmp_path, monkeypatch) -> None:
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "pages")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    _with_probe_variable(monkeypatch)
    client = _client(_store(tmp_path))
    # A saved detail (restart) and exit (next turn): the banner names the details only.
    response = _save(
        client,
        {
            "breaker": {"stability_warning": 0.7, "stability_warning_exit": 0.8},
            "breaker_details": {"bc_window_size": 30},
        },
    )
    assert response.status_code == 200, response.get_json()
    hub_state = client.get("/api/config/hub").get_json()
    assert PIN_VALUE not in json.dumps(hub_state)
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    state = tmp_path / "hub.json"
    state.write_text(json.dumps(hub_state), encoding="utf-8")
    proc = subprocess.run(
        [node, str(SCRIPT), str(page), str(state), PIN_VALUE],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"breaker surface page (exit {proc.returncode}):\n{output}"
