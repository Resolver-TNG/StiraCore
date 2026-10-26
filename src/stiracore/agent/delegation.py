"""Official Strands delegation adapters retaining NXP isolation (#56/#57)."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from strands.multiagent import Swarm

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.agent.subagent import NXPSubagent, create_nxp_subagent


def _register_delegation(parent: NXPAgent, delegate: Any) -> Any:
    """Delegation is write-capable regardless of the specialist's current tools."""
    if any(getattr(tool, "tool_name", None) == delegate.tool_name for tool in parent.tools):
        raise ValueError(f"Delegation tool name already registered: {delegate.tool_name}")
    parent.config = deepcopy(parent.config)
    parent.engine.config = parent.config
    parent.config.blocked_tool_names |= frozenset({delegate.tool_name})
    parent.tools = (*parent.tools, delegate)
    return delegate


def register_subagent_tool(
    parent: NXPAgent,
    child: NXPSubagent,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Any:
    """Register an official Agent.as_tool adapter with the parent's breaker.

    The child is persistent within this registration (but independent of the
    parent). Use register_spawn_tool for fresh per-call workers instead.
    """
    delegate = child.agent.as_tool(
        name=name or child.name,
        description=description,
        preserve_context=True,
    )
    return _register_delegation(parent, delegate)


def register_spawn_tool(
    parent: NXPAgent,
    *,
    name: str = "subagent",
    max_depth: int = 1,
    system_prompt: str = "Complete the delegated task and return a self-contained result.",
    on_complete: Any = None,
) -> Any:
    """Use Harness make_subagent for tool-free, fresh-context temporary workers.

    Tool grants are deliberately fixed empty: callers needing privileged
    specialists register explicit children with register_subagent_tool instead.
    The SDK transports results, errors, cancellation and recursion depth.
    """

    try:
        from strands_harness.tools.subagent import Fixed, make_subagent
    except ImportError as exc:
        raise ImportError("register_spawn_tool requires stiracore[harness]") from exc

    def builder(spec):
        return create_nxp_subagent(
            parent,
            spec.task,
            name=name,
            system_prompt=system_prompt,
            on_complete=on_complete,
        ).agent

    delegate = make_subagent(
        builder=builder,
        instructions=Fixed(system_prompt),
        tools=Fixed([]),
        mcp_servers=Fixed([]),
        context=Fixed("none"),
        max_depth=max_depth,
        name=name,
    )
    return _register_delegation(parent, delegate)


def create_nxp_swarm(
    children: Iterable[NXPSubagent],
    *,
    entry_point: NXPSubagent | None = None,
    **kwargs: Any,
) -> Swarm:
    """Build an official Swarm over guarded SDK agents, not bare inner agents.

    Each specialist keeps its own live NXP engine across Swarm history resets.
    The returned SDK Swarm supports sync/async/streaming calls and native limits.
    """
    children = list(children)
    if not children:
        raise ValueError("At least one specialist is required")
    if entry_point is not None and entry_point not in children:
        raise ValueError("entry_point must be one of the specialists")
    return Swarm(
        [child.agent for child in children],
        entry_point=entry_point.agent if entry_point is not None else None,
        **kwargs,
    )
