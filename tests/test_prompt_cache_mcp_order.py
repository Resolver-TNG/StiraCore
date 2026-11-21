"""#254: the tool prefix is deterministic for MCP-discovered tools too (offline).

An MCP server may list its tools in any order, and the order can differ between
connections. While prompt caching is on, NXPAgent name-sorts the turn's tools, so the
cached tool prefix (Bedrock caches tools first) must not depend on that listing order.

No Bedrock call and no MCP transport: requests are captured at the boto3 client and the
provider is a stand-in for the SDK ``ToolProvider`` protocol (what ``MCPClient`` implements).
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from strands import tool
from strands.tools.registry import ToolProvider
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

from stiracore.agent import nxp_agent
from stiracore.agent.nxp_agent import _tool_name
from stiracore.agent.prompt_cache import PromptCacheSettings, sort_tools
from stiracore.agent.stimulus_classifier import RuleBasedClassifier
from stiracore.core import NeuralState
from stiracore.tools import tool_registry
from tests.test_prompt_cache import _bedrock

MCP_NAMES = ("mcp_zeta_lookup", "mcp_alpha_search", "mcp_mid_put")


class _McpTool(AgentTool):
    """Stand-in for an ``MCPAgentTool``: a name and a JSON schema, never executed."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> ToolSpec:
        return {
            "name": self._name,
            "description": f"MCP tool {self._name}",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                }
            },
        }

    @property
    def tool_type(self) -> str:
        return "fake-mcp"

    def stream(
        self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any
    ) -> ToolGenerator:
        raise NotImplementedError


class _McpServer(ToolProvider):
    """Lists its tools in the order it was given (the server's order)."""

    def __init__(self, names) -> None:
        self._tools = [_McpTool(name) for name in names]

    async def load_tools(self, **kwargs: Any) -> list[AgentTool]:
        return list(self._tools)

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        pass

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        pass


@tool
def beta_read(path: str) -> str:
    """Read something."""
    return path


@tool
def omega_read(path: str) -> str:
    """Read something else."""
    return path


@pytest.fixture(autouse=True)
def _isolate_registry():
    snapshot = (
        dict(tool_registry.READ_TOOLS),
        dict(tool_registry.WRITE_TOOLS),
        dict(tool_registry._MCP_REGISTRIES),
    )
    yield
    for target, saved in zip(
        (tool_registry.READ_TOOLS, tool_registry.WRITE_TOOLS, tool_registry._MCP_REGISTRIES),
        snapshot,
        strict=True,
    ):
        target.clear()
        target.update(saved)


def _spec_names(request) -> list[str]:
    return [t["toolSpec"]["name"] for t in request["toolConfig"]["tools"] if "toolSpec" in t]


def _run(tools, *, cache: bool):
    """Two turns on a Bedrock Claude model with a fixed system prompt; captured requests."""
    captured: list = []
    agent = nxp_agent.NXPAgent(
        model=_bedrock(captured),
        state=NeuralState(stability=0.9),
        tools=tools,
        system_prompt="fixed",
        stimulus_classifier=RuleBasedClassifier(),
        prompt_cache=PromptCacheSettings(enabled=cache),
    )
    agent("one")
    agent("two")
    return agent, captured


def test_mcp_listing_order_does_not_change_the_cached_tool_prefix():
    """``register_mcp_tools`` + caching: one tool prefix whatever order the server lists."""
    seen = []
    for listing in itertools.permutations(MCP_NAMES):
        provider = _McpServer(listing)
        registered = tool_registry.register_mcp_tools(provider, read_tool_names=MCP_NAMES)
        assert registered == list(listing)  # discovery itself keeps the server's order
        try:
            mcp_tools = [tool_registry.READ_TOOLS[name] for name in registered]
            # Registration order on purpose: MCP tools first, in the server's order.
            agent, captured = _run([*mcp_tools, omega_read, beta_read], cache=True)
        finally:
            tool_registry.close_mcp_tools(provider)
        first, second = captured
        assert first["toolConfig"] == second["toolConfig"]
        assert agent.last_prompt_cache["mode"] == "static"
        assert "tools" not in agent.last_prompt_cache["changed"]
        seen.append(
            (_spec_names(first), first["toolConfig"], agent.last_prompt_cache["tools_hash"])
        )

    names, tool_config, tools_hash = seen[0]
    # Name-sorted across MCP and local tools alike; the cache point closes the block.
    assert names == sorted([*MCP_NAMES, "beta_read", "omega_read"])
    assert tool_config["tools"][-1] == {"cachePoint": {"type": "default"}}
    assert all(item == (names, tool_config, tools_hash) for item in seen)


def test_without_caching_the_registration_order_is_kept():
    """Off (the default): nothing is sorted, as before #254."""
    listing = ("mcp_zeta_lookup", "mcp_alpha_search")
    provider = _McpServer(listing)
    registered = tool_registry.register_mcp_tools(provider, read_tool_names=listing)
    try:
        mcp_tools = [tool_registry.READ_TOOLS[name] for name in registered]
        agent, captured = _run([*mcp_tools, omega_read, beta_read], cache=False)
    finally:
        tool_registry.close_mcp_tools(provider)
    assert _spec_names(captured[0]) == [*listing, "omega_read", "beta_read"]
    assert agent.last_prompt_cache["mode"] == "off"


def test_an_undiscovered_provider_is_not_sorted_into_the_named_tools():
    """Known limit (docs/prompt-cache.md): ``sort_tools`` can only order what has a name.

    A ``ToolProvider`` handed over as a tool has none until the SDK expands it, so it
    follows the named tools in the order given, and its own tools keep the server's
    order. Discover MCP tools with ``register_mcp_tools`` to get a name-sorted prefix.
    """
    first, second = _McpServer(MCP_NAMES), _McpServer(reversed(MCP_NAMES))
    assert _tool_name(first) is None
    ordered = sort_tools([second, omega_read, first, beta_read], _tool_name)
    assert ordered == [beta_read, omega_read, second, first]
    _agent, captured = _run([_McpServer(MCP_NAMES), omega_read, beta_read], cache=True)
    assert _spec_names(captured[0]) == ["beta_read", "omega_read", *MCP_NAMES]
