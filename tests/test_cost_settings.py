"""#470: cost settings in the settings hub (output cap, prompt cache, context management).

Three hub groups saved to ``gateway-config.json``: ``output`` (``max_output_tokens``, applied
from the next turn), ``cache`` (``prompt_cache`` + ``prompt_cache_ttl``) and ``context``
(``context_management`` + its options), both read at start-up. Field-level errors, the
settings history with rollback, and the env switches shown as pinned (names only).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.agent.context_management import ContextManagementSettings
from stiracore.agent.model_switch import apply_model_selection
from stiracore.config import AgentConfig, GatewayConfig
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.providers import ModelSpec

ENV_NAMES = ("STIRA_PROMPT_CACHE", "STIRA_PROMPT_CACHE_TTL", "STIRA_CONTEXT_MANAGEMENT")


@pytest.fixture(autouse=True)
def _no_cost_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


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


# --- defaults ---------------------------------------------------------------------------------


def test_defaults_are_automatic_cap_and_both_switches_off(client, store):
    state = _state(client)
    defaults = ContextManagementSettings().as_dict()
    assert state["values"]["output"] == {"max_output_tokens": None}
    assert state["values"]["cache"] == {"enabled": False, "ttl": "5m"}
    assert state["values"]["context"] == defaults
    for group in ("output", "cache", "context"):
        assert state["defaults"][group] == state["values"][group]
    # Where each value comes from / when it applies: the settings surface (#469), on the page
    # (``ui: yes``); the temporary ``cost.fields`` adapter is gone.
    assert set(state["cost"]) == {"model", "context_profile", "task_models"}
    # docs/task-models.md, read-only: by default every task uses the chat model.
    rows = state["cost"]["task_models"]["next_start"]
    assert {row["task"] for row in rows} == {"tool_summary", "context_summary"}
    assert all(row["inherit"] and row["error"] is None for row in rows)
    # #368: read-only; the default profile changes nothing.
    profile = state["cost"]["context_profile"]["next_start"]
    assert profile["name"] == "default" and profile["send_allowed_tools_only"] is False
    fields = state["surface"]["fields"]
    expected = {
        "gateway.max_output_tokens": ("next_turn", []),
        "gateway.prompt_cache": ("restart", ["STIRA_PROMPT_CACHE"]),
        "gateway.prompt_cache_ttl": ("restart", ["STIRA_PROMPT_CACHE_TTL"]),
        "gateway.context_management": ("restart", ["STIRA_CONTEXT_MANAGEMENT"]),
        "gateway.context_management_options": ("restart", []),
    }
    for item_id, (applies, env) in expected.items():
        field = fields[item_id]
        assert (field["ui"], field["applies"], field["env"]) == ("yes", applies, env), item_id
        assert (field["source"], field["pinned_by"]) == ("default", None), item_id
    # Every hub cost field is stored as one of them.
    assert set(hub.COST_CONFIG_FIELDS.values()) == set(expected)
    assert set(hub.COST_CONFIG_FIELDS) == {
        "output.max_output_tokens",
        "cache.enabled",
        "cache.ttl",
        *(f"context.{name}" for name in defaults),
    }
    # Opening the page writes nothing.
    assert not store.gateway_path.exists()


def test_model_info_gives_the_automatic_cap_and_caching_support():
    claude = hub.cost_model_info("bedrock", "global.anthropic.claude-sonnet-5-5")
    # 32000 for the answer + the 32000 thinking reserve (#416), under the 128K limit.
    assert claude["auto_max_output_tokens"] == 64_000
    assert claude["model_max_output"] == 128_000
    assert (claude["auto_max_output_source"], claude["thinking_reserve"]) == ("model", 32_000)
    assert claude["prompt_cache_supported"] is True
    unknown = hub.cost_model_info("openai", "some-local-model")
    assert unknown["auto_max_output_tokens"] == 8192
    assert unknown["model_max_output"] is None
    assert unknown["auto_max_output_source"] == "provider_default"
    assert unknown["thinking_reserve"] is None
    assert unknown["prompt_cache_supported"] is False
    plain = hub.cost_model_info("openai", "gpt-4.1")  # no thinking: no reserve
    assert (plain["auto_max_output_tokens"], plain["thinking_reserve"]) == (32_000, None)


# --- save -> reload ---------------------------------------------------------------------------


def test_save_reload_writes_gateway_config_only_changed_options(client, store):
    response = _save(
        client,
        {
            "output": {"max_output_tokens": 64_000},
            "cache": {"enabled": True, "ttl": "1h"},
            "context": {"enabled": True, "preview_tokens": 500, "context_window_tokens": 200_000},
        },
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["saved"] == ["output", "cache", "context"]
    data = _file(store)
    assert data["max_output_tokens"] == 64_000
    assert (data["prompt_cache"], data["prompt_cache_ttl"]) == (True, "1h")
    assert data["context_management"] is True
    # Only the options that differ from the code defaults are written.
    assert data["context_management_options"] == {
        "preview_tokens": 500,
        "context_window_tokens": 200_000,
    }
    again = _state(_client(store))
    assert again["values"]["output"] == {"max_output_tokens": 64_000}
    assert again["values"]["cache"] == {"enabled": True, "ttl": "1h"}
    assert again["values"]["context"]["preview_tokens"] == 500
    fields = again["surface"]["fields"]
    assert fields["gateway.context_management_options"]["source"] == "gateway"
    assert fields["gateway.max_output_tokens"]["source"] == "gateway"
    assert fields["gateway.prompt_cache_ttl"]["source"] == "gateway"
    # A server started with the new file has nothing waiting; the one that saved it has
    # the start-up switches waiting for a restart (the banner counts them), not the cap.
    assert again["surface"]["restart_pending_groups"] == []
    running = _state(client)["surface"]
    assert running["fields"]["gateway.prompt_cache"]["pending_restart"] is True
    assert running["fields"]["gateway.max_output_tokens"]["pending_restart"] is False
    assert {"prompt_cache", "context_management"} <= set(running["restart_pending_groups"])
    # Back to automatic: null in the file again.
    back = _save(client, {"output": {"max_output_tokens": None}})
    assert back.status_code == 200, back.get_json()
    assert _file(store)["max_output_tokens"] is None


def test_unrelated_save_leaves_the_cost_fields_untouched(client, store):
    GatewayConfig(
        path=store.gateway_path,
        max_output_tokens=12_345,
        context_management_options={"retention_hours": 48},
    ).save()
    response = _save(client, {"board": {"svg": False}, "breaker": {"stability_warning": 0.55}})
    assert response.status_code == 200, response.get_json()
    data = _file(store)
    assert data["max_output_tokens"] == 12_345
    assert data["context_management_options"] == {"retention_hours": 48}


def test_an_out_of_range_saved_value_does_not_block_an_unrelated_save(client, store):
    """The runtime accepts any positive cap in the file (GatewayConfig has no upper bound);
    the hub's sanity bound applies to a value being changed, not to one already saved."""
    GatewayConfig(path=store.gateway_path, max_output_tokens=2_000_000).save()
    assert _state(client)["values"]["output"] == {"max_output_tokens": 2_000_000}
    validated = client.post(
        "/api/config/hub/validate", json={"changes": {"breaker": {"stability_warning": 0.55}}}
    ).get_json()
    assert validated["ok"] is True, validated
    response = _save(client, {"breaker": {"stability_warning": 0.55}})
    assert response.status_code == 200, response.get_json()
    # Another cost group with the cap sent back unchanged (the page sends whole groups).
    response = _save(
        client,
        {"output": {"max_output_tokens": 2_000_000}, "cache": {"enabled": True, "ttl": "1h"}},
    )
    assert response.status_code == 200, response.get_json()
    data = _file(store)
    assert data["max_output_tokens"] == 2_000_000
    assert (data["prompt_cache"], data["prompt_cache_ttl"]) == (True, "1h")
    # A history rollback of the unrelated change is not blocked either.
    entry = client.get("/api/config/history").get_json()["entries"][0]
    undo = client.post(
        f"/api/config/history/{entry['id']}/rollback",
        json={"base_version": _state(client)["version"]},
    )
    assert undo.status_code == 200, undo.get_json()
    # Changing the cap itself is still checked.
    refused = _save(client, {"output": {"max_output_tokens": 3_000_000}})
    assert refused.status_code == 400
    assert [e["code"] for e in refused.get_json()["errors"]] == ["out_of_range"]


