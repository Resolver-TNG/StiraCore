"""Tool classes of ADR 0012 §4 — which tier a tool's Ask may be answered in.

* **internal** — a positive constant list (local reads and writes, Stira memory,
  ``stop``, ``handoff_to_user``);
* **external** — the "network egress" row of ``docs/tool-execution-paths.md`` and
  ``perplexity_search``;
* **exec** — the exec class (``tool_consent.EXEC_TOOLS``): never answered by YOLO unless
  the person chose "Include exec" at the press (phase 7, §9 option C — a window of tier
  external with the exec flag), and then only :data:`EXEC_AUTO_TOOLS` (``shell``,
  ``batch``) under :func:`exec_refusal`; ``cron``, ``use_agent``, ``think``, ``swarm`` and
  ``graph`` stay a person's card in every window (:data:`EXEC_PERSON_REASONS`);
* ``delegate`` (Stira's gated child, ADR 0016) — a person's card in every window until
  ADR 0016 phase 3 (:data:`CHILD_PERSON_REASONS`);
* anything else — MCP tools, new tools, a name that is not a plain ``str`` — is
  **external** (unclassified = external). ``tests/test_yolo_auto_approve.py`` fails for a
  registered tool that is in no list.

Phase 6 (§10): in a window of tier **external**, a tool of :data:`EXTERNAL_TOOLS` (the
positive list — not the unclassified rest) is answered too, under §10's mitigations:
:func:`external_refusal` sends a call to a person when ``mask_secrets`` would change its
arguments (known patterns only), when it takes a credential from the host by reference,
or when it is not plain JSON; the external sub-cap is counted by the service. MCP tools
(:func:`mcp_tool`) and exec are never answered, in any tier. A window of tier internal
keeps every external tool's card.

**Scope** (PR #435 review M1, :func:`scope_refusal`): an internal call is answered only
when what it touches resolves (symlinks followed) inside the workspace or a write root —
``~/.zshrc``, ``~/Library/LaunchAgents``, ``~/.ssh`` … keep the person's card. Only a
plain ``str`` argument is evaluated; a missing, odd or unresolvable one: a card. Writes
outside the roots were already refused before any approval (``path_denial``); the scope
adds the reads (``file_read``, ``editor view``, images, ``lsp_*``) and ``journal``'s
``date`` (a name joined to a directory). ``diagram`` left the internal class: it starts
processes (graphviz, the OS ``open``) and writes where its ``title`` points.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

from ..observability.masking import mask_secrets
from ..tools.tool_consent import EXEC_TOOLS

__all__ = [
    "CHILD_PERSON_REASONS",
    "EXEC",
    "EXEC_AUTO_TOOLS",
    "EXEC_PERSON_REASONS",
    "EXTERNAL",
    "EXTERNAL_TOOLS",
    "INTERNAL",
    "INTERNAL_TOOLS",
    "PERSON_TOOLS",
    "destination",
    "exec_refusal",
    "external_refusal",
    "mcp_tool",
    "scope_refusal",
    "tool_class",
]

INTERNAL, EXTERNAL, EXEC = "internal", "external", "exec"

INTERNAL_TOOLS = frozenset(
    {
        # local reads
        "file_read",
        "view_image",
        "image_reader",
        "current_time",
        "lsp_diagnostics",
        "lsp_hover",
        # Stira memory reads
        "memory_read",
        "memory_search",
        "memory_list",
        "memory_index",
        "handoff_read",
        # ADR 0018: one bounded rating of the turn's own episode (no text, no path, no job,
        # no child; a saved Ask may be answered like memory_read)
        "memory_rate",
        # local writes
        "file_write",
        "editor",
        "journal",
        # Stira memory writes
        "memory_write",
        "handoff_write",
        # control
        "stop",
        "handoff_to_user",
    }
)
EXTERNAL_TOOLS = frozenset(
    {
        "http_request",
        "web_fetch",
        "web_search",
        "webhook_send",
        "oauth_api_call",
        "rss",
        "speak",
        "memory",
        "perplexity_search",
    }
)


# Local tools that start processes or write outside every root by argument: like exec,
# never answered by YOLO in any tier (a person decides each call).
#
# ``delegate`` (ADR 0016 §8 option B, #486): Stira's gated child agent. Its child runs only
# tools the parent may use without asking, but its tokens and actions are not yet counted
# in the window or shown on the board (phase 2), so every call stays a person's card until
# phase 3 — code ``child_not_yet_covered`` (:data:`CHILD_PERSON_REASONS`).
#
# ``page_check`` (ADR 0015, #449) runs the agent's own JavaScript in a closed browser: in
# phase 1 it stays a card in every window, until the launch guard is measured at every
# start (Q2 of the ADR).
#
# ``propose_procedure`` (#463) stages a procedural rule for the review screen. The rule
# needs a person anyway; a window never answers the proposal either, so the person sees
# what the agent wants to learn when it is proposed (maintainer decision, 2026-10-20).
#
# ``run_checks`` (ADR 0019 Q2, #167) runs the agent's own code in a throwaway copy: a card
# in every window while its rule is Ask (phases 1-2); answering it inside a window on a
# verified Mac is a later, separate decision (phase 1b).
PERSON_TOOLS = frozenset({"diagram", "delegate", "page_check", "propose_procedure", "run_checks"})
CHILD_PERSON_REASONS = {"delegate": "child_not_yet_covered"}

# Phase 7 (§9 option C): the exec tools a window with the exec flag may answer. Each call
# of them goes through Stira's gate itself: ``shell`` runs its command inside this one
# call; ``batch`` runs each inner call through the parent agent's own hooks (the SDK's
# direct tool call fires ``BeforeToolCallEvent``: an inner Ask call is judged on its own
# — a card, or an automatic approval with its own slot — measured in
# ``tests/test_headless_exec_tools_e2e.py``, "A shell call inside an approved batch
# needs its own card"). That premise is the whole reason ``batch`` may be here
# (:func:`exec_refusal` does not read the inner names): ``tests/test_yolo_exec_batch_app.py``
# measures it on the real app for inner cron / child-agent / Never / MCP / non-tool
# names, ``handoff_to_user`` and nested batches. If an SDK update ever runs an inner
# call without the hook, take ``batch`` out of this set (it stays a card).
EXEC_AUTO_TOOLS = frozenset({"shell", "batch"})
# The other exec tools carry permission past the window's checks, so they stay a
# person's card in every window (the code of each):
# * ``cron`` writes a crontab line that runs later as the server's OS user — without
#   YOLO, a window, a card or Stira's gate (also after the window ended);
# * ``use_agent`` / ``think`` / ``swarm`` / ``graph`` start child agents with the
#   parent's tools and **none of Stira's hooks** (#145): a child's tool runs with no rule,
#   no card, no floor check of the gate (measured: ``think``'s inner agent writes SOUL,
#   ``tests/test_tool_path_contract.py``) and none of the window's mitigations
#   (secret-in-arguments, sub-caps, the audit row) — and ``graph`` keeps its agents
#   for a later ``execute``.
EXEC_PERSON_REASONS = {
    "cron": "exec_runs_later",
    "use_agent": "exec_child_ungated",
    "think": "exec_child_ungated",
    "swarm": "exec_child_ungated",
    "graph": "exec_child_ungated",
}
assert EXEC_AUTO_TOOLS | set(EXEC_PERSON_REASONS) == EXEC_TOOLS
assert not EXEC_AUTO_TOOLS & set(EXEC_PERSON_REASONS)


def tool_class(name: Any) -> str:
    """``internal`` / ``external`` / ``exec`` for a tool name (exactly a ``str``).

    Exec (and :data:`PERSON_TOOLS`) is checked first; anything not in a list is external
    (fail closed)."""
    if type(name) is not str or name in EXEC_TOOLS or name in PERSON_TOOLS:
        return EXEC if type(name) is str else EXTERNAL
    if name in INTERNAL_TOOLS:
        return INTERNAL
    return EXTERNAL


# -- scope (PR #435 review M1) ---------------------------------------------------------

# The path argument of each internal file tool. Workspace-anchored: the registry's wrapper
# (``WorkspaceAnchoredReadTool`` / ``IdentityProtectedTool``) resolves a relative path
# against the workspace before the tool runs. ``lsp_*`` keep the process cwd.
_WORKSPACE_PATHS = {
    "file_read": "path",
    "view_image": "path",
    "image_reader": "image_path",
    "file_write": "path",
    "editor": "path",
}
_CWD_PATHS = {"lsp_diagnostics": "file_path", "lsp_hover": "file_path"}
# Internal tools that take no path: Stira's own store (keys validated there), control.
_PATHLESS = frozenset(
    {
        "current_time",
        "memory_read",
        "memory_search",
        "memory_list",
        "memory_index",
        "memory_rate",
        "memory_write",
        "handoff_read",
        "handoff_write",
        "stop",
        "handoff_to_user",
    }
)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


_MULTI = frozenset(",*?[")  # strands_tools.file_read splits on "," and expands globs


def _text(value: Any) -> str | None:
    """A plain, non-empty ``str`` naming exactly one path. A list (``a,b``) or a glob
    (``*``, ``?``, ``[``) can name files the check never resolves, so it is not a single
    path: the call goes to a person (a file name that really has such a character costs
    one card)."""
    if type(value) is not str or not value.strip() or "\0" in value:
        return None
    return None if _MULTI & set(value) else value


def _inside(policy: Any, path: Path) -> bool:
    """``path`` (symlinks of its existing prefix followed) is inside a write root — or,
    without an allowlist, inside the workspace."""
    roots = getattr(policy, "write_roots", None)
    if roots is not None:
        return bool(roots) and policy.within_roots(path)
    workspace = Path(os.path.realpath(policy.workspace))
    resolved = Path(os.path.realpath(path))
    return resolved == workspace or workspace in resolved.parents


def _target(name: str, tool_input: dict, policy: Any) -> Path | None:
    if name in _WORKSPACE_PATHS:
        text = _text(tool_input.get(_WORKSPACE_PATHS[name]))
        return None if text is None else Path(policy.absolute(text))
    if name in _CWD_PATHS:
        text = _text(tool_input.get(_CWD_PATHS[name]))
        if text is None:
            return None
        path = Path(text).expanduser()
        return path if path.is_absolute() else Path.cwd() / path
    if name == "journal":
        # strands_tools.journal writes ``<cwd>/journal/<date>.md``: ``date`` is joined
        # as given, so only an exact YYYY-MM-DD (or none: today) names that directory.
        date = tool_input.get("date")
        if date is None:
            return Path.cwd() / "journal" / "today.md"
        if type(date) is not str or not _DATE.fullmatch(date):
            return None
        return Path.cwd() / "journal" / f"{date}.md"
    return None


def scope_refusal(name: Any, tool_input: Any, policy: Any) -> str | None:
    """``None`` when an internal call stays inside the workspace / write roots (or takes
    no path); else ``scope_outside`` (it resolves outside) or ``scope_unknown`` (no
    policy, an argument that is missing or not a plain ``str``, an unknown tool, a path
    that cannot be resolved). The gate sends anything but ``None`` to a person.

    Phase 6: a tool an MCP server registered is ``mcp_tool`` (whatever its name); an
    external tool is judged by :func:`external_refusal`."""
    if type(name) is not str:
        return "scope_unknown"
    if name in CHILD_PERSON_REASONS:
        return CHILD_PERSON_REASONS[name]
    if mcp_tool(name):
        return "mcp_tool"
    if name in EXTERNAL_TOOLS:
        return external_refusal(name, tool_input, policy)
    if name in EXEC_TOOLS:
        return exec_refusal(name, tool_input)
    if name in _PATHLESS:
        return None
    if policy is None or type(tool_input) is not dict:
        return "scope_unknown"
    try:
        target = _target(name, tool_input, policy)
        if target is None:
            return "scope_unknown"
        return None if _inside(policy, target) else "scope_outside"
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return "scope_unknown"


# -- the external tier (phase 6, §10) -------------------------------------------------

# Arguments that make an external tool send a credential the host holds, so the
# arguments themselves carry no secret ``mask_secrets`` could see: ``http_request``
# reads ``auth_env_var`` from the server's environment and signs with the server's AWS
# credentials (``aws_auth``). Such a call is a person's card in every window.
_BY_REFERENCE = {"http_request": frozenset({"auth_env_var", "aws_auth"})}
# External tools that also write a local file where an argument points (``speak`` in
# Polly mode saves the audio at ``output_path``, ``~`` expanded, relative to the cwd):
# answered only when that path is inside the workspace / write roots, as internal writes.
_LOCAL_OUTPUT = {"speak": "output_path"}
# Arguments whose host is the destination of the call (``url``): recorded as a keyed
# hash only (Open (10) (b)), never in clear.
_URL_ARGUMENTS = ("url",)
_MAX_NODES = 10000
_MAX_DEPTH = 32
_SCALARS = (str, int, bool, type(None))


def _plain(value: Any) -> bool:
    """Exactly JSON-shaped built-in values (``dict`` with ``str`` keys, ``list``, ``str``,
    ``int``, finite ``float``, ``bool``, ``None``) within a size bound. A subclass, a
    tuple, a ``bytes`` or an object (which could answer ``==`` or ``mask_secrets``
    differently) is not evaluated: the call goes to a person."""
    stack = [(value, 0)]
    seen = 0
    while stack:
        item, depth = stack.pop()
        seen += 1
        if seen > _MAX_NODES or depth > _MAX_DEPTH:
            return False
        kind = type(item)
        if kind in _SCALARS:
            continue
        if kind is float:
            if not math.isfinite(item):
                return False
            continue
        if kind is list:
            stack.extend((child, depth + 1) for child in item)
            continue
        if kind is dict:
            for key, child in item.items():
                if type(key) is not str:
                    return False
                stack.append((child, depth + 1))
            continue
        return False
    return True


def mcp_tool(name: Any) -> bool:
    """Whether an MCP server registered a tool of this name (``tool_registry``): never
    answered by YOLO in any tier, also when it reuses a listed name (§9 / phase 6)."""
    if type(name) is not str:
        return True
    from ..tools.tool_registry import mcp_tool_names

    return name in mcp_tool_names()


def external_refusal(name: Any, tool_input: Any, policy: Any) -> str | None:
    """``None`` when an external call may be answered in a window of tier external;
    else why it goes to a person (ADR 0012 §10):

    * ``args_unknown`` — the arguments are not plain JSON values (:func:`_plain`);
    * ``secret_in_args`` — ``mask_secrets`` would change them: a URL's query or
      userinfo, a header, a body, a host name that looks like a known credential or a
      secret-named key (known patterns only, ``observability.masking``);
    * ``credential_by_reference`` — the call takes a credential from the host;
    * ``destination_unknown`` — it has a ``url`` whose host cannot be named in ASCII
      (no host, or no IDNA form): its destination could not be recorded;
    * ``scope_outside`` / ``scope_unknown`` — it also writes a local file outside the
      workspace / write roots, or where that cannot be decided."""
    if type(name) is not str or name not in EXTERNAL_TOOLS:
        return "scope_unknown"
    if type(tool_input) is not dict or not _plain(tool_input):
        return "args_unknown"
    try:
        masked = mask_secrets(tool_input)
    except Exception:  # noqa: BLE001 - a check that cannot run sends the call to a person
        return "args_unknown"
    if masked != tool_input:
        return "secret_in_args"
    if _BY_REFERENCE.get(name, frozenset()) & set(tool_input):
        return "credential_by_reference"
    if _has_url(tool_input) and destination(name, tool_input) is None:
        return "destination_unknown"
    argument = _LOCAL_OUTPUT.get(name)
    if argument is not None and argument in tool_input:
        text = _text(tool_input[argument])
        if text is None or policy is None:
            return "scope_unknown"
        try:
            path = Path(text).expanduser()
            path = path if path.is_absolute() else Path.cwd() / path
            return None if _inside(policy, path) else "scope_outside"
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            return "scope_unknown"
    return None


def exec_refusal(name: Any, tool_input: Any) -> str | None:
    """Phase 7 (§9 option C): ``None`` when an exec call may be answered in a window with
    the exec flag (the service checks the flag, the tier and the exec sub-cap); else why
    it goes to a person:

    * ``exec_runs_later`` / ``exec_child_ungated`` — :data:`EXEC_PERSON_REASONS`;
    * ``args_unknown`` — the arguments are not plain JSON values;
    * ``secret_in_args`` — ``mask_secrets`` would change them (the command string
      included: ``export TOKEN=…``, a URL with a key, a bearer header …; known patterns
      only).

    The command is never evaluated further (no allow-list, no path check): it runs as the
    server's OS user and can reach what Stira's floors and audit protect — the residual
    the operator accepted for option C (ADR 0012 §9, rev. 8)."""
    if type(name) is not str or name not in EXEC_TOOLS:
        return "scope_unknown"
    if name not in EXEC_AUTO_TOOLS:
        return EXEC_PERSON_REASONS.get(name, "exec_not_covered")
    if type(tool_input) is not dict or not _plain(tool_input):
        return "args_unknown"
    try:
        masked = mask_secrets(tool_input)
    except Exception:  # noqa: BLE001 - a check that cannot run sends the call to a person
        return "args_unknown"
    if masked != tool_input:
        return "secret_in_args"
    return None


def _has_url(tool_input: dict) -> bool:
    return any(type(tool_input.get(a)) is str and tool_input[a] for a in _URL_ARGUMENTS)


def destination(name: Any, tool_input: Any) -> str | None:
    """The host an external call goes to (its ``url`` argument), lower-cased ASCII — or
    ``None`` (no URL argument: a search or speech provider fixed by the tool; or a host
    that is not plain ASCII, whose IDNA spellings (2003 vs 2008: ``faß.de`` / ``fass.de``)
    the tool and this check could read differently, which :func:`external_refusal` sends
    to a person). Only the service sees it, and only to store its keyed hash (Open (10)
    (b)); it never reaches a row, a log line or the page in clear."""
    if type(name) is not str or name not in EXTERNAL_TOOLS or type(tool_input) is not dict:
        return None
    from urllib.parse import urlsplit

    for argument in _URL_ARGUMENTS:
        value = tool_input.get(argument)
        if type(value) is not str or not value:
            continue
        try:
            host = urlsplit(value.strip()).hostname
            if type(host) is not str or not host:
                return None
            if not host.isascii():
                return None
            labels = host.removesuffix(".").split(".")
            return None if any(not 0 < len(label) < 64 for label in labels) else host
        except (ValueError, UnicodeError):
            return None
    return None
