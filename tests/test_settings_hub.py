"""Settings hub (#227): validated one-shot save, dangers, conflicts, and honest apply status."""

from __future__ import annotations

import json
import os
from unittest.mock import Mock

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.config import GatewayConfig
from stiracore.core import StiraConfig
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.tools import tool_registry
from stiracore.tools.tool_config import ToolConfig
from tests.provider_fakes import FakeModel, text_turn


def _store(tmp_path) -> ConfigStore:
    return ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )


@pytest.fixture
def store(tmp_path):
    # These tests start from an existing pre-#190 "allow everything" install, migrated
    # once (ON -> Always). A brand-new install starts from safe defaults (#190).
    store = _store(tmp_path)
    store.tool_path.write_text(
        json.dumps(
            {
                "mode": "trust_all",
                "enabled_tools": ToolConfig().as_dict()["enabled_tools"],
                "shell_allowlist": ToolConfig().as_dict()["shell_allowlist"],
            }
        ),
        encoding="utf-8",
    )
    return store


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


# --- Pure logic ----------------------------------------------------------------


def test_validate_mirrors_stira_config_rules():
    config = StiraConfig()
    values = hub.default_values()
    assert hub.validate_values(values, config, sokudan_available=False) == []

    values["breaker"]["stability_warning"] = 0.8  # >= stability_warning_exit (0.6)
    values["breaker"]["bc_threshold"] = "x"
    values["breaker"]["stability_critical"] = 1.5
    codes = {
        e["field"]: e["code"] for e in hub.validate_values(values, config, sokudan_available=False)
    }
    assert codes == {
        "breaker.stability_warning": "must_be_below",
        "breaker.bc_threshold": "not_number",
        "breaker.stability_critical": "out_of_range",
    }

    values = hub.default_values()
    values["breaker"].update(stability_warning=0.4, stability_critical=0.44)
    errors = hub.validate_values(values, config, sokudan_available=False)
    assert [(e["field"], e["code"]) for e in errors] == [
        ("breaker.stability_critical", "must_be_below_field")
    ]


def test_validate_does_not_reflag_already_saved_values():
    config = StiraConfig()
    current = hub.default_values()
    current["classifier"]["choice"] = "sokudan"
    current["tools"]["enabled_tools"].append("from_another_machine")
    assert hub.validate_values(current, config, sokudan_available=False, current=current) == []
    fresh = hub.default_values()
    fresh["classifier"]["choice"] = "sokudan"
    fresh["tools"]["enabled_tools"].append("from_another_machine")
    codes = [e["code"] for e in hub.validate_values(fresh, config, sokudan_available=False)]
    assert codes == ["not_installed", "unknown_tool"]


def test_presets_never_use_trust_all_or_exec_tools():
    base = hub.default_values()
    for preset_id in hub.PRESET_IDS:
        values = hub.preset_values(preset_id, base)
        assert values["tools"]["mode"] == "custom"
        assert not set(values["tools"]["enabled_tools"]) & tool_registry.EXEC_TOOL_NAMES
        assert hub.dangers(values) == []
        assert hub.validate_values(values, StiraConfig(), sokudan_available=False) == []
        assert hub.active_preset(values) == preset_id
    safe = set(hub.preset_values("safe", base)["tools"]["enabled_tools"])
    standard = set(hub.preset_values("standard", base)["tools"]["enabled_tools"])
    developer = set(hub.preset_values("developer", base)["tools"]["enabled_tools"])
    assert safe < standard < developer or (safe <= standard <= developer)
    assert not standard & hub.EXTERNAL_SEND_TOOLS
    assert hub.default_values()["tools"]["enabled_tools"] == sorted(standard)


def test_dangers_and_diff():
    current = hub.default_values()
    candidate = json.loads(json.dumps(current))
    candidate["tools"]["mode"] = "trust_all"
    candidate["breaker"]["stability_critical"] = 0.05
    assert hub.new_dangers(current, candidate) == ["trust_all", "breaker_low"]
    assert hub.new_dangers(candidate, candidate) == []
    diff = hub.diff_values(current, candidate)
    assert {"field": "tools.mode", "from": "custom", "to": "trust_all"} in diff


