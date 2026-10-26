"""Standard Strands tools: allow all normally; let Stira block write tools."""

import logging
import os
import sys
from collections.abc import Iterable, Mapping
from importlib import import_module
from types import ModuleType
from typing import Any

from strands.tools.registry import ToolProvider as _SdkToolProvider
from strands.tools.registry import ToolRegistry as _SdkToolRegistry
from strands.tools.tools import AgentTool

from .compact_specs import compact as _compact_descriptions
from .exec_gate import ExecGatedTool
from .file_editor_tool import EDITOR_TOOL_NAME, make_stira_editor
from .file_tools import (
    READ_FLOOR_MODULES,
    file_read,
    install_read_floor_opener,
    read_floor_opener_live,
    view_image,
)
from .http_consent import HttpConsentGatedTool
from .identity_tools import IdentityProtectedTool, WorkspaceAnchoredReadTool
from .integration_tools import oauth_api_call, webhook_send
from .lsp_tools import lsp_diagnostics, lsp_hover
from .memory_rate_tool import MEMORY_RATE_TOOL_NAME, unbound_memory_rate
from .os_protection import OsProtectionGuardTool, is_command_tool
from .own_dashboard_gate import URL_ARGUMENTS as _OWN_DASHBOARD_URL_TOOLS
from .own_dashboard_gate import OwnDashboardGuardedTool, install_registered_redirect_check
from .page_check import browser_status as _browser_status
from .page_check import page_check
from .procedure_tool import PROPOSE_PROCEDURE_TOOL_NAME, unbound_propose_procedure
from .run_checks import run_checks as _run_checks_tool
from .search_tools import perplexity_configured, perplexity_search
from .tool_consent import CONSENT_TOOL_MODULES, EXEC_TOOLS, bridge_refusal, install_consent_bridge
from .web_tools import web_fetch, web_search

_LOG = logging.getLogger(__name__)
READ_TOOLS: dict[str, Any] = {}
WRITE_TOOLS: dict[str, Any] = {}
_WRITE_BLOCKED_LEVELS = frozenset(
    {
        "warning",
        "critical",
        "emergency",
        "halt",
        "halted",
        "block_writes",
        "require_confirmation",
    }
)


# Tools that can run arbitrary commands or spawn tool-using agents. They are the
# "exec" class: hidden unless explicitly enabled until the approval-policy
# runner of the exec-tools epic (#167) exists. "batch", "cron", "use_agent",
# "swarm" and "graph" can reach "shell" indirectly, so they are gated together.
# "think" re-runs the parent's tools inside a bare inner Agent that has none of
# the NXP hooks (IdentityGuard, HITL), so it bypasses hook-level policy (#145).
# When exposed, each runs only inside a human approval of that exact call (#362,
# ``ExecGatedTool``); a tool whose consent bridge cannot be confirmed is dropped.
EXEC_TOOL_NAMES = EXEC_TOOLS
EXEC_ENV_VAR = "STIRA_ENABLE_EXEC_TOOLS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def exec_tools_enabled() -> bool:
    """Whether exec-class tools are exposed (explicit opt-in via environment)."""
    return os.environ.get(EXEC_ENV_VAR, "").strip().lower() in _TRUTHY


def is_exec_tool(name: str) -> bool:
    return name in EXEC_TOOL_NAMES


# #583: ``web_search`` needs a configured endpoint. Without one it stays in the registry
# (its rule, class and YOLO tier are kept, and the settings page lists it as not
# configured), but no agent is offered it: every call would fail the same way.
WEB_SEARCH_ENV_VAR = "STIRA_WEB_SEARCH_ENDPOINT"
UNCONFIGURED = "not_configured"


def web_search_configured() -> bool:
    """Whether ``STIRA_WEB_SEARCH_ENDPOINT`` is set (nonblank), read at each call."""
    return bool(os.environ.get(WEB_SEARCH_ENV_VAR, "").strip())


