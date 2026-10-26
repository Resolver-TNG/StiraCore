"""/api/gateway/status names the provider and model really in use (#558 follow-up, refs #107)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stiracore.providers.failure import status_model


def test_bedrock_keeps_the_plain_model_id():
    agent = SimpleNamespace(provider="bedrock", model_id="m1", model_spec=None)
    assert status_model(agent, "x", "bedrock") == {
        "model": "m1",
        "provider": "bedrock",
        "host": None,
    }


def test_llamacpp_names_provider_and_endpoint_not_the_bedrock_default():
    agent = SimpleNamespace(
        provider="llamacpp",
        model_id="global.anthropic.claude-sonnet-5",
        model_spec=SimpleNamespace(options={"base_url": "http://127.0.0.1:9999/v1?k=s"}),
    )
    view = status_model(agent, "global.anthropic.claude-sonnet-5", "bedrock")
    assert view == {
        "model": "llamacpp · 127.0.0.1:9999",
        "provider": "llamacpp",
        "host": "127.0.0.1:9999",
    }


def test_other_providers_show_id_with_provider_and_host():
    agent = SimpleNamespace(
        provider="openai",
        model_id="gpt-x",
        model_spec=SimpleNamespace(options={"base_url": "https://api.example.com/v1"}),
    )
    assert status_model(agent, "y", "bedrock")["model"] == "gpt-x (openai · api.example.com)"


def test_status_endpoint_on_llamacpp(tmp_path, monkeypatch):
    from dashboard import stira_server

    monkeypatch.setenv("STIRA_PROVIDER", "llamacpp")
    monkeypatch.setenv("STIRA_BASE_URL", "http://127.0.0.1:5599")
    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    client = stira_server.create_app().test_client()
    status = client.get("/api/gateway/status").json
    assert status["model"] == "llamacpp · 127.0.0.1:5599"
    assert status["model_host"] == "127.0.0.1:5599"
    assert status["provider"] == "llamacpp"
    assert "anthropic" not in status["model"]


@pytest.mark.parametrize("provider", ["bedrock"])
def test_status_endpoint_on_bedrock_is_unchanged(tmp_path, monkeypatch, provider):
    from dashboard import stira_server

    monkeypatch.delenv("STIRA_PROVIDER", raising=False)
    monkeypatch.delenv("STIRA_BASE_URL", raising=False)
    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    client = stira_server.create_app().test_client()
    status = client.get("/api/gateway/status").json
    assert status["model"] == stira_server.MODEL_ID
    assert status["model_host"] is None
