"""Shorter model-facing descriptions for a few upstream tools (#368).

Every tool definition is sent to the model on every call. A handful of upstream
``strands_tools`` descriptions are long docstrings (examples, internals, history):
``swarm``, ``use_agent`` and ``graph`` alone are ~13k characters, most of the
exec-class tools' cost. Stira sends a short summary instead.

Only the top-level ``description`` changes. The name, the ``inputSchema`` (the
very same object: property names, types, defaults, required, property
descriptions) and the execution are the upstream tool's, so a call is made and
runs exactly as before (tests pin the schema identity). The wrapper keeps the
tool at its position in the list (cache-stable order, #254).

Shorter property descriptions (opt-in, #368 proposal 2): with
:func:`set_short_schemas` on (``tools_short_schemas`` in the gateway config, or the
``small`` / ``tiny`` context profile), the listed tools' ``inputSchema`` is sent with
shorter ``description`` strings for the listed properties. Nothing else in the schema
changes: property names, types, enums, defaults, ``required`` and nesting are the
upstream's (tests compare the schema with every ``description`` removed). Off (the
default) the schema is the upstream object itself, as before.
"""

from __future__ import annotations

import copy
from typing import Any

from strands.tools.tools import AgentTool

COMPACT_DESCRIPTIONS: dict[str, str] = {
    "current_time": (
        "Current date and time in ISO 8601 (e.g. 2023-04-15T14:32:16.123456+00:00). "
        "`timezone` (e.g. 'UTC', 'Asia/Tokyo'); default: the DEFAULT_TIMEZONE "
        "environment variable, else UTC."
    ),
    "shell": (
        "Run shell command(s) in a PTY. `command`: one string, a list run in order, or "
        "objects {command, timeout, work_dir}. `parallel`: run a list at once; "
        "`ignore_errors`: continue after a failure; `timeout`: seconds per command; "
        "`work_dir`: working directory. Returns the status and the commands' output."
    ),
    "cron": (
        "Manage the system crontab. `action`: 'list'; 'add' (`schedule` + `command`, "
        "optional `description` kept as a comment); 'remove' / 'edit' (`job_id` = the "
        "crontab line number); 'raw' (a raw crontab entry in `command`). `schedule` is "
        "a cron expression, e.g. '*/5 * * * *' (every 5 minutes), '0 8 * * *' (daily at "
        "08:00). Redirect a job's output to a log file ('>> /path/job.log 2>&1')."
    ),
    "think": (
        "Process a thought through `cycle_count` recursive thinking cycles (1-10), each "
        "building on the previous one, in a nested agent: for problems that need "
        "sustained reasoning (architecture decisions, trade-offs, open-ended "
        "ideation). The nested agent may call other tools, never think itself. Returns "
        "the concatenated output of all cycles."
    ),
    "use_agent": (
        "Run one nested agent on `prompt` with its own `system_prompt` and return its "
        "response, model information and metrics; it exists only for this call. "
        "`tools`: names from this agent's tool registry (default: all of them). "
        "`model_provider`: omit to use this agent's model; 'env' reads STRANDS_PROVIDER, "
        "STRANDS_MODEL_ID, ...; or a provider name (bedrock, anthropic, litellm, "
        "llamaapi, ollama, openai, github) with optional `model_settings`, e.g. "
        '{"model_id": "...", "params": {"temperature": 1}}.'
    ),
    "swarm": (
        "Run a team of agents on one `task` (Strands Swarm). Each entry of `agents` is "
        "one agent: `system_prompt` (its role) and optionally `name`, `tools` (names "
        "from this agent's tool registry; unknown names are dropped), `model_provider`, "
        "`model_settings`, `inherit_parent_prompt`. The agents hand off to each other "
        "through injected coordination tools (handoff_to_agent, complete_swarm_task) "
        "and share working memory. `max_handoffs`, `max_iterations`, "
        "`execution_timeout`, `node_timeout` and the repetitive-handoff settings bound "
        "the run. Returns the team's result."
    ),
    "graph": (
        "Create and run a deterministic multi-agent DAG (Strands Graph). `action`: "
        "'create' (`graph_id` + `topology`), 'execute' (`graph_id` + `task`: entry "
        "points receive the task, every other node its dependencies' outputs, in "
        "topological order), 'status' (`graph_id`), 'list', 'delete' (`graph_id`). "
        "`model_provider`, `model_settings` and `tools` are defaults for every node; "
        "a node may set its own. Returns the operation's result."
    ),
}