def unconfigured_tools() -> dict[str, str]:
    """``name -> "not_configured"`` of registry tools that need a setting this process
    lacks (now: ``web_search`` without an endpoint). They are never handed to an agent."""
    return {} if web_search_configured() else {"web_search": UNCONFIGURED}


def _standard_memory_configured() -> bool:
    """Return whether the Bedrock Knowledge Base-backed tool is usable.

    The standard ``memory`` tool is an optional, Bedrock-only feature (#207).
    Stira's own MemoryStore tools (``memory_read``/``memory_search``/...) are the
    provider-neutral source of truth and never need AWS.
    """
    knowledge_base_id = os.environ.get("STRANDS_KNOWLEDGE_BASE_ID")
    return bool(knowledge_base_id and knowledge_base_id.strip())


def writes_allowed(breaker_level: Any = None) -> bool:
    """Return whether write-capable tools may be exposed at this breaker level."""
    level = getattr(breaker_level, "name", None)
    if level is None:
        level = getattr(breaker_level, "value", breaker_level)
    normalized = str(level).rsplit(".", 1)[-1].strip().lower()
    return normalized not in _WRITE_BLOCKED_LEVELS


def _register(
    target: dict[str, Any],
    tool: Any,
    *,
    name_hint: str | None = None,
    allow_function_name: bool = True,
) -> bool:
    """Let the official SDK normalize tools and supply their registered names.

    Keep the legacy private signature, but never attach synthetic metadata to
    bare callables or modules. The registered SDK AgentTool is authoritative.
    """
    if tool is None:
        return False
    if not allow_function_name and not isinstance(tool, (AgentTool, ModuleType)):
        return False
    registry = _SdkToolRegistry()
    try:
        names = registry.process_tools([tool])
    except ValueError:
        _LOG.warning("Could not load tool %s through Strands", name_hint or type(tool).__name__)
        return False
    target.update({name: registry.registry[name] for name in names})
    return bool(names)


def _load_standard_tool(target: dict[str, Any], name: str) -> bool:
    """Import one intended standard tool and make registration failures visible."""
    if name == "memory" and not _standard_memory_configured():
        _LOG.info("Skipping strands_tools.memory: STRANDS_KNOWLEDGE_BASE_ID is not configured")
        return False
    try:
        module = import_module(f"strands_tools.{name}")
    except ImportError as exc:
        _LOG.warning("Skipping strands_tools.%s: %s", name, exc)
        return False

    # Legacy tools keep TOOL_SPEC on the module, not on the bare function.
    # Pass that module to Strands so it retains the input schema and executor;
    # attaching tool_name to a bare function is not sufficient for SDK loading.
    candidate = getattr(module, name, None)
    if isinstance(getattr(module, "TOOL_SPEC", None), Mapping) and callable(candidate):
        candidate = module
    if _register(target, candidate, name_hint=name):
        return True
    _LOG.warning("Imported strands_tools.%s but could not register it", name)
    return False


# This allowlist defines intended candidates. Tools omitted from it are
# intentionally out of scope; listed tools must either register or emit a warning.
# Delegation and orchestration can invoke writes, so they are write-capable.
_STANDARD = (
    (
        READ_TOOLS,
        (
            "image_reader",
            "current_time",
            "stop",
            "handoff_to_user",
        ),
    ),
    (
        WRITE_TOOLS,
        (
            "shell",
            "think",
            "file_write",
            "editor",
            "http_request",
            "memory",
            "journal",
            "cron",
            "rss",
            "diagram",
            "speak",
            "batch",
            "graph",
            "use_agent",
            "swarm",
        ),
    ),
)

for _target, _names in _STANDARD:
    for _name in _names:
        if _name == EDITOR_TOOL_NAME:
            # Stira's sandbox-routed editor replaces the deprecated upstream one
            # at the same position (tool order is part of the cached prefix, #254).
            _register(_target, make_stira_editor(), name_hint=_name)
            continue
        _load_standard_tool(_target, _name)


