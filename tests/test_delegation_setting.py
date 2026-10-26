"""#486 (ADR 0016 phase 1): the ``delegation`` settings group (gateway ``delegation``).

On by default (operator decision 2026-10-19); the caps of one child are integers inside
``DelegationSettings``' bounds; only values that differ from the defaults are written; every
change is in the settings history and can be rolled back. Turning delegation on or off and
changing a cap are not dangers: a ``delegate`` call is a person's card either way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from flask import Flask

from stiracore.config import GatewayConfig
from stiracore.observability.config_api import ConfigStore, create_config_blueprint


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )


@pytest.fixture
def client(store):
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    return app.test_client()


def _state(client):
    return client.get("/api/config/hub").get_json()


def _save(client, changes):
    return client.post(
        "/api/config/hub", json={"changes": changes, "base_version": _state(client)["version"]}
    )


def test_gateway_section_is_checked_and_absent_by_default(tmp_path):
    path = tmp_path / "gateway-config.json"
    GatewayConfig(path=path).save()
    assert "delegation" not in json.loads(path.read_text(encoding="utf-8"))
    assert GatewayConfig(delegation={"enabled": False}).delegation == {"enabled": False}
    for bad in ({"enabled": 1}, {"max_tool_calls": 0}, {"max_seconds": 99999}, {"x": 1}):
        with pytest.raises(ValueError):
            GatewayConfig(delegation=bad)


def test_on_by_default_with_the_adr_caps_and_no_danger(client):
    state = _state(client)
    assert state["values"]["delegation"] == {
        "enabled": True,
        "max_tool_calls": 40,
        "max_seconds": 600,
        "max_children_per_turn": 20,
    }
    assert "delegation" not in " ".join(state["dangers"])  # not a danger
    assert state["constraints"]["delegation"]["max_tool_calls"] == {"min": 1, "max": 100}
    assert state["apply"]["delegation"]["applies"] == "next_turn"


def test_off_and_caps_are_saved_only_when_not_default_and_can_be_rolled_back(client, store):
    saved = _save(client, {"delegation": {"enabled": False, "max_tool_calls": 10}})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["saved"] == ["delegation"]
    data = json.loads(store.gateway_path.read_text(encoding="utf-8"))
    assert data["delegation"] == {"enabled": False, "max_tool_calls": 10}
    history = client.get("/api/config/history").get_json()["entries"]
    assert {"field": "delegation.enabled", "from": True, "to": False} in history[0]["changes"]
    rolled = client.post(
        f"/api/config/history/{history[0]['id']}/rollback",
        json={"base_version": _state(client)["version"]},
    )
    assert rolled.status_code == 200, rolled.get_json()
    assert rolled.get_json()["state"]["values"]["delegation"]["enabled"] is True
    assert "delegation" not in json.loads(store.gateway_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("change", "field", "code"),
    [
        ({"enabled": "yes"}, "delegation.enabled", "not_boolean"),
        ({"max_tool_calls": 0}, "delegation.max_tool_calls", "out_of_range"),
        ({"max_seconds": 1.5}, "delegation.max_seconds", "not_integer"),
        ({"max_children_per_turn": True}, "delegation.max_children_per_turn", "not_integer"),
    ],
)
def test_invalid_values_are_refused_by_field(client, store, change, field, code):
    response = _save(client, {"delegation": change})
    assert response.status_code == 400
    errors = response.get_json()["errors"]
    assert {(e["field"], e["code"]) for e in errors} == {(field, code)}
    assert not store.gateway_path.exists() or "delegation" not in json.loads(
        store.gateway_path.read_text(encoding="utf-8")
    )


def test_other_saves_keep_the_delegation_values(client, store):
    _save(client, {"delegation": {"max_seconds": 120}})
    saved = _save(client, {"classifier": {"choice": "rule"}})
    assert saved.status_code == 200, saved.get_json()
    data = json.loads(store.gateway_path.read_text(encoding="utf-8"))
    assert data["delegation"] == {"max_seconds": 120}