# Shorter property descriptions, used only while :func:`set_short_schemas` is on.
# Kept on purpose: limits and defaults (cycle_count 1-10, handoff/iteration/time bounds,
# redirects, timeouts), the token-variable rule of http_request (HTTP_REQUEST_TOKEN_CONFIG)
# and "verify_ssl ... may be restricted", which arguments each action needs, and the shape
# of nested objects (graph topology, swarm agent entries).
SHORT_PROPERTY_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "http_request": {
        "method": "HTTP method (GET, POST, PUT, DELETE, ...)",
        "url": "Request URL",
        "auth_type": "Authentication type",
        "auth_token": "Auth token (if omitted, auth_env_var is used when configured)",
        "auth_env_var": (
            "Environment variable holding the token; it must be listed in "
            "HTTP_REQUEST_TOKEN_CONFIG with a domain that matches the URL"
        ),
        "headers": "Headers (key-value pairs)",
        "body": "Request body",
        "verify_ssl": "Verify TLS certificates (disabling may be restricted)",
        "cookie": "Cookie file to send",
        "cookie_jar": "Cookie jar file to save cookies to",
        "session_config": "Session options",
        "metrics": "Collect request metrics",
        "streaming": "Stream the response",
        "allow_redirects": "Follow redirects (default true)",
        "max_redirects": "Maximum redirects (default 30)",
        "timeout": "Timeout in seconds (default 30)",
        "convert_to_markdown": "Convert an HTML response to Markdown (default false)",
        "aws_auth": "SigV4 settings",
        "basic_auth": "Basic auth credentials",
        "digest_auth": "Digest auth credentials",
        "jwt_config": "JWT settings",
    },
    "think": {
        "thought": "The question, problem or idea to think through.",
        "cycle_count": "Thinking cycles, 1-10 (3-5 is typical; more is slower and costlier).",
        "system_prompt": "Who the thinking agent is (persona, role, expertise).",
        "tools": "Tool names from this agent's registry (default: all of them).",
        "model_provider": (
            'bedrock, anthropic, litellm, llamaapi, ollama, openai, github, or "env" '
            "(default: this agent's model)."
        ),
        "model_settings": (
            'e.g. {"model_id": "...", "params": {"temperature": 1}} (default: the provider\'s).'
        ),
        "thinking_system_prompt": "How it thinks (method), unlike system_prompt (who it is).",
    },
    "use_agent": {
        "prompt": "The prompt for the nested agent.",
        "system_prompt": "The nested agent's system prompt.",
        "tools": "Tool names from this agent's registry (default: all of them).",
        "model_provider": (
            'Omit for this agent\'s model; "env" reads environment variables; or bedrock, '
            "anthropic, litellm, llamaapi, ollama, openai, github."
        ),
        "model_settings": (
            'e.g. {"model_id": "...", "params": {"temperature": 1}} (default: the provider\'s).'
        ),
    },
    "swarm": {
        "task": "The task for the team.",
        "agents": (
            "One entry per agent: system_prompt (its role) and optional name, tools "
            "(tool names), model_provider, model_settings, inherit_parent_prompt (bool)."
        ),
        "max_handoffs": "Maximum handoffs between agents (default 20).",
        "max_iterations": "Maximum iterations across all agents (default 20).",
        "execution_timeout": "Maximum total time in seconds (default 900).",
        "node_timeout": "Maximum time per agent in seconds (default 300).",
        "repetitive_handoff_detection_window": (
            "Recent handoffs checked for repetition (default 8)."
        ),
        "repetitive_handoff_min_unique_agents": (
            "Minimum distinct agents in that window (default 3)."
        ),
    },
    "graph": {
        "action": '"create", "execute", "status", "list" or "delete".',
        "graph_id": "The graph's id (every action but list).",
        "topology": (
            'For create: {"nodes": [{"id", "role", "system_prompt", optional '
            '"model_provider", "model_settings", "tools"}], "edges": [{"from", "to"}], '
            'optional "entry_points" (auto-detected)}.'
        ),
        "task": "For execute: the task.",
        "model_provider": (
            "Default provider for every node (bedrock, anthropic, litellm, ollama, "
            "openai, ...); a node may set its own."
        ),
        "model_settings": 'Default model settings, e.g. {"model_id": "..."}; a node may set its own.',
        "tools": "Default tool names for every node; a node may set its own.",
    },
    "shell": {
        "command": "One command string, or a list of commands.",
        "parallel": "Run a list in parallel (default false).",
        "ignore_errors": "Continue after a failed command (default false).",
        "timeout": "Seconds per command (default: SHELL_DEFAULT_TIMEOUT).",
        "work_dir": "Working directory (default: the current one).",
    },
    "cron": {
        "action": "'list', 'add', 'remove', 'edit' or 'raw' (raw: a crontab entry in command).",
        "schedule": "Cron expression, e.g. '*/5 * * * *' (every 5 minutes).",
        "command": "The command to schedule.",
        "job_id": "Crontab line number (remove / edit).",
        "description": "Comment for the job (optional).",
    },
}

