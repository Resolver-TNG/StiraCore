"""Exec-class tools run only inside the approval a human gave this exact call (#362).

The exec class (``shell``, ``batch``, ``cron``, ``use_agent``, ``swarm``, ``graph``,
``think``; #163) is hidden unless ``STIRA_ENABLE_EXEC_TOOLS`` is set, and every call is
Ask (ADR 0010 §3). Before #362 only ``file_write`` was routed through the consent
bridge, so an approved ``shell``/``cron`` call hit the upstream terminal prompt, read an
empty line from a headless stdin and failed with ``Command execution cancelled by user.
Input: `` — while the real ``BYPASS_TOOL_CONSENT`` / ``STRANDS_NON_INTERACTIVE`` in the
process environment let any caller skip it.

:class:`ExecGatedTool` is the same execution boundary as ``IdentityProtectedTool`` for
the file tools (#242): it consumes the single-use gate mark that
``ApprovalBindingVerifier`` issues only after a verified HITL approval of this exact
call (``tool_consent.HUMAN_APPROVAL_TOOLS``), runs a snapshot of the approved
arguments, re-checks the bridge, and holds the consent grant only while one upstream
step runs. A direct ``stream()``, a replay of the same call, an inner agent without
Stira's hooks (``think``/``use_agent``/``swarm``/``graph`` children) or a call inside an
approved ``batch`` that was not approved itself is refused. Not a sandbox: an approved
command runs as the server's OS user (#167).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from strands.tools.tools import AgentTool
from strands.types._events import ToolResultEvent

from .tool_consent import (
    EXEC_TOOLS,
    GateMark,
    ToolConsentError,
    bridge_refusal,
    claim_gate,
    consent_granted,
)

# The upstream cancellation text with an empty reason (#362): never shown alone.
_UPSTREAM_CANCELLED = re.compile(r"cancelled by user\. Input: ?$", re.IGNORECASE)


def _refused(tool_use: Mapping, text: str) -> ToolResultEvent:
    return ToolResultEvent(
        {"toolUseId": tool_use.get("toolUseId"), "status": "error", "content": [{"text": text}]}
    )


def _explained(tool_name: str, event: Any) -> Any:
    """An upstream "cancelled by user. Input: " result gains the reason Stira knows."""
    if not isinstance(event, ToolResultEvent) or not isinstance(event.tool_result, Mapping):
        return event
    result = event.tool_result
    if result.get("status") != "error":
        return event
    blocks = []
    changed = False
    for block in result.get("content") or ():
        text = block.get("text") if isinstance(block, Mapping) else None
        if isinstance(text, str) and _UPSTREAM_CANCELLED.search(text.strip()):
            text = (
                f"{text.strip()} [Stira: {tool_name} reported a cancellation with an empty "
                "reason inside an approved step; the consent bridge did not answer its prompt, "
                "so Stira cannot confirm whether any part of the call completed. Stira never "
                "reads the terminal for tool consent (#362).]"
            )
            block, changed = {**block, "text": text}, True
        blocks.append(block)
    if not changed:
        return event
    return ToolResultEvent({**result, "content": blocks}, event.exception)


class ExecGatedTool(AgentTool):
    """Keep the upstream exec tool's name/spec/type; run it only with its gate mark."""

    def __init__(self, wrapped: AgentTool) -> None:
        super().__init__()
        if wrapped.tool_name not in EXEC_TOOLS:
            raise ValueError(f"{wrapped.tool_name} is not an exec-class tool")
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
        name = self.tool_name
        try:
            arguments = copy.deepcopy(tool_use.get("input"))
        except Exception:  # noqa: BLE001 - uncopyable input cannot be approved
            yield _refused(tool_use, f"{name} was not run: input cannot be copied.")
            return
        mark: GateMark | str = claim_gate(
            name, invocation_state, tool_use.get("toolUseId"), arguments
        )
        refusal = mark if isinstance(mark, str) else bridge_refusal(name)
        if refusal:
            yield _refused(tool_use, refusal)
            return
        events = self.wrapped.stream({**tool_use, "input": arguments}, invocation_state, **kwargs)
        try:
            while True:
                try:
                    with consent_granted(name, mark):
                        event = await anext(events)
                except StopAsyncIteration:
                    return
                except ToolConsentError as error:
                    yield _refused(tool_use, str(error))
                    return
                yield _explained(name, event)
        finally:
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                await aclose()
