"""Integration tests for the Config API shared by both dashboard servers."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from dashboard import app as observer_server
from dashboard import stira_server
from stiracore.agent import nxp_agent
from stiracore.agent.stimulus_classifier import StimulusResult
from stiracore.core import BreakLevel, NeuralState
from stiracore.observability.config_api import ConfigStore
from stiracore.tools import tool_registry
from stiracore.tools.tool_config import level_name

CONFIG_GET_ENDPOINTS = (
    "/api/config",
    "/api/config/models",
    "/api/config/jev",
    "/api/config/tools",
    "/api/config/soul",
)
TOOL_LIST_ENDPOINTS = ("/api/config/tools", "/api/tools/list")
CANONICAL_BREAKER_LEVELS = {level_name(level) for level in BreakLevel}


def _config_store(root) -> ConfigStore:
    return ConfigStore(
        root / "gateway-config.json",
        root / "agent-config.json",
        root / "tool-config.json",
    )


@pytest.fixture
def dashboard_app_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))

    def build_agent(**kwargs):
        return Mock(
            return_value="offline reply",
            messages=list(kwargs.get("messages") or []),
            system_prompt=kwargs.get("system_prompt", ""),
        )

    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=build_agent))
    from stiracore.agent import stimulus_classifier

    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None,
            classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5)),
        ),
    )

    build_count = 0

    def build(stability=0.9):
        nonlocal build_count
        build_count += 1
        root = tmp_path / f"apps-{build_count}"
        stira_data = root / "stira-data"
        stira_data.mkdir(parents=True)
        monkeypatch.setattr(stira_server, "DATA_DIR", stira_data)

        observer_root = root / "observer-config"
        stira_root = root / "stira-config"
        observer_state_path = root / "observer-state.json"
        stira_state_path = stira_data / "neural-state.json"
        NeuralState(stability=stability).save(observer_state_path)
        NeuralState(stability=stability).save(stira_state_path)

        observer = observer_server.create_app(
            state_path=observer_state_path,
            gateway_config_path=observer_root / "gateway-config.json",
            agent_config_path=observer_root / "agent-config.json",
            tool_config_path=observer_root / "tool-config.json",
        )
        stira = stira_server.create_app(config_store=_config_store(stira_root))
        return observer, stira

    return build


@pytest.fixture
def dashboard_apps(dashboard_app_factory):
    return dashboard_app_factory()


def test_shared_navigation_routes_exist_and_pages_render(dashboard_apps):
    observer, stira = dashboard_apps
    required = {"/chat", "/tools", "/settings"}

    for app in (observer, stira):
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        assert required <= rules
        client = app.test_client()
        assert client.get("/settings").status_code == 200
        # One tool list only (#227): the old URL lands on the settings hub.
        response = client.get("/tools")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/settings#sec-tools")


def test_config_get_endpoints_match_between_servers(dashboard_apps):
    observer, stira = dashboard_apps
    observer_client = observer.test_client()
    stira_client = stira.test_client()

    for endpoint in CONFIG_GET_ENDPOINTS:
        observer_response = observer_client.get(endpoint)
        stira_response = stira_client.get(endpoint)
        assert observer_response.status_code == 200, endpoint
        assert stira_response.status_code == 200, endpoint
        # The chat server additionally reports the live classifier (#162); compare the shared part.
        runtime_only = {
            "effective",
            "effective_matches_config",
            "restart_required",
            "startup_choice",
            # Compares the saved values with the running engine's (#272); the observer has none.
            "stira_config_apply",
        }
        observer_json = {k: v for k, v in observer_response.json.items() if k not in runtime_only}
        stira_json = {k: v for k, v in stira_response.json.items() if k not in runtime_only}
        if "tools" in stira_json:
            # #554: only the chat server knows which registry tools it did not bind
            # (memory_rate without memory_autonomy); the observer binds none, knows none.
            unbound = {
                row["name"]
                for row in stira_json["tools"]
                if row["unavailable_reason"] == "not_configured"
            }
            # #583: web_search without an endpoint is not configured on both servers alike
            # (compared as is below).
            assert unbound == {"memory_rate", *tool_registry.unconfigured_tools()}, endpoint
            for data in (observer_json, stira_json):
                data["tools"] = [row for row in data["tools"] if row["name"] != "memory_rate"]
        assert observer_json == stira_json, endpoint


@pytest.mark.parametrize("endpoint", TOOL_LIST_ENDPOINTS)
def test_stira_breaker_level_is_canonical(dashboard_apps, endpoint):
    _observer, stira = dashboard_apps

    response = stira.test_client().get(endpoint)

    assert response.status_code == 200
    assert response.json["breaker_level"] in CANONICAL_BREAKER_LEVELS
    assert "breakresult" not in response.json["breaker_level"]


@pytest.mark.parametrize(
    ("stability", "expected_level"),
    ((0.9, "normal"), (0.4, "warning"), (0.2, "critical")),
)
def test_breaker_tool_contract_matches_between_servers(
    dashboard_app_factory, stability, expected_level
):
    observer, stira = dashboard_app_factory(stability=stability)
    observer_client = observer.test_client()
    stira_client = stira.test_client()

    for endpoint in TOOL_LIST_ENDPOINTS:
        observer_payload = observer_client.get(endpoint).get_json()
        stira_payload = stira_client.get(endpoint).get_json()

        assert observer_payload["breaker_level"] == expected_level
        assert stira_payload["breaker_level"] == expected_level
        assert {tool["name"]: tool["blocked_by_breaker"] for tool in observer_payload["tools"]} == {
            tool["name"]: tool["blocked_by_breaker"] for tool in stira_payload["tools"]
        }


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    (
        ("/api/config/models", {}),
        ("/api/config/jev", {}),
        ("/api/config/tools", {}),
    ),
)
def test_config_validation_matches_between_servers(dashboard_apps, endpoint, payload):
    observer, stira = dashboard_apps
    responses = [app.test_client().post(endpoint, json=payload) for app in (observer, stira)]
    assert responses[0].status_code == responses[1].status_code == 400
    assert responses[0].json == responses[1].json


def test_injected_config_stores_remain_independent(dashboard_apps):
    observer, stira = dashboard_apps
    observer_client = observer.test_client()
    stira_client = stira.test_client()
    target = observer_client.get("/api/config/models").json["models"][0]["id"]

    changed = observer_client.post("/api/config/models", json={"model_id": target})

    assert changed.status_code == 200
    assert observer_client.get("/api/config/models").json["agent_override"] == target
    assert stira_client.get("/api/config/models").json["agent_override"] is None
