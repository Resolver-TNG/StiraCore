"""Feeds the repeated-failure counter (#151) from finished tool calls. Observe only."""

from __future__ import annotations

from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from stiracore.core.repeated_failure import RepeatedFailureCounter


class RepeatedFailureHook(HookProvider):
    """Records each tool call's outcome; never reads or changes the call or its result."""

    def __init__(self, counter: RepeatedFailureCounter) -> None:
        self.counter = counter

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._record)

    def _record(self, event: AfterToolCallEvent) -> None:
        try:
            name = event.tool_use.get("name")
            failed = (
                getattr(event, "exception", None) is not None
                or event.result.get("status") == "error"
            )
        except Exception:  # noqa: BLE001 - an indicator must never fail a tool call
            return
        self.counter.record(name if isinstance(name, str) else "", not failed)
