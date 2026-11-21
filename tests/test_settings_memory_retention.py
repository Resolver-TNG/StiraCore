"""#474: ``memory_retention`` on the settings hub (values + a read-only dry run), and the
JSON Schema the page builds its form from (#195).

The values go through the hub's validation, history, rollback and conflict check like any
other group. The dry run returns counts only; ``--apply`` stays on the command line.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.config.models import AgentConfig
from stiracore.control.memory_layout import DataLayout
from stiracore.control.memory_retention import RETENTION_KEYS, RetentionPolicy
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

BODY = "Retention-Body-Canary-7731"
DRY_RUN = "/api/config/memory-retention/dry-run"


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


def _state(client):
    return client.get("/api/config/hub").get_json()


def _save(client, changes, **extra):
    return client.post(
        "/api/config/hub",
        json={"changes": changes, "base_version": _state(client)["version"], **extra},
    )


# --- values and schema -------------------------------------------------------------


def test_every_retention_setting_is_a_hub_value_and_a_schema_property(store):
    state = _state(_client(store))
    keys = {f"{s}__{name}" for s, names in RETENTION_KEYS.items() for name in names}
    values = state["values"]["memory_retention"]
    schema = state["schemas"]["memory_retention"]
    assert set(values) == keys == set(schema["properties"])
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    # Unset = no limit (null); the defaults of the CLI are what is shown.
    assert values["episodic__max_age_days"] is None
    assert values["episodic__keep_unharvested"] is True
    assert values["episodic__honour_expires_at"] is False
    assert values["memory_plans__max_age_days"] == RetentionPolicy().get(
        "memory_plans", "max_age_days"
    )
    assert state["defaults"]["memory_retention"] == values
    # Read by the CLI when it runs: saved is all the server can say.
    assert state["apply"]["memory_retention"] == {"status": "saved", "applies": "on_run"}


def test_schema_states_the_ranges_the_server_enforces(store):
    schema = hub.retention_schema()["properties"]
    days = schema["episodic__max_age_days"]
    assert days["type"] == ["integer", "null"]
    assert (days["minimum"], days["maximum"]) == (1, 1_000_000)
    assert schema["archive__warn_bytes"]["maximum"] == 10**15
    assert schema["episodic__keep_unharvested"] == {
        "type": "boolean",
        "default": True,
        "x-section": "episodic",
    }
    # A setting with a default cannot be "no limit": the CLI would use the default anyway.
    assert schema["memory_plans__max_age_days"]["type"] == "integer"
    client = _client(store)
    for key, rule in schema.items():
        if rule["type"] == "boolean":
            bad = [1, "true", None]
        else:
            bad = [rule["minimum"] - 1, rule["maximum"] + 1, 1.5, "7", True]
            if rule["type"] == "integer":
                bad.append(None)
        for value in bad:
            response = _save(client, {"memory_retention": {key: value}})
            assert response.status_code == 400, (key, value)
            (error,) = response.get_json()["errors"]
            assert error["field"] == f"memory_retention.{key}", (key, value)
            assert error["code"] in {"not_integer", "not_boolean", "out_of_range"}
    assert not store.agent_path.exists()  # nothing was written, nothing rounded


def test_unknown_setting_is_refused(store):
    response = _save(_client(store), {"memory_retention": {"episodic__nope": 3}})
    assert response.status_code == 400
    assert response.get_json()["errors"] == [
        {"field": "memory_retention.episodic__nope", "code": "unknown_field", "params": {}}
    ]


# --- save, history, rollback, conflict ---------------------------------------------


def test_save_writes_the_agent_file_history_and_rolls_back(store):
    client = _client(store)
    response = _save(
        client,
        {
            "memory_retention": {
                "episodic__max_age_days": 30,
                "episodic__keep_unharvested": False,
                "archive__warn_bytes": 2_000_000_000,
            }
        },
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["saved"] == ["memory_retention"]
    assert AgentConfig.load(store.agent_path).memory_retention == {
        "episodic": {"max_age_days": 30, "keep_unharvested": False},
        "archive": {"warn_bytes": 2_000_000_000},
    }
    assert body["state"]["values"]["memory_retention"]["episodic__max_age_days"] == 30
    # No confirmation: a limit removes nothing by itself (only ``--apply`` acts).
    assert body["state"]["dangers"] == _state(client)["dangers"]

    (entry,) = client.get("/api/config/history").get_json()["entries"]
    assert entry["id"] == body["history_id"]
    assert {change["field"] for change in entry["changes"]} == {
        "memory_retention.episodic__max_age_days",
        "memory_retention.episodic__keep_unharvested",
        "memory_retention.archive__warn_bytes",
    }

    rolled = client.post(
        f"/api/config/history/{entry['id']}/rollback",
        json={"base_version": _state(client)["version"]},
    )
    assert rolled.status_code == 200, rolled.get_json()
    assert AgentConfig.load(store.agent_path).memory_retention == {}
    # The rollback is a new history entry of its own.
    entries = client.get("/api/config/history").get_json()["entries"]
    assert [e["source"] for e in entries] == [f"rollback:{entry['id']}", "manual"]


def test_back_to_no_limit_removes_the_key_and_keeps_other_agent_settings(store):
    AgentConfig(
        path=store.agent_path,
        write_roots=["@workspace", "/tmp"],
        memory_retention={"turns": {"max_age_days": 14}, "episodic": {"max_entries": 500}},
    ).save()
    client = _client(store)
    assert _state(client)["values"]["memory_retention"]["turns__max_age_days"] == 14
    response = _save(client, {"memory_retention": {"turns__max_age_days": None}})
    assert response.status_code == 200, response.get_json()
    agent = AgentConfig.load(store.agent_path)
    assert agent.memory_retention == {"episodic": {"max_entries": 500}}
    assert agent.write_roots == ["@workspace", "/tmp"]


def test_a_save_from_another_tab_is_a_conflict_not_an_overwrite(store):
    client = _client(store)
    stale = _state(client)["version"]
    first = _save(client, {"memory_retention": {"turns__max_age_days": 14}})
    assert first.status_code == 200
    second = client.post(
        "/api/config/hub",
        json={"changes": {"memory_retention": {"turns__max_age_days": 99}}, "base_version": stale},
    )
    assert second.status_code == 409
    body = second.get_json()
    assert body["error_code"] == "conflict"
    assert body["state"]["values"]["memory_retention"]["turns__max_age_days"] == 14
    assert AgentConfig.load(store.agent_path).memory_retention == {"turns": {"max_age_days": 14}}


# --- dry run: counts only -------------------------------------------------------------


def _data_dir(tmp_path, entries=3):
    data = tmp_path / "data"
    memory = MemoryStore(base_dir=data / "memory", agent_id="stira")
    for index in range(entries):
        memory.write(MemoryLayer.EPISODIC, f"canary-key-{index}", f"{BODY} {index}")
    return data


def test_dry_run_returns_counts_only(store, tmp_path):
    data = _data_dir(tmp_path)
    AgentConfig(
        path=store.agent_path,
        memory_retention={"episodic": {"max_entries": 1, "keep_unharvested": False}},
    ).save()
    client = _client(store, retention_layout_provider=lambda: DataLayout.at(data))
    response = client.get(DRY_RUN)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.get_json()
    assert body["available"] is True
    assert body["apply"] == "cli"
    assert body["policy_configured"] is True
    assert body["will_remove"]["episodic"] == 2
    assert set(body["will_remove"]) == set(hub.RETENTION_COUNT_KEYS)
    assert all(type(count) is int for count in body["will_remove"].values())
    assert body["not_inspected"] == 0
    text = response.get_data(as_text=True)
    for leak in (BODY, "canary-key", str(tmp_path), "on_apply"):
        assert leak not in text
    assert set(body) == {
        "available",
        "apply",
        "at",
        "policy_configured",
        "will_remove",
        "not_inspected",
    }
    # Read only: every entry is still there.
    assert len(list((data / "memory").rglob("canary-key-*"))) == 3


def test_dry_run_without_limits_removes_nothing(store, tmp_path):
    data = _data_dir(tmp_path)
    client = _client(store, retention_layout_provider=lambda: DataLayout.at(data))
    body = client.get(DRY_RUN).get_json()
    assert body["available"] is True and body["policy_configured"] is False
    assert body["will_remove"]["episodic"] == 0


def test_dry_run_is_unavailable_without_a_data_directory(store, tmp_path):
    assert _client(store).get(DRY_RUN).get_json() == {
        "available": False,
        "apply": "cli",
        "reason": "no_data_directory",
    }

    def missing():
        return DataLayout.at(tmp_path / "nowhere" / BODY)

    response = _client(store, retention_layout_provider=missing).get(DRY_RUN)
    assert response.get_json() == {"available": False, "apply": "cli", "reason": "report_failed"}
    assert BODY not in response.get_data(as_text=True)


def test_dry_run_view_drops_everything_but_counts():
    report = {
        "at": "2026-10-20T00:00:00+00:00",
        "policy_configured": True,
        "stores": {"episodic": {"oldest": BODY}},
        "will_remove": {"episodic": 4, "turn_events": True, "surprise": 9, "checkpoints": BODY},
        "skipped": [f"/secret/{BODY}", "other"],
    }
    view = hub.retention_dry_run(report)
    assert BODY not in json.dumps(view)
    assert view["will_remove"]["episodic"] == 4
    # A count that is not a plain number is unknown (null), never 0.
    assert view["will_remove"]["turn_events"] is None
    assert view["will_remove"]["checkpoints"] is None
    assert "surprise" not in view["will_remove"]
    assert view["not_inspected"] == 2
