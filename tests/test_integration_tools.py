from unittest.mock import MagicMock, Mock

import pytest
import requests
from strands import Agent

from stiracore.integrations import http, oauth
from stiracore.tools import integration_tools as tools
from stiracore.tools import tool_registry
from stiracore.tools.tool_config import ToolConfig


@pytest.fixture
def network(monkeypatch):
    session, response = MagicMock(), MagicMock()
    response.status_code = 200
    response.iter_content.return_value = [b'{"ok":true}']
    session.request.return_value.__enter__.return_value = response
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    monkeypatch.setattr(http.requests, "Session", factory)
    # Offline: names resolve to one public address (the SSRF guard checks it, #98).
    monkeypatch.setattr(http, "system_resolver", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(tools, "CONNECTORS", {})
    return session, response, factory


def test_webhook_bounded_request_and_sdk_registration(network):
    session, _, _ = network
    assert (
        tools.webhook_send("https://hooks.example/event", {"event": "created"})["status"]
        == "success"
    )
    assert session.trust_env is False
    args = session.request.call_args.kwargs
    assert args["allow_redirects"] is False
    assert args["timeout"] == (5, 20)
    assert args["stream"] is True
    agent = Agent(tools=[tools.webhook_send, tools.oauth_api_call], callback_handler=None)
    assert {"webhook_send", "oauth_api_call"} <= set(agent.tool_names)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "file:///private",
        "https://user:secret@example.test",
        "https://example.test/#fragment",
    ],
)
def test_invalid_urls_offline(network, url):
    _, _, factory = network
    assert tools.webhook_send(url, {})["status"] == "error"
    factory.assert_not_called()


def test_limits_redirects_and_error_redaction(network, monkeypatch):
    _, response, factory = network
    assert tools.webhook_send("https://example.test", {"x": "a" * 65536})["status"] == "error"
    factory.assert_not_called()
    response.status_code = 302
    assert tools.webhook_send("https://example.test", {})["status"] == "error"
    response.status_code = 200
    monkeypatch.setattr(http, "MAX_RESPONSE_BYTES", 2)
    assert tools.webhook_send("https://example.test", {})["status"] == "error"
    response.raise_for_status.side_effect = requests.HTTPError("secret-token")
    result = tools.webhook_send("https://example.test", {})
    assert result["status"] == "error" and "secret-token" not in str(result)


def test_oauth_requires_host_registered_connector(network):
    _, _, factory = network
    assert tools.oauth_api_call("unknown", "GET", "https://api.example")["status"] == "error"
    factory.assert_not_called()


def test_oauth_origin_method_auth_and_token_redaction(network, tmp_path):
    session, response, factory = network
    config = oauth.OAuthConfig(
        "https://auth.example/authorize",
        "https://auth.example/token",
        "client",
        "https://app.example/callback",
        ("https://api.example",),
    )
    conn = oauth.GenericOAuthConnector("generic", config, token_dir=tmp_path)
    conn._save({"access_token": "secret-access", "refresh_token": "secret-refresh"})
    tools.CONNECTORS["generic"] = conn
    conn.access_token = Mock(wraps=conn.access_token)
    for method, url in [("POST", "https://api.example"), ("GET", "https://evil.example")]:
        assert tools.oauth_api_call("generic", method, url)["status"] == "error"
    conn.access_token.assert_not_called()
    factory.assert_not_called()
    response.iter_content.return_value = [b"secret-access secret-refresh safe"]
    result = tools.oauth_api_call("generic", "get", "https://api.example/items")
    assert result["status"] == "success"
    assert "secret-" not in str(result) and "safe" in str(result)
    assert session.request.call_args.kwargs["headers"]["Authorization"] == "Bearer secret-access"


@pytest.mark.parametrize("level", ["warning", "critical", "halt", "require_confirmation"])
def test_write_tools_omitted_by_breaker(level):
    names = {"webhook_send", "oauth_api_call"}
    assert names <= set(tool_registry.WRITE_TOOLS)
    assert names.isdisjoint(tool_registry.READ_TOOLS)
    assert names.isdisjoint({tool.tool_name for tool in tool_registry.get_tools(level)})
    assert names.isdisjoint({tool.tool_name for tool in ToolConfig().get_allowed_tools(level)})


def test_nxp_prepare_turn_enforces_write_filter():
    from stiracore.agent.nxp_agent import NXPAgent
    from stiracore.core import NeuralState, StiraEngine

    agent = object.__new__(NXPAgent)
    agent.engine = StiraEngine(NeuralState(stability=0.45))
    agent.config = agent.engine.config
    agent.tools = (tools.webhook_send, tools.oauth_api_call)
    agent.system_prompt_provider = None
    agent.system_prompt = "identity"
    breaker = agent.engine.pre_turn()
    allowed, _ = agent._prepare_turn(breaker)
    assert allowed == []
