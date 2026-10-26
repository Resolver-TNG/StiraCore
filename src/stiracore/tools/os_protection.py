"""Refuse tool calls that name OS-protection or privilege commands (#244).

Stira diagnoses a write that the OS refused (``stiracore.diagnostics``) and
tells a *person* how to fix it. The agent itself must never try: ``xattr``
(removing quarantine/iCloud attributes), ``chflags`` (clearing ``uchg``),
``sudo``, ``tccutil``/writes to the TCC database (privacy permissions) and
``csrutil`` (SIP) named in plain text in a command-capable tool's input are
refused, at every breaker level, and no approval lifts *that* refusal.

Two layers, like the file-tool floor (#145):

* :func:`os_protection_denial` in ``IdentityGuard.path_denial`` (the hook layer,
  shared with the HITL gate, which therefore never even asks for approval);
* :class:`OsProtectionGuardTool`, the execution boundary around command-capable
  tools in ``tool_registry`` — it re-checks the *final* input (after any HITL
  edit) and also holds in hook-less inner agents (``batch``/``use_agent`` reach
  the parent's wrapped tools).

**Scope (a limit, not a guarantee):** this is a denylist of *plain* command
names (case-insensitive — APFS resolves ``SUDO`` to ``sudo`` —, quotes,
backslashes and full-width forms folded), not a sandbox. A name computed at run
time (``getattr(os, "ch" + "flags")``, ``$(echo …)``, base64, variables, an alias
or a symlink to the binary) is **not** detected. Exec-class tools are disabled
by default (``STIRA_ENABLE_EXEC_TOOLS``); containing them is the exec sandbox's
job (#167). Writing the TCC database through
``file_write``/``editor`` is on the path floor (``identity_paths``).
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Iterator, Mapping
from typing import Any

from strands.tools.tools import AgentTool
from strands.types._events import ToolResultEvent

OS_PROTECTION_ERROR = (
    "Refused: the call names a command that changes OS protections or escalates privileges "
    "(xattr, chflags, sudo, tccutil, csrutil, the TCC database)"
)
OS_PROTECTION_COMMANDS = (
    "xattr",
    "setxattr",
    "removexattr",
    "chflags",
    "lchflags",
    "fchflags",
    "sudo",
    "sudoers",
    "visudo",
    "doas",
    "pkexec",
    "tccutil",
    "csrutil",
    "chattr",
    "spctl",
)
# Tools that run commands or other tools. Superset of tool_registry.EXEC_TOOL_NAMES
# (a test pins that); other names are matched by their parts below.
COMMAND_TOOL_NAMES = frozenset(
    {"shell", "batch", "cron", "use_agent", "swarm", "graph", "think", "python_repl"}
)
_COMMAND_PARTS = frozenset(
    {
        "shell",
        "bash",
        "zsh",
        "sh",
        "terminal",
        "exec",
        "execute",
        "command",
        "cmd",
        "powershell",
        "pwsh",
        "repl",
        "subprocess",
        "spawn",
    }
)
_NAME = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(OS_PROTECTION_COMMANDS) + r")(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_TCC_DB = re.compile(r"tcc\.db|com\.apple\.tcc", re.IGNORECASE)
_FOLD = re.compile(r"[\"'`\\]")


def is_command_tool(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    if name in COMMAND_TOOL_NAMES:
        return True
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
    return bool(_COMMAND_PARTS.intersection(re.split(r"[^a-z0-9]+", snake)))


def _strings(value: Any, seen: set[int] | None = None) -> Iterator[str]:
    if isinstance(value, str):
        yield value
        return
    if not isinstance(value, (Mapping, list, tuple)):
        return
    seen = set() if seen is None else seen
    if id(value) in seen:
        return
    seen.add(id(value))
    items = value.items() if isinstance(value, Mapping) else ((None, v) for v in value)
    for key, item in items:
        if isinstance(key, str):
            yield key
        yield from _strings(item, seen)


def os_protection_problem(tool_input: Any) -> str | None:
    """Why ``tool_input`` (any nesting) is refused, or ``None``."""
    for text in _strings(tool_input):
        normalized = unicodedata.normalize("NFKC", text)
        # Two readings (#601 C15): backslashes dropped (``su\do``) and backslashes kept as
        # a separator (``C:\Windows\System32\sudo.exe``, where dropping them would glue
        # ``System32sudo`` together). Either one matching refuses.
        for folded in (
            _FOLD.sub("", normalized),
            _FOLD.sub("", normalized.replace("\\", "/")),
        ):
            match = _NAME.search(folded)
            if match:
                return f"{OS_PROTECTION_ERROR}: {match.group(1).lower()}."
            if _TCC_DB.search(folded):
                return f"{OS_PROTECTION_ERROR}: the TCC database."
    return None


def os_protection_denial(tool_name: Any, tool_input: Any) -> str | None:
    """The hook-layer check: only command-capable tools are scanned (any input string)."""
    if not is_command_tool(tool_name):
        return None
    return os_protection_problem(tool_input)


class OsProtectionGuardTool(AgentTool):
    """Execution boundary: refuse a command-capable tool's call before it runs."""

    def __init__(self, wrapped: AgentTool) -> None:
        super().__init__()
        self.wrapped = wrapped

    @property
    def tool_name(self):
        return self.wrapped.tool_name

    @property
    def tool_spec(self):
        return self.wrapped.tool_spec

    @property
    def tool_type(self):
        return self.wrapped.tool_type

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        try:
            tool_input = copy.deepcopy(tool_use.get("input"))
        except Exception:  # noqa: BLE001 - an uncopyable input cannot be checked
            tool_input = None
            problem = f"{OS_PROTECTION_ERROR}: the input cannot be checked."
        else:
            problem = os_protection_problem(tool_input)
        if problem:
            yield ToolResultEvent(
                {
                    "toolUseId": tool_use.get("toolUseId"),
                    "status": "error",
                    "content": [{"text": problem}],
                }
            )
            return
        events = self.wrapped.stream({**tool_use, "input": tool_input}, invocation_state, **kwargs)
        try:
            async for event in events:
                yield event
        finally:
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                await aclose()
