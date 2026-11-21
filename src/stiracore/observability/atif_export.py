"""One recorded turn as an ATIF trajectory: a one-way adapter, codes only (#700).

ATIF (Agent Trajectory Interchange Format) is the JSON shape outside tools read for an
agent run. This module maps the turn events of one turn (``turn_events``, ``turns.db``) to
that shape so such a tool can be tried offline. Pure functions, as :mod:`.failure_digest`:
nothing here reads a clock, a store, the network or a model, and nothing is written.
The internal event format is not changed; the mapping lives here only.

**No body is exported.** ATIF expects the prompt, the reply, tool arguments and tool
results; Stira's turn events hold none of the first two and only masked previews of the
others. Every ``message`` is therefore the empty string, every ``arguments`` is ``{}``
and an observation's ``content`` is the tool call's status code. What is read from an
event is an allow-list: ids, numbers and codes from fixed sets (or token-shaped codes the
harness wrote). Previews, names of unknown tools and any other field are never read, and
every string of the result is passed through ``mask_secrets`` once more.

A turn whose recorded taint label (ADR 0022, ``turn.summary.taint.context_trust``) is
``untrusted`` is refused unless the caller passes ``include_untrusted=True``.

The ATIF field names are a minimal subset written without access to the specification:
the output was not checked against it or against a validator (docs/atif-export.md, "Limits").
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from stiracore import __version__

from .hooks import TOOL_DENY_CODES, TOOL_ERROR_CLASSES
from .model_progress import STATUS_CODES, STOP_CODES, call_id, model_id
from .security import mask_secrets
from .turn_autostop import AUTO_STOP_REASONS
from .turn_events import (
    EVENT_TYPES,
    INTERRUPT_REASONS,
    RESERVED_TYPES,
    USAGE_REASONS,
    validate_event,
)

__all__ = [
    "ATIF_SCHEMA_VERSION",
    "EXPORT_VERSION",
    "REFUSALS",
    "ExportRefused",
    "build",
]

ATIF_SCHEMA_VERSION = "ATIF-v1.4"
EXPORT_VERSION = 1
AGENT_NAME = "stiracore"
UNKNOWN_TOOL = "unknown_tool"
UNNAMED_TOOL = "unnamed_tool"
# Why a turn is not exported, as fixed codes.
REFUSALS = frozenset(
    {
        "no_events",  # no usable event of the turn
        "turn_id_mismatch",  # the rows belong to more than one turn
        "turn_id_unsafe",  # the turn id is not one token, or mask_secrets would rewrite it
        "turn_not_completed",  # no turn.completed: the turn may still be running
        "turn_untrusted",  # the recorded taint label is untrusted (include_untrusted=False)
    }
)
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
_TRUSTS = frozenset({"trusted", "untrusted"})


class ExportRefused(ValueError):
    """The turn is not exported; ``code`` is one of :data:`REFUSALS`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _stable(value: Any, pattern: re.Pattern[str]) -> str | None:
    """``value`` when it is one token of ``pattern`` that ``mask_secrets`` leaves alone."""
    if isinstance(value, str) and pattern.fullmatch(value) and mask_secrets(value) == value:
        return value
    return None