def _drop_unbridged(tools: dict[str, Any]) -> set[str]:
    """Fail closed: never register a file/exec tool whose consent is not routed to Stira.

    Upstream terminal consent is decided by Stira's policy, never by stdin or a
    process-wide BYPASS_TOOL_CONSENT (#242). Returns the names removed.
    """
    covered = install_consent_bridge()
    dropped = set()
    for name in CONSENT_TOOL_MODULES:
        if name in tools and name not in covered:
            _LOG.error("Not registering %s: %s", name, bridge_refusal(name))
            del tools[name]
            dropped.add(name)
    return dropped


_drop_unbridged(WRITE_TOOLS)
# Short model-facing descriptions for long upstream docstrings, innermost (directly on
# the upstream tool, inside every gate): same name, inputSchema, execution and
# position (#368).
_compact_descriptions(READ_TOOLS)
_compact_descriptions(WRITE_TOOLS)
for _name in ("file_write", "editor"):
    if _name in WRITE_TOOLS:
        WRITE_TOOLS[_name] = IdentityProtectedTool(WRITE_TOOLS[_name])
for _name in sorted(EXEC_TOOL_NAMES):
    if _name in WRITE_TOOLS:
        # Same name, spec and position: the tool list stays cache-stable (#254).
        WRITE_TOOLS[_name] = ExecGatedTool(WRITE_TOOLS[_name])
# #547: a POST/PUT/PATCH/DELETE runs only inside a person's approval of that call (the
# upstream terminal prompt is answered by the bridge). Same name, spec and position (#254).
if "http_request" in WRITE_TOOLS:
    WRITE_TOOLS["http_request"] = HttpConsentGatedTool(WRITE_TOOLS["http_request"])
# #495: URL tools never reach the dashboard they run under (any rule, with or without
# YOLO). Same name, spec and position (#254); redirects are checked hop by hop too.
for _name in sorted(_OWN_DASHBOARD_URL_TOOLS):
    if _name in WRITE_TOOLS:
        WRITE_TOOLS[_name] = OwnDashboardGuardedTool(WRITE_TOOLS[_name])
install_registered_redirect_check(WRITE_TOOLS, sys.modules)  # logged once when it cannot

# The upstream image_reader opens with a plain ``open``: route it through the read
# floor (#312); get_tools drops it when that cannot be confirmed.
for _name, _module_name in (("image_reader", READ_FLOOR_MODULES[0]),):
    if _name in READ_TOOLS and _module_name in sys.modules:
        install_read_floor_opener(sys.modules[_module_name])

# Prefer bounded local reads over the unbounded standard file_read. Keep
# image_reader as a compatibility alias; view_image is the bounded alternative.
for _tool in (file_read, view_image):
    _register(READ_TOOLS, _tool)
for _tool in (web_fetch, web_search, webhook_send, oauth_api_call, lsp_diagnostics, lsp_hover):
    _register(WRITE_TOOLS, _tool)

# ADR 0015 (#449): the verification browser only when a usable browser is found (not on
# Windows or as root); ``stira doctor`` says why it is missing. A write tool: it writes a
# screenshot under the workspace, and it is hidden at WARNING and above with the others.
PAGE_CHECK_STATUS = _browser_status()
if PAGE_CHECK_STATUS.available:
    _register(WRITE_TOOLS, page_check)
else:
    _LOG.info("Skipping page_check: %s", PAGE_CHECK_STATUS.detail or PAGE_CHECK_STATUS.code)

# ADR 0019 (#167): ``run_checks`` only when gateway-config.json says ``run_checks:
# {"phase": 1}`` (the server calls :func:`enable_run_checks` at start-up); absent = not
# registered, nothing else changes. Not on Windows or as root.
RUN_CHECKS_STATE: dict[str, Any] = {"code": "run_checks_off", "registered": False}


