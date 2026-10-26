"""The one eligibility function of "allow the same again" (ADR 0025 §3).

:func:`allow_again_eligibility` decides, for one tool call, whether an approval card may
offer the session allowance (and Make Always), and **what "the same" means** for it — the
target class. The card's offer, the press (recomputed on the server from the stored
request) and every later call (on that call's own arguments) use this function and the
same :data:`CLASS_VERSION`; there is no second copy in the page or in the approver.

The answer is :class:`TargetClass` (``class_kind``, ``class_key``) or a reason code
(:data:`REASON_CODES`). ``class_key`` is the **resolved** directory, the host or the
layer: it lives only in the server's memory and the page, never in a row (rows carry a
keyed hash, :mod:`.session_allow`).

Used by the card's offer, the press and the gate (ADR 0025 PR 3: both buttons on).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stiracore.observability.masking import mask_secrets

from .tiers import (
    EXEC_TOOLS,
    EXTERNAL_TOOLS,
    PERSON_TOOLS,
    _plain,
    destination,
    external_refusal,
    mcp_tool,
    scope_refusal,
)

__all__ = [
    "CLASS_KINDS",
    "CLASS_VERSION",
    "ORIGIN_CHAT",
    "REASON_CODES",
    "ClassPolicy",
    "TargetClass",
    "allow_again_eligibility",
    "class_contains",
]

# A code constant: a record made with another value never matches (ADR 0025 Terms).
CLASS_VERSION = 1
CLASS_KINDS = frozenset({"dir_subtree", "layer", "tool", "host", "host_read"})
# The only origin an allowance answers: a person's chat / Stage turn of this session.
ORIGIN_CHAT = "chat_turn"
REASON_CODES = frozenset(
    {
        "exec",
        "person_only",
        "unclassified",
        "scope_outside",
        "scope_unknown",
        "read_unbound",
        "secret_in_args",
        "credential_by_reference",
        "destination_unknown",
        "sends_data",
        "not_this_session",
        "args_unknown",
    }
)

_DIR_TOOLS = {"file_write": "path", "editor": "path"}
# editor commands that change a file through ``open_checked``; every other (``view``,
# ``find_line``, an unknown one) is a path read the class cannot bind.
_EDITOR_WRITES = frozenset({"create", "str_replace", "insert", "pattern_replace", "undo_edit"})
_READ_UNBOUND = frozenset(
    {"file_read", "view_image", "image_reader", "lsp_diagnostics", "lsp_hover"}
)
_TOOL_CLASS = frozenset(
    {"memory_read", "memory_search", "memory_list", "memory_rate", "journal", "current_time"}
)
_HOST_TOOLS = frozenset({"web_fetch", "rss"})
_SEARCH_TOOLS = frozenset({"web_search", "perplexity_search"})
_SENDS = frozenset({"webhook_send", "oauth_api_call", "speak", "memory"})
_READ_METHODS = frozenset({"GET", "HEAD"})
_CREDENTIAL_ARGS = frozenset({"auth_env_var", "aws_auth", "auth_token", "credential"})


@dataclass(frozen=True)
class TargetClass:
    tool: str
    class_kind: str
    class_key: str
    class_version: int = CLASS_VERSION
    whole_root: bool = False  # a dir_subtree that is a write root itself (shown on the button)


def _resolved(policy: Any, text: str) -> Path:
    return Path(os.path.realpath(policy.absolute(text)))


def _root_of(policy: Any, resolved: Path) -> Path | None:
    """The write root (resolved) that contains ``resolved`` — the workspace without one."""
    roots = getattr(policy, "write_roots", None)
    candidates = list(roots) if roots else [policy.workspace]
    best: Path | None = None
    for root in candidates:
        root_path = Path(os.path.realpath(os.fspath(root)))
        inside = resolved == root_path or root_path in resolved.parents
        if inside and (best is None or len(root_path.parts) > len(best.parts)):
            best = root_path
    return best


def _dir_class(tool: str, tool_input: dict, policy: Any) -> TargetClass | str:
    refusal = scope_refusal(tool, tool_input, policy)
    if refusal is not None:
        return refusal if refusal in REASON_CODES else "scope_unknown"
    text = tool_input.get(_DIR_TOOLS[tool])
    if type(text) is not str:
        return "scope_unknown"
    spelled = Path(policy.absolute(text))
    resolved = _resolved(policy, text)
    root = _root_of(policy, resolved)
    if root is None:
        return "scope_outside"
    directory = resolved.parent
    # A symlink component between the write root and the file (the file itself included):
    # the class is bound to the resolved directory, so a spelling through a link is not
    # "the same" place twice (§3: ``scope_unknown``).
    if os.path.islink(spelled):
        return "scope_unknown"
    node = Path(os.path.normpath(spelled.parent))
    while Path(os.path.realpath(node)) != root:
        if os.path.islink(node):
            return "scope_unknown"
        if node.parent == node:
            return "scope_unknown"
        node = node.parent
    if root != directory and root not in directory.parents:
        return "scope_outside"
    return TargetClass(tool, "dir_subtree", str(directory), whole_root=directory == root)


def allow_again_eligibility(
    tool: Any, tool_input: Any, policy: Any, origin: Any
) -> TargetClass | str:
    """:class:`TargetClass`, or the reason code why only "Approve once" is offered."""
    if type(tool) is not str or not tool:
        return "unclassified"
    if tool in EXEC_TOOLS:
        return "exec"
    if tool in PERSON_TOOLS:
        return "person_only"
    try:
        if mcp_tool(tool):
            return "unclassified"
    except Exception:  # noqa: BLE001 - a registry that cannot be read: not offered
        return "unclassified"
    if tool in _READ_UNBOUND:
        return "read_unbound"
    known = (
        (set(_DIR_TOOLS) | {"memory_write"} | _TOOL_CLASS | _HOST_TOOLS | _SEARCH_TOOLS)
        | {"http_request"}
        | _SENDS
    )
    if tool not in known:
        return "unclassified"
    if origin != ORIGIN_CHAT:
        return "not_this_session"
    if type(tool_input) is not dict or not _plain(tool_input):
        return "args_unknown"
    try:
        if mask_secrets(tool_input) != tool_input:
            return "secret_in_args"
    except Exception:  # noqa: BLE001 - a check that cannot run: not offered
        return "args_unknown"
    if _CREDENTIAL_ARGS & set(tool_input):
        return "credential_by_reference"
    if tool in _SENDS:
        return "sends_data"
    try:
        if tool in _DIR_TOOLS:
            if tool == "editor" and tool_input.get("command") not in _EDITOR_WRITES:
                return "read_unbound"
            if policy is None:
                return "scope_unknown"
            return _dir_class(tool, tool_input, policy)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return "scope_unknown"
    if tool == "memory_write":
        layer = tool_input.get("layer")
        if type(layer) is not str or layer.strip().lower() != "episodic":
            return "scope_unknown"
        return TargetClass(tool, "layer", "episodic")
    if tool in _TOOL_CLASS:
        return TargetClass(tool, "tool", tool)
    # External tools (``web_fetch`` / ``rss`` / ``http_request`` / searches).
    refusal = external_refusal(tool, tool_input, policy)
    if refusal is not None:
        return refusal if refusal in REASON_CODES else "args_unknown"
    if tool in _SEARCH_TOOLS:
        return TargetClass(tool, "tool", tool)
    if tool == "http_request":
        method = tool_input.get("method", "GET")
        if type(method) is not str or method.strip().upper() not in _READ_METHODS:
            return "sends_data"
    host = destination(tool, tool_input)
    if host is None:
        return "destination_unknown"
    kind = "host_read" if tool == "http_request" else "host"
    if tool not in EXTERNAL_TOOLS:
        return "unclassified"
    return TargetClass(tool, kind, host)


class ClassPolicy:
    """The class of a ``dir_subtree`` allowance as one more ``open_checked`` policy (§3):
    asked again on the opened file's path, it resolves the path **now** (symlinks of its
    existing prefix followed), so a parent directory swapped for a link to another
    directory after the gate's check is a path outside the class and the write is
    refused at the open."""

    def __init__(self, class_key: str) -> None:
        self.base = Path(os.path.realpath(class_key))

    def check(self, path: Any) -> Any:
        from types import SimpleNamespace

        try:
            resolved = Path(os.path.realpath(os.fspath(path)))
        except (OSError, TypeError, ValueError):
            resolved = None
        inside = resolved is not None and (resolved == self.base or self.base in resolved.parents)
        return SimpleNamespace(
            allowed=inside,
            reason="Not written: the target left the directory of the session allowance.",
            path=os.fspath(path) if inside else None,
        )


def class_contains(record_class: TargetClass, call_class: TargetClass) -> bool:
    """Whether a call's class lies inside the class of an allowance (use time, §4b):
    same tool, kind and version; a directory inside the class directory (resolved
    components); the same host / layer / tool otherwise."""
    if (
        record_class.tool != call_class.tool
        or record_class.class_kind != call_class.class_kind
        or record_class.class_version != CLASS_VERSION
        or call_class.class_version != CLASS_VERSION
    ):
        return False
    if record_class.class_kind == "dir_subtree":
        base, target = Path(record_class.class_key), Path(call_class.class_key)
        return target == base or base in target.parents
    return record_class.class_key == call_class.class_key
