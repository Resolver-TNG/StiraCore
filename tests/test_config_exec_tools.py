"""Settings/Tools views must report exec-class tools as they really are (#227, #163).

``config_api`` lists every registered tool; exec-class tools (``EXEC_TOOL_NAMES``)
are hidden from the agent unless ``STIRA_ENABLE_EXEC_TOOLS`` opts in, so the
views must show them apart and never as effective just because a switch is on.
"""

from __future__ import annotations

import pytest
from flask import Flask

from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.tools import tool_registry
from stiracore.tools.tool_config import ToolConfig

LIST_ENDPOINTS = ("/api/config/tools", "/api/tools/list")


class _Tool:
    def __init__(self, name: str) -> None:
        self.tool_name = name


@pytest.fixture
def fake_registry(monkeypatch):
    read = {"current_time": _Tool("current_time")}
    write_names = ["shell", "batch", "cron", "use_agent", "swarm", "graph", "think", "file_write"]
    write = {name: _Tool(name) for name in write_names}
    monkeypatch.setattr(tool_registry, "READ_TOOLS", read)
    monkeypatch.setattr(tool_registry, "WRITE_TOOLS", write)
    monkeypatch.delenv(tool_registry.EXEC_ENV_VAR, raising=False)
    return read, write


def _client(tmp_path, level="normal", mode="trust_all"):
    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    ToolConfig(
        mode=mode,
        enabled_tools=set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS),
        path=store.tool_path,
    ).save()
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: level))
    return app.test_client()


def _rows(client, endpoint):
    response = client.get(endpoint)
    assert response.status_code == 200, response.json
    return response.json, {row["name"]: row for row in response.json["tools"]}


@pytest.mark.parametrize("endpoint", LIST_ENDPOINTS)
def test_exec_tools_are_a_separate_category_and_never_effective_by_default(
    fake_registry, tmp_path, endpoint
):
    payload, rows = _rows(_client(tmp_path), endpoint)
    assert payload["exec_tools_enabled"] is False
    assert payload["exec_env_var"] == tool_registry.EXEC_ENV_VAR
    assert set(payload["exec_tool_names"]) == tool_registry.EXEC_TOOL_NAMES
    for name in tool_registry.EXEC_TOOL_NAMES:
        row = rows[name]
        assert row["category"] == "exec"
        assert row["exec_class"] is True
        # trust_all + every name enabled: configured, yet not exposed/effective.
        assert row["configured"] is True
        assert row["exposed"] is False
        assert row["enabled"] is False
        assert row["unavailable_reason"] == "exec_disabled"
    assert rows["file_write"]["category"] == "write"
    assert rows["file_write"]["enabled"] is True
    assert rows["current_time"]["category"] == "read"
    assert rows["current_time"]["enabled"] is True
    assert rows["file_write"]["unavailable_reason"] is None


@pytest.mark.parametrize("endpoint", LIST_ENDPOINTS)
def test_effective_rows_match_runtime_allowed_tools(fake_registry, tmp_path, endpoint):
    """The "effective" column equals what ToolConfig really hands to the agent."""
    client = _client(tmp_path)
    _payload, rows = _rows(client, endpoint)
    runtime = ToolConfig.load(tmp_path / "tool-config.json").get_allowed_tools("normal")
    assert {name for name, row in rows.items() if row["enabled"]} == {
        tool.tool_name for tool in runtime
    }


def test_env_opt_in_marks_exec_tools_exposed(fake_registry, tmp_path, monkeypatch):
    monkeypatch.setenv(tool_registry.EXEC_ENV_VAR, "1")
    payload, rows = _rows(_client(tmp_path), "/api/config/tools")
    assert payload["exec_tools_enabled"] is True
    assert rows["shell"]["exposed"] is True
    assert rows["shell"]["enabled"] is True
    assert rows["shell"]["category"] == "exec"


def test_exec_tools_blocked_at_warning_even_when_opted_in(fake_registry, tmp_path, monkeypatch):
    monkeypatch.setenv(tool_registry.EXEC_ENV_VAR, "1")
    _payload, rows = _rows(_client(tmp_path, level="warning"), "/api/config/tools")
    for name in tool_registry.EXEC_TOOL_NAMES:
        assert rows[name]["blocked_by_breaker"] is True
        assert rows[name]["enabled"] is False
    assert rows["current_time"]["enabled"] is True


def test_custom_mode_unconfigured_exec_tool_is_off(fake_registry, tmp_path, monkeypatch):
    monkeypatch.setenv(tool_registry.EXEC_ENV_VAR, "1")
    client = _client(tmp_path, mode="custom")
    client.post("/api/config/tools", json={"name": "shell", "enabled": False})
    _payload, rows = _rows(client, "/api/config/tools")
    assert rows["shell"]["configured"] is False
    assert rows["shell"]["enabled"] is False
    assert rows["batch"]["enabled"] is True


def test_real_registry_classifies_every_registered_exec_tool(tmp_path, monkeypatch):
    monkeypatch.delenv(tool_registry.EXEC_ENV_VAR, raising=False)
    _payload, rows = _rows(_client(tmp_path), "/api/config/tools")
    registered = (set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)) & (
        tool_registry.EXEC_TOOL_NAMES
    )
    assert {name for name, row in rows.items() if row["category"] == "exec"} == registered
    assert not any(rows[name]["enabled"] for name in registered)
