"""Provider-aware model selection and the connection test (#208).

Everything runs offline: the connection test uses the scripted FakeModel, and
the root conftest fails any test that would reach AWS.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from dashboard.app import create_app
from stiracore.agent.model_switch import apply_model_selection, selection_spec
from stiracore.config import AgentConfig, GatewayConfig
from stiracore.providers import PROVIDERS, ModelSpec
from stiracore.providers.connection_test import (
    REASONS,
    classify_error,
    run_connection_test,
)
from tests.provider_fakes import FakeModel, text_turn

CANARY = "sk-canary-208-DO-NOT-LEAK-0123456789"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("STIRA_PROVIDER", "STIRA_MODEL", "STIRA_BASE_URL", "STIRA_API_KEY_ENV"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def paths(tmp_path):
    from stiracore.core import NeuralState

    NeuralState(stability=0.9).save(tmp_path / "state.json")
    return tmp_path


@pytest.fixture
def app(paths):
    return create_app(
        state_path=paths / "state.json",
        gateway_config_path=paths / "gateway-config.json",
        agent_config_path=paths / "agent-config.json",
        tool_config_path=paths / "tool-config.json",
    )


@pytest.fixture
def client(app):
    return app.test_client()


def _fake_builder(script=None, calls=None):
    def build(spec):
        if calls is not None:
            calls.append(spec)
        return FakeModel(script or [text_turn("pong", usage=_usage())], model_id=spec.model_id)

    return build


def _usage():
    return {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4}


# --- GET /api/config/providers -------------------------------------------------


def test_providers_list_every_provider_with_badges_and_current(client):
    data = client.get("/api/config/providers").get_json()

    names = [row["name"] for row in data["providers"]]
    assert names == list(PROVIDERS)
    assert data["current"] == {
        "provider": "bedrock",
        "model_id": GatewayConfig().model_id,
        "base_url": None,
        "api_key_env": None,
    }
    bedrock = data["providers"][0]
    current = [m for m in bedrock["models"] if m["current"]]
    assert [m["id"] for m in current] == [GatewayConfig().model_id]
    caps = current[0]["capabilities"]
    assert set(caps) == {"vision", "tools", "thinking", "known"}
    assert caps["tools"] is True and caps["known"] is True
    llamacpp = next(row for row in data["providers"] if row["name"] == "llamacpp")
    assert llamacpp["local"] is True
    assert llamacpp["credentials"]["kind"] == "none"
    assert llamacpp["default_base_url"] == "http://localhost:8080"
    # The observer server runs no chat, so a saved selection applies at next start.
    assert data["applies"] == "next_start"
    assert data["running"] is None
    assert data["connection_test"]["max_tokens"] <= 32


def test_credentials_are_status_only_never_values(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", CANARY)
    response = client.get("/api/config/providers")
    body = response.get_data(as_text=True)

    assert CANARY not in body
    openai = next(row for row in response.get_json()["providers"] if row["name"] == "openai")
    assert openai["credentials"] == {
        "kind": "api_key",
        "configured": True,
        "env": "OPENAI_API_KEY",
        "warnings": [],
    }
    monkeypatch.delenv("OPENAI_API_KEY")
    openai = next(
        row
        for row in client.get("/api/config/providers").get_json()["providers"]
        if row["name"] == "openai"
    )
    assert openai["credentials"]["configured"] is False


def test_env_pins_are_reported(client, monkeypatch):
    monkeypatch.setenv("STIRA_MODEL", "global.anthropic.claude-sonnet-5")
    assert "STIRA_MODEL" in client.get("/api/config/providers").get_json()["pinned_by_env"]


# --- POST /api/config/models (provider selection) -------------------------------


def test_select_local_provider_persists_overrides(client, paths):
    response = client.post(
        "/api/config/models",
        json={"provider": "llamacpp", "model_id": "local", "base_url": "http://127.0.0.1:8080"},
    )

    assert response.status_code == 200, response.get_json()
    data = response.get_json()
    assert data["current"]["provider"] == "llamacpp"
    assert data["current"]["base_url"] == "http://127.0.0.1:8080"
    saved = AgentConfig.load(paths / "agent-config.json")
    assert saved.provider_override == "llamacpp"
    assert saved.model_override == "local"
    assert saved.base_url_override == "http://127.0.0.1:8080"
    # Switching models on the same provider keeps the endpoint.
    client.post("/api/config/models", json={"provider": "llamacpp", "model_id": "other"})
    saved = AgentConfig.load(paths / "agent-config.json")
    assert (saved.model_override, saved.base_url_override) == ("other", "http://127.0.0.1:8080")


def test_select_gateway_provider_uses_model_override_only(client, paths):
    response = client.post(
        "/api/config/models", json={"provider": "bedrock", "model_id": "custom.model-v1:0"}
    )

    assert response.status_code == 200
    saved = AgentConfig.load(paths / "agent-config.json")
    assert saved.model_override == "custom.model-v1:0"
    assert saved.provider_override is None


def test_legacy_catalog_pick_resets_provider_override(client, paths):
    client.post(
        "/api/config/models",
        json={"provider": "llamacpp", "model_id": "local", "base_url": "http://127.0.0.1:8080"},
    )
    target = GatewayConfig().model_id
    assert client.post("/api/config/models", json={"model_id": target}).status_code == 200
    saved = AgentConfig.load(paths / "agent-config.json")
    assert (saved.provider_override, saved.base_url_override) == (None, None)
    assert client.post("/api/config/models", json={"model_id": None}).status_code == 200
    assert AgentConfig.load(paths / "agent-config.json").model_override is None


def test_select_missing_key_is_rejected_with_variable_name(client, monkeypatch, paths):
    from stiracore.observability import config_api

    monkeypatch.delenv("STIRA_TEST_208_KEY", raising=False)
    monkeypatch.setattr(config_api, "provider_available", lambda name: True)
    response = client.post(
        "/api/config/models",
        json={"provider": "openai", "model_id": "gpt-5-mini", "api_key_env": "STIRA_TEST_208_KEY"},
    )

    assert response.status_code == 400
    assert response.get_json()["reason"] == "missing_credential"
    assert response.get_json()["env"] == "STIRA_TEST_208_KEY"
    assert not (paths / "agent-config.json").exists()


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "sk-not-a-provider-" + CANARY, "model_id": "x"},
        {"provider": "llamacpp", "model_id": CANARY + " with spaces"},
        {"provider": "openai", "model_id": "gpt-5-mini", "api_key_env": CANARY},
        {"provider": "openai", "model_id": "gpt-5-mini", "base_url": "http://u:" + CANARY + "@h"},
        {"provider": "llamacpp", "model_id": "local", "extra_field": CANARY},
    ],
)
def test_invalid_selection_never_echoes_input(client, body):
    response = client.post("/api/config/models", json=body)

    assert response.status_code == 400
    assert CANARY not in response.get_data(as_text=True)


def test_select_uninstalled_provider_names_the_extra(client, monkeypatch):
    from stiracore.observability import config_api

    monkeypatch.setattr(config_api, "provider_available", lambda name: False)
    response = client.post("/api/config/models", json={"provider": "gemini", "model_id": "g"})

    assert response.status_code == 400
    assert response.get_json()["reason"] == "provider_unavailable"
    assert response.get_json()["extra"] == "gemini"


def test_remote_endpoint_for_api_provider_is_config_file_only(client):
    response = client.post(
        "/api/config/models",
        json={"provider": "openai", "model_id": "gpt-5-mini", "base_url": "https://example.com/v1"},
    )

    assert response.status_code == 400
    assert "gateway-config.json" in response.get_json()["error"]


def test_selection_post_requires_csrf_and_honours_idempotency(client):
    body = {"provider": "llamacpp", "model_id": "local"}
    no_csrf = client.post("/api/config/models", json=body, headers={"X-Test-No-Csrf": "1"})
    assert no_csrf.status_code == 403
    headers = {"Idempotency-Key": "208-same-key"}
    assert client.post("/api/config/models", json=body, headers=headers).status_code == 200
    assert client.post("/api/config/models", json=body, headers=headers).status_code == 409


# --- POST /api/config/models/test -----------------------------------------------


def test_connection_test_ok_with_fake_model(app, client):
    calls = []
    app.extensions["stira_model_builder"] = _fake_builder(calls=calls)

    response = client.post(
        "/api/config/models/test",
        json={"provider": "llamacpp", "model_id": "local", "base_url": "http://127.0.0.1:8080"},
    )

    data = response.get_json()
    assert response.status_code == 200
    assert data["ok"] is True and data["reason"] == "ok"
    assert data["provider"] == "llamacpp" and data["model_id"] == "local"
    assert data["input_tokens"] == 3 and data["output_tokens"] == 1
    assert isinstance(data["latency_ms"], int)
    [spec] = calls
    assert spec.options["base_url"] == "http://127.0.0.1:8080"


def test_connection_test_defaults_to_current_selection(app, client):
    calls = []
    app.extensions["stira_model_builder"] = _fake_builder(calls=calls)

    assert client.post("/api/config/models/test", json={}).get_json()["ok"] is True
    assert calls[0].provider == "bedrock"
    assert calls[0].model_id == GatewayConfig().model_id


def test_connection_test_sends_one_tiny_request(app, client):
    model = FakeModel([text_turn("pong")])
    app.extensions["stira_model_builder"] = lambda spec: model

    client.post("/api/config/models/test", json={"provider": "llamacpp", "model_id": "local"})

    [request] = model.requests
    assert request["tool_names"] == []
    assert request["system_prompt"] is None
    assert request["messages"] == [{"role": "user", "content": [{"text": "ping"}]}]


class _HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status


@pytest.mark.parametrize(
    ("error", "reason", "status"),
    [
        (_HTTPError(401, "Incorrect API key provided: " + CANARY), "auth_failed", 401),
        (_HTTPError(404, "model " + CANARY + " not found"), "model_not_found", 404),
        (_HTTPError(400, "bad"), "bad_request", 400),
        (_HTTPError(503, "down"), "server_error", 503),
        (ConnectionRefusedError("refused " + CANARY), "unreachable", None),
        (RuntimeError("Authorization: Bearer " + CANARY), "error", None),
    ],
)
def test_connection_test_failure_reason_without_secrets(app, client, error, reason, status):
    app.extensions["stira_model_builder"] = _fake_builder(script=[[{"raise": error}]])

    response = client.post(
        "/api/config/models/test", json={"provider": "llamacpp", "model_id": "local"}
    )

    data = response.get_json()
    assert response.status_code == 200
    assert data["ok"] is False
    assert data["reason"] == reason
    assert data["http_status"] == status
    assert data["error_type"] == type(error).__name__
    assert CANARY not in response.get_data(as_text=True)


def test_connection_test_missing_key_names_variable(app, client, monkeypatch):
    monkeypatch.delenv("STIRA_TEST_208_KEY", raising=False)
    app.extensions["stira_model_builder"] = _fake_builder()

    data = client.post(
        "/api/config/models/test",
        json={"provider": "openai", "model_id": "gpt-5-mini", "api_key_env": "STIRA_TEST_208_KEY"},
    ).get_json()

    assert (data["ok"], data["reason"]) == (False, "missing_credential")
    assert data["env"] == "STIRA_TEST_208_KEY"


def test_connection_test_key_value_never_in_response(app, client, monkeypatch):
    monkeypatch.setenv("STIRA_TEST_208_KEY", CANARY)
    seen = []

    def build(spec):
        seen.append(spec)
        return FakeModel([[{"raise": _HTTPError(401, "bad key " + CANARY)}]])

    app.extensions["stira_model_builder"] = build
    response = client.post(
        "/api/config/models/test",
        json={"provider": "openai", "model_id": "gpt-5-mini", "api_key_env": "STIRA_TEST_208_KEY"},
    )

    assert response.get_json()["reason"] == "auth_failed"
    assert CANARY not in response.get_data(as_text=True)
    # The spec carries the variable NAME only.
    assert CANARY not in repr(seen[0]) and seen[0].options["api_key_env"] == "STIRA_TEST_208_KEY"


def test_connection_test_timeout(monkeypatch):
    release = threading.Event()

    class SlowModel(FakeModel):
        async def stream(self, *args, **kwargs):
            import asyncio

            while not release.is_set():
                await asyncio.sleep(0.01)
            yield {"messageStop": {"stopReason": "end_turn"}}

    try:
        result = run_connection_test(
            ModelSpec("llamacpp", "local"), timeout=0.2, model_builder=lambda spec: SlowModel()
        )
    finally:
        release.set()
    assert (result.ok, result.reason) == (False, "timeout")


def test_connection_test_is_single_flight(app, client):
    started, release = threading.Event(), threading.Event()

    class BlockingModel(FakeModel):
        async def stream(self, *args, **kwargs):
            import asyncio

            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            yield {"messageStop": {"stopReason": "end_turn"}}

    app.extensions["stira_model_builder"] = lambda spec: BlockingModel()
    body = {"provider": "llamacpp", "model_id": "local"}
    results = []
    worker = threading.Thread(
        target=lambda: results.append(app.test_client().post("/api/config/models/test", json=body))
    )
    worker.start()
    assert started.wait(5)
    try:
        busy = client.post("/api/config/models/test", json=body)
        assert busy.status_code == 429
        assert busy.get_json()["reason"] == "busy"
    finally:
        release.set()
        worker.join(5)
    assert results[0].get_json()["ok"] is True


def test_connection_test_post_requires_csrf(client):
    response = client.post("/api/config/models/test", json={}, headers={"X-Test-No-Csrf": "1"})
    assert response.status_code == 403


def test_classify_error_walks_causes_and_botocore_shapes():
    try:
        try:
            raise ConnectionRefusedError("no")
        except ConnectionRefusedError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert classify_error(outer) == ("unreachable", None)

    class ClientError(Exception):
        def __init__(self, code, status):
            super().__init__(code)
            self.response = {
                "Error": {"Code": code},
                "ResponseMetadata": {"HTTPStatusCode": status},
            }

    assert classify_error(ClientError("AccessDeniedException", 403)) == ("auth_failed", 403)
    assert classify_error(ClientError("ResourceNotFoundException", 404)) == (
        "model_not_found",
        404,
    )
    assert classify_error(TimeoutError()) == ("timeout", None)


def test_reason_codes_have_ui_messages_in_both_catalogs():
    js = (ROOT / "dashboard" / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    for lang in ("ja", "en"):
        catalog = json.loads(
            (ROOT / "dashboard" / "static" / "i18n" / f"catalog.{lang}.json").read_text("utf-8")
        )
        for reason in REASONS:
            assert f'{reason}: () => t("settings.reason_{reason}")' in js
            assert f"settings.reason_{reason}" in catalog


# --- next-turn switching ---------------------------------------------------------


class _Agent:
    def __init__(self, spec):
        self.model_spec = spec
        self.model = object()
        self.provider = spec.provider if spec else "custom"
        self.model_id = spec.model_id if spec else "fake"
        self.region_name = None
        self.max_output_tokens = 512
        self._legacy_bedrock = True


def test_apply_model_selection_swaps_only_when_changed(monkeypatch):
    from stiracore.agent import model_switch

    built = []
    monkeypatch.setattr(
        model_switch,
        "create_model",
        lambda spec, *, max_tokens=None: built.append((spec, max_tokens)) or FakeModel(),
    )
    agent = _Agent(ModelSpec.bedrock("m1", region_name="us-east-1"))

    assert apply_model_selection(agent, {"model_id": "m1", "region_name": "us-east-1"}) is False
    assert built == []
    target = ModelSpec("llamacpp", "local", {"base_url": "http://127.0.0.1:8080"})
    assert apply_model_selection(agent, {"model": target}) is True
    assert (agent.provider, agent.model_id, agent.model_spec) == ("llamacpp", "local", target)
    assert agent._legacy_bedrock is False
    assert built == [(target, 512)]
    # An injected Model instance (no spec) is never replaced.
    injected = _Agent(None)
    assert apply_model_selection(injected, {"model": target}) is False


def test_selection_spec_from_legacy_kwargs():
    assert selection_spec({"model_id": "m", "region_name": "eu-west-1"}) == ModelSpec.bedrock(
        "m", region_name="eu-west-1"
    )


def test_stira_applies_saved_selection_from_the_next_turn(tmp_path, monkeypatch):
    """End to end on the chat server: save -> unchanged until the next turn -> switched."""
    from unittest.mock import Mock

    from dashboard import stira_server
    from stiracore.agent import model_switch, nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.core import NeuralState
    from stiracore.observability.config_api import ConfigStore

    data_dir = tmp_path / "stira-data"
    data_dir.mkdir()
    NeuralState(stability=0.9).save(data_dir / "neural-state.json")
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    # Independent of any local .local/stira/gateway-config.json.
    monkeypatch.setattr(stira_server, "GATEWAY_CONFIG", GatewayConfig(path=tmp_path / "gw.json"))
    monkeypatch.setattr(stira_server, "MODEL_ID", GatewayConfig().model_id)
    monkeypatch.setattr(stira_server, "REGION", os.environ.get("AWS_REGION", "ap-northeast-1"))
    monkeypatch.setattr(
        nxp_agent, "BedrockModel", lambda **kw: FakeModel([text_turn("from bedrock")] * 3)
    )
    built = []

    def fake_create_model(spec, *, max_tokens=None):
        built.append(spec)
        return FakeModel([text_turn("from llamacpp")] * 3, model_id=spec.model_id)

    monkeypatch.setattr(model_switch, "create_model", fake_create_model)
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    app = stira_server.create_app(config_store=store)
    client = app.test_client()

    assert "from bedrock" in client.post("/api/chat", json={"message": "a"}).text
    before = client.get("/api/config/providers").get_json()
    assert before["applies"] == "next_turn"
    assert before["running"]["provider"] == "bedrock"

    saved = client.post(
        "/api/config/models",
        json={"provider": "llamacpp", "model_id": "local", "base_url": "http://127.0.0.1:8080"},
    )
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["applies"] == "next_turn"
    # Saved, not yet applied: the running model changes only with the next turn.
    assert client.get("/api/config/providers").get_json()["running"]["provider"] == "bedrock"

    assert "from llamacpp" in client.post("/api/chat", json={"message": "b"}).text
    running = client.get("/api/config/providers").get_json()["running"]
    assert (running["provider"], running["model_id"], running["apply_error"]) == (
        "llamacpp",
        "local",
        None,
    )
    assert [spec.provider for spec in built] == ["llamacpp"]
    assert built[0].options["base_url"] == "http://127.0.0.1:8080"