@pytest.mark.parametrize(
    "raw",
    [
        {"max_output_tokens": 64000.0},
        {"context_management_options": {"preview_tokens": 500.0}},
        {"context_management_options": {"offload_threshold_tokens": 2000.0}},
        {"context_management_options": {"preserve_recent": 4.0}},
        {"context_management_options": {"context_window_tokens": 200000.0}},
        {"context_management_options": {"retrieval_max_tokens": 10000.0}},
    ],
)
def test_a_whole_number_float_in_the_file_never_reaches_the_shape_check(store, raw):
    """#492 review round 2: the hub checks the shape of every cost value (``not_integer``),
    also of one sent back unchanged. That cannot block an unrelated save, because the runtime
    refuses the same file first: ``GatewayConfig`` / ``ContextManagementSettings`` take only a
    real ``int`` for these fields, so a hand-edited ``64000.0`` makes the whole config
    unavailable (503) before the hub reads a value. If the runtime ever accepts such a float,
    this fails: then the hub has to normalize it on read (``output_values`` / ``context_values``).
    """
    store.gateway_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        GatewayConfig.load(store.gateway_path)
    assert _client(store).get("/api/config/hub").status_code == 503


def test_a_whole_number_in_a_number_field_does_not_block_a_save_or_a_rollback(client, store):
    """The other direction is accepted by the runtime (``"retention_hours": 24``): it passes the
    hub's shape check unchanged, so a breaker save and its rollback go through; a float sent
    for an integer field is still refused."""
    store.gateway_path.write_text(
        json.dumps({"context_management_options": {"retention_hours": 24, "summary_ratio": 0}}),
        encoding="utf-8",
    )
    assert _client(store).get("/api/config/hub").status_code == 503  # summary_ratio 0 refused
    store.gateway_path.write_text(
        json.dumps({"context_management_options": {"retention_hours": 24}}), encoding="utf-8"
    )
    assert _state(client)["values"]["context"]["retention_hours"] == 24
    response = _save(client, {"breaker": {"stability_warning": 0.55}})
    assert response.status_code == 200, response.get_json()
    entry = client.get("/api/config/history").get_json()["entries"][0]
    undo = client.post(
        f"/api/config/history/{entry['id']}/rollback",
        json={"base_version": _state(client)["version"]},
    )
    assert undo.status_code == 200, undo.get_json()
    assert _file(store)["context_management_options"] == {"retention_hours": 24}
    refused = _save(client, {"output": {"max_output_tokens": 2000000.0}})
    assert refused.status_code == 400
    assert [(e["field"], e["code"]) for e in refused.get_json()["errors"]] == [
        ("output.max_output_tokens", "not_integer")
    ]


