"""#476: the model section's region / organization variable name, and its save contract.

``POST /api/config/models`` (provider selection) checks the fields, answers with error
codes (no free English text for the page, no exception text), refuses a stale
``base_version`` and records the change in the settings history. Offline: no model is
built and no variable value is read into a response.
"""

from __future__ import annotations

import json

import pytest

from dashboard.app import create_app
from stiracore.config import AgentConfig, GatewayConfig, resolve_config
from stiracore.observability import config_api

CANARY = "sk-canary-476-DO-NOT-LEAK-0123456789"
ROUTE = "/api/config/models"
PROVIDERS = "/api/config/providers"
PINS = ("STIRA_PROVIDER", "STIRA_MODEL", "STIRA_BASE_URL", "STIRA_API_KEY_ENV", "AWS_REGION")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in PINS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def paths(tmp_path):
    from stiracore.core import NeuralState

    NeuralState(stability=0.9).save(tmp_path / "state.json")
    return tmp_path


@pytest.fixture
def client(paths):
    app = create_app(
        state_path=paths / "state.json",
        gateway_config_path=paths / "gateway-config.json",
        agent_config_path=paths / "agent-config.json",
        tool_config_path=paths / "tool-config.json",
    )
    return app.test_client()


@pytest.fixture
def openai_ready(monkeypatch):
    monkeypatch.setattr(config_api, "provider_available", lambda name: True)
    monkeypatch.setenv("OPENAI_API_KEY", CANARY)
    monkeypatch.setenv("STIRA_TEST_ORG", "org-" + CANARY)


def _state(client):
    return client.get(PROVIDERS).get_json()


def _post(client, body, **extra):
    return client.post(ROUTE, json={**body, "base_version": _state(client)["version"], **extra})


def _history(client):
    return client.get("/api/config/history").get_json()["entries"]


# --- what the page reads ---------------------------------------------------------


def test_providers_report_region_organization_and_version(client):
    data = _state(client)

    assert data["region"] == GatewayConfig().region
    assert data["region_pinned_by"] is None
    assert data["organization_env"] is None
    assert isinstance(data["version"], str) and len(data["version"]) == 16
    assert data["version"] == _state(client)["version"]
    openai = next(row for row in data["providers"] if row["name"] == "openai")
    bedrock = next(row for row in data["providers"] if row["name"] == "bedrock")
    assert (openai["uses_organization"], openai["uses_region"]) == (True, False)
    assert (bedrock["uses_organization"], bedrock["uses_region"]) == (False, True)


