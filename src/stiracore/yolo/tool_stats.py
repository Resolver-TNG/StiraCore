"""What ran in a YOLO window: the tool calls of the window's period, by decision (#448).

The window's ``used`` / ``external_used`` / ``exec_used`` count **automatic approvals**
only (they are what the caps limit, unchanged). This counts every tool call the agent's
gate decided while the window was open (active or paused) — counts only, never an
argument, an output, a command, a path or a destination:

* ``total`` and ``by_decision``: ``auto_approved`` (YOLO answered the Ask), ``always``
  (no card was needed: the rule is Always, or the tool has no rule), ``manual`` (a person
  answered the card), ``denied`` (Never, a protected location, the breaker, a person's
  rejection, a refused automatic approval …), ``allowed_again`` (a session allowance
  answered the Ask, ADR 0025 §7). The five add up to ``total``;
* ``failed``: of those that ran, how many returned an error (a subset of ``total``);
* ``by_category``: ``internal`` / ``external`` / ``exec`` (:func:`tiers.tool_class`);
* ``by_tool``: a registry name (:data:`audit_schema.KNOWN_TOOLS`) in clear, everything
  else (MCP, dynamic names) as ``other``;
* ``turns`` (kept next to it by :class:`requests.YoloRequests`): agent turns started.

The counter lives in the window's in-memory record and is read under the service's lock;
:func:`snapshot` returns built-in types only (status, ``last_end``, ``audit.yolo.ended``).
"""

from __future__ import annotations

from typing import Any

from .audit_schema import KNOWN_TOOLS
from .tiers import EXEC, EXTERNAL, INTERNAL, tool_class

__all__ = ["CATEGORIES", "DECISIONS", "add", "add_failure", "empty", "snapshot", "tool_label"]

# ``allowed_again``: a session allowance answered the Ask (ADR 0025 §7) — never ``manual``.
DECISIONS = ("auto_approved", "always", "manual", "denied", "allowed_again")
CATEGORIES = (INTERNAL, EXTERNAL, EXEC)
OTHER = "other"


def tool_label(name: Any) -> str:
    """A registry tool name in clear; anything else ``other`` (never a free-form name)."""
    return name if type(name) is str and name in KNOWN_TOOLS else OTHER


def empty() -> dict:
    return {
        "total": 0,
        "by_decision": dict.fromkeys(DECISIONS, 0),
        "failed": 0,
        "by_category": dict.fromkeys(CATEGORIES, 0),
        "by_tool": {},
    }


def add(stats: dict, tool_name: Any, decision: str) -> None:
    """One call (the caller holds the lock). An unknown decision counts as ``denied``."""
    decision = decision if decision in DECISIONS else "denied"
    stats["total"] += 1
    stats["by_decision"][decision] += 1
    stats["by_category"][tool_class(tool_name)] += 1
    label = tool_label(tool_name)
    stats["by_tool"][label] = stats["by_tool"].get(label, 0) + 1


def add_failure(stats: dict) -> None:
    """One call that ran and returned an error (already counted by :func:`add`)."""
    if stats["failed"] < stats["total"]:
        stats["failed"] += 1


def snapshot(stats: Any) -> dict:
    """A copy in built-in types (a fresh counter for anything else)."""
    if type(stats) is not dict:
        stats = empty()
    return {
        "total": int(stats["total"]),
        "by_decision": {key: int(stats["by_decision"][key]) for key in DECISIONS},
        "failed": int(stats["failed"]),
        "by_category": {key: int(stats["by_category"][key]) for key in CATEGORIES},
        "by_tool": {key: int(value) for key, value in sorted(stats["by_tool"].items())},
    }