# --- out-of-range values are refused per field --------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "field", "code"),
    [
        ({"output": {"max_output_tokens": 0}}, "output.max_output_tokens", "out_of_range"),
        ({"output": {"max_output_tokens": 1.5}}, "output.max_output_tokens", "not_integer"),
        ({"output": {"max_output_tokens": "8192"}}, "output.max_output_tokens", "not_integer"),
        ({"output": {"max_output_tokens": True}}, "output.max_output_tokens", "not_integer"),
        (
            {"output": {"max_output_tokens": hub.MAX_OUTPUT_TOKENS_LIMIT + 1}},
            "output.max_output_tokens",
            "out_of_range",
        ),
        ({"cache": {"ttl": "10m"}}, "cache.ttl", "unknown_choice"),
        ({"cache": {"enabled": "yes"}}, "cache.enabled", "not_boolean"),
        ({"context": {"enabled": 1}}, "context.enabled", "not_boolean"),
        ({"context": {"preview_tokens": 1500}}, "context.preview_tokens", "must_be_below_other"),
        ({"context": {"preview_tokens": -1}}, "context.preview_tokens", "too_small"),
        ({"context": {"summary_ratio": 0.9}}, "context.summary_ratio", "out_of_range"),
        ({"context": {"summary_ratio": 0.05}}, "context.summary_ratio", "out_of_range"),
        (
            {"context": {"compression_threshold": 0}},
            "context.compression_threshold",
            "out_of_range_open",
        ),
        ({"context": {"retention_hours": 0}}, "context.retention_hours", "out_of_range_open"),
        ({"context": {"context_window_tokens": 0}}, "context.context_window_tokens", "too_small"),
        (
            {"context": {"offload_threshold_tokens": 2.5}},
            "context.offload_threshold_tokens",
            "not_integer",
        ),
        (
            {"context": {"retrieval_max_tokens": None}},
            "context.retrieval_max_tokens",
            "not_integer",
        ),
        ({"context": {"preserve_recent": float("nan")}}, "context.preserve_recent", "not_integer"),
        # A JSON true/false is not a number (bool is an int subclass in Python).
        ({"context": {"summary_ratio": True}}, "context.summary_ratio", "not_number"),
        (
            {"context": {"compression_threshold": True}},
            "context.compression_threshold",
            "not_number",
        ),
        ({"context": {"retention_hours": True}}, "context.retention_hours", "not_number"),
        ({"context": {"preserve_recent": True}}, "context.preserve_recent", "not_integer"),
        (
            {"context": {"context_window_tokens": True}},
            "context.context_window_tokens",
            "not_integer",
        ),
    ],
)
def test_out_of_range_values_are_refused_per_field_and_not_saved(
    client, store, changes, field, code
):
    before = store.gateway_path.read_bytes() if store.gateway_path.exists() else None
    response = _save(client, changes)
    assert response.status_code == 400
    errors = response.get_json()["errors"]
    assert {"field": field, "code": code} in [
        {"field": e["field"], "code": e["code"]} for e in errors
    ], errors
    after = store.gateway_path.read_bytes() if store.gateway_path.exists() else None
    assert after == before


def _context_errors(context) -> list[str]:
    errors: list[str] = []
    values = {"output": {}, "cache": {"enabled": False, "ttl": "5m"}, "context": context}
    hub._validate_cost(values, None, lambda field, code, **params: errors.append(field), {})
    return errors


