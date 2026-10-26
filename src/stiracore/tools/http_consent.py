"""A modifying ``http_request`` runs only inside the approval a person gave that call (#547).

The upstream ``http_request`` shows a ``[y/*]`` terminal prompt before a POST, PUT, PATCH
or DELETE unless the process environment has ``BYPASS_TOOL_CONSENT=true``. On a server
without a terminal the prompt read an empty line, so a request a person had approved on
Stira's card came back "HTTP request cancelled by the user. Reason: " and was never sent,
while a process-wide ``BYPASS_TOOL_CONSENT=true`` let every caller skip it.

Since #547 the module is in the consent bridge (``tool_consent.CONSENT_TOOL_MODULES``):
its prompt never reads the terminal and its ``BYPASS_TOOL_CONSENT`` is answered only
inside a grant. ``ApprovalBindingVerifier`` issues the single-use gate mark of a modifying
call only after a person approved this exact call (``tool_consent.needs_person``) and
cancels it otherwise with the reason; :class:`HttpConsentGatedTool` consumes that mark,
runs the approved arguments and holds the grant only while one upstream step runs. A GET
(or any method the upstream does not confirm) runs as before, without a mark or grant.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from strands.tools.tools import AgentTool
from strands.types._events import ToolResultEvent

from .tool_consent import (
    HTTP_REQUEST_TOOL,
    GateMark,
    ToolConsentError,
    bridge_refusal,
    claim_gate,
    consent_granted,
    modifying_http_call,
)


def _refused(tool_use: Mapping, text: str) -> ToolResultEvent:
    return ToolResultEvent(
        {"toolUseId": tool_use.get("toolUseId"), "status": "error", "content": [{"text": text}]}
    )


class HttpConsentGatedTool(AgentTool):
    """Keep the upstream tool's name/spec/type; a modifying call needs its gate mark."""

    def __init__(self, wrapped: AgentTool) -> None:
        super().__init__()
        if wrapped.tool_name != HTTP_REQUEST_TOOL:
            raise ValueError(f"{wrapped.tool_name} is not {HTTP_REQUEST_TOOL}")
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
        if not modifying_http_call({"name": name, "input": tool_use.get("input")}):
            # The upstream asks nothing for this method (unchanged); its bridged prompt,
            # if reached anyway, refuses outside a grant.
            async for event in self.wrapped.stream(tool_use, invocation_state, **kwargs):
                yield event
            return
        try:
            arguments = copy.deepcopy(tool_use.get("input"))
        except Exception:  # noqa: BLE001 - uncopyable input cannot be approved
            yield _refused(tool_use, f"{name} was not run: input cannot be copied.")
            return
        call = {**tool_use, "input": arguments}
        mark: GateMark | str = claim_gate(
            name, invocation_state, tool_use.get("toolUseId"), arguments
        )
        refusal = mark if isinstance(mark, str) else bridge_refusal(name)
        if refusal:
            yield _refused(tool_use, refusal)
            return
        events = self.wrapped.stream(call, invocation_state, **kwargs)
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
                yield event
        finally:
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                await aclose()
