"""Breaker details in the settings hub (#471): entry + exit as one pair, the details
fold (restart fields) and the read-only server-fixed values.

The defect this pins: the hub only edited the entry thresholds and refused an entry at
or above its (uneditable) exit, so a warning line of 0.6 or more could not be saved from
the UI at all. The exit is now part of the same group and saved with it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.config import stira_delivery
from stiracore.core import StiraConfig
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint


@pytest.fixture
def store(tmp_path):
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


def _save(client, changes, **extra):
    version = client.get("/api/config/hub").get_json()["version"]
    return client.post(
        "/api/config/hub", json={"changes": changes, "base_version": version, **extra}
    )


def _overrides(store) -> dict:
    return json.loads(store.agent_path.read_text(encoding="utf-8"))["stira_config_overrides"]


def _codes(response) -> dict:
    return {e["field"]: e["code"] for e in response.get_json()["errors"]}


# --- ① a warning line >= 0.6 is saved when its exit is raised with it ----------------


def test_warning_line_above_old_exit_saves_with_its_exit(client, store):
    response = _save(client, {"breaker": {"stability_warning": 0.7, "stability_warning_exit": 0.8}})
    assert response.status_code == 200, response.get_json()
    saved = _overrides(store)
    assert saved["stability_warning"] == 0.7 and saved["stability_warning_exit"] == 0.8
    values = response.get_json()["state"]["values"]["breaker"]
    assert values["stability_warning"] == 0.7 and values["stability_warning_exit"] == 0.8
    _gateway, agent = store.load()
    assert agent.stira_config_overrides["stability_warning_exit"] == 0.8


def test_entry_at_or_above_its_exit_is_still_refused(client, store):
    response = _save(client, {"breaker": {"stability_warning": 0.7}})  # exit stays 0.6
    assert response.status_code == 400
    errors = response.get_json()["errors"]
    assert _codes(response) == {"breaker.stability_warning": "must_be_below"}
    assert errors[0]["params"]["limit"] == 0.6
    assert not store.agent_path.exists()
    # The exit below its entry is tied to the exit field.
    lowered = _save(client, {"breaker": {"bc_exit_threshold": 0.4}})  # entry bc 0.5
    assert _codes(lowered) == {"breaker.bc_exit_threshold": "must_be_above"}


def test_exit_order_and_range_are_checked(client):
    crossed = _save(
        client, {"breaker": {"stability_critical_exit": 0.65, "stability_warning_exit": 0.65}}
    )
    assert _codes(crossed) == {"breaker.stability_critical_exit": "must_be_below_field"}
    out = _save(client, {"breaker": {"stability_warning_exit": 1.5}})
    assert _codes(out) == {"breaker.stability_warning_exit": "out_of_range"}
    text = _save(client, {"breaker": {"bc_exit_threshold": "0.8"}})
    assert _codes(text) == {"breaker.bc_exit_threshold": "not_number"}


# --- ② the danger floors stay; a low entry still needs the confirmation ----------------


def test_danger_floor_still_needs_the_confirmation(client, store):
    low = {
        "breaker": {
            "stability_warning": 0.25,
            "stability_critical": 0.2,
            "stability_warning_exit": 0.5,
        }
    }
    refused = _save(client, low)
    assert refused.status_code == 400
    assert refused.get_json()["needs_confirmation"] == ["breaker_low"]
    assert not store.agent_path.exists()
    confirmed = _save(client, low, acknowledged_dangers=["breaker_low"])
    assert confirmed.status_code == 200, confirmed.get_json()
    assert hub.DANGER_BREAKER_FLOORS == {
        "stability_warning": 0.3,
        "stability_critical": 0.1,
        "bc_threshold": 0.3,
    }


# --- ③ the next turn gets the whole set at once ----------------------------------------


def test_apply_breaker_thresholds_publishes_entries_and_exits_as_one_set():
    holder = SimpleNamespace(config=StiraConfig())
    before = holder.config
    values = {
        "stability_warning": 0.7,
        "stability_critical": 0.3,
        "bc_threshold": 0.5,
        "stability_warning_exit": 0.8,
        "stability_critical_exit": 0.45,
        "bc_exit_threshold": 0.7,
    }
    applied = hub.apply_breaker_thresholds(holder, values)
    assert applied == values
    assert holder.config is not before  # one assignment, never field by field
    assert before.stability_warning == 0.5 and before.stability_warning_exit == 0.6
    # The entry alone (old exit 0.6) is invalid: nothing is published.
    live = holder.config
    with pytest.raises(ValueError):
        hub.apply_breaker_thresholds(
            holder,
            {
                **values,
                "stability_warning_exit": 0.65,
                "stability_warning": 0.7,
                "bc_exit_threshold": 0.4,
            },
        )
    assert holder.config is live
    assert set(hub.BREAKER_SET) == set(stira_delivery.NEXT_TURN_FIELDS)


def test_breaker_apply_status_compares_the_whole_set(store):
    runtime = {"tools": None, "breaker": None, "classifier_choice": "auto", "error": None}
    client = _client(store, runtime_settings_provider=lambda: dict(runtime))
    state = client.get("/api/config/hub").get_json()
    entries_only = {name: state["values"]["breaker"][name] for name in hub.BREAKER_FIELDS}
    runtime.update(breaker=entries_only)
    assert client.get("/api/config/hub").get_json()["apply"]["breaker"]["status"] == "pending"
    runtime.update(breaker=dict(state["values"]["breaker"]))
    assert client.get("/api/config/hub").get_json()["apply"]["breaker"]["status"] == "applied"


# --- details: restart fields, rounded by the page, refused below the valves ------------


def test_details_are_edited_and_saved_for_the_next_start(store):
    runtime = {
        "tools": None,
        "breaker": None,
        "classifier_choice": "auto",
        "error": None,
        "engine": stira_delivery.plain_values(StiraConfig()),
    }
    client = _client(store, runtime_settings_provider=lambda: dict(runtime))
    state = client.get("/api/config/hub").get_json()
    defaults = StiraConfig()
    assert state["values"]["breaker_details"] == {
        name: getattr(defaults, name) for name in hub.DETAIL_FIELDS
    }
    assert state["apply"]["breaker_details"] == {"status": "applied", "applies": "restart"}
    response = _save(client, {"breaker_details": {"bc_window_size": 30, "hitl_timeout_sec": 600.0}})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["saved"] == ["breaker_details"]
    saved = _overrides(store)
    assert saved["bc_window_size"] == 30 and saved["hitl_timeout_sec"] == 600.0
    # Only what changed is written; the other details stay at their defaults.
    assert "critical_cooldown_turns" not in saved
    after = response.get_json()["state"]
    assert after["apply"]["breaker_details"]["status"] == "pending"
    # Source and applies come from the settings surface (#469), not a hub-only map.
    fields = after["surface"]["fields"]
    assert fields["stira.bc_window_size"]["source"] == "agent"
    assert fields["stira.critical_cooldown_turns"]["source"] == "default"
    assert fields["stira.bc_window_size"]["applies"] == "restart"
    assert "breaker_detail_fields" not in after


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("bc_window_size", 4, "out_of_range"),
        ("bc_window_size", 5.5, "not_integer"),
        ("bc_window_size", True, "not_integer"),
        ("critical_cooldown_turns", 2, "out_of_range"),
        ("sycophancy_window_size", 0, "out_of_range"),
        ("sycophancy_threshold", 1.2, "out_of_range"),
        ("decay_min_interval_sec", -1, "out_of_range"),
        ("hitl_timeout_sec", 0, "out_of_range"),
        ("hitl_timeout_sec", 86_401, "out_of_range"),
        ("hitl_max_edit_rounds", -1, "out_of_range"),
        ("decay_min_interval_sec", "60", "not_number"),
    ],
)
def test_details_below_the_valves_are_refused(client, store, field, value, code):
    response = _save(client, {"breaker_details": {field: value}})
    assert response.status_code == 400
    assert _codes(response) == {f"breaker_details.{field}": code}
    assert not store.agent_path.exists()


def test_detail_limits_are_published_for_the_page(client):
    rules = client.get("/api/config/hub").get_json()["constraints"]["breaker_details"]
    assert rules["bc_window_size"] == {"integer": True, "min": 5, "max": 200}
    assert rules["critical_cooldown_turns"]["min"] == 3
    assert rules["hitl_timeout_sec"]["max"] == stira_delivery.HITL_TIMEOUT_MAX_SEC
    pairs = client.get("/api/config/hub").get_json()["constraints"]["breaker_exits"]
    assert pairs["stability_warning"] == "stability_warning_exit"


# --- server-fixed values: shown, never editable -----------------------------------------


def test_server_fixed_values_are_shown_and_not_editable(client, store):
    state = client.get("/api/config/hub").get_json()
    fixed = {row["name"]: row for row in state["breaker_fixed"]}
    assert set(fixed) == {"protected_layers", "critical_action", "blocked_tool_names"}
    assert fixed["protected_layers"]["value"] == [3, 4, 5]
    assert fixed["protected_layers"]["reason"] == "layer_protection"
    assert fixed["critical_action"]["reason"] == "server_fixed"
    for name in fixed:
        refused = _save(client, {"breaker_details": {name: "x"}})
        assert refused.status_code == 400
        assert _codes(refused) == {f"breaker_details.{name}": "unknown_field"}


def test_fixed_values_show_what_the_running_engine_uses(store):
    engine = stira_delivery.plain_values(StiraConfig(critical_action="halt"))
    runtime = {
        "tools": None,
        "breaker": None,
        "classifier_choice": "auto",
        "error": None,
        "engine": engine,
    }
    client = _client(store, runtime_settings_provider=lambda: dict(runtime))
    fixed = {r["name"]: r for r in client.get("/api/config/hub").get_json()["breaker_fixed"]}
    assert fixed["critical_action"]["value"] == "halt"


# --- ④ the *_clamped flags stay -------------------------------------------------------


def test_clamped_flags_stay_and_a_details_save_writes_the_rounded_value(client, store):
    store.agent_path.write_text(
        json.dumps({"stira_config_overrides": {"bc_window_size": 3, "hitl_timeout_sec": 10**6}}),
        encoding="utf-8",
    )
    state = client.get("/api/config/hub").get_json()
    assert state["bc_window_clamped"] is True and state["hitl_timeout_clamped"] is True
    assert state["values"]["breaker_details"]["bc_window_size"] == 5
    assert state["values"]["breaker_details"]["hitl_timeout_sec"] == 86_400.0
    response = _save(client, {"breaker_details": {"critical_cooldown_turns": 4}})
    assert response.status_code == 200, response.get_json()
    body = response.get_json()["state"]
    assert body["bc_window_clamped"] is False and body["hitl_timeout_clamped"] is False
    assert _overrides(store)["bc_window_size"] == 5


# --- history and presets cover the new fields --------------------------------------------


def test_exit_and_detail_changes_roll_back(client, store):
    first = _save(
        client,
        {
            "breaker": {"stability_warning": 0.7, "stability_warning_exit": 0.8},
            "breaker_details": {"critical_cooldown_turns": 5},
        },
    )
    entry_id = first.get_json()["history_id"]
    version = client.get("/api/config/hub").get_json()["version"]
    back = client.post(f"/api/config/history/{entry_id}/rollback", json={"base_version": version})
    assert back.status_code == 200, back.get_json()
    values = back.get_json()["state"]["values"]
    assert values["breaker"]["stability_warning_exit"] == 0.6
    assert values["breaker"]["stability_warning"] == 0.5
    assert values["breaker_details"]["critical_cooldown_turns"] == 3


def test_presets_reset_the_exits_with_the_entries(client):
    saved = _save(client, {"breaker": {"stability_warning": 0.7, "stability_warning_exit": 0.8}})
    presets = {p["id"]: p for p in saved.get_json()["state"]["presets"]}
    defaults = StiraConfig()
    for preset in presets.values():
        assert preset["values"]["breaker"]["stability_warning_exit"] == (
            defaults.stability_warning_exit
        )
    assert {"field": "breaker.stability_warning_exit", "from": 0.8, "to": 0.6} in presets[
        "standard"
    ]["diff"]
    standard = presets["standard"]["values"]["breaker"]
    applied = _save(client, {"breaker": standard})
    assert applied.status_code == 200, applied.get_json()