def test_every_hub_rule_is_also_a_runtime_rule():
    """Values the hub accepts at the edge are accepted by ContextManagementSettings, and the
    first value past it is refused by both (the hub is not looser than the runtime)."""
    edges = {
        "offload_threshold_tokens": (1, 0),
        "preview_tokens": (0, -1),
        "compression_threshold": (1.0, 0.0),
        "summary_ratio": (0.8, 0.81),
        "preserve_recent": (0, -1),
        "retention_hours": (24.0 * 365, 24.0 * 365 + 1),
        "context_window_tokens": (1, 0),
        "retrieval_max_tokens": (1, 0),
    }
    base = ContextManagementSettings().as_dict()
    for name, (ok, bad) in edges.items():
        good = {**base, name: ok}
        if name == "offload_threshold_tokens":
            good["preview_tokens"] = 0
        errors = _context_errors(good)
        assert errors == [], (name, errors)
        ContextManagementSettings(**good)
        wrong = {**good, name: bad}
        errors = _context_errors(wrong)
        assert f"context.{name}" in errors, name
        with pytest.raises(ValueError):
            ContextManagementSettings(**wrong)


def test_validate_endpoint_reports_field_errors(client):
    response = client.post(
        "/api/config/hub/validate", json={"changes": {"context": {"summary_ratio": 2}}}
    )
    body = response.get_json()
    assert body["ok"] is False
    assert body["errors"][0]["field"] == "context.summary_ratio"
    assert body["errors"][0]["params"] == {"min": 0.1, "max": 0.8}


# --- env switches -----------------------------------------------------------------------------


def test_env_pinned_fields_show_the_name_only_and_refuse_a_change(client, store, monkeypatch):
    monkeypatch.setenv("STIRA_PROMPT_CACHE", "on")
    monkeypatch.setenv("STIRA_CONTEXT_MANAGEMENT", "0")
    state = _state(client)
    fields = state["surface"]["fields"]
    assert fields["gateway.prompt_cache"]["pinned_by"] == "STIRA_PROMPT_CACHE"
    assert fields["gateway.prompt_cache"]["source"] == "env"
    assert fields["gateway.context_management"]["pinned_by"] == "STIRA_CONTEXT_MANAGEMENT"
    assert fields["gateway.prompt_cache_ttl"]["pinned_by"] is None  # TTL variable not set
    # The location card lists the variable (name, the fields, when it applies).
    pinned = {entry["name"]: entry for entry in state["surface"]["pinned"]}
    assert pinned["STIRA_PROMPT_CACHE"]["fields"] == ["gateway.prompt_cache"]
    assert pinned["STIRA_PROMPT_CACHE"]["applies"] == "restart"
    # The env values are never returned (names only).
    assert '"on"' not in json.dumps(state["cost"]) + json.dumps(state["surface"])
    refused = _save(client, {"cache": {"enabled": True, "ttl": "5m"}})
    assert refused.status_code == 400
    assert refused.get_json()["errors"] == [
        {"field": "cache.enabled", "code": "env_pinned", "params": {"env": "STIRA_PROMPT_CACHE"}}
    ]
    # The rest of the group (and the context options) can still be saved.
    ok = _save(
        client, {"cache": {"enabled": False, "ttl": "1h"}, "context": {"preserve_recent": 6}}
    )
    assert ok.status_code == 200, ok.get_json()
    assert _file(store)["prompt_cache_ttl"] == "1h"


def test_empty_env_value_does_not_pin(client, monkeypatch):
    monkeypatch.setenv("STIRA_PROMPT_CACHE", "  ")
    assert _state(client)["surface"]["fields"]["gateway.prompt_cache"]["source"] == "default"
    assert _save(client, {"cache": {"enabled": True, "ttl": "5m"}}).status_code == 200


@pytest.mark.parametrize(
    ("env", "saved", "expected"),
    [
        ({}, (False, "5m"), (False, "5m")),
        ({}, (True, "1h"), (True, "1h")),
        ({"STIRA_PROMPT_CACHE": "yes"}, (False, "5m"), (True, "5m")),
        ({"STIRA_PROMPT_CACHE": "off"}, (True, "5m"), (False, "5m")),
        ({"STIRA_PROMPT_CACHE": ""}, (True, "5m"), (True, "5m")),
        ({"STIRA_PROMPT_CACHE_TTL": "1h"}, (True, "5m"), (True, "1h")),
    ],
)
def test_the_server_and_the_page_share_one_env_rule(monkeypatch, env, saved, expected):
    from dashboard import stira_server

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    gateway = GatewayConfig(prompt_cache=saved[0], prompt_cache_ttl=saved[1])
    settings = stira_server._prompt_cache_settings(gateway)
    assert (settings.enabled, settings.ttl) == expected == hub.effective_prompt_cache(gateway)


# --- history and rollback ---------------------------------------------------------------------