def enable_run_checks(settings: Any) -> str:
    """Register (or not) ``run_checks`` for this process; returns a ``stira doctor`` code."""
    from . import run_checks as _rc

    if not getattr(settings, "enabled", False):
        code = "run_checks_off"
    else:
        code = _rc.platform_code() or "run_checks_on"
    if code == "run_checks_on":
        if "run_checks" not in WRITE_TOOLS and not _register(WRITE_TOOLS, _run_checks_tool):
            code = "run_checks_off"
    else:
        WRITE_TOOLS.pop("run_checks", None)
    _rc.mark_enabled(code == "run_checks_on")
    RUN_CHECKS_STATE.update(code=code, registered=code == "run_checks_on")
    return code


if perplexity_configured():
    _register(READ_TOOLS, perplexity_search, name_hint="perplexity_search")
else:
    _LOG.info("Skipping perplexity_search: PERPLEXITY_API_KEY is not configured")

try:
    _memory_tools = import_module("stiracore.tools.memory_tools")
except ImportError as exc:
    _LOG.debug("Optional Stira memory tools unavailable: %s", exc)
else:
    # Memory tools may persist state. Include registered tools, not factories
    # or undecorated helpers that require application-specific dependencies.
    for _name, _tool in vars(_memory_tools).items():
        # Skip imported modules (sqlite3, ...): only AgentTool instances qualify.
        if not _name.startswith("_") and isinstance(_tool, AgentTool):
            _register(WRITE_TOOLS, _tool, allow_function_name=False)

# #463: names the tool rules, the breaker's write class and the YOLO classes govern, but
# whose working tool is bound by the chat server (its memory store and turn id). The
# registry keeps an unbound copy (same name and spec); ``get_tools`` never hands it out,
# so a subagent, an evaluation runner or a child never gets one.
HOST_BOUND_TOOLS = frozenset({PROPOSE_PROCEDURE_TOOL_NAME, MEMORY_RATE_TOOL_NAME})
_register(WRITE_TOOLS, unbound_propose_procedure)
# ADR 0018 (phase 0): the rule category is "read" — its only effect is one bounded number
# about the turn's own episode (no text, no file, no retention); the chat server binds it
# only when memory_autonomy is on, and refuses it outside a NORMAL breaker.
_register(READ_TOOLS, unbound_memory_rate)

# Read tools that take a local path, and the name of that argument (#306).
_READ_PATH_ARGUMENTS = {"file_read": "path", "view_image": "path", "image_reader": "image_path"}
# Write-class tools that read a local file: behind the read floor too (#312), with
# their own (cwd) base for relative paths.
_FLOOR_ONLY_READ_ARGUMENTS = {"lsp_diagnostics": "file_path", "lsp_hover": "file_path"}
# Upstream read tools whose module ``open`` Stira routes through the read floor.
_UPSTREAM_READ_MODULES = {"image_reader": READ_FLOOR_MODULES[0]}


def _read_floor_refusals(tools: dict[str, Any]) -> set[str]:
    """Fail closed (#312): upstream read tools whose ``open`` is not Stira's opener."""
    dropped = set()
    for name, module_name in _UPSTREAM_READ_MODULES.items():
        if name not in tools:
            continue
        module = sys.modules.get(module_name)
        if module is None or not (
            read_floor_opener_live(module) or install_read_floor_opener(module)
        ):
            _LOG.error("Not registering %s: its reads cannot follow the read floor", name)
            del tools[name]
            dropped.add(name)
    return dropped