def test_region_pin_is_reported_by_name_only(client, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    response = client.get(PROVIDERS)

    assert response.get_json()["region_pinned_by"] == "AWS_REGION"
    # The saved value is shown; the variable's value is not returned.
    assert response.get_json()["region"] == GatewayConfig().region
    assert "eu-central-1" not in response.get_data(as_text=True)


# --- region ----------------------------------------------------------------------


def test_region_is_saved_with_diff_and_history(client, paths):
    model_id = GatewayConfig().model_id
    before = _state(client)["version"]
    response = _post(client, {"provider": "bedrock", "model_id": model_id, "region": "us-west-2"})

    assert response.status_code == 200, response.get_json()
    data = response.get_json()
    assert data["diff"] == [
        {"field": "model.region", "from": GatewayConfig().region, "to": "us-west-2"}
    ]
    assert data["region"] == "us-west-2"
    assert data["version"] != before and data["version"] == _state(client)["version"]
    assert GatewayConfig.load(paths / "gateway-config.json").region == "us-west-2"
    resolved = resolve_config(
        GatewayConfig.load(paths / "gateway-config.json"),
        AgentConfig.load(paths / "agent-config.json"),
        environ={},
    )
    assert resolved.model_spec().options["region_name"] == "us-west-2"
    entry = _history(client)[0]
    assert entry["source"] == "model"
    assert entry["changes"] == data["diff"]


@pytest.mark.parametrize("region", ["", "US-WEST-2", "us west 2", CANARY, 5, "x" * 80])
def test_invalid_region_is_refused_with_a_code(client, paths, region):
    response = _post(
        client, {"provider": "bedrock", "model_id": GatewayConfig().model_id, "region": region}
    )

    assert response.status_code == 400
    data = response.get_json()
    assert (data["error_code"], data["field"]) == ("invalid_region", "region")
    assert CANARY not in response.get_data(as_text=True)
    assert not (paths / "gateway-config.json").exists()
    assert not (paths / "agent-config.json").exists()


def test_region_is_for_bedrock_only(client, paths):
    response = _post(client, {"provider": "llamacpp", "model_id": "local", "region": "us-west-2"})

    assert response.status_code == 400
    assert response.get_json()["error_code"] == "field_not_used"
    assert response.get_json()["field"] == "region"
    assert not (paths / "agent-config.json").exists()


def test_region_change_is_refused_while_the_variable_pins_it(client, paths, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    model_id = GatewayConfig().model_id
    refused = _post(client, {"provider": "bedrock", "model_id": model_id, "region": "us-west-2"})

    assert refused.status_code == 409
    assert refused.get_json()["error_code"] == "pinned_by_env"
    assert refused.get_json()["env"] == "AWS_REGION"
    assert not (paths / "gateway-config.json").exists()
    # The unchanged saved value (what the page sends back) is not a change.
    same = _post(
        client, {"provider": "bedrock", "model_id": model_id, "region": GatewayConfig().region}
    )
    assert same.status_code == 200, same.get_json()


# --- organization variable name ----------------------------------------------------


def test_organization_env_is_saved_as_a_name_and_used(client, paths, openai_ready):
    response = _post(
        client,
        {"provider": "openai", "model_id": "gpt-5-mini", "organization_env": "STIRA_TEST_ORG"},
    )

    assert response.status_code == 200, response.get_json()
    assert CANARY not in response.get_data(as_text=True)
    assert response.get_json()["organization_env"] == "STIRA_TEST_ORG"
    saved = AgentConfig.load(paths / "agent-config.json")
    assert saved.provider_override == "openai"
    assert saved.organization_env_override == "STIRA_TEST_ORG"
    assert CANARY not in (paths / "agent-config.json").read_text(encoding="utf-8")
    resolved = resolve_config(GatewayConfig(), saved, environ={})
    assert resolved.organization_env == "STIRA_TEST_ORG"
    assert resolved.model_spec().options["organization_env"] == "STIRA_TEST_ORG"
    listing = client.get(PROVIDERS)
    assert listing.get_json()["organization_env"] == "STIRA_TEST_ORG"
    assert CANARY not in listing.get_data(as_text=True)
    assert CANARY not in json.dumps(_history(client))
    # Another model on the same provider keeps the name; an empty string clears it.
    _post(client, {"provider": "openai", "model_id": "gpt-4.1-mini"})
    assert AgentConfig.load(paths / "agent-config.json").organization_env_override == (
        "STIRA_TEST_ORG"
    )
    _post(client, {"provider": "openai", "model_id": "gpt-4.1-mini", "organization_env": ""})
    assert AgentConfig.load(paths / "agent-config.json").organization_env_override is None


def test_organization_env_needs_a_name_a_set_variable_and_an_openai_provider(
    client, paths, openai_ready, monkeypatch
):
    body = {"provider": "openai", "model_id": "gpt-5-mini"}
    bad_name = _post(client, {**body, "organization_env": "org-" + CANARY})
    assert bad_name.status_code == 400
    assert bad_name.get_json()["error_code"] == "invalid_env_name"
    assert bad_name.get_json()["field"] == "organization_env"
    assert CANARY not in bad_name.get_data(as_text=True)

    monkeypatch.delenv("STIRA_TEST_ORG")
    unset = _post(client, {**body, "organization_env": "STIRA_TEST_ORG"})
    assert unset.status_code == 400
    assert unset.get_json()["error_code"] == "missing_credential"
    assert unset.get_json()["env"] == "STIRA_TEST_ORG"

    other = _post(
        client, {"provider": "llamacpp", "model_id": "local", "organization_env": "STIRA_TEST_ORG"}
    )
    assert other.status_code == 400
    assert other.get_json()["error_code"] == "field_not_used"
    assert other.get_json()["field"] == "organization_env"
    assert not (paths / "agent-config.json").exists()


def test_agent_config_keeps_files_without_the_new_field(paths):
    config = AgentConfig(path=paths / "agent-config.json")
    assert "organization_env_override" not in config.as_dict()
    with pytest.raises(ValueError):
        AgentConfig(organization_env_override="STIRA_TEST_ORG")
    with pytest.raises(ValueError):
        AgentConfig(
            provider_override="openai", model_override="m", organization_env_override=CANARY
        )


# --- the save contract: codes, conflict, dry run -----------------------------------


def test_stale_base_version_is_a_conflict_and_nothing_is_saved(client, paths):
    stale = _state(client)["version"]
    first = client.post(
        ROUTE, json={"provider": "llamacpp", "model_id": "local", "base_version": stale}
    )
    assert first.status_code == 200, first.get_json()
    second = client.post(
        ROUTE, json={"provider": "llamacpp", "model_id": "other", "base_version": stale}
    )

    assert second.status_code == 409
    data = second.get_json()
    assert data["error_code"] == "conflict"
    assert data["version"] == _state(client)["version"]
    assert data["current"]["model_id"] == "local"
    assert AgentConfig.load(paths / "agent-config.json").model_override == "local"
    assert len(_history(client)) == 1


def test_validate_only_checks_and_reports_the_diff_without_saving(client, paths):
    ok = _post(client, {"provider": "llamacpp", "model_id": "local"}, validate_only=True)

    assert ok.status_code == 200, ok.get_json()
    assert ok.get_json()["ok"] is True
    assert {change["field"] for change in ok.get_json()["diff"]} == {
        "model.provider",
        "model.model_id",
    }
    assert not (paths / "agent-config.json").exists()
    assert _history(client) == []
    bad = _post(client, {"provider": "llamacpp", "model_id": "a b"}, validate_only=True)
    assert bad.status_code == 400
    assert bad.get_json()["error_code"] == "invalid_model_id"


@pytest.mark.parametrize(
    ("body", "code", "field"),
    [
        ({"provider": "nope-" + CANARY, "model_id": "x"}, "invalid_provider", "provider"),
        ({"provider": "llamacpp", "model_id": CANARY + " x"}, "invalid_model_id", "model_id"),
        (
            {"provider": "openai", "model_id": "m", "api_key_env": CANARY},
            "invalid_env_name",
            "api_key_env",
        ),
        (
            {"provider": "openai", "model_id": "m", "base_url": "http://u:" + CANARY + "@h"},
            "invalid_base_url",
            "base_url",
        ),
        (
            {"provider": "openai", "model_id": "m", "base_url": "https://example.com/v1"},
            "remote_base_url",
            "base_url",
        ),
        ({"provider": "llamacpp", "model_id": "local", "extra": CANARY}, "unknown_field", None),
        ({"provider": "llamacpp"}, "invalid_body", None),
        ({"provider": "llamacpp", "model_id": "local", "base_version": 5}, "invalid_body", None),
    ],
)
def test_every_refusal_has_a_code_and_never_echoes_input(client, body, code, field):
    for route in (ROUTE, ROUTE + "/test"):
        if route.endswith("/test") and code == "invalid_body":
            continue  # the test route has no save keys and falls back to the current model
        response = client.post(route, json=body)

        assert response.status_code == 400, route
        data = response.get_json()
        assert data["error_code"] == code, route
        assert data.get("field") == field
        assert CANARY not in response.get_data(as_text=True)


def test_legacy_catalog_post_has_codes(client):
    for bad in ([], {}, {"model_id": ""}):
        assert client.post(ROUTE, json=bad).get_json()["error_code"] == "invalid_body"
    unknown = client.post(ROUTE, json={"model_id": "definitely-not-a-real-model"})
    assert unknown.get_json()["error_code"] == "unknown_model"


def test_unreadable_config_gives_a_fixed_body_without_exception_text(client, paths):
    (paths / "gateway-config.json").write_text('{"model_id": 5, "zz-' + CANARY + '": 1}', "utf-8")
    calls = [
        client.get(PROVIDERS),
        client.get(ROUTE),
        client.post(ROUTE, json={"provider": "llamacpp", "model_id": "local"}),
        client.post(ROUTE, json={"model_id": GatewayConfig().model_id}),
        client.post(ROUTE + "/test", json={"provider": "llamacpp", "model_id": "local"}),
    ]

    for response in calls:
        assert response.status_code == 503
        assert response.get_json() == {
            "error": "Config unavailable.",
            "error_code": "config_unavailable",
        }


def test_missing_credential_and_sdk_refusals_carry_codes(client, monkeypatch):
    monkeypatch.setattr(config_api, "provider_available", lambda name: False)
    sdk = _post(client, {"provider": "gemini", "model_id": "g"})
    assert (sdk.get_json()["error_code"], sdk.get_json()["extra"]) == (
        "provider_unavailable",
        "gemini",
    )
    monkeypatch.setattr(config_api, "provider_available", lambda name: True)
    monkeypatch.delenv("STIRA_TEST_476_KEY", raising=False)
    key = _post(
        client, {"provider": "openai", "model_id": "m", "api_key_env": "STIRA_TEST_476_KEY"}
    )
    assert key.status_code == 400
    assert key.get_json()["error_code"] == "missing_credential"
    assert key.get_json()["env"] == "STIRA_TEST_476_KEY"
    assert key.get_json()["error"] == "A required environment variable is not set."