def test_cost_changes_are_recorded_and_can_be_rolled_back(client, store):
    first = _save(client, {"output": {"max_output_tokens": 48_000}})
    assert first.status_code == 200
    second = _save(client, {"context": {"enabled": True, "retention_hours": 72}})
    assert second.status_code == 200
    entries = client.get("/api/config/history").get_json()["entries"]
    assert entries[1]["changes"] == [
        {"field": "output.max_output_tokens", "from": None, "to": 48_000}
    ]
    assert {c["field"] for c in entries[0]["changes"]} == {
        "context.enabled",
        "context.retention_hours",
    }
    undo = client.post(
        f"/api/config/history/{entries[1]['id']}/rollback",
        json={"base_version": _state(client)["version"]},
    )
    assert undo.status_code == 200, undo.get_json()
    data = _file(store)
    assert data["max_output_tokens"] is None  # back to automatic
    assert data["context_management"] is True  # the later change stays
    assert data["context_management_options"] == {"retention_hours": 72}


# --- apply status -----------------------------------------------------------------------------


def test_apply_status_output_next_turn_cache_and_context_at_restart(store):
    runtime = {
        "tools": None,
        "breaker": None,
        "classifier_choice": "auto",
        "error": None,
        "max_output_tokens": 64_000,
        "prompt_cache": {"enabled": False, "ttl": "5m"},
        "context_management": ContextManagementSettings().as_dict(),
    }
    client = _client(store, runtime_settings_provider=lambda: dict(runtime))
    apply = _state(client)["apply"]
    assert apply["output"] == {"status": "applied", "applies": "next_turn", "effective": 64_000}
    assert apply["cache"] == {
        "status": "applied",
        "applies": "restart",
        "running": {"enabled": False, "ttl": "5m"},
    }
    assert apply["context"] == {
        "status": "applied",
        "applies": "restart",
        "running": {"enabled": False},
    }
    saved = _save(
        client,
        {
            "output": {"max_output_tokens": 50_000},
            "cache": {"enabled": True, "ttl": "5m"},
            "context": {"preserve_recent": 8},
        },
    ).get_json()["state"]["apply"]
    assert saved["output"]["status"] == "pending"
    assert (saved["cache"]["status"], saved["cache"]["applies"]) == ("pending", "restart")
    assert (saved["context"]["status"], saved["context"]["applies"]) == ("pending", "restart")
    runtime["max_output_tokens"] = 50_000
    assert _state(client)["apply"]["output"]["status"] == "applied"


def test_a_pinned_switch_is_applied_when_the_env_value_runs(store, monkeypatch):
    """STIRA_PROMPT_CACHE=1 with "off" saved: the agent runs with the cache on; that is
    "applied" (not a pending change that never comes), and the page shows the running value."""
    monkeypatch.setenv("STIRA_PROMPT_CACHE", "1")
    runtime = {
        "tools": None,
        "breaker": None,
        "classifier_choice": "auto",
        "error": None,
        "max_output_tokens": 32_000,
        "prompt_cache": {"enabled": True, "ttl": "5m"},
        "context_management": ContextManagementSettings().as_dict(),
    }
    client = _client(store, runtime_settings_provider=lambda: dict(runtime))
    state = _state(client)
    assert state["values"]["cache"]["enabled"] is False  # the saved value is kept
    assert state["apply"]["cache"]["status"] == "applied"
    assert state["apply"]["cache"]["running"]["enabled"] is True


def _runtime(**overrides):
    return {
        "tools": None,
        "breaker": None,
        "classifier_choice": "auto",
        "error": None,
        "max_output_tokens": 32_000,
        "prompt_cache": {"enabled": False, "ttl": "5m"},
        "context_management": ContextManagementSettings().as_dict(),
        **overrides,
    }


def test_context_apply_ignores_a_window_the_runtime_resolves(store):
    """An empty (automatic) context window may be resolved by the runtime (model settings,
    capability table): that is not a pending change. A set window is still compared."""
    resolved = {**ContextManagementSettings().as_dict(), "context_window_tokens": 200_000}
    runtime = _runtime(context_management=resolved)
    client = _client(store, runtime_settings_provider=lambda: dict(runtime))
    assert _state(client)["apply"]["context"]["status"] == "applied"
    saved = _save(client, {"context": {"context_window_tokens": 150_000}})
    assert saved.get_json()["state"]["apply"]["context"]["status"] == "pending"
    runtime["context_management"] = {**resolved, "context_window_tokens": 150_000}
    assert _state(client)["apply"]["context"]["status"] == "applied"
    # Any other option still counts.
    runtime["context_management"] = {**runtime["context_management"], "preserve_recent": 9}
    assert _state(client)["apply"]["context"]["status"] == "pending"


def test_cache_and_context_status_without_a_running_cap(store):
    """An agent without ``max_output_tokens`` (f1ddf6a): the cap is "saved", but the start-up
    switches are still compared with what runs."""
    runtime = _runtime(max_output_tokens=None)
    client = _client(store, runtime_settings_provider=lambda: dict(runtime))
    apply = _state(client)["apply"]
    assert apply["output"] == {"status": "saved", "applies": "next_turn"}
    assert apply["cache"]["status"] == "applied"
    assert apply["context"]["status"] == "applied"