def get_tools(
    breaker_level: Any = None,
    *,
    read_only_paths: Iterable[str] = (),
    protected_dirs: Iterable[str] = (),
    include_exec: bool | None = None,
    write_policy: Any = None,
) -> list[Any]:
    """Return the allowed tools; WARNING and higher omit write tools.

    ``write_policy`` (a ``WritePathPolicy``) adds write roots to the file-tool
    wrapper (#243); without it only the floor applies (backward compatible).

    Exec-class tools (``EXEC_TOOL_NAMES``) are excluded unless ``include_exec``
    is true, or None with ``STIRA_ENABLE_EXEC_TOOLS`` set. Enabling them is an
    explicit operator decision; approval-gated execution is tracked in #167.

    NXPAgent's pre-turn action filtering remains authoritative, including
    HALT and transitions occurring after this list has been selected.
    """
    tools = dict(READ_TOOLS)
    for name in HOST_BOUND_TOOLS:
        tools.pop(name, None)  # the server adds its bound copy itself (ADR 0018)
    if not perplexity_configured():
        tools.pop("perplexity_search", None)
    if write_policy is not None:
        # A relative path means the same file for reads as for writes (#306), the data
        # directory is on the read floor (#312), and reads stay inside the read roots,
        # off the secret-path floor (#457): an upstream reader whose opens cannot follow
        # the policy is not offered.
        _read_floor_refusals(tools)
        for name, argument in _READ_PATH_ARGUMENTS.items():
            if name in tools:
                tools[name] = WorkspaceAnchoredReadTool(tools[name], write_policy, argument)
    write_tools = dict(WRITE_TOOLS)
    for name in HOST_BOUND_TOOLS:
        write_tools.pop(name, None)  # the server adds its bound copy itself (#463)
    for name in unconfigured_tools():
        write_tools.pop(name, None)  # #583: a call could only fail (no endpoint)
    _drop_unbridged(write_tools)  # re-checked per call: a broken bridge hides the tool
    if include_exec is None:
        include_exec = exec_tools_enabled()
    if include_exec:
        _LOG.warning("Exec-class tools are enabled: %s", ", ".join(sorted(EXEC_TOOL_NAMES)))
        for name in EXEC_TOOL_NAMES:
            if name in write_tools and not isinstance(write_tools[name], ExecGatedTool):
                write_tools[name] = ExecGatedTool(write_tools[name])  # never run ungated
    else:
        for name in EXEC_TOOL_NAMES:
            write_tools.pop(name, None)
    for name in ("file_write", "editor"):
        if name in write_tools:
            write_tools[name] = IdentityProtectedTool(
                write_tools[name], read_only_paths, tuple(protected_dirs), policy=write_policy
            )
    if write_policy is not None:
        for name, argument in _FLOOR_ONLY_READ_ARGUMENTS.items():
            if name in write_tools:
                write_tools[name] = WorkspaceAnchoredReadTool(
                    write_tools[name], write_policy, argument, anchor=False
                )
    if "page_check" in write_tools:
        # It needs the workspace to know what it may open and where the screenshot goes;
        # without a policy it is not offered (fail closed).
        if write_policy is None:
            write_tools.pop("page_check")
        else:
            from .page_check import WorkspacePageCheck

            write_tools["page_check"] = WorkspacePageCheck(
                write_tools["page_check"], write_policy, PAGE_CHECK_STATUS.path
            )
    if "run_checks" in write_tools:
        # Needs the workspace (what it may copy); without a policy it is not offered.
        if write_policy is None:
            write_tools.pop("run_checks")
        else:
            from .run_checks import WorkspaceRunChecks

            write_tools["run_checks"] = WorkspaceRunChecks(write_tools["run_checks"], write_policy)
    # Command-capable tools: a call naming xattr/chflags/sudo/tccutil/csrutil in plain text
    # is refused; no approval lifts that (#244; computed names are #167's sandbox).
    # Same name/spec/order: cache-stable (#254).
    for name, tool in write_tools.items():
        if is_command_tool(name) and not isinstance(tool, OsProtectionGuardTool):
            write_tools[name] = OsProtectionGuardTool(tool)
    # Also fail closed if the setting disappears after module import.
    if not _standard_memory_configured():
        write_tools.pop("memory", None)
    if writes_allowed(breaker_level):
        tools.update(write_tools)
    return list(tools.values())


# --- MCP discovery -----------------------------------------------------
#
# MCP servers are untrusted-by-default: we never talk to a transport or
# parse an MCP tool spec ourselves. Discovery and connection lifetime are
# fully delegated to the official strands.tools.registry.ToolRegistry /
# ToolProvider protocol (MCPClient implements ToolProvider). This module
# only classifies the resulting AgentTool instances into READ_TOOLS /
# WRITE_TOOLS and keeps enough bookkeeping to release the connection later.

