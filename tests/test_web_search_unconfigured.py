"""#583: ``web_search`` without ``STIRA_WEB_SEARCH_ENDPOINT`` is never offered to an agent.

Every call would fail the same way ("not configured"), and a model with no other search
tool kept calling it. Like ``memory_rate`` without memory autonomy (#554), the registry
keeps the name (its rule, class and YOLO tier stay as saved), the settings rows say
``not_configured`` and the agent's Tool Availability names the variable. With an endpoint
it is offered as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stiracore.tools import tool_availability, tool_profiles, tool_registry
from stiracore.tools.tool_config import ToolConfig

ENV = "STIRA_WEB_SEARCH_ENDPOINT"


def _names(tools) -> set[str]:
    return {getattr(tool, "tool_name", None) for tool in tools}


@pytest.fixture
def unset(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(ENV, "https://search.example/q")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_without_an_endpoint_no_agent_is_offered_web_search(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    assert tool_registry.web_search_configured() is False
    assert tool_registry.unconfigured_tools() == {"web_search": "not_configured"}
    assert "web_search" not in _names(tool_registry.get_tools("normal"))
    everything = ToolConfig(mode="trust_all", path=tmp_path / "tool-config.json")
    assert "web_search" not in _names(everything.get_allowed_tools("normal"))
    # The other network tool is unaffected, and the name stays known to the rules.
    assert "web_fetch" in _names(everything.get_allowed_tools("normal"))
    assert "web_search" in tool_registry.WRITE_TOOLS


def test_with_an_endpoint_it_is_offered_as_before(tmp_path, configured):
    assert tool_registry.web_search_configured() is True
    assert tool_registry.unconfigured_tools() == {}
    assert "web_search" in _names(tool_registry.get_tools("normal"))
    everything = ToolConfig(mode="trust_all", path=tmp_path / "tool-config.json")
    assert "web_search" in _names(everything.get_allowed_tools("normal"))
    # The breaker still hides it (a write-class tool) at WARNING.
    assert "web_search" not in _names(everything.get_allowed_tools("warning"))


def test_the_research_profile_keeps_web_search_ask_either_way(unset):
    """The saved rule applies once an endpoint is set; the profile table is unchanged."""
    assert tool_profiles.PROFILE_TABLES["research"]["web_search"] == "ask"
    tools = tool_profiles.profile_tools("research", {"enabled_tools": [], "ask_tools": []})
    assert "web_search" in tools["ask_tools"]


# --- the settings rows (GET /api/config/tools, /api/tools/list, the hub) ---------------------

flask = pytest.importorskip("flask")


def _client(tmp_path: Path):
    from flask import Flask

    from stiracore.observability.config_api import ConfigStore, create_config_blueprint

    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    ToolConfig(
        mode="custom",
        enabled_tools={"web_search", "web_fetch"},
        ask_tools={"web_search", "web_fetch"},
        path=store.tool_path,
    ).save()
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    return app.test_client()


def _rows(client):
    return {
        "config": client.get("/api/config/tools").get_json()["tools"],
        "list": client.get("/api/tools/list").get_json()["tools"],
        "hub": client.get("/api/config/hub").get_json()["tools"],
    }


def _row(rows, name):
    return next(row for row in rows if row["name"] == name)


def test_the_settings_rows_say_not_configured(tmp_path, unset):
    for route, rows in _rows(_client(tmp_path)).items():
        row = _row(rows, "web_search")
        assert row["policy"] == "ask", route  # the saved rule is kept
        assert row["enabled"] is False, route
        assert row["unavailable_reason"] == "not_configured", route
        assert row["effective"]["decision"] == "forbidden", route
        assert row["effective"]["reasons"] == ["not_configured"], route
        fetch = _row(rows, "web_fetch")
        assert (fetch["enabled"], fetch["unavailable_reason"]) == (True, None), route


def test_with_an_endpoint_the_rows_are_as_before(tmp_path, configured):
    for route, rows in _rows(_client(tmp_path)).items():
        row = _row(rows, "web_search")
        assert (row["enabled"], row["unavailable_reason"]) == (True, None), route
        assert row["effective"]["decision"] == "prompt", route


def test_tool_availability_names_the_variable(unset):
    text = tool_availability.section([], unbound={"web_search": tool_availability.NOT_CONFIGURED})
    line = next(line for line in text.splitlines() if line.startswith("- web_search:"))
    assert line.endswith("— not_configured [STIRA_WEB_SEARCH_ENDPOINT]")
    # The endpoint's value is never in the section (only the variable's name).
    assert "search.example" not in text