def test_env_values_never_appear_in_the_hub_state(store, monkeypatch):
    """Names only: the env values are not in the hub state, its apply status or the history,
    even when a runtime reports an unchecked value (only 5m / 1h / booleans are passed on)."""
    markers = {name: f"x-{name.lower()}-marker" for name in ENV_NAMES}
    for name, value in markers.items():
        monkeypatch.setenv(name, value)
    runtime = _runtime(
        prompt_cache={
            "enabled": markers["STIRA_PROMPT_CACHE"],
            "ttl": markers["STIRA_PROMPT_CACHE_TTL"],
        },
        context_management={
            **ContextManagementSettings().as_dict(),
            "enabled": markers["STIRA_CONTEXT_MANAGEMENT"],
        },
    )
    client = _client(store, runtime_settings_provider=lambda: dict(runtime))
    state = _state(client)
    assert state["apply"]["cache"]["running"] == {"enabled": None, "ttl": None}
    assert state["apply"]["context"]["running"] == {"enabled": None}
    assert _save(client, {"context": {"preserve_recent": 6}}).status_code == 200
    dumped = json.dumps(
        [state, _state(client), client.get("/api/config/history").get_json()]
    ).lower()
    for value in markers.values():
        assert value not in dumped
    for name in ENV_NAMES:
        assert name.lower() in dumped  # the names are shown


def test_rollback_of_a_pinned_field_is_refused_with_the_reason(client, store, monkeypatch):
    first = _save(client, {"cache": {"enabled": True, "ttl": "5m"}})
    assert first.status_code == 200
    monkeypatch.setenv("STIRA_PROMPT_CACHE", "1")
    entry = client.get("/api/config/history").get_json()["entries"][0]
    undo = client.post(
        f"/api/config/history/{entry['id']}/rollback",
        json={"base_version": _state(client)["version"]},
    )
    assert undo.status_code == 400
    assert undo.get_json()["errors"] == [
        {"field": "cache.enabled", "code": "env_pinned", "params": {"env": "STIRA_PROMPT_CACHE"}}
    ]
    assert _file(store)["prompt_cache"] is True


def test_every_cost_error_code_has_a_message_on_the_page():
    """Each code ``_validate_cost`` can return has its own text in ``ERROR_TEXT`` (an unknown
    code falls back to the generic "cannot be saved")."""
    import inspect
    import re

    source = inspect.getsource(hub._validate_cost)
    codes = set(re.findall(r'\b(?:report|bound)\(\s*[^,]+,\s*"(\w+)"', source))
    assert len(codes) >= 9 and {"env_pinned", "out_of_range_open", "not_number"} <= codes, codes
    js = (
        Path(__file__).resolve().parents[1] / "dashboard" / "static" / "js" / "settings_hub.js"
    ).read_text(encoding="utf-8")
    block = js.split("const ERROR_TEXT = {", 1)[1].split("};", 1)[0]
    keys = set(re.findall(r"^\s*(\w+): \(", block, re.MULTILINE))
    assert codes <= keys, codes - keys


def test_apply_status_without_a_running_agent_is_saved(client):
    apply = _state(client)["apply"]
    for group in ("output", "cache", "context"):
        assert apply[group] == {"status": "saved", "applies": "next_start"}


# --- when the values really apply (measured against the chat server code) ------------------


def test_region_and_cap_changes_rebuild_the_model_on_the_next_turn(monkeypatch):
    """``sync_model_selection`` runs before every turn with the file's resolved selection:
    a new ``region`` (Bedrock spec) or ``max_output_tokens`` rebuilds the model then."""
    built = Mock(return_value=object())
    monkeypatch.setattr("stiracore.agent.model_switch.create_model", built)
    model_id = "global.anthropic.claude-sonnet-5-5"
    agent = SimpleNamespace(
        model_spec=ModelSpec.bedrock(model_id, region_name="ap-northeast-1"),
        max_output_tokens=32_000,
    )
    same = {"model_id": model_id, "region_name": "ap-northeast-1"}
    assert apply_model_selection(agent, same, max_output_tokens=32_000) is False
    moved = {"model_id": model_id, "region_name": "us-east-1"}
    assert apply_model_selection(agent, moved, max_output_tokens=32_000) is True
    assert agent.region_name == "us-east-1"


