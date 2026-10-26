# Harness components

Requires `strands-agents>=1.57.1`. Only `register_spawn_tool` requires the optional
`pip install 'stiracore[harness]'` extra (`strands-harness>=0.1.2`).
The SDK owns orchestration, tool loading, MCP discovery, and snapshots.
Stira owns live NeuralState, breaker evaluation, and current identity injection.

## Temporary workers and specialists (#56 / #57)

```python
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.agent.subagent import create_nxp_subagent
from stiracore.agent.delegation import (
    register_spawn_tool,
    register_subagent_tool,
    create_nxp_swarm,
)

parent = NXPAgent("your-model-id", "us-east-1")
# Official make_subagent: fresh context/state per call, no tools by default.
register_spawn_tool(parent, max_depth=1)
worker = create_nxp_subagent(
    parent,
    "Review this design",
    name="reviewer",
    system_prompt="Review designs and report concrete risks.",
    on_complete=lambda child, result: print(child.name, str(result)),
)
register_subagent_tool(parent, worker)  # official Agent.as_tool
result = parent("Delegate a focused design review")
writer = create_nxp_subagent(parent, "Write a summary", name="writer")
swarm = create_nxp_swarm([worker, writer], entry_point=worker, max_iterations=4)
result = swarm("Review then summarize")
```

Children get fresh NeuralState/engine and copied configuration. Explicit initial
state is copied too. Parent history, prompt-provider closures, classifiers and
tools are not inherited implicitly. Delegation tools, including custom aliases,
are write-capable on the parent. Supply explicit child tools for specialists.

`worker()` executes its assigned task; `worker("another task")` continues that
child. Specialist registrations retain their own conversations; spawn constructs
a new worker each time. Completion callbacks run after post-turn updates, never
for failed/cancelled/interrupted invocations.

`child.agent` is a real SDK Agent with guarded streaming, usable as a Graph node
or Swarm specialist. SDK history/state resets cannot reset the live NXP breaker.
Hand-offs are write-capable and suppressed while the child's breaker restricts
writes. Separate children can run concurrently; one child cannot run concurrent
turns. Swarm node session managers are unsupported by the SDK.

## MCP discovery and classification

```python
from stiracore.tools.tool_registry import register_mcp_tools, close_mcp_tools, get_tools

# client is a configured official strands.tools.mcp.MCPClient.
register_mcp_tools(client, read_tool_names={"trusted_read_only_lookup"})
try:
    agent = NXPAgent("your-model-id", "us-east-1", tools=get_tools())
    agent("Use the available tools")
finally:
    close_mcp_tools(client)
```

Official ToolRegistry handles loading/names and MCPClient's ToolProvider handles
transport/discovery. Unclassified MCP tools default to WRITE, including dynamic
names absent from StiraConfig defaults. Name collisions are rejected, not
silently overwritten. Keep the provider registered for the whole lifetime of
agents using its tools. Close before rediscovery; registration is startup/shutdown
configuration, not a concurrent runtime mutation API.

## Session restoration

```python
from pathlib import Path
from strands.storage import LocalFileStorage
from stiracore.core import NeuralState
from stiracore.agent.state_persistence import StatePersistence
from stiracore.agent.session_manager import NXPSessionManager

state = NeuralState()
persistence = StatePersistence(state, Path("data/neural-state.json"))
manager = NXPSessionManager(
    session_id="conversation-1",
    storage=LocalFileStorage("data/sessions"),
    state_persistence=persistence,
    system_prompt_provider=lambda: Path("soul.md").read_text(),
)
agent = NXPAgent(
    "your-model-id",
    "us-east-1",
    state=state,
    session_manager=manager,
    agent_id="assistant",
)
agent("Remember our plan")
```

Reconstruct with the same session ID, agent ID and storage to continue history.
NXPAgent restores once, **before** its first breaker evaluation, regenerates the
prompt every turn and saves after `post_turn`. NXPAgent and StatePersistence must
share the same live state object. Snapshot `agent.state['nxp']` is a copy, never
the authoritative engine object. Latest NeuralState JSON wins over snapshots;
current SOUL/provider output wins over saved prompts.

NXPSessionManager also works directly with `Agent(session_manager=manager)`.
Automatic initialization and explicit `await manager.restore_snapshot(...)` both
reinject current disk state and provider output. Plain Agent hosts must still
supply their NXP lifecycle hooks and call `persistence.save_session(agent, manager)`
after post-turn updates. Omitting the provider opts out of prompt refresh; supply
it for SOUL protection. NXPAgent integration saves the latest snapshot at turn
boundaries; SDK hook-driven immutable snapshot triggers apply to directly attached
Agent managers, not the NXP wrapper's explicit sync path.

State and conversation persistence are ordered writes, not one distributed
transaction. State remains authoritative after a partial write failure.
