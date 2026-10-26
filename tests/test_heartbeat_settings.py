"""#472: the heartbeat on the settings page (switch + interval bounds, read at start-up).

One hub group ``heartbeat`` saved to ``gateway-config.json`` (``hb_enabled``,
``hb_interval_floor_sec``, ``hb_interval_ceil_sec``). ``STIRA_HEARTBEAT`` pins the switch
(names only). Field errors only on the fields a request moves, history with rollback, the
apply status measured against what the daemon was started with, and the daemon's read-only
status in the same card. A tick never calls a model, so turning it on needs no confirmation.
"""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.config import GatewayConfig
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint

MARKER = "x-heartbeat-marker-5310"  # not 1/true/yes/on: the switch is off, the value secret


@pytest.fixture(autouse=True)
def _no_heartbeat_env(monkeypatch):
    monkeypatch.delenv("STIRA_HEARTBEAT", raising=False)


def _store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )


@pytest.fixture
def store(tmp_path):
    return _store(tmp_path)


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


def _runtime(**heartbeat):
    running = {"enabled": False, "interval_floor_sec": 300.0, "interval_ceil_sec": 10800.0}
    running.update(heartbeat)
    return {
        "tools": None,
        "breaker": None,
        "classifier_choice": "auto",
        "error": None,
        "heartbeat": running,
    }


# --- defaults ---------------------------------------------------------------------------------


def test_defaults_off_5_minutes_to_3_hours_and_opening_writes_nothing(client, store):
    state = _state(client)
    expected = {"enabled": False, "interval_floor_sec": 300.0, "interval_ceil_sec": 10800.0}
    assert state["values"]["heartbeat"] == expected
    assert state["defaults"]["heartbeat"] == expected
    # The defaults are the GatewayConfig defaults (one source).
    defaults = GatewayConfig()
    assert hub.heartbeat_values(defaults) == expected
    assert state["constraints"]["heartbeat"] == {
        "interval_floor_sec": {"min": 60.0, "max": 86_400.0},
        "interval_ceil_sec": {"min": 60.0, "max": 86_400.0},
    }
    fields = state["surface"]["fields"]
    for item_id, env in (
        ("gateway.hb_enabled", ["STIRA_HEARTBEAT"]),
        ("gateway.hb_interval_floor_sec", []),
        ("gateway.hb_interval_ceil_sec", []),
    ):
        field = fields[item_id]
        assert (field["ui"], field["applies"], field["env"]) == ("yes", "restart", env), item_id
        assert (field["source"], field["pinned_by"]) == ("default", None), item_id
    assert set(hub.HEARTBEAT_CONFIG_FIELDS.values()) == {
        "gateway.hb_enabled",
        "gateway.hb_interval_floor_sec",
        "gateway.hb_interval_ceil_sec",
    }
    # No chat server: no running status (the page says it is shown while one runs).
    assert state["heartbeat"] == {"live": None}
    assert state["apply"]["heartbeat"] == {"status": "saved", "applies": "next_start"}
    assert "heartbeat_on" not in hub.dangers(state["values"])
    assert not store.gateway_path.exists()


# --- save -> reload ---------------------------------------------------------------------------