def test_stira_applies_a_saved_cap_from_the_next_turn(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import model_switch, nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.core import NeuralState
    from tests.provider_fakes import FakeModel, text_turn

    data_dir = tmp_path / "stira-data"
    data_dir.mkdir()
    NeuralState(stability=0.9).save(data_dir / "neural-state.json")
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "GATEWAY_CONFIG", GatewayConfig(path=tmp_path / "gw.json"))
    monkeypatch.setattr(stira_server, "MODEL_ID", GatewayConfig().model_id)
    monkeypatch.setattr(stira_server, "REGION", os.environ.get("AWS_REGION", "ap-northeast-1"))
    model = FakeModel([text_turn("one"), text_turn("two"), text_turn("three")])
    monkeypatch.setattr(nxp_agent, "BedrockModel", lambda **kw: model)
    caps = []

    def rebuilt(spec, max_tokens):
        caps.append(max_tokens)
        return model

    monkeypatch.setattr(model_switch, "create_model", rebuilt)
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    store = _store(tmp_path)
    app = stira_server.create_app(config_store=store)
    client = app.test_client()
    assert "one" in client.post("/api/chat", json={"message": "a"}).text
    state = _state(client)
    auto = state["cost"]["model"]["auto_max_output_tokens"]
    assert state["apply"]["output"] == {
        "status": "applied",
        "applies": "next_turn",
        "effective": auto,
    }

    saved = _save(client, {"output": {"max_output_tokens": 20_000}})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["state"]["apply"]["output"]["status"] == "pending"
    assert "two" in client.post("/api/chat", json={"message": "b"}).text
    assert caps[-1] == 20_000
    after = _state(client)["apply"]["output"]
    assert after == {"status": "applied", "applies": "next_turn", "effective": 20_000}
    # The start-up switches report what the running agent has (off, as saved).
    assert _state(client)["apply"]["cache"]["status"] == "applied"
    assert _state(client)["apply"]["context"]["status"] == "applied"


def test_stira_start_up_switches_are_applied_and_match_the_gateway_status(tmp_path, monkeypatch):
    """Measured on the chat server: context management on with an automatic window, a model
    that keeps the history on the server (context "unsupported" at run time), and the cache
    lifetime from STIRA_PROMPT_CACHE_TTL. Neither group stays "pending", and the running cache
    values on the settings page are the ones /api/gateway/status already shows."""
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.core import NeuralState
    from tests.provider_fakes import FakeModel, text_turn

    monkeypatch.setenv("STIRA_PROMPT_CACHE_TTL", "1h")
    data_dir = tmp_path / "stira-data"
    data_dir.mkdir()
    NeuralState(stability=0.9).save(data_dir / "neural-state.json")
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "GATEWAY_CONFIG", GatewayConfig(path=tmp_path / "gw.json"))
    monkeypatch.setattr(stira_server, "MODEL_ID", GatewayConfig().model_id)
    monkeypatch.setattr(stira_server, "REGION", os.environ.get("AWS_REGION", "ap-northeast-1"))

    class StatefulModel(FakeModel):
        stateful = True  # keeps the history on the server (context management "unsupported")

    model = StatefulModel([text_turn("one"), text_turn("two")])
    monkeypatch.setattr(nxp_agent, "BedrockModel", lambda **kw: model)
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    store = _store(tmp_path)
    GatewayConfig(
        path=store.gateway_path,
        prompt_cache=True,
        context_management=True,
        context_management_options={"preserve_recent": 6},
    ).save()
    client = stira_server.create_app(config_store=store).test_client()
    assert "one" in client.post("/api/chat", json={"message": "a"}).text
    status = client.get("/api/gateway/status").get_json()
    assert status["context_management"]["mode"] == "unsupported"
    apply = _state(client)["apply"]
    assert apply["context"] == {
        "status": "applied",
        "applies": "restart",
        "running": {"enabled": True},
    }
    assert apply["cache"]["status"] == "applied"
    gateway_cache = status["prompt_cache"]
    assert (
        apply["cache"]["running"]
        == {
            "enabled": gateway_cache["enabled"],
            "ttl": gateway_cache["ttl"],
        }
        == {"enabled": True, "ttl": "1h"}
    )


