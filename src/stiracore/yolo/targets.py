"""Which tools a YOLO window could answer, per scope (#450).

YOLO answers only a tool whose saved rule is **Ask** (Always never shows a card, Never
never runs). Of those, the press's scope decides (``tiers``, the same lists the approver
uses): *internal only* covers :data:`tiers.INTERNAL_TOOLS`; *external too* adds
:data:`tiers.EXTERNAL_TOOLS`; *exec too* adds :data:`tiers.EXEC_AUTO_TOOLS` (``shell``,
``batch``) when the exec tools are exposed. Every other Ask tool stays a person's card in
every window and is listed as ``excluded`` with the reason (``cron``, the child-agent
tools, ``diagram``, MCP tools, an unclassified tool, an exec tool that is not exposed).

This is a summary for the start dialog, computed from the saved rules only: a call may
still go to a person by its arguments (outside the workspace, a secret-like value), by
the breaker, or by a sub-cap. It reads; it never changes a rule or a window.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ..tools import tool_registry as registry
from ..tools.tool_policy import ASK
from .tiers import (
    EXEC_AUTO_TOOLS,
    EXEC_PERSON_REASONS,
    EXTERNAL_TOOLS,
    INTERNAL_TOOLS,
    PERSON_TOOLS,
    mcp_tool,
)

__all__ = ["SCOPES", "yolo_targets"]

SCOPES = ("internal", "external", "exec")


def _place(name: str, exec_exposed: bool, is_mcp: Callable[[str], bool]) -> tuple[str, str]:
    """``("scope", <scope>)`` or ``("excluded", <reason>)`` for one Ask tool."""
    if is_mcp(name):
        return "excluded", "mcp_tool"
    if name in PERSON_TOOLS:
        return "excluded", "person_tool"
    if name in EXEC_PERSON_REASONS:
        return "excluded", EXEC_PERSON_REASONS[name]
    if name in EXEC_AUTO_TOOLS:
        return ("scope", "exec") if exec_exposed else ("excluded", "exec_not_exposed")
    if name in INTERNAL_TOOLS:
        return "scope", "internal"
    if name in EXTERNAL_TOOLS:
        return "scope", "external"
    return "excluded", "unclassified"


def yolo_targets(
    tool_config: Any,
    *,
    names: Iterable[str] | None = None,
    exec_exposed: bool | None = None,
    is_mcp: Callable[[str], bool] = mcp_tool,
) -> dict:
    """``{"ask_total", "scopes": {scope: {"count", "tools"}}, "excluded": [{"name",
    "reason"}], "locked"}``. A scope counts what it adds and everything before it
    (external includes internal; exec includes both). Locked rules: every tool is Never,
    so every scope is empty."""
    if names is None:
        names = set(registry.READ_TOOLS) | set(registry.WRITE_TOOLS)
    if exec_exposed is None:
        exec_exposed = registry.exec_tools_enabled()
    locked = bool(getattr(tool_config, "integrity_error", None))
    own: dict[str, list[str]] = {scope: [] for scope in SCOPES}
    excluded = []
    ask_total = 0
    for name in sorted({n for n in names if type(n) is str}):
        if locked or tool_config.policy_of(name) != ASK:
            continue
        ask_total += 1
        kind, where = _place(name, exec_exposed, is_mcp)
        if kind == "scope":
            own[where].append(name)
        else:
            excluded.append({"name": name, "reason": where})
    scopes = {}
    covered: list[str] = []
    for scope in SCOPES:
        covered = sorted(covered + own[scope])
        scopes[scope] = {"count": len(covered), "tools": list(covered)}
    return {
        "ask_total": ask_total,
        "scopes": scopes,
        "excluded": excluded,
        "locked": locked,
    }
