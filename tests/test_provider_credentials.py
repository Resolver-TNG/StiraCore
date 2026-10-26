"""Provider configuration and credentials by reference (#205).

The canary below stands in for a real key. It must reach the provider client
constructor and nothing else: not config files, HTTP responses, SSE, the turn
store/replay API, exports, logs, stdout, or reprs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import ClassVar

import pytest

from stiracore.config import AgentConfig, GatewayConfig, resolve_config
from stiracore.config.resolver import model_kwargs
from stiracore.providers import ModelSpec, create_model
from stiracore.providers import factory as provider_factory
from stiracore.providers.credentials import (
    MissingCredentialError,
    credential_status,
    endpoint_warnings,
    require_credentials,
)
from tests.provider_fakes import FakeModel, text_turn

CANARY = "sk-test-CANARY-7f3a9c1e5b2d4086"
KEY_ENV = "STIRA_TEST_LLM_KEY"


class Recorder:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


# --- configuration stores names, never values ------------------------------


def test_gateway_provider_fields_round_trip_and_old_files_still_load(tmp_path):
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"model_id": "global.anthropic.claude-sonnet-5"}), encoding="utf-8")
    legacy = GatewayConfig.load(old)
    assert (legacy.provider, legacy.base_url, legacy.api_key_env) == ("bedrock", None, None)

    path = tmp_path / "gateway.json"
    GatewayConfig(
        model_id="gpt-test",
        provider="OpenAI",
        base_url="https://api.example.test/v1",
        api_key_env=KEY_ENV,
        organization_env="STIRA_TEST_ORG",
        path=path,
    ).save()
    loaded = GatewayConfig.load(path)
    assert (loaded.provider, loaded.api_key_env, loaded.organization_env) == (
        "openai",
        KEY_ENV,
        "STIRA_TEST_ORG",
    )
    assert loaded.base_url == "https://api.example.test/v1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_key_env", CANARY),
        ("organization_env", CANARY),
        ("provider", CANARY),
        ("base_url", f"https://user:{CANARY}@api.example.test/v1"),
        ("base_url", f"https://api.example.test/v1?key={CANARY}"),
    ],
)
def test_pasted_secrets_are_rejected_without_echoing_them(field, value):
    with pytest.raises(ValueError) as caught:
        GatewayConfig(**{field: value})
    assert CANARY not in str(caught.value)


def test_agent_provider_override_brings_its_own_references():
    with pytest.raises(ValueError, match="requires model_override"):
        AgentConfig(provider_override="ollama")
    with pytest.raises(ValueError, match="require provider_override"):
        AgentConfig(api_key_env_override=KEY_ENV)
    with pytest.raises(ValueError) as caught:
        AgentConfig(provider_override="openai", model_override="m", api_key_env_override=CANARY)
    assert CANARY not in str(caught.value)

    gateway = GatewayConfig(provider="openai", api_key_env=KEY_ENV)
    agent = AgentConfig(
        provider_override="ollama",
        model_override="llama3.2:3b",
        base_url_override="http://127.0.0.1:11434",
    )
    resolved = resolve_config(gateway, agent, environ={})
    assert (resolved.provider, resolved.model_id, resolved.api_key_env) == (
        "ollama",
        "llama3.2:3b",
        None,
    )
    assert resolved.base_url == "http://127.0.0.1:11434"

    # Environment wins over the agent override.
    env = {"STIRA_PROVIDER": "anthropic", "STIRA_MODEL": "claude-test", "STIRA_API_KEY_ENV": "K"}
    resolved = resolve_config(gateway, agent, environ=env)
    assert (resolved.provider, resolved.model_id, resolved.api_key_env) == (
        "anthropic",
        "claude-test",
        "K",
    )


def test_resolved_spec_is_secret_free(monkeypatch):
    monkeypatch.setenv(KEY_ENV, CANARY)
    resolved = resolve_config(
        GatewayConfig(provider="openai", model_id="gpt-test", api_key_env=KEY_ENV), environ={}
    )
    spec = resolved.model_spec()
    assert spec == ModelSpec("openai", "gpt-test", {"api_key_env": KEY_ENV})
    kwargs = resolved.nxp_model_kwargs()
    assert kwargs == {"model": spec}
    assert CANARY not in repr(spec) + repr(kwargs) + repr(resolved)


def test_plain_bedrock_keeps_the_legacy_constructor_arguments():
    resolved = resolve_config(GatewayConfig(), environ={})
    assert resolved.nxp_model_kwargs() == {
        "model_id": "global.anthropic.claude-sonnet-5",
        "region_name": "ap-northeast-1",
    }
    assert model_kwargs(
        "bedrock", "m", region="eu-west-1", api_key_env=KEY_ENV, environ={KEY_ENV: "x"}
    ) == {"model": ModelSpec("bedrock", "m", {"region_name": "eu-west-1", "api_key_env": KEY_ENV})}


# --- clear errors for missing credentials ---------------------------------


def test_missing_named_variable_is_a_clear_error():
    with pytest.raises(MissingCredentialError) as caught:
        model_kwargs("openai", "gpt-test", api_key_env=KEY_ENV, environ={})
    message = str(caught.value)
    assert KEY_ENV in message and "openai" in message and "not set" in message


def test_missing_default_variable_is_a_clear_error_unless_custom_endpoint():
    with pytest.raises(MissingCredentialError, match="ANTHROPIC_API_KEY"):
        require_credentials("anthropic", environ={})
    require_credentials("anthropic", environ={"ANTHROPIC_API_KEY": CANARY})
    require_credentials("gemini", environ={"GOOGLE_API_KEY": CANARY})
    # OpenAI-compatible local servers often need no key.
    require_credentials("openai", base_url="http://127.0.0.1:1234/v1", environ={})
    require_credentials("ollama", environ={})
    require_credentials("bedrock", environ={})  # AWS chain resolves at call time


def test_empty_variable_counts_as_missing():
    with pytest.raises(MissingCredentialError):
        require_credentials("openai", api_key_env=KEY_ENV, environ={KEY_ENV: "  "})


def test_switching_through_create_model_reports_the_missing_variable(monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    with pytest.raises(MissingCredentialError, match=KEY_ENV):
        create_model(ModelSpec("openai", "m", {"api_key_env": KEY_ENV}), model_class=Recorder)


# --- values are injected only into the client constructor -----------------


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("openai", {"client_args": {"api_key": CANARY}}),
        ("anthropic", {"client_args": {"api_key": CANARY}}),
        ("gemini", {"client_args": {"api_key": CANARY}}),
        ("mistral", {"client_args": {"api_key": CANARY}}),
        ("litellm", {"client_args": {"api_key": CANARY}}),
        ("bedrock", {"api_key": CANARY}),
    ],
)
def test_api_key_env_reaches_only_the_constructor(monkeypatch, provider, expected):
    monkeypatch.setenv(KEY_ENV, CANARY)
    spec = ModelSpec(provider, "m", {"api_key_env": KEY_ENV})
    model = create_model(spec, model_class=Recorder)
    for key, value in expected.items():
        assert model.kwargs[key] == value
    assert "api_key_env" not in model.kwargs
    assert CANARY not in repr(spec)


def test_organization_env_and_unsupported_references(monkeypatch):
    monkeypatch.setenv(KEY_ENV, CANARY)
    monkeypatch.setenv("STIRA_TEST_ORG", "org-test")
    spec = ModelSpec("openai", "m", {"api_key_env": KEY_ENV, "organization_env": "STIRA_TEST_ORG"})
    assert create_model(spec, model_class=Recorder).kwargs["client_args"] == {
        "api_key": CANARY,
        "organization": "org-test",
    }
    with pytest.raises(ValueError, match="does not use api_key_env"):
        create_model(ModelSpec("ollama", "m", {"api_key_env": KEY_ENV}), model_class=Recorder)
    with pytest.raises(ValueError, match="does not use organization_env"):
        create_model(
            ModelSpec("anthropic", "m", {"organization_env": "STIRA_TEST_ORG"}),
            model_class=Recorder,
        )


def test_bedrock_base_url_is_the_endpoint_url():
    model = create_model(
        ModelSpec("bedrock", "m", {"base_url": "https://vpce.example.test"}), model_class=Recorder
    )
    assert model.kwargs["endpoint_url"] == "https://vpce.example.test"


def test_spec_repr_redacts_inline_secrets():
    spec = ModelSpec("openai", "m", {"client_args": {"api_key": CANARY}, "api_key_env": KEY_ENV})
    text = repr(spec)
    assert CANARY not in text and KEY_ENV in text


# --- status: configured / not configured only ------------------------------


def test_credential_status_never_contains_values(tmp_path):
    configured = credential_status("openai", api_key_env=KEY_ENV, environ={KEY_ENV: CANARY})
    assert configured == {
        "provider": "openai",
        "base_url": None,
        "kind": "api_key",
        "env": KEY_ENV,
        "configured": True,
        "warnings": [],
    }
    assert CANARY not in json.dumps(configured)
    missing = credential_status("anthropic", environ={})
    assert (missing["env"], missing["configured"]) == ("ANTHROPIC_API_KEY", False)
    assert credential_status("ollama", environ={})["kind"] == "none"
    empty_home = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    assert credential_status("bedrock", environ=empty_home)["configured"] is None
    assert (
        credential_status("bedrock", environ={**empty_home, "AWS_PROFILE": "dev"})["configured"]
        is True
    )


def test_endpoint_warnings_for_remote_local_providers_and_plain_http(monkeypatch, caplog):
    assert endpoint_warnings("ollama", "http://127.0.0.1:11434", False) == []
    assert endpoint_warnings("ollama", "http://[::1]:11434", False) == []
    assert "loopback" in endpoint_warnings("ollama", "http://10.0.0.5:11434", False)[0]
    assert "plain HTTP" in endpoint_warnings("openai", "http://gpu.example.test/v1", True)[0]
    assert endpoint_warnings("openai", "https://gpu.example.test/v1", True) == []

    monkeypatch.setenv(KEY_ENV, CANARY)
    caplog.set_level(logging.DEBUG)
    spec = ModelSpec(
        "openai", "m", {"base_url": "http://gpu.example.test/v1", "api_key_env": KEY_ENV}
    )
    create_model(spec, model_class=Recorder)
    assert "plain HTTP" in caplog.text
    assert CANARY not in caplog.text


# --- end to end: nothing leaks from the running chat server ----------------


class ProviderClient(FakeModel):
    """Stands in for OpenAIModel: same constructor keywords, scripted replies."""

    seen_client_args: ClassVar[list] = []

    def __init__(self, client_args=None, model_id="m", params=None, **_):
        super().__init__([text_turn("hello from openai-like")], model_id=model_id)
        ProviderClient.seen_client_args.append(client_args)


def _walk_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def test_secret_never_leaves_the_client(tmp_path, monkeypatch, caplog, capsys):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import RuleBasedClassifier
    from stiracore.observability.config_api import ConfigStore

    root = tmp_path.resolve()
    monkeypatch.chdir(root)
    monkeypatch.setenv(KEY_ENV, CANARY)
    for name in ("STIRA_PROVIDER", "STIRA_BASE_URL", "STIRA_API_KEY_ENV"):
        monkeypatch.delenv(name, raising=False)
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(nxp_agent, "BedrockModel", None)  # Bedrock must not be used
    monkeypatch.setattr(stimulus_classifier, "get_classifier", RuleBasedClassifier)
    monkeypatch.setattr(provider_factory, "load_provider_class", lambda provider: ProviderClient)
    ProviderClient.seen_client_args = []

    gateway = GatewayConfig(
        model_id="gpt-test",
        provider="openai",
        api_key_env=KEY_ENV,
        path=root / "config" / "gateway-config.json",
    )
    gateway.save()
    monkeypatch.setattr(stira_server, "GATEWAY_CONFIG", gateway)
    monkeypatch.setattr(stira_server, "MODEL_ID", "gpt-test")
    monkeypatch.setattr(stira_server, "DATA_DIR", root / "stira-data")
    store = ConfigStore(
        root / "config" / "gateway-config.json",
        root / "config" / "agent-config.json",
        root / "config" / "tool-config.json",
    )
    app = stira_server.create_app(config_store=store)
    client = app.test_client()

    chat = client.post("/api/chat", json={"message": "hello"})
    assert chat.status_code == 200
    assert "hello from openai-like" in chat.text
    assert ProviderClient.seen_client_args == [{"api_key": CANARY}]  # the only destination

    outputs = [chat.text]
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.arguments or "stream" in rule.endpoint:
            continue
        response = client.get(rule.rule)
        outputs.append(response.get_data(as_text=True))
    turn_ids = [turn["turn_id"] for turn in client.get("/api/turns").get_json()["turns"]]
    assert turn_ids  # the replay/export API recorded this turn
    for turn_id in turn_ids:
        replay = client.get(f"/api/turns/{turn_id}/events")
        assert replay.status_code == 200 and replay.get_json()["events"]
        outputs.append(replay.get_data(as_text=True))

    status = client.get("/api/gateway/status").get_json()
    assert status["provider"] == "openai"
    assert status["credentials"]["configured"] is True
    assert status["credentials"]["env"] == KEY_ENV
    config = client.get("/api/config").get_json()
    assert (config["api_key_env"], config["credentials"]["configured"]) == (KEY_ENV, True)

    captured = capsys.readouterr()
    outputs += [caplog.text, captured.out, captured.err]
    outputs += [path.read_bytes().decode("utf-8", "replace") for path in _walk_files(root)]
    leaks = [index for index, text in enumerate(outputs) if CANARY in text]
    assert leaks == []


def test_missing_key_stops_the_chat_server_with_a_named_variable(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent

    root = tmp_path.resolve()
    monkeypatch.chdir(root)
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.setattr(nxp_agent, "BedrockModel", None)
    gateway = GatewayConfig(model_id="gpt-test", provider="openai", api_key_env=KEY_ENV)
    monkeypatch.setattr(stira_server, "GATEWAY_CONFIG", gateway)
    monkeypatch.setattr(stira_server, "DATA_DIR", root / "stira-data")

    with pytest.raises(MissingCredentialError, match=KEY_ENV):
        stira_server.create_app()