def test_filter_tools_only_removes_governed_names():
    tools = [Mock(tool_name=n) for n in ("file_read", "web_search", "memory_write")]
    config = ToolConfig(mode="custom", enabled_tools={"file_read"})
    kept = hub.filter_tools_by_config(
        tools, config, lambda t: t.tool_name, registry_names={"file_read", "web_search"}
    )
    assert [t.tool_name for t in kept] == ["file_read", "memory_write"]
    assert hub.filter_tools_by_config(
        tools, ToolConfig(mode="trust_all"), lambda t: t.tool_name, {"web_search"}
    ) == list(tools)


def test_apply_breaker_thresholds_validates_before_touching_live_config():
    from types import SimpleNamespace

    holder = SimpleNamespace(config=StiraConfig())  # the engine: anything with ``.config``
    before = holder.config
    applied = hub.apply_breaker_thresholds(
        holder, {"stability_warning": 0.55, "stability_critical": 0.4, "bc_threshold": 0.65}
    )
    # #471: the whole set (entries and their exits) is what the hub compares.
    assert applied == {
        "stability_warning": 0.55,
        "stability_critical": 0.4,
        "bc_threshold": 0.65,
        "stability_warning_exit": 0.6,
        "stability_critical_exit": 0.45,
        "bc_exit_threshold": 0.7,
    }
    assert holder.config.stability_warning == 0.55
    assert before.stability_warning == StiraConfig().stability_warning  # replaced, not edited
    live = holder.config
    with pytest.raises(ValueError):
        hub.apply_breaker_thresholds(holder, {"stability_critical": 0.9})
    assert holder.config is live and holder.config.stability_critical == 0.4


def test_startup_classifier_choice():
    calls = []

    def fake(**kw):
        calls.append(kw)
        return "classifier"

    assert hub.startup_classifier(GatewayConfig(), fake) is None  # auto: agent picks lazily
    assert hub.startup_classifier(GatewayConfig(jev_enabled=True), fake) == "classifier"
    assert calls == [{"prefer_sokudan": False}]
    hub.startup_classifier(GatewayConfig(jev_enabled=True, jev_classifier="sokudan"), fake)
    assert calls[-1] == {"prefer_sokudan": True}


# --- Routes ----------------------------------------------------------------------


def test_hub_state_shape_without_runtime(client):
    state = client.get("/api/config/hub").get_json()
    assert set(state["values"]) == {
        "classifier",
        "tools",
        "breaker",
        "breaker_details",
        "write",
        "read",
        "board",
        "output",
        "cache",
        "context",
        "heartbeat",
        "delegation",
        "agent_jobs",
    }
    # ADR 0021 S2: scheduled agent jobs are off by default, with the 50,000-token pool
    assert state["values"]["agent_jobs"] == {"enabled": False, "agent_daily_token_budget": 50000}
    assert state["values"]["classifier"]["choice"] == "auto"
    # #457: file tools read only the workspace by default
    assert state["values"]["read"] == state["defaults"]["read"] == {"roots": ["@workspace"]}
    # #453: static SVG on the board is on by default (ADR 0012 addendum 2026-10-24) and no
    # standing danger (only turning it back on is confirmed)
    assert state["values"]["board"] == {"svg": True}
    assert state["defaults"]["board"] == {"svg": True}
    assert "board_svg" not in state["dangers"]
    # #486 (ADR 0016, operator decision 2026-10-19): delegation is on by default
    defaults = {"enabled": True, "max_tool_calls": 40, "max_seconds": 600}
    assert state["values"]["delegation"] == {**defaults, "max_children_per_turn": 20}
    assert state["defaults"]["delegation"] == state["values"]["delegation"]
    # Fresh install: ToolConfig defaults to trust_all, flagged as a danger.
    assert state["dangers"] == ["trust_all"]
    assert state["defaults"]["tools"]["mode"] == "custom"
    # No runtime on this server: saved only, effective at the next start.
    for group in ("classifier", "tools", "breaker", "model"):
        assert state["apply"][group]["status"] == "saved"
        assert state["apply"][group]["applies"] == "next_start"
    assert state["constraints"]["breaker"]["stability_warning"]["below"] == 0.6
    assert state["summary"]["tools_total"] == len(state["tools"])


