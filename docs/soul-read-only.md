# SOUL / identity read-only policy

SOUL.md and IPD.md are read-only through MemoryStore and agent file mutation
surfaces at every breaker level. Default basenames are case-insensitive and
protected at any directory depth. Reads remain available, including the standard
editor's `view` command.

## Host configuration

`AgentConfig.read_only_paths` is a JSON array of additional paths. Defaults cannot
be removed by setting an empty array. `soul_path`, when supplied, is automatically
included even if its basename is not SOUL.md. For example:

```json
{
  "soul_path": "personas/assistant.md",
  "read_only_paths": ["identity/anchors.md"]
}
```

`resolve_config(...).read_only_paths` exposes the effective tuple. The chat server loads
this configuration at startup and passes absolute paths to its store and agent.
Changes require recreating/restarting that application; the policy is not a
model-controlled tool argument.

Library callers pass the effective paths explicitly:

```python
policy = resolve_config(agent=AgentConfig.load()).read_only_paths
agent = create_nxp_agent(model_id, region_name, read_only_paths=policy)
tools = get_tools(read_only_paths=policy)
```

For `NXPAgent`, `IdentityGuard`, and `get_tools`, relative paths are anchored to the
working directory when the policy is constructed. For `MemoryStore(...,
read_only_paths={...})`, relative additions are rooted at that store's agent memory
directory, e.g. `episodic/anchor.md`. Use absolute paths when sharing a policy
between both surfaces. Store writes and episodic deletion raise `ValueError`
before mutation; the agent tools return an error result with:

> SOUL/identity files are read-only and cannot be modified by the agent

## Enforcement and boundaries

- MemoryStore checks its write and delete operations; neither Markdown nor the
  search index is changed by a denied mutation.
- NXPAgent installs IdentityGuard as an SDK `BeforeToolCallEvent` hook on every
  turn. Checks occur **before tool execution**, not from assistant prose or only
  once at the start of a turn. Streaming children install the same hook and
  inherit a copied set of parent restrictions.
- Standard `file_write` and `editor` are additionally wrapped at their SDK stream
  boundary, including direct tool dispatch without an NXPAgent.
- Exact paths resolve `..`, `~`, and symlinks, and compare existing hard links.
  Legacy glob policies and literal shell/Python command detection remain supported.

This is an application-level boundary, **not an OS sandbox**. Arbitrary code,
computed shell paths, nested third-party executors that bypass hooks, and
concurrent filesystem replacement are not made safe by lexical inspection.
Use OS read-only mounts/permissions and restricted execution for those threats.
Host-owned Python objects and configuration are trusted; agents must not be given
a tool that can mutate the live policy. Humans can still edit identity files
outside these agent-facing APIs.
