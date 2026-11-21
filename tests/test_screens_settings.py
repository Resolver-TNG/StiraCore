"""#477 / #809 N1: the "Screens" section of the settings page (``ui.home`` and the Stage switch).

One hub group ``screens`` saved to ``gateway-config.json``: ``stage_enabled`` (top-level,
on by default, written only when off) and ``ui.home`` (default ``chat``). ``STIRA_STAGE=1/0``
still overrides the switch and
pins it on the page (names only). Both are read when the chat server starts, so a save
waits for a restart. Turning a page on is no loosening: no confirmation, no new route
until the restart, no authority.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.config import GatewayConfig
from stiracore.config import settings_surface as surface
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.observability.stage import stage_enabled

MARKER = "x-stage-marker-8090"  # not 1/true/yes/on: the switch is off, the value secret


@pytest.fixture(autouse=True)
def _no_stage_env(monkeypatch):
    monkeypatch.delenv("STIRA_STAGE", raising=False)


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )


def _client(store, **kwargs):
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal", **kwargs))
    return app.test_client()


@pytest.fixture
def client(store):
    return _client(store)


def _state(client):
    return client.get("/api/config/hub").get_json()


def _save(client, changes, **extra):
    return client.post(
        "/api/config/hub",
        json={"changes": changes, "base_version": _state(client)["version"], **extra},
    )


def _file(store) -> dict:
    return json.loads(store.gateway_path.read_text(encoding="utf-8"))


# --- the config bool and the variable ---------------------------------------------------------


def test_gateway_field_is_on_by_default_and_written_only_when_off(tmp_path):
    path = tmp_path / "gw.json"
    assert GatewayConfig().stage_enabled is True
    assert GatewayConfig().ui == {"home": "chat"}  # the default of ``/`` is unchanged
    GatewayConfig(path=path).save()
    assert "stage_enabled" not in json.loads(path.read_text(encoding="utf-8"))
    assert GatewayConfig.load(path).stage_enabled is True
    GatewayConfig(path=path, stage_enabled=False).save()
    assert json.loads(path.read_text(encoding="utf-8"))["stage_enabled"] is False
    assert GatewayConfig.load(path).stage_enabled is False
    # A file written before the default changed (explicit true) still loads as on.
    path.write_text('{"stage_enabled": true}', encoding="utf-8")
    assert GatewayConfig.load(path).stage_enabled is True
    for bad in (1, "1", None):
        with pytest.raises(ValueError, match="stage_enabled"):
            GatewayConfig(stage_enabled=bad)


@pytest.mark.parametrize(
    ("configured", "raw", "expected"),
    [
        (False, None, False),
        (True, None, True),
        (True, "", True),  # empty = not set: the file decides
        (True, "  ", True),
        (False, "1", True),
        (False, "on", True),
        (True, "0", False),  # the variable wins both ways
        (True, "no", False),
        (True, MARKER, False),  # anything else that is set is off, as before
    ],
)
def test_the_variable_overrides_the_config_bool(configured, raw, expected):
    environ = {} if raw is None else {"STIRA_STAGE": raw}
    gateway = GatewayConfig(stage_enabled=configured)
    assert stage_enabled(gateway, environ) is expected
    assert hub.effective_stage_enabled(gateway, environ) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, True), ("", True), ("  ", True), ("1", True), ("0", False), (MARKER, False)],
)
def test_the_default_config_is_on_unless_the_variable_says_off(raw, expected):
    environ = {} if raw is None else {"STIRA_STAGE": raw}
    assert stage_enabled(GatewayConfig(), environ) is expected


def test_without_a_config_only_the_variable_counts():
    assert stage_enabled(None, {}) is False
    assert stage_enabled(None, {"STIRA_STAGE": "1"}) is True


# --- the hub group ----------------------------------------------------------------------------


def test_defaults_and_opening_writes_nothing(client, store):
    state = _state(client)
    assert state["values"]["screens"] == {"stage": True, "home": "chat"}
    assert state["defaults"]["screens"] == {"stage": True, "home": "chat"}
    assert state["constraints"]["screens"] == {"homes": ["chat", "stage"]}
    # The next start has the Stage; this bare test app registered no Stage page.
    assert state["stage_enabled"] is True and state["stage_running"] is False
    field = state["surface"]["fields"]["gateway.stage_enabled"]
    assert (field["ui"], field["applies"], field["env"]) == ("yes", "restart", ["STIRA_STAGE"])
    assert (field["source"], field["pinned_by"], field["group"]) == ("default", None, "screens")
    assert state["apply"]["screens"] == {"status": "saved", "applies": "next_start"}
    assert not store.gateway_path.exists()


def test_turning_the_stage_on_saves_without_a_confirmation(store):
    store.gateway_path.write_text('{"stage_enabled": false}', encoding="utf-8")
    client = _client(store)
    before = _state(client)["values"]
    assert before["screens"] == {"stage": False, "home": "chat"}
    response = _save(client, {"screens": {"stage": True, "home": "stage"}})
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["saved"] == ["screens"]
    data = _file(store)
    assert "stage_enabled" not in data and data["ui"] == {"home": "stage"}
    assert body["state"]["values"]["screens"] == {"stage": True, "home": "stage"}
    # A page, not a permission: no danger, nothing to acknowledge.
    assert body["state"]["dangers"] == []
    assert hub.new_dangers(before, body["state"]["values"]) == []
    assert body["state"]["stage_enabled"] is True
    # Read at start-up: the page says it waits for a restart.
    surface_state = body["state"]["surface"]
    assert sorted(surface_state["restart_pending"]) == ["gateway.stage_enabled", "gateway.ui"]
    assert surface_state["restart_pending_groups"] == ["screens"]


def test_turning_it_off_writes_the_key_and_keeps_the_board_choice(client, store):
    assert _save(client, {"board": {"svg": False}}).status_code == 200
    assert "stage_enabled" not in _file(store)
    response = _save(client, {"screens": {"stage": False}})
    assert response.status_code == 200, response.get_json()
    # Turning a page off is no loosening either: nothing to acknowledge.
    assert response.get_json()["state"]["dangers"] == []
    assert response.get_json()["state"]["stage_enabled"] is False
    data = _file(store)
    assert data["stage_enabled"] is False
    assert data["ui"] == {"home": "chat", "board_svg": False}
    # ...and back on removes the key again.
    assert _save(client, {"screens": {"stage": True}}).status_code == 200
    data = _file(store)
    assert "stage_enabled" not in data
    assert data["ui"] == {"home": "chat", "board_svg": False}


def test_the_change_is_in_the_history_and_rolls_back(client, store):
    assert _save(client, {"screens": {"stage": False}}).status_code == 200
    entry = client.get("/api/config/history").get_json()["entries"][0]
    assert entry["changes"] == [{"field": "screens.stage", "from": True, "to": False}]
    undo = client.post(
        f"/api/config/history/{entry['id']}/rollback",
        json={"base_version": _state(client)["version"]},
    )
    assert undo.status_code == 200, undo.get_json()
    assert "stage_enabled" not in _file(store)


@pytest.mark.parametrize(
    ("changes", "field", "code"),
    [
        ({"stage": 1}, "screens.stage", "not_boolean"),
        ({"home": "observer"}, "screens.home", "unknown_choice"),
        # the Stage is (being turned) off: not selectable as the start page
        ({"stage": False, "home": "stage"}, "screens.home", "stage_off"),
        ({"extra": True}, "screens.extra", "unknown_field"),
    ],
)
def test_bad_values_are_refused_per_field_and_not_saved(client, store, changes, field, code):
    saved = _save(client, {"screens": changes})
    assert saved.status_code == 400
    errors = saved.get_json()["errors"]
    assert [(e["field"], e["code"]) for e in errors] == [(field, code)], errors
    assert not store.gateway_path.exists()


def test_home_stage_is_refused_while_the_stage_is_saved_off(store):
    store.gateway_path.write_text('{"stage_enabled": false}', encoding="utf-8")
    client = _client(store)
    saved = _save(client, {"screens": {"home": "stage"}})
    assert saved.status_code == 400
    errors = saved.get_json()["errors"]
    assert [(e["field"], e["code"]) for e in errors] == [("screens.home", "stage_off")]
    assert _file(store) == {"stage_enabled": False}


def test_home_stage_is_selectable_by_default(client, store):
    assert _save(client, {"screens": {"home": "stage"}}).status_code == 200
    assert _file(store)["ui"] == {"home": "stage"} and "stage_enabled" not in _file(store)


def test_a_saved_home_stage_does_not_block_turning_the_stage_off(client, store):
    assert _save(client, {"screens": {"stage": True, "home": "stage"}}).status_code == 200
    # ``/`` falls back to the chat at the next start (stira_server prints the warning).
    response = _save(client, {"screens": {"stage": False}})
    assert response.status_code == 200, response.get_json()
    data = _file(store)
    assert data["stage_enabled"] is False and data["ui"] == {"home": "stage"}
    # ...and an unrelated save still goes through with that pair in the file.
    assert _save(client, {"heartbeat": {"enabled": True}}).status_code == 200


def test_the_variable_pins_the_switch_names_only(client, store, monkeypatch):
    monkeypatch.setenv("STIRA_STAGE", MARKER)
    response = client.get("/api/config/hub")
    assert MARKER not in response.get_data(as_text=True)
    state = response.get_json()
    field = state["surface"]["fields"]["gateway.stage_enabled"]
    assert (field["source"], field["pinned_by"]) == ("env", "STIRA_STAGE")
    assert state["stage_enabled"] is False
    refused = _save(client, {"screens": {"stage": False}})
    assert refused.status_code == 400
    errors = refused.get_json()["errors"]
    assert [(e["field"], e["code"]) for e in errors] == [("screens.stage", "env_pinned")]
    assert errors[0]["params"] == {"env": "STIRA_STAGE"}
    assert not store.gateway_path.exists()
    # With the variable on, the Stage is on whatever the file says: home may be "stage".
    monkeypatch.setenv("STIRA_STAGE", "1")
    assert _save(client, {"screens": {"home": "stage"}}).status_code == 200
    assert _file(store)["ui"] == {"home": "stage"}


def test_apply_status_compares_with_the_running_server(store):
    app = Flask(__name__)
    runtime = {"tools": None, "breaker": None, "classifier_choice": "auto", "error": None}
    app.register_blueprint(
        create_config_blueprint(store, lambda: "normal", runtime_settings_provider=lambda: runtime)
    )
    client = app.test_client()
    # A chat server that started without the Stage (no /stage/ route, / is the chat),
    # while the configuration (the default) wants it at the next start.
    state = _state(client)
    assert state["apply"]["screens"] == {"status": "pending", "applies": "restart"}
    assert state["stage_enabled"] is True and state["stage_running"] is False
    assert _save(client, {"screens": {"stage": False}}).status_code == 200
    state = _state(client)
    assert state["apply"]["screens"] == {"status": "applied", "applies": "restart"}
    assert state["stage_enabled"] is False and state["stage_running"] is False


def test_the_manifest_rows():
    rows = {item["id"]: item for item in surface.items()}
    assert rows["env.STIRA_STAGE"]["overrides"] == ["gateway.stage_enabled"]
    assert rows["gateway.stage_enabled"]["group"] == "screens"
    assert rows["gateway.ui"]["ui"] == "yes" and rows["gateway.ui"]["group"] == "screens"
    assert set(hub.SCREENS_CONFIG_FIELDS.values()) == {"gateway.stage_enabled", "gateway.ui"}
