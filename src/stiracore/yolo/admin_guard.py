"""Admin-API defences that exist only while ``STIRA_YOLO_ALLOW`` is set (ADR 0012 §2, §8b).

**Reserved keys** (§2). ``yolo*`` is not a setting: every save path (``/api/config/*``,
``/api/tools/*``, ``/api/scheduler/*``) refuses a body that names it — at any depth, also
as a dotted hub field (``yolo.tier``, ``tools.yolo``) — with 409 ``yolo_not_settable``,
and the window state is never read from a config file.

**Running tool calls** (§8b, one layer of defence). A loosening admin operation —
approve / edit of an approval card, ``RECOVER``, a loosening tool-rule, hub or rollback
save, a scheduler create / resume / confirmed update — that arrives while an agent tool
call is running is refused (409 ``tool_call_running``). It holds the plain case: a tool
of the agent (``http_request`` with the CSRF token, an MCP client) calling the admin API
during its own call. It is **not** a proof that a person sent the request: a request
delivered after the call ended (a delayed send, a process the call left behind) passes.
The human channel is the host confirmation of phase 2. Narrowing operations (reject,
pause, cancel, a tightening save, stop) are never refused.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

__all__ = [
    "RESERVED_PREFIX",
    "RESERVED_REFUSAL_CODE",
    "TOOL_CALL_REFUSAL_CODE",
    "ToolCallTracker",
    "reserved_keys",
]

RESERVED_PREFIX = "yolo"
RESERVED_REFUSAL_CODE = "yolo_not_settable"
TOOL_CALL_REFUSAL_CODE = "tool_call_running"
_MAX_DEPTH = 12


def _reserved(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    return any(
        part.strip().lower().startswith(RESERVED_PREFIX)
        for part in name.replace("/", ".").split(".")
    )


def reserved_keys(value: Any, _depth: int = 0) -> list[str]:
    """Every key (or dotted key part) that names ``yolo*``, at any nesting depth.

    Too deep a structure counts as reserved (fail closed: it cannot be checked).
    """
    if _depth > _MAX_DEPTH:
        return ["<depth>"]
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _reserved(key):
                found.append(str(key)[:64])
            found.extend(reserved_keys(item, _depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(reserved_keys(item, _depth + 1))
    return found


class ToolCallTracker(HookProvider):
    """Counts the agent's tool calls between BeforeToolCall and AfterToolCall.

    A call is counted from the moment no earlier hook cancelled it, and removed at its
    AfterToolCall — or, if the SDK skipped that (an interrupt), at the end of the agent
    invocation. Counting a call another hook cancels later is fail-closed: the admin
    operation is refused for that moment, and the person retries.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[int, tuple[object, object]] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)
        registry.add_callback(AfterInvocationEvent, self.after_invocation)

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        if event.cancel_tool:
            return
        with self._lock:
            self._calls[id(event.tool_use)] = (event.tool_use, event.agent)

    def after_tool(self, event: AfterToolCallEvent) -> None:
        with self._lock:
            record = self._calls.get(id(event.tool_use))
            if record is not None and record[0] is event.tool_use:
                del self._calls[id(event.tool_use)]

    def after_invocation(self, event: AfterInvocationEvent) -> None:
        with self._lock:
            for key in [k for k, (_, agent) in self._calls.items() if agent is event.agent]:
                del self._calls[key]

    def running(self) -> int:
        with self._lock:
            return len(self._calls)

    # Explicit entry/exit for callers outside the hook registry (and tests).
    def enter(self, token: object) -> None:
        with self._lock:
            self._calls[id(token)] = (token, None)

    def leave(self, token: object) -> None:
        with self._lock:
            self._calls.pop(id(token), None)
