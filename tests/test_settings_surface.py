"""The settings surface manifest (#468) covers every setting; the hub shows it (#469).

Each check fails when its subject is removed: an unclassified field or variable, a class C
row without a reason, a stale docs table, a pinned variable's value in the hub answer.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path

import pytest

from stiracore.config import settings_surface as surface
from stiracore.config import stira_delivery
from stiracore.config.models import AgentConfig, GatewayConfig
from stiracore.core.engine import StiraConfig
from tests.test_docs_sync import CONFIG_DOCS, _doc_env_names, _marked

ROOT = Path(__file__).resolve().parents[1]

DOC = ROOT / "docs" / "settings-surface.md"


def _ids(kind: str) -> set[str]:
    return {item["id"].partition(".")[2] for item in surface.items(kind)}


def _config_fields(cls) -> set[str]:
    return {f.name for f in dataclasses.fields(cls) if f.init and f.name not in ("path", "lock")}


def test_manifest_is_valid() -> None:
    assert surface.problems(surface.load_manifest()) == []


@pytest.mark.parametrize(
    ("kind", "cls"),
    [("gateway", GatewayConfig), ("agent", AgentConfig), ("stira", StiraConfig)],
)
def test_every_config_field_is_classified(kind: str, cls) -> None:
    assert _ids(kind) == _config_fields(cls)


@pytest.mark.parametrize("path", CONFIG_DOCS, ids=lambda p: p.name)
def test_every_documented_variable_is_classified(path: Path) -> None:
    assert _ids("env") == _doc_env_names(path, "env-vars")
    assert _ids("dev") == _doc_env_names(path, "env-vars-dev")


@pytest.mark.parametrize(
    ("kind", "cls"),
    [("gateway", GatewayConfig), ("agent", AgentConfig), ("stira", StiraConfig)],
)
def test_a_dropped_row_fails_the_sync_check(monkeypatch, kind: str, cls) -> None:
    # The real checks above, run against the manifest with one row of each kind removed.
    data = surface.load_manifest()
    dropped = next(item for item in data["items"] if item["id"].startswith(f"{kind}."))
    data["items"].remove(dropped)
    monkeypatch.setattr(surface, "_cached", lambda: data)
    with pytest.raises(AssertionError):
        test_every_config_field_is_classified(kind, cls)


@pytest.mark.parametrize("kind", ["env", "dev"])
def test_a_dropped_variable_row_fails_the_docs_check(monkeypatch, kind: str) -> None:
    data = surface.load_manifest()
    dropped = next(item for item in data["items"] if item["id"].startswith(f"{kind}."))
    data["items"].remove(dropped)
    monkeypatch.setattr(surface, "_cached", lambda: data)
    for path in CONFIG_DOCS:
        with pytest.raises(AssertionError):
            test_every_documented_variable_is_classified(path)


def test_class_c_needs_a_known_reason() -> None:
    data = surface.load_manifest()
    row = next(item for item in data["items"] if item["class"] == "C")
    row.pop("reason")
    assert any("class C needs a reason" in p for p in surface.problems(data))
    data = surface.load_manifest()
    data["items"][0]["reason"] = "because"
    assert any("not in the vocabulary" in p for p in surface.problems(data))
    data = surface.load_manifest()
    data["items"].append(copy.deepcopy(data["items"][0]))
    assert any("duplicate" in p for p in surface.problems(data))


def test_every_c_row_has_a_reason() -> None:
    rows = [item for item in surface.items() if item["class"] == "C"]
    assert rows and all(item.get("reason") for item in rows)


def test_stira_applies_matches_the_delivery_table() -> None:
    for item in surface.items("stira"):
        name = item["id"].partition(".")[2]
        assert item["applies"] == stira_delivery.applies_when(name), name


def test_docs_table_is_generated_from_the_manifest() -> None:
    block = _marked(DOC, "settings-surface")
    assert block.strip() == surface.render_markdown().strip()


def test_docs_index_links_the_page() -> None:
    assert "(settings-surface.md)" in (ROOT / "docs" / "README.md").read_text("utf-8")


def test_manifest_ships_with_the_package() -> None:
    text = (ROOT / "pyproject.toml").read_text("utf-8")
    assert re.search(r'"stiracore\.config"\s*=\s*\["settings_surface\.json"\]', text)


# --- describe / pinned / restart pending (#469) ------------------------------------------


def test_source_env_agent_gateway_default() -> None:
    gateway = dataclasses.replace(GatewayConfig(), hb_enabled=True)
    agent = dataclasses.replace(
        AgentConfig(), model_override="m", stira_config_overrides={"bc_window_size": 30}
    )
    fields = surface.describe(gateway, agent, {"AWS_REGION": "x"})
    assert fields["gateway.region"]["source"] == "env"
    assert fields["gateway.region"]["pinned_by"] == "AWS_REGION"
    assert fields["gateway.model_id"]["source"] == "agent"
    assert fields["gateway.hb_enabled"]["source"] == "gateway"
    assert fields["gateway.prompt_cache"]["source"] == "default"
    assert fields["stira.bc_window_size"]["source"] == "agent"
    assert fields["stira.bc_threshold"]["source"] == "default"
    assert fields["agent.model_override"]["source"] == "agent"


def test_switch_variables_count_only_when_non_empty() -> None:
    blank = surface.describe(GatewayConfig(), AgentConfig(), {"STIRA_HEARTBEAT": "  "})
    assert blank["gateway.hb_enabled"]["pinned_by"] is None
    on = surface.describe(GatewayConfig(), AgentConfig(), {"STIRA_HEARTBEAT": "0"})
    assert on["gateway.hb_enabled"]["pinned_by"] == "STIRA_HEARTBEAT"
    # apply_env_overrides counts an empty STIRA_PROVIDER (it means the default).
    empty = surface.describe(GatewayConfig(), AgentConfig(), {"STIRA_PROVIDER": ""})
    assert empty["gateway.provider"]["pinned_by"] == "STIRA_PROVIDER"


def test_pinned_variables_are_names_only() -> None:
    secret = "sk-value-that-must-not-appear-1234567890"
    environ = {"STIRA_PROMPT_CACHE": secret, "ANTHROPIC_API_KEY": secret, "STIRA_PORT": "5999"}
    pinned = surface.pinned_variables(environ)
    names = [entry["name"] for entry in pinned]
    assert names == ["STIRA_PORT", "STIRA_PROMPT_CACHE"]  # a secret pins nothing: not listed
    text = json.dumps([pinned, surface.describe(GatewayConfig(), AgentConfig(), environ)])
    assert secret not in text and "5999" not in text


def test_yolo_variables_stay_out_of_the_hub() -> None:
    # ADR 0012: the allowance changes no existing read API; the YOLO section shows its pins.
    environ = {"STIRA_YOLO_ALLOW": "external", "STIRA_YOLO_HOST_CONFIRM": "command"}
    assert surface.pinned_variables(environ) == []
    fields = surface.describe(GatewayConfig(), AgentConfig(), environ)
    assert fields == surface.describe(GatewayConfig(), AgentConfig(), {})
    # ...while the manifest still says which variables win over gateway.yolo.
    assert "`STIRA_YOLO_ALLOW`" in surface.render_markdown()


def test_restart_pending_ignores_next_turn_and_pinned_fields() -> None:
    start = surface.saved_snapshot(GatewayConfig(), AgentConfig())
    gateway = dataclasses.replace(GatewayConfig(), hb_enabled=True, prompt_cache=True)
    agent = dataclasses.replace(AgentConfig(), stira_config_overrides={"stability_warning": 0.4})
    fields = surface.describe(gateway, agent, {"STIRA_PROMPT_CACHE": "0"})
    pending = surface.restart_pending(start, gateway, agent, fields)
    assert pending == ["gateway.hb_enabled"]  # prompt_cache pinned, breaker is next turn
    assert surface.restart_pending(None, gateway, agent, fields) == []


def test_one_change_counts_once_per_group() -> None:
    start = surface.saved_snapshot(GatewayConfig(), AgentConfig())
    gateway = dataclasses.replace(GatewayConfig(), jev_enabled=True, jev_classifier="sokudan")
    fields = surface.describe(gateway, AgentConfig(), {})
    pending = surface.restart_pending(start, gateway, AgentConfig(), fields)
    assert sorted(pending) == ["gateway.jev_classifier", "gateway.jev_enabled"]
    assert surface.pending_groups(pending, fields) == ["classifier"]


# --- GET /api/config/hub → surface (#469) -------------------------------------------------


def _hub_client(tmp_path, **kwargs):
    pytest.importorskip("flask")
    from flask import Flask

    from stiracore.observability.config_api import ConfigStore, create_config_blueprint

    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal", **kwargs))
    return app.test_client(), store


def test_hub_answer_has_surface_without_any_variable_value(tmp_path, monkeypatch):
    secret = "AKIA-not-a-real-value-0123456789"
    monkeypatch.setenv("AWS_REGION", secret)
    monkeypatch.setenv("STIRA_HEARTBEAT", secret)
    monkeypatch.setenv("STIRA_TOOL_RULES_ANCHOR_DIR", str(tmp_path / f"anchor-{secret}"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    client, _store = _hub_client(tmp_path)
    response = client.get("/api/config/hub")
    assert response.status_code == 200
    assert secret not in response.get_data(as_text=True)
    body = response.get_json()["surface"]
    assert body["fields"]["gateway.region"]["pinned_by"] == "AWS_REGION"
    assert body["fields"]["gateway.hb_enabled"]["source"] == "env"
    assert {"AWS_REGION", "STIRA_HEARTBEAT"} <= {entry["name"] for entry in body["pinned"]}
    location = body["location"]
    assert location["files"] == ["gateway-config.json", "agent-config.json", "tool-config.json"]
    assert location["anchor_configured"] is True and location["same_directory"] is True
    assert str(tmp_path) not in json.dumps(body)


def test_hub_marks_a_saved_restart_field_as_pending(tmp_path):
    client, store = _hub_client(
        tmp_path, config_location_provider=lambda: {"matches_data_dir": True, "data_dir": "default"}
    )
    first = client.get("/api/config/hub").get_json()["surface"]
    assert first["restart_pending"] == [] and first["restart_known"] is True
    assert first["location"]["matches_data_dir"] is True
    # A restart field saved after the start (by hand, the way #470-#472 will save it).
    gateway = GatewayConfig.load(store.gateway_path)
    gateway.hb_enabled = True
    gateway.path = store.gateway_path
    gateway.save()
    later = client.get("/api/config/hub").get_json()["surface"]
    assert later["restart_pending"] == ["gateway.hb_enabled"]
    assert later["restart_pending_groups"] == ["heartbeat"]
    assert later["fields"]["gateway.hb_enabled"]["pending_restart"] is True
    # The hub's own classifier save (a restart group) counts once.
    version = client.get("/api/config/hub").get_json()["version"]
    saved = client.post(
        "/api/config/hub",
        json={"changes": {"classifier": {"choice": "rule"}}, "base_version": version},
    )
    assert saved.status_code == 200, saved.get_json()
    groups = client.get("/api/config/hub").get_json()["surface"]["restart_pending_groups"]
    assert groups == ["heartbeat", "classifier"]


def test_next_turn_save_is_not_pending_restart(tmp_path):
    client, _store = _hub_client(tmp_path)
    version = client.get("/api/config/hub").get_json()["version"]
    saved = client.post(
        "/api/config/hub",
        json={"changes": {"breaker": {"stability_warning": 0.45}}, "base_version": version},
    )
    assert saved.status_code == 200, saved.get_json()
    assert client.get("/api/config/hub").get_json()["surface"]["restart_pending"] == []


# --- the surface never takes the hub down (#490 review, major-1) -------------------------


def _broken_manifest(monkeypatch, tmp_path) -> None:
    broken = tmp_path / "settings_surface.json"
    broken.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(surface, "MANIFEST_PATH", broken)
    surface._cached.cache_clear()


def _schema_error_manifest(monkeypatch, tmp_path) -> None:
    data = surface.load_manifest()
    data["items"][0]["class"] = "Z"
    broken = tmp_path / "settings_surface.json"
    broken.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(surface, "MANIFEST_PATH", broken)
    surface._cached.cache_clear()


def _resolve_fails(monkeypatch, tmp_path) -> None:
    def fail(self, *args, **kwargs):
        raise OSError("resolve failed")

    monkeypatch.setattr(Path, "resolve", fail)


@pytest.mark.parametrize(
    "inject",
    [_broken_manifest, _schema_error_manifest, _resolve_fails],
    ids=["manifest_json", "manifest_schema", "resolve_oserror"],
)
def test_a_broken_surface_still_answers_and_saves(tmp_path, monkeypatch, caplog, inject):
    client, _store = _hub_client(tmp_path)  # started healthy: the snapshot exists
    inject(monkeypatch, tmp_path)
    try:
        with caplog.at_level("WARNING"):
            response = client.get("/api/config/hub")
        assert response.status_code == 200, response.get_data(as_text=True)[:200]
        body = response.get_json()
        assert body["surface"]["available"] is False
        assert body["surface"]["fields"] == {} and body["surface"]["restart_known"] is False
        warnings = [r for r in caplog.records if "Settings surface unavailable" in r.message]
        assert len(warnings) == 1 and "resolve failed" not in warnings[0].getMessage()
        saved = client.post(
            "/api/config/hub",
            json={
                "changes": {"breaker": {"stability_warning": 0.45}},
                "base_version": body["version"],
            },
        )
        assert saved.status_code == 200, saved.get_json()
    finally:
        monkeypatch.undo()
        surface._cached.cache_clear()


def test_a_broken_manifest_at_start_up_still_starts(tmp_path, monkeypatch):
    _broken_manifest(monkeypatch, tmp_path)
    try:
        client, _store = _hub_client(tmp_path)
        response = client.get("/api/config/hub")
        assert response.status_code == 200
        assert response.get_json()["surface"]["available"] is False
    finally:
        monkeypatch.undo()
        surface._cached.cache_clear()


def test_a_healthy_surface_says_available(tmp_path):
    client, _store = _hub_client(tmp_path)
    assert client.get("/api/config/hub").get_json()["surface"]["available"] is True


# --- source follows the resolver for the provider bundle (#490 review, minor-1) ----------
# resolve_config: STIRA_PROVIDER (present) > agent.provider_override (brings base_url and
# api_key_env with it, even when they are None) > STIRA_BASE_URL / STIRA_API_KEY_ENV >
# gateway. agent.base_url_override without provider_override is not used.

_MARK = {
    "gateway": {"base_url": "http://gateway.test", "api_key_env": "GATEWAY_KEY"},
    "agent": {"base_url": "http://agent.test", "api_key_env": "AGENT_KEY"},
    "env": {"base_url": "http://env.test", "api_key_env": "ENV_KEY"},
}
_ENV_NAME = {"base_url": "STIRA_BASE_URL", "api_key_env": "STIRA_API_KEY_ENV"}


def _provider_cases():
    import itertools

    for env_provider, env_field, override, field_override in itertools.product(
        (False, True), (False, True), (None, "openai"), (False, True)
    ):
        if override is None and field_override:
            continue  # AgentConfig refuses an endpoint override without provider_override
        yield env_provider, env_field, override, field_override


@pytest.mark.parametrize("name", ["base_url", "api_key_env"])
@pytest.mark.parametrize(
    ("env_provider", "env_field", "override", "field_override"), list(_provider_cases())
)
def test_provider_bundle_source_matches_the_resolver(
    name, env_provider, env_field, override, field_override
) -> None:
    from stiracore.config import resolve_config

    gateway = dataclasses.replace(GatewayConfig(), **{name: _MARK["gateway"][name]})
    agent = dataclasses.replace(
        AgentConfig(),
        provider_override=override,
        model_override="m" if override else None,
        **{f"{name}_override": _MARK["agent"][name] if field_override else None},
    )
    environ = {}
    if env_provider:
        environ["STIRA_PROVIDER"] = "bedrock"
    if env_field:
        environ[_ENV_NAME[name]] = _MARK["env"][name]
    value = getattr(resolve_config(gateway, agent, environ=environ), name)
    # The agent bundle may bring None; gateway and env always hold a marker here.
    actual = next((src for src, marks in _MARK.items() if marks[name] == value), "agent")
    field = surface.describe(gateway, agent, environ)[f"gateway.{name}"]
    assert field["source"] == actual, (value, environ, override)
    assert (field["pinned_by"] is not None) == (actual == "env")


@pytest.mark.parametrize("env_model", [False, True])
@pytest.mark.parametrize("env_provider", [False, True])
@pytest.mark.parametrize("override", [None, "model", "provider"])
def test_model_and_provider_source_match_the_resolver(env_model, env_provider, override) -> None:
    from stiracore.config import resolve_config

    gateway = dataclasses.replace(GatewayConfig(), model_id="gateway-model", provider="anthropic")
    agent = dataclasses.replace(
        AgentConfig(),
        model_override="agent-model" if override else None,
        provider_override="openai" if override == "provider" else None,
    )
    environ = {}
    if env_model:
        environ["STIRA_MODEL"] = "env-model"
    if env_provider:
        environ["STIRA_PROVIDER"] = "bedrock"
    resolved = resolve_config(gateway, agent, environ=environ)
    fields = surface.describe(gateway, agent, environ)
    model_from = {"gateway-model": "gateway", "agent-model": "agent", "env-model": "env"}
    provider_from = {"anthropic": "gateway", "openai": "agent", "bedrock": "env"}
    assert fields["gateway.model_id"]["source"] == model_from[resolved.model_id]
    assert fields["gateway.provider"]["source"] == provider_from[resolved.provider]


# --- gateway.ui: board_svg applies on the next Stage load, home at a restart (minor-2) ----


def test_board_svg_save_is_not_pending_restart_but_home_is(tmp_path):
    client, store = _hub_client(tmp_path)
    version = client.get("/api/config/hub").get_json()["version"]
    saved = client.post(
        "/api/config/hub",
        json={
            # on by default (ADR 0012 addendum 2026-10-24): the save turns it off
            "changes": {"board": {"svg": False}},
            "base_version": version,
        },
    )
    assert saved.status_code == 200, saved.get_json()
    assert GatewayConfig.load(store.gateway_path).ui.get("board_svg") is False
    assert client.get("/api/config/hub").get_json()["surface"]["restart_pending"] == []
    # ui.home is read once at start-up (stira_server): a saved change waits for a restart.
    gateway = GatewayConfig.load(store.gateway_path)
    gateway.ui = {**gateway.ui, "home": "stage"}
    gateway.path = store.gateway_path
    gateway.save()
    later = client.get("/api/config/hub").get_json()["surface"]
    assert later["restart_pending"] == ["gateway.ui"]


def test_restart_keys_need_per_field_and_strings() -> None:
    data = surface.load_manifest()
    row = next(item for item in data["items"] if item["id"] == "gateway.ui")
    assert row["applies"] == "per_field" and row["restart_keys"] == ["home"]
    row["applies"] = "restart"
    assert any("restart_keys" in p for p in surface.problems(data))
    row["applies"] = "per_field"
    row["restart_keys"] = [1]
    assert any("restart_keys" in p for p in surface.problems(data))