_SHORT_SCHEMAS = {"on": False}


def set_short_schemas(on: bool) -> None:
    """Process-wide: send the shorter property descriptions (read at start-up, #368)."""
    _SHORT_SCHEMAS["on"] = on is True


def short_schemas_enabled() -> bool:
    return _SHORT_SCHEMAS["on"]


def shorten_schema(schema: Any, descriptions: dict[str, str]) -> Any:
    """A copy of ``schema`` with the listed top-level properties' descriptions replaced.

    Only an existing string ``description`` is replaced (a property without one, or a
    name the schema does not have, is left as it is); nothing is added or removed.
    """
    if not isinstance(schema, dict):
        return schema
    out = copy.deepcopy(schema)
    body = out.get("json") if isinstance(out.get("json"), dict) else out
    properties = body.get("properties") if isinstance(body, dict) else None
    if not isinstance(properties, dict):
        return out
    for name, text in descriptions.items():
        prop = properties.get(name)
        if isinstance(prop, dict) and isinstance(prop.get("description"), str):
            prop["description"] = text
    return out


class CompactSpecTool(AgentTool):
    """The wrapped tool with a shorter ``description``; everything else delegated."""

    def __init__(
        self,
        wrapped: AgentTool,
        description: str | None,
        properties: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.wrapped = wrapped
        self._description = description  # None: the upstream description
        self._properties = properties or {}
        self._short_schema: tuple[int, Any] | None = None  # (id of upstream, short copy)

    @property
    def tool_name(self) -> str:
        return self.wrapped.tool_name

    @property
    def tool_spec(self) -> Any:
        spec = self.wrapped.tool_spec
        # A shallow copy: the inputSchema is the upstream object itself (switch off).
        out = dict(spec)
        if self._description is not None:
            out["description"] = self._description
        if self._properties and _SHORT_SCHEMAS["on"] and "inputSchema" in spec:
            upstream = spec["inputSchema"]
            cached = self._short_schema
            if cached is None or cached[0] != id(upstream):
                cached = (id(upstream), shorten_schema(upstream, self._properties))
                self._short_schema = cached
            out["inputSchema"] = cached[1]
        return out

    @property
    def tool_type(self) -> str:
        return self.wrapped.tool_type

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        events = self.wrapped.stream(tool_use, invocation_state, **kwargs)
        try:
            async for event in events:
                yield event
        finally:
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                await aclose()


def compact(tools: dict[str, Any]) -> None:
    """Swap in the short description for the listed tools, in place (order kept)."""
    for name in (*COMPACT_DESCRIPTIONS, *SHORT_PROPERTY_DESCRIPTIONS):
        tool = tools.get(name)
        if isinstance(tool, AgentTool) and not isinstance(tool, CompactSpecTool):
            tools[name] = CompactSpecTool(
                tool, COMPACT_DESCRIPTIONS.get(name), SHORT_PROPERTY_DESCRIPTIONS.get(name)
            )