def test_save_persists_every_group(client, store):
    response = _save(
        client,
        {
            "classifier": {"choice": "rule"},
            "tools": {"mode": "custom", "enabled_tools": ["file_read"]},
            "breaker": {"stability_warning": 0.55},
        },
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["saved"] == ["classifier", "tools", "breaker"]
    gateway, agent = store.load()
    assert (gateway.jev_enabled, gateway.jev_classifier) == (True, "rule")
    assert store.load_tools().mode == "custom"
    assert store.load_tools().enabled_tools == {"file_read"}
    assert agent.stira_config_overrides["stability_warning"] == 0.55
    assert body["state"]["values"]["breaker"]["stability_warning"] == 0.55

    back = _save(client, {"classifier": {"choice": "auto"}})
    assert back.status_code == 200
    assert store.load()[0].jev_enabled is False


def test_invalid_values_are_tied_to_fields_and_not_saved(client, store):
    response = _save(client, {"breaker": {"stability_warning": 0.9, "stability_critical": "a"}})
    assert response.status_code == 400
    fields = {e["field"]: e["code"] for e in response.get_json()["errors"]}
    assert fields == {
        "breaker.stability_warning": "must_be_below",
        "breaker.stability_critical": "not_number",
    }
    assert not store.agent_path.exists()
    unknown = _save(client, {"breaker": {"nope": 1}, "other": {}})
    assert {e["field"] for e in unknown.get_json()["errors"]} == {"breaker.nope", "other"}


def test_validate_endpoint_reports_errors_diff_and_needed_confirmation(client):
    ok = client.post(
        "/api/config/hub/validate",
        json={"changes": {"breaker": {"stability_critical": 0.05}}},
    ).get_json()
    assert ok["ok"] is True
    assert ok["needs_confirmation"] == ["breaker_low"]
    assert ok["diff"] == [{"field": "breaker.stability_critical", "from": 0.3, "to": 0.05}]
    bad = client.post(
        "/api/config/hub/validate", json={"changes": {"classifier": {"choice": "x"}}}
    ).get_json()
    assert bad["ok"] is False
    assert bad["errors"][0]["field"] == "classifier.choice"


def test_new_dangers_need_acknowledgement(client, store):
    _save(client, {"tools": {"mode": "custom"}})
    refused = _save(client, {"tools": {"mode": "trust_all"}})
    assert refused.status_code == 400
    assert refused.get_json()["needs_confirmation"] == ["trust_all"]
    assert store.load_tools().mode == "custom"
    accepted = _save(client, {"tools": {"mode": "trust_all"}}, acknowledged_dangers=["trust_all"])
    assert accepted.status_code == 200
    assert store.load_tools().mode == "trust_all"
    # An existing danger does not block unrelated changes.
    other = _save(client, {"breaker": {"bc_threshold": 0.65}})
    assert other.status_code == 200


def test_stale_version_is_a_conflict_not_an_overwrite(client, store):
    stale = client.get("/api/config/hub").get_json()["version"]
    assert _save(client, {"breaker": {"stability_warning": 0.55}}).status_code == 200
    response = client.post(
        "/api/config/hub",
        json={"changes": {"breaker": {"stability_warning": 0.45}}, "base_version": stale},
    )
    assert response.status_code == 409
    assert response.get_json()["error_code"] == "conflict"
    assert response.get_json()["state"]["values"]["breaker"]["stability_warning"] == 0.55
    assert store.load()[1].stira_config_overrides["stability_warning"] == 0.55


def test_unknown_saved_tool_names_do_not_block_other_changes(client, store):
    ToolConfig(
        mode="custom", enabled_tools={"file_read", "rss_from_elsewhere"}, path=store.tool_path
    ).save()
    response = _save(client, {"breaker": {"stability_warning": 0.55}})
    assert response.status_code == 200, response.get_json()


def test_apply_status_follows_runtime_facts(store):
    runtime = {"tools": None, "breaker": None, "classifier_choice": "auto", "error": None}
    client = _client(
        store,
        runtime_settings_provider=lambda: dict(runtime),
        effective_classifier_provider=lambda: {"name": "RuleBasedClassifier"},
    )
    state = client.get("/api/config/hub").get_json()
    assert state["apply"]["tools"]["status"] == "pending"
    runtime.update(tools=state["values"]["tools"], breaker=state["values"]["breaker"])
    state = client.get("/api/config/hub").get_json()
    assert {g: state["apply"][g]["status"] for g in ("classifier", "tools", "breaker")} == {
        "classifier": "applied",
        "tools": "applied",
        "breaker": "applied",
    }
    assert state["apply"]["classifier"]["effective"] == "RuleBasedClassifier"
    saved = _save(client, {"classifier": {"choice": "rule"}, "breaker": {"bc_threshold": 0.65}})
    apply = saved.get_json()["state"]["apply"]
    assert (apply["classifier"]["status"], apply["classifier"]["applies"]) == ("pending", "restart")
    assert (apply["breaker"]["status"], apply["breaker"]["applies"]) == ("pending", "next_turn")
    runtime["error"] = "ValueError"
    failed = client.get("/api/config/hub").get_json()["apply"]["tools"]
    assert failed == {"status": "failed", "applies": "next_turn", "reason": "ValueError"}


def test_tools_page_redirects_to_the_hub(client):
    response = client.get("/tools")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/settings#sec-tools")


# --- chat server end to end -----------------------------------------------------------


def test_stira_applies_hub_tools_and_breaker_from_the_next_turn(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.core import NeuralState

    # web_search is the switch toggled below; it is offered only with an endpoint (#583,
    # never contacted here: the fake model calls no tool).
    monkeypatch.setenv("STIRA_WEB_SEARCH_ENDPOINT", "https://search.example/q")
    data_dir = tmp_path / "stira-data"
    data_dir.mkdir()
    NeuralState(stability=0.9).save(data_dir / "neural-state.json")
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "GATEWAY_CONFIG", GatewayConfig(path=tmp_path / "gw.json"))
    monkeypatch.setattr(stira_server, "MODEL_ID", GatewayConfig().model_id)
    monkeypatch.setattr(stira_server, "REGION", os.environ.get("AWS_REGION", "ap-northeast-1"))
    model = FakeModel([text_turn("one"), text_turn("two"), text_turn("three")])
    monkeypatch.setattr(nxp_agent, "BedrockModel", lambda **kw: model)
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    store = _store(tmp_path)
    app = stira_server.create_app(config_store=store)
    client = app.test_client()

    reply = client.post("/api/chat", json={"message": "a"})
    assert "one" in reply.text, reply.text
    first = set(model.requests[-1]["tool_names"])
    assert "web_search" in first or "web_search" not in tool_registry.WRITE_TOOLS
    state = client.get("/api/config/hub").get_json()
    assert state["apply"]["tools"]["status"] == "applied"

    enabled = sorted(set(state["values"]["tools"]["enabled_tools"]) - {"web_search"})
    saved = _save(
        client,
        {
            "tools": {"mode": "custom", "enabled_tools": enabled},
            "breaker": {"stability_warning": 0.55},
        },
    )
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["state"]["apply"]["tools"]["status"] == "pending"

    reply = client.post("/api/chat", json={"message": "b"})
    assert "two" in reply.text, reply.text
    assert len(model.requests) == 2
    second = set(model.requests[-1]["tool_names"])
    assert "web_search" not in second
    # Bound memory tools are not governed by the tool switches.
    assert "memory_write" in second
    state = client.get("/api/config/hub").get_json()
    assert state["apply"]["tools"]["status"] == "applied"
    assert state["apply"]["breaker"]["status"] == "applied"
    assert app.extensions["config_store"] is store


def test_hub_script_ids_exist_in_the_settings_template():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "dashboard"
    source = (root / "static" / "js" / "settings_hub.js").read_text(encoding="utf-8")
    template = (root / "templates" / "settings.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', template))
    referenced = set(re.findall(r'\$\(\s*"([^"]+)"\s*\)', source))
    assert referenced and not sorted(referenced - ids)
    # Field-error targets the inline validation writes to.
    for field in (
        "classifier-choice",
        "breaker-stability_warning",
        "breaker-stability_critical",
        "breaker-bc_threshold",
        "tools-mode",
        "tools-enabled_tools",
        "tools-shell_allowlist",
    ):
        assert f"err-{field}" in ids


def test_hub_state_offers_three_presets_with_diffs(client):
    state = client.get("/api/config/hub").get_json()
    presets = {p["id"]: p for p in state["presets"]}
    assert list(presets) == ["safe", "standard", "developer"]
    assert [p["id"] for p in state["presets"] if p["default"]] == ["standard"]
    for preset in presets.values():
        assert preset["values"]["tools"]["mode"] == "custom"
        assert not set(preset["values"]["tools"]["enabled_tools"]) & tool_registry.EXEC_TOOL_NAMES
        assert {"field": "tools.mode", "from": "trust_all", "to": "custom"} in preset["diff"]
    # #471: the preset's entries with the default exits (the whole set)
    safe_breaker = presets["safe"]["values"]["breaker"]
    assert {name: safe_breaker[name] for name in hub.SAFE_BREAKER} == hub.SAFE_BREAKER
    assert safe_breaker["stability_warning_exit"] == StiraConfig().stability_warning_exit
    assert state["summary"]["preset"] is None  # fresh install: trust_all matches no preset

    safe = presets["safe"]["values"]
    saved = _save(client, {"tools": safe["tools"], "breaker": safe["breaker"]})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["state"]["summary"]["preset"] == "safe"
    assert saved.get_json()["state"]["dangers"] == []


# --- Change history (#195) ---------------------------------------------------------


def test_saves_append_history_and_rollback_restores_earlier_values(client, store):
    first = _save(client, {"breaker": {"stability_warning": 0.55}}, source="manual")
    assert first.status_code == 200
    _save(client, {"tools": {"mode": "custom", "enabled_tools": ["file_read"]}}, source="defaults")
    lines = store.history_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # JSONL, one object per line, append-only
    entries = client.get("/api/config/history").get_json()["entries"]
    assert [e["source"] for e in entries] == ["defaults", "manual"]
    assert entries[1]["id"] == first.get_json()["history_id"]
    assert entries[1]["changes"] == [
        {"field": "breaker.stability_warning", "from": 0.5, "to": 0.55}
    ]

    version = client.get("/api/config/hub").get_json()["version"]
    undo = client.post(
        f"/api/config/history/{entries[1]['id']}/rollback", json={"base_version": version}
    )
    assert undo.status_code == 200, undo.get_json()
    state = undo.get_json()["state"]
    assert state["values"]["breaker"]["stability_warning"] == 0.5
    # Field-level: the later tool change is kept.
    assert state["values"]["tools"]["enabled_tools"] == ["file_read"]
    newest = client.get("/api/config/history").get_json()["entries"][0]
    assert newest["source"] == f"rollback:{entries[1]['id']}"


def test_rollback_into_a_danger_needs_confirmation(client):
    _save(client, {"tools": {"mode": "custom"}})
    entry = client.get("/api/config/history").get_json()["entries"][0]
    version = client.get("/api/config/hub").get_json()["version"]
    url = f"/api/config/history/{entry['id']}/rollback"
    refused = client.post(url, json={"base_version": version})
    assert refused.status_code == 400
    assert refused.get_json()["needs_confirmation"] == ["trust_all"]
    ok = client.post(url, json={"base_version": version, "acknowledged_dangers": ["trust_all"]})
    assert ok.status_code == 200
    assert ok.get_json()["state"]["values"]["tools"]["mode"] == "trust_all"


def test_rollback_conflict_and_unknown_entry(client):
    _save(client, {"breaker": {"stability_warning": 0.55}})
    entry = client.get("/api/config/history").get_json()["entries"][0]
    stale = client.post(
        f"/api/config/history/{entry['id']}/rollback", json={"base_version": "stale"}
    )
    assert stale.status_code == 409
    assert client.post("/api/config/history/nope/rollback", json={}).status_code == 404


def test_history_never_stores_secret_looking_commands(client, store):
    secret = "curl -H Authorization:Bearer ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    # A command containing a secret is refused outright, never stored even masked
    # (#190 review round 5): not in the rules, their head, or the history.
    response = _save(
        client,
        {"tools": {"shell_allowlist": ["ls", secret]}},
        acknowledged_dangers=["tool_policy_loosened"],
    )
    assert response.status_code == 400, response.get_json()
    assert [e["code"] for e in response.get_json()["errors"]] == ["secret_command"]
    for path in (
        store.tool_path,
        store.tool_path.with_name(store.tool_path.name + ".head"),
        store.history_path,
    ):
        if path.exists():
            assert b"a1B2c3D4e5F6" not in path.read_bytes(), path
    assert "ghp_a1B2" not in json.dumps(store.load_tools().as_dict())


def test_model_switch_is_recorded(client):
    from stiracore.agent.model_factory import DEFAULT_MODELS

    before = client.get("/api/config/providers").get_json()["current"]["model_id"]
    target = min(set(DEFAULT_MODELS.values()) - {before})
    assert client.post("/api/config/models", json={"model_id": target}).status_code == 200
    entry = client.get("/api/config/history").get_json()["entries"][0]
    assert entry["source"] == "model"
    assert entry["changes"] == [{"field": "model.model_id", "from": before, "to": target}]


def test_unknown_source_is_rejected(client):
    assert _save(client, {"breaker": {"bc_threshold": 0.65}}, source="robot").status_code == 400


def test_tools_redirect_keeps_the_query_string(client):
    response = client.get("/tools?lang=en")
    assert response.headers["Location"].endswith("/settings?lang=en#sec-tools")


def test_hub_mutations_require_csrf_on_the_dashboard(tmp_path):
    from dashboard.app import create_app
    from stiracore.core import NeuralState

    NeuralState().save(tmp_path / "state.json")
    app = create_app(
        state_path=tmp_path / "state.json",
        gateway_config_path=tmp_path / "gateway-config.json",
        agent_config_path=tmp_path / "agent-config.json",
        tool_config_path=tmp_path / "tool-config.json",
    )
    client = app.test_client()
    no_csrf = {"X-Test-No-Csrf": "1"}
    body = {"changes": {"breaker": {"stability_warning": 0.55}}}
    for url in (
        "/api/config/hub",
        "/api/config/hub/validate",
        "/api/config/history/x/rollback",
    ):
        assert client.post(url, json=body, headers=no_csrf).status_code == 403, url
    assert not (tmp_path / "agent-config.json").exists()
    assert client.post("/api/config/hub", json=body).status_code == 200


# --- #516: "reset to defaults" proposes the first-run rules -----------------------------------


def _first_run(path):
    return ToolConfig.first_run_defaults(path)


def test_reset_to_defaults_proposes_the_first_run_tool_rules(tmp_path):
    """#516: not the standard preset's ``ask_tools: []`` (every local write Always), but
    what a genuinely new install starts with: reads Always, local writes Ask, external
    sends and exec off."""
    first = _first_run(tmp_path / "tool-config.json")
    tools = hub.default_values()["tools"]
    assert tools["ask_tools"] == sorted(first.ask_tools)
    assert tools["ask_tools"]  # the local writes, never empty
    assert set(tools["enabled_tools"]) == set(first.enabled_tools)
    assert tools["mode"] == first.mode == "custom"
    assert not set(tools["enabled_tools"]) & hub.EXTERNAL_SEND_TOOLS
    assert not set(tools["enabled_tools"]) & tool_registry.EXEC_TOOL_NAMES
    # The presets keep their contents (they keep the saved Ask set, #190).
    empty = {**hub.default_values(), "tools": {**tools, "ask_tools": []}}
    for preset_id in hub.PRESET_IDS:
        assert hub.preset_values(preset_id, empty)["tools"]["ask_tools"] == []


def test_reset_to_defaults_saved_matches_a_fresh_install_and_rolls_back(client, store, tmp_path):
    """The pre-#190 install of this file (trust_all): the file is unchanged until the save;
    the saved rules are the first-run ones; the history records ``defaults`` and the
    rollback restores the earlier rules."""
    client.get("/api/config/hub")  # the one-time pre-#190 migration (ON -> Always)
    before = store.tool_path.read_bytes()
    defaults = client.get("/api/config/hub").get_json()["defaults"]["tools"]
    validated = client.post("/api/config/hub/validate", json={"changes": {"tools": defaults}})
    assert store.tool_path.read_bytes() == before  # showing the defaults writes nothing
    # The diff is shown before the save, as for any change.
    validated = validated.get_json()
    assert validated["ok"] is True, validated
    fields = {change["field"] for change in validated["diff"]}
    assert "tools.ask_tools" in fields and "tools.mode" in fields
    saved = _save(client, {"tools": defaults}, source="defaults")
    assert saved.status_code == 200, saved.get_json()
    first = _first_run(tmp_path / "fresh-tool-config.json")
    rules = store.load_tools()
    assert rules.mode == "custom"
    assert set(rules.ask_tools) == set(first.ask_tools)
    assert set(rules.enabled_tools) == set(first.enabled_tools)
    entry = client.get("/api/config/history").get_json()["entries"][0]
    assert entry["source"] == "defaults"
    body = {
        "base_version": client.get("/api/config/hub").get_json()["version"],
        # Back to "allow everything": the same confirmations as any such loosening.
        "acknowledged_dangers": ["trust_all"],
    }
    route = f"/api/config/history/{entry['id']}/rollback"
    asked = client.post(route, json=body)
    assert asked.get_json()["error_code"] == "needs_tool_names"
    undo = client.post(route, json={**body, "confirm_always": asked.get_json()["needs_tool_names"]})
    assert undo.status_code == 200, undo.get_json()
    assert store.load_tools().mode == "trust_all"