def _member(value: Any, allowed: Iterable[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10**12:
        return value
    return None


def _iso(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _put(target: dict, key: str, value: Any) -> None:
    if value is not None:
        target[key] = value


def _mask_strings(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _mask_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_strings(item) for item in value]
    if isinstance(value, str):
        return mask_secrets(value)
    return value


def _usable(events: Iterable[Any]) -> tuple[list[Mapping], int]:
    kept, skipped = [], 0
    for event in events:
        try:
            kept.append(validate_event(event))
        except ValueError:
            skipped += 1
    kept.sort(key=lambda event: event["seq"])
    return kept, skipped


class _Builder:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.current: dict | None = None  # the agent step tool calls attach to
        self.call_ids: dict[Any, str] = {}  # recorded tool_use_id -> exported id
        self.calls: dict[str, tuple[dict, dict]] = {}  # exported id -> (step, tool call)
        self.results: dict[str, dict] = {}  # exported id -> codes of the last update

    def step(self, source: str, ts: Any) -> dict:
        item: dict[str, Any] = {"step_id": len(self.steps) + 1}
        _put(item, "timestamp", _iso(ts))
        item["source"] = source
        item["message"] = ""  # bodies are not exported
        self.steps.append(item)
        return item

    def _call(self, event: Mapping) -> str:
        data = event["data"]
        raw = data.get("tool_use_id")
        key = raw if isinstance(raw, str) else None
        ident = self.call_ids.get(key)
        if ident is None:
            ident = _stable(raw, _ID) or f"call-{len(self.call_ids) + 1}"
            self.call_ids[key] = ident
        if ident not in self.calls:
            if self.current is None:
                self.current = self.step("agent", event["ts"])
            call = {
                "tool_call_id": ident,
                "function_name": _stable(data.get("name"), _TOOL_NAME) or UNNAMED_TOOL,
                "arguments": {},  # arguments are a body: not exported
            }
            self.current.setdefault("tool_calls", []).append(call)
            self.calls[ident] = (self.current, call)
        return ident

    def tool_created(self, event: Mapping) -> None:
        self._call(event)

    def tool_updated(self, event: Mapping) -> None:
        ident = self._call(event)
        data = event["data"]
        codes: dict[str, Any] = {"tool_call_id": ident, "status": data["status"]}
        _put(codes, "error_class", _member(data.get("error_class"), TOOL_ERROR_CLASSES))
        _put(codes, "deny_code", _member(data.get("deny_code"), TOOL_DENY_CODES))
        _put(codes, "duration_ms", _count(data.get("duration_ms")))
        if codes.get("error_class") == UNKNOWN_TOOL:
            # The name of a tool that does not exist is text the model chose.
            self.calls[ident][1]["function_name"] = UNKNOWN_TOOL
        self.results[ident] = codes

    def model_completed(self, event: Mapping) -> None:
        data = event["data"]
        item = self.step("agent", event["ts"])
        _put(item, "model_name", model_id(data.get("model")))
        usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
        metrics: dict[str, int] = {}
        _put(metrics, "prompt_tokens", _count(usage.get("input_tokens")))
        _put(metrics, "completion_tokens", _count(usage.get("output_tokens")))
        if metrics:
            item["metrics"] = metrics
        extra: dict[str, Any] = {}
        _put(extra, "call_id", call_id(data.get("call_id")))
        _put(extra, "duration_ms", _count(data.get("duration_ms")))
        _put(extra, "stop", _member(data.get("stop"), STOP_CODES))
        _put(extra, "status", _member(data.get("status"), STATUS_CODES))
        if data.get("retry") is True:
            extra["retry"] = True
        item["extra"] = {"stira": extra}
        self.current = item

    def finish(self) -> list[dict]:
        for ident, codes in self.results.items():
            item = self.calls[ident][0]
            observation = item.setdefault("observation", {"results": []})
            # ``content`` is the status code of the call, never its output.
            observation["results"].append({"source_call_id": ident, "content": codes["status"]})
            stira = item.setdefault("extra", {}).setdefault("stira", {})
            stira.setdefault("tool_results", []).append(codes)
        return self.steps


def build(events: Iterable[Any], *, include_untrusted: bool = False) -> dict[str, Any]:
    """The ATIF trajectory of one turn from its turn events (any order; sorted by ``seq``).

    An event the turn-event contract refuses is skipped and counted. Raises
    :class:`ExportRefused` with a code of :data:`REFUSALS`.
    """
    rows, skipped = _usable(events)
    if not rows:
        raise ExportRefused("no_events")
    if len({event["turn_id"] for event in rows}) != 1:
        raise ExportRefused("turn_id_mismatch")
    turn_id = _stable(rows[0]["turn_id"], _ID)
    if turn_id is None:
        raise ExportRefused("turn_id_unsafe")
    completed = [event for event in rows if event["type"] == "turn.completed"]
    if not completed:
        raise ExportRefused("turn_not_completed")
    trust = "unknown"
    for event in rows:
        if event["type"] == "turn.summary" and isinstance(event["data"].get("taint"), Mapping):
            trust = _member(event["data"]["taint"].get("context_trust"), _TRUSTS) or "unknown"
    if trust == "untrusted" and not include_untrusted:
        raise ExportRefused("turn_untrusted")

    builder = _Builder()
    approvals: list[dict] = []
    transitions: list[dict] = []
    unmapped: dict[str, int] = {}
    for event in rows:
        kind, data = event["type"], event["data"]
        if kind == "turn.started":
            auto = _stable(data.get("auto_continue"), _ID)
            item = builder.step("user" if auto is None else "system", event["ts"])
            extra: dict[str, Any] = {}
            _put(extra, "prompt_chars", _count(data.get("prompt_chars")))
            _put(extra, "image_count", _count(data.get("image_count")))
            _put(extra, "auto_continue", auto)
            item["extra"] = {"stira": extra}
        elif kind == "model.completed":
            builder.model_completed(event)
        elif kind == "tool_call.created":
            builder.tool_created(event)
        elif kind == "tool_call.updated":
            builder.tool_updated(event)
        elif kind in ("approval.requested", "approval.resolved"):
            entry: dict[str, Any] = {"event": kind.split(".", 1)[1]}
            _put(entry, "request_id", _stable(data.get("request_id"), _ID))
            _put(entry, "decision", _stable(data.get("decision"), _CODE))
            approvals.append(entry)
        elif kind == "breaker.transition":
            entry = {}
            _put(entry, "from", _stable(data.get("from"), _CODE))
            _put(entry, "to", _stable(data.get("to"), _CODE))
            transitions.append(entry)
        elif kind != "turn.completed" and kind in EVENT_TYPES | RESERVED_TYPES:
            unmapped[kind] = unmapped.get(kind, 0) + 1
    steps = builder.finish()

    end = completed[-1]["data"]
    usage = end.get("usage") if isinstance(end.get("usage"), Mapping) else {}
    final: dict[str, Any] = {}
    _put(final, "total_prompt_tokens", _count(usage.get("input_tokens")))
    _put(final, "total_completion_tokens", _count(usage.get("output_tokens")))
    final["total_steps"] = len(steps)
    outcome: dict[str, Any] = {}
    _put(outcome, "stop", _stable(end.get("stop"), _CODE))
    if isinstance(end.get("error"), bool):
        outcome["error"] = end["error"]
    # #680: an automatic stop's fixed code (ADR 0034) passes as well.
    _put(outcome, "reason", _member(end.get("reason"), INTERRUPT_REASONS | AUTO_STOP_REASONS))
    _put(outcome, "usage_reason", _member(end.get("usage_reason"), USAGE_REASONS))
    final["extra"] = {"stira": outcome}

    agent: dict[str, Any] = {"name": AGENT_NAME, "version": __version__}
    _put(agent, "model_name", model_id(end.get("model")))
    document = {
        "schema_version": ATIF_SCHEMA_VERSION,
        "session_id": turn_id,  # turn events carry no session id: one turn, one trajectory
        "agent": agent,
        "steps": steps,
        "final_metrics": final,
        "extra": {
            "stira": {
                "export_version": EXPORT_VERSION,
                "turn_id": turn_id,
                "bodies": "excluded",
                "context_trust": trust,
                "approvals": approvals,
                "breaker_transitions": transitions,
                "unmapped_events": dict(sorted(unmapped.items())),
                "skipped_events": skipped,
            }
        },
    }
    return _mask_strings(document)
