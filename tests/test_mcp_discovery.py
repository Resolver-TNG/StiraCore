"""Tests for MCP tool discovery: classification, collisions, and lifecycle."""

from typing import Any

import pytest
from strands.tools.registry import ToolProvider
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

from stiracore.tools import tool_registry


class _FakeAgentTool(AgentTool):
    """Minimal AgentTool double standing in for an MCPAgentTool."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> ToolSpec:
        return {"name": self._name, "description": self._name, "inputSchema": {"json": {}}}

    @property
    def tool_type(self) -> str:
        return "fake-mcp"

    def stream(
        self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any
    ) -> ToolGenerator:
        raise NotImplementedError


class _FakeMcpProvider(ToolProvider):
    """A stand-in MCP connection implementing the official ToolProvider protocol."""

    def __init__(self, tool_names: list[str]) -> None:
        self._tools = [_FakeAgentTool(name) for name in tool_names]
        self.consumers: set[Any] = set()
        self.stopped = False

    async def load_tools(self, **kwargs: Any) -> list[AgentTool]:
        return list(self._tools)

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.consumers.add(consumer_id)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.consumers.discard(consumer_id)
        if not self.consumers:
            self.stopped = True


@pytest.fixture(autouse=True)
def _clean_mcp_state():
    """Isolate READ_TOOLS/WRITE_TOOLS/_MCP_REGISTRIES mutations per test."""
    read_snapshot = dict(tool_registry.READ_TOOLS)
    write_snapshot = dict(tool_registry.WRITE_TOOLS)
    registries_snapshot = dict(tool_registry._MCP_REGISTRIES)
    yield
    tool_registry.READ_TOOLS.clear()
    tool_registry.READ_TOOLS.update(read_snapshot)
    tool_registry.WRITE_TOOLS.clear()
    tool_registry.WRITE_TOOLS.update(write_snapshot)
    tool_registry._MCP_REGISTRIES.clear()
    tool_registry._MCP_REGISTRIES.update(registries_snapshot)


def test_register_mcp_tools_requires_official_tool_provider() -> None:
    with pytest.raises(TypeError):
        tool_registry.register_mcp_tools(object())


def test_unknown_mcp_tools_default_to_write() -> None:
    provider = _FakeMcpProvider(["mcp_search"])

    registered = tool_registry.register_mcp_tools(provider)

    assert registered == ["mcp_search"]
    assert "mcp_search" in tool_registry.WRITE_TOOLS
    assert "mcp_search" not in tool_registry.READ_TOOLS

    tool_registry.close_mcp_tools(provider)


def test_explicitly_named_mcp_tools_are_read() -> None:
    provider = _FakeMcpProvider(["mcp_lookup"])

    registered = tool_registry.register_mcp_tools(provider, read_tool_names=["mcp_lookup"])

    assert registered == ["mcp_lookup"]
    assert "mcp_lookup" in tool_registry.READ_TOOLS
    assert "mcp_lookup" not in tool_registry.WRITE_TOOLS

    tool_registry.close_mcp_tools(provider)


def test_mcp_tool_cannot_escalate_existing_read_tool_to_write() -> None:
    tool_registry.READ_TOOLS["file_read"] = object()
    provider = _FakeMcpProvider(["file_read"])

    registered = tool_registry.register_mcp_tools(provider)

    assert registered == []
    assert "file_read" not in tool_registry.WRITE_TOOLS
    assert isinstance(tool_registry.READ_TOOLS["file_read"], object)

    tool_registry.close_mcp_tools(provider)


def test_mcp_tool_cannot_move_existing_write_tool_to_read() -> None:
    tool_registry.WRITE_TOOLS["shell"] = object()
    provider = _FakeMcpProvider(["shell"])

    registered = tool_registry.register_mcp_tools(provider, read_tool_names=["shell"])

    assert registered == []
    assert "shell" not in tool_registry.READ_TOOLS

    tool_registry.close_mcp_tools(provider)


def test_close_mcp_tools_unregisters_and_releases_connection() -> None:
    provider = _FakeMcpProvider(["mcp_write_thing"])
    tool_registry.register_mcp_tools(provider)
    assert "mcp_write_thing" in tool_registry.WRITE_TOOLS

    tool_registry.close_mcp_tools(provider)

    assert "mcp_write_thing" not in tool_registry.WRITE_TOOLS
    assert provider.stopped is True


def test_close_mcp_tools_leaves_rejected_collisions_untouched() -> None:
    sentinel = object()
    tool_registry.READ_TOOLS["file_read"] = sentinel
    provider = _FakeMcpProvider(["file_read"])
    tool_registry.register_mcp_tools(provider)

    tool_registry.close_mcp_tools(provider)

    assert tool_registry.READ_TOOLS["file_read"] is sentinel


def test_close_mcp_tools_is_idempotent() -> None:
    provider = _FakeMcpProvider(["mcp_idempotent"])
    tool_registry.register_mcp_tools(provider)

    tool_registry.close_mcp_tools(provider)
    tool_registry.close_mcp_tools(provider)  # must not raise

    assert "mcp_idempotent" not in tool_registry.WRITE_TOOLS


def test_registered_mcp_tools_reach_actual_strands_registry() -> None:
    from unittest.mock import Mock

    from strands import Agent

    provider = _FakeMcpProvider(["mcp_agent_reachable"])
    tool_registry.register_mcp_tools(provider)
    try:
        agent = Agent(model=Mock(), tools=tool_registry.get_tools(), callback_handler=None)
        assert "mcp_agent_reachable" in agent.tool_names
    finally:
        tool_registry.close_mcp_tools(provider)


def test_duplicate_provider_registration_cannot_leak_a_consumer():
    provider = _FakeMcpProvider(["mcp_repeat"])
    tool_registry.register_mcp_tools(provider)
    with pytest.raises(ValueError, match="already registered"):
        tool_registry.register_mcp_tools(provider)
    assert len(provider.consumers) == 1
    tool_registry.close_mcp_tools(provider)
    assert provider.stopped


def test_same_classification_collision_preserves_existing_tool():
    existing = object()
    tool_registry.WRITE_TOOLS["existing_writer"] = existing
    provider = _FakeMcpProvider(["existing_writer"])
    assert tool_registry.register_mcp_tools(provider) == []
    assert tool_registry.WRITE_TOOLS["existing_writer"] is existing
    assert provider.stopped


def test_nxp_breaker_blocks_dynamic_mcp_writes(monkeypatch):
    from unittest.mock import Mock

    from stiracore.agent.nxp_agent import NXPAgent
    from stiracore.core import NeuralState

    monkeypatch.setattr("stiracore.agent.nxp_agent.BedrockModel", Mock())
    provider = _FakeMcpProvider(["unlisted_dynamic_write"])
    tool_registry.register_mcp_tools(provider)
    wrapper = NXPAgent(
        "offline",
        "us-east-1",
        tools=tool_registry.get_tools(),
        state=NeuralState(stability=0.4),
    )
    allowed, _ = wrapper._prepare_turn(wrapper.engine.pre_turn())
    assert "unlisted_dynamic_write" not in {tool.tool_name for tool in allowed}
    wrapper.engine.post_turn(stimulus=None)
    tool_registry.close_mcp_tools(provider)