def test_turning_it_on_saves_without_a_confirmation_and_reloads(client, store):
    response = _save(
        client,
        {"heartbeat": {"enabled": True, "interval_floor_sec": 120, "interval_ceil_sec": 1800}},
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["saved"] == ["heartbeat"]
    data = _file(store)
    assert (data["hb_enabled"], data["hb_interval_floor_sec"], data["hb_interval_ceil_sec"]) == (
        True,
        120.0,
        1800.0,
    )
    assert body["state"]["values"]["heartbeat"] == {
        "enabled": True,
        "interval_floor_sec": 120.0,
        "interval_ceil_sec": 1800.0,
    }
    # No confirmation was asked: a tick never calls a model (no token cost).
    assert body["state"]["dangers"] == []
    assert GatewayConfig.load(store.gateway_path).hb_enabled is True


def test_an_unrelated_save_leaves_the_heartbeat_fields_untouched(client, store):
    # The board's pictures are on by default (ADR 0012 addendum 2026-10-24): turn them off.
    assert _save(client, {"board": {"svg": False}}).status_code == 200
    data = _file(store)
    assert (data["hb_enabled"], data["hb_interval_floor_sec"], data["hb_interval_ceil_sec"]) == (
        False,
        300.0,
        10800.0,
    )


@pytest.mark.parametrize(
    ("changes", "field", "code"),
    [
        ({"interval_floor_sec": 30}, "heartbeat.interval_floor_sec", "interval_out_of_range"),
        ({"interval_ceil_sec": 90_000}, "heartbeat.interval_ceil_sec", "interval_out_of_range"),
        (
            {"interval_floor_sec": 7200, "interval_ceil_sec": 3600},
            "heartbeat.interval_floor_sec",
            "floor_above_ceil",
        ),
        ({"interval_floor_sec": "5"}, "heartbeat.interval_floor_sec", "not_number"),
        ({"interval_ceil_sec": True}, "heartbeat.interval_ceil_sec", "not_number"),
        ({"interval_ceil_sec": float("nan")}, "heartbeat.interval_ceil_sec", "not_number"),
        ({"enabled": 1}, "heartbeat.enabled", "not_boolean"),
    ],
)
def test_bad_values_are_refused_per_field_and_not_saved(client, store, changes, field, code):
    response = client.post("/api/config/hub/validate", json={"changes": {"heartbeat": changes}})
    errors = response.get_json()["errors"]
    assert [(e["field"], e["code"]) for e in errors] == [(field, code)], errors
    if code == "interval_out_of_range":
        assert errors[0]["params"] == {"min": 60.0, "max": 86_400.0}
    if code == "floor_above_ceil":
        assert errors[0]["params"] == {"limit": 3600.0}
    if not any(isinstance(v, float) and math.isnan(v) for v in changes.values()):
        saved = _save(client, {"heartbeat": changes})
        assert saved.status_code == 400
        assert not store.gateway_path.exists()


def test_the_bounds_are_inclusive(client):
    ok = client.post(
        "/api/config/hub/validate",
        json={"changes": {"heartbeat": {"interval_floor_sec": 60, "interval_ceil_sec": 86_400}}},
    )
    assert ok.get_json()["errors"] == []
    same = client.post(
        "/api/config/hub/validate",
        json={"changes": {"heartbeat": {"interval_floor_sec": 600, "interval_ceil_sec": 600}}},
    )
    assert same.get_json()["errors"] == []


def test_a_hand_edited_value_outside_the_bounds_does_not_block_other_saves(client, store):
    """GatewayConfig accepts 0..any; the hub's bounds apply to a field that moves only."""
    GatewayConfig(
        path=store.gateway_path, hb_interval_floor_sec=30.0, hb_interval_ceil_sec=200_000.0
    ).save()
    state = _state(client)
    assert state["values"]["heartbeat"]["interval_floor_sec"] == 30.0
    # Another field of the same group moves: the kept ones are not re-checked.
    response = _save(client, {"heartbeat": {"enabled": True}})
    assert response.status_code == 200, response.get_json()
    data = _file(store)
    assert (data["hb_enabled"], data["hb_interval_floor_sec"], data["hb_interval_ceil_sec"]) == (
        True,
        30.0,
        200_000.0,
    )
    # Its rollback goes through too.
    entry = client.get("/api/config/history").get_json()["entries"][0]
    undo = client.post(
        f"/api/config/history/{entry['id']}/rollback",
        json={"base_version": _state(client)["version"]},
    )
    assert undo.status_code == 200, undo.get_json()
    assert _file(store)["hb_enabled"] is False
    # Moving the kept value itself is checked.
    moved = _save(client, {"heartbeat": {"interval_floor_sec": 45}})
    assert moved.status_code == 400
    assert moved.get_json()["errors"][0]["code"] == "interval_out_of_range"


def _write_inverted(store, floor=4000, ceil=3600, **extra):
    data = {"hb_interval_floor_sec": floor, "hb_interval_ceil_sec": ceil, **extra}
    store.gateway_path.write_text(
        json.dumps({k: v for k, v in data.items() if v is not _OMIT}), encoding="utf-8"
    )


_OMIT = object()
# (file, the bounds the page shows): both written, or one omitted (the model's default
# fills it in, 300 s / 10800 s, and the pair is still inverted).
INVERTED = [
    ({"floor": 4000, "ceil": 3600}, (4000.0, 3600.0)),
    ({"floor": 20000, "ceil": _OMIT}, (20000.0, 10800.0)),
    ({"floor": _OMIT, "ceil": 120}, (300.0, 120.0)),
]


@pytest.mark.parametrize(("pair", "shown"), INVERTED)
def test_a_saved_floor_above_ceil_does_not_take_the_settings_page_down(client, store, pair, shown):
    """#512: a hand-edited (or other-version) inverted pair used to make every settings
    route 503 through the shared loader, also when one bound was omitted and its default
    made the pair inverted. Now the load runs on the defaults (fail-closed: the heartbeat
    does not start), flags it, and the page shows the file's bounds with a note."""
    _write_inverted(store, **pair, hb_enabled=True)
    raw = store.gateway_path.read_text(encoding="utf-8")
    for route in (
        "/api/config",
        "/api/config/hub",
        "/api/config/models",
        "/api/config/providers",
        "/api/config/history",
    ):
        response = client.get(route)
        assert response.status_code == 200, (route, response.get_json())
    state = _state(client)
    assert state["hb_intervals_reset"] is True
    assert state["values"]["heartbeat"] == {
        "enabled": True,
        "interval_floor_sec": shown[0],
        "interval_ceil_sec": shown[1],
    }
    assert client.get("/api/config").get_json()["hb_intervals_reset"] is True
    loaded = store.load()[0]
    assert (loaded.hb_interval_floor_sec, loaded.hb_interval_ceil_sec) == (300.0, 10800.0)
    # Opening the page writes nothing.
    assert store.gateway_path.read_text(encoding="utf-8") == raw
    # The start-up refuses it (fail-closed).
    from stiracore.heartbeat import HeartbeatGateway, HeartbeatIntervalsError

    with pytest.raises(HeartbeatIntervalsError):
        HeartbeatGateway(SimpleNamespace(), loaded)
    # A direct construction (any save) still refuses the pair.
    with pytest.raises(ValueError, match="hb_interval_floor_sec must be <="):
        GatewayConfig(hb_interval_floor_sec=shown[0], hb_interval_ceil_sec=shown[1])


@pytest.mark.parametrize(
    "raw",
    [
        {"hb_interval_floor_sec": None},
        {"hb_interval_ceil_sec": "x"},
        {"hb_interval_floor_sec": True},
    ],
)
def test_a_null_or_a_wrong_type_is_not_replaced_by_the_default(client, store, raw):
    """Only an omitted bound takes the default; a written null / string / boolean is an
    invalid value, refused as before (503), never silently replaced."""
    store.gateway_path.write_text(json.dumps(raw), encoding="utf-8")
    response = client.get("/api/config/hub")
    assert response.status_code == 503
    assert "hb_interval" in response.get_json()["error"]


@pytest.mark.parametrize("seconds", [(600, 7200), (300, 10800)])
def test_saving_bounds_from_that_state_fixes_the_file_and_clears_the_note(client, store, seconds):
    """Entering bounds and saving is the only way out. (300, 10800) is "take the defaults":
    the page shows the file's bounds, so typing the defaults is a change it can save."""
    _write_inverted(store, hb_enabled=True)
    floor, ceil = seconds
    response = _save(
        client, {"heartbeat": {"interval_floor_sec": floor, "interval_ceil_sec": ceil}}
    )
    assert response.status_code == 200, response.get_json()
    state = response.get_json()["state"]
    assert state["hb_intervals_reset"] is False
    data = _file(store)
    assert (data["hb_interval_floor_sec"], data["hb_interval_ceil_sec"]) == (
        float(floor),
        float(ceil),
    )
    assert _state(client)["hb_intervals_reset"] is False


@pytest.mark.parametrize(("pair", "shown"), INVERTED)
def test_an_unrelated_save_in_that_state_keeps_the_file_bounds_and_the_refusal(
    client, store, pair, shown
):
    """Review r568 M-2: a save of anything else (another gateway group, the heartbeat
    switch alone, agent-config) leaves the file's bounds as they were (an omitted one stays
    omitted), so the flag and the start-up refusal stay until a person saves bounds."""
    _write_inverted(store, **pair, hb_enabled=True)
    kept = {k: v for k, v in _file(store).items() if k.startswith("hb_interval_")}
    for changes in (
        {"cache": {"enabled": True, "ttl": "5m"}},
        {"breaker": {"stability_warning": 0.55}},
        {"heartbeat": {"enabled": False}},
        {"heartbeat": {"enabled": True}},
    ):
        response = _save(client, changes)
        assert response.status_code == 200, (changes, response.get_json())
        assert response.get_json()["state"]["hb_intervals_reset"] is True, changes
        data = _file(store)
        assert {k: v for k, v in data.items() if k.startswith("hb_interval_")} == kept, changes
    data = _file(store)
    assert (data["prompt_cache"], data["hb_enabled"]) == (True, True)
    reloaded = store.load()[0]
    assert reloaded.hb_intervals_reset is True
    from stiracore.heartbeat import HeartbeatGateway, HeartbeatIntervalsError

    with pytest.raises(HeartbeatIntervalsError):
        HeartbeatGateway(SimpleNamespace(), reloaded)
    # The model page saves the gateway file too (another writer): the same.
    model = client.post(
        "/api/config/models",
        json={"provider": "bedrock", "model_id": "global.anthropic.claude-sonnet-5"},
    )
    assert model.status_code == 200, model.get_json()
    assert {k: v for k, v in _file(store).items() if k.startswith("hb_interval_")} == kept
    assert store.load()[0].hb_intervals_reset is True


def test_the_reset_warning_is_logged_once_without_the_values(store, caplog, monkeypatch):
    from stiracore.config import models

    monkeypatch.setattr(models, "_HB_RESET_WARNED", False)
    _write_inverted(store, floor=4321, ceil=1234)
    with caplog.at_level("WARNING", logger=models.logger.name):
        store.load()
        store.load()
    lines = [r.getMessage() for r in caplog.records if "hb_intervals_reset" in r.getMessage()]
    assert len(lines) == 1
    assert "4321" not in lines[0] and "1234" not in lines[0]
    caplog.clear()
    # A valid file never warns.
    monkeypatch.setattr(models, "_HB_RESET_WARNED", False)
    _write_inverted(store, floor=300, ceil=3600)
    with caplog.at_level("WARNING", logger=models.logger.name):
        assert store.load()[0].hb_intervals_reset is False
    assert not [r for r in caplog.records if "hb_intervals_reset" in r.getMessage()]


def test_saving_an_inverted_pair_is_still_refused(client, store):
    """Save-time validation is unchanged: moving a bound past the other one is a 400."""
    assert _save(client, {"heartbeat": {"enabled": True}}).status_code == 200
    for change in (
        {"interval_floor_sec": 20000},
        {"interval_ceil_sec": 120},
        {"interval_floor_sec": 7200, "interval_ceil_sec": 3600},
    ):
        moved = _save(client, {"heartbeat": change})
        assert moved.status_code == 400, change
        assert moved.get_json()["errors"][0]["code"] == "floor_above_ceil"
    assert _file(store)["hb_interval_floor_sec"] == 300.0


def test_the_heartbeat_does_not_start_with_a_reset_pair():
    """Fail-closed at start-up (#512): the bounds the person saved are unusable, so the tick
    does not run on bounds they did not choose; the reason is a class name."""
    from stiracore.heartbeat import HeartbeatGateway, HeartbeatIntervalsError

    config = GatewayConfig(hb_enabled=True)
    config.hb_intervals_reset = True
    with pytest.raises(HeartbeatIntervalsError):
        HeartbeatGateway(SimpleNamespace(), config)


# --- the environment variable -----------------------------------------------------------------


def test_stira_heartbeat_pins_the_switch_names_only_and_intervals_stay_editable(
    client, store, monkeypatch
):
    monkeypatch.setenv("STIRA_HEARTBEAT", MARKER)
    state = _state(client)
    assert state["surface"]["fields"]["gateway.hb_enabled"]["pinned_by"] == "STIRA_HEARTBEAT"
    assert state["surface"]["fields"]["gateway.hb_interval_floor_sec"]["pinned_by"] is None
    assert MARKER not in json.dumps(state)
    refused = _save(client, {"heartbeat": {"enabled": True}})
    assert refused.status_code == 400
    assert refused.get_json()["errors"] == [
        {"field": "heartbeat.enabled", "code": "env_pinned", "params": {"env": "STIRA_HEARTBEAT"}}
    ]
    assert not store.gateway_path.exists()
    ok = _save(client, {"heartbeat": {"interval_ceil_sec": 3600}})
    assert ok.status_code == 200, ok.get_json()
    assert _file(store)["hb_interval_ceil_sec"] == 3600.0
    assert MARKER not in store.history_path.read_text(encoding="utf-8")


def test_an_empty_variable_does_not_pin(client, monkeypatch):
    monkeypatch.setenv("STIRA_HEARTBEAT", "  ")
    assert _state(client)["surface"]["fields"]["gateway.hb_enabled"]["pinned_by"] is None
    assert _save(client, {"heartbeat": {"enabled": True}}).status_code == 200


@pytest.mark.parametrize(
    ("env", "saved", "expected"),
    [
        (None, True, True),
        ("1", False, True),
        ("on", False, True),
        ("0", True, False),
        ("", True, True),
        (MARKER, True, False),
    ],
)
def test_the_server_start_and_the_page_share_one_env_rule(monkeypatch, env, saved, expected):
    from dashboard import stira_server

    if env is not None:
        monkeypatch.setenv("STIRA_HEARTBEAT", env)
    gateway = GatewayConfig(hb_enabled=saved)
    assert stira_server._heartbeat_enabled(gateway) is expected
    assert hub.effective_heartbeat_enabled(gateway) is expected
    pinned = hub.heartbeat_env_pins()
    assert pinned == ({"heartbeat.enabled": "STIRA_HEARTBEAT"} if (env or "").strip() else {})


# --- history and rollback ---------------------------------------------------------------------


def test_changes_are_recorded_and_can_be_rolled_back(client, store):
    assert _save(client, {"heartbeat": {"enabled": True}}).status_code == 200
    assert _save(client, {"heartbeat": {"interval_floor_sec": 600}}).status_code == 200
    entries = client.get("/api/config/history").get_json()["entries"]
    assert entries[1]["changes"] == [{"field": "heartbeat.enabled", "from": False, "to": True}]
    assert entries[0]["changes"] == [
        {"field": "heartbeat.interval_floor_sec", "from": 300.0, "to": 600}
    ]
    undo = client.post(
        f"/api/config/history/{entries[1]['id']}/rollback",
        json={"base_version": _state(client)["version"]},
    )
    assert undo.status_code == 200, undo.get_json()
    data = _file(store)
    assert data["hb_enabled"] is False  # undone
    assert data["hb_interval_floor_sec"] == 600.0  # the later change stays


def test_rollback_of_the_switch_while_pinned_is_refused_with_the_reason(client, store, monkeypatch):
    assert _save(client, {"heartbeat": {"enabled": True}}).status_code == 200
    monkeypatch.setenv("STIRA_HEARTBEAT", "1")
    entry = client.get("/api/config/history").get_json()["entries"][0]
    undo = client.post(
        f"/api/config/history/{entry['id']}/rollback",
        json={"base_version": _state(client)["version"]},
    )
    assert undo.status_code == 400
    assert undo.get_json()["errors"][0]["code"] == "env_pinned"
    assert _file(store)["hb_enabled"] is True


def test_reset_to_defaults_turns_it_off_again(client, store):
    assert (
        _save(client, {"heartbeat": {"enabled": True, "interval_ceil_sec": 600}}).status_code == 200
    )
    state = _state(client)
    response = client.post(
        "/api/config/hub",
        json={
            "changes": {"heartbeat": state["defaults"]["heartbeat"]},
            "base_version": state["version"],
            "source": "defaults",
        },
    )
    assert response.status_code == 200, response.get_json()
    data = _file(store)
    assert (data["hb_enabled"], data["hb_interval_ceil_sec"]) == (False, 10800.0)


# --- apply status and the running status ------------------------------------------------------


def test_apply_status_is_restart_and_compares_what_the_daemon_runs_with(store):
    runtime = _runtime()
    client = _client(store, runtime_settings_provider=lambda: dict(runtime))
    assert _state(client)["apply"]["heartbeat"] == {
        "status": "applied",
        "applies": "restart",
        "running": {"enabled": False},
    }
    saved = _save(client, {"heartbeat": {"enabled": True, "interval_floor_sec": 120}})
    after = saved.get_json()["state"]["apply"]["heartbeat"]
    assert (after["status"], after["applies"]) == ("pending", "restart")
    # The restart: the daemon now runs with the saved values.
    runtime["heartbeat"] = {
        "enabled": True,
        "interval_floor_sec": 120.0,
        "interval_ceil_sec": 10800.0,
        "error": None,
    }
    assert _state(client)["apply"]["heartbeat"]["status"] == "applied"
    # Only one bound differs: still pending.
    runtime["heartbeat"]["interval_ceil_sec"] = 3600.0
    assert _state(client)["apply"]["heartbeat"]["status"] == "pending"


def test_a_pinned_switch_is_applied_when_the_env_value_runs(store, monkeypatch):
    GatewayConfig(path=store.gateway_path, hb_enabled=False).save()
    monkeypatch.setenv("STIRA_HEARTBEAT", "1")
    client = _client(store, runtime_settings_provider=lambda: _runtime(enabled=True))
    apply = _state(client)["apply"]["heartbeat"]
    assert apply == {"status": "applied", "applies": "restart", "running": {"enabled": True}}


def test_turned_on_but_not_started_is_failed_with_the_class_name(store):
    GatewayConfig(path=store.gateway_path, hb_enabled=True).save()
    runtime = _runtime(enabled=True, error="HeartbeatLockError")
    client = _client(store, runtime_settings_provider=lambda: runtime)
    apply = _state(client)["apply"]["heartbeat"]
    assert (apply["status"], apply["reason"]) == ("failed", "HeartbeatLockError")


def test_a_runtime_without_heartbeat_facts_is_only_saved(store):
    client = _client(store, runtime_settings_provider=lambda: {"tools": None, "breaker": None})
    assert _state(client)["apply"]["heartbeat"] == {"status": "saved", "applies": "restart"}


@pytest.mark.parametrize("error", [OSError("disk"), ValueError("broken"), RuntimeError("x")])
def test_an_unreadable_file_for_the_heartbeat_status_still_answers_200(store, error, monkeypatch):
    """#497 review minor: the heartbeat's own status failing costs only its badge: "saved",
    never a 500. Since #513 it uses the config the hub already read (no second read of the
    file), so the failure is raised where it works out the running switch."""
    client = _client(store, runtime_settings_provider=lambda: _runtime(enabled=False))
    healthy = _state(client)["apply"]
    assert healthy["heartbeat"]["status"] == "applied"

    def broken(_gateway, environ=None):
        raise error

    # Only the apply status asks for the effective switch; the values keep working.
    monkeypatch.setattr(hub, "effective_heartbeat_enabled", broken)
    response = client.get("/api/config/hub")
    assert response.status_code == 200
    apply = response.get_json()["apply"]
    assert apply.pop("heartbeat") == {"status": "saved", "applies": "restart"}
    # Every other group keeps its status.
    assert apply == {k: v for k, v in healthy.items() if k != "heartbeat"}


def test_the_running_status_is_read_only_and_plain(store):
    status = {
        "enabled": True,
        "state": "running",
        "running": True,
        "last_tick_at": 1_700_000_000.0,
        "next_tick_at": 1_700_000_900.0,
        "interval_sec": 900.0,
        "last_level": "normal",
        "tick_count": 3,
        "consecutive_failures": 0,
        "degraded": False,
        "last_error": "/home/someone/private/path: denied",  # not a class name: dropped
        "clock_resyncs": 0,  # not shown
        "extra": object(),  # unknown: not shown
    }
    client = _client(store, heartbeat_status_provider=lambda: status)
    live = _state(client)["heartbeat"]["live"]
    assert live == {
        "enabled": True,
        "running": True,
        "state": "running",
        "last_tick_at": 1_700_000_000.0,
        "next_tick_at": 1_700_000_900.0,
        "interval_sec": 900.0,
        "last_level": "normal",
        "tick_count": 3,
        "consecutive_failures": 0,
        "degraded": False,
        "last_error": None,
    }

    # A loop that ended reports "loop:<Class>" (heartbeat-daemon.md): kept, it is a name.
    ended = dict(status, running=False, last_error="loop:RuntimeError")
    live = _state(_client(store, heartbeat_status_provider=lambda: ended))["heartbeat"]["live"]
    assert live["last_error"] == "loop:RuntimeError"

    def broken():
        raise RuntimeError("boom")

    assert _state(_client(store, heartbeat_status_provider=broken))["heartbeat"]["live"] is None


def test_every_heartbeat_error_code_has_a_message_on_the_page():
    import inspect
    import re

    source = inspect.getsource(hub._validate_heartbeat)
    codes = set(re.findall(r'\breport\(\s*[^,]+,\s*"(\w+)"', source))
    assert codes == {
        "not_boolean",
        "not_number",
        "interval_out_of_range",
        "floor_above_ceil",
        "env_pinned",
    }, codes
    js = (
        Path(__file__).resolve().parents[1] / "dashboard" / "static" / "js" / "settings_hub.js"
    ).read_text(encoding="utf-8")
    block = js.split("const ERROR_TEXT = {", 1)[1].split("};", 1)[0]
    keys = set(re.findall(r"^\s*(\w+): \(", block, re.MULTILINE))
    assert codes <= keys, codes - keys


def test_a_tick_never_calls_a_model():
    """The reason no confirmation is asked: HeartbeatGateway.tick only touches the engine's
    state, breaker status and config (an engine double that fails on anything else)."""
    from stiracore.core import BreakLevel
    from stiracore.heartbeat import HeartbeatGateway

    state = Mock()
    state.stability = 0.7
    state.apply_decay.return_value = {}
    state.snapshot.return_value = {"stability": 0.7}
    engine = SimpleNamespace(
        state=state,
        state_lock=threading.Lock(),
        config=SimpleNamespace(decay_min_interval_sec=60.0),
        breaker_status=lambda: SimpleNamespace(level=BreakLevel.NORMAL),
    )
    result = HeartbeatGateway(engine, GatewayConfig(hb_enabled=True)).tick()
    assert result["level"] == "normal"
    called = {name for name, *_ in state.method_calls}
    assert called == {"apply_decay", "snapshot"}


# --- measured on the chat server ---------------------------------------------------------------


@pytest.fixture
def stira(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    data_dir = tmp_path / "data"
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    created = []
    store = _store(tmp_path)

    def make(**kwargs):
        app = stira_server.create_app(config_store=store, **kwargs)
        created.append(app)
        return app, store

    yield make
    for app in created:
        if app.extensions.get("heartbeat") is not None:
            app.extensions["heartbeat"].stop()


def test_stira_off_by_default_applied_and_status_off(stira):
    app, _ = stira()
    assert app.extensions["heartbeat"] is None
    state = _state(app.test_client())
    assert state["apply"]["heartbeat"] == {
        "status": "applied",
        "applies": "restart",
        "running": {"enabled": False},
    }
    assert state["heartbeat"]["live"] == {
        **dict.fromkeys(hub.HEARTBEAT_LIVE_FIELDS),
        "enabled": False,
        "running": False,
    }


def test_stira_a_tick_reaches_no_model_classifier_agent_tool_or_job(stira, monkeypatch):
    """#497 review: the whole tick on the chat server, not HeartbeatGateway.tick alone.

    stira runs with the heartbeat on. Every place that would cost tokens or act on its
    own is a tripwire that fails when reached (not a Mock that answers): the model's
    calls, a Strands Agent, the stimulus classifiers, a chat turn (pre_turn / post_turn),
    any @tool, and the memory harvest job. Then one tick runs through the daemon and one
    more through the same on_tick hook in the test's thread (so nothing is swallowed by
    the daemon's failure handling).
    """
    from strands.agent import agent as strands_agent
    from strands.types.tools import AgentTool

    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.core.engine import StiraEngine

    hits = []

    def tripwire(name):
        def fired(*_args, **_kwargs):
            hits.append(name)
            raise AssertionError(f"{name} reached")

        return fired

    model = Mock(side_effect=tripwire("model()"))
    for method in ("stream", "structured_output", "converse"):
        getattr(model, method).side_effect = tripwire(f"model.{method}")
    bedrock = Mock(return_value=model)
    monkeypatch.setattr(nxp_agent, "BedrockModel", bedrock)
    monkeypatch.setattr(nxp_agent, "Agent", tripwire("strands Agent (nxp_agent)"))
    monkeypatch.setattr(strands_agent.Agent, "__init__", tripwire("strands Agent"))
    # Real classifiers, with classify() as a tripwire (no Mock that answers "joy").
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda **_kw: stimulus_classifier.RuleBasedClassifier(),
    )
    for cls in (stimulus_classifier.RuleBasedClassifier, stimulus_classifier.SokudanClassifier):
        monkeypatch.setattr(cls, "classify", tripwire(f"{cls.__name__}.classify"))
    monkeypatch.setattr(nxp_agent.NXPAgent, "_get_classifier", tripwire("classifier lookup"))
    monkeypatch.setattr(nxp_agent.NXPAgent, "__call__", tripwire("chat turn"))
    monkeypatch.setattr(StiraEngine, "pre_turn", tripwire("engine.pre_turn"))
    monkeypatch.setattr(StiraEngine, "post_turn", tripwire("engine.post_turn"))
    # Every tool class (Strands' own and Stira's wrappers): a tool runs through stream().
    classes, seen = [AgentTool], set()
    while classes:
        cls = classes.pop()
        if cls in seen:
            continue
        seen.add(cls)
        classes.extend(cls.__subclasses__())
        for method in ("stream", "__call__"):
            if method in vars(cls):
                monkeypatch.setattr(cls, method, tripwire(f"{cls.__name__}.{method}"))
    monkeypatch.setattr(
        stira_server, "memory_harvest_action", lambda *a, **k: tripwire("memory harvest")
    )

    first, _ = stira()
    assert _save(first.test_client(), {"heartbeat": {"enabled": True}}).status_code == 200
    app, _ = stira(_tests_share_data_dir=True)
    daemon = app.extensions["heartbeat"]
    assert daemon is not None and daemon.running
    deadline = time.monotonic() + 5
    while daemon.status()["tick_count"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert hits == []
    status = daemon.status()
    assert (status["tick_count"], status["last_error"]) == (1, None), status
    # One more tick in this thread, through stira's own on_tick (save + status refresh).
    state_file = stira_server.DATA_DIR / "neural-state.json"
    state_file.unlink(missing_ok=True)
    daemon._on_tick(daemon.gateway.tick())
    assert state_file.exists()  # after_heartbeat_tick ran (persistence.save)
    assert hits == []
    assert bedrock.called  # the chat agent's model is the tripwired one
    assert model.mock_calls == []  # and it is not touched at all


def test_stira_saved_on_waits_for_the_restart_then_runs_with_the_saved_bounds(stira):
    app, store = stira()
    client = app.test_client()
    saved = _save(
        client,
        {"heartbeat": {"enabled": True, "interval_floor_sec": 120, "interval_ceil_sec": 600}},
    )
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["state"]["apply"]["heartbeat"]["status"] == "pending"
    # The running server did not start a heartbeat (no route, no live reload).
    assert app.extensions["heartbeat"] is None
    assert "gateway.hb_enabled" in _state(client)["surface"]["restart_pending"]
    # "Restart": a new server on the same files (tests only share the data directory).
    restarted, _ = stira(_tests_share_data_dir=True)
    daemon = restarted.extensions["heartbeat"]
    assert daemon is not None and daemon.running
    assert (
        daemon.gateway.gateway_config.hb_interval_floor_sec,
        daemon.gateway.gateway_config.hb_interval_ceil_sec,
    ) == (120.0, 600.0)
    deadline = time.monotonic() + 5
    while daemon.status()["tick_count"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    state = _state(restarted.test_client())
    assert state["apply"]["heartbeat"]["status"] == "applied", state["apply"]["heartbeat"]
    live = state["heartbeat"]["live"]
    assert (live["enabled"], live["running"], live["tick_count"]) == (True, True, 1)
    assert 120.0 <= live["interval_sec"] <= 600.0
    assert store.gateway_path.exists()


def test_stira_starts_with_an_inverted_pair_and_the_heartbeat_stays_off_with_a_reason(stira):
    """#512 on the chat server: the server starts, the settings answer 200, the heartbeat
    that was turned on does not run, and the card says why (a class name, no values)."""
    _first, store = stira()
    _write_inverted(store, hb_enabled=True)
    app, _ = stira(_tests_share_data_dir=True)
    assert app.extensions["heartbeat"] is None
    client = app.test_client()
    response = client.get("/api/config/hub")
    assert response.status_code == 200, response.get_json()
    state = response.get_json()
    assert state["hb_intervals_reset"] is True
    apply = state["apply"]["heartbeat"]
    assert (apply["status"], apply["reason"]) == ("failed", "HeartbeatIntervalsError")
    live = state["heartbeat"]["live"]
    assert (live["enabled"], live["running"], live["last_error"]) == (
        True,
        False,
        "HeartbeatIntervalsError",
    )
