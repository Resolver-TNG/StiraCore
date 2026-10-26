"""Read-only hooks that feed the taint ledger (ADR 0022 phase 0, #458).

Nothing here changes a call, a prompt or a result: the callbacks read the event (the tool
name and input, the agent's messages and system prompt) and count fixed codes. They never
set ``cancel_tool``, never replace ``result``, never write to the agent and never raise.

Per turn (one agent invocation) the observer keeps the codes ``flow_would_hold`` gave to
egress calls; at the end of the turn it computes the context label (§3) and records one
row — codes and numbers only (no text, argument, URL or host).
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from stiracore import labels

__all__ = ["RECALL_MARKERS", "TaintObserver", "TaintObserverHook", "injected_sources"]

_LOG = logging.getLogger(__name__)
# Blocks the prompt assembler injects from stored memory (rows 6 and 7 of the inventory).
RECALL_MARKERS = ("## Episodic Memory", "<recalled-memory", "<associated-memory", "## Associated Memory")  # fmt: skip
_SCAN = 200_000


def _texts(agent: Any) -> list[str]:
    out = []
    prompt = getattr(agent, "system_prompt", None)
    if isinstance(prompt, str):
        out.append(prompt[:_SCAN])
    for message in getattr(agent, "messages", None) or ():
        content = message.get("content") if isinstance(message, Mapping) else None
        for block in content if isinstance(content, list) else ():
            text = block.get("text") if isinstance(block, Mapping) else None
            if isinstance(text, str):
                out.append(text[:_SCAN])
    return out


def injected_sources(agent: Any) -> list[str]:
    """``["memory"]`` when a recalled-memory block is in the context the model sees."""
    try:
        if any(marker in text for text in _texts(agent) for marker in RECALL_MARKERS):
            return ["memory"]
    except Exception:  # noqa: BLE001 - unreadable: counted as recalled (fail closed)
        return ["memory"]
    return []


class TaintObserver:
    """Counts per turn and in total; ``record`` receives one row per finished turn."""

    def __init__(self, record: Callable[[dict], Any] | None = None) -> None:
        self.record = record
        self._lock = threading.Lock()
        self._held: Counter[str] = Counter()
        self._egress_calls = 0
        self.last: dict[str, Any] | None = None
        self.totals: Counter[str] = Counter()

    def begin(self) -> None:
        with self._lock:
            self._held.clear()
            self._egress_calls = 0

    def note_call(self, code: str | None) -> None:
        with self._lock:
            self._egress_calls += 1
            if code in labels.FLOW_CODES:
                self._held[code] += 1

    def finish(self, label: labels.ContextLabel) -> dict[str, Any]:
        with self._lock:
            row = {
                **label.as_row(),
                "egress_calls": self._egress_calls,
                "flow_would_hold": dict(sorted(self._held.items())),
            }
            self.last = row
            self.totals["turns"] += 1
            self.totals[f"trust.{label.trust}"] += 1
            self.totals["sensitive"] += int(label.sensitive)
            self.totals["untrusted_recall_only"] += int(label.recall_only)
            for code, count in self._held.items():
                self.totals[f"flow.{code}"] += count
        if self.record is not None:
            self.record(row)
        return row

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"last_turn": self.last, "totals": dict(sorted(self.totals.items()))}


class TaintObserverHook(HookProvider):
    """Feed the observer from one agent. Reads events; never writes."""

    def __init__(
        self, observer: TaintObserver, started_by_person: Callable[[], bool] = lambda: False
    ) -> None:
        self.observer = observer
        self.started_by_person = started_by_person
        self._origin = "external"

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.begin)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterInvocationEvent, self.finish)

    def _label(self, agent: Any) -> labels.ContextLabel:
        return labels.context_label(
            getattr(agent, "messages", None),
            origin=self._origin,
            injected=injected_sources(agent),
        )

    def begin(self, event: BeforeInvocationEvent) -> None:
        try:
            self._origin = labels.origin_source(bool(self.started_by_person()))
            self.observer.begin()
        except Exception as error:  # noqa: BLE001 - observation never touches the turn
            _LOG.warning("taint: begin failed (%s)", type(error).__name__)

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        try:
            tool_use = event.tool_use if isinstance(event.tool_use, Mapping) else {}
            name = tool_use.get("name")
            if labels.egress_tool(name):
                code = labels.flow_would_hold(name, tool_use.get("input"), self._label(event.agent))
                self.observer.note_call(code)
        except Exception as error:  # noqa: BLE001
            _LOG.warning("taint: before-tool observation failed (%s)", type(error).__name__)

    def finish(self, event: AfterInvocationEvent) -> None:
        try:
            self.observer.finish(self._label(event.agent))
        except Exception as error:  # noqa: BLE001
            _LOG.warning("taint: finish failed (%s)", type(error).__name__)