def test_the_cut_off_note_links_to_the_output_limit():
    """The chat's "cut off at the output limit" note links to the field that raises it."""
    root = Path(__file__).resolve().parents[1] / "dashboard"
    chat = (root / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    template = (root / "templates" / "settings.html").read_text(encoding="utf-8")
    assert 'link.href = "/settings#sec-cost-output";' in chat
    assert 'id="sec-cost-output"' in template
    for lang in ("ja", "en"):
        catalog = json.loads((root / "static" / "i18n" / f"catalog.{lang}.json").read_text("utf-8"))
        assert catalog["chat.max_tokens_settings_link"]


# --- one rule with the settings surface (#469) and with #471 ----------------------------------


@pytest.mark.parametrize("raw", [None, "", "  ", "0", "off", "1", "on", "x-marker"])
@pytest.mark.parametrize(
    ("field", "name"),
    [
        ("cache.enabled", "STIRA_PROMPT_CACHE"),
        ("cache.ttl", "STIRA_PROMPT_CACHE_TTL"),
        ("context.enabled", "STIRA_CONTEXT_MANAGEMENT"),
    ],
)
def test_the_refusal_and_the_page_use_the_surface_pin(field, name, raw):
    """The server refuses (``env_pinned``) exactly the inputs the page disables: both read
    ``settings_surface`` (the manifest's ``overrides`` / ``when``), no second list."""
    from stiracore.config import settings_surface

    env = {} if raw is None else {name: raw}
    item_id = hub.COST_CONFIG_FIELDS[field]
    described = settings_surface.describe(GatewayConfig(), AgentConfig(), env)[item_id]["pinned_by"]
    assert hub.cost_env_pins(env).get(field) == described
    assert described == (name if raw is not None and raw.strip() else None)


def test_a_broken_manifest_pins_nothing(monkeypatch):
    """With no surface facts the page disables nothing, so the server refuses nothing."""
    from stiracore.config import settings_surface

    def broken(*_args, **_kwargs):
        raise settings_surface.ManifestError("broken")

    monkeypatch.setattr(settings_surface, "pinned_by", broken)
    assert hub.cost_env_pins({"STIRA_PROMPT_CACHE": "1"}) == {}


def test_a_manifest_that_breaks_its_schema_does_not_block_a_save(
    client, store, monkeypatch, tmp_path
):
    """#492 review round 2: ``cost_env_pins`` catches ``(OSError, ValueError)``; the manifest's
    own error is a ``ValueError`` (``ManifestError(ValueError)``), so a real broken manifest
    (not a stub) leaves the save open, as #490 has it for the page (``surface.available``)."""
    from stiracore.config import settings_surface

    assert issubclass(settings_surface.ManifestError, ValueError)
    broken = tmp_path / "settings_surface.json"
    monkeypatch.setattr(settings_surface, "MANIFEST_PATH", broken)
    monkeypatch.setenv("STIRA_PROMPT_CACHE", "1")
    try:
        for text in ('{"items": [{"id": "gateway.prompt_cache"}]}', "{not json"):
            broken.write_text(text, encoding="utf-8")
            settings_surface._cached.cache_clear()
            with pytest.raises(ValueError):
                settings_surface.load_manifest()
            assert hub.cost_env_pins() == {}
            enabled = not _state(client)["values"]["cache"]["enabled"]
            response = _save(client, {"cache": {"enabled": enabled, "ttl": "5m"}})
            assert response.status_code == 200, response.get_json()
            assert _file(store)["prompt_cache"] is enabled
    finally:
        settings_surface._cached.cache_clear()  # the real manifest is read again afterwards


def test_one_moved_rule_for_every_section():
    """``field_moved`` decides "only a field being changed is checked against a bound" for the
    cost groups (#470 review M2) and the breaker details (#471 review M-1)."""
    import inspect

    saved = {"output": {"max_output_tokens": 2_000_000}, "cache": {"enabled": False}}
    same = {"output": {"max_output_tokens": 2_000_000}, "cache": {"enabled": False}}
    assert not hub.field_moved(same, saved, "output.max_output_tokens", "cache.enabled")
    assert hub.field_moved(same, None, "output.max_output_tokens")  # nothing saved yet
    assert hub.field_moved({"cache": {"enabled": 0}}, saved, "cache.enabled")  # False vs 0
    assert not hub.field_moved(
        {"output": {"max_output_tokens": 2e6}}, saved, "output.max_output_tokens"
    )
    assert hub.field_moved({"output": {"max_output_tokens": 3}}, saved, "output.max_output_tokens")
    assert hub.field_moved(
        {"breaker_details": {"x": 1}}, {"breaker_details": {}}, "breaker_details.x"
    )
    for function in (hub._validate_cost, hub.validate_values):
        assert "field_moved(" in inspect.getsource(function), function.__name__
    # The earlier per-section helper is gone.
    assert not hasattr(hub, "_cost_changed")
    assert not hasattr(hub, "cost_field_meta")


def test_a_float_for_a_saved_integer_cap_is_still_refused(client, store):
    """Shape is always checked: 2000000.0 equals the saved cap but is not an integer."""
    GatewayConfig(path=store.gateway_path, max_output_tokens=2_000_000).save()
    response = _save(client, {"output": {"max_output_tokens": 2_000_000.0}})
    assert response.status_code == 400
    assert response.get_json()["errors"][0]["code"] == "not_integer"
    assert _file(store)["max_output_tokens"] == 2_000_000


def test_one_hub_state_reads_the_config_files_once(store):
    """#513: the values and the apply status (cost, heartbeat) come from one read of the
    settings files; the apply status never loads them a second time."""
    runtime = _runtime(heartbeat={"enabled": False, "error": None})
    client = _client(store, runtime_settings_provider=lambda: dict(runtime))
    loaded = store.load
    calls = []

    def load():
        calls.append(1)
        return loaded()

    store.load = load
    state = _state(client)
    assert len(calls) == 1
    assert state["apply"]["cache"]["status"] == "applied"
    calls.clear()
    response = _save(client, {"output": {"max_output_tokens": 50_000}})
    assert response.status_code == 200
    # _save reads the hub state once for its base version; the save then reads the files
    # once before it writes and once for the state it answers with.
    assert len(calls) == 3
    assert response.get_json()["state"]["apply"]["output"]["status"] == "pending"