_MCP_REGISTRIES: dict[int, _SdkToolRegistry] = {}


def register_mcp_tools(
    provider: Any,
    *,
    read_tool_names: Iterable[str] = (),
) -> list[str]:
    """Discover tools from an MCP ToolProvider (e.g. an MCPClient) and classify them.

    Connection startup, name resolution, and tool object construction are
    delegated to a dedicated ``strands.tools.registry.ToolRegistry`` instance
    via the official ``ToolProvider`` protocol; this function never inspects
    MCP transports directly.

    Unknown MCP tools default to WRITE_TOOLS (conservative: a tool must be
    explicitly named in ``read_tool_names`` to land in READ_TOOLS). A tool
    name already classified on the *other* side (from standard tools or a
    previous MCP registration) is rejected rather than silently moved, so an
    MCP server cannot escalate a read tool into a write tool by reusing its
    name, or vice versa.

    Call ``close_mcp_tools(provider)`` to release the connection and
    unregister the tools it contributed.

    Returns:
        Names of the tools that were newly registered (excludes any skipped
        due to a read/write collision).
    """
    if not isinstance(provider, _SdkToolProvider):
        raise TypeError("register_mcp_tools requires a strands ToolProvider (e.g. MCPClient)")

    if id(provider) in _MCP_REGISTRIES:
        raise ValueError("MCP provider already registered; close it before rediscovery")
    sdk_registry = _SdkToolRegistry()
    try:
        tool_names = sdk_registry.process_tools([provider])
    except ValueError as exc:
        _LOG.warning("Failed to discover MCP tools from %s: %s", type(provider).__name__, exc)
        try:
            sdk_registry.cleanup()
        except Exception:  # noqa: BLE001 - best-effort cleanup after a failed load
            _LOG.debug("Cleanup after failed MCP discovery raised", exc_info=True)
        return []

    read_set = frozenset(read_tool_names)
    registered: list[str] = []
    for name in tool_names:
        tool = sdk_registry.registry[name]
        is_read = name in read_set
        target, other = (READ_TOOLS, WRITE_TOOLS) if is_read else (WRITE_TOOLS, READ_TOOLS)
        if name in other or name in target:
            _LOG.warning(
                "Skipping MCP tool %r from %s: already registered (%s classification)",
                name,
                type(provider).__name__,
                "write" if other is WRITE_TOOLS else "read",
            )
            continue
        target[name] = tool
        registered.append(name)

    if registered:
        _MCP_REGISTRIES[id(provider)] = sdk_registry
    else:
        sdk_registry.cleanup()
    return registered


def mcp_tool_names() -> frozenset[str]:
    """Names of the tools an MCP provider registered here (ADR 0012: YOLO never answers
    them, in any tier, whatever the name)."""
    names: set[str] = set()
    for sdk_registry in tuple(_MCP_REGISTRIES.values()):
        # A snapshot: another thread may register or release a tool while this reads.
        names.update(tuple(sdk_registry.registry))
    return frozenset(names)


def close_mcp_tools(provider: Any) -> None:
    """Release an MCP ToolProvider's connection and unregister its tools.

    Idempotent: calling this for a provider with no active registration (or
    calling it twice) is a no-op. Removal only touches tool names that this
    provider actually contributed, so a name that was rejected in
    ``register_mcp_tools`` due to a collision is left untouched.
    """
    sdk_registry = _MCP_REGISTRIES.pop(id(provider), None)
    if sdk_registry is None:
        return

    for name, tool in sdk_registry.registry.items():
        if READ_TOOLS.get(name) is tool:
            del READ_TOOLS[name]
        elif WRITE_TOOLS.get(name) is tool:
            del WRITE_TOOLS[name]

    sdk_registry.cleanup()
