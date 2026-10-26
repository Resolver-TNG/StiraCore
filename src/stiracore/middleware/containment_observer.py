"""Read-only hooks that feed the containment observer (ADR 0023 phase 0, #460-0).

Nothing here changes a tool call: the callbacks only *read* the event (the tool name, its
input, the gate's cancel text, the result) and pass fixed codes to
:class:`stiracore.core.containment.ContainmentObserver`. They never set
``cancel_tool``, never replace ``result`` and never raise (a failure is logged).

Sources (all of them refusals or holds the gate already makes, ADR 0023 §3):

* refused reads — the file tools' read-audit callback (``file_tools.set_read_audit``) is
  wrapped by :func:`tee_read_refusals`, so ``E_READ_SECRET_PATH`` / ``E_READ_OUTSIDE_ROOTS``
  are counted from the same call that writes ``audit.read.denied``;
* a write refused by the write floor or outside the write roots — the fixed texts
  (``READ_ONLY_ERROR`` / ``OUTSIDE_WRITE_ROOTS_ERROR``) or their
  ``[refused by: floor; …`` / ``[refused by: write_roots; …`` detail
  (``identity_paths.refusal_detail``) in an **error** result;
* ``memory_write`` to a layer other than episodic — its fixed refusal text;
* ``E_OWN_DASHBOARD`` — the fixed refusal text of #495 in an error result;
* an egress call the YOLO check sends to a person (``tiers.external_refusal``:
  ``secret_in_args`` / ``credential_by_reference``), evaluated on the call's input;
* every tool call (the burst counter) and the same refused call repeated (an in-memory
  fingerprint, never recorded);
* a fixed phrase list in the successful result of a tool that brings outside content
  (``T``, weight 0).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from stiracore.core.containment import ContainmentObserver
from stiracore.identity_paths import OUTSIDE_WRITE_ROOTS_ERROR, READ_ONLY_ERROR

__all__ = [
    "TEXT_CUES",
    "ContainmentObserverHook",
    "result_codes",
    "tee_read_refusals",
]

_LOG = logging.getLogger(__name__)

_SCAN_CHARS = 4000  # only the head of a result is read
_FLOOR_DETAIL = "[refused by: floor;"
_WRITE_ROOTS_DETAIL = "[refused by: write_roots;"
_MEMORY_LAYER_TEXT = "Memory write denied: only episodic memories may be written"
_OWN_DASHBOARD_TEXT = "E_OWN_DASHBOARD: Stira refuses connections"
_READ_CODES = frozenset({"E_READ_SECRET_PATH", "E_READ_OUTSIDE_ROOTS"})
_EGRESS_HOLDS = frozenset({"secret_in_args", "credential_by_reference"})
# Tools whose result is outside content (ADR 0022 labels them untrusted; until then: the
# external tools, page_check and any MCP tool).
_OUTSIDE_CONTENT_TOOLS = frozenset(
    {"http_request", "web_fetch", "web_search", "rss", "perplexity_search", "page_check"}
)
# ADR 0023 §3 ``T*``: fixed phrases, lower case; counted, never raising.
TEXT_CUES = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard previous instructions",
    "ignore the above instructions",
    "disable the breaker",
    "disable the safety",
    "turn off the breaker",
    "you are now",
    "reveal your system prompt",
    "print your system prompt",
)


def _result_text(result: Any) -> str:
    if not isinstance(result, Mapping):
        return ""
    parts = []
    for block in result.get("content") or ():
        if isinstance(block, Mapping) and isinstance(block.get("text"), str):
            parts.append(block["text"][:_SCAN_CHARS])
        if sum(map(len, parts)) >= _SCAN_CHARS:
            break
    return "\n".join(parts)[:_SCAN_CHARS]


def result_codes(tool_name: Any, result: Any) -> list[str]:
    """The signal codes a refused call's result carries (reads excluded: they come by the
    tee). ``memory_write`` returns its layer refusal as a plain (successful) result that
    starts with a fixed text; every other code is read from an **error** result only."""
    if not isinstance(result, Mapping):
        return []
    text = _result_text(result)
    if tool_name == "memory_write" and text.startswith(_MEMORY_LAYER_TEXT):
        return ["memory_layer"]
    if result.get("status") != "error":
        return []
    codes = []
    stripped = text.strip()
    # The write floor's / write roots' fixed texts, as the write-failure classifier reads
    # them (``write_errors.classify_write_failure``), or their ``[refused by: …]`` detail.
    if _FLOOR_DETAIL in text or stripped.startswith(READ_ONLY_ERROR):
        codes.append("write_floor")
    if _WRITE_ROOTS_DETAIL in text or stripped.startswith(OUTSIDE_WRITE_ROOTS_ERROR):
        codes.append("outside_write_roots")
    if "[E_WRITE_SECRET_PATH]" in text:  # #536: the write side of the secret-path floor
        codes.append("E_WRITE_SECRET_PATH")
    if _OWN_DASHBOARD_TEXT in text:
        codes.append("E_OWN_DASHBOARD")
    return codes


def _fingerprint(tool_name: Any, tool_input: Any) -> str | None:
    try:
        material = json.dumps(
            [tool_name, tool_input], sort_keys=True, default=repr, ensure_ascii=False
        )
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


def _outside_content(tool_name: Any) -> bool:
    if type(tool_name) is not str:
        return False
    if tool_name in _OUTSIDE_CONTENT_TOOLS:
        return True
    try:
        from stiracore.yolo.tiers import mcp_tool

        return bool(mcp_tool(tool_name))
    except Exception:  # noqa: BLE001
        return False


def _has_cue(text: str) -> bool:
    folded = text.casefold()
    return any(cue in folded for cue in TEXT_CUES)


class ContainmentObserverHook(HookProvider):
    """Feed the observer from the tool calls of one agent. Reads events; never writes."""

    def __init__(self, observer: ContainmentObserver) -> None:
        self.observer = observer

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        try:
            tool_use = event.tool_use if isinstance(event.tool_use, Mapping) else {}
            name = tool_use.get("name")
            self.observer.note_tool_call(id(event.agent))
            from stiracore.yolo.tiers import EXTERNAL_TOOLS, external_refusal

            if name in EXTERNAL_TOOLS:
                code = external_refusal(name, tool_use.get("input"), None)
                if code in _EGRESS_HOLDS:
                    self.observer.observe(code)
        except Exception as error:  # noqa: BLE001 - observation never touches the call
            _LOG.warning("containment: before-tool observation failed (%s)", type(error).__name__)

    def after_tool(self, event: AfterToolCallEvent) -> None:
        try:
            tool_use = event.tool_use if isinstance(event.tool_use, Mapping) else {}
            name = tool_use.get("name")
            result = event.result
            codes = result_codes(name, result)
            for code in codes:
                self.observer.observe(code)
            if codes:
                self.observer.note_refused(_fingerprint(name, tool_use.get("input")))
            if (
                isinstance(result, Mapping)
                and result.get("status") == "success"
                and _outside_content(name)
                and _has_cue(_result_text(result))
            ):
                self.observer.observe("text_cue")
        except Exception as error:  # noqa: BLE001
            _LOG.warning("containment: after-tool observation failed (%s)", type(error).__name__)


def tee_read_refusals(
    observer: ContainmentObserver, record: Callable[..., Any] | None
) -> Callable[..., Any]:
    """Wrap the read-audit callback: count the refusal, then call ``record`` unchanged.

    ``record``'s return value and exceptions pass through as before (the file tool already
    guards its call); the observation itself never raises.
    """

    def tee(*, tool_name: Any = None, target: Any = None, reason_code: Any = None) -> Any:
        try:
            if reason_code in _READ_CODES:
                observer.observe(reason_code)
                if isinstance(target, str):
                    observer.note_refused(f"read:{tool_name}:{target}")
        except Exception as error:  # noqa: BLE001
            _LOG.warning("containment: read observation failed (%s)", type(error).__name__)
        if record is None:
            return None
        return record(tool_name=tool_name, target=target, reason_code=reason_code)

    return tee
